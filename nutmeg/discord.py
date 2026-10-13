"""A client for the Discord app's local IPC socket, enough to set an activity.

The Discord app listens on a Unix socket named `discord-ipc-<n>` (n from 0 to
9) in `XDG_RUNTIME_DIR`, or in a folder of its own there when it is a Flatpak
or a Snap. Each message is a frame: an opcode and a length, both 32-bit little
endian, followed by that many bytes of JSON. A connection starts with a
handshake naming the application, then sends commands such as SET_ACTIVITY.
"""

from __future__ import annotations

import json
import os
import socket
import struct
import uuid
from pathlib import Path

HANDSHAKE, FRAME, CLOSE, PING, PONG = range(5)

# Folders under the runtime directory that Discord's sockets are found in:
# the app itself, its Flatpak, its Snap, and the Vesktop Flatpak.
_SUBDIRECTORIES = (
    "",
    "app/com.discordapp.Discord",
    "app/com.discordapp.DiscordCanary",
    "snap.discord",
    ".flatpak/dev.vencord.Vesktop/xdg-run",
)


class DiscordError(OSError):
    pass


def socket_paths() -> list[Path]:
    """Where the Discord app's socket may be, in the order they are tried."""
    bases = []
    for variable in ("XDG_RUNTIME_DIR", "TMPDIR", "TMP", "TEMP"):
        value = os.environ.get(variable)
        if value and value not in bases:
            bases.append(value)
    if "/tmp" not in bases:
        bases.append("/tmp")
    found = []
    for base in bases:
        for sub in _SUBDIRECTORIES:
            for number in range(10):
                found.append(Path(base, sub, f"discord-ipc-{number}"))
    return found


class Client:
    """One connection to the Discord app for application `client_id`."""

    def __init__(self, client_id: str, timeout: float = 5.0):
        self.client_id = client_id
        self.timeout = timeout
        self._socket: socket.socket | None = None

    @property
    def connected(self) -> bool:
        return self._socket is not None

    def connect(self) -> bool:
        """Connect and shake hands with the first Discord app that answers.
        Returns whether one did."""
        self.close()
        for path in socket_paths():
            if not path.exists():
                continue
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(self.timeout)
            try:
                sock.connect(str(path))
                self._socket = sock
                self._send(HANDSHAKE, {"v": 1, "client_id": self.client_id})
                op, reply = self._receive()
                if op == FRAME and reply.get("evt") == "READY":
                    return True
            except (OSError, ValueError):
                pass
            self.close()
        return False

    def set_activity(self, activity: dict | None) -> None:
        """Show `activity`, or clear it with None. Raises DiscordError when
        Discord refuses it or the connection is gone."""
        if self._socket is None:
            raise DiscordError("not connected to Discord")
        nonce = str(uuid.uuid4())
        self._send(
            FRAME,
            {"cmd": "SET_ACTIVITY", "args": {"pid": os.getpid(), "activity": activity}, "nonce": nonce},
        )
        # Discord answers each command; other frames are not asked for, but a
        # ping can arrive at any time.
        for _ in range(8):
            op, reply = self._receive()
            if op == PING:
                self._send(PONG, reply)
                continue
            if op == CLOSE:
                self.close()
                raise DiscordError(f"Discord closed the connection: {reply.get('message', reply)}")
            if reply.get("nonce") == nonce:
                if reply.get("evt") == "ERROR":
                    raise DiscordError(f"Discord refused the activity: {reply.get('data', {}).get('message', reply)}")
                return
        raise DiscordError("Discord did not answer")

    def close(self) -> None:
        if self._socket is not None:
            try:
                self._socket.close()
            finally:
                self._socket = None

    def _send(self, op: int, payload: dict) -> None:
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        try:
            self._socket.sendall(struct.pack("<II", op, len(data)) + data)
        except OSError:
            self.close()
            raise

    def _receive(self) -> tuple[int, dict]:
        try:
            op, length = struct.unpack("<II", self._exactly(8))
            payload = json.loads(self._exactly(length).decode("utf-8")) if length else {}
        except (OSError, ValueError):
            self.close()
            raise
        return op, payload if isinstance(payload, dict) else {}

    def _exactly(self, count: int) -> bytes:
        data = b""
        while len(data) < count:
            chunk = self._socket.recv(count - len(data))
            if not chunk:
                raise DiscordError("Discord closed the connection")
            data += chunk
        return data
