# AlbusWall - Modular Image Gallery for Hyprland Ricing

**Your wall of memories, frame by frame.**  
*A modular, plugin-driven image gallery for your ricing shots.*

[![Python](https://img.shields.io/badge/python-3.10+-blue.svg)](https://python.org)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Stage](https://img.shields.io/badge/stage-alpha-orange.svg)]()
[![Version](https://img.shields.io/badge/version-0.1.3--dev-lightgrey.svg)]()

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

**There is no stable package release yet.**  
Clone the repository and install the optional dependencies needed by the built-in UI.

```bash
git clone https://github.com/Zer0x70e8/albusWall.git
cd albusWall
```

For the built-in PySide6 UI and image features:

```bash
pip install PySide6 Pillow
```

> The core is no longer hard-dependent on PySide6.  
> If you only want to develop or test the core/plugin system, you may not need PySide6 until the built-in UI plugin is loaded.

### Launch the Application

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
    ├── resources/           # Config, SQL, themes
    ├── services/            # Business logic
    ├── ui/                  # UI abstraction and bootstrap
    │   ├── builtin/
    │   ├── protocol.py
    │   └── vo/
    └── utils/               # Path, signal, mount, time, SQL helpers
```

> Note: the directory name `configue/` follows the current repository layout.

---

## 🧩 Dependencies

- Python ≥ 3.10
- SQLite via the standard library
- Core: standard library + project modules
- Optional built-in UI: PySide6
- image processing: Pillow ≥ 10.0

PySide6 is no longer a mandatory core dependency.  
It is required only when using the built-in PySide6 UI plugin.

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
- [ ] Packaging and PyPI release

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
