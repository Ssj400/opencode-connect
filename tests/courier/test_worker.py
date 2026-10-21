"""The courier worker loop, its run lifecycle, and the local relay thread."""

import errno
from pathlib import Path
import signal
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, Mock, patch

from opencode_connect.core import BridgeError
from opencode_connect.courier.app import Courier
from opencode_connect.courier.cli import main
from opencode_connect.courier.settings import write_config
from opencode_connect.courier.worker import listener, relay_worker, run_courier, serve_until, stop_on_sigterm
from opencode_connect.local import mailbox
from tests.courier.fakes import FakeQueue, configuration


class WorkerLoopTests(unittest.TestCase):
    def loop(self, ticks_before_stop, **failures):
        stop, server, worker, ticks = threading.Event(), Mock(), MagicMock(), []

        def handle():
            ticks.append(1)
            if len(ticks) == ticks_before_stop:
                stop.set()

        server.handle_request.side_effect = handle
        for name, side_effect in failures.items():
            getattr(worker, name).side_effect = side_effect
        return stop, server, worker, ticks

    def test_a_failing_tick_is_logged_and_the_worker_keeps_going(self):
        stop, server, worker, ticks = self.loop(3, send_once=[sqlite3.OperationalError("database is locked"), 0, 0])
        with patch("opencode_connect.courier.worker.log") as log:
            serve_until(worker, server, stop, backoff=lambda failures: 0)
        self.assertEqual(len(ticks), 3)
        log.assert_called_once()
        self.assertIn("database is locked", log.call_args.args[0])
        worker.recover_outbound.assert_called_once()  # Owned claims are released again.

    def test_unexpected_errors_are_not_swallowed(self):
        stop, server, worker, _ = self.loop(5, poll_once=ValueError("a bug"))
        with patch("opencode_connect.courier.worker.log"), self.assertRaises(ValueError):
            serve_until(worker, server, stop, backoff=lambda failures: 0)

    def test_a_relay_that_ended_on_its_own_ends_the_worker(self):
        stop, server, worker, _ = self.loop(5)
        with self.assertRaisesRegex(BridgeError, "local relay stopped"):
            serve_until(worker, server, stop, healthy=lambda: False)
        server.handle_request.assert_not_called()
        stop.set()  # A relay that ended because of a stop request is not a failure.
        serve_until(worker, server, stop, healthy=lambda: False)


