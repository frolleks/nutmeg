"""The windows, driven through GTK. Skipped without a display; run under
xvfb-run on a machine without one. With NUTMEG_SCREENSHOTS set to a folder,
each window is also saved there as a PNG."""

from __future__ import annotations

import json
import os
import time
import unittest
from pathlib import Path
from unittest import mock

from nutmeg import launcher as launcher_module
from nutmeg import config as config_module
from nutmeg import paths, roblox, rosemary
from nutmeg.config import Config

from helpers import Sandbox, Server, roblox_zip

try:
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Graphene", "1.0")
    from gi.repository import GLib, Graphene, Gtk

    HAVE_DISPLAY = Gtk.init_check()
except (ImportError, ValueError):
    HAVE_DISPLAY = False

if HAVE_DISPLAY:
    from nutmeg.ui.app import Application
    from nutmeg.ui.launch_window import LaunchWindow
    from nutmeg.ui.settings_window import SettingsWindow


_APPLICATION = None


def shared_application():
    """One registered application for every test, as nutmeg has one per
    process: on a desktop session bus, a second registration of the same
    application ID in one process is refused."""
    global _APPLICATION
    if _APPLICATION is None:
        _APPLICATION = Application("settings", None)
        _APPLICATION.register(None)
    return _APPLICATION


def pump(until, timeout: float = 20.0) -> bool:
    """Run the main loop until `until()` is true."""
    context = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        while context.pending():
            context.iteration(False)
        if until():
            return True
        time.sleep(0.02)
    return False


def toplevels() -> list:
    model = Gtk.Window.get_toplevels()
    return [model.get_item(i) for i in range(model.get_n_items())]


def dialog_titled(title: str):
    for window in toplevels():
        if window.get_title() == title and window.get_visible():
            return window
    return None


def buttons(widget) -> list:
    found = []
    child = widget.get_first_child()
    while child is not None:
        if isinstance(child, Gtk.Button) and not isinstance(child, (Gtk.ToggleButton,)):
            found.append(child)
        found.extend(buttons(child))
        child = child.get_next_sibling()
    return found


def find_all(widget, kind) -> list:
    found = []
    child = widget.get_first_child()
    while child is not None:
        if isinstance(child, kind):
            found.append(child)
        found.extend(find_all(child, kind))
        child = child.get_next_sibling()
    return found


def find(widget, kind):
    return find_all(widget, kind)[0]


def click(window, label: str) -> None:
    for button in buttons(window):
        if button.get_label() == label:
            button.emit("clicked")
            return
    raise AssertionError(f"no {label!r} button in {window.get_title()!r}")


def screenshot(window, name: str) -> None:
    folder = os.environ.get("NUTMEG_SCREENSHOTS")
    if not folder:
        return
    pump(lambda: window.get_width() > 0, 5)
    pump(lambda: False, 0.3)
    width, height = window.get_width(), window.get_height()
    paintable = Gtk.WidgetPaintable.new(window)
    snapshot = Gtk.Snapshot.new()
    paintable.snapshot(snapshot, width, height)
    node = snapshot.to_node()
    if node is None:
        return
    renderer = window.get_native().get_renderer()
    rect = Graphene.Rect().init(0, 0, width, height)
    renderer.render_texture(node, rect).save_to_png(str(Path(folder) / f"{name}.png"))


