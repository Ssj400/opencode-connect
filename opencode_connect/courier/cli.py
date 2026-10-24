"""Opt-in, consent-gated person-to-person text courier over a private LAN or VPN."""

import argparse
import json
from pathlib import Path
import sqlite3
import sys
import threading
import time

from opencode_connect.core import BridgeError
from opencode_connect.courier.app import Courier, local_target
from opencode_connect.courier.doctor import doctor
from opencode_connect.courier.notify import desktop_notification
from opencode_connect.courier.pairing import OFFER_SECONDS, is_vpn, join_offer, reach_advice
from opencode_connect.courier.service import restart_service, service_command
from opencode_connect.courier.settings import (SOCKET_TIMEOUT, clean_display, contact_url, default_config_path,
                                               interactive_confirmation, listen_address, read_config,
                                               require_terminal, write_config)
from opencode_connect.courier.setup import firewall_for, setup
from opencode_connect.courier.status import status
from opencode_connect.courier.worker import CourierBusy, relay_worker, run_courier, stop_on_sigterm
from opencode_connect.transports import http_json

WAY_BACK_SECONDS = 30


def real_queue(config_path):
    from opencode_connect.local.mailbox import LocalMailbox, state_root
    return LocalMailbox(state_root())


def approve_held(courier, message_id=None, target=None):
    """Show what would be approved, then ask for one code for all of it.

    Only the messages shown are approved, each to the destination shown: a reply
    goes to the session that asked, any other message to its contact's route.
    """
    if message_id is None and target is not None:
        raise BridgeError("--to needs a message ID; without one, each message goes to its own destination.")
    held = courier.inbox()
    if message_id is not None:
        held = [row for row in held if row["id"] == message_id]
        if not held:
            raise BridgeError("Only a held, unexpired message can be approved.")
        if target is not None:
            held[0]["suggested_target"] = local_target(target)
        elif held[0]["suggested_target"] is None:
            raise BridgeError("This message has no destination: name one with --to, or set its contact's route.")
    for row in held:
        print(json.dumps({"id": row["id"], "person": row["person"], "text": row["text"], "to": row["suggested_target"]},
                         ensure_ascii=False))
    plan = [(row["id"], row["suggested_target"]) for row in held if row["suggested_target"]]
    if plan:
        targets = ", ".join(sorted({target for _, target in plan}))
        interactive_confirmation(f"Approve {len(plan)} message(s) shown above, to {targets}")
        approved = 0
        for result in courier.approve_all(plan):
            if result["state"] == "approved":
                approved += 1
            else:
                print(clean_display(f"Skipped {result['id']}: {result['reason']}"))
        print(f"{approved} message(s) approved for local delivery; not proof of session reception.")
    if len(plan) < len(held):
        print(f"{len(held) - len(plan)} message(s) without a destination stay held: "
              "approve each with `approve ID --to SESSION`, or set its contact's route.")
    elif not held:
        print("No held messages.")


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


def invite_by_link(courier, config, name, replace):
    """Open an offer, print its link, and wait for someone to pair before it lapses."""
    code = courier.offer(name, replace=replace)
    host = listen_address(config["listen"], allow_wildcard=True)[0]
    print(f"Pairing offer from {config['person']} at {config['listen']}, open for {OFFER_SECONDS // 60} minutes.")
    print("On the other person's machine, run:")
    print(f"  python3 bin/courier.py pair '{config['url']}#{code}'")
    print("Send it privately: whoever uses it first becomes a contact.")
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


