"""Tests of the pieces that do not need the network, a display or rosemary."""

from __future__ import annotations

import io
import json
import os
import struct
import subprocess
import sys
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from nutmeg import config as config_module
from nutmeg import butter, fastflags, paths, processes, protocol, roblox, rosemary
from nutmeg.config import Config, ConfigError
from nutmeg.errors import NutmegError

from helpers import Sandbox, fat, macho, roblox_zip

# A bottle.toml shaped as Butter's toml::to_string_pretty writes one.
BOTTLE_TOML = """\
name = "Roblox"
runner = "rosemary-v0-3-0-official"
created = 1760000000
arch = "host"
system_files = "repair"
backend = "emulated"
decorations = "server"
menu = "hidden"
swap_interval = "adaptive"
prefix_home = "prefix"
steam = false
menu_ctrl = true
confine_pointer = false
disable_metal = true
process_server = true
dead_flags = true
call_return = false
strict_imports = false

[env]
FOO = "bar"
ROSEMARY_STEAM = "1"

[[programs]]
name = "Tool"
path = "/x/tool"
args = [
    "[not a table]",
    "--flag",
]
backend = "native"
"""


class ProtocolTests(unittest.TestCase):
    def test_accepts_both_schemes(self):
        for url in (
            "roblox-player:1+launchmode:play+gameinfo:abc+launchtime:1+placelauncherurl:https%3A%2F%2Fx",
            "roblox://experiences/start?placeId=1818",
            "ROBLOX-PLAYER:1+launchmode:app",
        ):
            self.assertEqual(protocol.check(url), url)

    def test_redacts_the_ticket(self):
        url = "roblox-player:1+launchmode:play+gameinfo:SECRET_TICKET+launchtime:1+browsertrackerid:2"
        self.assertEqual(
            protocol.redact(f"run x -protocolString {url}"),
            "run x -protocolString roblox-player:1+launchmode:play+gameinfo:<hidden>+launchtime:1+browsertrackerid:2",
        )
        self.assertEqual(protocol.redact("roblox://experiences/start?placeId=1"), "roblox://experiences/start?placeId=1")

    def test_refuses_other_links(self):
        for url in ("https://roblox.com", "roblox-player:", "file:///etc/passwd", "", "roblox", "roblox-player:1\n2"):
            with self.assertRaises(protocol.InvalidURL):
                protocol.check(url)


