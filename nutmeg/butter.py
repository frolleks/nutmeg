"""The rosemary runners and bottles Butter has installed.

Butter (https://github.com/microlift/butter) manages rosemary the way Bottles
manages Wine. It keeps each runner in `<data>/butter/runners/<id>/`, with a
`runner.toml` naming its executable, and each bottle in
`<data>/butter/bottles/<id>/`, with a `bottle.toml` and the rosemary prefix in
`prefix/`. `<data>` is `XDG_DATA_HOME`, or `~/.var/app/<id>/data` for the
Flatpak. nutmeg reads these files to offer Butter's runners and bottles in its
settings; it never writes to them.

When Roblox runs in a bottle's prefix, it runs with that bottle's settings,
turned into rosemary's environment the way Butter's `Bottle::environment` does
it, so that Roblox is started as Butter would start a program in the bottle.
"""

from __future__ import annotations

import re
import os
from dataclasses import dataclass, field
from pathlib import Path

from . import paths

FLATPAK_ID = "io.github.microlift.butter"
WEBSITE = "https://github.com/microlift/butter"


@dataclass(frozen=True)
class Runner:
    name: str
    # "binary" (an official build), "source" (built by Butter) or "custom".
    kind: str
    tag: str | None
    directory: Path
    executable: Path
    flatpak: bool

    def label(self) -> str:
        return f"{self.name} (Butter Flatpak)" if self.flatpak else f"{self.name} (Butter)"


# How Butter stores each bottle setting that becomes a variable, and the value
# rosemary is given for each choice; a choice not listed leaves the variable
# unset. These follow the `choice!` enums in Butter's src/bottle.rs.
_CHOICES = {
    "decorations": ("ROSEMARY_DECORATIONS", {"server": "server"}),
    "menu": ("ROSEMARY_MENU", {"titlebar": "titlebar", "strip": "strip", "hidden": "none"}),
    "swap_interval": ("ROSEMARY_SWAP_INTERVAL", {"immediate": "0", "vsync": "1", "adaptive": "-1"}),
    "prefix_home": ("ROSEMARY_PREFIX_HOME", {"host": "host"}),
}
# Switches: Butter's default, the variable, and its value when the switch is
# not at the default.
_SWITCHES = {
    "steam": (True, "ROSEMARY_STEAM", "0"),
    "menu_ctrl": (True, "ROSEMARY_MENU_CTRL", "0"),
    "confine_pointer": (False, "ROSEMARY_CONFINE_POINTER", "1"),
    "disable_metal": (False, "VK_DRIVER_FILES", "/dev/null"),
    "process_server": (True, "ROSEMARY_SERVER", "off"),
    "dead_flags": (False, "ROSEMARY_DEAD_FLAGS", "1"),
    "call_return": (False, "ROSEMARY_CALL_RETURN", "1"),
    "strict_imports": (False, "ROSEMARY_STRICT_IMPORTS", "1"),
}
# Butter's `backend` setting as `rosemary run`'s execution mode.
_BACKENDS = {"native": "native", "emulated": "emulated"}


@dataclass(frozen=True)
class Bottle:
    name: str
    prefix: Path
    flatpak: bool
    # The bottle's bottle.toml, as `read_manifest` reads it.
    settings: dict = field(default_factory=dict, compare=False, repr=False)

    def label(self) -> str:
        return f"{self.name} (Butter Flatpak bottle)" if self.flatpak else f"{self.name} (Butter bottle)"

    def environment(self) -> dict[str, str]:
        """The variables Butter runs this bottle's programs with: its
        settings, then the bottle's own variables, which win.

        Butter leaves ROSEMARY_STEAM alone only when it can reach Steam's
        folders, which outside the Flatpak it always can; nutmeg runs outside
        it, so the Steam switch is all that decides it here.
        """
        env = {"ROSEMARY_PREFIX": str(self.prefix)}
        for key, (variable, values) in _CHOICES.items():
            value = values.get(self.settings.get(key))
            if value is not None:
                env[variable] = value
        for key, (default, variable, value) in _SWITCHES.items():
            setting = self.settings.get(key, default)
            if isinstance(setting, bool) and setting != default:
                env[variable] = value
        own = self.settings.get("env")
        if isinstance(own, dict):
            env.update({k: v for k, v in own.items() if isinstance(v, str)})
        return env

    def engine(self) -> str:
        """`auto`, `native` or `emulated`, from the bottle's backend."""
        return _BACKENDS.get(self.settings.get("backend"), "auto")


def data_dirs() -> list[tuple[Path, bool]]:
    """Butter's data folders, each with whether it is the Flatpak's."""
    return [
        (paths.data_home() / "butter", False),
        (Path.home() / ".var/app" / FLATPAK_ID / "data/butter", True),
    ]


_KEY = re.compile(
    r"""^(?:([A-Za-z0-9_-]+)|"((?:[^"\\]|\\.)*)")\s*=\s*"""
    r"""(?:"((?:[^"\\]|\\.)*)"|'([^']*)'|(true|false)|([+-]?[0-9][0-9_]*))\s*(?:#.*)?$"""
)
_TABLE = re.compile(r"^\[\s*([A-Za-z0-9_-]+)\s*\]\s*(?:#.*)?$")
_ESCAPE = re.compile(r"\\(?:u([0-9A-Fa-f]{4})|U([0-9A-Fa-f]{8})|(.))")
_ESCAPES = {"b": "\b", "t": "\t", "n": "\n", "f": "\f", "r": "\r", "e": "\x1b", '"': '"', "\\": "\\"}


