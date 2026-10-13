"""The settings window."""

from __future__ import annotations

import shutil
import threading
from pathlib import Path
from typing import Callable, Optional

from gi.repository import GLib, Gtk

from .. import __version__, butter, desktop, paths, processes, roblox, rosemary
from .. import config as config_module
from ..config import Config, ConfigError
from ..errors import Cancelled, NutmegError
from ..launcher import install_lock
from . import dialogs
from .flag_editor import FlagEditor
from .launch_window import LaunchWindow


def parse_env(text: str) -> dict[str, str]:
    """`NAME=value` lines into a mapping. Blank lines and lines starting with
    `#` are skipped; the value is everything after the first `=`."""
    result: dict[str, str] = {}
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        name, sep, value = line.partition("=")
        name = name.strip()
        if not sep:
            raise ConfigError(f"Extra environment variables, line {number}: expected NAME=value")
        if name in result:
            raise ConfigError(f"Extra environment variables, line {number}: {name} is set twice")
        result[name] = value
    return result


def format_env(env: dict[str, str]) -> str:
    return "".join(f"{name}={value}\n" for name, value in env.items())


class Choice:
    """A drop-down over (value, label) pairs."""

    def __init__(self, options: list[tuple[str, str]]):
        self.values = [value for value, _ in options]
        self.widget = Gtk.DropDown.new_from_strings([label for _, label in options])
        self.widget.set_valign(Gtk.Align.CENTER)

    def get(self) -> str:
        return self.values[self.widget.get_selected()]

    def set(self, value: str) -> None:
        self.widget.set_selected(self.values.index(value) if value in self.values else 0)


class PathChoice:
    """A drop-down over (path, label) pairs, the first of which is the empty
    default, followed by an "other" item that shows an entry and a Browse
    button for any other path."""

    _OTHER = object()

    def __init__(self, options: list[tuple[str, str]], other_label: str, other: Gtk.Widget, entry: Gtk.Entry):
        self.values: list = [value for value, _ in options] + [self._OTHER]
        self.dropdown = Gtk.DropDown.new_from_strings([label for _, label in options] + [other_label])
        self.dropdown.set_halign(Gtk.Align.START)
        self.entry = entry
        self._other = other
        self.widget = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.widget.append(self.dropdown)
        self.widget.append(other)
        self.dropdown.connect("notify::selected", lambda *_a: self._sync())
        self._sync()

    def _other_selected(self) -> bool:
        return self.values[self.dropdown.get_selected()] is self._OTHER

    def _sync(self) -> None:
        self._other.set_visible(self._other_selected())

    def get(self) -> str:
        if self._other_selected():
            return self.entry.get_text().strip()
        return self.values[self.dropdown.get_selected()]

    def set(self, value: str) -> None:
        if value in self.values:
            self.dropdown.set_selected(self.values.index(value))
            self.entry.set_text("")
        else:
            self.dropdown.set_selected(len(self.values) - 1)
            self.entry.set_text(value)

    def connect_changed(self, callback: Callable[[], None]) -> None:
        self.dropdown.connect("notify::selected", lambda *_a: callback())
        self.entry.connect("changed", lambda _e: callback())


def _margins(widget: Gtk.Widget, size: int) -> None:
    for side in ("top", "bottom", "start", "end"):
        getattr(widget, f"set_margin_{side}")(size)


def _label(text: str, *, dim: bool = False, wrap: bool = True) -> Gtk.Label:
    label = Gtk.Label(label=text, xalign=0, wrap=wrap)
    if dim:
        label.add_css_class("dim-label")
    return label


def _text_rows(title: str, subtitle: Optional[str]) -> Gtk.Box:
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
    box.append(_label(title))
    if subtitle:
        box.append(_label(subtitle, dim=True))
    return box


def _row(title: str, widget: Gtk.Widget, subtitle: Optional[str] = None) -> Gtk.Widget:
    """A setting with its control on the right."""
    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
    _margins(box, 8)
    box.append(_text_rows(title, subtitle))
    widget.set_valign(Gtk.Align.CENTER)
    box.append(widget)
    return box


def _wide_row(title: str, widget: Gtk.Widget, subtitle: Optional[str] = None) -> Gtk.Widget:
    """A setting with its control underneath, for paths and text."""
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    _margins(box, 8)
    box.append(_text_rows(title, subtitle))
    box.append(widget)
    return box


