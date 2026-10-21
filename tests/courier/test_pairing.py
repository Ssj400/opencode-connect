"""Pairing two couriers with a one-time code, over loopback HTTP; no VPN or network."""

import io
import ipaddress
from pathlib import Path
import re
import tempfile
import threading
import unittest
from unittest.mock import patch

from opencode_connect.core import BridgeError
from opencode_connect.courier import pairing
from opencode_connect.courier.app import Courier
from opencode_connect.courier.cli import main, way_back
from opencode_connect.courier.pairing import (OFFER_SECONDS, Throttle, is_vpn, join_offer, new_code, normal_code,
                                              pairing_key, parse_link, same_host, seal)
from opencode_connect.courier.server import CourierServer
from opencode_connect.courier.settings import RequestError, read_config, write_config
from opencode_connect.courier.worker import CourierBusy
from opencode_connect.transports import HttpError, http_json
from tests.courier.fakes import FakeQueue, configuration

CODE = re.compile(r"[0-9A-HJKMNP-TV-Z]{4}-[0-9A-HJKMNP-TV-Z]{4}-[0-9A-HJKMNP-TV-Z]{4}\Z")


def fast_keys(case):
    """The real key derivation is deliberately slow; the tests need only its shape."""
    rounds = patch("opencode_connect.courier.pairing.KDF_ROUNDS", 1000)
    rounds.start()
    case.addCleanup(rounds.stop)


class CodeTests(unittest.TestCase):
    def test_codes_are_crockford_and_forgiving_to_type(self):
        code = new_code()
        self.assertRegex(code, CODE)
        self.assertEqual(normal_code(code.lower().replace("-", " ")), code.replace("-", ""))
        self.assertEqual(normal_code("o1il-abcd-efgh"), "0111ABCDEFGH")
        for wrong in ("ABCD-EFGH", "ABCD-EFGH-JKMU", "", "ABCD-EFGH-JKMN-P"):
            with self.subTest(code=wrong), self.assertRaises(BridgeError):
                normal_code(wrong)

    def test_the_key_is_slow_and_depends_only_on_the_code(self):
        self.assertGreaterEqual(pairing.KDF_ROUNDS, 600_000)
        fast_keys(self)
        self.assertEqual(pairing_key("ABCD-EFGH-JKMN"), pairing_key("abcd efgh jkmn"))
        self.assertNotEqual(pairing_key("ABCD-EFGH-JKMN"), pairing_key("ABCD-EFGH-JKMP"))

    def test_links_keep_the_code_in_their_fragment(self):
        self.assertEqual(parse_link("http://192.0.2.10:8765#abcd-efgh-jkmn"), ("http://192.0.2.10:8765", "ABCDEFGHJKMN"))
        for wrong in ("http://192.0.2.10:8765", "http://192.0.2.10:8765/path#ABCD-EFGH-JKMN"):
            with self.subTest(link=wrong), self.assertRaises(BridgeError):
                parse_link(wrong)

    def test_addresses(self):
        mesh = str(ipaddress.ip_address(0x6440000A))
        self.assertTrue(is_vpn(mesh))
        self.assertFalse(is_vpn("192.0.2.10"))
        self.assertTrue(same_host("http://192.0.2.10:8765", "192.0.2.10"))
        self.assertTrue(same_host("http://192.0.2.10:8765", "::ffff:192.0.2.10"))
        self.assertFalse(same_host("http://192.0.2.11:8765", "192.0.2.10"))
        self.assertFalse(same_host("http://example:8765", "192.0.2.10"))

    def test_only_network_failures_are_repeated(self):
        calls = []

        def refuse():
            calls.append(1)
            raise HttpError(403, "The pairing code does not match.")

        with self.assertRaises(HttpError):
            pairing.retrying(refuse, 3, 0)
        self.assertEqual(len(calls), 1)
        outcomes = iter([BridgeError("HTTP request failed (TimeoutError)."), {"paired": True}])

        def flaky():
            outcome = next(outcomes)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        self.assertEqual(pairing.retrying(flaky, 3, 0), {"paired": True})

    def test_the_throttle_counts_per_address_and_in_total_in_memory(self):
        now = [0.0]
        throttle = Throttle(per_address=2, total=3, window=60, clock=lambda: now[0])
        self.assertEqual([throttle.allow("192.0.2.1") for _ in range(3)], [True, True, False])
        self.assertEqual([throttle.allow("192.0.2.2") for _ in range(2)], [True, False])
        now[0] = 61
        self.assertTrue(throttle.allow("192.0.2.1"))


