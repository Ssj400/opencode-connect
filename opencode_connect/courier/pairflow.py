"""The two sides of `pair` as the command line runs them: printing a link and
waiting for the joiner, or using a link; each checks that both ways work."""

import threading
import time

from opencode_connect.core import BridgeError
from opencode_connect.courier.pairing import OFFER_SECONDS, is_vpn, join_offer, reach_advice
from opencode_connect.courier.settings import SOCKET_TIMEOUT, listen_address
from opencode_connect.courier.worker import CourierBusy, run_courier
from opencode_connect.transports import http_json

WAY_BACK_SECONDS = 30


def serve_while(courier, config, done, seconds, poll=0.5):
    """Answer requests until `done()`, for at most `seconds`: in the foreground when no
    worker runs, else by waiting while the worker, such as the service, answers."""
    stop = threading.Event()
    timer = threading.Timer(seconds, stop.set)
    timer.daemon = True
    timer.start()
    try:
        run_courier(courier, config["listen"], stop=stop, until=done)
    except CourierBusy:
        while not done() and not stop.is_set():
            time.sleep(poll)
    finally:
        timer.cancel()


def way_back(card, name, attempts=10, wait=1.0):
    """Whether the contact's courier answers as them with the token it gave this side."""
    for attempt in range(attempts):
        try:
            identity = http_json(card["url"] + "/v1/whoami", headers={"Authorization": "Bearer " + card["token"]},
                                 timeout=SOCKET_TIMEOUT)
            return isinstance(identity, dict) and identity.get("person") == name
        except BridgeError:
            if attempt < attempts - 1:
                time.sleep(wait)
    return False


def invite_by_link(courier, config, name, replace, *, team=False, joined=lambda contact: None):
    """Open an offer, print its link, and wait for someone to pair before it lapses. A
    team offer admits them to this courier's team; `joined(contact)` then follows."""
    code = courier.team_invite(name, replace=replace) if team else courier.offer(name, replace=replace)
    host = listen_address(config["listen"], allow_wildcard=True)[0]
    print(f"{'Team' if team else 'Pairing'} offer from {config['person']} at {config['listen']}, "
          f"open for {OFFER_SECONDS // 60} minutes.")
    print("On the other person's machine, run:")
    print(f"  python3 bin/courier.py {'join' if team else 'pair'} '{config['url']}#{code}'")
    print("Send it privately: whoever uses it first " + ("joins your team." if team else "becomes a contact."))
    if not is_vpn(host):
        print(f"Warning: {host} is not in a VPN range. Messages travel unencrypted, so other devices "
              "on this network could read them.")
    print("Waiting...", flush=True)
    finished = False
    try:
        serve_while(courier, config, lambda: (courier.offer_state() or {}).get("state") in {None, "confirmed"},
                    OFFER_SECONDS + 5)
        state = courier.offer_state()
        finished = True
    finally:
        if not finished:
            courier.close_offer()  # Interrupted: undo a pairing that nobody confirmed.
    courier.close_offer()
    if state is None or state["state"] != "confirmed":
        print("The offer lapsed; nobody paired.")
        return 1
    contact = state["contact"]
    card = courier.settings()["contacts"][contact]
    if way_back(card, contact):
        print(f"Paired with {contact}, both ways.")
    else:
        print(f"Paired with {contact}, but their courier did not answer at {card['url']}, so messages to "
              f"{contact} wait until it can be reached. " + reach_advice(card["url"]))
    joined(contact)
    return 0


def join_by_link(courier, config, link, replace):
    start = courier.clock()
    name = join_offer(courier, link, replace=replace)
    print(f"Paired with {name}. Waiting for {name} to check the way back...", flush=True)
    seen = lambda: (courier.last_seen(name) or 0) >= start
    serve_while(courier, config, seen, WAY_BACK_SECONDS)
    print(f"{name} checked the way back: both ways work." if seen() else
          f"{name}'s courier has not reached yours yet; keep your courier running so that their messages "
          "arrive. If it never does, the VPN's access rules (ACLs) or this machine's firewall may block it: "
          + reach_advice(config["url"]))
    return 0