def _group(title: str, rows: list[Gtk.Widget], description: Optional[str] = None) -> Gtk.Widget:
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    heading = Gtk.Label(xalign=0)
    heading.set_markup(f"<b>{GLib.markup_escape_text(title)}</b>")
    box.append(heading)
    if description:
        box.append(_label(description, dim=True))
    listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
    listbox.add_css_class("rich-list")
    for child in rows:
        listbox.append(Gtk.ListBoxRow(activatable=False, selectable=False, child=child))
    box.append(Gtk.Frame(child=listbox))
    return box


def _buttons(*buttons: Gtk.Button) -> Gtk.Box:
    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, halign=Gtk.Align.START)
    for button in buttons:
        box.append(button)
    return box


def _page(*children: Gtk.Widget) -> Gtk.Widget:
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=22)
    _margins(box, 18)
    for child in children:
        box.append(child)
    return Gtk.ScrolledWindow(child=box, hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)


def _text_view(height: int) -> tuple[Gtk.TextView, Gtk.Widget]:
    view = Gtk.TextView(monospace=True, accepts_tab=False)
    view.set_wrap_mode(Gtk.WrapMode.CHAR)
    for side in ("top", "bottom", "left", "right"):
        getattr(view, f"set_{side}_margin")(6)
    scroller = Gtk.ScrolledWindow(child=view)
    scroller.set_min_content_height(height)
    return view, Gtk.Frame(child=scroller)


def _text_of(view: Gtk.TextView) -> str:
    buffer = view.get_buffer()
    return buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)


