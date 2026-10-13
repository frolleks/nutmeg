"""The steps between a click on Play and Roblox running.

This module has no GTK in it: the window and the terminal front ends both
drive the same `Launcher`, and see it through the `Frontend` methods.
"""

from __future__ import annotations

import fcntl
import os
import shlex
import signal
import subprocess
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Optional, Protocol

from . import __version__, paths, presence, processes, roblox, rosemary
from .config import Config
from .errors import Cancelled, NutmegError
from .protocol import redact

# How long a freshly started Roblox is watched for an immediate failure.
STARTUP_GRACE_SECONDS = 5.0
# How many launch logs are kept.
KEEP_LOGS = 20


class Frontend(Protocol):
    def status(self, text: str, detail: str = "") -> None:
        """What is happening now, and optionally a second line about it."""

    def progress(self, fraction: Optional[float]) -> None:
        """How far along the current step is, or None when that is not known."""

    def ask(self, title: str, message: str, choices: list[tuple[str, str]]) -> Optional[str]:
        """Ask the user to pick one of `choices`, given as (id, label) with the
        suggested one first. Returns the id picked, or None if the question was
        dismissed. Called from the worker thread; blocks until answered."""


@dataclass
class Launched:
    process: subprocess.Popen
    log: Path
    build: roblox.Build


class LaunchLog:
    """One file per launch holding nutmeg's own steps followed by everything
    rosemary and Roblox print."""

    def __init__(self) -> None:
        directory = paths.logs_dir()
        directory.mkdir(parents=True, exist_ok=True)
        old = sorted(directory.glob("launch-*.log"))
        for stale in old[: max(0, len(old) - (KEEP_LOGS - 1))]:
            stale.unlink(missing_ok=True)
        name = time.strftime("launch-%Y%m%d-%H%M%S") + f"-{os.getpid()}.log"
        self.path = directory / name
        self._file = open(self.path, "ab", buffering=0)

    def write(self, line: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self._file.write(f"[nutmeg {stamp}] {line}\n".encode("utf-8", "replace"))

    def fileno(self) -> int:
        return self._file.fileno()

    def close(self) -> None:
        self._file.close()

    def tail(self, count: int = 25) -> str:
        try:
            with open(self.path, "rb") as f:
                f.seek(0, os.SEEK_END)
                f.seek(max(0, f.tell() - 64 * 1024))
                text = f.read().decode("utf-8", "replace")
        except OSError:
            return ""
        return "\n".join(text.splitlines()[-count:])


@contextmanager
def install_lock(cancel: threading.Event, waiting: Callable[[], None]) -> Iterator[None]:
    """Hold nutmeg's install lock, so that two launches at once do not
    download or remove builds under each other."""
    path = paths.lock_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o644)
    try:
        told = False
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if not told:
                    waiting()
                    told = True
                if cancel.wait(0.2):
                    raise Cancelled()
        yield
    finally:
        # Closing the descriptor releases the lock.
        os.close(fd)


def _megabytes(count: int) -> str:
    return f"{count / 1e6:.1f} MB"