class CourierRunTests(unittest.TestCase):
    """The worker's lifecycle, with the listener replaced: no port is opened."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="courier-run-")
        self.addCleanup(folder.cleanup)
        self.queue = FakeQueue(Path(folder.name) / "queue.sqlite3")
        self.addCleanup(self.queue.db.close)
        self.app = Courier(self.queue, configuration())

    def test_the_relay_runs_beside_the_listener_and_ends_before_the_worker_returns(self):
        started, finished, stopped = threading.Event(), threading.Event(), []

        def relay(stop):
            started.set()
            stopped.append(stop.wait(2))
            time.sleep(0.05)  # A delivery finishing after the stop request.
            finished.set()

        def serve(app, server, stop, *, healthy):
            self.assertTrue(started.wait(2))
            self.assertTrue(healthy())

        with patch("opencode_connect.courier.worker.CourierServer"), \
                patch("opencode_connect.courier.worker.serve_until", side_effect=serve), \
                patch("builtins.print") as banner:
            run_courier(self.app, "127.0.0.1:8765", relay=relay)
        self.assertEqual(stopped, [True])
        self.assertTrue(finished.is_set())  # The caller closes the queue only after this.
        self.assertIn("delivering this machine's local queue", banner.call_args.args[0])

    def test_a_stop_before_the_address_is_assigned_ends_cleanly(self):
        stop, relayed = threading.Event(), []
        with patch("opencode_connect.courier.worker.listener", side_effect=lambda *args, **kwargs: stop.set()), \
                patch("opencode_connect.courier.worker.serve_until") as serve:
            run_courier(self.app, "127.0.0.1:8765", stop=stop, relay=relayed.append)
        serve.assert_not_called()
        self.assertEqual(relayed, [stop])

    def test_the_listener_waits_for_its_address_to_be_assigned(self):
        stop = threading.Event()
        unassigned = OSError(errno.EADDRNOTAVAIL, "Cannot assign requested address")
        with patch("opencode_connect.courier.worker.CourierServer", side_effect=[unassigned, unassigned, "server"]), \
                patch("opencode_connect.courier.worker.log") as log:
            self.assertEqual(listener(self.app, "127.0.0.1:8765", stop, retry=0), "server")
        log.assert_called_once()
        self.assertIn("not assigned yet", log.call_args.args[0])
        stop.set()
        with patch("opencode_connect.courier.worker.CourierServer", side_effect=[unassigned]):
            self.assertIsNone(listener(self.app, "127.0.0.1:8765", stop, retry=0))

    def test_a_port_in_use_is_not_retried(self):
        with patch("opencode_connect.courier.worker.CourierServer", side_effect=[OSError(errno.EADDRINUSE, "Address in use")]), \
                self.assertRaises(OSError):
            listener(self.app, "127.0.0.1:8765", threading.Event(), retry=0)

    def test_sigterm_requests_a_clean_stop_and_the_handler_is_restored(self):
        stop, before = threading.Event(), signal.getsignal(signal.SIGTERM)
        with stop_on_sigterm(stop):
            signal.raise_signal(signal.SIGTERM)
            self.assertTrue(stop.is_set())
        self.assertIs(signal.getsignal(signal.SIGTERM), before)

    def test_cli_run_relay_gives_the_relay_this_queue_node(self):
        with tempfile.TemporaryDirectory(prefix="courier-run-cli-") as folder:
            path = Path(folder) / "courier.json"
            write_config(path, configuration())
            self.queue.node = "linux"
            with patch("opencode_connect.courier.cli.run_courier") as run, \
                    patch("opencode_connect.courier.cli.relay_worker") as worker:
                self.assertEqual(main(["--config", str(path), "run", "--relay"],
                                              queue_factory=lambda config_path: self.queue), 0)
                event = run.call_args.kwargs["stop"]
                run.call_args.kwargs["relay"](event)
            worker.assert_called_once_with(event, node="linux")


class RelayWorkerTests(unittest.TestCase):
    def test_a_separate_relay_is_waited_for_and_reported_once(self):
        stop, boxes = threading.Event(), []

        def open_box():
            boxes.append(Mock())
            return boxes[-1]

        calls = []

        def relay(box, adapter, **kwargs):
            calls.append(box)
            if len(calls) < 3:
                raise mailbox.RelayBusy("A local relay is already running.")
            stop.set()

        with patch("opencode_connect.local.mailbox.relay", side_effect=relay), \
                patch("opencode_connect.courier.worker.log") as log:
            relay_worker(stop, open_box=open_box, adapter=Mock(), retry=0)
        self.assertEqual(calls, boxes)
        log.assert_called_once()
        self.assertIn("Another local relay", log.call_args.args[0])
        for box in boxes:
            box.db.close.assert_called_once()

    def test_a_queue_that_cannot_open_is_retried(self):
        stop, box = threading.Event(), Mock()
        with patch("opencode_connect.local.mailbox.relay", side_effect=lambda *a, **k: stop.set()), \
                patch("opencode_connect.courier.worker.log") as log:
            relay_worker(stop, open_box=Mock(side_effect=[BridgeError("Queue contract differs."), box]),
                                 adapter=Mock(), retry=0)
        self.assertIn("Queue contract differs.", log.call_args.args[0])
        box.db.close.assert_called_once()

    def test_a_stop_during_a_delivery_leaves_nothing_sending(self):
        with tempfile.TemporaryDirectory(prefix="courier-relay-") as folder:
            root, stop, failures = Path(folder), threading.Event(), []
            box = mailbox.LocalMailbox(root, node="linux")
            message = box.enqueue("ses_example", "linux:ses_target", "hello")["id"]
            box.db.close()
            adapter = Mock(supported_deliveries=frozenset({"queue"}))
            adapter.api.call.side_effect = lambda method, path: (
                {"version": "2.0.0"} if path == "/api/info" else {"data": {"id": "ses_target", "time": {}}})

            def accept(*args, **kwargs):
                stop.set()  # Stop requested while the delivery is in progress.
                return "accepted_example"

            adapter.send_message.side_effect = accept

            def worker():
                try:  # The mailbox opens in this thread: SQLite connections stay in theirs.
                    relay_worker(stop, open_box=lambda: mailbox.LocalMailbox(root, node="linux"),
                                         adapter=adapter, retry=0)
                except BaseException as exc:
                    failures.append(exc)

            thread = threading.Thread(target=worker)
            with patch("opencode_connect.courier.worker.log"):
                thread.start()
                thread.join(10)
            self.assertFalse(thread.is_alive())
            self.assertEqual(failures, [])
            box = mailbox.LocalMailbox(root, node="linux")
            try:
                self.assertEqual(box.get(message)["state"], "delivered")
            finally:
                box.db.close()
