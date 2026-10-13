"""End-to-end tests of a launch: a local server stands in for Roblox's version
endpoint and CDN, and a script stands in for rosemary."""

from __future__ import annotations

import fcntl
import os
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from nutmeg import launcher as launcher_module
from nutmeg import paths, roblox, rosemary
from nutmeg.config import Config
from nutmeg.errors import Cancelled, NutmegError
from nutmeg.launcher import Launcher

from helpers import Sandbox, Server, roblox_zip

GUID_A = "version-aaaaaaaaaaaaaaaa"
GUID_B = "version-bbbbbbbbbbbbbbbb"
URL = "roblox-player:1+launchmode:play+gameinfo:ticket+launchtime:1+browsertrackerid:2"


class Frontend:
    def __init__(self, answers: dict | None = None, on_status=None):
        self.answers = answers or {}
        self.asked: list[str] = []
        self.statuses: list[tuple[str, str]] = []
        self.on_status = on_status

    def status(self, text: str, detail: str = "") -> None:
        self.statuses.append((text, detail))
        if self.on_status:
            self.on_status(text)

    def progress(self, fraction) -> None:
        pass

    def ask(self, title, message, choices):
        self.asked.append(title)
        return self.answers.get(title, choices[0][0])


class LaunchTests(Sandbox):
    def setUp(self) -> None:
        super().setUp()
        self.server = Server()
        self.patches = [
            mock.patch.object(roblox, "CLIENT_VERSION_URLS", (f"{self.server.url}/client-version",)),
            mock.patch.object(roblox, "CDN_HOSTS", (f"{self.server.url}/a", f"{self.server.url}/b")),
            mock.patch.object(rosemary, "host_arch", return_value="x86_64"),
            mock.patch.object(launcher_module, "STARTUP_GRACE_SECONDS", 0.5),
        ]
        for patch in self.patches:
            patch.start()
        self.tool = self.fake_rosemary()
        self.config = Config(rosemary_path=str(self.tool), fast_flags={"FFlagTest": True})
        self.server.publish(GUID_A, "0.700.0.1", {"x86_64": roblox_zip()}, ["b"])
        self.started: list[subprocess.Popen] = []

    def tearDown(self) -> None:
        for process in self.started:
            if process.poll() is None:
                process.kill()
            process.wait()
        for patch in self.patches:
            patch.stop()
        self.server.close()
        super().tearDown()

    def launch(self, frontend=None, url=URL, start=True, cancel=None):
        frontend = frontend or Frontend()
        launcher = Launcher(self.config, frontend, cancel or threading.Event())
        try:
            launched = launcher.run(url, start)
            if launched is not None:
                self.started.append(launched.process)
                launcher.settle(launched)
        finally:
            launcher.log.close()
        return launched, frontend, launcher

    def test_first_launch(self):
        launched, frontend, launcher = self.launch()
        prefix = paths.default_prefix()
        build = roblox.Build(GUID_A, "0.700.0.1", "x86_64")
        program = build.bundle() / "Contents/MacOS/RobloxPlayer"
        self.assertEqual(frontend.asked, ["Set up the rosemary prefix"])
        self.assertEqual(rosemary.prefix_base_arch(prefix), "x86_64")
        record = self.record()
        self.assertEqual(record["argv"], ["run", str(build.bundle()), "-protocolString", URL])
        self.assertEqual(record["env"]["ROSEMARY_PREFIX"], str(prefix))
        self.assertEqual(os.path.realpath(record["cwd"]), os.path.realpath(prefix / "root"))
        self.assertEqual(roblox.load_installed(), build)
        flags = program.parent / "ClientSettings/ClientAppSettings.json"
        self.assertEqual(flags.read_text().strip(), '{\n  "FFlagTest": true\n}')
        # The first CDN host has nothing, so the second was used.
        self.assertIn(f"/a/mac/{GUID_A}-RobloxPlayer.zip", self.server.requests)
        self.assertIn(f"/b/mac/{GUID_A}-RobloxPlayer.zip", self.server.requests)
        self.assertEqual(list(paths.downloads_dir().iterdir()), [])
        self.assertIsNone(launched.process.poll())
        log = self.wait_for_text(launched.log, "guest output")
        self.assertIn("guest output", log)
        self.assertIn("running: ", log)
        self.assertIn("gameinfo:<hidden>", log)
        self.assertNotIn("ticket", log)

    def test_second_launch_closes_the_first(self):
        first, _, _ = self.launch()
        with self.assertRaises(Cancelled):
            self.launch(Frontend({"Roblox is already running": "cancel"}))
        self.assertIsNone(first.process.poll())
        second, frontend, _ = self.launch()
        self.assertEqual(frontend.asked, ["Roblox is already running"])
        self.assertIsNotNone(first.process.poll())
        self.assertIsNone(second.process.poll())
        # The prefix's process server was asked to stop with the first Roblox.
        stopped = Path(os.environ["FAKE_ROSEMARY_RECORD"] + ".server").read_text()
        self.assertEqual(stopped, f"{paths.default_prefix()}\n")
        self.assertIn("rosemary server --stop exited with status 1", second.log.read_text())

    def test_in_a_butter_bottle_the_bottles_settings_apply(self):
        prefix = self.butter_bottle(
            "roblox", 'name = "Roblox"\ndisable_metal = true\nswap_interval = "vsync"\n\n[env]\nFOO = "bar"\n'
        )
        self.config.prefix_path = str(prefix)
        self.config.swap_interval = "0"
        self.config.dead_flags = True
        first, _, _ = self.launch()
        env = self.record()["env"]
        self.assertEqual(env["ROSEMARY_PREFIX"], str(prefix))
        self.assertEqual(env["ROSEMARY_SWAP_INTERVAL"], "1")
        self.assertNotIn("ROSEMARY_DEAD_FLAGS", env)
        log = self.wait_for_text(first.log, "guest output")
        self.assertIn("VK_DRIVER_FILES=/dev/null", log)
        self.assertIn("using its settings", log)
        # Closing the first Roblox leaves the bottle's process server alone.
        self.launch()
        self.assertFalse(Path(os.environ["FAKE_ROSEMARY_RECORD"] + ".server").exists())

    def test_discord_presence_is_started_when_on(self):
        launched, _, _ = self.launch()
        self.presence_helper.assert_called_once()
        prefix, since, log = self.presence_helper.call_args.args
        self.assertEqual((prefix, log), (paths.default_prefix(), launched.log))
        self.assertLessEqual(since, time.time())
        self.presence_helper.reset_mock()
        self.config.discord_presence = False
        self.launch()
        self.presence_helper.assert_not_called()

    def test_no_server_to_stop_when_it_is_off(self):
        self.config.process_server = False
        self.launch()
        self.launch()
        self.assertFalse(Path(os.environ["FAKE_ROSEMARY_RECORD"] + ".server").exists())
        self.assertEqual(self.record()["env"]["ROSEMARY_SERVER"], "off")

    def test_update_replaces_the_old_build(self):
        self.launch(start=False)
        old = roblox.Build(GUID_A, "", "x86_64").directory()
        self.assertTrue(old.is_dir())
        self.server.publish(GUID_B, "0.701.0.1", {"x86_64": roblox_zip()}, ["b"])
        self.launch(start=False)
        self.assertEqual(roblox.installed_keys(), [f"{GUID_B}-x86_64"])
        self.assertEqual(roblox.load_installed().guid, GUID_B)

    def test_a_build_in_use_is_kept(self):
        first, _, _ = self.launch()
        self.server.publish(GUID_B, "0.701.0.1", {"x86_64": roblox_zip()}, ["b"])
        self.launch(start=False)
        self.assertEqual(roblox.installed_keys(), [f"{GUID_A}-x86_64", f"{GUID_B}-x86_64"])
        first.process.kill()
        first.process.wait()
        self.launch(start=False)
        self.assertEqual(roblox.installed_keys(), [f"{GUID_B}-x86_64"])

    def test_offline_launch_uses_the_installed_build(self):
        self.launch(start=False)
        self.server.routes["/client-version"] = 503
        launched, frontend, _ = self.launch()
        self.assertIn(("Could not check for Roblox updates", "Starting the installed version, 0.700.0.1."), frontend.statuses)
        self.assertIsNone(launched.process.poll())

    def test_offline_with_nothing_installed_fails(self):
        self.server.routes["/client-version"] = 503
        with self.assertRaisesRegex(NutmegError, "current Roblox version"):
            self.launch()

    def test_immediate_failure_is_reported(self):
        os.environ["FAKE_ROSEMARY_EXIT"] = "3"
        with self.assertRaisesRegex(NutmegError, "exit status 3"):
            self.launch()

    def test_truncated_download_falls_back_to_the_next_host(self):
        data = roblox_zip()
        self.server.routes[f"/a/mac/{GUID_A}-RobloxPlayer.zip"] = (data[:100], len(data))
        self.launch(start=False)
        self.assertEqual(roblox.installed_keys(), [f"{GUID_A}-x86_64"])
        self.assertIn(f"/b/mac/{GUID_A}-RobloxPlayer.zip", self.server.requests)

    def test_every_host_failing_is_reported(self):
        del self.server.routes[f"/b/mac/{GUID_A}-RobloxPlayer.zip"]
        with self.assertRaisesRegex(NutmegError, "Could not download Roblox 0.700.0.1"):
            self.launch()
        self.assertEqual(list(paths.downloads_dir().iterdir()), [])

    def test_cancel_during_download(self):
        cancel = threading.Event()

        def on_status(text: str) -> None:
            if text.startswith("Downloading"):
                cancel.set()

        with self.assertRaises(Cancelled):
            self.launch(Frontend(on_status=on_status), cancel=cancel)
        self.assertEqual(list(paths.downloads_dir().iterdir()), [])
        self.assertEqual(roblox.installed_keys(), [])

    def test_prefix_without_base(self):
        self.launch(Frontend({"Set up the rosemary prefix": "no-base"}))
        self.assertTrue(rosemary.prefix_created(paths.default_prefix()))
        self.assertIsNone(rosemary.prefix_base_arch(paths.default_prefix()))

    def test_dismissing_the_prefix_question_cancels(self):
        with self.assertRaises(Cancelled):
            self.launch(Frontend({"Set up the rosemary prefix": None}))
        self.assertFalse(rosemary.prefix_created(paths.default_prefix()))
        self.assertEqual(roblox.installed_keys(), [])

    def test_waits_for_another_launch(self):
        paths.lock_file().parent.mkdir(parents=True, exist_ok=True)
        holder = os.open(paths.lock_file(), os.O_RDWR | os.O_CREAT)
        fcntl.flock(holder, fcntl.LOCK_EX)
        waiting = threading.Event()
        frontend = Frontend(on_status=lambda text: text.startswith("Waiting") and waiting.set())
        result: list = []
        worker = threading.Thread(target=lambda: result.append(self.launch(frontend, start=False)))
        worker.start()
        self.assertTrue(waiting.wait(5))
        time.sleep(0.3)
        self.assertEqual(roblox.installed_keys(), [])
        os.close(holder)
        worker.join(10)
        self.assertEqual(len(result), 1)
        self.assertEqual(roblox.installed_keys(), [f"{GUID_A}-x86_64"])

    def test_missing_rosemary(self):
        self.config.rosemary_path = str(self.root / "missing")
        with self.assertRaises(NutmegError) as caught:
            self.launch()
        self.assertTrue(caught.exception.settings)

    def test_roblox_does_not_inherit_the_lock(self):
        launched, _, _ = self.launch()
        # With the launcher finished, the lock must be free although Roblox runs.
        fd = os.open(paths.lock_file(), os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)
        self.assertIsNone(launched.process.poll())