def _unescape(match: re.Match) -> str:
    code = match.group(1) or match.group(2)
    if code:
        return chr(int(code, 16))
    return _ESCAPES.get(match.group(3), match.group(0))


def read_manifest(path: Path) -> dict:
    """The keys of a TOML file Butter wrote that nutmeg uses.

    Butter writes its manifests with Rust's `toml` crate. This reads the
    shapes that produces for the settings nutmeg needs: plain keys holding a
    string, true or false, or a whole number, at the top or in a `[table]`,
    which becomes a nested dict. Arrays of tables (a bottle's
    `[[programs]]`) and other values are skipped. nutmeg reads TOML itself
    because it supports Python 3.10, which has no tomllib.
    """
    try:
        lines = path.read_text("utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return {}
    found: dict = {}
    table: dict | None = found
    closing = None  # The delimiter of a multi-line string being skipped.
    for line in lines:
        line = line.strip()
        if closing is not None:
            if closing in line:
                closing = None
            continue
        if line.startswith("["):
            header = _TABLE.match(line)
            table = found.setdefault(header.group(1), {}) if header else None
            if table is not None and not isinstance(table, dict):
                table = None
            continue
        value_start = line.partition("=")[2].lstrip()
        for delimiter in ('"""', "'''"):
            if value_start.startswith(delimiter) and delimiter not in value_start[3:]:
                closing = delimiter
        match = _KEY.match(line)
        if match is None or table is None:
            continue
        bare, quoted, basic, literal, boolean, integer = match.groups()
        key = bare if bare is not None else _ESCAPE.sub(_unescape, quoted)
        if basic is not None:
            table[key] = _ESCAPE.sub(_unescape, basic)
        elif literal is not None:
            table[key] = literal
        elif boolean is not None:
            table[key] = boolean == "true"
        else:
            table[key] = int(integer.replace("_", ""))
    return found


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _version(tag: str | None) -> tuple[int, ...]:
    if not tag:
        return ()
    return tuple(int(part) for part in re.findall(r"\d+", tag.split("-", 1)[0]))


def _subdirectories(directory: Path) -> list[Path]:
    try:
        entries = list(directory.iterdir())
    except OSError:
        return []
    # Butter unpacks into hidden directories and renames them when done.
    return sorted(e for e in entries if e.is_dir() and not e.name.startswith("."))


def runners() -> list[Runner]:
    """Runners whose executable exists, in the order Butter lists them:
    newest release first, custom runners last."""
    found = []
    for data, flatpak in data_dirs():
        for directory in _subdirectories(data / "runners"):
            info = read_manifest(directory / "runner.toml")
            if not isinstance(info.get("executable"), str) or not info["executable"]:
                continue
            executable = directory / info["executable"]
            if not executable.is_file():
                continue
            found.append(
                Runner(
                    name=_text(info.get("name")) or directory.name,
                    kind=_text(info.get("kind")),
                    tag=_text(info.get("tag")) or None,
                    directory=directory,
                    executable=executable,
                    flatpak=flatpak,
                )
            )
    found.sort(
        key=lambda r: (
            r.kind == "custom",
            not _version(r.tag),
            tuple(-n for n in _version(r.tag)),
            r.flatpak,
            r.name,
        )
    )
    return found


def bottles() -> list[Bottle]:
    """Bottles whose rosemary prefix has been created, by name. Like
    `rosemary.prefix_created`, this goes by the prefix's state file, which
    `rosemary prefix create` writes last."""
    found = []
    for data, flatpak in data_dirs():
        for directory in _subdirectories(data / "bottles"):
            bottle = _bottle(directory, flatpak)
            if bottle is not None and (bottle.prefix / ".rosemary/state.yaml").is_file():
                found.append(bottle)
    found.sort(key=lambda b: (b.name.lower(), b.flatpak))
    return found


def _bottle(directory: Path, flatpak: bool) -> Bottle | None:
    settings = read_manifest(directory / "bottle.toml")
    name = _text(settings.get("name"))
    if not name:
        return None
    return Bottle(name=name, prefix=directory / "prefix", flatpak=flatpak, settings=settings)


def bottle_for_prefix(prefix: Path) -> Bottle | None:
    """The Butter bottle whose prefix `prefix` is, read afresh, or None when
    it is no bottle's. A bottle's prefix is `<data>/butter/bottles/<id>/prefix`
    next to its bottle.toml."""
    prefix = Path(os.path.normpath(prefix))
    if prefix.name != "prefix":
        return None
    directory = prefix.parent
    for data, flatpak in data_dirs():
        if directory.parent == data / "bottles":
            return _bottle(directory, flatpak)
    return None


def runner_directory(path: Path) -> Path | None:
    """The Butter runner directory `path` is inside, whether or not that
    runner is still installed."""
    for data, _flatpak in data_dirs():
        try:
            relative = path.relative_to(data / "runners")
        except ValueError:
            continue
        if relative.parts:
            return data / "runners" / relative.parts[0]
    return None
