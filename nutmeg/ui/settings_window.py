"""The settings window."""

from __future__ import annotations

import shutil
import threading
from pathlib import Path
from typing import Callable, Optional

from gi.repository import Adw, Gdk, GLib, GObject, Gtk

from .. import __version__, butter, desktop, paths, processes, roblox, rosemary, updater
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
            raise ConfigError(
                f"Extra environment variables, line {number}: expected NAME=value"
            )
        if name in result:
            raise ConfigError(
                f"Extra environment variables, line {number}: {name} is set twice"
            )
        result[name] = value
    return result


def format_env(env: dict[str, str]) -> str:
    return "".join(f"{name}={value}\n" for name, value in env.items())


class Choice:
    """A row with a drop-down over (value, label) pairs. Unlike a combo row's,
    the drop-down shows its whole label next to a long subtitle."""

    def __init__(
        self, title: str, options: list[tuple[str, str]], subtitle: Optional[str] = None
    ):
        self.values = [value for value, _ in options]
        self.dropdown = Gtk.DropDown.new_from_strings([label for _, label in options])
        self.dropdown.add_css_class("flat")
        self.widget = _row(title, subtitle, self.dropdown)

    def get(self) -> str:
        return self.values[self.dropdown.get_selected()]

    def set(self, value: str) -> None:
        self.dropdown.set_selected(
            self.values.index(value) if value in self.values else 0
        )


class PathChoice:
    """A combo row over (path, label) pairs, the first of which is the empty
    default, followed by an "other" item that shows an entry row, with a
    Browse button, for any other path. `rows` are the two rows. The chosen
    label, which holds a path, is the combo row's subtitle, where it has
    room."""

    _OTHER = object()

    def __init__(
        self,
        title: str,
        options: list[tuple[str, str]],
        other_label: str,
        entry_title: str,
        folder: bool,
        browse_title: str,
    ):
        self.values: list = [value for value, _ in options] + [self._OTHER]
        self.combo = Adw.ComboRow(
            title=title,
            use_subtitle=True,
            use_markup=False,
            model=Gtk.StringList.new([label for _, label in options] + [other_label]),
        )
        self.entry = Adw.EntryRow(title=entry_title, use_markup=False)
        browse = Gtk.Button(
            icon_name="document-open-symbolic",
            tooltip_text="Browse…",
            valign=Gtk.Align.CENTER,
        )
        browse.add_css_class("flat")
        browse.connect(
            "clicked",
            lambda _b: dialogs.pick_file(
                self.entry, browse_title, folder, self.entry.set_text
            ),
        )
        self.entry.add_suffix(browse)
        self.rows = [self.combo, self.entry]
        self.combo.connect("notify::selected", lambda *_a: self._sync())
        self._sync()

    def _other_selected(self) -> bool:
        return self.values[self.combo.get_selected()] is self._OTHER

    def _sync(self) -> None:
        self.entry.set_visible(self._other_selected())

    def get(self) -> str:
        if self._other_selected():
            return self.entry.get_text().strip()
        return self.values[self.combo.get_selected()]

    def set(self, value: str) -> None:
        if value in self.values:
            self.combo.set_selected(self.values.index(value))
            self.entry.set_text("")
        else:
            self.combo.set_selected(len(self.values) - 1)
            self.entry.set_text(value)

    def connect_changed(self, callback: Callable[[], None]) -> None:
        self.combo.connect("notify::selected", lambda *_a: callback())
        self.entry.connect("changed", lambda _e: callback())


def _row(
    title: str, subtitle: Optional[str] = None, *suffixes: Gtk.Widget
) -> Adw.ActionRow:
    """A row with `suffixes` at its end. Its text is shown as it is, not as
    markup, as is every row's here."""
    row = Adw.ActionRow(title=title, use_markup=False)
    if subtitle:
        row.set_subtitle(subtitle)
    for widget in suffixes:
        widget.set_valign(Gtk.Align.CENTER)
        row.add_suffix(widget)
    return row


def _property(title: str, value: str = "", *suffixes: Gtk.Widget) -> Adw.ActionRow:
    """A row that shows a value, which can be selected, under its name."""
    row = _row(title, None, *suffixes)
    row.set_subtitle(value)
    row.set_subtitle_selectable(True)
    row.add_css_class("property")
    return row


def _switch(title: str, subtitle: Optional[str] = None) -> Adw.SwitchRow:
    return Adw.SwitchRow(title=title, subtitle=subtitle or "", use_markup=False)


