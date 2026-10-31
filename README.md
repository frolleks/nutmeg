# Nutmeg

Nutmeg is a Roblox bootstrapper for Linux. It downloads the macOS Roblox
client from Roblox's servers, keeps it up to date, and starts it through
[Rosemary](https://github.com/microlift/rosemary), which runs macOS programs on
Linux. It is written in Python with a GTK 4 and libadwaita interface.

Rosemary is experimental, and so is running Roblox on it. nutmeg installs and
starts the client the way Rosemary's documentation describes; whether Roblox
then works depends on the Rosemary build you use.

## Requirements

- Linux on x86-64.
- Rosemary. Rosemary's installation guide recommends getting it through
  [Butter](https://github.com/microlift/butter), which installs official
  Rosemary builds (it calls them runners) and lists them for nutmeg to use.
  A Rosemary on `PATH`, or one you built yourself with
  `cargo build --release --bin rosemary`, also works.
- A Vulkan driver, which Rosemary's Metal uses. If you don't have a GPU that supports Vulkan, enable `Force OpenGL` in Nutmeg settings.
- Python 3.10 or newer.
- PyGObject, GTK 4 and libadwaita 1.5 or newer (nutmeg is tested with GTK 4.14
  and libadwaita 1.5). On Debian and Ubuntu:
  `sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1`. On Fedora:
  `sudo dnf install python3-gobject gtk4 libadwaita`. On Arch Linux:
  `sudo pacman -S python-gobject gtk4 libadwaita`.
- xdg-utils, so that nutmeg can open `roblox-player:` links from the browser.

The Flatpak needs none of the Python, PyGObject and xdg-utils parts, and the
AppImage none of the Python and PyGObject ones: each brings its own (see
Installing below).

## Nix development environment

For Nix users, simply run `nix develop`.

## Installing

nutmeg is published on the Releases page in two kinds: releases, tagged
`vX.Y.Z`, and nightly builds, marked as pre-releases. Each has the same
files: a Flatpak and an AppImage for x86-64, a `.pyz`, a wheel
and the source.

### With Flatpak

Download the `.flatpak` from the Releases page and install it:

```sh
flatpak install --user nutmeg-1.2.0-x86_64.flatpak
```

Flatpak offers to install GNOME's runtime from Flathub with it. Then open
"nutmeg Settings" from the application menu, or run
`flatpak run io.github.frolleks.Nutmeg`, and carry on with Setting up below.
To update, install a newer `.flatpak` the same way; nutmeg's own updater
leaves the Flatpak alone.

The Flatpak is allowed to reach:

- the network, the display (Wayland, and X11 for Rosemary's X11 video
  driver), sound and the GPU, as Butter's Flatpak is;
- your home folder, read-only, so that it can run Rosemary from wherever it
  is: a Rosemary runs files that sit beside it, which the file chooser
  cannot hand over one at a time;
- Butter's runners and bottles, in `~/.local/share/butter/` and in Butter's
  Flatpak, with write access, because Roblox running in a bottle writes to
  the bottle's prefix;
- `~/.local/share/icons/hicolor/`, to give the Roblox menu entry Roblox's
  icon, which is copied out of the installed client and so cannot be shipped
  with the Flatpak;
- the Discord app's socket, for showing your game on Discord: Discord's own,
  its Snap's, and the Discord and Discord Canary Flatpaks'.

It differs from the other ways of installing nutmeg in a few ways:

- Its settings, Roblox, the prefix and the logs are kept in
  `~/.var/app/io.github.frolleks.Nutmeg/`, so it starts with settings of its
  own.
- For Rosemary, choose one of Butter's runners, or the `bin/rosemary` of a
  Rosemary release unpacked in your home folder. Programs on your system's
  `PATH` are outside the sandbox, and a Rosemary you built yourself usually
  needs a newer C library than GNOME's runtime has.
- The Flatpak adds the menu entries and opens `roblox-player:` and `roblox:`
  links itself, so the Integration page has no Install button. If another
  program (such as Sober) opens the links, make nutmeg their default:

  ```sh
  xdg-mime default io.github.frolleks.Nutmeg.desktop x-scheme-handler/roblox-player x-scheme-handler/roblox
  ```

- GameMode works as it is. MangoHud, gamescope and OBS's Vulkan capture are
  runtime extensions, and run once installed, for example
  `flatpak install flathub org.freedesktop.Platform.VulkanLayer.MangoHud//26.08`.
- A prefix outside nutmeg's folder, the "Your home folder" setting for the
  guest's home, or another Discord client's socket need more access, which
  `flatpak override` gives, for example
  `flatpak override --user --filesystem=home io.github.frolleks.Nutmeg`.
- Each `flatpak run` is a sandbox of its own, which cannot see the processes
  of another. While Roblox runs, the `nutmeg watch` beside it (see Discord
  below) answers the other sandboxes, so that a launch from a link can still
  find and close the Roblox an earlier launch started.

To remove it, first take Roblox's icon back out of your icon theme, which
Flatpak does not know about:

```sh
flatpak run io.github.frolleks.Nutmeg uninstall-desktop
flatpak uninstall io.github.frolleks.Nutmeg
```

### With the AppImage

The AppImage carries its own Python, PyGObject, GTK 4 and libadwaita, from Ubuntu 24.04,
so it runs on distributions with glibc 2.39 or newer: Ubuntu 24.04, Debian 13,
Fedora 40 and later ones. On older ones, use the Flatpak. Download the
`.AppImage` for your computer from the Releases page, put it somewhere it can
stay, make it executable and run it:

```sh
mkdir -p ~/Applications
mv ~/Downloads/nutmeg-1.2.0-x86_64.AppImage ~/Applications/
chmod +x ~/Applications/nutmeg-1.2.0-x86_64.AppImage
~/Applications/nutmeg-1.2.0-x86_64.AppImage
```

It opens the settings window; carry on with Setting up below. The menu
entries that the Integration page's Install button adds run the AppImage
from where it is, so after moving it, run it from its new place and install
them again. AppImage managers such as Gear Lever add a "nutmeg Settings"
entry of their own, but only nutmeg's Install button makes the AppImage
open `roblox-player:` links.

AppImages mount themselves with FUSE, so they need `fusermount3` (the `fuse3`
package) or `fusermount`, which most desktops have. The AppImage keeps its
settings and Roblox in the same places as a `.pyz` or pipx install does, and
updates itself in place (see Updating nutmeg below): the file keeps its name,
so `--version` tells which version it is. To remove it, take the menu entries
out first, then delete the file:

```sh
~/Applications/nutmeg-1.2.0-x86_64.AppImage uninstall-desktop
rm ~/Applications/nutmeg-1.2.0-x86_64.AppImage
```

### Without Flatpak or AppImage

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

The `.pyz` file on the Releases page runs as it is, with the system's
Python and PyGObject:

```sh
python3 nutmeg-1.2.0.pyz
```

The wheel there installs with pipx in the same way:
`pipx install --system-site-packages ./nutmeg-1.2.0-py3-none-any.whl`.
Both keep themselves up to date (see Updating nutmeg below).

## Updating nutmeg

When "Update nutmeg automatically" is on (the default, on the Integration
page), a launch started from the browser, the menu or the command line first
asks GitHub's API for the newest nutmeg of the kind it is. A release asks for
the latest release (`api.github.com/repos/frolleks/nutmeg/releases/latest`)
and a nightly build for the newest nightly builds
(`api.github.com/repos/frolleks/nutmeg/releases`), so a release only ever
updates to a newer release and a nightly build to a newer nightly build. If
there is a newer one, nutmeg downloads its file from the release, checks it
against the release's `SHA256SUMS`, installs it, and starts again on it with
the same link, so that the launch runs on the new version:

- A `.pyz` file or an AppImage is replaced in place, keeping its name and
  permissions, after the new file has been run once to check that it is the
  version expected. Menu entries and links that point at the file then run
  the new version. An AppImage is about 40 MB, which the launch waits for.
- A wheel installed in a virtual environment, as pipx installs it, is
  replaced by the release's wheel, installed with that environment's pip
  (`python -m pip install --no-deps --no-index <wheel>`). `pipx list` goes on
  showing the version pipx installed; `nutmeg --version` shows the one
  running.
- A source checkout is not updated; use `git pull`. Neither is the Flatpak
  (install a newer `.flatpak`), a copy installed some other way, or a `.pyz`
  or AppImage nutmeg cannot write to.

Nothing is installed while another nutmeg is running, such as the settings
window or the `nutmeg watch` that runs alongside Roblox, because a nutmeg
still running from a replaced `.pyz` could not load the rest of itself. The
update then waits for a later launch. A launch started from the settings
window does not update nutmeg either. If the check or the update fails, the
launch goes on with the running version, and the launch log says why.

## Setting up

1. In Butter, open Runners and install a Rosemary release. Skip this if
   Rosemary is on `PATH` or you built it yourself.
2. Run `nutmeg` (or `python3 -m nutmeg`) to open the settings.
3. On the Rosemary page, choose the program: one of Butter's runners, the
   Rosemary on `PATH`, or "Another program…" for any other file. Click Save.
4. On the Integration page, click Install. This adds "Roblox (nutmeg)" and
   "nutmeg Settings" to the application menu, and makes nutmeg the handler
   for `roblox-player:` and `roblox:` links. Right-clicking "Roblox (nutmeg)"
   also offers nutmeg Settings. "Roblox (nutmeg)" gets Roblox's own icon,
   taken from the installed client's `AppIcon.icns`; until the first launch
   installs the client, it shows a generic games icon. The Flatpak does all
   this when it is installed, so it has no Install button.

After that, the Play button on roblox.com starts Roblox through nutmeg.

The first launch asks whether Rosemary should install its Darwin base system
(MiniDarwin) into the prefix. Rosemary downloads it from GitHub and checks it
against hashes built into Rosemary. Programs run without it, because Rosemary
links them against its own system libraries.

## Commands

```
nutmeg                      open the settings window
nutmeg launch [LINK]        install or update Roblox, then start it
nutmeg update               install or update Roblox without starting it
nutmeg install-desktop      add the menu entries and the link handler
nutmeg uninstall-desktop    remove the menu entries
nutmeg watch                follow Roblox until it closes, for Discord and
                            for closing it when you leave a game (started
                            by launch; see Discord and Leaving a game below)
```

`launch` and `update` show a progress window. With `--no-gui` they report on
the terminal instead, and ask their questions there; `--yes` answers every
question with its first choice.

## What nutmeg does

Each launch goes through these steps.

1. It asks `https://clientsettingscdn.roblox.com/v2/client-version/MacPlayer`
   for the current macOS build, a name like `version-3bc33ee7ffad426f`.
2. If that build is not installed, it downloads
   `https://setup.rbxcdn.com/mac/<build>-RobloxPlayer.zip`, falling back to `setup-aws.rbxcdn.com` and `setup-ak.rbxcdn.com`.
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
   `-protocolString` argument is how Rosemary's `tools/install-url-handler.sh`
   passes a link, and the client reads it. Without a link, the client is
   started with no arguments, as it is when Roblox.app is opened on a Mac.

If Roblox's version service cannot be reached, nutmeg starts the build it
installed last.

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
settings. Butter's official builds bundle the libraries Rosemary needs, so they
run outside the Flatpak too. A runner the Flatpak compiled from source was
linked against the Flatpak's runtime and may not start outside it.

The Prefix setting lists Butter's bottles next to nutmeg's own prefix. In a
bottle's prefix, Roblox shares its files with the bottle's other programs and
runs with the bottle's settings, read from its `bottle.toml` at each launch.
nutmeg turns them into variables the way Butter does for a program it starts
in the bottle:

| `bottle.toml` key                             | Variable                                                                 |
| --------------------------------------------- | ------------------------------------------------------------------------ |
| `decorations`                                 | `ROSEMARY_DECORATIONS`                                                   |
| `menu`                                        | `ROSEMARY_MENU`                                                          |
| `swap_interval`                               | `ROSEMARY_SWAP_INTERVAL`                                                 |
| `prefix_home`                                 | `ROSEMARY_PREFIX_HOME`                                                   |
| `steam = false`                               | `ROSEMARY_STEAM=0`                                                       |
| `menu_ctrl = false`                           | `ROSEMARY_CONTROL_EDIT_KEYS=0`                                           |
| `confine_pointer = true`                      | `ROSEMARY_CONFINE_POINTER=1`                                             |
| `disable_metal = true`                        | `VK_DRIVER_FILES=/dev/null`                                              |
| `process_server = false`                      | `ROSEMARY_SERVER=off`                                                    |
| `dead_flags`, `call_return`, `strict_imports` | `ROSEMARY_DEAD_FLAGS`, `ROSEMARY_CALL_RETURN`, `ROSEMARY_STRICT_IMPORTS` |
| `[env]`                                       | set as they are, after the above, so they win                            |

The bottle's `backend` also decides `rosemary run --native` or
`--emulated`. nutmeg's own runtime, display and emulation settings, its extra
environment variables and its Execution setting are not used in a bottle's
prefix; the settings window greys them out. nutmeg's Build setting,
FastFlags, GameMode, MangoHud and wrapper command still apply, because Butter
has nothing for them. Butter does not
start Roblox, so Roblox does not appear in Butter's Process Manager.

## Settings

The settings are stored in `~/.config/nutmeg/config.json`. The runtime
options set Rosemary's environment variables, which Rosemary's
`docs/usage.md` describes:

| Setting                                       | Variable                                              |
| --------------------------------------------- | ----------------------------------------------------- |
| Prefix                                        | `ROSEMARY_PREFIX`                                     |
| Home folder                                   | `ROSEMARY_PREFIX_HOME`                                |
| Process server                                | `ROSEMARY_SERVER` (`off` when switched off)           |
| Display server                                | `SDL_VIDEO_DRIVER` (`wayland`, or `x11` for XWayland) |
| Force OpenGL                                  | `VK_DRIVER_FILES` (`/dev/null` when switched on)      |
| VSync                                         | `ROSEMARY_SWAP_INTERVAL`                              |
| Presentation mode                             | `ROSEMARY_METAL_PRESENTATION_MODE`                    |
| Refresh rate                                  | `ROSEMARY_SCREEN_REFRESH_RATE`                        |
| Title bar                                     | `ROSEMARY_DECORATIONS`                                |
| Menus                                         | `ROSEMARY_MENU`                                       |
| Control works as Command in editing shortcuts | `ROSEMARY_CONTROL_EDIT_KEYS` (`1` when switched on)   |
| Keep the pointer inside the window            | `ROSEMARY_CONFINE_POINTER`                            |
| Dead flag elimination                         | `ROSEMARY_DEAD_FLAGS`                                 |
| Native call return                            | `ROSEMARY_CALL_RETURN`                                |
| Execution                                     | `rosemary run --native` or `--emulated`               |

In a Butter bottle's prefix, the bottle's settings take the place of these;
see above. A setting left at "Default" leaves its variable unset, except Force
OpenGL, which leaves `VK_DRIVER_FILES` as it is when switched off. nutmeg
removes the variables the other settings control from the environment it
inherits, so those settings are the only place they come from. Any other
variable can be added under "Extra environment
variables". A settings file from an older nutmeg that sets
`ROSEMARY_PREFIX_HOME`, `ROSEMARY_SERVER`, `ROSEMARY_CONTROL_EDIT_KEYS`,
`ROSEMARY_METAL_PRESENTATION_MODE`, `ROSEMARY_SCREEN_REFRESH_RATE` or
`SDL_VIDEO_DRIVER` there has the value moved
to its setting when it is loaded.

Rosemary has no variable for choosing between Wayland and X11. It opens its
windows with SDL 3 and leaves the choice to SDL, so the Display server setting
sets SDL's `SDL_VIDEO_DRIVER`. nutmeg also removes the older spelling,
`SDL_VIDEODRIVER`, from the environment it inherits, because SDL reads it when
`SDL_VIDEO_DRIVER` is unset. At "Default", SDL 3.4 uses Wayland when the
compositor supports the `wp_fifo_v1` protocol and X11 otherwise, which on a
Wayland desktop means XWayland. Choosing Wayland outside a Wayland session
leaves Rosemary unable to open a window.

The Performance settings start Roblox through other programs, found on
`PATH`, as `gamemoderun mangohud rosemary run …`:

| Setting                   | Program                                                                      |
| ------------------------- | ---------------------------------------------------------------------------- |
| Run with GameMode         | `gamemoderun`, from [GameMode](https://github.com/FeralInteractive/gamemode) |
| Show the MangoHud overlay | `mangohud`, from [MangoHud](https://github.com/flightlessmango/MangoHud)     |

Both set their variables and replace themselves with the command they are
given, so the process nutmeg watches is still Rosemary's. MangoHud draws its
overlay through Vulkan, which Rosemary's Metal runs on. If a switched-on
program is not installed, the launch stops and says so.

For any other program, such as gamescope or OBS's `obs-gamecapture`, fill in
"Wrapper command". It is written the way Steam's launch options are:
`%command%` stands for Rosemary and its arguments, and `NAME=value` words at
the start set environment variables for Roblox. Without `%command%`, the
command goes in front of Rosemary. Words after `%command%` are passed on to
Roblox. GameMode and MangoHud, when switched on, run inside the wrapper:

| Wrapper command                        | Runs                                                  |
| -------------------------------------- | ----------------------------------------------------- |
| `gamescope -f -- %command%`            | `gamescope -f -- gamemoderun mangohud rosemary run …` |
| `obs-gamecapture`                      | `obs-gamecapture rosemary run …`                      |
| `MANGOHUD_CONFIG=fps_only=1 %command%` | `rosemary run …` with `MANGOHUD_CONFIG` set           |

The words are split as a shell would split them, so quotes work, but nothing
else a shell does: there are no pipes, `$VARIABLES` or `~`, except that a `~`
at the start of the program's path is your home folder. A program without a
`/` is looked up on `PATH`. Variables that a setting controls, such as
`ROSEMARY_PREFIX`, cannot be set here.

## Discord

When "Show the game you are playing on Discord" is on (the default, on the
Integration page), each launch also starts `nutmeg watch`, which
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
sets a Discord activity for application 363445589247131668: the game's name,
"by" its creator, the game's icon, the time since joining, and a "See game
page" button that opens `https://www.roblox.com/games/<place id>`. On leaving
or losing the connection, the activity is cleared. Teleporting to another
place writes a new joining line, so the activity follows.

The API sometimes answers without a game's name or creator, or with
`[TITLE UNAVAILABLE]` and `[UNKNOWN]` in their place, so nutmeg looks the
game up again every minute while you play it and fills in what it learns. A
lookup that fails, or answers with those placeholders, keeps what an earlier
one found. Until the name is known, the activity says "In a Roblox game".

The activity goes through the Discord app's IPC socket, `discord-ipc-<n>` in
`XDG_RUNTIME_DIR`, or in the folder the Discord Flatpak or Snap, or the Vesktop
Flatpak, uses there. nutmeg's Flatpak can reach all of these but Vesktop's. If Discord is not running, nutmeg tries again every 15
seconds. Only one `nutmeg watch` runs at a time; a launch while one is
running leaves it to follow the new Roblox. Its own lines go to the launch log.

## Leaving a game

When "Close Roblox when you leave an experience" is on (on the Roblox page;
off by default), leaving a game closes Roblox instead of taking you back to
its home screen. `nutmeg watch` follows the client's logs, as for Discord,
and looks for the line the client writes when it goes back to its home screen
from a game:

```
[FLog::SingleSurfaceApp] returnToLuaApp: (stage:UGCGame).
```

Two seconds later, which leaves the client time to disconnect from the game's
server, nutmeg closes it the way it closes a running Roblox before a launch.
Joining another game within those two seconds keeps it open. Quitting Roblox
during a game, or losing the connection to the game's server, writes other
lines, and does not count as leaving.

`nutmeg watch` reads the saved settings again each time you join or leave a
game, so turning this on or off while Roblox runs applies straight away, as
long as `nutmeg watch` is running: that is, when this setting or the Discord
one was on when Roblox was started, and always in the Flatpak.

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

| Path                                                             | Contents                                                                         |
| ---------------------------------------------------------------- | -------------------------------------------------------------------------------- |
| `~/.config/nutmeg/config.json`                                   | settings                                                                         |
| `~/.local/share/nutmeg/versions/`                                | installed Roblox builds                                                          |
| `~/.local/share/nutmeg/prefix/`                                  | the Rosemary prefix, including Roblox's own files under `root/Users/<name>/`     |
| `~/.cache/nutmeg/downloads/`                                     | downloads in progress                                                            |
| `~/.local/state/nutmeg/logs/`                                    | one log per launch, with nutmeg's steps and everything Rosemary and Roblox print |
| `~/.local/share/applications/io.github.frolleks.Nutmeg*.desktop` | the menu entries                                                                 |
| `~/.local/share/icons/hicolor/*/apps/io.github.frolleks.Nutmeg*` | their icons                                                                      |

Each path follows the matching `XDG_*_HOME` variable when it is set. The logs
hide the authentication ticket in `roblox-player:` links; the last 20 are kept.

In the Flatpak, the first five are under `~/.var/app/io.github.frolleks.Nutmeg/`
(in `config/`, `data/`, `cache/` and `.local/state/`), the menu entries are the
Flatpak's own, and only Roblox's icon is written to `~/.local/share/icons/`.

## Releases and nightly builds

A new release is published when there's a bump version commit. Otherwise, there are also nightly builds that builds, you guessed it, every night.

### Nightly builds

`.github/workflows/nightly.yml` runs every day at 23:41 UTC.

### Building locally

To build the wheel, the source archive and the `.pyz` (the `build` package is
needed):

```sh
python3 scripts/build_nightly.py --version 1.0.1.dev1 --out dist
```

To build and install the Flatpak, with flatpak-builder (or Flathub's
`org.flatpak.Builder`):

```sh
flatpak-builder --user --install --install-deps-from=flathub --force-clean \
  flatpak_app flatpak/io.github.frolleks.Nutmeg.yml
```

The AppImage is built on Ubuntu 24.04 (or in a container of it) from
Ubuntu's packages, with the system's `python3`, and the AppImage runtime for
the machine from
[AppImage's type2-runtime releases](https://github.com/AppImage/type2-runtime/releases):

```sh
sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 librsvg2-common libglib2.0-bin squashfs-tools patchelf
curl -fLo runtime https://github.com/AppImage/type2-runtime/releases/download/20251108/runtime-x86_64
/usr/bin/python3 scripts/build_appimage.py --version 1.0.1.dev1 --runtime runtime --out dist
```

It bundles every library the bundled programs load, except those an AppImage
takes from the system it runs on (glibc, the graphics drivers, X11's and
Wayland's client libraries, fontconfig, FreeType and HarfBuzz, among others).
`--test` also runs the tests on what it bundles, which needs a display.

## Development

The tests use a local web server in place of Roblox's servers and a script in
place of Rosemary:

```sh
python3 -m unittest discover -s tests
```

The window tests need a display. Without one, run them under Xvfb:

```sh
xvfb-run -a python3 -m unittest discover -s tests
```
