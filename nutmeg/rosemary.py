"""Running rosemary, as its docs/usage.md describes it.

Roblox runs as `rosemary run [--native|--emulated] <Roblox.app> [args...]` in
a prefix of its own, named with ROSEMARY_PREFIX. A join link is passed the way
rosemary's tools/install-url-handler.sh passes one, as `-protocolString <url>`:
the macOS client reads that argument, and rosemary's AppKit also hands the URL
to the application delegate's `application:openURLs:`.
"""

from __future__ import annotations

import collections
import os
import platform
import re
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Callable

from . import butter
from .config import MANAGED_ENV, Config
from .errors import Cancelled, NutmegError

# How a prefix's Darwin base names each Roblox build's architecture.
BASE_ARCH = {"x86_64": "x86_64", "arm64": "aarch64"}


def host_arch() -> str | None:
    """This machine's architecture as Roblox's builds name it, or None when
    Roblox has no build for it."""
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        return "x86_64"
    if machine in ("aarch64", "arm64"):
        return "arm64"
    return None


def bottle(config: Config) -> butter.Bottle | None:
    """The Butter bottle whose prefix Roblox runs in, if it is one. Its
    settings then take the place of nutmeg's runtime settings."""
    return butter.bottle_for_prefix(config.prefix())


def engine(config: Config) -> tuple[str, str]:
    """The execution mode, `auto`, `native` or `emulated`, and where it is
    chosen: nutmeg's settings, or the Butter bottle's."""
    found = bottle(config)
    if found is not None:
        return found.engine(), f"the {found.name} bottle's settings in Butter"
    return config.engine, "Settings"


def build_arch(config: Config) -> str:
    """Which Roblox build to install."""
    mode, where = engine(config)
    host = host_arch()
    if host is None:
        raise NutmegError(
            f"Roblox for macOS has no build for this machine ({platform.machine()})."
        )
    wanted = host if config.architecture == "auto" else config.architecture
    # rosemary recompiles x86-64 only; ARM emulation is not supported.
    if wanted != host and wanted != "x86_64":
        raise NutmegError(
            f"The {wanted} build cannot run on this {host} machine: rosemary can only "
            "emulate x86_64. Choose another build in Settings.",
            settings=True,
        )
    if mode == "native" and wanted != host:
        raise NutmegError(
            f"The {wanted} build cannot run natively on this {host} machine. "
            f"Choose another build in Settings or another execution mode in {where}.",
            settings=True,
        )
    if mode == "emulated" and wanted != "x86_64":
        raise NutmegError(
            "rosemary can only emulate the x86_64 build. Choose the x86_64 build in "
            f"Settings or another execution mode in {where}.",
            settings=True,
        )
    return wanted


def find_rosemary(config: Config) -> Path:
    """The rosemary program the settings choose: a Butter runner's or another
    executable, or else the one on PATH."""
    chosen = config.rosemary()
    if chosen is not None:
        if not chosen.is_file() or not os.access(chosen, os.X_OK):
            runner = butter.runner_directory(chosen)
            if runner is not None:
                raise NutmegError(
                    f"The Butter runner {runner.name} is no longer installed. Choose "
                    "another runner in Settings.",
                    settings=True,
                )
            raise NutmegError(f"{chosen} is not an executable file.", settings=True)
        return chosen
    found = shutil.which("rosemary")
    if found is None:
        if butter.runners():
            hint = "Choose one of the runners Butter has installed in Settings."
        else:
            hint = (
                f"Install a rosemary runner with Butter ({butter.WEBSITE}) and choose "
                "it in Settings, or choose a rosemary program you built."
            )
        raise NutmegError(f"rosemary was not found on PATH. {hint}", settings=True)
    return Path(found)


def prefix_created(prefix: Path) -> bool:
    """Whether `rosemary prefix create` has made a prefix here; its state file
    is written last, so it is what says the prefix is complete."""
    return (prefix / ".rosemary" / "state.yaml").is_file()


_BASE_ARCH_LINE = re.compile(r"""^  arch:\s*["']?([A-Za-z0-9_]+)["']?\s*$""")