def _value(text: str) -> Gtk.Label:
    """A short value shown at the end of a row."""
    label = Gtk.Label(label=text)
    label.add_css_class("dim-label")
    return label


def _button(
    label: str, on_clicked: Callable[[], None], style: Optional[str] = None
) -> Gtk.Button:
    button = Gtk.Button(label=label, valign=Gtk.Align.CENTER)
    if style:
        button.add_css_class(style)
    button.connect("clicked", lambda _b: on_clicked())
    return button


def _note() -> Gtk.Label:
    """An explanation under a group's rows, shown only at times."""
    return Gtk.Label(xalign=0, wrap=True, visible=False, margin_top=12)


def _missing(program: str) -> str:
    """A note for a setting's subtitle when the program it runs is not on PATH."""
    return "" if shutil.which(program) else f" {program} was not found on PATH."


def _group(
    title: str,
    rows: list[Gtk.Widget],
    description: Optional[str] = None,
    suffix: Optional[Gtk.Widget] = None,
) -> Adw.PreferencesGroup:
    """A group of rows. Other widgets in `rows` go under the rows."""
    group = Adw.PreferencesGroup(title=title)
    if description:
        group.set_description(description)
    if suffix is not None:
        group.set_header_suffix(suffix)
    for row in rows:
        group.add(row)
    return group


def _page(*groups: Adw.PreferencesGroup) -> Adw.PreferencesPage:
    page = Adw.PreferencesPage()
    for group in groups:
        page.add(group)
    return page


def _text_of(view: Gtk.TextView) -> str:
    buffer = view.get_buffer()
    return buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)