class PairingTests(unittest.TestCase):
    def setUp(self):
        fast_keys(self)
        folder = tempfile.TemporaryDirectory(prefix="courier-pairing-")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.now = 1000.0
        self.alice, self.alice_url = self.start("alice")
        self.bob, self.bob_url = self.start("bob")

    def start(self, name):
        queue = FakeQueue(self.root / f"{name}.sqlite3")
        self.addCleanup(queue.db.close)
        app = Courier(queue, configuration(name), clock=lambda: self.now)
        server = CourierServer(app, "127.0.0.1:0")
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()

        def stop():
            server.shutdown()
            thread.join(timeout=3)
            server.server_close()

        self.addCleanup(stop)
        url = f"http://127.0.0.1:{server.server_address[1]}"
        with app.transaction():
            config = app.settings()
            config["url"] = url
            app.save_settings(config)
        return app, url

    def request(self, code, nonce="a" * 32, **card):
        card = {"person": "bob", "url": self.bob_url, "token": "B" * 43, **card}
        return {"nonce": nonce, "card": card, "mac": seal(pairing_key(code), b"join", nonce, card)}

    def refused(self, status, payload, peer="127.0.0.1"):
        with self.assertRaises(RequestError) as caught:
            self.alice.join(payload, peer)
        self.assertEqual(caught.exception.status, status)
        return caught.exception

    def test_pairing_exchanges_cards_and_both_ways_work(self):
        code = self.alice.offer()
        self.assertRegex(code, CODE)
        self.assertEqual(join_offer(self.bob, f"{self.alice_url}#{code.lower()}"), "alice")
        self.assertEqual(self.alice.offer_state(), {"state": "confirmed", "contact": "bob"})
        for app, name, url in ((self.alice, "bob", self.bob_url), (self.bob, "alice", self.alice_url)):
            card = app.settings()["contacts"][name]
            self.assertEqual(card["url"], url)
            identity = http_json(url + "/v1/whoami", headers={"Authorization": "Bearer " + card["token"]})
            self.assertEqual(identity["person"], name)
        # Once confirmed, the answer and its token are no longer kept in clear.
        self.assertIsNone(self.alice.db.execute("SELECT response FROM courier_offer").fetchone()[0])

    def test_a_wrong_code_writes_nothing_on_either_side(self):
        code = self.alice.offer()
        wrong = "0000-0000-0000" if code != "0000-0000-0000" else "1111-1111-1111"
        before = (list(self.alice.db.iterdump()), list(self.bob.db.iterdump()))
        with self.assertRaises(HttpError) as caught:
            join_offer(self.bob, f"{self.alice_url}#{wrong}")
        self.assertEqual(caught.exception.status, 403)
        self.assertEqual((list(self.alice.db.iterdump()), list(self.bob.db.iterdump())), before)

    def test_a_repeated_request_gets_the_same_answer_and_another_is_refused(self):
        code = self.alice.offer()
        answer = self.alice.join(self.request(code), "127.0.0.1")
        self.assertEqual(self.alice.join(self.request(code), "127.0.0.1"), answer)
        self.assertEqual(answer["mac"], seal(pairing_key(code), b"offer", "a" * 32, answer["card"]))
        self.refused(409, self.request(code, nonce="b" * 32))
        # Over HTTP too, although the offer is no longer open to anyone else.
        self.assertEqual(http_json(self.alice_url + "/v1/pair", method="POST", data=self.request(code)), answer)

    def test_a_lapsed_offer_is_refused_and_one_pairing_waits_at_a_time(self):
        code = self.alice.offer()
        self.now += OFFER_SECONDS + 1
        self.refused(404, self.request(code))
        self.alice.join(self.request(self.alice.offer()), "127.0.0.1")
        with self.assertRaisesRegex(BridgeError, "waits for its confirmation"):
            self.alice.offer()

    def test_an_unconfirmed_pairing_lapses_and_is_undone(self):
        self.alice.join(self.request(self.alice.offer()), "127.0.0.1")
        self.assertEqual([row["person"] for row in self.alice.contacts()], ["bob"])
        self.now += OFFER_SECONDS + 1
        self.alice.tidy()
        self.assertEqual(self.alice.contacts(), [])
        self.assertIsNone(self.alice.offer_state())

    def test_a_lapsed_replacement_restores_the_previous_contact(self):
        self.alice.trust("bob", {"person": "bob", "url": "http://192.0.2.20:8765", "token": "C" * 43})
        self.alice.invite("bob")
        before = self.alice.settings()
        self.refused(409, self.request(self.alice.offer()))
        self.alice.join(self.request(self.alice.offer(replace=True)), "127.0.0.1")
        self.assertNotEqual(self.alice.settings(), before)
        self.alice.close_offer()
        self.assertEqual(self.alice.settings(), before)

    def test_a_contact_removed_while_pairing_stays_removed(self):
        self.alice.trust("bob", {"person": "bob", "url": "http://192.0.2.20:8765", "token": "C" * 43})
        self.alice.join(self.request(self.alice.offer(replace=True)), "127.0.0.1")
        self.alice.untrust("bob")
        self.assertIsNone(self.alice.offer_state())
        self.alice.close_offer()
        self.now += OFFER_SECONDS + 1
        self.alice.tidy()
        self.assertEqual(self.alice.contacts(), [])

    def test_the_card_must_come_from_the_address_it_names_and_the_name_offered(self):
        code = self.alice.offer()
        error = self.refused(400, self.request(code, url="http://192.0.2.30:8765"))
        self.assertIn("127.0.0.1", str(error))
        self.refused(409, self.request(code, person="alice"))
        self.assertEqual(self.alice.contacts(), [])
        self.refused(403, self.request(self.alice.offer("carol")))
        self.assertEqual(self.alice.contacts(), [])

    def test_the_open_route_needs_an_offer_and_a_small_body(self):
        with self.assertRaises(HttpError) as caught:
            http_json(self.alice_url + "/v1/pair", method="POST", data={"nonce": "a" * 32})
        self.assertEqual(caught.exception.status, 404)
        self.alice.offer()
        with self.assertRaises(HttpError) as caught:
            http_json(self.alice_url + "/v1/pair", method="POST", data={"padding": "x" * 2000})
        self.assertEqual(caught.exception.status, 413)

    def test_the_open_route_is_throttled(self):
        for _ in range(10):
            with self.assertRaises(HttpError):
                http_json(self.alice_url + "/v1/pair", method="POST", data={})
        with self.assertRaises(HttpError) as caught:
            http_json(self.alice_url + "/v1/pair", method="POST", data={})
        self.assertEqual(caught.exception.status, 429)

    def test_a_confirmation_needs_the_new_token_and_the_nonce(self):
        answer = self.alice.join(self.request(self.alice.offer()), "127.0.0.1")
        token = "Bearer " + answer["card"]["token"]
        for authorization, payload, status in ((token, {"nonce": "b" * 32}, 404), ("Bearer " + "D" * 43,
                                                {"nonce": "a" * 32}, 401), (token, {"nonce": "x"}, 400)):
            with self.subTest(status=status), self.assertRaises(RequestError) as caught:
                self.alice.confirm_pairing(authorization, payload)
            self.assertEqual(caught.exception.status, status)
        for _ in range(2):  # A repeated confirmation is harmless.
            self.assertEqual(self.alice.confirm_pairing(token, {"nonce": "a" * 32}), {"person": "alice", "paired": True})

    def test_the_joiner_undoes_its_side_when_the_confirmation_fails(self):
        code, real = self.alice.offer(), http_json

        def no_confirmation(url, **kwargs):
            if url.endswith("/v1/pair/ack"):
                raise BridgeError("HTTP request failed (ConnectionRefusedError).")
            return real(url, **kwargs)

        with patch("opencode_connect.courier.pairing.http_json", side_effect=no_confirmation), \
                patch("opencode_connect.courier.pairing.time.sleep"), self.assertRaisesRegex(BridgeError, "undone"):
            join_offer(self.bob, f"{self.alice_url}#{code}")
        self.assertEqual(self.bob.contacts(), [])

    def test_the_joiner_stores_nothing_from_a_courier_that_does_not_know_the_code(self):
        impostor = {"person": "alice", "url": self.alice_url, "token": "E" * 43}
        before = list(self.bob.db.iterdump())
        with patch("opencode_connect.courier.pairing.http_json", return_value={"card": impostor, "mac": "0" * 64}), \
                self.assertRaisesRegex(BridgeError, "did not prove"):
            join_offer(self.bob, f"{self.alice_url}#ABCD-EFGH-JKMN")
        self.assertEqual(list(self.bob.db.iterdump()), before)

    def test_the_joiner_refuses_an_answer_for_another_address_or_with_the_joiners_label(self):
        code = "ABCD-EFGH-JKMN"
        for label, url in ((b"offer", "http://192.0.2.40:8765"), (b"join", self.alice_url)):
            card = {"person": "alice", "url": url, "token": "E" * 43}

            def answer(address, **kwargs):
                return {"card": card, "mac": seal(pairing_key(code), label, kwargs["data"]["nonce"], card)}

            with self.subTest(label=label), patch("opencode_connect.courier.pairing.http_json", side_effect=answer), \
                    self.assertRaisesRegex(BridgeError, "did not prove"):
                join_offer(self.bob, f"{self.alice_url}#{code}")
        self.assertEqual(self.bob.contacts(), [])

    def test_the_joiner_needs_replace_for_a_name_it_has_at_another_address(self):
        old = {"person": "alice", "url": "http://192.0.2.50:8765", "token": "F" * 43}
        self.bob.trust("alice", old)
        with self.assertRaisesRegex(BridgeError, "--replace"):
            join_offer(self.bob, f"{self.alice_url}#{self.alice.offer()}")
        self.assertEqual(self.bob.settings()["contacts"], {"alice": old})

    def test_the_joiner_needs_replace_for_a_contact_it_already_has(self):
        self.bob.trust("alice", {"person": "alice", "url": self.alice_url, "token": "F" * 43})
        code = self.alice.offer()
        with self.assertRaisesRegex(BridgeError, "--replace"):
            join_offer(self.bob, f"{self.alice_url}#{code}")
        self.assertEqual(self.alice.offer_state()["state"], "open")
        self.bob.invite("alice")
        self.assertEqual(join_offer(self.bob, f"{self.alice_url}#{code}", replace=True), "alice")


