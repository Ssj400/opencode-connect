"""Pairing: two couriers exchange cards after their operators share a one-time code.

The code never travels. Whoever joins proves they know it with an HMAC over their
card, keyed from the code; whoever invited answers with their own card under the
same key, so each side knows the other card came from the code's holder. The
inviter's trust stays provisional until the joiner confirms with its new token;
an offer that lapses without that confirmation undoes it.
"""

import hashlib
import hmac
import ipaddress
import json
import re
import secrets
import time
from urllib.parse import urlsplit

from opencode_connect.core import BridgeError
from opencode_connect.courier.settings import (SOCKET_TIMEOUT, RequestError, contact_url, person,
                                               validate_card)
from opencode_connect.transports import HttpError, http_json

ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford's base 32: no I, L, O, or U.
OFFER_SECONDS = 10 * 60
KDF_ROUNDS = 600_000
MAX_PAIR_BODY = 1024
NONCE = re.compile(r"[0-9a-f]{32}\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
# Carrier-grade NAT, which mesh VPNs use, and IPv6 unique local addresses.
VPN_NETWORKS = (ipaddress.ip_network((0x64400000, 10)), ipaddress.ip_network((0xFC << 120, 7)))


def new_code():
    """60 random bits as 12 Crockford characters, shown as XXXX-XXXX-XXXX."""
    value = secrets.randbits(60)
    characters = "".join(ALPHABET[(value >> shift) & 31] for shift in range(55, -1, -5))
    return "-".join(characters[start:start + 4] for start in (0, 4, 8))


def normal_code(text):
    """Accept lower case, spaces, and dashes, and read O as 0 and I or L as 1."""
    code = str(text).upper().replace("-", "").replace(" ", "").translate(str.maketrans("OIL", "011"))
    if len(code) != 12 or any(character not in ALPHABET for character in code):
        raise BridgeError("A pairing code has 12 letters and digits, such as ABCD-EFGH-JKMN.")
    return code


def pairing_key(code):
    return hashlib.pbkdf2_hmac("sha256", normal_code(code).encode(), b"opencode-connect pairing v1", KDF_ROUNDS)


def seal(key, label, nonce, card):
    """HMAC over one side's card; the label keeps a joiner's MAC from passing as an answer."""
    message = b"\0".join((label, nonce.encode(), json.dumps(card, sort_keys=True, separators=(",", ":")).encode()))
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def same_host(url, peer):
    """Whether a card's URL names the IP its request came from, which also keeps a
    joiner from pointing this courier at another host."""
    try:
        addresses = [ipaddress.ip_address(urlsplit(url).hostname or ""), ipaddress.ip_address(peer)]
    except ValueError:
        return False
    addresses = [address.ipv4_mapped or address if address.version == 6 else address for address in addresses]
    return addresses[0] == addresses[1]


def reach_advice(url):
    """What to check when the courier at `url` does not answer."""
    port = urlsplit(url).port or (443 if url.startswith("https:") else 80)
    return (f"Check that both machines are on the same VPN or LAN, that the VPN's access rules (ACLs) allow "
            f"TCP port {port} between them in both directions, and that the firewall of the machine at {url} "
            f"lets it in. A quick check: `curl -s -o /dev/null -w '%{{http_code}}\\n' {url}/v1/whoami` prints "
            "401 from a machine that can reach that courier, and times out from one that cannot.")


def is_vpn(address):
    return any(ipaddress.ip_address(address) in network for network in VPN_NETWORKS)


def parse_link(link):
    """`http://IP:PORT#CODE`: the inviter's origin, and the code, which stays local."""
    origin, separator, code = str(link).partition("#")
    if not separator:
        raise BridgeError("A pairing link ends with #CODE, as the other person's `pair` printed it.")
    return contact_url(origin), normal_code(code)


class Throttle:
    """Pairing attempts per address and in total over a sliding window, in memory only:
    an attempt never writes to the database."""

    def __init__(self, per_address=10, total=30, window=60, clock=time.monotonic):
        self.per_address, self.total, self.window, self.clock = per_address, total, window, clock
        self.attempts = []

    def allow(self, address):
        now = self.clock()
        self.attempts = [(when, who) for when, who in self.attempts if now - when < self.window]
        if (len(self.attempts) >= self.total
                or sum(who == address for _, who in self.attempts) >= self.per_address):
            return False
        self.attempts.append((now, address))
        return True


class PairingMixin:
    """The inviting side of pairing, for Courier, whose single offer row it owns."""

    def offer(self, name=None, *, replace=False):
        """Open the one offer for OFFER_SECONDS and return its code; only the derived key is kept."""
        if name is not None:
            person(name)
        code = new_code()
        key = pairing_key(code)
        with self.transaction():
            revoked = self._expire_offer_locked()
            row = self.db.execute("SELECT state FROM courier_offer WHERE id=1").fetchone()
            if row is not None and row[0] == "provisional":
                raise BridgeError("Another pairing waits for its confirmation; try again in a few minutes.")
            self.db.execute("INSERT OR REPLACE INTO courier_offer(id, key, expires, name, replacing, state) "
                            "VALUES(1,?,?,?,?,'open')", (key.hex(), self.clock() + OFFER_SECONDS, name, int(replace)))
        if revoked:
            self.sync_config()
        return code

    def offer_open(self):
        """Read only, for the listener to answer 404 before reading a body. A used offer
        still counts, so that a joiner that lost its answer gets it again."""
        with self.lock:
            return self.db.execute("SELECT 1 FROM courier_offer WHERE id=1 AND expires>?",
                                   (self.clock(),)).fetchone() is not None

    def join(self, payload, peer):
        """Answer a joiner that proved the code: provisional trust, and this side's card.

        A wrong code is refused before any transaction, so it writes nothing. The
        same request again, after a lost answer, gets the same answer.
        """
        if (not isinstance(payload, dict) or set(payload) != {"nonce", "card", "mac"}
                or not isinstance(payload["nonce"], str) or not NONCE.fullmatch(payload["nonce"])
                or not isinstance(payload["mac"], str) or not DIGEST.fullmatch(payload["mac"])):
            raise RequestError(400, "Invalid pairing request.")
        try:
            card = validate_card(payload["card"])
        except BridgeError as exc:
            raise RequestError(400, "Invalid pairing card.") from exc
        nonce, request = payload["nonce"], digest(json.dumps(payload, sort_keys=True))
        with self.lock:
            row = self.db.execute("SELECT key FROM courier_offer WHERE id=1 AND expires>?", (self.clock(),)).fetchone()
        if row is None:
            raise RequestError(404, "No pairing offer is open.")
        key = bytes.fromhex(row[0])
        if not hmac.compare_digest(seal(key, b"join", nonce, card), payload["mac"]):
            raise RequestError(403, "The pairing code does not match.")
        with self.transaction():
            offer = self.db.execute("SELECT key, name, replacing, state, nonce, request, response FROM courier_offer "
                                    "WHERE id=1 AND expires>?", (self.clock(),)).fetchone()
            if offer is None or offer[0] != row[0]:
                raise RequestError(404, "No pairing offer is open.")
            _, wanted, replacing, state, used, previous_request, response = offer
            if state != "open":
                if used == nonce and previous_request == request and response:
                    return json.loads(response)
                raise RequestError(409, "This pairing offer was already used.")
            name, config = card["person"], self.settings()
            if name == config["person"]:
                raise RequestError(409, "A courier cannot pair with itself.")
            if wanted and name != wanted:
                raise RequestError(403, f"This offer is for {wanted}.")
            if not same_host(card["url"], peer):
                raise RequestError(400, f"The card's URL must use the address this request came from, {peer}.")
            if (name in config["tokens"] or name in config["contacts"]) and not replacing:
                raise RequestError(409, f"{name} is already a contact; pair again with --replace.")
            previous = {"token": config["tokens"].get(name), "card": config["contacts"].get(name)}
            token = secrets.token_urlsafe(32)
            config["tokens"][name], config["contacts"][name] = digest(token), card
            self.save_settings(config)
            own = {"person": config["person"], "url": config["url"], "token": token}
            answer = {"card": own, "mac": seal(key, b"offer", nonce, own)}
            # The answer keeps this token in clear until confirmation, for a repeated request.
            self.db.execute("UPDATE courier_offer SET state='provisional', nonce=?, contact=?, request=?, response=?, "
                            "previous=? WHERE id=1", (nonce, name, request, json.dumps(answer), json.dumps(previous)))
        self.sync_config()
        return answer

    def confirm_pairing(self, authorization, payload, *, rate_checked=False):
        """The joiner, with its new token, confirms the card it received; repeats are harmless."""
        if not isinstance(payload, dict) or set(payload) != {"nonce"} or not NONCE.fullmatch(str(payload["nonce"])):
            raise RequestError(400, "Invalid pairing confirmation.")
        with self.transaction():
            revoked = self._expire_offer_locked()
            if not revoked:
                name = self.authenticate(authorization) if rate_checked else self.check_request(authorization)
                # The answer's token is no longer needed in clear; the CLI gets a minute to report.
                confirmed = self.db.execute(
                    "UPDATE courier_offer SET state='confirmed', response=NULL, expires=max(expires, ?) "
                    "WHERE id=1 AND state!='open' AND nonce=? AND contact=?",
                    (self.clock() + 60, payload["nonce"], name)).rowcount
                if confirmed:
                    self.note_peer(name)
                    config = self.settings()
        if revoked:
            self.sync_config()
            raise RequestError(404, "The pairing offer lapsed before its confirmation.")
        if not confirmed:
            raise RequestError(404, "No pairing waits for this confirmation.")
        return {"person": config["person"], "paired": True}

    def last_seen(self, name):
        with self.lock:
            row = self.db.execute("SELECT last_seen FROM courier_peers WHERE contact=?", (name,)).fetchone()
        return None if row is None else row[0]

    def offer_state(self):
        """The offer's state and contact, or None once it lapsed or was closed."""
        with self.transaction():
            revoked = self._expire_offer_locked()
            row = self.db.execute("SELECT state, contact FROM courier_offer WHERE id=1").fetchone()
        if revoked:
            self.sync_config()
        return None if row is None else {"state": row[0], "contact": row[1]}

    def close_offer(self):
        """End the offer now; an unconfirmed pairing is undone as if it had lapsed."""
        with self.transaction():
            revoked = self._expire_offer_locked(now=float("inf"))
        if revoked:
            self.sync_config()

    def _expire_offer_locked(self, now=None):
        """Delete a lapsed offer, undoing its provisional trust. True if contacts changed."""
        row = self.db.execute("SELECT state, contact, previous FROM courier_offer WHERE id=1 AND expires<=?",
                              (self.clock() if now is None else now,)).fetchone()
        if row is None:
            return False
        self.db.execute("DELETE FROM courier_offer WHERE id=1")
        if row[0] != "provisional":
            return False
        name, previous = row[1], json.loads(row[2])
        if previous["token"] is None and previous["card"] is None:
            self._untrust_locked(name)
        else:
            self._restore_contact_locked(name, previous)
        return True

    def _restore_contact_locked(self, name, previous):
        config = self.settings()
        for key, value in (("tokens", previous["token"]), ("contacts", previous["card"])):
            if value is None:
                config[key].pop(name, None)
            else:
                config[key][name] = value
        self.save_settings(config)


def join_offer(courier, link, *, replace=False, attempts=3, wait=1.0):
    """Pair with the courier behind a link that its `pair` printed. Returns its name.

    Trust is stored only after the inviter proved it knows the code, and undone if
    the inviter does not take the confirmation.
    """
    origin, code = parse_link(link)
    config = courier.settings()
    known = [name for name, card in config["contacts"].items() if card["url"] == origin]
    if known and not replace:
        raise BridgeError(f"{known[0]} is already a contact at {origin}; pair again with --replace.")
    key = pairing_key(code)
    token, nonce = secrets.token_urlsafe(32), secrets.token_hex(16)
    own = {"person": config["person"], "url": config["url"], "token": token}
    try:
        answer = retrying(lambda: http_json(origin + "/v1/pair", method="POST", timeout=SOCKET_TIMEOUT,
                                            data={"nonce": nonce, "card": own, "mac": seal(key, b"join", nonce, own)}),
                          attempts, wait)
    except HttpError:
        raise  # It answered: the code, the offer, or the address was refused.
    except BridgeError as exc:
        raise BridgeError(f"No answer from {origin}: {str(exc).rstrip('.')}. {reach_advice(origin)}") from exc
    try:
        card = validate_card(answer["card"])
        valid = hmac.compare_digest(seal(key, b"offer", nonce, card), str(answer["mac"]))
    except (BridgeError, KeyError, TypeError):
        valid = False
    if not valid or card["url"] != origin:
        raise BridgeError("The courier at this link did not prove it knows the code; nothing was stored.")
    name = card["person"]
    with courier.transaction():
        config = courier.settings()
        if name == config["person"]:
            raise BridgeError("A courier cannot pair with itself.")
        if (name in config["tokens"] or name in config["contacts"]) and not replace:
            raise BridgeError(f"{name} is already a contact; pair again with --replace.")
        previous = {"token": config["tokens"].get(name), "card": config["contacts"].get(name)}
        config["tokens"][name], config["contacts"][name] = digest(token), card
        courier.save_settings(config)
    courier.sync_config()
    try:
        retrying(lambda: http_json(origin + "/v1/pair/ack", method="POST", data={"nonce": nonce},
                                   headers={"Authorization": "Bearer " + card["token"]}, timeout=SOCKET_TIMEOUT),
                 attempts, wait)
    except BridgeError as exc:
        with courier.transaction():
            if previous["token"] is None and previous["card"] is None:
                courier._untrust_locked(name)
            else:
                courier._restore_contact_locked(name, previous)
        courier.sync_config()
        raise BridgeError(f"{name} did not take the confirmation ({exc}), so the pairing was undone here; "
                          "ask for a new link.") from exc
    return name


def retrying(request, attempts, wait):
    """Repeat a request that failed in the network: both pairing requests are idempotent.
    An HTTP answer, such as a refused code, is final."""
    for attempt in range(attempts):
        try:
            return request()
        except HttpError:
            raise
        except BridgeError:
            if attempt == attempts - 1:
                raise
            time.sleep(wait)
