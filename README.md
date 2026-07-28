# Web Update Notifier

A lightweight, persistent Linux desktop utility to monitor web documents and browser bookmarks for updates. When modifications are detected, it issues native Linux desktop notifications with direct browser action buttons and hosts a local HTML dashboard.

Licensed under the BSD 3-Clause License.

## Features

- **Refined Body Content Extraction**: Parses HTML body text while filtering out `<head>`, `<header>`, `<footer>`, `<form>`, and tags with class/id matching `header`, `footer`, `head`, `foot`, `ad`, `ads`, or `banner`.
- **Smart HTTP Status State Machine**:
  - **2XX -> 2XX**: Compares text SHA-256 hash, `ETag`, and `Last-Modified` headers.
  - **2XX -> 4XX**: Triggers an alert when a previously working page starts returning client errors (e.g. 404 Not Found).
  - **4XX -> 4XX**: Suppresses duplicate alerts for persistent HTTP error pages.
  - **4XX -> 2XX**: Triggers an alert when an erroring page recovers and becomes accessible.
  - **3XX Redirects**: Automatically follows HTTP redirects and evaluates the final target page content.
- **Browser Bookmark Integration**: Scans SQLite databases and JSON bookmark files across system, Flatpak, and Snap installations of Firefox, Chrome, Brave, Vivaldi, Opera, Edge, LibreWolf, and more.
- **Auto-Discovery of New Browsers**: Automatically monitors newly installed browsers without needing manual re-configuration.
- **Single Grouped Notifications & Browser Action Launching**: Fires a single consolidated notification per browser for bookmark updates. Clicking the action launches the target browser command (e.g., `firefox` or `google-chrome`) opening the local Web Dashboard.
- **Embedded Web Dashboard**: Hosts a local dark-mode HTML dashboard (`http://127.0.0.1:8989/dashboard`) with relative timestamp badges and one-click AJAX exclusion controls.
- **High-Resolution Favicon Ranking**: Scores favicon candidates by parsing `sizes` attributes or numerical filename endings to pick the highest resolution icon.
- **Persistent Daemon & Single Instance**: Runs as a systemd user service (`notifier.py daemon`) with process signal handling and single-instance process termination.
- **Exclusion Management**: Exclude specific URLs, entire domain names, or full browser bookmark sets.
- **Summary Statistics**: Reports breakdown of monitored browsers, total URLs, pending updates, and exclusion counts.

## Prerequisites

Python 3.8+ is required. Native desktop notifications use PyGObject bindings (`python3-gobject`). If PyGObject is unavailable, notifications fall back to `notify-send`.

```bash
# On Fedora / RHEL / CentOS:
sudo dnf install python3-gobject

# On Debian / Ubuntu:
sudo apt install python3-gi
```

## Installation

Run the automated interactive installer subcommand:

```bash
./notifier.py install
```

The installer performs the following actions:
1. Sets executable permissions on `notifier.py`.
2. Creates a symlink at `~/.local/bin/web-notifier`.
3. Copies `web-update-notifier.service` to `~/.config/systemd/user/`, reloads systemd user daemon, enables, and starts the service.
4. Detects all installed browsers on your system and interactively asks if you wish to exclude any browser from bookmark monitoring.

### Manual Service Installation (Alternative)

If you prefer to manually configure systemd:

```bash
chmod +x notifier.py
mkdir -p ~/.local/bin ~/.config/systemd/user
ln -sf "$(pwd)/notifier.py" ~/.local/bin/web-notifier
cp web-update-notifier.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now web-update-notifier.service
```

## CLI Usage & Commands

### `install`
Performs system setup, creates binary symlinks, enables systemd user service, and interactively scans installed browsers for exclusion preferences.
```bash
./notifier.py install
```

### `add <url>`
Registers a custom URL for monitoring, extracts its page title, caches its highest-resolution favicon, and records initial content signatures.
```bash
./notifier.py add https://example.com
```

### `remove <url>`
Removes a URL from database tracking and deletes cached favicon assets.
```bash
./notifier.py remove https://example.com
```

### `mark-read <url>`
Marks a pending URL update as read and dismisses any active desktop notification.
```bash
./notifier.py mark-read https://example.com
```

### `exclude --scope [url|domain|browser] [--browser BROWSER] target`
Adds an exclusion rule to ignore specific URLs, entire domain names, or all bookmarks from a specific browser.
```bash
# Exclude a single URL:
./notifier.py exclude --scope url https://example.com/feed

# Exclude an entire domain:
./notifier.py exclude --scope domain example.com

# Exclude a browser from bookmark monitoring:
./notifier.py exclude --scope browser "Google Chrome"
```

### `list-exclusions`
Lists all active exclusion rules registered in the database.
```bash
./notifier.py list-exclusions
```

### `list [--stats|--summary]`
Lists all tracked URLs with titles, last check timestamps, and pending change statuses.
```bash
./notifier.py list
```
Use `--stats` or `--summary` to display a summary breakdown instead of the full table:
```bash
./notifier.py list --stats
```

### `stats`
Displays summary statistics showing the count of monitored browsers, total URLs, pending updates, and active exclusions.
```bash
./notifier.py stats
```
Example output:
```text
=== Web Update Notifier Statistics ===
TOTAL: 3 browsers, 334 urls, 3 pending (Exclusions: 0)
  Mozilla Firefox: 31 urls, 3 pending
  Chrome Flatpak: 299 urls, 0 pending
  Independent: 4 urls, 0 pending
```

### `check`
Executes HTTP update checks immediately across all tracked URLs and browser bookmarks, firing notifications for pending changes.
```bash
./notifier.py check
```

### `daemon`
Runs the persistent background daemon process with periodic check schedules every 4 hours and starts the local Web Dashboard HTTP server.
```bash
./notifier.py daemon
```

## Configuration & Browser Support (`browsers.csv`)

Browser paths and command definitions are stored in `browsers.csv`. Supported browsers include:
- **Mozilla Firefox** (Native, Developer Edition, Flatpak, Snap)
- **Google Chrome** (Native, Flatpak, Snap)
- **Brave Browser** (Native, Flatpak, Snap)
- **Vivaldi** (Native, Flatpak, Snap)
- **Opera** (Native, Flatpak, Snap)
- **Microsoft Edge** (Native, Flatpak)
- **LibreWolf** (Native, Flatpak)

Any browser installed after running `notifier install` is automatically discovered and monitored on subsequent check runs unless explicitly excluded.

## Data Storage & Architecture

- **SQLite Database**: Stored at `~/.config/web-update-notifier/notifier.db`.
- **Favicon Cache**: Stored at `~/.cache/web-update-notifier/favicons/`.
- **Web Dashboard**: Listens on `http://127.0.0.1:8989/dashboard` when the daemon or check process is active.

## License

This project is licensed under the BSD 3-Clause License.
