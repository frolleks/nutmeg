"""The user's settings, stored as JSON in the config directory."""

from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from . import fsutil, paths

# Which Roblox build to install. `auto` picks the one this machine runs
# natively: x86_64 on x86-64 hosts, arm64 on AArch64 hosts.
ARCHITECTURES = ("auto", "x86_64", "arm64")
# `rosemary run --native` / `--emulated`, or neither.
ENGINES = ("auto", "native", "emulated")
# ROSEMARY_SWAP_INTERVAL. The empty string leaves it unset.
SWAP_INTERVALS = ("", "0", "1", "-1")
# ROSEMARY_DECORATIONS.
DECORATIONS = ("", "client", "server")
# ROSEMARY_MENU.
MENUS = ("", "titlebar", "strip", "none")
# ROSEMARY_PREFIX_HOME.
PREFIX_HOMES = ("", "prefix", "host")
# ROSEMARY_METAL_PRESENTATION_MODE.
PRESENTATION_MODES = ("", "immediate", "fifo", "fifo_relaxed", "mailbox")
# SDL_VIDEO_DRIVER: SDL's Wayland backend, or its X11 one (XWayland on a
# Wayland desktop). rosemary has no variable of its own for this; it opens
# its windows with SDL and leaves the choice to SDL.
VIDEO_DRIVERS = ("", "wayland", "x11")

_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Variables a setting in this file controls, and the setting's name. They may
# not appear in `extra_env`, so that there is one place each is decided.
MANAGED_ENV = {
    "ROSEMARY_PREFIX": "Prefix",
    "ROSEMARY_SWAP_INTERVAL": "VSync",
    "ROSEMARY_DECORATIONS": "Title bar",
    "ROSEMARY_MENU": "Menu placement",
    "ROSEMARY_MENU_CTRL": "Control as Command",
    "ROSEMARY_CONFINE_POINTER": "Confine pointer",
    "ROSEMARY_DEAD_FLAGS": "Dead flag elimination",
    "ROSEMARY_CALL_RETURN": "Native call return",
    "ROSEMARY_PREFIX_HOME": "Home folder",
    "ROSEMARY_SERVER": "Process server",
    "ROSEMARY_METAL_PRESENTATION_MODE": "Presentation mode",
    "ROSEMARY_SCREEN_REFRESH_RATE": "Refresh rate",
    "SDL_VIDEO_DRIVER": "Display server",
    # SDL reads this older spelling when SDL_VIDEO_DRIVER is unset.
    "SDL_VIDEODRIVER": "Display server",
}


def _refresh_rate_ok(value: str) -> bool:
    """Whether rosemary accepts `value` as ROSEMARY_SCREEN_REFRESH_RATE: a
    finite number of hertz above zero."""
    # Python's float() takes digit separators; Rust's f32 parser does not.
    if "_" in value:
        return False
    try:
        rate = float(value.strip())
    except ValueError:
        return False
    return math.isfinite(rate) and rate > 0


# Settings that older versions of nutmeg left to "Extra environment
# variables". When a saved file has one there and not the setting, the value
# moves to the setting, read the way rosemary reads the variable, so that the
# file still loads and Roblox runs as it did.
_ADOPTED_ENV = {
    "ROSEMARY_PREFIX_HOME": ("prefix_home", lambda v: "host" if v == "host" else "prefix"),
    "ROSEMARY_SERVER": ("process_server", lambda v: v not in ("off", "0", "false")),
    "ROSEMARY_METAL_PRESENTATION_MODE": (
        "metal_presentation_mode",
        lambda v: v if v in PRESENTATION_MODES else "",
    ),
    "ROSEMARY_SCREEN_REFRESH_RATE": (
        "screen_refresh_rate",
        lambda v: v.strip() if _refresh_rate_ok(v) else "",
    ),
    "SDL_VIDEO_DRIVER": ("video_driver", lambda v: v.strip().lower() if v.strip().lower() in VIDEO_DRIVERS else ""),
    "SDL_VIDEODRIVER": ("video_driver", lambda v: v.strip().lower() if v.strip().lower() in VIDEO_DRIVERS else ""),
}


class ConfigError(ValueError):
    pass


