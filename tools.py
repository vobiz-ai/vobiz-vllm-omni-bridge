"""
tools.py — the functions the agent may call, and how each one acts on the call
===============================================================================

Both engines expose the same tool schemas (OpenAI function-calling shape) and
hand results to `ToolRunner.run()`, which performs the Vobiz-side action:

    end_call            say goodbye first; the socket closes after playedStream
    transfer_to_human   REST transfer of the caller's leg to /transfer XML (<Dial>)
    send_dtmf           REST DTMF onto the live call (rare: driving a remote IVR)
"""

from __future__ import annotations

import json
import logging

import vobiz_api
from config import Flags

logger = logging.getLogger("tools")


def definitions(flags: Flags) -> list[dict]:
    tools = []
    if flags.tool_end_call:
        tools.append(
            {
                "type": "function",
                "name": "end_call",
                "description": "Hang up the telephone call. Call this only after saying goodbye, when the conversation is finished or the caller asks to end the call.",
                "parameters": {"type": "object", "properties": {"reason": {"type": "string"}}},
            }
        )
    if flags.tool_transfer:
        tools.append(
            {
                "type": "function",
                "name": "transfer_to_human",
                "description": "Transfer the caller to a human colleague. Call this when the caller asks for a person, or asks something you cannot handle. Tell the caller you are connecting them first.",
                "parameters": {"type": "object", "properties": {"reason": {"type": "string"}}},
            }
        )
    if flags.tool_send_dtmf:
        tools.append(
            {
                "type": "function",
                "name": "send_dtmf",
                "description": "Send keypad digits on the call, for example to navigate a menu on the other side.",
                "parameters": {"type": "object", "properties": {"digits": {"type": "string"}}, "required": ["digits"]},
            }
        )
    return tools


def chat_tools(flags: Flags) -> list[dict]:
    """Chat-completions wraps each function under {"type":"function","function":{...}}."""
    return [{"type": "function", "function": {k: v for k, v in t.items() if k != "type"}} for t in definitions(flags)]


class ToolRunner:
    def __init__(self, flags: Flags, call_uuid: str, base_url: str, on_event=None):
        self.flags = flags
        self.call_uuid = call_uuid
        self.base_url = base_url
        self.on_event = on_event or (lambda n, d: None)
        self.hangup_after_playback = False
        self.transfer_after_playback = False

    async def run(self, name: str, arguments) -> dict:
        try:
            args = json.loads(arguments) if isinstance(arguments, str) and arguments else (arguments or {})
        except json.JSONDecodeError:
            args = {}
        logger.info(f"tool {name}({args})")
        self.on_event("tool", {"name": name, "args": args})
        if name == "end_call":
            self.hangup_after_playback = True
            return {"result": "ok", "note": "say goodbye now; the call ends after it is played"}
        if name == "transfer_to_human":
            if not self.flags.tool_transfer:
                return {"error": "transfer disabled"}
            self.transfer_after_playback = True
            return {"result": "ok", "note": "announce the transfer now; the caller is connected after it is played"}
        if name == "send_dtmf":
            digits = str(args.get("digits", ""))
            if not digits or not self.flags.tool_send_dtmf:
                return {"error": "dtmf disabled or empty"}
            status = await vobiz_api.send_dtmf(self.flags, self.call_uuid, digits)
            return {"result": "ok" if status < 300 else "failed", "http": status}
        return {"error": f"unknown tool {name}"}

    async def after_playback(self) -> str | None:
        """Called when the last checkpoint is acknowledged. Returns the action taken."""
        if self.transfer_after_playback:
            self.transfer_after_playback = False
            status = await vobiz_api.transfer(self.flags, self.call_uuid, f"{self.base_url}/transfer")
            self.on_event("transfer", {"http": status})
            return "transfer"
        if self.hangup_after_playback:
            return "hangup"
        return None
