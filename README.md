# AlbusWall - Modular Image Gallery for Hyprland Ricing

**Your wall of memories, frame by frame.**  
*A modular, plugin-driven image gallery for your ricing shots.*

[![Python](https://img.shields.io/badge/python-3.11+-blue.svg)](https://python.org)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Stage](https://img.shields.io/badge/stage-1.0-brightgreen.svg)]()
[![Version](https://img.shields.io/badge/version-1.0.0-lightgrey.svg)]()

> ⚠️ **Early Development Notice**  
> AlbusWall is still in **active development**.  
> The project has recently gone through a major refactor. The old temporary CLI has been removed, a plugin system has been introduced, and the built-in UI is now replaceable. The core no longer hard-depends on PySide6.  
> Most previously working features are being restored on top of the new architecture and should return quickly.  
> This is a personal spare-time project and my first public Python application. Feedback, issues, and patience are deeply appreciated.

**AlbusWall** is an image gallery designed to showcase **Hyprland ricing setups** with a clean, HIG-inspired interface.  
Under the hood, it is now built as a modular application: the core manages media, metadata, views, and services, while the UI is provided through plugins.

---

## ✨ What Changed in the Refactor

This is not just a cleanup — the architecture is now much more flexible.

- **Temporary CLI removed**  
  The previous short-lived CLI entry point is gone. Future CLI support, if needed, should be implemented as a plugin or a separate frontend instead of being coupled to the GUI.

- **Plugin system added**  
  AlbusWall now has plugin discovery, declaration, and lifecycle management.  
  Relevant modules:
  - `albuswall.plugin.declaration`
  - `albuswall.plugin.discovery`
  - `albuswall.plugin.manager`

- **UI is now replaceable**  
  The built-in UI is implemented as a plugin. The core application no longer forces PySide6 as a hard dependency.  
  Relevant modules:
  - `albuswall.ui.protocol`
  - `albuswall.ui.bootstrap`
  - `albuswall.plugins.builtin_ui`

- **Most technical debt addressed**  
  The project is now split into clearer layers: common, config, core, DTOs, infrastructure, logging, plugins, repositories, services, UI, and utilities.

- **Feature recovery is in progress**  
  Grid rendering, album switching, image viewing, and other previous GUI capabilities are being rebuilt on the new plugin-based foundation.

---

## 🧠 Architecture Overview

AlbusWall is designed so the core is independent from any specific UI toolkit.

```text
┌─────────────────────────────────────┐
│              Frontends              │
│   builtin_ui / third-party UI       │
└──────────────────┬──────────────────┘
                   │ UI protocol
┌──────────────────▼──────────────────┐
│           Plugin Manager            │
│  discovery, declaration, lifecycle  │
└──────────────────┬──────────────────┘
                   │
┌──────────────────▼──────────────────┐
│        Services & Repositories      │
│  import, view, album, source, task  │
└──────────────────┬──────────────────┘
                   │
┌──────────────────▼──────────────────┐
│        Infrastructure / DB          │
│      SQLite, logging, config        │
└─────────────────────────────────────┘
```

The built-in UI is only one possible frontend. In principle, another Qt UI, GTK UI, TUI, web UI, or headless automation layer can be added as a plugin without rewriting the core.

---

## 🚀 Quick Start

### Arch Linux (AUR)

```bash
yay -S albuswall          # or: paru -S albuswall
```

The AUR package pulls in `pyside6`, `python-psutil`, `python-pillow` and
`python-apscheduler` (AUR), and installs:

| Path | Content |
|---|---|
| `/usr/bin/albuswall` | GUI entry point |
| `/usr/bin/albusctl` | out-of-process control CLI |
| `/usr/share/applications/albuswall.desktop` | desktop entry (menu / launcher) |
| `/usr/share/icons/hicolor/scalable/apps/albuswall.svg` | application icon |

Everything the app needs at runtime (`config/`, `sql/`, `themes/`, `i18n/`,
`icons/`) ships inside the Python package — the app reads it from
`albuswall/resources/`, never from `/usr/share`.

### From source

```bash
git clone https://github.com/Zer0x70e8/albusWall.git
cd albusWall
python -m venv .venv && . .venv/bin/activate
pip install -e .
```

`pip install -e .` needs PySide6, Pillow, psutil and APScheduler, and registers
both console scripts (`albuswall`, `albusctl`). For a throwaway environment you
can also install only what the built-in UI needs:

```bash
pip install PySide6 Pillow psutil apscheduler
```

> The core is no longer hard-dependent on PySide6.  
> If you only want to develop or test the core/plugin system, you may not need PySide6 until the built-in UI plugin is loaded.

### Launch the Application

```bash
albuswall                 # installed entry point
python -m albuswall       # same thing, no install required (cd src first)
```

With an editable/not-installed checkout:

```bash
cd src
python -m albuswall
```

If your current build supports explicit UI plugin selection, it may look like this:

```bash
python -m albuswall --ui builtin
```

> Please check `python -m albuswall --help` for the actual arguments in your current revision.  
> This README intentionally avoids promising CLI flags that may still be changing.

---

## 🔌 Plugin & UI System

AlbusWall now treats functionality and frontends as plugins.

### Core plugin modules

| Module | Responsibility |
|---|---|
| `plugin/declaration.py` | Plugin interfaces and declarations |
| `plugin/discovery.py` | Finding available plugins |
| `plugin/manager.py` | Loading, initialising, and managing plugins |
| `plugins/builtin_ui.py` | The default UI provided as a plugin |
| `ui/protocol.py` | UI abstraction / protocol |
| `ui/bootstrap.py` | UI startup and wiring |

### Why this matters

- The core can run without a GUI.
- The built-in UI can be replaced without changing domain logic.
- New features can be added as plugins.
- Future CLI, headless tools, or alternative frontends are not blocked by the GUI architecture.

---

## 🔗 Cross-Process Calls (IPC)

A running AlbusWall instance can be driven from **other processes** — a CLI,
a shell script, a Hyprland keybind, or another app.

The core idea: the GUI process owns the state (SQLite, `SourceService.state`,
Qt models, the task queue). Another process cannot just import the code —
it would get a second, empty copy of that state and race for the same rows.
So the running instance listens on a local endpoint and executes calls **on
its own main thread**.

```bash
albusctl ping           # is it running?
albusctl sources        # list tracked import sources
albusctl import         # wake the import worker
albusctl thumbs         # queue thumbnail backfill
albusctl stats          # task service counters
albusctl watch import.assets_imported   # stream events
albusctl call source.list '{}'          # any registered command
albusctl quit
```

`albusctl` exists after `pip install -e .` (it is a `[project.scripts]` entry).
From a plain source checkout — no install — use the wrapper, which puts `src/`
on `PYTHONPATH` and picks the repo's own venv:

```bash
./scripts/albusctl.sh ping     # note the ./ — fish & zsh don't search CWD
```

Every name printed by `albusctl list` can be used directly as a subcommand
(`albusctl task.stats` == `albusctl call task.stats`); the short aliases
(`sources`, `import`, `thumbs`, `stats`, `status`, `quit`) are just sugar.

Exit codes: `0` ok · `2` usage · `3` not running · `4` token mismatch ·
`5` remote error · `6` timeout · `130` interrupted.

### Design

| Layer | Module | Responsibility |
|---|---|---|
| Endpoint | `utils/ipc_endpoint.py` | socket address + token, shared by both sides |
| Protocol | `common/ipc.py` | JSON-RPC style frames, error codes, exceptions |
| Commands | `services/command.py` | `@command` registry + `Dispatcher` |
| Transport | `infrastructure/ipc/server.py` | accept loop, connections, forwarding |
| Client | `client/` | `albusctl` — no GUI, no core imports |

- **Transport is `multiprocessing.connection`**: `AF_UNIX` on POSIX,
  named pipes (`AF_PIPE`) on Windows — one code path, zero dependencies.
  Frames are `send_bytes`/`recv_bytes` JSON, never pickle.
- **Commands run on the main loop thread**, dispatched via
  `MainLoop.call_soon`, because service state has a single-thread owner.
- **Address** lives in `$XDG_RUNTIME_DIR/albuswall/ipc.sock` (`0600`, dir
  `0700`) or `\\.\pipe\albuswall-<user>`; the `authkey` token sits next to
  it. Binding the endpoint doubles as a **single-instance lock**.
- The endpoint is a plugin (`plugins/ipc.py`), so `[plugin_disables] ipc`
  removes it entirely and `[ipc] enabled = false` just stops listening.

### Adding a command

```python
from albuswall.services.command import command

@command("source.rescan")
def _rescan(container, *, source_id: int) -> dict:
    # runs on the main loop thread; keep it short, or hand off to task_service
    container.get("source_service").update_source(source_id)
    return {"accepted": True}
```

Layout:

```text
common/ipc.py              # frames + error codes
common/enums/ipc.py        # IpcErrorCode
utils/ipc_endpoint.py      # address + token
services/command.py        # @command + Dispatcher + builtin commands
infrastructure/ipc/        # config, server, bootstrap
client/                    # IpcClient + albusctl
plugins/ipc.py             # plugin declaration
test/test_ipc.py           # headless end-to-end tests
```

### Logs

IPC activity follows the usual levels, so the global `[log] level` decides how
much you see:

| Event | Level |
|---|---|
| `IPC server listening / stopped` | INFO |
| connection opened / closed, `req#N <method> ok in X ms` | DEBUG |
| unknown command, bad params, broken frame (i.e. the **caller** is wrong) | WARNING |
| command raised inside the app | ERROR (with traceback) |

The shipped `conf.ini` defaults to `[log] level = WARNING`, which means a
healthy start is silent and only caller mistakes show up. While working on
IPC, set `level = DEBUG` (or `TRACE`) in `~/.config/albuswall/config.ini`.

No-log quick check instead of reading logs:

```bash
albusctl status     # running / address / clients / accepted / handled / errors
```

---

## 📁 Project Layout

```text
src/
└── albuswall/
    ├── __about__.py
    ├── __main__.py
    ├── common/              # Enums, exceptions, shared primitives
    ├── configue/            # Configuration system
    ├── core/                # Application lifecycle, bootstrap, runtime
    ├── dto/                 # Data transfer objects
    ├── infrastructure/      # Database and infrastructure bootstrapping
    ├── log/                 # Logging system
    ├── plugin/              # Plugin declaration, discovery, manager
    ├── plugins/             # Built-in plugins, e.g. builtin_ui
    ├── repositories/        # Data access layer
    ├── resources/           # Config, SQL, themes, i18n, app icon (package data)
    ├── services/            # Business logic
    ├── ui/                  # UI abstraction and bootstrap
    │   ├── builtin/
    │   ├── protocol.py
    │   └── vo/
    └── utils/               # Path, signal, mount, time, SQL helpers

packaging/
├── albuswall.desktop        # Desktop entry
└── aur/                     # PKGBUILD + .SRCINFO for Arch Linux
```

> Note: the directory name `configue/` follows the current repository layout.

---

## 🧩 Dependencies

- Python ≥ 3.11
- SQLite via the standard library
- Core: standard library + project modules
- Optional built-in UI: PySide6
- image processing: Pillow ≥ 10.0

PySide6 is no longer a mandatory core dependency.  
It is required only when using the built-in PySide6 UI plugin.

---

## 📦 Building & Packaging

The project uses [hatchling](https://hatch.pypa.io/) and a `src/` layout, so
`pyproject.toml` is the single source of truth for metadata and entry points.

```bash
python -m build           # wheel + sdist into dist/
```

### Resources

Runtime assets live inside the package (`src/albuswall/resources/`):

```text
resources/
├── config/     conf.ini, log_config.ini     # defaults copied to ~/.config
├── i18n/       albuswall_<lang>.{ts,qm}     # translations
├── icons/      albuswall.svg                # application / window icon
├── sql/        media_library_schema.sql
└── themes/     default/main_window.qss + icons/
```

They are **not** separate data files: hatchling collects every non-`.py` file
under the package, so `pip install` / the AUR package ship them in
`site-packages/albuswall/resources/` and `albuswall.resources.get()` resolves
them there. Nothing is written back into the package at runtime — user copies
(theme, config, thumbnails) go to `~/.config/albuswall` / `~/.cache/albuswall`.

### Arch Linux / AUR

| File | Purpose |
|---|---|
| `packaging/aur/PKGBUILD` | AUR recipe (`pkgname=albuswall`) |
| `packaging/aur/.SRCINFO` | pre-generated metadata for the AUR web UI |
| `packaging/albuswall.desktop` | desktop entry |
| `scripts/aur_release.py` | fills the source checksum + regenerates `.SRCINFO` |

`PKGBUILD` installs the wheel with `python -m installer`, then adds the desktop
entry, the hicolor icon and the MIT license.

The checksum can only be computed after the release tag exists, so the release
order is:

```bash
# 1. bump version, commit, tag, push
git tag -a v1.0.0 -m "AlbusWall 1.0.0" && git push origin v1.0.0

# 2. compute the tag tarball hash and regenerate .SRCINFO
python scripts/aur_release.py            # --tarball <file> to use a local archive

# 3. copy PKGBUILD + .SRCINFO into the AUR repo and push
```

```bash
# verify the recipe locally before publishing
cd packaging/aur && makepkg --printsrcinfo > .SRCINFO   # regenerate only
```

---

## 🛠 Development Status & Roadmap

### Completed / Refactored

- [x] Major architecture refactor
- [x] Plugin system: declaration, discovery, manager
- [x] Replaceable UI abstraction
- [x] Built-in UI moved into a plugin
- [x] Core no longer hard-depends on PySide6
- [x] Removed temporary CLI entry point
- [x] Database, repository, and service layers reorganised
- [x] Theming / theme switching
- [x] Configuration foundation

### Restoring / In Progress

- [ ] Image directory scanner
- [ ] Thumbnail generation and cache cleanup
- [ ] Grid layout rendering
- [ ] Basic image viewing
- [ ] Album switching in the GUI
- [ ] Album editing
- [ ] Gallery interaction: selection, zoom
- [ ] Single image detail view
- [ ] Settings GUI
- [ ] Logging system
- [x] Packaging: wheel / sdist + Arch Linux (AUR)
- [ ] PyPI release

The new architecture is intended to make these features faster to restore and easier to maintain.

---

## 🎯 Planned Features

- Responsive HIG-like grid layout
- Custom title bar and window controls
- QSS stylesheet support and theme switching
- Smooth scrolling and animated labels
- Windows 11 corner radius awareness
- Purpose-built presentation for Hyprland and other WM ricing shots
- Plugin-based UI and feature extensions

---

## 🤝 Contributing

AlbusWall is a personal learning project, but contributions, issues, and design feedback are welcome.

Useful areas right now:

- Testing the plugin system
- Reviewing the UI protocol abstraction
- Helping restore previous gallery features
- Improving documentation
- Reporting architecture or packaging issues

Please open an issue before starting large changes so the direction can be discussed first.

---

## 👤 About the Author

Zer0x70e8 — a solo developer passionate about Linux ricing, desktop aesthetics, and learning Python GUI development.  
GitHub: [@Zer0x70e8](https://github.com/Zer0x70e8)

---

## 📄 License

MIT — see [LICENSE](LICENSE) for details.

---

*AlbusWall — Your wall of memories, frame by frame.*
