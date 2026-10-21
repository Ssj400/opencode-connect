"""The courier's HTTP listener: it authenticates before reading any body byte.

Pairing is the one exception: `POST /v1/pair` carries no token yet, so it is
throttled in memory, limited to a small body, and refused while no offer is open.
"""

from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import re
import socket
import sqlite3
import time

from opencode_connect.core import BridgeError
from opencode_connect.courier.pairing import MAX_PAIR_BODY, Throttle
from opencode_connect.courier.settings import SOCKET_TIMEOUT, RequestError, listen_address

MAX_BODY = 64 * 1024
DRAIN_SECONDS = 1


class CourierServer(HTTPServer):
    """A bounded, single-request-at-a-time listener; never logs request headers."""

    def __init__(self, courier, listen, *, allow_wildcard=False):
        host, port = listen_address(listen, allow_wildcard)
        self.address_family = socket.AF_INET6 if ":" in host else socket.AF_INET
        self.courier = courier
        self.pairing = Throttle()
        super().__init__((host, port), CourierHandler)

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(SOCKET_TIMEOUT)
        return connection, address

    def handle_error(self, request, client_address):
        # Do not print raw remote requests, addresses, or credentials on errors.
        pass

    def shutdown_request(self, request):
        self.drain(request)
        self.close_request(request)

    def drain(self, request):
        """Read what the client still sends, briefly, before closing.

        A rejection is sent before the body is read; closing with the body still
        arriving makes the kernel reset the connection, and a proxy such as a Windows
        port proxy then forwards the reset instead of the status. Returns the bytes read.
        """
        drained = 0
        try:
            request.shutdown(socket.SHUT_WR)
            deadline = time.monotonic() + DRAIN_SECONDS
            while drained <= MAX_BODY and (remaining := deadline - time.monotonic()) > 0:
                request.settimeout(remaining)
                chunk = request.recv(4096)
                if not chunk:
                    break
                drained += len(chunk)
        except OSError:
            pass
        return drained


class CourierHandler(BaseHTTPRequestHandler):
    server_version = "Courier/1"
    sys_version = ""

    def log_message(self, format, *args):
        pass

    def reply(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def handle_expect_100(self):
        # HTTP/1.0 responses never invite an unauthenticated client to upload.
        return False

    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def body_length(self, limit):
        if self.headers.get("Transfer-Encoding") is not None:
            raise RequestError(400, "Transfer encoding is not supported.")
        lengths = self.headers.get_all("Content-Length", [])
        if lengths and (len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,6}", lengths[0])):
            raise RequestError(411, "A single Content-Length is required.")
        length = int(lengths[0]) if lengths else None
        if length is not None and length > limit:
            raise RequestError(413, "Message body is too large.")
        return length

    def json_body(self, length):
        if length is None:
            raise RequestError(411, "A single Content-Length is required.")
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip() != "application/json":
            raise RequestError(415, "JSON content type is required.")
        body = self.rfile.read(length)
        if len(body) != length:
            raise RequestError(400, "Incomplete body.")
        try:
            return json.loads(body)
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise RequestError(400, "Invalid JSON body.") from exc

    def pair(self):
        address = self.client_address[0]
        if not self.server.pairing.allow(address):
            raise RequestError(429, "Too many pairing attempts; wait a minute.")
        if not self.server.courier.offer_open():
            raise RequestError(404, "No pairing offer is open.")
        payload = self.json_body(self.body_length(MAX_PAIR_BODY))
        self.reply(200, self.server.courier.join(payload, address))

    def dispatch(self, method):
        try:
            if method == "POST" and self.path == "/v1/pair":
                return self.pair()
            auth = self.headers.get_all("Authorization", [])
            if len(auth) != 1:
                raise RequestError(401, "Authentication required.")
            self.server.courier.begin_request(auth[0])
            length = self.body_length(MAX_BODY)
            if method == "GET" and length:
                raise RequestError(400, "GET requests cannot contain a body.")
            if method == "GET" and self.path == "/v1/whoami":
                self.reply(200, self.server.courier.whoami(auth[0], rate_checked=True))
            elif method == "GET" and re.fullmatch(r"/v1/messages/msg_[a-f0-9]{32}", self.path):
                self.reply(200, self.server.courier.remote_status(auth[0], self.path.rsplit("/", 1)[1], rate_checked=True))
            elif method == "POST" and self.path == "/v1/messages":
                self.reply(202, self.server.courier.receive(auth[0], self.json_body(length), rate_checked=True))
            elif method == "POST" and self.path == "/v1/pair/ack":
                self.reply(200, self.server.courier.confirm_pairing(auth[0], self.json_body(length), rate_checked=True))
            else:
                raise RequestError(404, "Route not found.")
        except RequestError as exc:
            self.reply(exc.status, {"error": str(exc)})
        except (TimeoutError, ConnectionError, BrokenPipeError):
            self.close_connection = True
        except (BridgeError, sqlite3.Error):
            self.reply(503, {"error": "Courier storage unavailable."})