class CommandLineTests(Sandbox):
    def run_cli(self, *args: str) -> subprocess.CompletedProcess:
        root = Path(__file__).resolve().parent.parent
        env = dict(os.environ, PYTHONPATH=str(root))
        return subprocess.run(
            [sys.executable, "-m", "nutmeg", *args], env=env, capture_output=True, text=True, timeout=60,
            stdin=subprocess.DEVNULL,
        )

    def test_refuses_foreign_links(self):
        result = self.run_cli("launch", "--no-gui", "https://example.com")
        self.assertEqual(result.returncode, 2)
        self.assertIn("not a Roblox link", result.stderr)

    def test_no_terminal_means_no(self):
        self.fake_rosemary()
        (paths.config_dir()).mkdir(parents=True)
        paths.config_file().write_text('{"rosemary_path": "%s"}' % (self.root / "bin/rosemary"))
        result = self.run_cli("update", "--no-gui")
        self.assertEqual(result.returncode, 1)
        self.assertIn("no terminal to ask on", result.stderr)
        self.assertFalse(rosemary.prefix_created(paths.default_prefix()))

    def test_version(self):
        result = self.run_cli("--version")
        self.assertEqual(result.returncode, 0)
        self.assertRegex(result.stdout, r"^nutmeg \d")


if __name__ == "__main__":
    unittest.main()
