"""OpenCode V2 CLI and session adapter; no SSH or operating-system selection."""

import json
import re
from typing import Any, Protocol, TYPE_CHECKING

from core import BridgeError
if TYPE_CHECKING:
    from transports import CommandTransport


class JsonApi(Protocol):
    def call(self, method: str, path: str, body: Any = None, timeout: float = 30) -> Any:
        ...


class OpenCodeCli:
    def __init__(self, transport: "CommandTransport", executable: str):
        self.transport = transport
        self.executable = executable

    def call(self, method, path, body=None, timeout=30):
        argv = [self.executable, "api", method, path]
        if body is not None:
            argv += ["--data", json.dumps(body, ensure_ascii=False)]
        result = self.transport.run(argv, timeout=timeout)
        if isinstance(result, dict) and "_tag" in result:
            raise BridgeError(f"API error: {result.get('message', result['_tag'])}")
        return result


class OpenCodeAdapter:
    supported_deliveries = frozenset(("queue", "steer"))

    def __init__(self, api: JsonApi):
        self.api = api

    @staticmethod
    def session_id(value):
        if not isinstance(value, str) or not re.fullmatch(r"ses_[A-Za-z0-9_-]{1,128}", value):
            raise BridgeError("Invalid OpenCode session ID.")
        return value

    @classmethod
    def caller_session(cls, params):
        metadata = params.get("_meta")
        if not isinstance(metadata, dict) or not metadata.get("ai.opencode/sessionID"):
            raise BridgeError("Missing ai.opencode/sessionID metadata; source identity is never guessed.")
        return cls.session_id(metadata["ai.opencode/sessionID"])

    def send_message(self, session, *, message_id, text, delivery):
        self.session_id(session)
        if delivery not in self.supported_deliveries:
            raise BridgeError("OpenCode does not support the requested delivery mode.")
        response = self.api.call("post", f"/api/session/{session}/prompt",
                                 {"id": message_id, "text": text, "delivery": delivery})
        return response["data"]["id"]

    def create_session(self, *, title, directory):
        return self.api.call("post", "/api/session", {
            "title": title, "location": {"directory": directory},
            "permissions": [{"action": "*", "resource": "*", "effect": "ask"}],
        })["data"]


def check(endpoints):
    for label, endpoint in endpoints:
        for path in ("/api/info", "/api/session?limit=1"):
            try:
                response = endpoint.call("get", path)
            except BridgeError as exc:
                raise BridgeError(f"{label}, GET {path}: {exc}") from exc
            if path == "/api/info":
                if not isinstance(response, dict) or not isinstance(response.get("version"), str):
                    raise BridgeError(f"{label}: unexpected /api/info response.")
                info = response
                if not info["version"].startswith("2."):
                    raise BridgeError(f"{label}: OpenCode V2 is required.")
            elif not isinstance(response, dict) or not isinstance(response.get("data"), list):
                raise BridgeError(f"{label}: unexpected session list response.")
        print(f"{label}: OpenCode {info['version']}, API accessible.", flush=True)
