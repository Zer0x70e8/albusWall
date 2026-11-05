# AlbusWall - Modular Image Gallery for Hyprland Ricing

**Your wall of memories, frame by frame.**  
*A modular, plugin-driven image gallery for your ricing shots.*

[![Python](https://img.shields.io/badge/python-3.11+-blue.svg)](https://python.org)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Stage](https://img.shields.io/badge/stage-1.0-brightgreen.svg)]()
[![Version](https://img.shields.io/badge/version-1.1.0-lightgrey.svg)]()

> ⚠️ **Early Development Notice**  
> AlbusWall is still in **active development**.  
> The project has just finished a major refactor (configuration layer, service layer, and a presenter-based UI). A plugin system has replaced the old temporary CLI, the built-in UI is replaceable, and the core no longer hard-depends on PySide6.  
> Most of the previous gallery features are restored on the new architecture; the settings GUI and shortcut wiring are still missing.  
> This is a personal spare-time project and my first public Python application. Feedback, issues, and patience are deeply appreciated.

**AlbusWall** is an image gallery designed to showcase **Hyprland ricing setups** with a clean, HIG-inspired interface.  
Under the hood, it is now built as a modular application: the core manages media, metadata, views, and services, while the UI is provided through plugins.

---

## ✨ What Changed in the Refactor

This is not just a cleanup — the architecture is now much more flexible.

- **Temporary CLI replaced**  
  The previous short-lived in-process CLI is gone. Control from outside now goes
  through the IPC endpoint (`albusctl`, see below), so the GUI process stays the
  single owner of the state instead of a second process importing the same code.

- **Plugin system added**  
  AlbusWall now has plugin discovery, declaration, and lifecycle management.  
  Relevant modules:
  - `albuswall.core.plugins` (`Plugin`, `PluginManager`, `@plugin`)
  - `albuswall.core.application` (`Application`: lifecycle, slots, hooks)
  - `albuswall.plugin.discovery`

- **UI is now replaceable**  
  The built-in UI is implemented as a plugin. The core application no longer forces PySide6 as a hard dependency.  
  Relevant modules:
  - `albuswall.ui.application` (`Application` / `UIApplication`, the passive Qt `MainLoop`)
  - `albuswall.ui.bootstrap` (`setup_ui` / `show_ui`: UI-side boot — language, window, presenters)
  - `albuswall.ui.window` / `albuswall.ui.widgets` (pure views: they only emit intents)
  - `albuswall.ui.presenter` (presentation layer: `MainPresenter` orchestrates every presenter's lifecycle and owns the window theme)
  - `albuswall.plugins.builtin_ui`

- **Most technical debt addressed**  
  The project is now split into clearer layers: common, config, core, DTOs, infrastructure, logging, plugins, repositories, services, UI, and utilities.

- **Feature recovery is largely done**  
  Grid rendering, albums, tags, the detail view, thumbnails and the asset trash
  were rebuilt on the new foundation in this refactor. What is still missing is
  the settings GUI and the final wiring of the shortcut library into the views —
  see the roadmap below.

---

## 🧠 Architecture Overview

AlbusWall is designed so the core is independent from any specific UI toolkit.

```text
┌─────────────────────────────────────┐
│              Frontends              │
│   builtin_ui / third-party UI       │
│   views  ──►  presenters (MainPresenter root)
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

Inside the built-in UI, views (`ui/window`, `ui/widgets`) only emit intents; the
`albuswall.ui.presenter` package turns those intents into service calls and writes
results back. `MainPresenter` is the single root of that presenter tree — it builds
every child presenter from the dependency container, wires them together, mirrors
the configured window theme, and cascades `start()` / `stop()` to the whole tree.

---

## 🚀 Quick Start

### Arch Linux (AUR)

> **Status:** the AUR package is **not published yet** (`albuswall` does not
> exist on aur.archlinux.org), so `yay -S albuswall` will fail. The recipe in
> `packaging/aur/` is ready — build it locally until the first release is
> pushed:

```bash
git clone https://github.com/Zer0x70e8/albusWall.git
cd albusWall/packaging/aur
makepkg -si                    # needs a tag whose tarball hash is filled in
```

The recipe pulls in `pyside6`, `python-psutil`, `python-pillow` and
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

`python -m albuswall` is a single entry point: `entrypoint.should_launch_main()`
decides whether this process becomes the GUI or acts as an IPC client
(`status` / `start` / `stop` / `restart` / `reload` / `version`, or `--cli`).
There is no `--ui` selector — the built-in UI is a plugin, picked through the
plugin slot, so alternative frontends are chosen by plugins rather than flags.

---

## 🔌 Plugin & UI System

AlbusWall now treats functionality and frontends as plugins.

### Core plugin modules

| Module | Responsibility |
|---|---|
| `core/plugins.py` | `Plugin` protocol, `PluginManager`, `@plugin` registration |
| `core/application.py` | `Application` lifecycle, slot resolution, hook dispatch |
| `core/main_loop.py` | `MainLoop` protocol (`call_soon` / `run` / `stop`) |
| `plugin/discovery.py` | Finding available plugins (entry points, directories, packages) |
| `plugins/builtin_ui.py` | The default UI as a plugin: provides `main_loop`, runs the UI boot |
| `plugins/command.py` | Assembles every command handler, then freezes the registry |
| `ui/application.py` | Qt `MainLoop` (`Application` / `UIApplication`) — passive, no boot |
| `ui/bootstrap.py` | UI boot: `[ui] language` → window → shortcut registry → presenters → container keys (the window is taken from the container, so a derived window replaces it) |
| `ui/window/`, `ui/widgets/` | Pure views (they only emit intent signals) |
| `ui/presenter/` | Presentation layer: `MainPresenter` + one presenter per screen |

### Why this matters

- The core can run without a GUI.
- The built-in UI can be replaced without changing domain logic.
- New features can be added as plugins.
- Future CLI, headless tools, or alternative frontends are not blocked by the GUI architecture.

### Theming & objectName inspection

The built-in theme lives in `resources/themes/<name>/`:

```text
resources/themes/default/
├─ main_window.qss     # 全量样式表（按 objectName / 类名选择器命中）
└─ icons/              # 主题图标（标题栏按钮 / 收藏按钮 / 应用图标）
```

Widgets get their `objectName` automatically (`auto_set_object_names`), e.g.
`#TitleBarAlbumButton`, `#AlbumAlbumView`, `#AlbumTrashView`,
`#DetailFavouriteButton`. The style sheet can be inspected against the real
widget tree with the bundled CLI — run it whenever you touch selectors:

```bash
QT_QPA_PLATFORM=offscreen python src/albuswall/ui/utils/qt_objectname_utils.py \
    src/albuswall/ui/window/window.py --style tree      # 整棵控件树
QT_QPA_PLATFORM=offscreen python src/albuswall/ui/utils/qt_objectname_utils.py \
    src/albuswall/ui/window/window.py --style table     # 名字 ↔ 类名
```

`#objectName` selectors in `main_window.qss` must match what that CLI prints
(`tests/test_ui_presenter_flow.py::test_theme_qss_selectors_match_widget_object_names`
guards this). Icons are not referenced from QSS: the theme's `icons/*.svg` are
installed programmatically by `WindowPresenter` (icon-only capsule buttons,
text moved into tooltips), and fall back to plain text buttons when a theme
ships no icons.

#### Theme paths are composed by the config resolver

The UI owns **no** path constants: every theme file location is a `${...}`
expression that `albuswall.configue` expands at read time. `[path]` holds the
directories, `[files]` the file names, and `[ui]` glues them together:

```ini
[path]
theme = ${path:config}/theme                 # user theme root: <theme>/<name>/…
icons = ${theme}/${ui:theme}/icons           # icons/ of the *active* theme

[files]
main_window_qss = main_window.qss
app_icon      = albuswall.svg
album_button  = album_button.svg
search_button = search_button.svg
more_button   = more_button.svg
favorite_icon = favorite_icon.svg

[ui]
theme       = default
style_sheet = ${path:theme}/${ui:theme}/${files:main_window_qss}
album_button = ${path:icons}/${files:album_button}
# … the other icons follow the same shape
```

Consequences worth knowing:

- **One key switches everything.** Writing `[ui] theme` (what
  `MainPresenter.set_theme` persists) invalidates the `${ui:theme}` expansion, so
  the stylesheet *and* every icon path move together — no repathing code in the
  UI.
- **User themes override bundled ones.** Drop
  `~/.config/albuswall/theme/<name>/main_window.qss` (and optionally
  `icons/`) and it is used instead of `resources/themes/<name>/`; anything the
  user theme does not provide falls back to the bundled theme (unknown names
  fall back to `default`).
- **Every icon is configurable.** A button/target asks the theme for a *logical
  name* (`album_button`, `search_button`, `more_button`, `favorite_icon`,
  `app_icon`); the file name comes from `[files] <same key>`, the directory from
  `[path] icons`. Rename the file there, or point the `[ui]` key at any absolute
  path.
- The defaults of the declaration classes (`ThemeConfig`, `Paths`, `Files`) are
  byte-for-byte the shipped `conf.ini` expressions, so a config file is optional
  (`tests/test_ui_presenter.py::test_defaults_equal_shipped_conf` guards the
  drift).

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
albusctl help                    # every registered command, grouped by namespace
albusctl version                 # project version + IPC protocol version
albusctl ping                    # answered on the transport thread
albusctl source list             # namespace form of `source.list`
albusctl album create 旅行 --description="trip shots"
albusctl asset trash <asset_uuid> # positional args + --flags are typed by ast
albusctl task.stats              # task service counters
albusctl thumbnail.request <asset_uuid>
```

`albusctl` exists after `pip install -e .` (it is a `[project.scripts]` entry).
From a plain source checkout — no install — use the wrapper, which puts `src/`
on `PYTHONPATH` and picks the repo's own venv:

```bash
./scripts/albusctl.sh help     # note the ./ — fish & zsh don't search CWD
```

Every registered command is reachable two ways: the dotted path
(`albusctl source.list`) and the namespace form (`albusctl source list`), which
is the same dispatcher underneath. `albusctl help --all` includes hidden
commands, `albusctl help <command>` prints one command's parameters.

Exit codes: `0` ok · `2` usage · `3` not running · `4` token mismatch ·
`5` remote error · `6` timeout · `130` interrupted.

### Design

| Layer | Module | Responsibility |
|---|---|---|
| Endpoint | `infrastructure/ipc/_endpoints.py` | socket address + token, shared by both sides |
| Protocol | `common/ipc.py` | JSON-RPC style frames, error codes, exceptions |
| Registry | `command/registry.py` | `CommandRegistry`, `Param` schemas, ACL |
| Dispatcher | `service/command/dispatcher.py` | argument binding, error translation |
| Handlers | `service/command/handlers/` | one module per domain (`album`, `asset`, `tag`, …) |
| Transport | `infrastructure/ipc/manager.py` | `register_ipc`: accept loop, connections, forwarding |
| Client | `cli.py` | `albusctl` — no GUI, no core imports beyond version |

- **Transport is `multiprocessing.connection`**: `AF_UNIX` on POSIX,
  named pipes (`AF_PIPE`) on Windows — one code path, zero dependencies.
  Frames are `send_bytes`/`recv_bytes` JSON, never pickle.
- **Commands run on the main loop thread**, dispatched via
  `MainLoop.call_soon`, because service state has a single-thread owner.
- **Address** lives in `$XDG_RUNTIME_DIR/albuswall/ipc.sock` (`0600`, dir
  `0700`) or `\\.\pipe\albuswall-<user>`; the `authkey` token sits next to
  it. Binding the endpoint doubles as a **single-instance lock**.
- The endpoint is registered by `infrastructure.bootstrap` (`register_ipc`),
  so it is part of the infrastructure layer rather than a plugin; business
  commands are separate handler modules assembled by the `command` plugin.

### Adding a command

```python
from albuswall.command import CommandRegistry
from albuswall.core import Container

from ._params import pos, opt

def setup(registry: CommandRegistry, container: Container) -> None:
    source_service = container.require("source_service")

    @registry.command(
        "source.rescan",
        help_="rescan one import source",
        params=(pos("source_id", help="source id"),),
    )
    def source_rescan(source_id: int) -> dict:
        # runs on the main loop thread; keep it short, or hand off to task_service
        source_service.scan(source_id)
        return {"accepted": True}
```

Then add the module to `MODULES` in
`service/command/handlers/__init__.py` — the list is explicit on purpose, no
directory scanning, and `registry.freeze()` in the `command` plugin turns a
missing registration into a startup error instead of a silent gap.

Layout:

```text
common/ipc.py                     # frames + error codes
common/enums/ipc.py               # IpcErrorCode
infrastructure/ipc/_endpoints.py  # address + token
infrastructure/ipc/manager.py     # register_ipc, server loop
command/registry.py               # CommandRegistry + Param schemas
service/command/dispatcher.py     # bind params, translate errors
service/command/handlers/         # one module per domain
cli.py                            # IpcClient + albusctl entry point
tests/test_handlers_*.py          # headless end-to-end tests
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
    ├── __about__.py           # version, authors, supported Python range
    ├── __main__.py            # `python -m albuswall`
    ├── entrypoint.py          # decide: launch GUI or act as IPC client
    ├── main.py                # composition root: container + plugins
    ├── cli.py                 # `albusctl` (IPC client, no GUI imports)
    ├── command/               # CommandRegistry, Param schemas, ACL, exceptions
    ├── common/                # enums, exceptions, IPC frames, primitives
    ├── configue/              # declaration → validation → atomic write
    │   ├── bootstrap.py       # setup_config(): [path]/[files] + aliases
    │   ├── registry.py        # @configclass declarations
    │   ├── schema.py          # enumerable/writable settings tree
    │   └── backends/ resolver.py serialization.py tree.py utils/
    ├── core/                  # Application, PluginManager, MainLoop protocol
    ├── dto/                   # DTOs split by Command / Query ownership
    │   ├── album/ asset/ tag/ source/ thumbnail/ utils/
    │   └── import_.py task.py trash.py trigger.py sentinel.py
    ├── infrastructure/        # database, ipc, task, trigger, reconcile, platform
    ├── log/                   # logging system (filters, formatters, handlers)
    ├── plugin/                # discovery (entry points / directories / packages)
    ├── plugins/               # built-ins: builtin_ui, command
    │                          # sample: hello.py.txt (not auto-loaded)
    ├── repositories/          # data access (+ utils/visibility.py)
    ├── resources/             # conf.ini, shortcuts.ini, sql, themes, i18n, icons
    ├── service/               # business logic, one package per domain
    │   ├── album/ asset/ source/ tag/ thumbnail/ view/ importer/
    │   ├── command/           # dispatcher + one handler module per domain
    │   └── main_loop_service.py
    ├── ui/                    # the built-in Qt UI
    │   ├── application.py     # Qt MainLoop (Application / UIApplication)
    │   ├── window/ widgets/   # views: they only emit intent signals
    │   ├── presenter/         # one presenter per screen, MainPresenter root
    │   ├── delegate/ dialog/ model/ anims/
    │   ├── keymap/            # key identities, bindings, config, Qt key binder
    │   ├── i18n.py languages.py
    │   └── utils/             # objectName inspector, signal adapter
    └── utils/                 # paths, images, thumbnails, time, SQL helpers

assets/                        # screenshots and promo images
docs/                          # i18n.md, keymap.md, plugin-author-guide.md
packaging/
├── albuswall.desktop          # Desktop entry
└── aur/                       # PKGBUILD + .SRCINFO for Arch Linux
scripts/                       # albusctl.sh, dbtool.sh, build_i18n.py,
                               # i18n_catalog.py, aur_release.py
tests/                         # pytest suite (headless: QT_QPA_PLATFORM=offscreen)
```

> Note: the directory name `configue/` follows the current repository layout.

---

## 🧩 Dependencies

- Python ≥ 3.11 (3.11 – 3.14 supported)
- SQLite via the standard library
- Core: standard library + `psutil`, `apscheduler`
- Optional built-in UI: PySide6
- Image processing: Pillow ≥ 12.3.0

`pyproject.toml` pins the ranges actually tested:

| Package | Constraint | Why |
|---|---|---|
| `psutil` | `>=7.2.2` | load/memory aware concurrency control |
| `apscheduler` | `>=3.10.0` | trash cleanup + scheduled tasks |
| `pillow` | `>=12.3.0` | thumbnails, EXIF, format sniffing |
| `pyside6` | `>=6.11.0` | the built-in UI plugin |

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
├── config/     conf.ini, log_config.ini, shortcuts.ini   # defaults copied to ~/.config
├── i18n/       albuswall_<lang>.{ts,qm}                  # translations
├── icons/      albuswall.svg                             # application / window icon
├── sql/        media_library_schema.sql
└── themes/     default/main_window.qss + icons/
```

`shortcuts.ini` is the default key map: one line per command, in the
`command.path = [keys, positional_args, keyword_args]` form documented inside
the file itself. Every command the built-in UI registers is listed there; the
six that ship unbound (`window.album`, `window.source`, `viewer.next`,
`viewer.prev`, `content.preview`, `asset.restore`) are written as `= []` --
give them a key in the user file to switch them on. User overrides live in
`<user config dir>/shortcuts.ini` and win per command path.

Translations are generated, not hand-edited. `scripts/i18n_catalog.py` holds
the translation table; `scripts/build_i18n.py` runs `pyside6-lupdate` over
`src/albuswall/ui`, merges both sources and compiles `.qm`:

```bash
python scripts/build_i18n.py            # regenerate .ts + .qm under resources/i18n
python scripts/build_i18n.py --check    # CI: fail if a translation is missing
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
entry, the hicolor icon and the MIT license. `build()` needs the `makedepends`
(`python-build`, `python-hatchling`, `python-installer`, `python-wheel`), and
`package()` reads `packaging/albuswall.desktop` from the source tree — so the
recipe has to be built from a checkout or a source tarball, not from a bare
`PKGBUILD` download.

The checksum can only be computed after the release tag exists
(`sha256sums=('SKIP')` in the repository build passes `makepkg`, but is not
acceptable for AUR), so the release order is:

```bash
# 1. bump version, commit, tag, push
git tag -a v1.1.0 -m "AlbusWall 1.1.0" && git push origin v1.1.0

# 2. compute the tag tarball hash and regenerate .SRCINFO
python scripts/aur_release.py            # --tarball <file> to use a local archive
                                         # --check / --srconly for dry runs

# 3. copy PKGBUILD + .SRCINFO into the AUR repo and push
git clone ssh://aur@aur.archlinux.org/albuswall.git
cp packaging/aur/PKGBUILD packaging/aur/.SRCINFO albuswall/ && cd albuswall
git add -A && git commit -m "albuswall 1.1.0" && git push
```

```bash
# regenerate .SRCINFO only, without touching the checksum
cd packaging/aur && makepkg --printsrcinfo > .SRCINFO
```

The AUR push needs an AUR account whose SSH key is registered on
aur.archlinux.org (a GitHub key is not enough) — that key is the last step
before `yay -S albuswall` / `paru -S albuswall` starts working for everybody
else.

---

## 🛠 Development Status & Roadmap

### Completed / Refactored

- [x] Major architecture refactor (configue, services, presenters)
- [x] Plugin system: declaration, discovery, lifecycle
- [x] Replaceable UI abstraction
- [x] Built-in UI moved into a plugin
- [x] Core no longer hard-depends on PySide6
- [x] Database, repository, and service layers reorganised
- [x] Configuration system: declare → validate → atomic write
- [x] Theming / theme switching (`main_window.qss` + presenter-driven icons)
- [x] Image directory scanner and import pipeline
- [x] Thumbnail generation on demand + cache maintenance
- [x] Grid rendering with virtual scrolling
- [x] Single image detail view, zoom, information panel
- [x] Albums (create / rename / reorder / cover / trash) and tag trees
- [x] Asset trash with retention policy and cleanup
- [x] Out-of-process control (`albusctl` over IPC)
- [x] i18n (`zh_CN` / `en_US`) with `scripts/build_i18n.py`
- [x] Packaging: wheel / sdist + Arch Linux (AUR recipe)

### Restoring / In Progress

- [ ] Settings GUI (settings page, key-binding page, about page)
- [x] Registering the current UI's actions as shortcut slots (config-driven)
- [x] Real key events: application-level `ShortcutBinder` (single keys, modifier combos, chords)
- [ ] Multi-file config includes (`[configue] include`)
- [ ] Publishing to PyPI / AUR

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

## 📚 Documentation

| Document | Content |
|---|---|
| `docs/plugin-author-guide.md` | writing a plugin: declaration, hooks, lifecycle, examples |
| `docs/plugin-system-pr-description.md` | rationale behind the plugin architecture |
| `docs/i18n.md` | translation pipeline: `tr()` conventions, catalogs, language switching |
| `docs/keymap.md` | shortcut model: config format, key identities (aliases), registry, chords, UI slots, Qt binder |
| `MIGRATION_CONFIGUE1.md` | how the `configue` refactor maps onto the previous config layer |
| `src/albuswall/dto/CONVENTIONS.md` | DTO ownership (Command vs Query) and naming |

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
