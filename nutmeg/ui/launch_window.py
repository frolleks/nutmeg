"""The small window shown while Roblox is installed, updated and started."""

from __future__ import annotations

import threading
import traceback
from importlib import resources
from pathlib import Path
from typing import Callable, Optional

from gi.repository import Adw, Gdk, GLib, Gtk

from .. import config as config_module
from ..errors import Cancelled, NutmegError
from ..launcher import Launcher
from . import dialogs


class GtkFrontend:
    """The launcher's view of the window. Called from the worker thread; it
    hands everything to the main loop, and coalesces the frequent progress
    updates so that the main loop sees at most one pending update."""

    def __init__(self, window: "LaunchWindow"):
        self._window = window
        self._lock = threading.Lock()
        self._pending: dict = {}
        self._scheduled = False

    def _post(self, **changes) -> None:
        with self._lock:
            self._pending.update(changes)
            if self._scheduled:
                return
            self._scheduled = True
        GLib.idle_add(self._flush)

    def _flush(self) -> bool:
        with self._lock:
            pending, self._pending = self._pending, {}
            self._scheduled = False
        if "status" in pending:
            self._window.show_status(*pending["status"])
        if "progress" in pending:
            self._window.show_progress(pending["progress"])
        return GLib.SOURCE_REMOVE

    def status(self, text: str, detail: str = "") -> None:
        self._post(status=(text, detail))

    def progress(self, fraction: Optional[float]) -> None:
        self._post(progress=fraction)

    def ask(
        self, title: str, message: str, choices: list[tuple[str, str]]
    ) -> Optional[str]:
        answered = threading.Event()
        result: list[Optional[str]] = [None]

        def done(choice: Optional[str]) -> None:
            result[0] = choice
            answered.set()

        def show() -> bool:
            if self._window.cancel.is_set():
                done(None)
            else:
                dialogs.choose(self._window, title, message, choices, done)
            return GLib.SOURCE_REMOVE

        GLib.idle_add(show)
        while not answered.wait(0.1):
            if self._window.cancel.is_set():
                return None
        return result[0]


def _icon() -> Optional[Gdk.Paintable]:
    """nutmeg's icon, or None where no SVG loader is installed."""
    try:
        data = (resources.files("nutmeg") / "data" / "nutmeg.svg").read_bytes()
        return Gdk.Texture.new_from_bytes(GLib.Bytes.new(data))
    except (OSError, GLib.Error):
        return None