class FastFlagTests(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(
            [fastflags.kind_of(v) for v in (True, 144, "x", 1.5, None, [1], {"a": 1})],
            ["bool", "int", "string", "json", "json", "json", "json"],
        )
        self.assertEqual(
            [fastflags.kind_for_name(n) for n in ("FFlagX", "DFFlagX", "SFFlagX", "FIntX", "DFIntX", "FStringX", "DFStringX")],
            ["bool", "bool", "bool", "int", "int", "string", "string"],
        )
        for name in ("FFlag", "FLogX", "Other", ""):
            self.assertIsNone(fastflags.kind_for_name(name))

    def test_values(self):
        self.assertEqual(fastflags.value_of("int", " -42 ", False), -42)
        self.assertIs(fastflags.value_of("bool", "ignored", True), True)
        self.assertEqual(fastflags.value_of("string", " spaced ", False), " spaced ")
        self.assertEqual(fastflags.value_of("json", '{"a": [1, null]}', False), {"a": [1, None]})
        for kind, text in (("int", "1.5"), ("int", "1_000"), ("int", ""), ("json", "{"), ("json", "")):
            with self.subTest(kind=kind, text=text), self.assertRaises(ValueError):
                fastflags.value_of(kind, text, False)
        for value in (True, 7, "s", 2.5, None, [1, "x"], {"k": False}):
            kind = fastflags.kind_of(value)
            self.assertEqual(fastflags.value_of(kind, fastflags.text_of(value), bool(value)), value)

    def test_collect(self):
        rows = [("  FFlagA ", "bool", "", True), ("FIntB", "int", "60", False)]
        self.assertEqual(fastflags.collect(rows), {"FFlagA": True, "FIntB": 60})
        with self.assertRaisesRegex(ConfigError, "row 2 has no name"):
            fastflags.collect([("A", "bool", "", True), (" ", "bool", "", True)])
        with self.assertRaisesRegex(ConfigError, "A is listed twice"):
            fastflags.collect([("A", "bool", "", True), ("A ", "int", "1", False)])
        with self.assertRaisesRegex(ConfigError, "FIntB needs a whole number"):
            fastflags.collect([("FIntB", "int", "sixty", False)])

    def test_parse_object(self):
        self.assertEqual(fastflags.parse_object("  "), {})
        self.assertEqual(fastflags.parse_object('{"A": 1, "B": true}'), {"A": 1, "B": True})
        for text in ("[1]", '"x"', "{"):
            with self.assertRaises(ConfigError):
                fastflags.parse_object(text)


class ConfigTests(Sandbox):
    def test_defaults_when_nothing_is_saved(self):
        self.assertEqual(config_module.load(), Config())
        self.assertEqual(Config().prefix(), paths.default_prefix())

    def test_round_trip(self):
        config = Config(
            rosemary_path="/opt/rosemary/target/release/rosemary",
            prefix_path="~/prefixes/roblox",
            architecture="x86_64",
            swap_interval="0",
            menu_ctrl=False,
            extra_env={"RUST_LOG": "debug", "A": "b=c"},
            fast_flags={"FFlagExample": True},
        )
        config_module.save(config)
        self.assertEqual(config_module.load(), config)
        self.assertEqual(config.prefix(), Path.home() / "prefixes/roblox")

    def test_rejects_bad_values(self):
        bad = [
            {"architecture": "ppc"},
            {"engine": "fast"},
            {"swap_interval": "2"},
            {"menu_ctrl": "yes"},
            {"prefix_path": "relative/prefix"},
            {"extra_env": {"ROSEMARY_PREFIX": "/x"}},
            {"extra_env": {"1BAD": "x"}},
            {"extra_env": {"GOOD": 1}},
            {"fast_flags": [1, 2]},
            {"prefix_home": "elsewhere"},
            {"process_server": "off"},
            {"metal_presentation_mode": "vsync"},
            {"screen_refresh_rate": 60},
            {"screen_refresh_rate": "0"},
            {"screen_refresh_rate": "fast"},
            {"screen_refresh_rate": "inf"},
            {"screen_refresh_rate": "1_000"},
            {"video_driver": "xwayland"},
        ]
        for data in bad:
            with self.subTest(data=data), self.assertRaises(ConfigError):
                Config.from_dict(data)

    def test_variables_that_became_settings_are_moved_out_of_extra_env(self):
        config = Config.from_dict(
            {
                "extra_env": {
                    "ROSEMARY_PREFIX_HOME": "host",
                    "ROSEMARY_SERVER": "0",
                    "ROSEMARY_METAL_PRESENTATION_MODE": "fifo",
                    "ROSEMARY_SCREEN_REFRESH_RATE": " 59.94 ",
                    "RUST_LOG": "debug",
                }
            }
        )
        self.assertEqual(config.prefix_home, "host")
        self.assertFalse(config.process_server)
        self.assertEqual(config.metal_presentation_mode, "fifo")
        self.assertEqual(config.screen_refresh_rate, "59.94")
        self.assertEqual(config.extra_env, {"RUST_LOG": "debug"})
        # SDL reads SDL_VIDEO_DRIVER before its older spelling, and so does this.
        config = Config.from_dict({"extra_env": {"SDL_VIDEODRIVER": "wayland", "SDL_VIDEO_DRIVER": "X11"}})
        self.assertEqual((config.video_driver, config.extra_env), ("x11", {}))
        config = Config.from_dict({"extra_env": {"SDL_VIDEODRIVER": "wayland"}})
        self.assertEqual(config.video_driver, "wayland")
        # Values rosemary would ignore become the defaults, which it uses then.
        config = Config.from_dict(
            {"extra_env": {"ROSEMARY_METAL_PRESENTATION_MODE": "vsync", "ROSEMARY_SCREEN_REFRESH_RATE": "0"}}
        )
        self.assertEqual((config.metal_presentation_mode, config.screen_refresh_rate), ("", ""))
        # A file that has the setting already keeps it, and the variable is refused.
        with self.assertRaises(ConfigError):
            Config.from_dict({"process_server": True, "extra_env": {"ROSEMARY_SERVER": "off"}})

    def test_unreadable_file_is_an_error_not_the_defaults(self):
        paths.config_file().parent.mkdir(parents=True)
        paths.config_file().write_text("{not json")
        with self.assertRaises(ConfigError):
            config_module.load()

    def test_unknown_keys_are_ignored(self):
        self.assertEqual(Config.from_dict({"future_setting": 1}), Config())


class MachOTests(Sandbox):
    def check(self, data: bytes) -> set:
        path = self.root / "binary"
        path.write_bytes(data)
        return roblox.macho_architectures(path)

    def test_thin_and_fat(self):
        self.assertEqual(self.check(macho("x86_64")), {"x86_64"})
        self.assertEqual(self.check(macho("arm64")), {"arm64"})
        self.assertEqual(self.check(fat("x86_64", "arm64")), {"x86_64", "arm64"})

    def test_not_macho(self):
        self.assertEqual(self.check(b"\x7fELF" + b"\0" * 60), set())
        # A Java class file begins with the fat magic, then its version.
        self.assertEqual(self.check(bytes.fromhex("cafebabe00000034") + b"\0" * 64), set())
        self.assertEqual(self.check(b"\xcf"), set())


class InstallTests(Sandbox):
    build = roblox.Build(guid="version-0123456789abcdef", version="0.1.0.1", arch="x86_64")

    def install(self, data: bytes, build: roblox.Build | None = None) -> Path:
        archive = self.root / "archive.zip"
        archive.write_bytes(data)
        return roblox.install(archive, build or self.build, lambda *_: None, threading.Event())

    def test_installs_as_roblox_app_and_keeps_only_the_executable_bit(self):
        os.umask(0o022)
        link = ("RobloxPlayer.app/Contents/Frameworks/Current", b"Versions/A", 0o120777)
        program = self.install(roblox_zip(extra=[link]))
        bundle = self.build.bundle()
        self.assertEqual(program, bundle / "Contents/MacOS/RobloxPlayer")
        self.assertEqual(program.stat().st_mode & 0o777, 0o755)
        resource = bundle / "Contents/Resources/content.txt"
        self.assertEqual(resource.stat().st_mode & 0o777, 0o644)
        self.assertEqual(os.readlink(bundle / "Contents/Frameworks/Current"), "Versions/A")
        self.assertEqual(roblox.installed_executable(self.build), program)
        self.assertEqual(roblox.installed_keys(), [self.build.key])
        self.assertEqual([p.name for p in paths.versions_dir().iterdir()], [self.build.key])

    def test_refuses_unsafe_archives(self):
        cases = [
            [("RobloxPlayer.app/../../escape", b"x", 0o100644)],
            [("/tmp/absolute", b"x", 0o100644)],
            [("Other.app/file", b"x", 0o100644)],
            [("RobloxPlayer.app/link", b"../../outside", 0o120777)],
            [("RobloxPlayer.app/link", b"/etc", 0o120777)],
            [("RobloxPlayer.app/fifo", b"", 0o010644)],
        ]
        for extra in cases:
            with self.subTest(extra=extra[0][0]):
                with self.assertRaises(NutmegError):
                    self.install(roblox_zip(extra=extra))
                self.assertEqual(list(paths.versions_dir().iterdir()), [])
        self.assertFalse((self.root / "escape").exists())

    def test_wrong_architecture_is_refused(self):
        with self.assertRaisesRegex(NutmegError, "expected to be arm64"):
            self.install(roblox_zip("x86_64"), roblox.Build(self.build.guid, "", "arm64"))
        self.assertEqual(list(paths.versions_dir().iterdir()), [])

    def test_corruption_is_detected(self):
        name = "RobloxPlayer.app/Contents/Resources/content.txt"
        for compression in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            with self.subTest(compression=compression):
                source = zipfile.ZipFile(io.BytesIO(roblox_zip()))
                buffer = io.BytesIO()
                with zipfile.ZipFile(buffer, "w") as rewritten:
                    for info in source.infolist():
                        contents = source.read(info)
                        info.compress_type = compression
                        rewritten.writestr(info, contents)
                data = bytearray(buffer.getvalue())
                info = zipfile.ZipFile(io.BytesIO(bytes(data))).getinfo(name)
                start = info.header_offset
                name_length, extra_length = struct.unpack("<HH", data[start + 26 : start + 30])
                body = start + 30 + name_length + extra_length
                data[body + info.compress_size // 2] ^= 0xFF
                with self.assertRaises(NutmegError):
                    self.install(bytes(data))
                self.assertEqual(list(paths.versions_dir().iterdir()), [])

    def test_cancel_leaves_nothing(self):
        cancel = threading.Event()
        cancel.set()
        archive = self.root / "archive.zip"
        archive.write_bytes(roblox_zip())
        with self.assertRaises(Exception) as caught:
            roblox.install(archive, self.build, lambda *_: None, cancel)
        self.assertEqual(type(caught.exception).__name__, "Cancelled")
        self.assertEqual(list(paths.versions_dir().iterdir()), [])

    def test_fast_flags(self):
        self.install(roblox_zip())
        bundle = self.build.bundle()
        roblox.write_fast_flags(bundle, {"FIntTargetRefreshRate": 144})
        written = bundle / "Contents/MacOS/ClientSettings/ClientAppSettings.json"
        self.assertEqual(json.loads(written.read_text()), {"FIntTargetRefreshRate": 144})
        roblox.write_fast_flags(bundle, {})
        self.assertFalse(written.exists())

    def test_installed_record(self):
        self.assertIsNone(roblox.load_installed())
        roblox.save_installed(self.build)
        self.assertEqual(roblox.load_installed(), self.build)
        paths.installed_file().write_text('{"guid": "version-../../x", "arch": "x86_64"}')
        self.assertIsNone(roblox.load_installed())

    def test_remove_versions(self):
        for key in ("version-aa-x86_64", "version-bb-x86_64", "version-cc-x86_64", ".tmp-version-dd-x86_64-1"):
            (paths.versions_dir() / key).mkdir(parents=True)
        removed = roblox.remove_versions({"version-aa-x86_64"}, {"version-bb-x86_64"})
        self.assertEqual(removed, ["version-cc-x86_64"])
        roblox.remove_leftovers()
        self.assertEqual(sorted(p.name for p in paths.versions_dir().iterdir()), ["version-aa-x86_64", "version-bb-x86_64"])

    def test_zip_urls(self):
        x86 = roblox.Build("version-1", "", "x86_64").zip_urls()
        arm = roblox.Build("version-1", "", "arm64").zip_urls()
        self.assertEqual(x86[0], "https://setup.rbxcdn.com/mac/version-1-RobloxPlayer.zip")
        self.assertEqual(arm[0], "https://setup.rbxcdn.com/mac/arm64/version-1-RobloxPlayer.zip")
        self.assertEqual(len(x86), len(roblox.CDN_HOSTS))


class RosemaryTests(Sandbox):
    def test_base_arch_from_state_file(self):
        prefix = self.root / "prefix"
        (prefix / ".rosemary").mkdir(parents=True)
        state = prefix / ".rosemary/state.yaml"
        self.assertFalse(rosemary.prefix_created(prefix))
        state.write_text('version: 1\ncreated_with:\n  runtime_version: "0.1.0"\n')
        self.assertTrue(rosemary.prefix_created(prefix))
        self.assertIsNone(rosemary.prefix_base_arch(prefix))
        # The shape rosemary's State::render writes.
        state.write_text(
            '# Written by `rosemary prefix`. See docs/prefixes.md.\nversion: 1\ncreated_with:\n'
            '  runtime_version: "0.1.0"\nbase:\n  project: "minidarwin"\n  arch: "aarch64"\n'
            '  sequence: 9\n  tag: "v9.0.0"\naccepted_sequence: 9\n'
        )
        self.assertEqual(rosemary.prefix_base_arch(prefix), "aarch64")
        state.write_text("base:\n  arch: x86_64\n")
        self.assertEqual(rosemary.prefix_base_arch(prefix), "x86_64")

    def test_environment(self):
        inherited = {
            "ROSEMARY_DEAD_FLAGS": "1",
            "ROSEMARY_PREFIX": "/elsewhere",
            "PATH": "/bin",
            "ROSEMARY_OBJC": "builtin",
            "SDL_VIDEODRIVER": "x11",
        }
        env = rosemary.run_env(Config(), inherited)
        self.assertNotIn("ROSEMARY_DEAD_FLAGS", env)
        self.assertNotIn("SDL_VIDEODRIVER", env)
        self.assertEqual(env["ROSEMARY_PREFIX"], str(paths.default_prefix()))
        self.assertEqual(env["ROSEMARY_OBJC"], "builtin")
        self.assertEqual(env["PATH"], "/bin")
        config = Config(
            swap_interval="-1",
            decorations="server",
            menu="strip",
            menu_ctrl=False,
            confine_pointer=True,
            dead_flags=True,
            call_return=True,
            prefix_home="host",
            process_server=False,
            metal_presentation_mode="immediate",
            screen_refresh_rate="144",
            video_driver="wayland",
            extra_env={"RUST_BACKTRACE": "1"},
        )
        env = rosemary.run_env(config, {"ROSEMARY_SCREEN_REFRESH_RATE": "30"})
        self.assertEqual(
            {k: v for k, v in env.items() if k != "ROSEMARY_PREFIX"},
            {
                "ROSEMARY_SWAP_INTERVAL": "-1",
                "ROSEMARY_DECORATIONS": "server",
                "ROSEMARY_MENU": "strip",
                "ROSEMARY_MENU_CTRL": "0",
                "ROSEMARY_CONFINE_POINTER": "1",
                "ROSEMARY_DEAD_FLAGS": "1",
                "ROSEMARY_CALL_RETURN": "1",
                "ROSEMARY_PREFIX_HOME": "host",
                "ROSEMARY_SERVER": "off",
                "ROSEMARY_METAL_PRESENTATION_MODE": "immediate",
                "ROSEMARY_SCREEN_REFRESH_RATE": "144",
                "SDL_VIDEO_DRIVER": "wayland",
                "RUST_BACKTRACE": "1",
            },
        )

    def test_a_butter_bottle_prefix_uses_the_bottles_settings(self):
        prefix = self.butter_bottle("roblox", BOTTLE_TOML)
        config = Config(
            prefix_path=str(prefix),
            engine="native",
            swap_interval="1",
            decorations="client",
            video_driver="x11",
            extra_env={"RUST_LOG": "debug"},
        )
        env = rosemary.run_env(config, {"PATH": "/bin", "ROSEMARY_MENU": "strip"})
        self.assertEqual(env["PATH"], "/bin")
        self.assertEqual(env["ROSEMARY_PREFIX"], str(prefix))
        self.assertEqual(env["ROSEMARY_SWAP_INTERVAL"], "-1")
        self.assertEqual(env["ROSEMARY_DECORATIONS"], "server")
        self.assertEqual(env["ROSEMARY_MENU"], "none")
        self.assertEqual(env["VK_DRIVER_FILES"], "/dev/null")
        for name in ("SDL_VIDEO_DRIVER", "RUST_LOG"):
            self.assertNotIn(name, env)
        app = Path("/data/versions/v/Roblox.app")
        self.assertEqual(
            rosemary.run_argv(Path("/bin/rosemary"), config, app, None),
            ["/bin/rosemary", "run", "--emulated", str(app)],
        )
        with mock.patch.object(rosemary, "host_arch", return_value="arm64"):
            # The bottle emulates, so the arm64 build is refused, and the
            # message says where execution is chosen.
            with self.assertRaisesRegex(NutmegError, "the Roblox bottle's settings in Butter"):
                rosemary.build_arch(config)
            self.assertEqual(rosemary.build_arch(Config(prefix_path=str(prefix), architecture="x86_64")), "x86_64")

    def test_command_line(self):
        program = Path("/data/versions/v/Roblox.app")
        tool = Path("/bin/rosemary")
        self.assertEqual(rosemary.run_argv(tool, Config(), program, None), ["/bin/rosemary", "run", str(program)])
        self.assertEqual(
            rosemary.run_argv(tool, Config(engine="emulated"), program, "roblox-player:1"),
            ["/bin/rosemary", "run", "--emulated", str(program), "-protocolString", "roblox-player:1"],
        )

    def test_build_choice(self):
        with mock.patch.object(rosemary, "host_arch", return_value="x86_64"):
            self.assertEqual(rosemary.build_arch(Config()), "x86_64")
            with self.assertRaises(NutmegError):
                rosemary.build_arch(Config(architecture="arm64"))
        with mock.patch.object(rosemary, "host_arch", return_value="arm64"):
            self.assertEqual(rosemary.build_arch(Config()), "arm64")
            self.assertEqual(rosemary.build_arch(Config(architecture="x86_64")), "x86_64")
            self.assertEqual(rosemary.build_arch(Config(architecture="x86_64", engine="emulated")), "x86_64")
            for bad in (Config(architecture="x86_64", engine="native"), Config(engine="emulated")):
                with self.assertRaises(NutmegError):
                    rosemary.build_arch(bad)
        with mock.patch.object(rosemary, "host_arch", return_value=None):
            with self.assertRaises(NutmegError):
                rosemary.build_arch(Config())

    def test_find_rosemary(self):
        with mock.patch.dict(os.environ, {"PATH": str(self.root / "nowhere")}):
            with self.assertRaises(NutmegError):
                rosemary.find_rosemary(Config())
            tool = self.fake_rosemary()
            self.assertEqual(rosemary.find_rosemary(Config(rosemary_path=str(tool))), tool)
        with mock.patch.dict(os.environ, {"PATH": str(tool.parent)}):
            self.assertEqual(rosemary.find_rosemary(Config()), tool)
        with self.assertRaises(NutmegError):
            rosemary.find_rosemary(Config(rosemary_path=str(self.root)))

    def test_find_rosemary_with_butter(self):
        with mock.patch.dict(os.environ, {"PATH": str(self.root / "nowhere")}):
            with self.assertRaisesRegex(NutmegError, "Install a rosemary runner with Butter"):
                rosemary.find_rosemary(Config())
            tool = self.butter_runner("rosemary-v1", 'name = "Rosemary 1"\nexecutable = "bin/rosemary"\n')
            with self.assertRaisesRegex(NutmegError, "Choose one of the runners Butter has installed"):
                rosemary.find_rosemary(Config())
            self.assertEqual(rosemary.find_rosemary(Config(rosemary_path=str(tool))), tool)
            tool.unlink()
            with self.assertRaisesRegex(NutmegError, "Butter runner rosemary-v1 is no longer installed"):
                rosemary.find_rosemary(Config(rosemary_path=str(tool)))

    def test_stop_server(self):
        tool = self.fake_rosemary()
        lines = []
        code = rosemary.stop_server(tool, Config(), lines.append, threading.Event())
        self.assertEqual(code, 1)
        self.assertIn("no server", lines[0])
        stopped = Path(os.environ["FAKE_ROSEMARY_RECORD"] + ".server").read_text()
        self.assertEqual(stopped, f"{paths.default_prefix()}\n")

    def test_create_prefix(self):
        tool = self.fake_rosemary()
        prefix = self.root / "prefix"
        lines = []
        rosemary.create_prefix(tool, prefix, "x86_64", lines.append, threading.Event())
        called = json.loads(Path(os.environ["FAKE_ROSEMARY_RECORD"] + ".prefix").read_text())
        self.assertEqual(called["argv"], ["prefix", "create", str(prefix), "--arch", "x86_64"])
        self.assertEqual(called["setup"], "download")
        self.assertEqual(lines, [f"created {prefix}"])
        self.assertEqual(rosemary.prefix_base_arch(prefix), "x86_64")

        other = self.root / "other"
        rosemary.create_prefix(tool, other, None, lines.append, threading.Event())
        called = json.loads(Path(os.environ["FAKE_ROSEMARY_RECORD"] + ".prefix").read_text())
        self.assertEqual(called["argv"], ["prefix", "create", str(other), "--no-base"])
        self.assertIsNone(rosemary.prefix_base_arch(other))

        os.environ["FAKE_ROSEMARY_PREFIX_FAIL"] = "1"
        with self.assertRaisesRegex(NutmegError, "could not download the base"):
            rosemary.create_prefix(tool, self.root / "third", "x86_64", lines.append, threading.Event())


class ButterTests(Sandbox):
    def test_manifest(self):
        path = self.root / "runner.toml"
        path.write_text(
            'name = "Rosemary \\"dev\\" \\u00e9"\n'
            "executable = 'C:\\odd\\path' # comment\n"
            "installed = 1_760_000_000\n"
            'notes = """\n'
            "[not a table]\n"
            '"""\n'
            "\n"
            "[env]\n"
            '"QUOTED" = "from a table"\n'
            "[[programs]]\n"
            'name = "skipped"\n'
        )
        self.assertEqual(
            butter.read_manifest(path),
            {
                "name": 'Rosemary "dev" \u00e9',
                "executable": "C:\\odd\\path",
                "installed": 1760000000,
                "env": {"QUOTED": "from a table"},
            },
        )
        self.assertEqual(butter.read_manifest(self.root / "missing.toml"), {})

    def test_runners(self):
        self.assertEqual(butter.runners(), [])
        self.butter_runner("custom", 'name = "Mine"\nkind = "custom"\nexecutable = "bin/rosemary"\n')
        self.butter_runner("rosemary-v0-9-0", 'name = "Rosemary 0.9.0"\nkind = "source"\ntag = "v0.9.0"\nexecutable = "bin/rosemary"\n')
        self.butter_runner(
            "rosemary-v0-10-0-official",
            'name = "Rosemary 0.10.0 (Official Build)"\nkind = "binary"\ntag = "v0.10.0"\nexecutable = "bin/rosemary"\n',
            flatpak=True,
        )
        self.butter_runner("rosemary-git-abc", 'name = "Rosemary dev abc"\nkind = "source"\nexecutable = "bin/rosemary"\n')
        self.butter_runner(".rosemary-v2-partial", 'name = "Unpacking"\nexecutable = "bin/rosemary"\n')
        self.butter_runner("rosemary-v3", 'name = "No binary"\nexecutable = "bin/gone"\n')
        found = butter.runners()
        self.assertEqual(
            [r.label() for r in found],
            [
                "Rosemary 0.10.0 (Official Build) (Butter Flatpak)",
                "Rosemary 0.9.0 (Butter)",
                "Rosemary dev abc (Butter)",
                "Mine (Butter)",
            ],
        )
        self.assertEqual(found[1].executable, self.root / "data/butter/runners/rosemary-v0-9-0/bin/rosemary")
        self.assertEqual(
            butter.runner_directory(found[1].executable), self.root / "data/butter/runners/rosemary-v0-9-0"
        )
        self.assertIsNone(butter.runner_directory(self.root / "bin/rosemary"))

    def test_bottle_settings_become_butters_environment(self):
        prefix = self.butter_bottle("roblox", 'name = "Roblox"\n')
        bottle = butter.bottle_for_prefix(prefix)
        # A new bottle: Butter sets only the prefix.
        self.assertEqual(bottle.environment(), {"ROSEMARY_PREFIX": str(prefix)})
        self.assertEqual(bottle.engine(), "auto")
        (prefix.parent / "bottle.toml").write_text(BOTTLE_TOML)
        bottle = butter.bottle_for_prefix(prefix)
        self.assertEqual(
            bottle.environment(),
            {
                "ROSEMARY_PREFIX": str(prefix),
                "ROSEMARY_DECORATIONS": "server",
                "ROSEMARY_MENU": "none",
                "ROSEMARY_SWAP_INTERVAL": "-1",
                "VK_DRIVER_FILES": "/dev/null",
                "ROSEMARY_DEAD_FLAGS": "1",
                # The bottle's own variables win, as in Butter.
                "ROSEMARY_STEAM": "1",
                "FOO": "bar",
            },
        )
        self.assertEqual(bottle.engine(), "emulated")
        self.assertEqual(butter.bottle_for_prefix(Path(str(prefix) + "/")), bottle)
        self.assertIsNone(butter.bottle_for_prefix(paths.default_prefix()))
        self.assertIsNone(butter.bottle_for_prefix(prefix.parent))

    def test_bottles(self):
        prefix = self.butter_bottle("roblox", 'name = "Roblox"\nrunner = "rosemary-v1"\n\n[env]\n')
        self.butter_bottle("new", 'name = "Not created"\n', created=False)
        self.assertEqual(butter.bottles(), [butter.Bottle("Roblox", prefix, False)])


class ProcessTests(Sandbox):
    def test_finds_and_stops_processes_running_a_build(self):
        program = paths.versions_dir() / "version-aa-x86_64/Roblox.app/Contents/MacOS/RobloxPlayer"
        guest = processes.GUEST_HOST_MOUNT + str(paths.versions_dir() / "version-bb-x86_64/Roblox.app/x")
        sleeper = [sys.executable, "-c", "import time; time.sleep(60)"]
        client = subprocess.Popen(sleeper + [str(program)])
        child = subprocess.Popen(sleeper + [guest])
        unrelated = subprocess.Popen(sleeper + [str(self.root / "elsewhere")])
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                found = processes.running()
                if len(found) == 2:
                    break
                time.sleep(0.05)
            self.assertEqual(found, {client.pid: "version-aa-x86_64", child.pid: "version-bb-x86_64"})
            self.assertEqual(processes.terminate(sorted(found)), [])
            self.assertEqual(processes.running(), {})
            self.assertIsNone(unrelated.poll())
        finally:
            for process in (client, child, unrelated):
                process.kill()
                process.wait()


if __name__ == "__main__":
    unittest.main()