class SettingsWindow(Adw.ApplicationWindow):
    def __init__(self, application: Gtk.Application):
        super().__init__(application=application, title="nutmeg Settings")
        self.set_default_size(780, 720)
        self._closing = False
        self._busy = False
        self._working: Optional[Adw.Toast] = None
        self._own_groups: list[Gtk.Widget] = []
        problem = None
        try:
            self._saved = config_module.load()
        except ConfigError as e:
            self._saved = Config()
            problem = f"The saved settings could not be read, so the defaults are shown. Saving replaces them. {e}"
        # The file on disk is unusable, so it is written even if nothing changes.
        self._must_save = problem is not None

        self._stack = Adw.ViewStack(vexpand=True)
        self._stack.add_titled_with_icon(
            self._roblox_page(), "roblox", "Roblox", "applications-games-symbolic"
        )
        self._stack.add_titled_with_icon(
            self._rosemary_page(),
            "rosemary",
            "rosemary",
            "applications-system-symbolic",
        )
        self._stack.add_titled_with_icon(
            self._flags_page(), "flags", "FastFlags", "emoji-flags-symbolic"
        )
        self._stack.add_titled_with_icon(
            self._integration_page(),
            "integration",
            "Integration",
            "preferences-other-symbolic",
        )

        save = _button("Save", self._save, "suggested-action")
        switcher = Adw.ViewSwitcher(
            stack=self._stack, policy=Adw.ViewSwitcherPolicy.WIDE
        )
        header = Adw.HeaderBar(title_widget=switcher)
        header.pack_end(save)
        # Errors stay shown until they are dismissed or the settings are
        # saved; other messages come and go as toasts.
        self._message = Adw.Banner(use_markup=False, button_label="Dismiss")
        self._message.connect(
            "button-clicked", lambda _b: self._message.set_revealed(False)
        )
        self._toasts = Adw.ToastOverlay(child=self._stack)
        bottom = Adw.ViewSwitcherBar(stack=self._stack)
        view = Adw.ToolbarView(content=self._toasts)
        view.add_top_bar(header)
        view.add_top_bar(self._message)
        view.add_bottom_bar(bottom)
        self.set_content(view)
        # In a narrow window the pages are switched at the bottom instead.
        narrow = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 640sp"))
        narrow.add_setter(header, "title-widget", GObject.Value(Gtk.Widget, None))
        narrow.add_setter(bottom, "reveal", True)
        self.add_breakpoint(narrow)

        self._fill(self._saved)
        if problem:
            self._say(problem, error=True)
        self._refresh()
        self._refresh_integration()
        self.connect("close-request", self._close_requested)

    # Pages.

    def _roblox_page(self) -> Gtk.Widget:
        host = rosemary.host_arch()
        self._launch_button = _button(
            "Launch Roblox", lambda: self._launch(start=True), "suggested-action"
        )
        self._update_button = _button("Update", lambda: self._launch(start=False))
        self._reinstall_button = _button(
            "Remove", self._remove_builds, "destructive-action"
        )
        self._installed = _property("Installed version", "", self._launch_button)
        self._architecture = Choice(
            "Build",
            [
                ("auto", f"Automatic ({host})" if host else "Automatic"),
                ("x86_64", "x86_64 (Intel)"),
                ("arm64", "arm64 (Apple silicon)"),
            ],
            f"This computer is {host or 'not a supported architecture'}. "
            "rosemary can emulate the x86_64 build on AArch64, but cannot emulate arm64.",
        )
        self._engine = Choice(
            "Execution",
            [("auto", "Automatic"), ("native", "Native"), ("emulated", "Emulated")],
            "Automatic runs the build natively when it matches this computer, "
            "and recompiles x86_64 otherwise (rosemary run --native / --emulated).",
        )
        self._engine_note = _note()
        self._close_on_leave = _switch(
            "Close Roblox when you leave an experience",
            "When you leave an experience, Roblox closes instead of going back to its home screen.",
        )
        return _page(
            _group(
                "Roblox",
                [
                    self._installed,
                    _row(
                        "Update Roblox",
                        "Download the newest version without starting it.",
                        self._update_button,
                    ),
                    _row(
                        "Remove installed builds",
                        "The next launch downloads Roblox again. Your Roblox settings and data "
                        "in the prefix are kept.",
                        self._reinstall_button,
                    ),
                ],
            ),
            _group(
                "Build",
                [self._architecture.widget, self._engine.widget, self._engine_note],
            ),
            _group("Experiences", [self._close_on_leave]),
        )

    def _rosemary_page(self) -> Gtk.Widget:
        found = shutil.which("rosemary")
        self._rosemary_choice = PathChoice(
            "rosemary program",
            [("", f"rosemary on PATH ({found or 'not found'})")]
            + [(str(runner.executable), runner.label()) for runner in butter.runners()],
            "Another program…",
            "Path to rosemary",
            False,
            "Choose the rosemary program",
        )
        self._prefix_choice = PathChoice(
            "Prefix",
            [("", f"nutmeg's own ({paths.default_prefix()})")]
            + [(str(bottle.prefix), bottle.label()) for bottle in butter.bottles()],
            "Another folder…",
            "Path to the prefix folder",
            True,
            "Choose the prefix folder",
        )
        self._prefix_choice.connect_changed(self._refresh_prefix)
        self._base_button = _button("Install Darwin Base", self._install_base)
        self._repair_button = _button("Repair", self._repair_prefix)
        self._prefix_status = _row(
            "Prefix state", None, self._base_button, self._repair_button
        )
        self._bottle_note = _note()

        self._swap = Choice(
            "VSync",
            [
                ("", "Default"),
                ("0", "Off (0)"),
                ("1", "On (1)"),
                ("-1", "Adaptive (-1)"),
            ],
            "ROSEMARY_SWAP_INTERVAL",
        )
        self._decorations = Choice(
            "Title bar",
            [
                ("", "Default"),
                ("client", "Drawn by rosemary"),
                ("server", "Drawn by the window manager"),
            ],
            "ROSEMARY_DECORATIONS",
        )
        self._menu = Choice(
            "Menus",
            [
                ("", "Default"),
                ("titlebar", "In the title bar"),
                ("strip", "In a row below it"),
                ("none", "Hidden"),
            ],
            "ROSEMARY_MENU. With the window manager's title bar, menus go in a row below it.",
        )
        self._menu_ctrl = _switch(
            "Control works as Command in menu shortcuts", "ROSEMARY_CONTROL_EDIT_KEYS"
        )
        self._confine = _switch(
            "Keep the pointer inside the window", "ROSEMARY_CONFINE_POINTER"
        )
        self._gamemode = _switch(
            "Run with GameMode",
            "Starts Roblox with gamemoderun, which asks Feral's GameMode to tune the "
            "system while Roblox runs." + _missing("gamemoderun"),
        )
        self._mangohud = _switch(
            "Show the MangoHud overlay",
            "Starts Roblox with mangohud, which draws the frame rate and other "
            "statistics over the game." + _missing("mangohud"),
        )
        self._wrapper = Adw.EntryRow(title="Wrapper command", use_markup=False)
        self._prefix_home = Choice(
            "Home folder",
            [
                ("", "Default"),
                ("prefix", "Inside the prefix"),
                ("host", "Your home folder"),
            ],
            "ROSEMARY_PREFIX_HOME. With your home folder, the guest's /Users/<name> is "
            "your real home folder, so Roblox's files go there instead of into the prefix.",
        )
        self._process_server = _switch(
            "Process server",
            "ROSEMARY_SERVER. Lets rosemary's programs see each other. When nutmeg "
            "closes a running Roblox, it also stops the server (rosemary server --stop).",
        )
        self._presentation = Choice(
            "Presentation mode",
            [
                ("", "Default"),
                ("immediate", "Immediate"),
                ("fifo", "FIFO"),
                ("fifo_relaxed", "FIFO relaxed"),
                ("mailbox", "Mailbox"),
            ],
            "ROSEMARY_METAL_PRESENTATION_MODE, the Vulkan present mode behind Metal. "
            "By default rosemary uses Mailbox, or FIFO when the driver has no Mailbox.",
        )
        self._refresh_rate = Gtk.Entry(placeholder_text="The display's", width_chars=12)
        self._video_driver = Choice(
            "Display server",
            [("", "Default"), ("wayland", "Wayland"), ("x11", "X11 (XWayland)")],
            "SDL_VIDEO_DRIVER, read by SDL, which rosemary opens its windows with. "
            "By default SDL uses Wayland when the compositor supports the fifo-v1 "
            "protocol, and X11 otherwise, which is XWayland on a Wayland desktop. "
            "Wayland needs a Wayland session.",
        )
        self._force_opengl = _switch(
            "Force OpenGL",
            "Sets VK_DRIVER_FILES=/dev/null, which leaves rosemary's Metal without a "
            "Vulkan driver, so Roblox uses its OpenGL renderer instead.",
        )
        self._dead_flags = _switch(
            "Dead flag elimination",
            "ROSEMARY_DEAD_FLAGS. rosemary's docs warn that it may have semantic accuracy bugs.",
        )
        self._call_return = _switch("Native call return", "ROSEMARY_CALL_RETURN")
        self._env_view, env_box = dialogs.text_box("", 90, editable=True)
        return _page(
            _group(
                "rosemary",
                [
                    *self._rosemary_choice.rows,
                    *self._prefix_choice.rows,
                    self._prefix_status,
                    self._bottle_note,
                ],
                "The runners Butter has installed are listed here. To add one, install it "
                "on Butter's Runners page, then reopen these settings. The prefix is where "
                "Roblox runs (ROSEMARY_PREFIX). In a Butter bottle's prefix, Roblox shares "
                "files with that bottle's programs and runs with the bottle's settings "
                "from Butter.",
            ),
            self._own_group(
                "Prefix options", [self._prefix_home.widget, self._process_server]
            ),
            self._own_group(
                "Display",
                [
                    self._video_driver.widget,
                    self._force_opengl,
                    self._swap.widget,
                    self._presentation.widget,
                    _row(
                        "Refresh rate (Hz)",
                        "ROSEMARY_SCREEN_REFRESH_RATE: the refresh rate Roblox is told the display has.",
                        self._refresh_rate,
                    ),
                ],
            ),
            self._own_group(
                "Runtime",
                [
                    self._decorations.widget,
                    self._menu.widget,
                    self._menu_ctrl,
                    self._confine,
                ],
                "These set rosemary's environment variables, described in its docs/usage.md.",
            ),
            _group(
                "Performance",
                [self._gamemode, self._mangohud],
                "These also apply in a Butter bottle's prefix, which has no settings for them.",
            ),
            _group(
                "Wrapper command",
                [self._wrapper],
                "Starts Roblox through other programs, written like Steam's launch "
                "options: %command% stands for rosemary and its arguments, as in "
                "gamescope -f -- %command%. Without %command%, the command goes in "
                "front. NAME=value words at the start set environment variables. "
                "GameMode and MangoHud run inside it. It also applies in a Butter bottle's prefix.",
            ),
            self._own_group(
                "Emulation",
                [self._dead_flags, self._call_return],
                "Only used when rosemary recompiles the x86_64 build.",
            ),
            self._own_group(
                "Extra environment variables",
                [env_box],
                "One NAME=value per line, passed to rosemary and Roblox. Lines starting with # are ignored.",
            ),
        )

    def _own_group(
        self, title: str, rows: list[Gtk.Widget], description: Optional[str] = None
    ) -> Gtk.Widget:
        """A group of nutmeg's own runtime settings, which a Butter bottle's
        settings replace when Roblox runs in its prefix."""
        group = _group(title, rows, description)
        self._own_groups.append(group)
        return group

    def _flags_page(self) -> Gtk.Widget:
        self._flags = FlagEditor()
        group = _group(
            "FastFlags",
            [self._flags],
            "FastFlags are written to Roblox.app/Contents/MacOS/ClientSettings/"
            "ClientAppSettings.json before each launch. Roblox decides which flags it "
            "applies. A new flag's type follows its name prefix (FFlag, FInt, FString) "
            "until you choose one.",
        )
        # A preferences page is at most 600 pixels wide, which cuts flag names
        # short; this page is laid out like one, but wider.
        for side in ("top", "bottom", "start", "end"):
            getattr(group, f"set_margin_{side}")(
                24 if side in ("top", "bottom") else 12
            )
        clamp = Adw.Clamp(child=group, maximum_size=960, tightening_threshold=600)
        return Gtk.ScrolledWindow(
            child=clamp, hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True
        )

    def _desktop_group(self) -> Adw.PreferencesGroup:
        self._entries_state = _property("Menu entries")
        if paths.in_flatpak():
            # Flatpak exports the entries and registers the links; which
            # program the host opens them with cannot be seen from inside.
            self._handler_rows = {}
            copy = Gtk.Button(icon_name="edit-copy-symbolic", tooltip_text="Copy")
            copy.add_css_class("flat")
            copy.connect(
                "clicked", lambda _b: self._copy(desktop.FLATPAK_DEFAULT_COMMAND)
            )
            return _group(
                "Desktop",
                [
                    self._entries_state,
                    _property(
                        "If another program opens Roblox links, run this in a terminal, "
                        "outside the Flatpak, to make nutmeg open them",
                        desktop.FLATPAK_DEFAULT_COMMAND,
                        copy,
                    ),
                ],
                "The Flatpak adds Roblox and nutmeg Settings to the application menu, and "
                "opens roblox-player: and roblox: links with nutmeg, so that Play on "
                "roblox.com starts Roblox through nutmeg. The first launch gives the Roblox "
                "entry Roblox's icon.",
            )
        self._handler_rows = {
            mime: _property(f"{mime.split('/', 1)[1]}: links open with")
            for mime in desktop.MIME_TYPES
        }
        buttons = Gtk.Box(spacing=8, valign=Gtk.Align.END)
        buttons.append(_button("Remove", self._uninstall_desktop))
        buttons.append(_button("Install", self._install_desktop, "suggested-action"))
        return _group(
            "Desktop",
            [self._entries_state, *self._handler_rows.values()],
            "Adds Roblox and nutmeg Settings to the application menu, and makes nutmeg "
            "open roblox-player: and roblox: links, so that Play on roblox.com starts "
            "Roblox through nutmeg.",
            buttons,
        )

    def _integration_page(self) -> Gtk.Widget:
        self._discord = _switch(
            "Show the game you are playing on Discord",
            "nutmeg follows Roblox's log and sets your activity in the Discord app "
            "on this computer: the game's name, creator and icon, how long you have "
            "been playing, and a button to the game's page. It is visible wherever "
            "Discord shows your activity.",
        )
        self._auto_update = _switch(
            "Update nutmeg automatically",
            "When Roblox is started from the browser, the menu or the command line, "
            "nutmeg first installs its newest release from GitHub, if there is a newer "
            "one, and starts again on it. " + updater.installation().description,
        )
        # Only a newer .flatpak updates the Flatpak.
        self._auto_update.set_sensitive(not paths.in_flatpak())
        return _page(
            self._desktop_group(),
            _group("Discord", [self._discord]),
            _group(
                "Logs",
                [
                    _property(
                        "Folder",
                        str(paths.logs_dir()),
                        _button("Open", self._open_logs),
                    )
                ],
                "Each launch writes one log with nutmeg's steps and everything rosemary and Roblox print.",
            ),
            _group(
                "nutmeg",
                [_row("Version", None, _value(__version__)), self._auto_update],
            ),
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
        self._gamemode.set_active(config.use_gamemode)
        self._mangohud.set_active(config.use_mangohud)
        self._wrapper.set_text(config.wrapper_command)
        self._dead_flags.set_active(config.dead_flags)
        self._call_return.set_active(config.call_return)
        self._prefix_home.set(config.prefix_home)
        self._process_server.set_active(config.process_server)
        self._presentation.set(config.metal_presentation_mode)
        self._refresh_rate.set_text(config.screen_refresh_rate)
        self._video_driver.set(config.video_driver)
        self._force_opengl.set_active(config.force_opengl)
        self._discord.set_active(config.discord_presence)
        self._close_on_leave.set_active(config.close_on_leave)
        self._auto_update.set_active(config.auto_update)
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
            use_gamemode=self._gamemode.get_active(),
            use_mangohud=self._mangohud.get_active(),
            wrapper_command=self._wrapper.get_text().strip(),
            dead_flags=self._dead_flags.get_active(),
            call_return=self._call_return.get_active(),
            prefix_home=self._prefix_home.get(),
            process_server=self._process_server.get_active(),
            metal_presentation_mode=self._presentation.get(),
            screen_refresh_rate=self._refresh_rate.get_text().strip(),
            video_driver=self._video_driver.get(),
            force_opengl=self._force_opengl.get_active(),
            discord_presence=self._discord.get_active(),
            close_on_leave=self._close_on_leave.get_active(),
            auto_update=self._auto_update.get_active(),
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
        """Show an error in the banner, which stays until it is dismissed or
        the settings are saved, or other text as a toast."""
        if error:
            self._message.set_title(text)
            self._message.set_revealed(True)
        elif text:
            dialogs.toast(self._toasts, text)

    def _copy(self, text: str) -> None:
        self.get_clipboard().set_content(
            Gdk.ContentProvider.new_for_value(GObject.Value(GObject.TYPE_STRING, text))
        )
        self._say("Copied to the clipboard.")

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
        self._message.set_revealed(False)
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
            destructive=("discard",),
        )
        return True

    # State shown in the window.

    def _refresh(self) -> None:
        installed = roblox.load_installed()
        if installed is not None and roblox.installed_executable(installed) is not None:
            text = f"{installed.label} ({installed.arch})"
        else:
            text = "Not installed. Roblox is downloaded the first time it is launched."
        self._installed.set_subtitle(text)
        self._refresh_prefix()
        for button in (
            self._launch_button,
            self._update_button,
            self._reinstall_button,
        ):
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
            text = (
                f"Created, with the {base} Darwin base."
                if base
                else "Created, without a Darwin base."
            )
        self._prefix_status.set_subtitle(text)
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
        if paths.in_flatpak():
            self._entries_state.set_subtitle("Added by the Flatpak")
        else:
            self._entries_state.set_subtitle(
                "Installed" if desktop.installed() else "Not installed"
            )
        for mime, row in self._handler_rows.items():
            row.set_subtitle(desktop.default_handler(mime) or "Nothing")

    # Actions.

    def _launch(self, start: bool) -> None:
        if self._save() is None:
            return
        window = LaunchWindow(
            self.get_application(), url=None, start=start, parent=self
        )
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
        self._working = dialogs.toast(self._toasts, working, timeout=0)

        def run() -> None:
            try:
                output, failed = work(), False
            except (NutmegError, OSError) as e:
                output, failed = str(e), True
            except Cancelled:
                output, failed = "Cancelled.", True
            except Exception as e:  # Reported, so that the window does not stay busy.
                output, failed = (
                    f"Something unexpected went wrong: {type(e).__name__}: {e}",
                    True,
                )
            GLib.idle_add(finished, output, failed)

        def finished(output: str, failed: bool) -> bool:
            self._busy = False
            self._refresh()
            if self._working is not None:
                self._working.dismiss()
                self._working = None
            if show_output or failed:
                dialogs.inform(
                    self,
                    title + (" failed" if failed else ""),
                    output.strip() or "Done.",
                )
            else:
                self._say(output)
            return GLib.SOURCE_REMOVE

        threading.Thread(target=run, daemon=True).start()

    @staticmethod
    def _busy_lock():
        def busy() -> None:
            raise NutmegError(
                "Another nutmeg window is installing Roblox. Try again when it has finished."
            )

        return install_lock(threading.Event(), busy)

    def _remove_builds(self) -> None:
        def work() -> str:
            with self._busy_lock():
                if processes.running() or processes.running_elsewhere():
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
            destructive=("remove",),
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
                rosemary.create_prefix(
                    program, config.prefix(), base_arch, lines.append, threading.Event()
                )
            return "\n".join(lines)

        def answered(choice: Optional[str]) -> None:
            if choice == "install":
                self._in_background(
                    "Installing the Darwin base…", work, "Install Darwin base", True
                )

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
                code = rosemary.repair_prefix(
                    program, config.prefix(), lines.append, threading.Event()
                )
            if code != 0:
                raise NutmegError(
                    "\n".join(lines) or f"rosemary exited with status {code}"
                )
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
            self._say(
                "; ".join(done) if done else "There were no menu entries to remove."
            )
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