def prefix_base_arch(prefix: Path) -> str | None:
    """The architecture of the prefix's Darwin base, or None when it has none
    or the state file cannot be read."""
    try:
        lines = (prefix / ".rosemary" / "state.yaml").read_text("utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    in_base = False
    for line in lines:
        if not line.startswith(" "):
            in_base = line.rstrip() == "base:"
            continue
        if in_base:
            match = _BASE_ARCH_LINE.match(line)
            if match:
                return match.group(1)
    return None


def _run_captured(
    argv: list[str],
    env: dict[str, str],
    output: Callable[[str], None],
    cancel: threading.Event,
) -> int:
    """Run a short rosemary command, passing each line it prints to `output`."""
    try:
        process = subprocess.Popen(
            argv,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
    except OSError as e:
        raise NutmegError(f"Could not run {argv[0]}: {e.strerror}", settings=True) from e
    # A cancel kills the command; the read loop below then sees end of file.
    watcher_done = threading.Event()

    def watch() -> None:
        while not watcher_done.wait(0.2):
            if cancel.is_set():
                process.kill()
                return

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    try:
        assert process.stdout is not None
        for raw in process.stdout:
            output(raw.decode("utf-8", "replace").rstrip("\n"))
        code = process.wait()
    finally:
        watcher_done.set()
        watcher.join()
        if process.poll() is None:
            process.kill()
            process.wait()
        process.stdout.close()
    if cancel.is_set():
        raise Cancelled()
    return code


def create_prefix(
    rosemary: Path,
    prefix: Path,
    base_arch: str | None,
    output: Callable[[str], None],
    cancel: threading.Event,
) -> None:
    """`rosemary prefix create`. With `base_arch`, the Darwin base for that
    architecture is installed too, downloading it if it is not cached; the
    caller has asked the user first, as rosemary does. Without it, only the
    prefix's directories are made (`--no-base`)."""
    env = dict(os.environ)
    argv = [str(rosemary), "prefix", "create", str(prefix)]
    if base_arch is None:
        argv.append("--no-base")
    else:
        argv += ["--arch", base_arch]
        env["ROSEMARY_PREFIX_SETUP"] = "download"
    last: collections.deque[str] = collections.deque(maxlen=12)

    def keep(line: str) -> None:
        last.append(line)
        output(line)

    code = _run_captured(argv, env, keep, cancel)
    if code != 0:
        raise NutmegError(
            f"rosemary could not create the prefix (exit status {code}):\n" + "\n".join(last)
        )


def repair_prefix(
    rosemary: Path, prefix: Path, output: Callable[[str], None], cancel: threading.Event
) -> int:
    """`rosemary prefix repair`, which puts back system files that differ from
    the base, offline. Returns the exit status."""
    argv = [str(rosemary), "prefix", "repair", str(prefix)]
    return _run_captured(argv, dict(os.environ), output, cancel)


def stop_server(
    rosemary: Path, config: Config, output: Callable[[str], None], cancel: threading.Event
) -> int:
    """`rosemary server --stop`, which asks the prefix's process server to
    exit, as Butter's Stop button does. Returns the exit status, which is not
    zero when no server was running."""
    argv = [str(rosemary), "server", "--stop"]
    return _run_captured(argv, run_env(config), output, cancel)


def run_env(config: Config, base: dict[str, str] | None = None) -> dict[str, str]:
    """The environment for `rosemary run`.

    Every variable a setting controls is removed from the inherited
    environment before the settings are applied, so a value exported in a
    shell cannot silently override what the settings say. In a Butter
    bottle's prefix, the bottle's settings and variables are applied in place
    of nutmeg's, as Butter applies them, so that nutmeg does not override
    what the bottle is set to.
    """
    env = dict(os.environ if base is None else base)
    for name in MANAGED_ENV:
        env.pop(name, None)
    found = bottle(config)
    if found is not None:
        env.update(found.environment())
        return env
    env.update(config.extra_env)
    env["ROSEMARY_PREFIX"] = str(config.prefix())
    if config.swap_interval:
        env["ROSEMARY_SWAP_INTERVAL"] = config.swap_interval
    if config.decorations:
        env["ROSEMARY_DECORATIONS"] = config.decorations
    if config.menu:
        env["ROSEMARY_MENU"] = config.menu
    if not config.menu_ctrl:
        env["ROSEMARY_MENU_CTRL"] = "0"
    if config.confine_pointer:
        env["ROSEMARY_CONFINE_POINTER"] = "1"
    if config.dead_flags:
        env["ROSEMARY_DEAD_FLAGS"] = "1"
    if config.call_return:
        env["ROSEMARY_CALL_RETURN"] = "1"
    if config.prefix_home:
        env["ROSEMARY_PREFIX_HOME"] = config.prefix_home
    if not config.process_server:
        env["ROSEMARY_SERVER"] = "off"
    if config.metal_presentation_mode:
        env["ROSEMARY_METAL_PRESENTATION_MODE"] = config.metal_presentation_mode
    if config.screen_refresh_rate:
        env["ROSEMARY_SCREEN_REFRESH_RATE"] = config.screen_refresh_rate
    if config.video_driver:
        env["SDL_VIDEO_DRIVER"] = config.video_driver
    return env


def run_argv(rosemary: Path, config: Config, app: Path, url: str | None) -> list[str]:
    """The command line that starts Roblox. `app` is the Roblox.app folder;
    rosemary finds the executable inside it."""
    argv = [str(rosemary), "run"]
    mode, _where = engine(config)
    if mode == "native":
        argv.append("--native")
    elif mode == "emulated":
        argv.append("--emulated")
    argv.append(str(app))
    if url is not None:
        argv += ["-protocolString", url]
    return argv
