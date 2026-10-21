"""The courier worker: its listener loop, the optional local relay, and a clean stop."""

from contextlib import contextmanager
import errno
import fcntl
import json
import os
from pathlib import Path
import signal
import sqlite3
import sys
import threading
import time

from opencode_connect.core import BridgeError
from opencode_connect.courier.server import CourierServer
from opencode_connect.courier.settings import listen_address


def log(message):
    """One line for the service journal. Never message text or credentials."""
    print(time.strftime("%Y-%m-%d %H:%M:%S"), message, file=sys.stderr, flush=True)


class CourierBusy(BridgeError):
    """Another worker, such as the service, owns this queue."""


def serve_until(courier, server, stop, *, backoff=lambda failures: min(30, 2 ** min(failures, 5)),
                healthy=lambda: True, until=lambda: False):
    """One request, one upload, one receipt probe and expiry per tick, until `stop`.

    A storage or network error in a tick is logged and retried after a growing
    wait instead of ending the worker. An upload interrupted by the error stays
    claimed, so the worker releases its own claims again: uploads are idempotent.
    `healthy` turning false without a stop request, a relay thread that died,
    ends the worker so that its service restarts. `until` turning true, as when a
    pairing that `pair` serves in the foreground ends, stops it after a tick.
    """
    failures = 0
    while not stop.is_set() and not until():
        if not healthy() and not stop.is_set():
            raise BridgeError("The local relay stopped unexpectedly; see the error above.")
        try:
            server.handle_request()
            courier.send_once()
            courier.poll_once()
            courier.tidy()
            failures = 0
        except (BridgeError, OSError, sqlite3.Error) as exc:
            failures += 1
            delay = backoff(failures)
            log(f"Worker tick failed ({type(exc).__name__}: {exc}); retrying in {delay} s.")
            stop.wait(delay)
            try:
                courier.recover_outbound()
            except (BridgeError, OSError, sqlite3.Error):
                pass  # The next tick reports a lasting failure.


def listener(courier, listen, stop, *, allow_wildcard=False, retry=5):
    """Bind, waiting while the address is not assigned yet: a user service can start
    before the VPN or Wi-Fi brings it up. None if stopped first."""
    waiting = False
    while not stop.is_set():
        try:
            return CourierServer(courier, listen, allow_wildcard=allow_wildcard)
        except OSError as exc:
            if exc.errno != errno.EADDRNOTAVAIL:
                raise
            if not waiting:
                log(f"Listen address {listen} is not assigned yet; retrying every {retry} s.")
                waiting = True
            stop.wait(retry)
    return None


def relay_worker(stop, *, node=None, open_box=None, adapter=None, retry=10):
    """Deliver this machine's local queue beside the courier until `stop` is set.

    Its mailbox opens in this thread, since a SQLite connection stays in its own.
    A separate relay owning the queue, or a queue that cannot open, is logged when
    that changes and retried: it never ends the courier.
    """
    from opencode_connect.local import mailbox
    open_box = open_box or (lambda: mailbox.LocalMailbox(mailbox.state_root(), node=node))
    adapter = adapter or mailbox.local_adapter()
    reported = None
    while not stop.is_set():
        box, problem = None, None
        try:
            box = open_box()
            mailbox.relay(box, adapter, stop=stop, report=lambda entry: log("Local relay: " + json.dumps(entry)))
        except mailbox.RelayBusy:
            problem = "Another local relay owns this queue; waiting for it to stop."
        except (BridgeError, OSError, sqlite3.Error) as exc:
            problem = f"Local relay unavailable ({type(exc).__name__}: {exc})."
        finally:
            if box is not None:
                box.db.close()
        if problem and problem != reported:
            log(problem)
        reported = problem
        stop.wait(retry)


@contextmanager
def stop_on_sigterm(stop):
    """SIGTERM, which systemd sends to stop the unit, ends the worker between ticks."""
    previous = signal.signal(signal.SIGTERM, lambda signum, frame: stop.set())
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def run_courier(courier, listen, *, allow_wildcard=False, stop=None, relay=None, until=lambda: False):
    """Serve until `stop`; `relay(stop)`, when given, runs beside it in a thread.

    On stop the relay finishes a delivery in progress and is joined before the
    listener and the queue close, so it leaves no message `sending`.
    """
    if listen_address(listen, allow_wildcard)[1] == 0:
        raise BridgeError("The operational listener requires a fixed nonzero port.")
    stop = stop or threading.Event()
    # One coordinator per queue; no recovery of unrelated sending/uncertain rows.
    with courier.lock:
        database = courier.db.execute("PRAGMA database_list").fetchone()[2]
    if not database:
        raise BridgeError("The worker requires a persistent private queue.")
    lock_path = Path(database).parent / "courier.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise CourierBusy("A courier already owns this queue.") from exc
        worker = threading.Thread(target=relay, args=(stop,), name="local-relay") if relay else None
        if worker is not None:
            worker.start()
        try:
            server = listener(courier, listen, stop, allow_wildcard=allow_wildcard)
            if server is None:
                return
            with server:
                courier.recover_outbound()
                server.timeout = 0.2
                print(f"Courier listening on {listen}"
                      + (" and delivering this machine's local queue." if worker else "."), flush=True)
                serve_until(courier, server, stop, healthy=worker.is_alive if worker else lambda: True, until=until)
        finally:
            stop.set()
            if worker is not None:
                worker.join()
    finally:
        os.close(fd)
