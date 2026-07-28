# Web Update Notifier

A lightweight, local utility to monitor a list of web URLs for modifications. When updates are detected, it triggers native Gnome/Linux desktop notifications with an action to open the modified page in the browser. It persists tracking information to avoid duplicate or redundant alerts.

## Features

- **Change Detection**: Leverages conditional HTTP GET request headers (`If-None-Match`, `If-Modified-Since`) and falls back to SHA-256 checks on stripped HTML body text.
- **Rich Desktop Notifications**: Connects to GNOME notification daemon via GObject Introspection. Displays actual page title and cached favicon icon with persistent notification actions ("Abrir página", "Marcar como leída").
- **Persistent Lifecycle & Daemon**: Runs as a lightweight systemd user service (`notifier.py daemon`) to maintain active notifications across reboots, avoid duplicate notifications, and allow users to read or dismiss updates at any time.
- **Systemd Integration**: Pre-configured systemd user service (`web-update-notifier.service`) to run daemon mode seamlessly in the background.

## Prerequisites

The notifier depends on Python 3 and its standard libraries. Native desktop notifications utilize Python GObject Introspection bindings:

```bash
# On Fedora/RHEL/CentOS:
sudo dnf install python3-gobject
```

If GObject Introspection is not available, the notifier automatically falls back to firing notifications via `notify-send`.

## Installation

1. Make the script executable:
   ```bash
   chmod +x notifier.py
   ```

2. Symlink the script into a directory in your PATH (optional, e.g., `~/.local/bin/`):
   ```bash
   ln -s "$(pwd)/notifier.py" ~/.local/bin/web-notifier
   ```

3. Setup the background service daemon by copying the systemd unit file:
   ```bash
   mkdir -p ~/.config/systemd/user
   cp web-update-notifier.service ~/.config/systemd/user/
   ```

4. Reload systemd user daemon, enable, and start the service:
   ```bash
   systemctl --user daemon-reload
   systemctl --user enable --now web-update-notifier.service
   ```

## CLI Usage

### Add a URL to Track
Registers a URL, extracts its page title, downloads its favicon, and saves baseline signatures.
```bash
./notifier.py add https://example.com
```

### List Tracked URLs or Summary Statistics
Displays registered URLs, page titles, last check timestamps, and pending change status.
```bash
./notifier.py list
```
You can also view summary statistics per browser and overall counts:
```bash
./notifier.py stats
# or
./notifier.py list --stats
```

### Mark a URL as Read Manually
Marks an updated URL as read and clears any active notification.
```bash
./notifier.py mark-read https://example.com
```

### Remove a URL
Removes the URL from database tracking and deletes cached favicon files.
```bash
./notifier.py remove https://example.com
```

### Check for Updates Manually
Executes HTTP checks immediately and issues notifications for pending updates.
```bash
./notifier.py check
```

### Run Daemon Mode
Runs the persistent background daemon process with periodic check schedules.
```bash
./notifier.py daemon
```

## How It Works

- **Persistence**: Tracking database is stored at `~/.config/web-update-notifier/notifier.db`. Favicons are cached at `~/.cache/web-update-notifier/favicons/`.
- **Change Detection States**:
  - `last_checked`: Status retrieved during the most recent check.
  - `last_viewed`: Status when user opened or marked the update as read.
- **Anti-Spam & Duplicate Prevention**: Active notifications are tracked per URL (`notification_active`). Duplicate notifications are prevented, and pending notifications are restored across system reboots.
- **HTML Sanitization**: Extracts page titles and readable body text, stripping dynamic script and style blocks to avoid false positives.
