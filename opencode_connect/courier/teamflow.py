"""The team commands as the command line runs them: `team invite` and `join`, which
admit a machine, and the owner's and member's other `team` actions."""

from opencode_connect.core import BridgeError
from opencode_connect.courier.pairflow import invite_by_link, serve_while
from opencode_connect.courier.pairing import reach_advice, request_pairing, store_pairing
from opencode_connect.courier.settings import interactive_confirmation

COURIER = "python3 bin/courier.py"
# How long `team invite` and `join` keep serving while the introductions go out.
INTRO_WAIT = 90
RUNNING = f"while both couriers run, as the service does (`{COURIER} service install` starts it at each login)"


def introduce_newcomer(courier, config, name):
    """Keep serving while `name`'s introductions go out, then say how they went."""
    if not courier.introductions(name):
        print(f"{name} is the first member of your team.")
        return
    print(f"Introducing {name} to the team...", flush=True)
    serve_while(courier, config, lambda: "pending" not in courier.introductions(name).values(), INTRO_WAIT)
    states = courier.introductions(name)
    done = sorted(member for member, state in states.items() if state == "done")
    waiting = sorted(member for member, state in states.items() if state == "pending")
    failed = sorted(member for member, state in states.items() if state not in {"done", "pending"})
    if done:
        print(f"{name} is connected with {', '.join(done)}.")
    if waiting:
        print(f"Still introducing {name} to {', '.join(waiting)}: it goes on {RUNNING}. "
              f"`{COURIER} status` shows how it goes.")
    if failed:
        print(f"{name} was not introduced to {', '.join(failed)}: `{COURIER} status` says why.")


def join_team_by_link(courier, config, link, replace):
    """Join the team behind a `team invite` link, after showing who owns it and who is in
    it and asking for one code. Declining stores nothing."""
    if courier.members():
        raise BridgeError("This courier has its own team, and a courier is in one team at a time.")
    pending = request_pairing(courier, link, replace=replace, team=True)
    owner, members = pending["card"]["person"], pending["team"]
    current = courier.owner()
    if current not in (None, owner):
        raise BridgeError(f"This courier is already in {current}'s team; `{COURIER} team leave` first.")
    print(f"{owner}'s team" + (f", with {', '.join(members)}." if members else "; you would be its first member."))
    print(f"Joining lets {owner} add and remove teammates as your contacts. {owner} creates the tokens those "
          f"contacts use, so {owner} could also write to you as one of them. Your own contacts, and anyone you "
          f"unpair, stay out of its reach, and `{COURIER} team leave` ends it.")
    interactive_confirmation(f"Join {owner}'s team")
    start = courier.clock()
    store_pairing(courier, pending, replace=replace)
    print(f"Joined {owner}'s team." + (" Waiting for the introductions..." if members else ""), flush=True)
    seen = lambda: (courier.last_seen(owner) or 0) >= start
    arrived = lambda: [member for member in members if member in courier.settings()["contacts"]]
    serve_while(courier, config, lambda: seen() and len(arrived()) == len(members), INTRO_WAIT)
    print(f"{owner} checked the way back: both ways work." if seen() else
          f"{owner}'s courier has not reached yours yet; keep your courier running so that its introductions "
          "arrive. If it never does, the VPN's access rules (ACLs) or this machine's firewall may block it: "
          + reach_advice(config["url"]))
    connected = arrived()
    if connected:
        print(f"Connected with {', '.join(connected)}.")
    missing = [member for member in members if member not in connected]
    if missing:
        print(f"Not connected with {', '.join(missing)} yet: {owner}'s courier introduces you {RUNNING}. "
              f"`{COURIER} status` shows your contacts.")
    return 0


def team_command(courier, config, args):
    """`team invite`, `remove`, `introduce`, or `leave`; each change after one code."""
    if args.action == "invite":
        return invite_by_link(courier, config, args.for_person, args.replace, team=True,
                              joined=lambda contact: introduce_newcomer(courier, config, contact))
    if args.action == "leave":
        owner = courier.owner()
        if owner is None:
            raise BridgeError("This courier is not in a team.")
        interactive_confirmation(f"Leave {owner}'s team: {owner} can no longer add or remove your contacts, "
                                 "and you keep the ones you have")
        courier.leave_team()
        print(f"Left {owner}'s team. You keep the teammates it introduced; `{COURIER} unpair NAME` removes one.")
        return 0
    name = args.person
    if name not in courier.members():
        raise BridgeError(f"{name} is not in your team.")
    if args.action == "remove":
        interactive_confirmation(f"Remove {name} from your team: the other members drop {name}, "
                                 f"and {name} drops them")
        courier.team_remove_member(name)
        print(f"Removed {name} from your team. Each courier drops it as soon as it can be reached; "
              f"`{COURIER} status` shows what is left. {name} stays your own contact: `{COURIER} unpair {name}` "
              "ends that too.")
        return 0
    interactive_confirmation(f"Introduce {name} again to the members it is not connected with")
    again = courier.introduce_again(name)
    if not again:
        print(f"{name} is connected with every member, or an introduction is under way: `{COURIER} status` shows it.")
        return 0
    print(f"Introducing {name} again to {', '.join(again)}.")
    introduce_newcomer(courier, config, name)
    return 0