class LaunchWindow(Adw.ApplicationWindow):
    """Runs a `Launcher` on a worker thread and shows how it is going.

    With `start`, Roblox is started and the window closes once it is running;
    without, Roblox is only installed or updated. `on_settings`, if given, is
    offered when an error is one a setting could fix. `relaunch` is passed to
    the `Launcher`.
    """

    def __init__(
        self,
        application: Gtk.Application,
        url: Optional[str],
        start: bool,
        parent: Optional[Gtk.Window] = None,
        on_settings: Optional[Callable[[], None]] = None,
        relaunch: Optional[list[str]] = None,
    ):
        # Resizable, because libadwaita shows a non-resizable window's dialogs
        # as windows of their own.
        super().__init__(application=application)
        self.set_title("Roblox" if start else "Update Roblox")
        if parent is not None:
            self.set_transient_for(parent)
            self.set_modal(True)
        self.cancel = threading.Event()
        self._url = url
        self._start = start
        self._on_settings = on_settings
        self._relaunch = relaunch
        self._working = True

        self._status = Adw.StatusPage(vexpand=True)
        self._status.add_css_class("compact")
        icon = _icon()
        if icon is not None:
            self._status.set_paintable(icon)
        else:
            self._status.set_icon_name("applications-games-symbolic")
        self._bar = Gtk.ProgressBar(width_request=280, halign=Gtk.Align.CENTER)
        self._button = Gtk.Button(label="Cancel", halign=Gtk.Align.CENTER)
        self._button.add_css_class("pill")
        self._button.connect("clicked", lambda _b: self.close())
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        box.append(self._bar)
        box.append(self._button)
        self._status.set_child(box)

        header = Adw.HeaderBar(show_title=False)
        view = Adw.ToolbarView(content=self._status)
        view.add_top_bar(header)
        # The questions asked on the way are dialogs inside this window,
        # which libadwaita shows over it from 450×360 up, and squeezes into a
        # sheet at its bottom below that.
        view.set_size_request(480, 420)
        self.set_content(view)
        self.show_status("Starting…" if start else "Checking for Roblox updates…", "")
        self._pulse = GLib.timeout_add(120, self._tick)
        self._indeterminate = True
        self.connect("close-request", self._close_requested)

        self._frontend = GtkFrontend(self)
        threading.Thread(target=self._work, daemon=True).start()

    # Main-thread display.

    def show_status(self, text: str, detail: str) -> None:
        self._status.set_title(text)
        # The description is markup.
        self._status.set_description(
            GLib.markup_escape_text(detail) if detail else None
        )

    def show_progress(self, fraction: Optional[float]) -> None:
        self._indeterminate = fraction is None
        if fraction is not None:
            self._bar.set_fraction(max(0.0, min(1.0, fraction)))

    def _tick(self) -> bool:
        if self._indeterminate and self._working:
            self._bar.pulse()
        return GLib.SOURCE_CONTINUE

    def _close_requested(self, _window) -> bool:
        if not self._working:
            return False
        # The worker notices, stops what it is doing, and closes the window.
        self.cancel.set()
        self._button.set_sensitive(False)
        self.show_status("Cancelling…", "")
        return True

    def _finish(self) -> None:
        self._working = False
        if self._pulse:
            GLib.source_remove(self._pulse)
            self._pulse = 0

    # Worker thread.

    def _work(self) -> None:
        log: Optional[Path] = None
        tail = ""
        try:
            config = config_module.load()
            launcher = Launcher(config, self._frontend, self.cancel, self._relaunch)
            log = launcher.log.path
            try:
                launched = launcher.run(self._url, self._start)
                if launched is not None:
                    launcher.settle(launched)
            except (NutmegError, Cancelled):
                raise
            except Exception:
                launcher.log.write(traceback.format_exc().rstrip())
                raise
            finally:
                tail = launcher.log.tail()
                launcher.log.close()
        except Cancelled:
            GLib.idle_add(self._done_cancelled)
            return
        except config_module.ConfigError as e:
            GLib.idle_add(
                self._done_failed,
                f"The settings could not be read.\n\n{e}",
                True,
                None,
                "",
            )
            return
        except NutmegError as e:
            GLib.idle_add(self._done_failed, str(e), e.settings, log, tail)
            return
        except OSError as e:
            where = f" ({e.filename})" if e.filename else ""
            GLib.idle_add(
                self._done_failed, f"{e.strerror or e}{where}", False, log, tail
            )
            return
        except Exception as e:
            message = f"Something unexpected went wrong: {type(e).__name__}: {e}"
            GLib.idle_add(
                self._done_failed, message, False, log, tail or traceback.format_exc()
            )
            return
        GLib.idle_add(self._done_ok)

    # Back on the main thread.

    def _done_ok(self) -> bool:
        self._finish()
        if self._start:
            self.destroy()
        else:
            self._bar.set_fraction(1.0)
            self._button.set_label("Close")
            self._button.set_sensitive(True)
        return GLib.SOURCE_REMOVE

    def _done_cancelled(self) -> bool:
        self._finish()
        self.destroy()
        return GLib.SOURCE_REMOVE

    def _done_failed(
        self, message: str, settings: bool, log: Optional[Path], tail: str
    ) -> bool:
        self._finish()
        title = (
            "Roblox could not be started"
            if self._start
            else "Roblox could not be updated"
        )
        choices = [("close", "Close")]
        if log is not None:
            choices.append(("log", "Open Log"))
        if settings and self._on_settings is not None:
            choices.insert(0, ("settings", "Open Settings"))

        # The dialog is shown once this window is gone, over the window it was
        # opened from, or as a window of its own. The application is held
        # meanwhile, as the dialog may be the only window left.
        parent = self.get_transient_for()
        application = self.get_application()
        if application is not None:
            application.hold()
        self.destroy()

        def release() -> None:
            if application is not None:
                application.release()

        def answered(choice: Optional[str]) -> None:
            if choice == "log" and log is not None:
                problem = dialogs.open_path(log)
                if problem is not None:
                    dialogs.choose(
                        parent,
                        "Could not open the log",
                        problem,
                        [("close", "Close")],
                        lambda _choice: release(),
                    )
                    return
            elif choice == "settings" and self._on_settings is not None:
                self._on_settings()
            release()

        dialogs.choose(
            parent,
            title,
            message,
            choices,
            answered,
            detail=tail or None,
        )
        return GLib.SOURCE_REMOVE
