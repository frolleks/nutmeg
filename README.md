# nutmeg

nutmeg is a Roblox bootstrapper for Linux. It downloads the macOS Roblox
client from Roblox's servers, keeps it up to date, and starts it through
[rosemary](https://github.com/microlift/rosemary), which runs macOS programs on
Linux. It is written in Python with a GTK 4 interface.

rosemary is experimental, and so is running Roblox on it. nutmeg installs and
starts the client the way rosemary's documentation describes; whether Roblox
then works depends on the rosemary build you use.

## Requirements

- Linux on x86-64 or AArch64.
- rosemary. rosemary's installation guide recommends getting it through
  [Butter](https://github.com/microlift/butter), which installs official
  rosemary builds (it calls them runners) and lists them for nutmeg to use.
  A rosemary on `PATH`, or one you built yourself with
  `cargo build --release --bin rosemary`, also works.
- A Vulkan driver, which rosemary's Metal uses. A rosemary you built yourself
  also needs SDL 3.4.0 or newer (`libSDL3.so.0`); Butter's official builds
  bring their own.
- Python 3.10 or newer.
- PyGObject and GTK 4 (nutmeg is tested with GTK 4.14). On Debian and Ubuntu:
  `sudo apt install python3-gi gir1.2-gtk-4.0`. On Fedora:
  `sudo dnf install python3-gobject gtk4`. On Arch Linux:
  `sudo pacman -S python-gobject gtk4`.
- xdg-utils, so that nutmeg can open `roblox-player:` links from the browser.

## Nix development environment

For Nix users, simply run `nix develop`.

## Installing

nutmeg runs straight from a checkout:

```sh
git clone https://github.com/frolleks/nutmeg
cd nutmeg
python3 -m nutmeg
```

To install the `nutmeg` command, use pipx with access to the system's
PyGObject:

```sh
pipx install --system-site-packages .
```

Nightly builds are on the Releases page, marked as pre-releases. The `.pyz`
file there runs as it is, with the system's Python and PyGObject:

```sh
python3 nutmeg-0.1.0.dev202609302341.pyz
```

## Setting up

1. In Butter, open Runners and install a rosemary release. Skip this if
   rosemary is on `PATH` or you built it yourself.
2. Run `nutmeg` (or `python3 -m nutmeg`) to open the settings.
3. On the rosemary page, choose the program: one of Butter's runners, the
   rosemary on `PATH`, or "Another program…" for any other file. Click Save.
4. On the Integration page, click Install. This adds "Roblox (nutmeg)" and
   "nutmeg Settings" to the application menu, and makes nutmeg the handler
   for `roblox-player:` and `roblox:` links.

After that, the Play button on roblox.com starts Roblox through nutmeg.

The first launch asks whether rosemary should install its Darwin base system
(MiniDarwin) into the prefix. rosemary downloads it from GitHub and checks it
against hashes built into rosemary. Programs run without it, because rosemary
links them against its own system libraries.

## Commands

```
nutmeg                      open the settings window
nutmeg launch [LINK]        install or update Roblox, then start it
nutmeg update               install or update Roblox without starting it
nutmeg install-desktop      add the menu entries and the link handler
nutmeg uninstall-desktop    remove the menu entries
nutmeg discord-presence     show the game on Discord until Roblox closes
                            (started by launch; see Discord below)
```

`launch` and `update` show a progress window. With `--no-gui` they report on
the terminal instead, and ask their questions there; `--yes` answers every
question with its first choice.

## What nutmeg does

Each launch goes through these steps.

1. It asks `https://clientsettingscdn.roblox.com/v2/client-version/MacPlayer`
   for the current macOS build, a name like `version-3bc33ee7ffad426f`.
2. If that build is not installed, it downloads
   `https://setup.rbxcdn.com/mac/<build>-RobloxPlayer.zip` (the x86_64 build)
   or `https://setup.rbxcdn.com/mac/arm64/<build>-RobloxPlayer.zip` (the arm64
   build), falling back to `setup-aws.rbxcdn.com` and `setup-ak.rbxcdn.com`.
   It unpacks the zip into
   `~/.local/share/nutmeg/versions/<build>-<arch>/Roblox.app`, refusing any
   path or link that would land outside that folder, and checks that the
   executable is a Mach-O file for the expected architecture.
3. It writes the FastFlags from the settings to
   `Roblox.app/Contents/MacOS/ClientSettings/ClientAppSettings.json`.
4. If Roblox is already running, it offers to close it. The macOS client runs
   one instance at a time (`LSMultipleInstancesProhibited` in its Info.plist).
   After closing it, nutmeg runs `rosemary server --stop` to stop the
   prefix's process server, as Butter's Stop button does, unless the Process
   server setting is off or the prefix is a Butter bottle's. In a bottle's
   prefix the server is left alone, because stopping it ends every program
   still running in the bottle.
5. It removes older builds that no Roblox process is using.
6. It runs

   ```sh
   ROSEMARY_PREFIX=~/.local/share/nutmeg/prefix \
     rosemary run ~/.local/share/nutmeg/versions/<build>-<arch>/Roblox.app \
     -protocolString <link>
   ```

   from the prefix's `root/` folder, which the client sees as `/`. The
   `-protocolString` argument is how rosemary's `tools/install-url-handler.sh`
   passes a link, and the client reads it. Without a link, the client is
   started with no arguments, as it is when Roblox.app is opened on a Mac.

If Roblox's version service cannot be reached, nutmeg starts the build it
installed last.

On an x86-64 computer nutmeg installs the x86_64 build, and on AArch64 the
arm64 build, so that rosemary runs the client natively. On AArch64 the
settings can choose the x86_64 build instead, which rosemary recompiles with
its Microlift emulator. rosemary cannot emulate ARM, so the arm64 build is
refused on x86-64.

nutmeg installs Roblox's public release only. When nutmeg was written, the
version service answered HTTP 401 to requests for other channels.

## Using Butter's runners and bottles

nutmeg reads Butter's files to list its runners and bottles in the settings.
It looks in `$XDG_DATA_HOME/butter/` and, for the Flatpak,
`~/.var/app/io.github.microlift.butter/data/butter/`, and it never changes
anything there.

A runner is listed when its `runner.toml` names an executable that exists.
Choosing one stores the path of that executable, such as
`~/.local/share/butter/runners/<id>/bin/rosemary`. If
Butter later removes the runner, the next launch says so and points to the
settings. Butter's official builds bundle the libraries rosemary needs, so they
run outside the Flatpak too. A runner the Flatpak compiled from source was
linked against the Flatpak's runtime and may not start outside it.

The Prefix setting lists Butter's bottles next to nutmeg's own prefix. In a
bottle's prefix, Roblox shares its files with the bottle's other programs and
runs with the bottle's settings, read from its `bottle.toml` at each launch.
nutmeg turns them into variables the way Butter does for a program it starts
in the bottle:

| `bottle.toml` key | Variable |
| ----------------- | -------- |
| `decorations` | `ROSEMARY_DECORATIONS` |
| `menu` | `ROSEMARY_MENU` |
| `swap_interval` | `ROSEMARY_SWAP_INTERVAL` |
| `prefix_home` | `ROSEMARY_PREFIX_HOME` |
| `steam = false` | `ROSEMARY_STEAM=0` |
| `menu_ctrl = false` | `ROSEMARY_MENU_CTRL=0` |
| `confine_pointer = true` | `ROSEMARY_CONFINE_POINTER=1` |
| `disable_metal = true` | `VK_DRIVER_FILES=/dev/null` |
| `process_server = false` | `ROSEMARY_SERVER=off` |
| `dead_flags`, `call_return`, `strict_imports` | `ROSEMARY_DEAD_FLAGS`, `ROSEMARY_CALL_RETURN`, `ROSEMARY_STRICT_IMPORTS` |
| `[env]` | set as they are, after the above, so they win |

The bottle's `backend` also decides `rosemary run --native` or
`--emulated`. nutmeg's own runtime, display and emulation settings, its extra
environment variables and its Execution setting are not used in a bottle's
prefix; the settings window greys them out. nutmeg's Build setting and
FastFlags still apply, because Butter has nothing for them. Butter does not
start Roblox, so Roblox does not appear in Butter's Process Manager.

## Settings

The settings are stored in `~/.config/nutmeg/config.json`. The runtime
options set rosemary's environment variables, which rosemary's
`docs/usage.md` describes:

| Setting | Variable |
| ------- | -------- |
| Prefix | `ROSEMARY_PREFIX` |
| Home folder | `ROSEMARY_PREFIX_HOME` |
| Process server | `ROSEMARY_SERVER` (`off` when switched off) |
| Display server | `SDL_VIDEO_DRIVER` (`wayland`, or `x11` for XWayland) |
| VSync | `ROSEMARY_SWAP_INTERVAL` |
| Presentation mode | `ROSEMARY_METAL_PRESENTATION_MODE` |
| Refresh rate | `ROSEMARY_SCREEN_REFRESH_RATE` |
| Title bar | `ROSEMARY_DECORATIONS` |
| Menus | `ROSEMARY_MENU` |
| Control works as Command in menu shortcuts | `ROSEMARY_MENU_CTRL` |
| Keep the pointer inside the window | `ROSEMARY_CONFINE_POINTER` |
| Dead flag elimination | `ROSEMARY_DEAD_FLAGS` |
| Native call return | `ROSEMARY_CALL_RETURN` |
| Execution | `rosemary run --native` or `--emulated` |

In a Butter bottle's prefix, the bottle's settings take the place of these;
see above. A setting left at "Default" leaves its variable unset, and nutmeg removes
these variables from the environment it inherits, so the settings are the only
place they come from. Any other variable can be added under "Extra environment
variables". A settings file from an older nutmeg that sets
`ROSEMARY_PREFIX_HOME`, `ROSEMARY_SERVER`, `ROSEMARY_METAL_PRESENTATION_MODE`,
`ROSEMARY_SCREEN_REFRESH_RATE` or `SDL_VIDEO_DRIVER` there has the value moved
to its setting when it is loaded.

rosemary has no variable for choosing between Wayland and X11. It opens its
windows with SDL 3 and leaves the choice to SDL, so the Display server setting
sets SDL's `SDL_VIDEO_DRIVER`. nutmeg also removes the older spelling,
`SDL_VIDEODRIVER`, from the environment it inherits, because SDL reads it when
`SDL_VIDEO_DRIVER` is unset. At "Default", SDL 3.4 uses Wayland when the
compositor supports the `wp_fifo_v1` protocol and X11 otherwise, which on a
Wayland desktop means XWayland. Choosing Wayland outside a Wayland session
leaves rosemary unable to open a window.

## Discord

When "Show the game you are playing on Discord" is on (the default, on the
Integration page), each launch also starts `nutmeg discord-presence`, which
runs until Roblox has been closed for 10 seconds. It reads the new lines of
the client's logs in `Library/Logs/Roblox` in the guest's home folder: under
the prefix's `root/Users/`, or in your real home folder when the home folder
setting is "Your home folder". It looks for these lines, which the macOS
client writes:

```
[FLog::Output] ! Joining game '<job id>' place <place id> at <address>
[FLog::SingleSurfaceApp] leaveUGCGameInternal
[FLog::Network] Connection lost
```

On joining, it looks the place up with Roblox's web API
(`apis.roblox.com/universes/v1/places/<place>/universe`,
`games.roblox.com/v1/games` and `thumbnails.roblox.com/v1/games/icons`) and
sets a Discord activity for application 826716557051691068: the game's name,
"by" its creator, the game's icon, the time since joining, and a "See game
page" button that opens `https://www.roblox.com/games/<place id>`. On leaving
or losing the connection, the activity is cleared. Teleporting to another
place writes a new joining line, so the activity follows.

The activity goes through the Discord app's IPC socket, `discord-ipc-<n>` in
`XDG_RUNTIME_DIR`, or in the folder the Discord Flatpak or Snap, or the Vesktop
Flatpak, uses there. If Discord is not running, nutmeg tries again every 15
seconds. Only one `discord-presence` runs at a time; a launch while one is
running leaves it to follow the new Roblox. Its own lines go to the launch log.

## FastFlags

The FastFlags page of the settings lists one flag per row: its name, its type
(Boolean, Integer, String, or JSON for any other value) and its value. Add Flag
adds a row. While you type a new flag's name, its type follows the prefix:
`FFlag`, `DFFlag` and `SFFlag` are Boolean, `FInt` and `DFInt` are Integer,
`FString` and `DFString` are String. Choosing a type yourself stops that.

Import JSON takes a JSON object of flags, pasted or loaded from a file, and
either adds it to the list (replacing the values of flags already listed) or
replaces the list. Copy as JSON puts the list on the clipboard. The search box
filters the rows by name.

Save refuses a list with an empty name, a name listed twice, or a value that
does not fit its type, and says which flag is wrong. The flags are stored in
`config.json` as a JSON object, and nutmeg writes that object to
`Roblox.app/Contents/MacOS/ClientSettings/ClientAppSettings.json` before each
launch, or removes the file when the list is empty. Roblox decides which flags
it applies.

## Files

| Path | Contents |
| ---- | -------- |
| `~/.config/nutmeg/config.json` | settings |
| `~/.local/share/nutmeg/versions/` | installed Roblox builds |
| `~/.local/share/nutmeg/prefix/` | the rosemary prefix, including Roblox's own files under `root/Users/<name>/` |
| `~/.cache/nutmeg/downloads/` | downloads in progress |
| `~/.local/state/nutmeg/logs/` | one log per launch, with nutmeg's steps and everything rosemary and Roblox print |

Each path follows the matching `XDG_*_HOME` variable when it is set. The logs
hide the authentication ticket in `roblox-player:` links; the last 20 are kept.

## Nightly builds

`.github/workflows/nightly.yml` runs every day at 23:41 UTC. If the default
branch has commits that are not in a nightly release yet, it runs the test
suite under Xvfb, builds the release files with `scripts/build_nightly.py`,
and publishes them on the Releases page as a pre-release. A day without new
commits publishes nothing.

Each nightly has a wheel, a source archive, a `.pyz` and `SHA256SUMS`. Its
version is the one in `nutmeg/__init__.py` followed by `.devYYYYMMDDHHMM`, and
its tag is `nightly-YYYYMMDD-HHMM`, both from the build time in UTC. The notes
list the commits since the previous nightly.

The workflow can also be started from the Actions tab. Its `force` option
builds even when the newest commit already has a nightly.

To build the same files locally (the `build` package is needed):

```sh
python3 scripts/build_nightly.py --version 0.1.0.dev1 --out dist
```

## Development

The tests use a local web server in place of Roblox's servers and a script in
place of rosemary:

```sh
python3 -m unittest discover -s tests
```

The window tests need a display. Without one, run them under Xvfb:

```sh
xvfb-run -a python3 -m unittest discover -s tests
```