class Launcher:
    def __init__(self, config: Config, frontend: Frontend, cancel: threading.Event):
        self.config = config
        self.frontend = frontend
        self.cancel = cancel
        self.log = LaunchLog()

    def run(self, url: Optional[str], start: bool = True) -> Optional[Launched]:
        """Install or update Roblox, then start it with `url` when `start`.

        Raises `NutmegError` for a failure to report and `Cancelled` when the
        user backed out.
        """
        what = f"opening {redact(url)}" if url else ("starting Roblox" if start else "updating Roblox")
        self.log.write(f"nutmeg {__version__}, {what}")
        binary = rosemary.find_rosemary(self.config)
        arch = rosemary.build_arch(self.config)
        prefix = self.config.prefix()
        self.log.write(f"rosemary: {binary}; build: {arch}; prefix: {prefix}")
        in_bottle = rosemary.bottle(self.config)
        if in_bottle is not None:
            self.log.write(f"the prefix is the Butter bottle {in_bottle.name}'s; using its settings")
        self.frontend.progress(None)
        with install_lock(self.cancel, self._waiting):
            roblox.remove_leftovers()
            self._ensure_prefix(binary, prefix, arch)
            build, _executable = self._ensure_build(arch)
            roblox.write_fast_flags(build.bundle(), self.config.fast_flags)
            if start:
                self._stop_running(binary)
            removed = roblox.remove_versions({build.key}, set(processes.running().values()))
            for name in removed:
                self.log.write(f"removed the old build {name}")
            if not start:
                self.frontend.status(f"Roblox {build.label} is installed")
                self.frontend.progress(1.0)
                return None
            return self._spawn(binary, url, build)

    def _waiting(self) -> None:
        self.frontend.status("Waiting for another nutmeg window to finish…")
        self.frontend.progress(None)

    def _ensure_prefix(self, binary: Path, prefix: Path, arch: str) -> None:
        wanted = rosemary.BASE_ARCH[arch]
        if rosemary.prefix_created(prefix):
            have = rosemary.prefix_base_arch(prefix)
            if have is not None and have != wanted:
                self.log.write(
                    f"the prefix's Darwin base is {have} but the Roblox build is {arch}; "
                    "rosemary warns about this, and Settings can install a matching base"
                )
            return
        answer = self.frontend.ask(
            "Set up the rosemary prefix",
            f"Roblox will run in its own rosemary prefix at {prefix}.\n\n"
            "rosemary can also install its Darwin base system (MiniDarwin) into the "
            "prefix. It downloads the base from GitHub and checks it against hashes "
            "built into rosemary. rosemary runs programs without a base, because it "
            "links them against its own system libraries.",
            [("base", "Download the base"), ("no-base", "Continue without it")],
        )
        if answer is None:
            raise Cancelled()
        base = wanted if answer == "base" else None
        self.frontend.status("Setting up the rosemary prefix…")
        self.frontend.progress(None)

        def output(line: str) -> None:
            self.log.write(f"rosemary: {line}")
            if line.strip():
                self.frontend.status("Setting up the rosemary prefix…", line.strip())

        rosemary.create_prefix(binary, prefix, base, output, self.cancel)

    def _ensure_build(self, arch: str) -> tuple[roblox.Build, Path]:
        self.frontend.status("Checking for Roblox updates…")
        self.frontend.progress(None)
        try:
            latest = roblox.fetch_latest(arch)
        except NutmegError as e:
            self.log.write(str(e))
            installed = roblox.load_installed()
            program = None
            if installed is not None and installed.arch == arch:
                program = roblox.installed_executable(installed)
            if installed is None or program is None:
                raise
            self.frontend.status(
                "Could not check for Roblox updates",
                f"Starting the installed version, {installed.label}.",
            )
            self.log.write(f"using the installed build {installed.key}")
            return installed, program
        self.log.write(f"current build: {latest.key} ({latest.version})")
        program = roblox.installed_executable(latest)
        if program is None:
            program = self._install(latest)
        roblox.save_installed(latest)
        return latest, program

    def _install(self, build: roblox.Build) -> Path:
        title = f"Downloading Roblox {build.label}…"
        self.frontend.status(title)

        def downloaded(done: int, total: Optional[int]) -> None:
            if total:
                self.frontend.progress(done / total)
                self.frontend.status(title, f"{_megabytes(done)} of {_megabytes(total)}")
            else:
                self.frontend.progress(None)
                self.frontend.status(title, _megabytes(done))

        archive = roblox.download(build, downloaded, self.cancel)
        self.log.write(f"downloaded {archive}")
        try:
            title = f"Installing Roblox {build.label}…"
            self.frontend.status(title)

            def unpacked(done: int, total: Optional[int]) -> None:
                if total:
                    self.frontend.progress(done / total)
                    self.frontend.status(title, f"{done} of {total} files")

            program = roblox.install(archive, build, unpacked, self.cancel)
        finally:
            archive.unlink(missing_ok=True)
        self.log.write(f"installed {build.key}")
        return program

    def _stop_running(self, binary: Path) -> None:
        running = processes.running()
        if not running:
            return
        self.log.write(f"already running: {sorted(running)}")
        answer = self.frontend.ask(
            "Roblox is already running",
            "Roblox for macOS runs one instance at a time. Close the running Roblox "
            "to continue?",
            [("close", "Close Roblox"), ("cancel", "Cancel")],
        )
        if answer != "close":
            raise Cancelled()
        self.frontend.status("Closing Roblox…")
        self.frontend.progress(None)
        left = processes.terminate(sorted(running))
        if left:
            raise NutmegError(
                "Could not close the running Roblox (process "
                + ", ".join(map(str, left))
                + ")."
            )
        if self.config.process_server and rosemary.bottle(self.config) is None:
            # As Butter's Stop button does: the prefix's process server goes
            # with the client instead of lingering (ROSEMARY_SERVER_LINGER)
            # until it times out. A status other than 0 usually means that no
            # server was running, which is not a reason to stop the launch.
            # Never in a Butter bottle's prefix: the server ends every guest
            # still connected, so it would close the bottle's other programs.
            code = rosemary.stop_server(
                binary, self.config, lambda line: self.log.write(f"rosemary: {line}"), self.cancel
            )
            self.log.write(f"rosemary server --stop exited with status {code}")

    def _spawn(
        self, binary: Path, url: Optional[str], build: roblox.Build
    ) -> Launched:
        argv = rosemary.run_argv(binary, self.config, build.bundle(), url)
        env = rosemary.run_env(self.config)
        # Where a Mac program started by the Finder begins: the guest's `/`.
        cwd = self.config.prefix() / "root"
        if not cwd.is_dir():
            cwd = Path.home()
        self.log.write("running: " + redact(shlex.join(argv)))
        for name in sorted(env):
            if name.startswith("ROSEMARY_") or name in ("SDL_VIDEO_DRIVER", "SDL_VIDEODRIVER", "VK_DRIVER_FILES"):
                self.log.write(f"  {name}={env[name]}")
        self.frontend.status("Starting Roblox…", "")
        self.frontend.progress(None)
        started = time.time()
        try:
            process = subprocess.Popen(
                argv,
                env=env,
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=self.log.fileno(),
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as e:
            raise NutmegError(f"Could not start {binary}: {e.strerror}", settings=True) from e
        self.log.write(f"started process {process.pid}")
        if self.config.discord_presence:
            try:
                helper = presence.start_helper(self.config.prefix(), started, self.log.path)
            except (OSError, NutmegError) as e:
                self.log.write(f"could not start the Discord presence: {e}")
            else:
                self.log.write(f"showing the game on Discord (process {helper.pid})")
        return Launched(process=process, log=self.log.path, build=build)

    def settle(self, launched: Launched) -> None:
        """Watch the new process briefly and raise if it failed straight away.
        Afterwards the process is left to run, and reaped in the background if
        this program is still running when it exits."""
        try:
            code = launched.process.wait(timeout=STARTUP_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            threading.Thread(target=launched.process.wait, daemon=True).start()
            return
        self.log.write(f"Roblox exited with status {code}")
        if code < 0:
            try:
                how = f"it was killed by {signal.Signals(-code).name}"
            except ValueError:
                how = f"it was killed by signal {-code}"
            raise NutmegError(f"Roblox stopped right after starting: {how}.")
        if code != 0:
            raise NutmegError(f"Roblox stopped right after starting (exit status {code}).")