@dataclass
class Config:
    rosemary_path: str = ""
    prefix_path: str = ""
    architecture: str = "auto"
    engine: str = "auto"
    swap_interval: str = ""
    decorations: str = ""
    menu: str = ""
    menu_ctrl: bool = True
    confine_pointer: bool = False
    dead_flags: bool = False
    call_return: bool = False
    prefix_home: str = ""
    process_server: bool = True
    metal_presentation_mode: str = ""
    screen_refresh_rate: str = ""
    video_driver: str = ""
    discord_presence: bool = True
    extra_env: dict = field(default_factory=dict)
    fast_flags: dict = field(default_factory=dict)

    def prefix(self) -> Path:
        """The rosemary prefix Roblox runs in."""
        if self.prefix_path:
            return Path(self.prefix_path).expanduser()
        return paths.default_prefix()

    def rosemary(self) -> Path | None:
        """The rosemary binary the user chose, or None to search `PATH`."""
        if self.rosemary_path:
            return Path(self.rosemary_path).expanduser()
        return None

    def validate(self) -> None:
        problems = []
        for name in ("rosemary_path", "prefix_path"):
            value = getattr(self, name)
            if not isinstance(value, str):
                problems.append(f"{name} must be a string")
            elif "\0" in value:
                problems.append(f"{name} contains a NUL character")
            elif value and not Path(value).expanduser().is_absolute():
                problems.append(f"{name} must be an absolute path, not {value!r}")
        choices = {
            "architecture": ARCHITECTURES,
            "engine": ENGINES,
            "swap_interval": SWAP_INTERVALS,
            "decorations": DECORATIONS,
            "menu": MENUS,
            "prefix_home": PREFIX_HOMES,
            "metal_presentation_mode": PRESENTATION_MODES,
            "video_driver": VIDEO_DRIVERS,
        }
        for name, allowed in choices.items():
            if getattr(self, name) not in allowed:
                problems.append(f"{name} must be one of {', '.join(map(repr, allowed))}")
        for name in ("menu_ctrl", "confine_pointer", "dead_flags", "call_return", "process_server", "discord_presence"):
            if not isinstance(getattr(self, name), bool):
                problems.append(f"{name} must be true or false")
        rate = self.screen_refresh_rate
        if not isinstance(rate, str) or (rate and not _refresh_rate_ok(rate)):
            problems.append("screen_refresh_rate must be a number of hertz above zero")
        if not isinstance(self.extra_env, dict):
            problems.append("extra_env must be an object")
        else:
            for key, value in self.extra_env.items():
                if not _ENV_NAME.match(key):
                    problems.append(f"{key!r} is not a valid environment variable name")
                elif key in MANAGED_ENV:
                    problems.append(f"{key} is set by the {MANAGED_ENV[key]} setting instead")
                if not isinstance(value, str):
                    problems.append(f"the value of {key} must be a string")
                elif "\0" in value:
                    problems.append(f"the value of {key} contains a NUL character")
        if not isinstance(self.fast_flags, dict):
            problems.append("fast_flags must be a JSON object")
        if problems:
            raise ConfigError("; ".join(problems))

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: object) -> "Config":
        if not isinstance(data, dict):
            raise ConfigError("the settings file must hold a JSON object")
        known = {f.name for f in fields(cls)}
        data = {key: value for key, value in data.items() if key in known}
        extra = data.get("extra_env")
        if isinstance(extra, dict):
            extra = dict(extra)
            adopted = set()
            for variable, (name, convert) in _ADOPTED_ENV.items():
                value = extra.get(variable)
                if not isinstance(value, str):
                    continue
                if name not in data:
                    data[name] = convert(extra.pop(variable))
                    adopted.add(name)
                elif name in adopted:
                    # A second spelling of a variable already adopted, which
                    # SDL would not have read.
                    del extra[variable]
            data["extra_env"] = extra
        config = cls(**data)
        config.validate()
        return config


def load(path: Path | None = None) -> Config:
    """The saved settings, or the defaults when nothing has been saved.

    A file that exists but cannot be used raises `ConfigError` rather than
    falling back to the defaults, which could quietly point Roblox at a
    different prefix.
    """
    path = path or paths.config_file()
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return Config()
    except OSError as e:
        raise ConfigError(f"{path}: {e.strerror}") from e
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ConfigError(f"{path}: not valid JSON ({e})") from e
    try:
        return Config.from_dict(data)
    except ConfigError as e:
        raise ConfigError(f"{path}: {e}") from e


def save(config: Config, path: Path | None = None) -> None:
    config.validate()
    fsutil.write_json(path or paths.config_file(), config.to_dict())