class SettingsWindow(Gtk.ApplicationWindow):
    def __init__(self, application: Gtk.Application):
        super().__init__(application=application, title="nutmeg Settings")
        self.set_default_size(700, 640)
        self._closing = False
        self._busy = False
        self._chooser = None
        self._own_groups: list[Gtk.Widget] = []
        problem = None
        try:
            self._saved = config_module.load()
        except ConfigError as e:
            self._saved = Config()
            problem = f"The saved settings could not be read, so the defaults are shown. Saving replaces them.\n{e}"
        # The file on disk is unusable, so it is written even if nothing changes.
        self._must_save = problem is not None

        header = Gtk.HeaderBar()
        self._stack = Gtk.Stack(vexpand=True)
        header.set_title_widget(Gtk.StackSwitcher(stack=self._stack))
        save = Gtk.Button(label="Save")
        save.add_css_class("suggested-action")
        save.connect("clicked", lambda _b: self._save())
        header.pack_end(save)
        self.set_titlebar(header)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        if problem:
            banner = _label(problem)
            banner.add_css_class("error")
            _margins(banner, 12)
            outer.append(banner)
        outer.append(self._stack)
        self._message = _label("")
        _margins(self._message, 10)
        self._message.set_visible(False)
        outer.append(self._message)
        self.set_child(outer)

        self._stack.add_titled(self._roblox_page(), "roblox", "Roblox")
        self._stack.add_titled(self._rosemary_page(), "rosemary", "rosemary")
        self._stack.add_titled(self._flags_page(), "flags", "FastFlags")
        self._stack.add_titled(self._integration_page(), "integration", "Integration")
        self._fill(self._saved)
        self._refresh()
        self._refresh_integration()
        self.connect("close-request", self._close_requested)

    # Pages.

    def _roblox_page(self) -> Gtk.Widget:
        host = rosemary.host_arch()
        self._installed = _label("", wrap=True)
        self._installed.set_selectable(True)
        self._launch_button = Gtk.Button(label="Launch Roblox")
        self._launch_button.add_css_class("suggested-action")
        self._launch_button.connect("clicked", lambda _b: self._launch(start=True))
        self._update_button = Gtk.Button(label="Update Roblox")
        self._update_button.connect("clicked", lambda _b: self._launch(start=False))
        self._reinstall_button = Gtk.Button(label="Remove Installed Builds")
        self._reinstall_button.connect("clicked", lambda _b: self._remove_builds())
        self._architecture = Choice(
            [
                ("auto", f"Automatic ({host})" if host else "Automatic"),
                ("x86_64", "x86_64 (Intel)"),
                ("arm64", "arm64 (Apple silicon)"),
            ]
        )
        self._engine = Choice(
            [("auto", "Automatic"), ("native", "Native"), ("emulated", "Emulated")]
        )
        self._engine_note = _label("", dim=True)
        self._engine_note.set_visible(False)
        return _page(
            _group("Installed", [_row("Version", self._installed)]),
            _buttons(self._launch_button, self._update_button, self._reinstall_button),
            _group(
                "Build",
                [
                    _row(
                        "Build",
                        self._architecture.widget,
                        f"This computer is {host or 'not a supported architecture'}. "
                        "rosemary can emulate the x86_64 build on AArch64, but cannot emulate arm64.",
                    ),
                    _row(
                        "Execution",
                        self._engine.widget,
                        "Automatic runs the build natively when it matches this computer, "
                        "and recompiles x86_64 otherwise (rosemary run --native / --emulated).",
                    ),
                ],
            ),
            self._engine_note,
        )

    def _path_entry(self, placeholder: str, folder: bool, title: str) -> tuple[Gtk.Entry, Gtk.Widget]:
        entry = Gtk.Entry(hexpand=True, placeholder_text=placeholder)
        browse = Gtk.Button(label="Browse…")
        browse.connect("clicked", lambda _b: self._browse(entry, folder, title))
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        box.append(entry)
        box.append(browse)
        return entry, box

    def _rosemary_page(self) -> Gtk.Widget:
        found = shutil.which("rosemary")
        entry, box = self._path_entry("/path/to/rosemary", False, "Choose the rosemary program")
        self._rosemary_choice = PathChoice(
            [("", f"rosemary on PATH ({found or 'not found'})")]
            + [(str(runner.executable), runner.label()) for runner in butter.runners()],
            "Another program…",
            box,
            entry,
        )
        entry, box = self._path_entry("/path/to/prefix", True, "Choose the prefix folder")
        self._prefix_choice = PathChoice(
            [("", f"nutmeg's own ({paths.default_prefix()})")]
            + [(str(bottle.prefix), bottle.label()) for bottle in butter.bottles()],
            "Another folder…",
            box,
            entry,
        )
        self._prefix_choice.connect_changed(self._refresh_prefix)
        self._prefix_status = _label("")
        self._base_button = Gtk.Button(label="Install Darwin Base")
        self._base_button.connect("clicked", lambda _b: self._install_base())
        self._repair_button = Gtk.Button(label="Repair Prefix")
        self._repair_button.connect("clicked", lambda _b: self._repair_prefix())
        prefix_actions = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        _margins(prefix_actions, 8)
        prefix_actions.append(self._prefix_status)
        self._bottle_note = _label("")
        self._bottle_note.set_visible(False)
        prefix_actions.append(self._bottle_note)
        prefix_actions.append(_buttons(self._base_button, self._repair_button))

        self._swap = Choice(
            [("", "Default"), ("0", "Off (0)"), ("1", "On (1)"), ("-1", "Adaptive (-1)")]
        )
        self._decorations = Choice(
            [("", "Default"), ("client", "Drawn by rosemary"), ("server", "Drawn by the window manager")]
        )
        self._menu = Choice(
            [("", "Default"), ("titlebar", "In the title bar"), ("strip", "In a row below it"), ("none", "Hidden")]
        )
        self._menu_ctrl = Gtk.Switch()
        self._confine = Gtk.Switch()
        self._prefix_home = Choice(
            [("", "Default"), ("prefix", "Inside the prefix"), ("host", "Your home folder")]
        )
        self._process_server = Gtk.Switch()
        self._presentation = Choice(
            [
                ("", "Default"),
                ("immediate", "Immediate"),
                ("fifo", "FIFO"),
                ("fifo_relaxed", "FIFO relaxed"),
                ("mailbox", "Mailbox"),
            ]
        )
        self._refresh_rate = Gtk.Entry(placeholder_text="The display's", width_chars=10)
        self._video_driver = Choice(
            [("", "Default"), ("wayland", "Wayland"), ("x11", "X11 (XWayland)")]
        )
        self._dead_flags = Gtk.Switch()
        self._call_return = Gtk.Switch()
        self._env_view, env_box = _text_view(90)
        return _page(
            _group(
                "rosemary",
                [
                    _wide_row(
                        "rosemary program",
                        self._rosemary_choice.widget,
                        "The runners Butter has installed are listed here. To add one, install "
                        "it on Butter's Runners page, then reopen these settings.",
                    ),
                    _wide_row(
                        "Prefix",
                        self._prefix_choice.widget,
                        "The rosemary prefix Roblox runs in (ROSEMARY_PREFIX). In a Butter "
                        "bottle's prefix, Roblox shares files with that bottle's programs and "
                        "runs with the bottle's settings from Butter.",
                    ),
                    prefix_actions,
                ],
            ),
            self._own_group(
                "Prefix options",
                [
                    _row(
                        "Home folder",
                        self._prefix_home.widget,
                        "ROSEMARY_PREFIX_HOME. With your home folder, the guest's "
                        "/Users/<name> is your real home folder, so Roblox's files go there "
                        "instead of into the prefix.",
                    ),
                    _row(
                        "Process server",
                        self._process_server,
                        "ROSEMARY_SERVER. Lets rosemary's programs see each other. When nutmeg "
                        "closes a running Roblox, it also stops the server (rosemary server --stop).",
                    ),
                ],
            ),
            self._own_group(
                "Display",
                [
                    _row(
                        "Display server",
                        self._video_driver.widget,
                        "SDL_VIDEO_DRIVER, read by SDL, which rosemary opens its windows with. "
                        "By default SDL uses Wayland when the compositor supports the fifo-v1 "
                        "protocol, and X11 otherwise, which is XWayland on a Wayland desktop. "
                        "Wayland needs a Wayland session.",
                    ),
                    _row("VSync", self._swap.widget, "ROSEMARY_SWAP_INTERVAL"),
                    _row(
                        "Presentation mode",
                        self._presentation.widget,
                        "ROSEMARY_METAL_PRESENTATION_MODE, the Vulkan present mode behind Metal. "
                        "By default rosemary uses Mailbox, or FIFO when the driver has no Mailbox.",
                    ),
                    _row(
                        "Refresh rate (Hz)",
                        self._refresh_rate,
                        "ROSEMARY_SCREEN_REFRESH_RATE: the refresh rate Roblox is told the display has.",
                    ),
                ],
            ),
            self._own_group(
                "Runtime",
                [
                    _row("Title bar", self._decorations.widget, "ROSEMARY_DECORATIONS"),
                    _row(
                        "Menus",
                        self._menu.widget,
                        "ROSEMARY_MENU. With the window manager's title bar, menus go in a row below it.",
                    ),
                    _row(
                        "Control works as Command in menu shortcuts",
                        self._menu_ctrl,
                        "ROSEMARY_MENU_CTRL",
                    ),
                    _row(
                        "Keep the pointer inside the window",
                        self._confine,
                        "ROSEMARY_CONFINE_POINTER",
                    ),
                ],
                "These set rosemary's environment variables, described in its docs/usage.md.",
            ),
            self._own_group(
                "Emulation",
                [
                    _row(
                        "Dead flag elimination",
                        self._dead_flags,
                        "ROSEMARY_DEAD_FLAGS. rosemary's docs warn that it may have semantic "
                        "accuracy bugs.",
                    ),
                    _row("Native call return", self._call_return, "ROSEMARY_CALL_RETURN"),
                ],
                "Only used when rosemary recompiles the x86_64 build.",
            ),
            self._own_group(
                "Extra environment variables",
                [env_box],
                "One NAME=value per line, passed to rosemary and Roblox. Lines starting with # are ignored.",
            ),
        )

    def _own_group(self, title: str, rows: list[Gtk.Widget], description: Optional[str] = None) -> Gtk.Widget:
        """A group of nutmeg's own runtime settings, which a Butter bottle's
        settings replace when Roblox runs in its prefix."""
        group = _group(title, rows, description)
        self._own_groups.append(group)
        return group

    def _flags_page(self) -> Gtk.Widget:
        self._flags = FlagEditor()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        _margins(box, 18)
        box.append(
            _label(
                "FastFlags are written to Roblox.app/Contents/MacOS/ClientSettings/"
                "ClientAppSettings.json before each launch. Roblox decides which flags it "
                "applies. A new flag's type follows its name prefix (FFlag, FInt, FString) "
                "until you choose one.",
                dim=True,
            )
        )
        self._flags.set_vexpand(True)
        box.append(self._flags)
        return box

    def _integration_page(self) -> Gtk.Widget:
        self._entries_state = _label("")
        self._handler_labels = {mime: _label("") for mime in desktop.MIME_TYPES}
        install = Gtk.Button(label="Install")
        install.add_css_class("suggested-action")
        install.connect("clicked", lambda _b: self._install_desktop())
        remove = Gtk.Button(label="Remove")
        remove.connect("clicked", lambda _b: self._uninstall_desktop())
        self._discord = Gtk.Switch()
        logs = Gtk.Button(label="Open Logs Folder")
        logs.connect("clicked", lambda _b: self._open_logs())
        rows = [_row("Menu entries", self._entries_state)]
        for mime, label in self._handler_labels.items():
            scheme = mime.split("/", 1)[1]
            rows.append(_row(f"{scheme}: links open with", label))
        return _page(
            _group(
                "Desktop",
                rows,
                "Adds Roblox and nutmeg Settings to the application menu, and makes nutmeg "
                "open roblox-player: and roblox: links, so that Play on roblox.com starts "
                "Roblox through nutmeg.",
            ),
            _buttons(install, remove),
            _group(
                "Discord",
                [
                    _row(
                        "Show the game you are playing on Discord",
                        self._discord,
                        "nutmeg follows Roblox's log and sets your activity in the Discord app "
                        "on this computer: the game's name, creator and icon, how long you have "
                        "been playing, and a button to the game's page. It is visible wherever "
                        "Discord shows your activity.",
                    ),
                ],
            ),
            _group(
                "Logs",
                [_row("Folder", _label(str(paths.logs_dir())))],
                "Each launch writes one log with nutmeg's steps and everything rosemary and Roblox print.",
            ),
            _buttons(logs),
            _label(f"nutmeg {__version__}", dim=True),
        )

    # Moving settings between the widgets and a Config.

    def _fill(self, config: Config) -> None:
        self._rosemary_choice.set(config.rosemary_path)
        self._prefix_choice.set(config.prefix_path)
        self._architecture.set(config.architecture)
        self._engine.set(config.engine)
        self._swap.set(config.swap_interval)
        self._decorations.set(config.decorations)
        self._menu.set(config.menu)
        self._menu_ctrl.set_active(config.menu_ctrl)
        self._confine.set_active(config.confine_pointer)
        self._dead_flags.set_active(config.dead_flags)
        self._call_return.set_active(config.call_return)
        self._prefix_home.set(config.prefix_home)
        self._process_server.set_active(config.process_server)
        self._presentation.set(config.metal_presentation_mode)
        self._refresh_rate.set_text(config.screen_refresh_rate)
        self._video_driver.set(config.video_driver)
        self._discord.set_active(config.discord_presence)
        self._env_view.get_buffer().set_text(format_env(config.extra_env))
        self._flags.set_flags(config.fast_flags)

    def _gather(self) -> Config:
        config = Config(
            rosemary_path=self._rosemary_choice.get(),
            prefix_path=self._prefix_choice.get(),
            architecture=self._architecture.get(),
            engine=self._engine.get(),
            swap_interval=self._swap.get(),
            decorations=self._decorations.get(),
            menu=self._menu.get(),
            menu_ctrl=self._menu_ctrl.get_active(),
            confine_pointer=self._confine.get_active(),
            dead_flags=self._dead_flags.get_active(),
            call_return=self._call_return.get_active(),
            prefix_home=self._prefix_home.get(),
            process_server=self._process_server.get_active(),
            metal_presentation_mode=self._presentation.get(),
            screen_refresh_rate=self._refresh_rate.get_text().strip(),
            video_driver=self._video_driver.get(),
            discord_presence=self._discord.get_active(),
            extra_env=parse_env(_text_of(self._env_view)),
            fast_flags=self._flags.get_flags(),
        )
        config.validate()
        if rosemary.host_arch() is not None:
            try:
                rosemary.build_arch(config)
            except NutmegError as e:
                raise ConfigError(str(e)) from e
        return config

    def _say(self, text: str, error: bool = False) -> None:
        self._message.set_text(text)
        self._message.set_visible(bool(text))
        if error:
            self._message.add_css_class("error")
        else:
            self._message.remove_css_class("error")

    def _save(self) -> Optional[Config]:
        """Save what the window shows. Returns the settings, or None when they
        could not be saved (and says why)."""
        try:
            config = self._gather()
        except ConfigError as e:
            self._say(str(e), error=True)
            return None
        if config != self._saved or self._must_save:
            try:
                config_module.save(config)
            except (OSError, ConfigError) as e:
                self._say(f"Could not save the settings: {e}", error=True)
                return None
            self._saved = config
            self._must_save = False
            self._say("Settings saved.")
        self._refresh()
        return config

    def _close_requested(self, _window) -> bool:
        if self._closing:
            return False
        try:
            dirty = self._gather() != self._saved
        except ConfigError:
            dirty = True
        if not dirty:
            return False

        def answered(choice: Optional[str]) -> None:
            if choice == "save" and self._save() is None:
                return
            if choice in ("save", "discard"):
                self._closing = True
                self.close()

        dialogs.choose(
            self,
            "Save changes?",
            "Some settings have changed since they were last saved.",
            [("save", "Save"), ("discard", "Discard"), ("cancel", "Cancel")],
            answered,
        )
        return True

    # State shown in the window.

    def _refresh(self) -> None:
        installed = roblox.load_installed()
        if installed is not None and roblox.installed_executable(installed) is not None:
            text = f"{installed.label} ({installed.arch})"
        else:
            text = "Not installed. Roblox is downloaded the first time it is launched."
        self._installed.set_text(text)
        self._refresh_prefix()
        for button in (self._launch_button, self._update_button, self._reinstall_button):
            button.set_sensitive(not self._busy)

    def _prefix(self) -> Path:
        text = self._prefix_choice.get()
        return Path(text).expanduser() if text else paths.default_prefix()

    def _refresh_prefix(self) -> None:
        prefix = self._prefix()
        base = None
        if not prefix.is_absolute():
            text = "The prefix must be an absolute path."
        elif not rosemary.prefix_created(prefix):
            text = "Not created yet. It is set up the first time Roblox is launched."
        else:
            base = rosemary.prefix_base_arch(prefix)
            text = f"Created, with the {base} Darwin base." if base else "Created, without a Darwin base."
        self._prefix_status.set_text(text)
        found = butter.bottle_for_prefix(prefix) if prefix.is_absolute() else None
        for group in self._own_groups:
            group.set_sensitive(found is None)
        self._engine.widget.set_sensitive(found is None)
        if found is not None:
            self._bottle_note.set_text(
                f"Roblox runs with the {found.name} bottle's settings: its title bar, menus, "
                "VSync, Metal, process server and emulation options, its environment "
                "variables, and its execution backend. Change them in Butter. nutmeg's "
                "own settings below are kept for other prefixes but not used here."
            )
            self._engine_note.set_text(
                f"Execution follows the {found.name} bottle's backend in Butter, because "
                "Roblox runs in that bottle's prefix."
            )
        self._bottle_note.set_visible(found is not None)
        self._engine_note.set_visible(found is not None)
        self._base_button.set_sensitive(not self._busy and prefix.is_absolute())
        self._repair_button.set_sensitive(not self._busy and base is not None)

    def _refresh_integration(self) -> None:
        self._entries_state.set_text("Installed" if desktop.installed() else "Not installed")
        for mime, label in self._handler_labels.items():
            label.set_text(desktop.default_handler(mime) or "Nothing")

    # Actions.

    def _browse(self, entry: Gtk.Entry, folder: bool, title: str) -> None:
        def chosen(path: Optional[str]) -> None:
            if path:
                entry.set_text(path)

        if hasattr(Gtk, "FileDialog"):
            dialog = Gtk.FileDialog(title=title, modal=True)

            def finished(source, result) -> None:
                try:
                    picked = source.select_folder_finish(result) if folder else source.open_finish(result)
                except GLib.Error:
                    return
                chosen(picked.get_path() if picked is not None else None)

            if folder:
                dialog.select_folder(self, None, finished)
            else:
                dialog.open(self, None, finished)
            return
        action = Gtk.FileChooserAction.SELECT_FOLDER if folder else Gtk.FileChooserAction.OPEN
        chooser = Gtk.FileChooserNative.new(title, self, action, "_Select", "_Cancel")

        def response(native, answer) -> None:
            if answer == Gtk.ResponseType.ACCEPT and native.get_file() is not None:
                chosen(native.get_file().get_path())
            self._chooser = None

        chooser.connect("response", response)
        self._chooser = chooser  # Kept alive until it answers.
        chooser.show()

    def _launch(self, start: bool) -> None:
        if self._save() is None:
            return
        window = LaunchWindow(self.get_application(), url=None, start=start, parent=self)
        # "unrealize" rather than "destroy": see dialogs.choose.
        window.connect("unrealize", lambda _w: self._refresh())
        window.present()

    def _in_background(
        self, working: str, work: Callable[[], str], title: str, show_output: bool
    ) -> None:
        """Run `work` on a thread with the action buttons disabled, then show
        what it returned, or the error it raised."""
        self._busy = True
        self._refresh()
        self._say(working)

        def run() -> None:
            try:
                output, failed = work(), False
            except (NutmegError, OSError) as e:
                output, failed = str(e), True
            except Cancelled:
                output, failed = "Cancelled.", True
            except Exception as e:  # Reported, so that the window does not stay busy.
                output, failed = f"Something unexpected went wrong: {type(e).__name__}: {e}", True
            GLib.idle_add(finished, output, failed)

        def finished(output: str, failed: bool) -> bool:
            self._busy = False
            self._refresh()
            self._say("")
            if show_output or failed:
                dialogs.inform(self, title + (" failed" if failed else ""), output.strip() or "Done.")
            else:
                self._say(output)
            return GLib.SOURCE_REMOVE

        threading.Thread(target=run, daemon=True).start()

    @staticmethod
    def _busy_lock():
        def busy() -> None:
            raise NutmegError("Another nutmeg window is installing Roblox. Try again when it has finished.")

        return install_lock(threading.Event(), busy)

    def _remove_builds(self) -> None:
        def work() -> str:
            with self._busy_lock():
                if processes.running():
                    raise NutmegError("Close Roblox first.")
                removed = roblox.remove_versions(keep=set(), in_use=set())
                roblox.forget_installed()
                roblox.remove_leftovers()
            return f"Removed {len(removed)} installed build(s). The next launch downloads Roblox again."

        def answered(choice: Optional[str]) -> None:
            if choice == "remove":
                self._in_background("Removing…", work, "Remove installed builds", False)

        dialogs.choose(
            self,
            "Remove the installed builds?",
            "Roblox is downloaded again the next time it is launched. Your Roblox settings "
            "and data in the prefix are kept.",
            [("remove", "Remove"), ("cancel", "Cancel")],
            answered,
        )

    def _prefix_tools(self) -> Optional[tuple[Config, Path, str]]:
        config = self._save()
        if config is None:
            return None
        try:
            return config, rosemary.find_rosemary(config), rosemary.build_arch(config)
        except NutmegError as e:
            dialogs.inform(self, "rosemary is not ready", str(e))
            return None

    def _install_base(self) -> None:
        tools = self._prefix_tools()
        if tools is None:
            return
        config, program, arch = tools
        base_arch = rosemary.BASE_ARCH[arch]

        def work() -> str:
            lines: list[str] = []
            with self._busy_lock():
                rosemary.create_prefix(program, config.prefix(), base_arch, lines.append, threading.Event())
            return "\n".join(lines)

        def answered(choice: Optional[str]) -> None:
            if choice == "install":
                self._in_background("Installing the Darwin base…", work, "Install Darwin base", True)

        dialogs.choose(
            self,
            "Install the Darwin base?",
            f"rosemary installs its {base_arch} Darwin base system (MiniDarwin) into "
            f"{config.prefix()}. If it is not cached, rosemary downloads it from GitHub and "
            "checks it against hashes built into rosemary.",
            [("install", "Install"), ("cancel", "Cancel")],
            answered,
        )

    def _repair_prefix(self) -> None:
        tools = self._prefix_tools()
        if tools is None:
            return
        config, program, _arch = tools

        def work() -> str:
            lines: list[str] = []
            with self._busy_lock():
                code = rosemary.repair_prefix(program, config.prefix(), lines.append, threading.Event())
            if code != 0:
                raise NutmegError("\n".join(lines) or f"rosemary exited with status {code}")
            return "\n".join(lines)

        self._in_background("Repairing the prefix…", work, "Repair prefix", True)

    def _install_desktop(self) -> None:
        try:
            done = desktop.install()
        except (NutmegError, OSError) as e:
            dialogs.inform(self, "Could not install the menu entries", str(e))
        else:
            self._say("Installed: " + "; ".join(done))
        self._refresh_integration()

    def _uninstall_desktop(self) -> None:
        try:
            done = desktop.uninstall()
        except OSError as e:
            dialogs.inform(self, "Could not remove the menu entries", str(e))
        else:
            self._say("; ".join(done) if done else "There were no menu entries to remove.")
        self._refresh_integration()

    def _open_logs(self) -> None:
        directory = paths.logs_dir()
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            dialogs.inform(self, "Could not open the logs folder", str(e))
            return
        problem = dialogs.open_path(directory)
        if problem is not None:
            dialogs.inform(self, "Could not open the logs folder", problem)