def main(argv=None, *, queue_factory=real_queue):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=default_config_path())
    # Without a command, a terminal gets the menu; anything else, the usual usage error.
    sub = parser.add_subparsers(dest="command")
    first = sub.add_parser("setup", help="Choose a name and address, check this machine, and say what is left to do")
    first.add_argument("--allow-172", action="store_true",
                       help="On WSL, also offer 172.16/12 addresses, for a LAN that uses that range")
    sub.add_parser("status", help="Who this courier is, whether it runs, and what waits; reads only")
    init = sub.add_parser("init", help="Create a private courier identity interactively")
    init.add_argument("--person", required=True)
    init.add_argument("--listen", required=True)
    init.add_argument("--url", required=True, help="Reachable HTTP(S) origin included in invitation cards")
    run = sub.add_parser("run", help="Serve inbound messages and process outbound person messages")
    run.add_argument("--allow-wildcard", action="store_true")
    run.add_argument("--relay", action="store_true",
                     help="Also deliver this machine's local queue, instead of a separate bin/local_bridge.py relay")
    service = sub.add_parser("service", help="Optional systemd user service that runs `run --relay` at login")
    service.add_argument("action", choices=("install", "uninstall", "start", "stop", "status", "logs"),
                         help="install writes the unit and starts it; status and logs only read")
    address = sub.add_parser("address", help="Change where this courier listens and the URL its cards give")
    address.add_argument("--listen", required=True)
    address.add_argument("--url", required=True, help="Reachable HTTP(S) origin included in invitation cards")
    address.add_argument("--allow-wildcard", action="store_true")
    diagnostic = sub.add_parser("doctor", help="Read-only state checks and bounded authenticated identity probes")
    diagnostic.add_argument("--offline", action="store_true", help="Skip all network probes")
    invite = sub.add_parser("invite", help="Issue a token and display its card once")
    invite.add_argument("person")
    trust = sub.add_parser("trust", help="Accept an independently received contact card")
    trust.add_argument("person")
    trust.add_argument("--card", required=True, help="JSON card, or '-' to paste privately in this terminal")
    untrust = sub.add_parser("untrust", help="Revoke contact and cancel unsent messages")
    untrust.add_argument("person")
    pair = sub.add_parser("pair", help="Pair with another person's courier: print a link, or use theirs")
    pair.add_argument("link", nargs="?", help="The link the other person's `pair` printed; without it, print one")
    pair.add_argument("--for", dest="for_person", help="Accept only this person's courier")
    pair.add_argument("--replace", action="store_true", help="Pair again with someone who is already a contact")
    unpair = sub.add_parser("unpair", help="Stop exchanging messages with a contact, in both directions")
    unpair.add_argument("person")
    sub.add_parser("contacts", help="List contact direction permissions and routes, without tokens")
    contact = sub.add_parser("contact", help="Choose the local session a contact's messages go to")
    contact.add_argument("person")
    contact.add_argument("--to", required=True, help="A session on this machine: node:ses_ID or claude:UUID")
    mode = contact.add_mutually_exclusive_group()
    mode.add_argument("--direct", action="store_true",
                      help="Deliver without approval, up to 20 messages an hour; revoke it with --ask")
    mode.add_argument("--ask", action="store_true", help="Hold each message for approval (the default)")
    inbox = sub.add_parser("inbox", help="Show sanitized held messages without approving them")
    inbox.add_argument("--watch", action="store_true")
    approve = sub.add_parser("approve", help="Release held messages to local sessions, after one confirmation")
    approve.add_argument("id", nargs="?", help="One message; without it, every held message that has a destination")
    approve.add_argument("--to", help="Destination for that message, instead of the asking session or the route")
    reject = sub.add_parser("reject", help="Reject one held message")
    reject.add_argument("id")
    args = parser.parse_args(argv)
    if args.command is None:
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            parser.error("a command is required; in a terminal, running it without one opens a menu")
        from opencode_connect.courier.menu import run_menu
        return run_menu(args.config, queue_factory)
    try:
        if args.command == "doctor":
            findings = doctor(args.config, network=not args.offline)
            print(json.dumps(findings, ensure_ascii=False))
            return 1 if any(row["status"] == "FAIL" for row in findings) else 0
        if args.command == "status":
            code, lines = status(args.config)
            print("\n".join(lines))
            return code
        if args.command == "address":
            listen_address(args.listen, args.allow_wildcard)
            contact_url(args.url)
            interactive_confirmation(f"Confirm address: listen on {args.listen}, reachable at {args.url}")
        elif args.command in {"approve", "contact", "pair", "setup", "unpair"}:
            require_terminal()  # The code is asked once it is clear what would change.
        elif (args.command in {"init", "invite", "trust", "untrust", "reject"}
                or args.command == "service" and args.action in {"install", "uninstall", "start", "stop"}):
            detail = args.command
            if args.command == "reject":
                detail += f" {args.id}"
            elif args.command == "service":
                detail += " " + args.action
            elif hasattr(args, "person"):
                detail += " " + args.person
            interactive_confirmation("Confirm " + detail)
        if args.command == "init":
            if args.config.exists() or args.config.is_symlink():
                raise BridgeError("Courier configuration already exists; it was not overwritten.")
            config = {"person": args.person, "listen": args.listen, "url": args.url, "tokens": {}, "contacts": {}}
            write_config(args.config, config)
            print("Private courier configuration created; no listener was started.")
            return 0
        if args.command == "service":
            return service_command(args.action, args.config)
        if args.command == "setup":
            return setup(args.config, allow_172=args.allow_172)
        config = read_config(args.config)
        queue = queue_factory(args.config)
        try:
            readdress = {"listen": args.listen, "url": args.url} if args.command == "address" else None
            courier = Courier(queue, config, config_path=args.config, notify=desktop_notification, readdress=readdress)
            if args.command == "invite":
                print(json.dumps(courier.invite(args.person)))
            elif args.command == "trust":
                if args.card == "-":
                    import getpass
                    raw = getpass.getpass("Paste contact card (hidden): ")
                else:
                    raw = args.card
                try:
                    card = json.loads(raw)
                except (ValueError, UnicodeError) as exc:
                    raise BridgeError("Invalid contact card JSON.") from exc
                courier.trust(args.person, card)
                print("Contact trusted.")
            elif args.command == "untrust":
                courier.untrust(args.person)
                print("Contact revoked; unsent messages cancelled.")
            elif args.command == "unpair":
                if args.person not in {row["person"] for row in courier.contacts()}:
                    raise BridgeError(f"{args.person} is not a contact.")
                interactive_confirmation(f"Confirm unpair {args.person}: revoke both directions, cancel unsent "
                                         "messages, and remove the route")
                courier.untrust(args.person)
                print(f"Unpaired {args.person}: their token no longer works here, so what they send fails at once, "
                      "and unsent messages were cancelled.")
                print(f"Ask {args.person} to run `python3 bin/courier.py unpair {config['person']}` too.")
            elif args.command == "contacts":
                print(json.dumps(courier.contacts()))
            elif args.command == "inbox":
                while True:
                    for row in courier.inbox():
                        print(json.dumps(row, ensure_ascii=False), flush=True)
                    if not args.watch:
                        break
                    time.sleep(2)
            elif args.command == "address":
                print(f"Listening on {args.listen}, reachable at {args.url}.")
                print("The courier service was restarted." if restart_service() else
                      "If `courier.py run` is running, restart it to use the new address.")
                firewall_for(args.listen)
                print("Your contacts' couriers are told the new URL as soon as each can be reached; "
                      "`status` shows who has not received it yet, and who must pair again because "
                      "their courier is older.")
            elif args.command == "pair":
                if args.link is None:
                    return invite_by_link(courier, config, args.for_person, args.replace)
                if args.for_person is not None:
                    raise BridgeError("--for applies when this side prints the link.")
                return join_by_link(courier, config, args.link, args.replace)
            elif args.command == "contact":
                from opencode_connect.local.mailbox import canonical
                target = local_target(canonical(args.to, queue.node))
                mode = "direct" if args.direct else "ask"
                interactive_confirmation(f"Confirm contact {args.person} to {target}, " + (
                    "delivered without approval" if mode == "direct" else "held for approval"))
                courier.set_route(args.person, target, mode)
                print(f"Messages from {args.person} now go to {target}"
                      + (" without approval." if mode == "direct" else " after approval."))
            elif args.command == "approve":
                approve_held(courier, args.id, args.to)
            elif args.command == "reject":
                courier.reject(args.id)
                print("Message rejected.")
            elif args.command == "run":
                stop = threading.Event()
                relay = (lambda event: relay_worker(event, node=queue.node)) if args.relay else None
                with stop_on_sigterm(stop):
                    run_courier(courier, config["listen"], allow_wildcard=args.allow_wildcard,
                                stop=stop, relay=relay)
        finally:
            queue.db.close()
        return 0
    except (BridgeError, OSError, sqlite3.Error) as exc:
        # Avoid disclosing config content or credentials through parser tracebacks.
        message = str(exc) if isinstance(exc, BridgeError) else "Courier storage or listener unavailable."
        print(clean_display(message), file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        return 130
