"""Showing the Roblox game being played as a Discord activity.

When the Discord setting is on, the launcher starts `nutmeg discord-presence`
next to Roblox. That process follows the client's logs for the game it joins
and leaves, looks the game up with Roblox's web API, and sets the activity
through the Discord app on this computer. It stops a little after Roblox does.

The client writes its logs under the guest's home folder, `Library/Logs/
Roblox`: inside the prefix (`root/Users/<name>/`), or in the real home folder
when rosemary's home folder setting is `host`. Both are watched. The lines
followed are ones the macOS client writes:

    [FLog::Output] ! Joining game '<job id>' place <place id> at <address>
    [FLog::SingleSurfaceApp] leaveUGCGameInternal
    [FLog::Network] Connection lost
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from . import discord, paths, processes

CLIENT_ID = "826716557051691068"

UNIVERSE_URL = "https://apis.roblox.com/universes/v1/places/{place}/universe"
GAMES_URL = "https://games.roblox.com/v1/games?universeIds={universe}"
ICON_URL = (
    "https://thumbnails.roblox.com/v1/games/icons?universeIds={universe}"
    "&returnPolicy=PlaceHolder&size=512x512&format=Png&isCircular=false"
)
GAME_PAGE = "https://www.roblox.com/games/{place}"

JOIN = re.compile(r"\[FLog::Output\] ! Joining game '([0-9A-Fa-f-]+)' place (\d+) at ")
LEAVE = ("[FLog::SingleSurfaceApp] leaveUGCGameInternal", "[FLog::Network] Connection lost")

# How often the logs are read, and Roblox's processes looked for.
POLL_SECONDS = 1.0
# How long Roblox may be gone before this stops. A launch that closes Roblox
# and starts it again takes less, so the presence carries on through it.
GONE_SECONDS = 10.0
# How long to wait between tries to reach the Discord app.
RETRY_SECONDS = 15.0
# How long a second copy waits for the first to stop before giving way to it.
LOCK_WAIT_SECONDS = 15.0
TIMEOUT = 10


def log_dirs(prefix: Path) -> list[Path]:
    """The folders the client's logs may be in, for Roblox in `prefix`."""
    found = sorted((prefix / "root/Users").glob("*/Library/Logs/Roblox"))
    found.append(Path.home() / "Library/Logs/Roblox")
    return found


class LogFollower:
    """Reads the lines added to the `.log` files in some folders, counting
    only files changed since `since` (seconds since the epoch)."""

    def __init__(self, folders: Callable[[], list[Path]], since: float):
        self._folders = folders
        self._since = since
        self._offsets: dict[Path, int] = {}
        self._partial: dict[Path, bytes] = {}

    def lines(self) -> list[str]:
        found = []
        for folder in self._folders():
            try:
                files = sorted(folder.glob("*.log"))
            except OSError:
                continue
            for path in files:
                try:
                    stat = path.stat()
                except OSError:
                    continue
                if path not in self._offsets:
                    if stat.st_mtime < self._since:
                        continue
                    self._offsets[path] = 0
                offset = self._offsets[path]
                if stat.st_size < offset:
                    offset = 0  # Replaced or truncated: start again.
                    self._partial.pop(path, None)
                if stat.st_size == offset:
                    continue
                try:
                    with open(path, "rb") as f:
                        f.seek(offset)
                        data = f.read(stat.st_size - offset)
                except OSError:
                    continue
                self._offsets[path] = offset + len(data)
                data = self._partial.pop(path, b"") + data
                *complete, rest = data.split(b"\n")
                if rest:
                    self._partial[path] = rest
                found.extend(line.decode("utf-8", "replace").rstrip("\r") for line in complete)
        return found


def parse(line: str) -> Optional[tuple[str, Optional[int]]]:
    """("join", place id), ("leave", None), or None for any other line."""
    match = JOIN.search(line)
    if match:
        return "join", int(match.group(2))
    if any(marker in line for marker in LEAVE):
        return "leave", None
    return None


@dataclass
class Game:
    place_id: int
    name: Optional[str] = None
    creator: Optional[str] = None
    icon: Optional[str] = None


def _get_json(url: str) -> object:
    from .roblox import USER_AGENT

    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


def lookup(place_id: int, note: Callable[[str], None] = lambda _l: None) -> Game:
    """The game a place belongs to, as far as Roblox's web API says. What
    cannot be found is left as None."""
    game = Game(place_id)
    try:
        universe = _get_json(UNIVERSE_URL.format(place=place_id))["universeId"]
        details = _get_json(GAMES_URL.format(universe=int(universe)))["data"][0]
        game.name = details.get("name") or None
        game.creator = (details.get("creator") or {}).get("name") or None
        icons = _get_json(ICON_URL.format(universe=int(universe)))["data"]
        if icons and icons[0].get("state") == "Completed":
            game.icon = icons[0].get("imageUrl") or None
    except (OSError, ValueError, KeyError, IndexError, TypeError, urllib.error.URLError) as e:
        note(f"could not look up place {place_id}: {e}")
    return game


