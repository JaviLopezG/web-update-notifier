# Web Update Notifier

A lightweight, local utility to monitor a list of web URLs for modifications. When updates are detected, it triggers native Gnome/Linux desktop notifications with an action to open the modified page in the browser. It persists tracking information to avoid duplicate or redundant alerts.

## Features

- **Change Detection**: Leverages conditional HTTP GET request headers (`If-None-Match`, `If-Modified-Since`) and fall back to SHA-256 checks on stripped HTML body text.
- **Native Notifications**: Connects to the desktop notification daemon via GObject Introspection. Clickable action "Abrir página" launches the default web browser and marks the URL as viewed.
- **Timeout Management**: Integrates a 60-second GMainLoop timeout to prevent lingering background processes.
- **Systemd Integration**: Comes with pre-configured timer and service files for automatic daily checks.

## Prerequisites

The notifier depends on Python 3 and its standard libraries. Native desktop notifications utilize the Python GObject Introspection bindings:

```bash
# On Fedora/RHEL/CentOS:
sudo dnf install python3-gobject
```

If GObject Introspection is not available, the notifier automatically falls back to firing fire-and-forget notifications via the command-line utility `notify-send`.

## Installation

1. Make the script executable:
   ```bash
   chmod +x notifier.py
   ```

2. Symlink the script into a directory in your PATH (optional, e.g., `~/.local/bin/`):
   ```bash
   ln -s "$(pwd)/notifier.py" ~/.local/bin/web-notifier
   ```

3. Setup the daily check scheduler by copying the systemd unit files:
   ```bash
   mkdir -p ~/.config/systemd/user
   cp web-update-notifier.service ~/.config/systemd/user/
   cp web-update-notifier.timer ~/.config/systemd/user/
   ```

4. Reload systemd user daemon, enable, and start the timer:
   ```bash
   systemctl --user daemon-reload
   systemctl --user enable --now web-update-notifier.timer
   ```

## CLI Usage

### Add a URL to Track
Registers a URL and saves its initial signature (headers and content hash) as the baseline.
```bash
./notifier.py add https://example.com
```

### List Tracked URLs
Displays the registered URLs, their last checked time, and whether they have unread modifications.
```bash
./notifier.py list
```

### Remove a URL
Removes the URL from database tracking.
```bash
./notifier.py remove https://example.com
```

### Check for Updates Manually
Executes the HTTP check immediately, displays Gnome notifications for any updated pages, and waits for user interaction (timeout of 60 seconds).
```bash
./notifier.py check
```

## How It Works

- **Persistence**: URLs are saved inside a SQLite database at `~/.config/web-update-notifier/notifier.db`.
- **Change Detection States**:
  - `last_checked`: Refers to the URL status retrieved on the most recent check command.
  - `last_viewed`: Refers to the URL status when the user last clicked "Abrir página" (or when first added).
- **Anti-Spam logic**: A notification is only triggered if the remote content differs from the `last_viewed` state AND the `last_checked` state has transitioned. Daily checks that find the same updated state will not trigger repeated notifications.
- **HTML Sanitization**: To avoid false positives on dynamic pages (like pages containing varying timestamps, trackers, or dynamic script comments), the notifier extracts only readable text content for hash comparison.