@unittest.skipUnless(HAVE_DISPLAY, "needs GTK 4 and a display")
class WindowTests(Sandbox):
    def setUp(self) -> None:
        super().setUp()
        self.app = shared_application()
        self.addCleanup(self.close_all)

    def close_all(self) -> None:
        for window in toplevels():
            window.destroy()
        pump(lambda: not toplevels(), 5)

    def test_settings_pages_save_and_ask_before_discarding(self):
        window = SettingsWindow(self.app)
        window.present()
        pump(lambda: window.get_mapped())
        stack = window._stack
        for name in ("roblox", "rosemary", "flags", "integration"):
            stack.set_visible_child_name(name)
            screenshot(window, f"settings-{name}")
        window._swap.set("0")
        click(window, "Add Flag")
        row = window._flags.rows()[-1]
        row.name.set_text("FIntTargetRefreshRate")
        self.assertEqual(row.kind_name(), "int")
        row.entry.set_text("144")
        window._env_view.get_buffer().set_text("# comment\nRUST_BACKTRACE=1\n")
        click(window, "Save")
        saved = config_module.load()
        self.assertEqual(saved.swap_interval, "0")
        self.assertEqual(saved.fast_flags, {"FIntTargetRefreshRate": 144})
        self.assertEqual(saved.extra_env, {"RUST_BACKTRACE": "1"})

        # A setting the extra variables may not hold is refused with a message.
        window._env_view.get_buffer().set_text("ROSEMARY_PREFIX=/x\n")
        click(window, "Save")
        self.assertIn("Prefix setting", window._message.get_text())
        self.assertEqual(config_module.load(), saved)

        window.close()
        self.assertTrue(pump(lambda: dialog_titled("Save changes?") is not None))
        screenshot(dialog_titled("Save changes?"), "settings-unsaved")
        click(dialog_titled("Save changes?"), "Discard")
        self.assertTrue(pump(lambda: not window.get_visible()))
        self.assertEqual(config_module.load(), saved)

    def test_butter_runners_and_bottles_are_offered(self):
        tool = self.butter_runner("rosemary-v1-official", 'name = "Rosemary 1 (Official Build)"\nexecutable = "bin/rosemary"\n')
        prefix = self.butter_bottle("roblox", 'name = "Roblox"\n')
        window = SettingsWindow(self.app)
        window.present()
        pump(lambda: window.get_mapped())
        window._stack.set_visible_child_name("rosemary")
        rosemary_choice, prefix_choice = window._rosemary_choice, window._prefix_choice
        self.assertFalse(rosemary_choice.entry.get_parent().get_visible())
        rosemary_choice.dropdown.set_selected(1)
        prefix_choice.dropdown.set_selected(1)
        window._process_server.set_active(False)
        window._presentation.set("fifo")
        window._refresh_rate.set_text("75")
        window._video_driver.set("x11")
        screenshot(window, "settings-butter")
        click(window, "Save")
        saved = config_module.load()
        self.assertEqual(saved.rosemary_path, str(tool))
        self.assertEqual(saved.prefix_path, str(prefix))
        self.assertFalse(saved.process_server)
        self.assertEqual((saved.metal_presentation_mode, saved.screen_refresh_rate), ("fifo", "75"))
        self.assertEqual(saved.video_driver, "x11")
        self.assertIn("Created", window._prefix_status.get_text())
        # The bottle's settings replace nutmeg's runtime settings, so those are greyed out.
        self.assertTrue(window._bottle_note.get_visible())
        self.assertIn("Roblox bottle's settings", window._bottle_note.get_text())
        self.assertFalse(window._engine.widget.get_sensitive())
        self.assertFalse(window._swap.widget.is_sensitive())
        screenshot(window, "settings-bottle")
        prefix_choice.dropdown.set_selected(0)
        self.assertFalse(window._bottle_note.get_visible())
        self.assertTrue(window._swap.widget.is_sensitive())
        prefix_choice.dropdown.set_selected(1)

        # Any other program goes in the entry under "Another program…".
        rosemary_choice.set("/opt/rosemary/target/release/rosemary")
        self.assertTrue(rosemary_choice.entry.get_parent().get_visible())
        window._refresh_rate.set_text("0")
        click(window, "Save")
        self.assertIn("screen_refresh_rate", window._message.get_text())
        window._refresh_rate.set_text("")
        click(window, "Save")
        self.assertEqual(config_module.load().rosemary_path, "/opt/rosemary/target/release/rosemary")

    def test_an_unreadable_settings_file_is_replaced_on_save(self):
        paths.config_file().parent.mkdir(parents=True)
        paths.config_file().write_text("{broken")
        window = SettingsWindow(self.app)
        window.present()
        screenshot(window, "settings-unreadable")
        click(window, "Save")
        self.assertEqual(config_module.load(), Config())

    def test_flag_rows_types_and_errors(self):
        window = SettingsWindow(self.app)
        window.present()
        window._stack.set_visible_child_name("flags")
        editor = window._flags

        click(window, "Add Flag")
        flag = editor.rows()[-1]
        flag.name.set_text("FFlagExample")
        self.assertEqual(flag.kind_name(), "bool")
        flag.switch.set_active(True)

        click(window, "Add Flag")
        number = editor.rows()[-1]
        number.name.set_text("FIntExample")
        number.entry.set_text("abc")
        self.assertIn("FIntExample needs a whole number", editor._state.get_text())
        screenshot(window, "settings-flags-error")
        click(window, "Save")
        self.assertIn("FIntExample needs a whole number", window._message.get_text())
        self.assertFalse(paths.config_file().exists())
        number.entry.set_text("-60")
        click(window, "Save")
        self.assertEqual(config_module.load().fast_flags, {"FFlagExample": True, "FIntExample": -60})
        self.assertEqual(editor._state.get_text(), "2 flags.")

        # A type chosen by hand is kept when the name changes, and the value
        # carries over.
        number.kind.set_selected(2)
        self.assertEqual(number.kind_name(), "string")
        number.name.set_text("FFlagRenamed")
        self.assertEqual(number.kind_name(), "string")
        self.assertEqual(editor.get_flags()["FFlagRenamed"], "-60")
        # Switching a Boolean to text shows its value as text.
        flag.kind.set_selected(3)
        self.assertEqual(flag.entry.get_text(), "true")
        self.assertEqual(editor.get_flags()["FFlagExample"], True)

        click(window, "Add Flag")
        editor.rows()[-1].name.set_text("FFlagRenamed")
        self.assertIn("FFlagRenamed is listed twice", editor._state.get_text())
        click(editor.rows()[-1].widget, "Remove")
        self.assertEqual(len(editor.rows()), 2)
        click(window, "Add Flag")
        self.assertIn("row 3 has no name", editor._state.get_text())
        click(editor.rows()[-1].widget, "Remove")
        screenshot(window, "settings-flags")

    def test_flags_of_any_json_type_survive(self):
        window = SettingsWindow(self.app)
        flags = {"A": 1.5, "B": None, "C": [1, 2], "D": {"x": "y"}, "E": "True", "F": -3, "G": False, "H": ""}
        window._flags.set_flags(flags)
        self.assertEqual(list(window._flags.get_flags().items()), list(flags.items()))
        self.assertEqual(
            [row.kind_name() for row in window._flags.rows()],
            ["json", "json", "json", "json", "string", "int", "bool", "string"],
        )

    def test_import_search_and_copy(self):
        window = SettingsWindow(self.app)
        window.present()
        window._stack.set_visible_child_name("flags")
        editor = window._flags
        editor.set_flags({"FIntOne": 1, "FFlagTwo": True})

        def import_text(text: str, button: str) -> None:
            click(window, "Import JSON…")
            self.assertTrue(pump(lambda: dialog_titled("Import FastFlags") is not None))
            dialog = dialog_titled("Import FastFlags")
            find(dialog, Gtk.TextView).get_buffer().set_text(text)
            screenshot(dialog, "flags-import")
            click(dialog, button)

        import_text('{"FIntOne": 2, "FStringThree": "x"}', "Add to List")
        self.assertTrue(pump(lambda: len(editor.rows()) == 3))
        self.assertEqual(editor.get_flags(), {"FIntOne": 2, "FFlagTwo": True, "FStringThree": "x"})

        import_text("[1, 2]", "Add to List")
        self.assertTrue(pump(lambda: dialog_titled("Import FastFlags") is not None))
        again = dialog_titled("Import FastFlags")
        self.assertEqual(find(again, Gtk.TextView).get_buffer().get_property("text"), "[1, 2]")
        self.assertTrue(any("must be a JSON object" in (label.get_text() or "") for label in find_all(again, Gtk.Label)))
        click(again, "Cancel")
        self.assertTrue(pump(lambda: dialog_titled("Import FastFlags") is None))
        self.assertEqual(len(editor.rows()), 3)

        editor._search.set_text("fint")
        self.assertTrue(pump(lambda: [r.widget.get_child_visible() for r in editor.rows()] == [True, False, False]))
        editor._search.set_text("")
        self.assertTrue(pump(lambda: all(r.widget.get_child_visible() for r in editor.rows())))

        copied = editor.copy_json()
        self.assertEqual(json.loads(copied), editor.get_flags())
        result = []
        clipboard = editor.get_clipboard()
        clipboard.read_text_async(None, lambda source, task: result.append(source.read_text_finish(task)))
        self.assertTrue(pump(lambda: result))
        self.assertEqual(result[0], copied)

        import_text('{"Only": 1}', "Replace List")
        self.assertTrue(pump(lambda: len(editor.rows()) == 1))
        self.assertEqual(editor.get_flags(), {"Only": 1})

    def test_launch_window_runs_a_launch(self):
        server = Server()
        self.addCleanup(server.close)
        guid = "version-cccccccccccccccc"
        server.publish(guid, "0.702.0.1", {"x86_64": roblox_zip()}, ["a"])
        patches = [
            mock.patch.object(roblox, "CLIENT_VERSION_URLS", (f"{server.url}/client-version",)),
            mock.patch.object(roblox, "CDN_HOSTS", (f"{server.url}/a",)),
            mock.patch.object(rosemary, "host_arch", return_value="x86_64"),
            mock.patch.object(launcher_module, "STARTUP_GRACE_SECONDS", 0.5),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        tool = self.fake_rosemary()
        config_module.save(Config(rosemary_path=str(tool)))
        url = "roblox-player:1+launchmode:play"

        window = LaunchWindow(self.app, url=url, start=True)
        window.present()
        self.assertTrue(pump(lambda: dialog_titled("Set up the rosemary prefix") is not None))
        screenshot(dialog_titled("Set up the rosemary prefix"), "launch-prefix-question")
        click(dialog_titled("Set up the rosemary prefix"), "Download the base")
        self.assertTrue(pump(lambda: not window.get_visible(), 30))
        record = self.record()
        self.assertEqual(record["argv"][-2:], ["-protocolString", url])
        self.assertEqual(roblox.load_installed().guid, guid)
        self._stop_fake_roblox()

    def test_launch_failure_offers_the_log(self):
        config_module.save(Config(rosemary_path=str(self.root / "missing")))
        window = LaunchWindow(self.app, url=None, start=True, on_settings=lambda: None)
        window.present()
        title = "Roblox could not be started"
        self.assertTrue(pump(lambda: any(w is not window and w.get_title() == title for w in toplevels())))
        dialog = next(w for w in toplevels() if w is not window and w.get_title() == title)
        screenshot(window, "launch-failed-window")
        screenshot(dialog, "launch-failed-dialog")
        # The suggested choice is rightmost.
        self.assertEqual([b.get_label() for b in buttons(dialog)], ["Open Log", "Close", "Open Settings"])
        click(dialog, "Close")
        self.assertTrue(pump(lambda: not window.get_visible()))

    def test_update_window_stays_open_when_done(self):
        server = Server()
        self.addCleanup(server.close)
        server.publish("version-dddddddddddddddd", "0.703.0.1", {"x86_64": roblox_zip()}, ["a"])
        for patch in (
            mock.patch.object(roblox, "CLIENT_VERSION_URLS", (f"{server.url}/client-version",)),
            mock.patch.object(roblox, "CDN_HOSTS", (f"{server.url}/a",)),
            mock.patch.object(rosemary, "host_arch", return_value="x86_64"),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        config_module.save(Config(rosemary_path=str(self.fake_rosemary())))
        window = LaunchWindow(self.app, url=None, start=False)
        window.present()
        self.assertTrue(pump(lambda: dialog_titled("Set up the rosemary prefix") is not None))
        click(dialog_titled("Set up the rosemary prefix"), "Continue without it")
        self.assertTrue(pump(lambda: window._button.get_label() == "Close", 30))
        self.assertIn("is installed", window._status.get_text())
        screenshot(window, "update-done")
        click(window, "Close")
        self.assertTrue(pump(lambda: not window.get_visible()))

    def _stop_fake_roblox(self) -> None:
        from nutmeg import processes

        processes.terminate(sorted(processes.running()))


if __name__ == "__main__":
    unittest.main()