def _text(value: str, limit: int = 128) -> str:
    """Discord takes 2 to 128 characters in an activity's text fields."""
    value = value.strip()
    if len(value) > limit:
        value = value[: limit - 1] + "…"
    return value.ljust(2)


def activity(game: Game, started: float) -> dict:
    """The Discord activity for playing `game` since `started`."""
    result: dict = {
        "type": 0,
        "details": _text(game.name or "In a Roblox game"),
        "timestamps": {"start": int(started)},
        "buttons": [{"label": "See game page", "url": GAME_PAGE.format(place=game.place_id)}],
    }
    if game.creator:
        result["state"] = _text(f"by {game.creator}")
    if game.icon:
        result["assets"] = {"large_image": game.icon, "large_text": _text(game.name or "Roblox")}
    return result


class Log:
    """nutmeg's launch log, which this process adds its lines to."""

    def __init__(self, path: Optional[Path]):
        self._file = open(path, "ab", buffering=0) if path else None

    def write(self, line: str) -> None:
        if self._file is not None:
            stamp = time.strftime("%H:%M:%S")
            self._file.write(f"[nutmeg discord {stamp}] {line}\n".encode("utf-8", "replace"))

    def close(self) -> None:
        if self._file is not None:
            self._file.close()


def _lock(wait: float) -> Optional[int]:
    """Hold the presence lock, waiting up to `wait` seconds for another copy
    to finish. Returns the descriptor, or None when another copy is running;
    it follows the new Roblox's logs too, so this one is not needed."""
    path = paths.state_dir() / "discord-presence.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o644)
    deadline = time.monotonic() + wait
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except BlockingIOError:
            if time.monotonic() >= deadline:
                os.close(fd)
                return None
            time.sleep(0.2)


def run(
    prefix: Path,
    since: float,
    log_path: Optional[Path] = None,
    stop: Optional[threading.Event] = None,
) -> int:
    """Keep the Discord activity in step with the game Roblox is in, until
    Roblox has been gone for GONE_SECONDS (or `stop` is set)."""
    stop = stop or threading.Event()
    log = Log(log_path)
    lock = _lock(LOCK_WAIT_SECONDS)
    if lock is None:
        log.write("another nutmeg is already showing Roblox on Discord")
        log.close()
        return 0
    client = discord.Client(CLIENT_ID)
    follower = LogFollower(lambda: log_dirs(prefix), since)
    current: Optional[dict] = None  # The activity to show; None shows none.
    # What Discord was last given. Discord drops an activity when the
    # connection that set it closes, so there is none to begin with.
    shown: Optional[dict] = None
    last_try = float("-inf")
    gone_since: Optional[float] = None
    games: dict[int, Game] = {}
    try:
        while not stop.is_set():
            for line in follower.lines():
                event = parse(line)
                if event is None:
                    continue
                kind, place = event
                if kind == "join":
                    if place not in games:
                        games[place] = lookup(place, log.write)
                    game = games[place]
                    log.write(f"joined place {place} ({game.name or 'name unknown'})")
                    current = activity(game, time.time())
                elif current is not None:
                    log.write("left the game")
                    current = None

            if current != shown:
                if not client.connected and time.monotonic() - last_try >= RETRY_SECONDS:
                    last_try = time.monotonic()
                    if client.connect():
                        log.write("connected to Discord")
                if client.connected:
                    try:
                        client.set_activity(current)
                        shown = current
                    except (OSError, ValueError) as e:
                        log.write(f"could not set the Discord activity: {e}")
                        client.close()

            if processes.running():
                gone_since = None
            elif gone_since is None:
                gone_since = time.monotonic()
            elif time.monotonic() - gone_since >= GONE_SECONDS:
                break
            stop.wait(POLL_SECONDS)
    finally:
        if client.connected:
            try:
                client.set_activity(None)
            except (OSError, ValueError):
                pass
            client.close()
        os.close(lock)
        log.write("stopped showing Roblox on Discord")
        log.close()
    return 0


def start_helper(prefix: Path, since: float, log_path: Path) -> subprocess.Popen:
    """Start `nutmeg discord-presence` for Roblox in `prefix`, in a session of
    its own so that it outlives the nutmeg that started it."""
    from .desktop import nutmeg_command

    argv = nutmeg_command() + [
        "discord-presence",
        "--prefix",
        str(prefix),
        "--since",
        repr(since),
        "--log",
        str(log_path),
    ]
    # Anything it prints, such as a traceback, goes to the launch log.
    with open(log_path, "ab") as output:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    # Reaped here if this nutmeg is still running when it ends.
    threading.Thread(target=process.wait, daemon=True).start()
    return process