class PairingCliTests(unittest.TestCase):
    def setUp(self):
        fast_keys(self)
        folder = tempfile.TemporaryDirectory(prefix="courier-pairing-cli-")
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "courier.json"
        self.database = Path(folder.name) / "queue.sqlite3"
        write_config(self.path, {**configuration("alice"), "listen": "192.0.2.10:8765", "url": "http://192.0.2.10:8765"})

    def main(self, *arguments, output=None):
        output = output or io.StringIO()
        with patch("opencode_connect.courier.cli.require_terminal"), patch("sys.stdout", output), \
                patch("sys.stderr", output):
            result = main(["--config", str(self.path), *arguments], queue_factory=lambda path: FakeQueue(self.database))
        return result, output.getvalue()

    def other(self):
        queue = FakeQueue(self.database)
        self.addCleanup(queue.db.close)
        return Courier(queue, read_config(self.path))

    def test_the_inviter_prints_a_link_waits_for_the_service_and_checks_the_way_back(self):
        output, answered = io.StringIO(), []

        def service_answers(seconds):
            if answered:
                return
            code = re.search(r"#([0-9A-Z-]{14})'", output.getvalue())[1]
            card = {"person": "bob", "url": "http://192.0.2.20:8765", "token": "B" * 43}
            service = self.other()
            answer = service.join({"nonce": "a" * 32, "card": card,
                                   "mac": seal(pairing_key(code), b"join", "a" * 32, card)}, "192.0.2.20")
            answered.append(service.confirm_pairing("Bearer " + answer["card"]["token"], {"nonce": "a" * 32}))

        with patch("opencode_connect.courier.cli.run_courier", side_effect=CourierBusy("busy")), \
                patch("opencode_connect.courier.cli.time.sleep", side_effect=service_answers), \
                patch("opencode_connect.courier.cli.http_json", return_value={"person": "bob", "protocol": 1}) as probe:
            result, text = self.main("pair", "--for", "bob", output=output)
        self.assertEqual(result, 0, text)
        self.assertIn("python3 bin/courier.py pair 'http://192.0.2.10:8765#", text)
        self.assertIn("not in a VPN range", text)
        self.assertIn("Paired with bob, both ways.", text)
        self.assertEqual(probe.call_args.args[0], "http://192.0.2.20:8765/v1/whoami")
        other = self.other()
        self.assertIsNone(other.offer_state())
        self.assertEqual([row["person"] for row in other.contacts()], ["bob"])

    def test_an_interrupted_offer_is_withdrawn_with_its_unconfirmed_pairing(self):
        output = io.StringIO()

        def joined_then_interrupted(seconds):
            code = re.search(r"#([0-9A-Z-]{14})'", output.getvalue())[1]
            card = {"person": "bob", "url": "http://192.0.2.20:8765", "token": "B" * 43}
            self.other().join({"nonce": "a" * 32, "card": card,
                               "mac": seal(pairing_key(code), b"join", "a" * 32, card)}, "192.0.2.20")
            raise KeyboardInterrupt

        with patch("opencode_connect.courier.cli.run_courier", side_effect=CourierBusy("busy")), \
                patch("opencode_connect.courier.cli.time.sleep", side_effect=joined_then_interrupted):
            result, _ = self.main("pair", output=output)
        self.assertEqual(result, 130)
        other = self.other()
        self.assertIsNone(other.offer_state())
        self.assertEqual(other.contacts(), [])

    def test_the_way_back_needs_the_contact_to_answer_as_itself(self):
        card = {"person": "bob", "url": "http://192.0.2.20:8765", "token": "B" * 43}
        for answer in ({"side_effect": BridgeError("HTTP request failed (TimeoutError).")},
                       {"return_value": {"person": "carol", "protocol": 1}}):
            with self.subTest(answer=answer), patch("opencode_connect.courier.cli.http_json", **answer):
                self.assertFalse(way_back(card, "bob", attempts=2, wait=0))

    def test_a_lapsed_offer_reports_that_nobody_paired(self):
        with patch("opencode_connect.courier.cli.run_courier", side_effect=CourierBusy("busy")), \
                patch("opencode_connect.courier.cli.time.sleep", side_effect=lambda seconds: self.other().close_offer()):
            result, text = self.main("pair")
        self.assertEqual(result, 1)
        self.assertIn("nobody paired", text)

    def test_the_joiner_answers_the_way_back_in_the_foreground(self):
        def serve(courier, listen, *, stop, until):
            self.assertFalse(until())
            with courier.transaction():
                courier.note_peer("bob")  # The inviter's whoami arrived.
            self.assertTrue(until())

        with patch("opencode_connect.courier.cli.join_offer", return_value="bob") as join, \
                patch("opencode_connect.courier.cli.run_courier", side_effect=serve):
            result, text = self.main("pair", "http://192.0.2.20:8765#ABCD-EFGH-JKMN", "--replace")
        self.assertEqual(result, 0, text)
        self.assertEqual(join.call_args.kwargs, {"replace": True})
        self.assertIn("bob checked the way back: both ways work.", text)
        result, text = self.main("pair", "http://192.0.2.20:8765#ABCD-EFGH-JKMN", "--for", "bob")
        self.assertEqual(result, 1)
        self.assertIn("--for", text)


if __name__ == "__main__":
    unittest.main()
