#!/usr/bin/env python3
"""MCP stdio clásico: herramientas locales sin abrir un puerto de red."""

import json
import os
import sys

from bridge import BridgeError
from collab import Mailbox, ROOT, settings


INSTRUCTIONS = """Comunicación dirigida entre sesiones OpenCode autorizadas por el usuario.
Usa send solo para un encargo o aviso necesario dentro del trabajo autorizado.
Normal: queue; urgent=true: steer, solo para un bloqueo o cambio urgente real.
No cancelar agentes, ampliar tareas ni cambiar el modo Plan por este canal.
No reenviar tus respuestas ni contestar acuses automáticamente: evita ping-pong.
No te quedes esperando una respuesta en un bucle: puedes seguir tu tarea.
send_file solo lee archivos preparados explícitamente en outgoing/ del puente.
La entrega requiere el relay Linux y SSH vivos; pending NO significa entregado.
Consulta status; sending antiguo o uncertain requiere revisión, nunca reenvío ciego.
Lee AGENTS.md en la carpeta de este servidor para contexto y límites.
"""


def tool_schema(name, description, properties, required=()):
    return {"name": name, "description": description, "inputSchema": {
        "type": "object", "properties": properties, "required": list(required),
        "additionalProperties": False,
    }}


SEND_PROPERTIES = {
    "to": {"type": "string", "description": "Alias registrado del otro equipo; consulta peers."},
    "text": {"type": "string", "minLength": 1, "maxLength": 12000},
    "urgent": {"type": "boolean", "default": False, "description": "Solo urgencias explícitas: steer en lugar de queue."},
}
TOOLS = [
    tool_schema("peers", "Ver las sesiones registradas, sin leer sus conversaciones.", {}),
    tool_schema("status", "Estado de los últimos 30 envíos de este equipo; no reintentar uncertain.", {}),
    tool_schema("send", "Enviar un mensaje explícito al otro OpenCode; no espera respuesta.", SEND_PROPERTIES, ("to", "text")),
    tool_schema("send_file", "Transferir archivo de outgoing/ (máximo 1 MiB), sin sobrescribir ni ejecutar.",
                {**SEND_PROPERTIES, "filename": {"type": "string", "description": "Nombre simple, sin rutas."}},
                ("to", "text", "filename")),
]


def call_tool(params, root=ROOT):
    name = params["name"]
    args = params.get("arguments", {})
    if name == "peers":
        return settings(root)["peers"]
    box = Mailbox(root)
    try:
        if name == "status":
            return box.status()
        if name not in ("send", "send_file"):
            raise BridgeError("Herramienta desconocida.")
        allowed = {"to", "text", "urgent"} | ({"filename"} if name == "send_file" else set())
        if set(args) - allowed:
            raise BridgeError("Argumentos desconocidos.")
        session_id = params.get("_meta", {}).get("ai.opencode/sessionID")
        if not session_id:
            raise BridgeError("Falta metadata ai.opencode/sessionID; no se adivina la sesión origen.")
        return box.enqueue(session_id, args["to"], args["text"], args.get("urgent", False),
                           args["filename"] if name == "send_file" else None)
    finally:
        box.db.close()


def handle(request, root=ROOT):
    method = request.get("method")
    if "id" not in request:
        return None
    reply = {"jsonrpc": "2.0", "id": request["id"]}
    if method == "initialize":
        requested = request.get("params", {}).get("protocolVersion")
        version = requested if requested in ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25") else "2025-11-25"
        reply["result"] = {"protocolVersion": version,
                           "capabilities": {"tools": {"listChanged": False}},
                           "serverInfo": {"name": "opencode-connect", "version": "0.1.0"},
                           "instructions": INSTRUCTIONS}
    elif method == "ping":
        reply["result"] = {}
    elif method == "tools/list":
        reply["result"] = {"tools": TOOLS}
    elif method == "tools/call":
        try:
            result = call_tool(request.get("params", {}), root)
            reply["result"] = {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]}
        except (BridgeError, KeyError, TypeError, ValueError, OSError) as exc:
            reply["result"] = {"isError": True, "content": [{"type": "text", "text": str(exc)}]}
    else:
        reply["error"] = {"code": -32601, "message": "Method not found"}
    return reply


def main():
    os.umask(0o077)
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            request = json.loads(line)
            response = handle(request)
        except (ValueError, AttributeError, TypeError):
            response = {"jsonrpc": "2.0", "id": None,
                        "error": {"code": -32700, "message": "Parse error"}}
        if response is not None:
            print(json.dumps(response, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
