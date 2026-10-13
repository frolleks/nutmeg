"""The `nutmeg` command."""

from __future__ import annotations

import argparse
import sys
import threading
import time
import traceback
from typing import Optional

from . import __version__, desktop
from . import config as config_module
from .errors import Cancelled, NutmegError
from .protocol import InvalidURL, check


class TerminalFrontend:
    """The launcher reporting to a terminal. Questions are asked on the
    terminal, or answered with their first choice when `assume_yes`."""

    def __init__(self, assume_yes: bool):
        self._assume_yes = assume_yes
        self._shown = ("", "")
        self._shown_at = 0.0

    def status(self, text: str, detail: str = "") -> None:
        now = time.monotonic()
        # A new step is always shown; a changing detail at most once a second.
        if text == self._shown[0] and (detail == self._shown[1] or now - self._shown_at < 1.0):
            return
        self._shown, self._shown_at = (text, detail), now
        print(f"{text} ({detail})" if detail else text, flush=True)

    def progress(self, fraction: Optional[float]) -> None:
        pass

    def ask(self, title: str, message: str, choices: list[tuple[str, str]]) -> Optional[str]:
        if self._assume_yes:
            print(f"{title}: {choices[0][1]} (--yes)", flush=True)
            return choices[0][0]
        if not sys.stdin.isatty():
            print(
                f"{title}. There is no terminal to ask on; --yes answers "
                f"\"{choices[0][1]}\".",
                file=sys.stderr,
            )
            return None
        print(f"\n{title}\n\n{message}\n")
        for number, (_, label) in enumerate(choices, 1):
            print(f"  {number}. {label}")
        while True:
            try:
                answer = input(f"Choose 1-{len(choices)} (empty to cancel): ").strip()
            except EOFError:
                return None
            if not answer:
                return None
            if answer.isdigit() and 1 <= int(answer) <= len(choices):
                return choices[int(answer) - 1][0]


def _launch_in_terminal(url: Optional[str], start: bool, assume_yes: bool) -> int:
    # Imported here so that the other commands do not pay for it.
    from .launcher import Launcher

    try:
        config = config_module.load()
    except config_module.ConfigError as e:
        print(f"nutmeg: {e}", file=sys.stderr)
        return 1
    cancel = threading.Event()
    frontend = TerminalFrontend(assume_yes)
    launcher = None
    try:
        launcher = Launcher(config, frontend, cancel)
        launched = launcher.run(url, start)
        if launched is not None:
            launcher.settle(launched)
            print(f"Roblox is running. Log: {launched.log}")
        return 0
    except (Cancelled, KeyboardInterrupt):
        cancel.set()
        print("nutmeg: cancelled", file=sys.stderr)
        return 1
    except NutmegError as e:
        print(f"nutmeg: {e}", file=sys.stderr)
    except OSError as e:
        print(f"nutmeg: {e}", file=sys.stderr)
    except Exception:
        if launcher is not None:
            launcher.log.write(traceback.format_exc().rstrip())
        raise
    finally:
        if launcher is not None:
            launcher.log.close()
    if launcher is not None:
        print(f"nutmeg: the log is {launcher.log.path}", file=sys.stderr)
    return 1


def _gui(mode: str, url: Optional[str]) -> int:
    try:
        from .ui import app
    except (ImportError, ValueError) as e:
        print(
            f"nutmeg: GTK 4 for Python is not available ({e}). Install PyGObject and "
            "GTK 4 (on Debian and Ubuntu: python3-gi gir1.2-gtk-4.0), or use --no-gui.",
            file=sys.stderr,
        )
        return 1
    return app.run(mode, url)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nutmeg",
        description="Install, update and start the macOS Roblox client through rosemary.",
    )
    parser.add_argument("--version", action="version", version=f"nutmeg {__version__}")
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")
    commands.add_parser("settings", help="open the settings window (the default)")
    for name, text in (
        ("launch", "install or update Roblox, then start it"),
        ("update", "install or update Roblox without starting it"),
    ):
        command = commands.add_parser(name, help=text)
        if name == "launch":
            command.add_argument("url", nargs="?", help="a roblox-player: or roblox: link to open")
        command.add_argument(
            "--no-gui", action="store_true", help="report on the terminal instead of in a window"
        )
        command.add_argument(
            "--yes", action="store_true", help="with --no-gui, answer every question with its first choice"
        )
    commands.add_parser(
        "install-desktop", help="add menu entries and open roblox-player: and roblox: links with nutmeg"
    )
    commands.add_parser("uninstall-desktop", help="remove the menu entries")
    command = commands.add_parser(
        "discord-presence",
        help="show the game Roblox is in on Discord until it closes (started by launch)",
    )
    command.add_argument("--prefix", required=True, help="the rosemary prefix Roblox runs in")
    command.add_argument(
        "--since", type=float, default=0.0, help="only follow logs changed after this time (seconds since the epoch)"
    )
    command.add_argument("--log", help="a log file to add lines to")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = _parser().parse_args(argv)
    if not sys.platform.startswith("linux"):
        print("nutmeg: nutmeg is made for Linux.", file=sys.stderr)
        return 1
    command = args.command or "settings"
    if command in ("install-desktop", "uninstall-desktop"):
        try:
            done = desktop.install() if command == "install-desktop" else desktop.uninstall()
        except (NutmegError, OSError) as e:
            print(f"nutmeg: {e}", file=sys.stderr)
            return 1
        for line in done or ["nothing to remove"]:
            print(line)
        return 0
    if command == "settings":
        return _gui("settings", None)
    if command == "discord-presence":
        from pathlib import Path

        from . import presence

        return presence.run(Path(args.prefix), args.since, Path(args.log) if args.log else None)
    url = getattr(args, "url", None)
    if url is not None:
        try:
            check(url)
        except InvalidURL as e:
            print(f"nutmeg: {e}", file=sys.stderr)
            if not args.no_gui:
                # Most likely started by a browser, with no terminal to read.
                _gui("error", f"nutmeg was asked to open a link it does not handle: {e}")
            return 2
    start = command == "launch"
    if args.no_gui:
        return _launch_in_terminal(url, start, args.yes)
    return _gui(command, url)
