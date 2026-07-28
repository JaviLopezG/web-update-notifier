#!/usr/bin/env python3
"""
Web Update Notifier - A lightweight script to monitor changes in web documents,
browser bookmarks, and send persistent desktop notifications.

Licensed under the BSD 3-Clause License.
"""

import argparse
import csv
import glob
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import urllib.error
import urllib.parse
import webbrowser
from datetime import datetime, timezone
from html.parser import HTMLParser
from http.server import HTTPServer, BaseHTTPRequestHandler

# Try importing GObject Introspection for native desktop notifications
try:
    import gi
    gi.require_version('Notify', '0.7')
    from gi.repository import Notify, GLib
    HAS_PYGOBJECT = True
except (ImportError, ValueError):
    HAS_PYGOBJECT = False

# Global state for notification tracking and HTTP dashboard
active_notifications = {}
loop = None
dashboard_server = None


def close_all_notifications():
    """Close all active desktop notifications created by this process."""
    for url, n in list(active_notifications.items()):
        try:
            n.close()
        except Exception:
            pass
    active_notifications.clear()


def setup_signal_handlers():
    """Register signal handlers to gracefully clean up notifications on exit."""
    def handle_signal(signum, frame):
        close_all_notifications()
        if loop and loop.is_running():
            loop.quit()
        sys.exit(0)

    try:
        signal.signal(signal.SIGTERM, handle_signal)
        signal.signal(signal.SIGINT, handle_signal)
    except Exception:
        pass


def kill_previous_instances():
    """
    Terminate any currently running instances of notifier.py
    excluding the current process PID, clearing old active notifications.
    """
    my_pid = os.getpid()
    script_name = "notifier.py"

    pids_to_kill = []
    if not os.path.exists('/proc'):
        return

    for pid_str in os.listdir('/proc'):
        if not pid_str.isdigit():
            continue
        pid = int(pid_str)
        if pid == my_pid:
            continue

        try:
            cmdline_path = os.path.join('/proc', pid_str, 'cmdline')
            if not os.path.exists(cmdline_path):
                continue
            with open(cmdline_path, 'rb') as f:
                cmdline = f.read().decode('utf-8', errors='ignore').split('\0')

            if any(script_name in arg for arg in cmdline):
                pids_to_kill.append(pid)
        except Exception:
            continue

    if pids_to_kill:
        print(f"Terminating {len(pids_to_kill)} previous notifier.py process(es)...")
        for pid in pids_to_kill:
            try:
                os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
        time.sleep(0.3)


def launch_browser_command(command, url):
    """
    Launch a specific browser command with a target URL.
    Example: command='firefox', url='http://127.0.0.1:8989/dashboard?browser=Mozilla%20Firefox'
    """
    if not command:
        command = "xdg-open"

    try:
        cmd_parts = shlex.split(command)
        cmd_parts.append(url)
        print(f"Launching browser command: {cmd_parts}")
        subprocess.Popen(cmd_parts, start_new_session=True)
    except Exception as e:
        print(f"Error launching browser command '{command}': {e}", file=sys.stderr)
        try:
            webbrowser.open(url)
        except Exception:
            pass


VOID_TAGS = {
    'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link',
    'meta', 'param', 'source', 'track', 'wbr'
}

IGNORE_TAGS = {
    'script', 'style', 'head', 'noscript', 'header', 'footer', 'form'
}

IGNORE_KEYWORDS = {'header', 'footer', 'head', 'foot', 'ad', 'ads', 'banner'}


def score_favicon_candidate(sizes_attr, href):
    """
    Score a favicon candidate URL.
    1. Parse largest integer from sizes_attr (e.g. '180x180' -> 180).
    2. If no sizes_attr, extract the LAST sequence of digits in href filename string.
    """
    if sizes_attr:
        nums = [int(n) for n in re.findall(r'\d+', sizes_attr)]
        if nums:
            return max(nums)

    if href:
        filename = href.split('?')[0].split('#')[0]
        nums = [int(n) for n in re.findall(r'\d+', filename)]
        if nums:
            return nums[-1]

    return 0


class WebMetadataExtractor(HTMLParser):
    """
    HTML parser to extract visible body text, page title, and best resolution favicon URL.
    Filters out head, header, footer, form elements, and elements containing
    class or id values with: header, footer, head, foot, ad, ads, or banner.
    """
    def __init__(self, base_url):
        super().__init__()
        self.base_url = base_url
        self.text_parts = []
        self.ignore_stack = []
        self.in_title = False
        self.title_parts = []
        self.favicon_candidates = []

    def _should_ignore_attrs(self, attrs):
        """Check if any class or id attribute contains an ignored keyword."""
        for name, val in attrs:
            if name and name.lower() in ('class', 'id') and val:
                tokens = re.split(r'[^a-z0-9]+', val.lower())
                if any(kw in tokens for kw in IGNORE_KEYWORDS):
                    return True
        return False

    def handle_starttag(self, tag, attrs):
        tag_lower = tag.lower()

        if tag_lower == 'title':
            self.in_title = True
        elif tag_lower == 'link':
            attr_dict = {k.lower(): v for k, v in attrs if v is not None}
            rel = attr_dict.get('rel', '').lower()
            if 'icon' in rel or 'shortcut icon' in rel or 'apple-touch-icon' in rel:
                href = attr_dict.get('href')
                sizes = attr_dict.get('sizes')
                if href:
                    full_url = urllib.parse.urljoin(self.base_url, href)
                    score = score_favicon_candidate(sizes, href)
                    self.favicon_candidates.append((score, full_url))

        if tag_lower in VOID_TAGS:
            return

        should_ignore = (tag_lower in IGNORE_TAGS) or self._should_ignore_attrs(attrs)
        if should_ignore or self.ignore_stack:
            self.ignore_stack.append(tag_lower)

    def handle_endtag(self, tag):
        tag_lower = tag.lower()
        if tag_lower == 'title':
            self.in_title = False

        if tag_lower in VOID_TAGS:
            return

        if tag_lower in self.ignore_stack:
            while self.ignore_stack and self.ignore_stack[-1] != tag_lower:
                self.ignore_stack.pop()
            if self.ignore_stack:
                self.ignore_stack.pop()

    def handle_data(self, data):
        if self.in_title:
            self.title_parts.append(data)
        if not self.ignore_stack:
            self.text_parts.append(data)

    def get_text(self):
        return " ".join(" ".join(self.text_parts).split())

    def get_title(self):
        title = " ".join(" ".join(self.title_parts).split())
        return title if title else None

    def get_best_favicon_url(self):
        if not self.favicon_candidates:
            return None
        self.favicon_candidates.sort(key=lambda x: x[0], reverse=True)
        return self.favicon_candidates[0][1]


def calculate_hash(text):
    """Calculate SHA-256 checksum of a given text string."""
    return hashlib.sha256(text.encode('utf-8', errors='replace')).hexdigest()


def fetch_and_cache_favicon(url, favicon_url=None):
    """Download and cache a website favicon locally."""
    cache_dir = os.path.expanduser("~/.cache/web-update-notifier/favicons")
    os.makedirs(cache_dir, exist_ok=True)

    parsed = urllib.parse.urlparse(url)
    domain = parsed.netloc.replace(":", "_")
    target_path = os.path.join(cache_dir, f"{domain}.ico")

    candidates = []
    if favicon_url:
        candidates.append(favicon_url)
    default_favicon = urllib.parse.urljoin(url, "/favicon.ico")
    if default_favicon not in candidates:
        candidates.append(default_favicon)

    for cand in candidates:
        try:
            req = urllib.request.Request(
                cand,
                headers={"User-Agent": "Web-Update-Notifier/1.0 (Linux; Desktop Notification Utility)"}
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status == 200:
                    data = resp.read()
                    if data and len(data) > 10:
                        with open(target_path, "wb") as f:
                            f.write(data)
                        return target_path
        except Exception:
            continue

    return None


def get_db_path():
    """Retrieve the path to the SQLite tracking database."""
    config_dir = os.path.expanduser("~/.config/web-update-notifier")
    os.makedirs(config_dir, exist_ok=True)
    return os.path.join(config_dir, "notifier.db")


def init_db():
    """Initialize SQLite database schema and handle migrations."""
    db_path = get_db_path()
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS urls (
            url TEXT PRIMARY KEY,
            added_at TEXT DEFAULT CURRENT_TIMESTAMP,
            last_checked_at TEXT,
            last_viewed_at TEXT,
            last_checked_etag TEXT,
            last_checked_modified TEXT,
            last_checked_hash TEXT,
            last_viewed_etag TEXT,
            last_viewed_modified TEXT,
            last_viewed_hash TEXT,
            title TEXT,
            favicon_path TEXT,
            notification_active INTEGER DEFAULT 0
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS exclusions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scope_type TEXT NOT NULL,
            browser TEXT,
            target TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()

    # Migration for missing columns
    cursor.execute("PRAGMA table_info(urls)")
    columns = [row[1] for row in cursor.fetchall()]
    if "title" not in columns:
        cursor.execute("ALTER TABLE urls ADD COLUMN title TEXT")
    if "favicon_path" not in columns:
        cursor.execute("ALTER TABLE urls ADD COLUMN favicon_path TEXT")
    if "notification_active" not in columns:
        cursor.execute("ALTER TABLE urls ADD COLUMN notification_active INTEGER DEFAULT 0")

    conn.commit()
    conn.close()


def is_excluded(url, browser_name=None):
    """
    Check if a URL, domain, or browser is excluded in the database.
    scope_type can be 'browser', 'domain', or 'url'.
    """
    domain = urllib.parse.urlparse(url).netloc.lower() if url else None

    db_path = get_db_path()
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    if browser_name:
        cursor.execute("""
            SELECT 1 FROM exclusions
            WHERE scope_type = 'browser' AND (browser = ? OR target = ?)
        """, (browser_name, browser_name))
        if cursor.fetchone():
            conn.close()
            return True

        if domain:
            cursor.execute("""
                SELECT 1 FROM exclusions
                WHERE scope_type = 'domain' AND target = ? AND (browser = ? OR browser IS NULL OR browser = '')
            """, (domain, browser_name))
            if cursor.fetchone():
                conn.close()
                return True

        cursor.execute("""
            SELECT 1 FROM exclusions
            WHERE scope_type = 'url' AND target = ? AND (browser = ? OR browser IS NULL OR browser = '')
        """, (url, browser_name))
        if cursor.fetchone():
            conn.close()
            return True
    else:
        if domain:
            cursor.execute("SELECT 1 FROM exclusions WHERE scope_type = 'domain' AND target = ?", (domain,))
            if cursor.fetchone():
                conn.close()
                return True
        cursor.execute("SELECT 1 FROM exclusions WHERE scope_type = 'url' AND target = ?", (url,))
        if cursor.fetchone():
            conn.close()
            return True

    conn.close()
    return False


def add_exclusion(scope_type, target, browser=None):
    """Add a new exclusion rule to the database."""
    db_path = get_db_path()
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO exclusions (scope_type, browser, target)
        VALUES (?, ?, ?)
    """, (scope_type, browser, target))
    conn.commit()
    conn.close()
    print(f"Exclusion added: [{scope_type}] target='{target}' browser='{browser}'")


def remove_exclusion(scope_type, target):
    """Remove an exclusion rule from the database."""
    db_path = get_db_path()
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("""
        DELETE FROM exclusions
        WHERE scope_type = ? AND (target = ? OR browser = ?)
    """, (scope_type, target, target))
    conn.commit()
    conn.close()
    print(f"Removed exclusion: [{scope_type}] target='{target}'")


def format_relative_time(timestamp_str):
    """Format an ISO/SQLite timestamp into relative English string (e.g. '2 hours ago')."""
    if not timestamp_str:
        return "Unknown"
    try:
        dt = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc) if dt.tzinfo else datetime.now()
        diff = (now - dt).total_seconds()

        if diff < 60:
            return "Just now"
        elif diff < 3600:
            mins = int(diff // 60)
            return f"{mins} min ago" if mins > 1 else "1 min ago"
        elif diff < 86400:
            hours = int(diff // 3600)
            return f"{hours} hours ago" if hours > 1 else "1 hour ago"
        else:
            days = int(diff // 86400)
            return f"{days} days ago" if days > 1 else "1 day ago"
    except Exception:
        return timestamp_str


def get_browser_icon(browser_name):
    """Return system icon name for a browser."""
    name_lower = browser_name.lower()
    if "firefox" in name_lower:
        return "firefox"
    elif "chrome" in name_lower:
        return "google-chrome"
    elif "brave" in name_lower:
        return "brave-browser"
    elif "vivaldi" in name_lower:
        return "vivaldi"
    elif "opera" in name_lower:
        return "opera"
    elif "librewolf" in name_lower:
        return "librewolf"
    elif "edge" in name_lower:
        return "microsoft-edge"
    return "web-browser"


def load_browser_definitions():
    """Load browser definitions from browsers.csv."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    csv_path = os.path.join(script_dir, "browsers.csv")
    if not os.path.exists(csv_path):
        return []

    definitions = []
    try:
        with open(csv_path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                name = row.get("name", "").strip()
                command = row.get("command", "").strip()
                bookmarks = row.get("bookmarks", "").strip()
                if name and command and bookmarks:
                    definitions.append({
                        "name": name,
                        "command": command,
                        "bookmarks": bookmarks
                    })
    except Exception as e:
        print(f"Error reading browsers.csv: {e}", file=sys.stderr)

    return definitions


def format_firefox_date(val):
    """Format Firefox SQLite dateAdded (microseconds Unix epoch) to formatted date string."""
    if not val:
        return None
    try:
        ts = int(val) / 1000000.0
        if ts > 0:
            return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        pass
    return None


def format_chromium_date(val):
    """Format Chromium JSON date_added (microseconds Windows 1601 epoch) to formatted date string."""
    if not val:
        return None
    try:
        val_int = int(val)
        ts = (val_int / 1000000.0) - 11644473600
        if ts > 0:
            return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        pass
    return None


def scan_all_browser_bookmarks():
    """
    Scan bookmarks from browsers configured in browsers.csv.
    Returns dict: { browser_name: { 'command': command, 'items': [ (url, title, folder_path, date_added), ... ] } }
    """
    browser_defs = load_browser_definitions()
    browser_data = {}

    for b_def in browser_defs:
        b_name = b_def["name"]
        b_cmd = b_def["command"]
        b_pattern = os.path.expanduser(b_def["bookmarks"])

        bm_files = glob.glob(b_pattern)
        if not bm_files:
            continue

        if is_excluded(None, browser_name=b_name):
            continue

        entry = browser_data.setdefault(b_name, {"command": b_cmd, "items": []})
        b_items = entry["items"]

        for bm_file in bm_files:
            if bm_file.endswith("places.sqlite"):
                # Parse Firefox SQLite bookmarks
                try:
                    tmp_db = os.path.join(tempfile.gettempdir(), f"notifier_{hashlib.md5(bm_file.encode()).hexdigest()}_places.sqlite")
                    shutil.copy2(bm_file, tmp_db)
                    conn = sqlite3.connect(tmp_db)
                    cursor = conn.cursor()
                    cursor.execute("""
                        SELECT b.id, b.parent, b.title, b.type, b.fk, p.url, b.dateAdded
                        FROM moz_bookmarks b
                        LEFT JOIN moz_places p ON b.fk = p.id
                    """)
                    rows = cursor.fetchall()
                    conn.close()

                    try:
                        os.remove(tmp_db)
                    except Exception:
                        pass

                    nodes = {r[0]: {'parent': r[1], 'title': r[2], 'type': r[3], 'fk': r[4], 'url': r[5], 'date_added': format_firefox_date(r[6])} for r in rows}

                    def get_ff_path(node_id):
                        path_parts = []
                        curr = node_id
                        while curr in nodes and nodes[curr]['parent'] is not None and nodes[curr]['parent'] != 0:
                            parent_id = nodes[curr]['parent']
                            if parent_id in nodes:
                                t = nodes[parent_id]['title']
                                if t and t not in ('root________',):
                                    path_parts.insert(0, t)
                            curr = parent_id
                        return ' > '.join(path_parts) if path_parts else 'Bookmarks'

                    for nid, n in nodes.items():
                        url = n['url']
                        if url and (url.startswith("http://") or url.startswith("https://")):
                            if not is_excluded(url, browser_name=b_name):
                                fpath = get_ff_path(nid)
                                b_title = n['title'] or url
                                b_date = n['date_added']
                                b_items.append((url, b_title, fpath, b_date))
                except Exception:
                    continue

            elif "Bookmarks" in bm_file or bm_file.endswith(".json"):
                # Parse Chromium JSON bookmarks
                try:
                    with open(bm_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    roots = data.get("roots", {})
                    for root_val in roots.values():
                        if isinstance(root_val, dict):
                            for b_item in parse_chromium_node(root_val):
                                url = b_item[0]
                                if not is_excluded(url, browser_name=b_name):
                                    b_items.append(b_item)
                except Exception:
                    continue

    return browser_data


def parse_chromium_node(node, current_path=None):
    """Recursively traverse Chromium Bookmark JSON tree."""
    if current_path is None:
        current_path = []

    name = node.get("name", "")
    node_type = node.get("type", "")

    if node_type == "url":
        url = node.get("url", "")
        date_added = format_chromium_date(node.get("date_added"))
        if url.startswith("http://") or url.startswith("https://"):
            folder_str = " > ".join(current_path) if current_path else "Bookmarks"
            yield (url, name, folder_str, date_added)
    elif node_type == "folder" or "children" in node:
        new_path = current_path + [name] if name else current_path
        for child in node.get("children", []):
            yield from parse_chromium_node(child, new_path)


def update_checked_timestamp(url):
    """Update only the last_checked_at timestamp for a given URL."""
    db_path = get_db_path()
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE urls
        SET last_checked_at = CURRENT_TIMESTAMP
        WHERE url = ?
    """, (url,))
    conn.commit()
    conn.close()


def update_checked_state(url, etag, modified, content_hash):
    """Update checked metadata and timestamp for a given URL."""
    db_path = get_db_path()
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE urls
        SET last_checked_at = CURRENT_TIMESTAMP,
            last_checked_etag = ?,
            last_checked_modified = ?,
            last_checked_hash = ?
        WHERE url = ?
    """, (etag, modified, content_hash, url))
    conn.commit()
    conn.close()


def update_url_metadata(url, title, favicon_path):
    """Update page title and favicon_path for a given URL."""
    db_path = get_db_path()
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE urls
        SET title = COALESCE(?, title),
            favicon_path = COALESCE(?, favicon_path)
        WHERE url = ?
    """, (title, favicon_path, url))
    conn.commit()
    conn.close()


def update_last_viewed(url):
    """Update last viewed state to match currently checked state."""
    db_path = get_db_path()
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE urls
        SET last_viewed_at = CURRENT_TIMESTAMP,
            last_viewed_etag = last_checked_etag,
            last_viewed_modified = last_checked_modified,
            last_viewed_hash = last_checked_hash,
            notification_active = 0
        WHERE url = ?
    """, (url,))
    conn.commit()
    conn.close()


def set_notification_active(url, active):
    """Set notification_active flag in database."""
    db_path = get_db_path()
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE urls
        SET notification_active = ?
        WHERE url = ?
    """, (1 if active else 0, url))
    conn.commit()
    conn.close()


def fetch_page(url, etag=None, last_modified=None):
    """Perform HTTP GET request supporting standard conditional headers and 4xx status handling."""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Web-Update-Notifier/1.0 (Linux; Desktop Notification Utility)"
        }
    )
    if etag:
        req.add_header("If-None-Match", etag)
    if last_modified:
        req.add_header("If-Modified-Since", last_modified)

    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            status = response.status
            headers = response.info()
            body = response.read()
            try:
                charset = response.headers.get_content_charset() or 'utf-8'
                html_content = body.decode(charset, errors='replace')
            except Exception:
                html_content = body.decode('utf-8', errors='replace')
            return status, headers, html_content
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return 304, e.headers, None
        elif 400 <= e.code < 500:
            return e.code, e.headers, None
        raise e


def check_url(url_row):
    """Check if a tracked URL has been modified applying 2XX/4XX state transitions. Returns (has_unviewed_change, url, title, favicon_path)."""
    (url, added_at, last_checked_at, last_viewed_at,
     checked_etag, checked_mod, checked_hash,
     viewed_etag, viewed_mod, viewed_hash,
     title, favicon_path, notification_active) = url_row

    print(f"Checking {url}...")
    try:
        status, headers, html_content = fetch_page(url, checked_etag, checked_mod)
    except Exception as e:
        print(f"  ERROR: Could not fetch page ({e})")
        return False, url, title, favicon_path

    if status == 304:
        etag_304 = headers.get("ETag") if headers else None
        mod_304 = headers.get("Last-Modified") if headers else None
        print(f"  [Received] ETag: {etag_304} | Last-Modified: {mod_304} | Status: 304 Not Modified")
        print(f"  [Saved]    ETag: {checked_etag} | Last-Modified: {checked_mod} | Hash: {checked_hash}")
        print("  Result: No changes (304).")
        update_checked_timestamp(url)

        has_pending_view = (checked_hash != viewed_hash) or \
                           (checked_etag and checked_etag != viewed_etag) or \
                           (checked_mod and checked_mod != viewed_mod)
        return has_pending_view, url, title, favicon_path

    # Handle 4XX HTTP Client Error response
    if 400 <= status < 500:
        new_hash = f"HTTP_{status}"
        print(f"  [Received] Status: {status} Client Error")
        print(f"  [Saved]    Hash: {checked_hash}")

        prev_is_4xx = bool(checked_hash and checked_hash.startswith("HTTP_4"))

        if prev_is_4xx:
            print("  Result: No changes (persistent 4XX error).")
            update_checked_state(url, None, None, new_hash)
            return False, url, title, favicon_path
        else:
            print("  Result: MODIFIED! (Page now returning 4XX error).")
            update_checked_state(url, None, None, new_hash)
            has_changed_since_view = not bool(viewed_hash and viewed_hash.startswith("HTTP_4"))
            return has_changed_since_view, url, title, favicon_path

    # Process 200 OK response
    new_etag = headers.get("ETag")
    new_mod = headers.get("Last-Modified")

    try:
        parser = WebMetadataExtractor(url)
        parser.feed(html_content)
        cleaned_text = parser.get_text()
        extracted_title = parser.get_title()
        fav_url = parser.get_best_favicon_url()
    except Exception:
        cleaned_text = html_content
        extracted_title = None
        fav_url = None

    new_title = extracted_title if extracted_title else title
    new_favicon = fetch_and_cache_favicon(url, fav_url) or favicon_path
    if new_title != title or new_favicon != favicon_path:
        update_url_metadata(url, new_title, new_favicon)

    new_hash = calculate_hash(cleaned_text)

    print(f"  [Received] ETag: {new_etag} | Last-Modified: {new_mod} | Hash: {new_hash}")
    print(f"  [Saved]    ETag: {checked_etag} | Last-Modified: {checked_mod} | Hash: {checked_hash}")

    prev_was_4xx = bool(checked_hash and checked_hash.startswith("HTTP_4"))

    has_changed_since_check = False
    if prev_was_4xx:
        has_changed_since_check = True
    elif new_hash != checked_hash:
        has_changed_since_check = True
    elif new_etag and new_etag != checked_etag:
        has_changed_since_check = True
    elif new_mod and new_mod != checked_mod:
        has_changed_since_check = True

    if has_changed_since_check:
        print("  Result: MODIFIED!")
        update_checked_state(url, new_etag, new_mod, new_hash)
    else:
        print("  Result: No changes.")
        update_checked_timestamp(url)

    effective_hash = new_hash if has_changed_since_check else checked_hash
    effective_etag = new_etag if has_changed_since_check else checked_etag
    effective_mod = new_mod if has_changed_since_check else checked_mod

    has_changed_since_view = False
    if prev_was_4xx:
        has_changed_since_view = True
    elif effective_hash != viewed_hash:
        has_changed_since_view = True
    elif effective_etag and effective_etag != viewed_etag:
        has_changed_since_view = True
    elif effective_mod and effective_mod != viewed_mod:
        has_changed_since_view = True

    return has_changed_since_view, url, new_title, new_favicon


class BookmarkTreeNode:
    def __init__(self, name, full_path=""):
        self.name = name
        self.full_path = full_path
        self.subfolders = {}
        self.items = []


def render_folder_tree(node, browser_name, is_root=True):
    html = ""
    # Render items at this node level
    for item in node.items:
        domain = item["domain"]
        url_hash = hashlib.md5(item['url'].encode()).hexdigest()
        date_added_html = ""
        if item.get("date_added"):
            date_added_html = f'<span class="badge badge-date">📅 Added: {item["date_added"]}</span>'

        html += f"""
        <div class="card" id="card-{url_hash}" data-url="{item['url']}">
            <div class="card-header">
                <div class="card-title-group">
                    <span class="favicon-icon">🌐</span>
                    <div>
                        <a href="{item['url']}" target="_blank" rel="noopener noreferrer" class="card-title" onclick="openAndMarkRead('{item['url']}', this)" onauxclick="openAndMarkRead('{item['url']}', this)">{item['title']}</a>
                        <a href="{item['url']}" target="_blank" rel="noopener noreferrer" class="card-url" onclick="openAndMarkRead('{item['url']}', this)" onauxclick="openAndMarkRead('{item['url']}', this)">{item['url']}</a>
                    </div>
                </div>
                <div class="badge-group">
                    {date_added_html}
                    <span class="badge badge-time">{item['rel_time']}</span>
                </div>
            </div>
            <div class="card-actions">
                <button class="btn btn-primary" onclick="markReadItem('{item['url']}', this)">
                    Mark as Read
                </button>
                <button class="btn btn-danger" onclick="excludeItem('url', '{browser_name}', '{item['url']}', this)">
                    Exclude URL
                </button>
                <button class="btn btn-danger" onclick="excludeItem('domain', '{browser_name}', '{domain}', this)">
                    Exclude Domain ({domain})
                </button>
            </div>
        </div>
        """

    # Render subfolders recursively
    for sub_name, sub_node in sorted(node.subfolders.items()):
        sub_html = render_folder_tree(sub_node, browser_name, is_root=False)
        html += f"""
        <div class="folder-node" data-folder="{sub_node.full_path}">
            <div class="folder-header">
                <div class="folder-title">
                    <span class="folder-icon">📁</span>
                    <h2>{sub_node.name}</h2>
                </div>
                <button class="btn btn-primary btn-sm" onclick="markFolderRead(this)">
                    Mark Folder as Read
                </button>
            </div>
            <div class="folder-content">
                {sub_html}
            </div>
        </div>
        """

    return html


def generate_dashboard_html(browser_name):
    """Generate HTML dashboard for updated browser bookmarks."""
    db_path = get_db_path()
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT url, title, favicon_path, last_checked_at, last_viewed_at,
               last_checked_hash, last_viewed_hash,
               last_checked_etag, last_viewed_etag,
               last_checked_modified, last_viewed_modified
        FROM urls
    """)
    rows = cursor.fetchall()
    conn.close()

    pending_urls_dict = {}
    for r in rows:
        url = r[0]
        c_hash, v_hash = r[5], r[6]
        c_etag, v_etag = r[7], r[8]
        c_mod, v_mod = r[9], r[10]

        has_pending = (c_hash != v_hash) or \
                      (c_etag and c_etag != v_etag) or \
                      (c_mod and c_mod != v_mod)
        if has_pending:
            pending_urls_dict[url] = r

    browser_data = scan_all_browser_bookmarks()
    b_info = browser_data.get(browser_name, {"command": "xdg-open", "items": []})
    bookmarks = b_info["items"]

    updated_items = []
    for bm_entry in bookmarks:
        b_url = bm_entry[0]
        b_title = bm_entry[1]
        b_path = bm_entry[2]
        b_date_added = bm_entry[3] if len(bm_entry) > 3 else None

        if b_url in pending_urls_dict:
            row = pending_urls_dict[b_url]
            u_title = row[1] or b_title
            u_fav = row[2]
            u_checked = row[3]
            rel_time = format_relative_time(u_checked)
            domain = urllib.parse.urlparse(b_url).netloc
            updated_items.append({
                "url": b_url,
                "title": u_title,
                "favicon_path": u_fav,
                "bookmark_path": b_path,
                "date_added": b_date_added,
                "rel_time": rel_time,
                "domain": domain
            })

    root_node = BookmarkTreeNode("Root")
    for item in updated_items:
        path_str = item["bookmark_path"] or "Bookmarks"
        parts = [p.strip() for p in path_str.split(" > ") if p.strip()]
        curr = root_node
        curr_path_parts = []
        for part in parts:
            curr_path_parts.append(part)
            full_path = " > ".join(curr_path_parts)
            if part not in curr.subfolders:
                curr.subfolders[part] = BookmarkTreeNode(part, full_path)
            curr = curr.subfolders[part]
        curr.items.append(item)

    cards_html = render_folder_tree(root_node, browser_name)

    if not cards_html.strip():
        cards_html = """
        <div class="empty-state">
            <h3>All up to date!</h3>
            <p>No pending updated bookmarks found for this browser.</p>
        </div>
        """

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Updated Bookmarks - {browser_name}</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;600;700&display=swap" rel="stylesheet">
    <style>
        :root {{
            --bg-color: #0f172a;
            --card-bg: rgba(30, 41, 59, 0.7);
            --folder-bg: rgba(15, 23, 42, 0.5);
            --border-color: rgba(255, 255, 255, 0.1);
            --primary-accent: #38bdf8;
            --primary-btn-bg: rgba(56, 189, 248, 0.15);
            --primary-btn-border: rgba(56, 189, 248, 0.4);
            --primary-btn-hover: #0284c7;
            --danger-accent: #f87171;
            --danger-btn-bg: rgba(239, 68, 68, 0.15);
            --danger-btn-border: rgba(239, 68, 68, 0.4);
            --danger-btn-hover: #ef4444;
            --text-main: #f8fafc;
            --text-sub: #94a3b8;
        }}
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{
            font-family: 'Outfit', sans-serif;
            background: var(--bg-color);
            color: var(--text-main);
            min-height: 100vh;
            padding: 2rem;
            background-image: radial-gradient(circle at 50% 0%, #1e293b 0%, #0f172a 75%);
        }}
        .container {{ max-width: 960px; margin: 0 auto; }}
        header {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            margin-bottom: 2.5rem;
            padding-bottom: 1.5rem;
            border-bottom: 1px solid var(--border-color);
            gap: 1rem;
            flex-wrap: wrap;
        }}
        .header-title {{ display: flex; align-items: center; gap: 1rem; }}
        .header-title h1 {{ font-size: 1.75rem; font-weight: 700; }}
        .header-actions {{ display: flex; align-items: center; gap: 0.75rem; }}
        .folder-node {{
            background: var(--folder-bg);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            padding: 1.25rem;
            margin-bottom: 1.25rem;
        }}
        .folder-header {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            margin-bottom: 1rem;
            padding-bottom: 0.75rem;
            border-bottom: 1px solid rgba(255, 255, 255, 0.05);
            gap: 1rem;
        }}
        .folder-title {{ display: flex; align-items: center; gap: 0.75rem; }}
        .folder-title h2 {{ font-size: 1.15rem; font-weight: 600; color: var(--text-main); }}
        .folder-content {{
            margin-left: 0.5rem;
            padding-left: 0.75rem;
            border-left: 2px solid rgba(56, 189, 248, 0.2);
        }}
        .card {{
            background: var(--card-bg);
            backdrop-filter: blur(12px);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            padding: 1.25rem;
            margin-bottom: 1rem;
            transition: transform 0.2s ease, border-color 0.2s ease;
        }}
        .card:hover {{
            transform: translateY(-2px);
            border-color: var(--primary-accent);
        }}
        .card-header {{
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            margin-bottom: 0.75rem;
            gap: 1rem;
        }}
        .card-title-group {{ display: flex; align-items: flex-start; gap: 0.75rem; }}
        .card-title {{
            font-size: 1.1rem;
            font-weight: 600;
            color: var(--primary-accent);
            text-decoration: none;
            word-break: break-word;
        }}
        .card-title:hover {{ text-decoration: underline; }}
        .card-url {{ font-size: 0.85rem; color: var(--text-sub); margin-top: 0.25rem; word-break: break-all; }}
        .badge-group {{ display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap; }}
        .badge {{
            padding: 0.25rem 0.75rem;
            border-radius: 9999px;
            font-size: 0.8rem;
            font-weight: 600;
        }}
        .badge-time {{
            background: rgba(56, 189, 248, 0.15);
            color: var(--primary-accent);
        }}
        .badge-date {{
            background: rgba(148, 163, 184, 0.15);
            color: var(--text-sub);
            font-weight: 500;
        }}
        .card-actions {{
            display: flex;
            gap: 0.75rem;
            margin-top: 0.75rem;
            padding-top: 0.75rem;
            border-top: 1px solid rgba(255, 255, 255, 0.05);
            flex-wrap: wrap;
        }}
        .btn {{
            padding: 0.45rem 0.9rem;
            border-radius: 8px;
            font-size: 0.85rem;
            font-weight: 600;
            cursor: pointer;
            border: 1px solid transparent;
            transition: all 0.2s ease;
        }}
        .btn-sm {{
            padding: 0.3rem 0.7rem;
            font-size: 0.8rem;
        }}
        .btn-primary {{
            background: var(--primary-btn-bg);
            color: var(--primary-accent);
            border-color: var(--primary-btn-border);
        }}
        .btn-primary:hover {{
            background: var(--primary-btn-hover);
            color: #ffffff;
        }}
        .btn-danger {{
            background: var(--danger-btn-bg);
            color: var(--danger-accent);
            border-color: var(--danger-btn-border);
        }}
        .btn-danger:hover {{
            background: var(--danger-btn-hover);
            color: #ffffff;
        }}
        .empty-state {{
            text-align: center;
            padding: 4rem 2rem;
            background: var(--card-bg);
            border-radius: 12px;
            border: 1px dashed var(--border-color);
        }}
    </style>
</head>
<body>
    <div class="container">
        <header>
            <div class="header-title">
                <h1>Updated Bookmarks: {browser_name}</h1>
            </div>
            <div class="header-actions">
                <button class="btn btn-primary" onclick="markAllRead('{browser_name}', this)">
                    Mark All as Read
                </button>
                <button class="btn btn-danger" onclick="excludeBrowser('{browser_name}')">
                    Exclude All {browser_name}
                </button>
            </div>
        </header>

        <div id="cards-container">
            {cards_html}
        </div>
    </div>

    <script>
        function openAndMarkRead(url, linkEl) {{
            fetch(`/api/mark-read?url=${{encodeURIComponent(url)}}`);
            const card = linkEl.closest('.card');
            if (card) {{
                const parentFolder = card.closest('.folder-node');
                card.remove();
                cleanEmptyFolders(parentFolder);
            }}
            checkEmptyState();
        }}

        async function excludeItem(scope, browser, target, btn) {{
            if (!confirm('Are you sure you want to add this exclusion?')) return;
            const res = await fetch(`/api/exclude?scope=${{scope}}&browser=${{encodeURIComponent(browser)}}&url=${{encodeURIComponent(target)}}&domain=${{encodeURIComponent(target)}}`);
            if (res.ok) {{
                const card = btn.closest('.card');
                if (card) {{
                    const parentFolder = card.closest('.folder-node');
                    card.remove();
                    cleanEmptyFolders(parentFolder);
                }}
                checkEmptyState();
            }}
        }}

        async function excludeBrowser(browser) {{
            if (!confirm(`Are you sure you want to exclude all of ${{browser}}?`)) return;
            const res = await fetch(`/api/exclude?scope=browser&browser=${{encodeURIComponent(browser)}}`);
            if (res.ok) {{
                location.reload();
            }}
        }}

        async function markReadItem(url, btn) {{
            const res = await fetch(`/api/mark-read?url=${{encodeURIComponent(url)}}`);
            if (res.ok) {{
                const card = btn.closest('.card');
                if (card) {{
                    const parentFolder = card.closest('.folder-node');
                    card.remove();
                    cleanEmptyFolders(parentFolder);
                }}
                checkEmptyState();
            }}
        }}

        async function markFolderRead(btn) {{
            const folderNode = btn.closest('.folder-node');
            if (!folderNode) return;
            const cards = Array.from(folderNode.querySelectorAll('.card'));
            const urls = cards.map(c => c.dataset.url).filter(Boolean);
            if (urls.length === 0) return;

            if (!confirm(`Mark ${{urls.length}} page(s) in this folder as read?`)) return;

            const res = await fetch('/api/mark-read-batch', {{
                method: 'POST',
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify({{ urls: urls }})
            }});
            if (res.ok) {{
                const parentFolder = folderNode.parentElement ? folderNode.parentElement.closest('.folder-node') : null;
                folderNode.remove();
                cleanEmptyFolders(parentFolder);
                checkEmptyState();
            }}
        }}

        async function markAllRead(browser, btn) {{
            const cards = Array.from(document.querySelectorAll('.card'));
            const urls = cards.map(c => c.dataset.url).filter(Boolean);
            if (urls.length === 0) return;

            if (!confirm(`Mark all ${{urls.length}} pending page(s) as read?`)) return;

            const res = await fetch('/api/mark-read-batch', {{
                method: 'POST',
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify({{ urls: urls }})
            }});
            if (res.ok) {{
                document.getElementById('cards-container').innerHTML = `
                    <div class="empty-state">
                        <h3>All up to date!</h3>
                        <p>No pending updated bookmarks found for this browser.</p>
                    </div>
                `;
            }}
        }}

        function cleanEmptyFolders(folderNode) {{
            while (folderNode) {{
                const remainingCards = folderNode.querySelectorAll('.card');
                const remainingFolders = folderNode.querySelectorAll('.folder-node');
                if (remainingCards.length === 0 && remainingFolders.length === 0) {{
                    const parent = folderNode.parentElement ? folderNode.parentElement.closest('.folder-node') : null;
                    folderNode.remove();
                    folderNode = parent;
                }} else {{
                    break;
                }}
            }}
        }}

        function checkEmptyState() {{
            const cards = document.querySelectorAll('.card');
            if (cards.length === 0) {{
                document.getElementById('cards-container').innerHTML = `
                    <div class="empty-state">
                        <h3>All up to date!</h3>
                        <p>No pending updated bookmarks found for this browser.</p>
                    </div>
                `;
            }}
        }}
    </script>
</body>
</html>
"""
    return html


class DashboardRequestHandler(BaseHTTPRequestHandler):
    """HTTP Request Handler for Local Dashboard & API."""
    def log_message(self, format, *args):
        pass

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/api/mark-read-batch":
            length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(length) if length > 0 else b'{}'
            try:
                data = json.loads(body.decode('utf-8'))
                urls = data.get('urls', [])
                for u in urls:
                    update_last_viewed(u)
            except Exception as e:
                print(f"Error processing mark-read-batch: {e}", file=sys.stderr)

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok"}).encode("utf-8"))
            return

        self.send_error(404, "Not Found")

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        if path == "/api/exclude":
            scope = query.get("scope", [None])[0]
            browser = query.get("browser", [None])[0]
            url = query.get("url", [None])[0]
            domain = query.get("domain", [None])[0]

            target = url or domain or browser
            if scope and target:
                add_exclusion(scope, target, browser)

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok"}).encode("utf-8"))
            return

        elif path == "/api/mark-read":
            url = query.get("url", [None])[0]
            if url:
                update_last_viewed(url)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok"}).encode("utf-8"))
            return

        elif path == "/dashboard":
            browser = query.get("browser", ["Firefox"])[0]
            html = generate_dashboard_html(browser)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html.encode("utf-8"))
            return

        self.send_error(404, "Not Found")


def start_dashboard_server(port=8989):
    """Start embedded local HTTP dashboard server in background thread."""
    global dashboard_server
    if dashboard_server is not None:
        return
    try:
        server = HTTPServer(("127.0.0.1", port), DashboardRequestHandler)
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        dashboard_server = server
        print(f"Dashboard server active at http://127.0.0.1:{port}/")
    except Exception as e:
        print(f"Local web server: {e}")


def show_notification(url, title=None, favicon_path=None):
    """Display a persistent native desktop notification for a single non-bookmark URL."""
    if url in active_notifications:
        return

    display_title = title if title else url
    body = f"Updated content detected for: {url}"
    icon_spec = favicon_path if (favicon_path and os.path.exists(favicon_path)) else "document-properties"

    if not HAS_PYGOBJECT:
        try:
            subprocess.run([
                "notify-send",
                "-u", "normal",
                "-i", icon_spec,
                display_title,
                body
            ], check=True)
            set_notification_active(url, True)
        except Exception as e:
            print(f"Error notify-send: {e}", file=sys.stderr)
        return

    try:
        if not Notify.is_initted():
            Notify.init("Web Update Notifier")

        n = Notify.Notification.new(display_title, body, icon_spec)
        n.set_hint("resident", GLib.Variant.new_boolean(True))
        n.set_urgency(Notify.Urgency.NORMAL)

        def on_action_open(notification, action, target_url):
            webbrowser.open(target_url)
            update_last_viewed(target_url)
            if target_url in active_notifications:
                del active_notifications[target_url]

        def on_action_mark_read(notification, action, target_url):
            update_last_viewed(target_url)
            if target_url in active_notifications:
                try:
                    active_notifications[target_url].close()
                except Exception:
                    pass
                del active_notifications[target_url]

        def on_notification_closed(notification, target_url):
            update_last_viewed(target_url)
            if target_url in active_notifications:
                del active_notifications[target_url]

        n.add_action("open", "Open Page", on_action_open, url)
        n.add_action("mark_read", "Mark as Read", on_action_mark_read, url)
        n.connect("closed", on_notification_closed, url)
        n.show()

        active_notifications[url] = n
        set_notification_active(url, True)
    except Exception as e:
        print(f"Error showing notification: {e}", file=sys.stderr)


def show_browser_bookmark_notification(browser_name, updated_count, browser_command):
    """
    Display a single grouped notification per browser for updated bookmarks.
    Action triggers <browser_command> <dashboard_url>.
    """
    icon_name = get_browser_icon(browser_name)
    dashboard_url = f"http://127.0.0.1:8989/dashboard?browser={urllib.parse.quote(browser_name)}"

    display_title = browser_name
    body = f"Updated pages found in your bookmarks ({updated_count})."

    if not HAS_PYGOBJECT:
        try:
            subprocess.run(["notify-send", "-i", icon_name, display_title, body], check=True)
        except Exception:
            pass
        return

    try:
        if not Notify.is_initted():
            Notify.init("Web Update Notifier")

        n = Notify.Notification.new(display_title, body, icon_name)
        n.set_hint("resident", GLib.Variant.new_boolean(True))
        n.set_urgency(Notify.Urgency.NORMAL)

        def on_open_dashboard(notification, action, target_url):
            print(f"[TRACE] on_open_dashboard triggered: browser_name={browser_name!r}, action={action!r}, target_url={target_url!r}, browser_command={browser_command!r}", flush=True)
            launch_browser_command(browser_command, target_url)
            browser_key = f"browser:{browser_name}"
            if browser_key in active_notifications:
                del active_notifications[browser_key]

        def on_browser_notification_closed(notification, b_name):
            browser_key = f"browser:{b_name}"
            if browser_key in active_notifications:
                del active_notifications[browser_key]

        n.add_action("open", "Open Bookmarks", on_open_dashboard, dashboard_url)
        n.connect("closed", on_browser_notification_closed, browser_name)
        n.show()

        active_notifications[f"browser:{browser_name}"] = n
    except Exception as e:
        print(f"Error showing bookmark notification ({browser_name}): {e}", file=sys.stderr)


def install_cmd():
    """Command logic to perform system installation and interactive browser configuration."""
    print("=== Web Update Notifier Installation / Setup ===")
    script_dir = os.path.dirname(os.path.abspath(__file__))
    notifier_script = os.path.join(script_dir, "notifier.py")

    # 1. Set executable permissions
    try:
        os.chmod(notifier_script, 0o755)
        print("Set executable permissions on notifier.py.")
    except Exception as e:
        print(f"Warning setting permissions: {e}")

    # 2. Symlink in ~/.local/bin/
    bin_dir = os.path.expanduser("~/.local/bin")
    os.makedirs(bin_dir, exist_ok=True)
    symlink_path = os.path.join(bin_dir, "web-notifier")

    if os.path.lexists(symlink_path):
        try:
            os.remove(symlink_path)
        except Exception:
            pass
    try:
        os.symlink(notifier_script, symlink_path)
        print(f"Created symlink: {symlink_path} -> {notifier_script}")
    except Exception as e:
        print(f"Warning creating symlink: {e}")

    # 3. Setup systemd user service
    service_dir = os.path.expanduser("~/.config/systemd/user")
    os.makedirs(service_dir, exist_ok=True)
    source_service = os.path.join(script_dir, "web-update-notifier.service")
    target_service = os.path.join(service_dir, "web-update-notifier.service")

    if os.path.exists(source_service):
        try:
            shutil.copy2(source_service, target_service)
            print(f"Installed systemd service: {target_service}")
            subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
            subprocess.run(["systemctl", "--user", "enable", "--now", "web-update-notifier.service"], check=False)
            print("Enabled and started systemd user service (web-update-notifier.service).")
        except Exception as e:
            print(f"Warning configuring systemd service: {e}")

    # 4. Detect installed browsers and prompt for exclusions
    print("\n--- Scanning Installed Browsers ---")
    browser_defs = load_browser_definitions()
    detected_browsers = set()

    for b_def in browser_defs:
        b_name = b_def["name"]
        b_cmd = b_def["command"]
        b_pattern = os.path.expanduser(b_def["bookmarks"])

        cmd_binary = shlex.split(b_cmd)[0] if b_cmd else None
        has_binary = bool(cmd_binary and shutil.which(cmd_binary))
        has_files = bool(glob.glob(b_pattern))

        if has_binary or has_files:
            detected_browsers.add((b_name, b_cmd))

    if not detected_browsers:
        print("No supported browsers detected on system.")
    else:
        for b_name, b_cmd in sorted(detected_browsers, key=lambda x: x[0]):
            if is_excluded(None, browser_name=b_name):
                print(f"Browser '{b_name}' is currently EXCLUDED.")
                try:
                    ans = input(f"Do you want to REMOVE exclusion for {b_name}? [y/N]: ").strip().lower()
                    if ans in ('y', 'yes'):
                        remove_exclusion("browser", b_name)
                except (EOFError, KeyboardInterrupt):
                    pass
            else:
                try:
                    ans = input(f"Detected {b_name} ({b_cmd}). Exclude from bookmark monitoring? [y/N]: ").strip().lower()
                    if ans in ('y', 'yes'):
                        add_exclusion("browser", b_name, b_name)
                except (EOFError, KeyboardInterrupt):
                    pass

    print("\nInstallation and configuration complete!")


def add_url_cmd(url):
    """Command logic to add a new URL to monitoring."""
    if not (url.startswith("http://") or url.startswith("https://")):
        print("Error: URL must start with http:// or https://")
        return

    db_path = get_db_path()
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT url FROM urls WHERE url = ?", (url,))
    if cursor.fetchone():
        conn.close()
        return
    conn.close()

    print(f"Fetching baseline version for {url}...")
    try:
        status, headers, html_content = fetch_page(url)
    except Exception as e:
        print(f"Error fetching URL: {e}")
        return

    etag = headers.get("ETag") if headers else None
    modified = headers.get("Last-Modified") if headers else None

    if 400 <= status < 500:
        content_hash = f"HTTP_{status}"
        extracted_title = None
        favicon_path = None
    else:
        try:
            parser = WebMetadataExtractor(url)
            parser.feed(html_content)
            cleaned_text = parser.get_text()
            extracted_title = parser.get_title()
            fav_url = parser.get_best_favicon_url()
        except Exception:
            cleaned_text = html_content
            extracted_title = None
            fav_url = None

        content_hash = calculate_hash(cleaned_text)
        favicon_path = fetch_and_cache_favicon(url, fav_url)

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO urls (
            url, last_checked_at, last_viewed_at,
            last_checked_etag, last_checked_modified, last_checked_hash,
            last_viewed_etag, last_viewed_modified, last_viewed_hash,
            title, favicon_path, notification_active
        ) VALUES (?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, ?, ?, ?, ?, ?, ?, ?, ?, 0)
    """, (url, etag, modified, content_hash, etag, modified, content_hash, extracted_title, favicon_path))
    conn.commit()
    conn.close()
    print(f"URL '{url}' added successfully.")


def remove_url_cmd(url):
    """Command logic to remove a URL from monitoring."""
    db_path = get_db_path()
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT url, favicon_path FROM urls WHERE url = ?", (url,))
    row = cursor.fetchone()
    if not row:
        print(f"Error: URL '{url}' is not tracked.")
        conn.close()
        return

    favicon_path = row[1]
    if favicon_path and os.path.exists(favicon_path):
        try:
            os.remove(favicon_path)
        except Exception:
            pass

    cursor.execute("DELETE FROM urls WHERE url = ?", (url,))
    conn.commit()
    conn.close()
    if url in active_notifications:
        try:
            active_notifications[url].close()
        except Exception:
            pass
        del active_notifications[url]
    print(f"URL '{url}' removed from tracking.")


def mark_read_cmd(url):
    """Command logic to mark a URL as read manually."""
    db_path = get_db_path()
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT url FROM urls WHERE url = ?", (url,))
    if not cursor.fetchone():
        print(f"Error: URL '{url}' is not tracked.")
        conn.close()
        return

    conn.close()
    update_last_viewed(url)
    if url in active_notifications:
        try:
            active_notifications[url].close()
        except Exception:
            pass
        del active_notifications[url]
    print(f"URL '{url}' marked as read.")


def get_stats_summary():
    """
    Calculate summary statistics for monitored browsers, URLs, pending updates, and exclusions.
    Returns dict with total and per-browser metrics.
    """
    db_path = get_db_path()
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT url, last_checked_hash, last_viewed_hash,
               last_checked_etag, last_viewed_etag,
               last_checked_modified, last_viewed_modified
        FROM urls
    """)
    rows = cursor.fetchall()

    cursor.execute("SELECT COUNT(*) FROM exclusions")
    exclusion_count = cursor.fetchone()[0]
    conn.close()

    tracked_urls = {}
    pending_urls = set()
    for r in rows:
        url = r[0]
        c_hash, v_hash = r[1], r[2]
        c_etag, v_etag = r[3], r[4]
        c_mod, v_mod = r[5], r[6]

        has_pending = False
        if c_hash != v_hash:
            has_pending = True
        elif c_etag and c_etag != v_etag:
            has_pending = True
        elif c_mod and c_mod != v_mod:
            has_pending = True

        tracked_urls[url] = has_pending
        if has_pending:
            pending_urls.add(url)

    browser_data = scan_all_browser_bookmarks()
    all_bookmark_urls = set()

    browser_stats = {}
    for b_name, b_info in browser_data.items():
        b_items = b_info["items"]
        b_urls = {item[0] for item in b_items}
        all_bookmark_urls.update(b_urls)

        monitored = [u for u in b_urls if u in tracked_urls]
        pending = [u for u in monitored if u in pending_urls]
        browser_stats[b_name] = {
            "total": len(monitored),
            "pending": len(pending)
        }

    independent_urls = [u for u in tracked_urls if u not in all_bookmark_urls]
    independent_pending = [u for u in independent_urls if u in pending_urls]

    active_browsers_count = sum(1 for b, s in browser_stats.items() if s["total"] > 0)
    total_urls = len(tracked_urls)
    total_pending = len(pending_urls)

    return {
        "active_browsers_count": active_browsers_count,
        "total_urls": total_urls,
        "total_pending": total_pending,
        "exclusion_count": exclusion_count,
        "browser_stats": browser_stats,
        "independent_total": len(independent_urls),
        "independent_pending": len(independent_pending)
    }


def stats_cmd():
    """Command logic to display summary statistics in console."""
    stats = get_stats_summary()

    print("=== Web Update Notifier Statistics ===")
    print(f"TOTAL: {stats['active_browsers_count']} browsers, {stats['total_urls']} urls, {stats['total_pending']} pending (Exclusions: {stats['exclusion_count']})")
    for b_name, s in stats["browser_stats"].items():
        print(f"  {b_name}: {s['total']} urls, {s['pending']} pending")
    print(f"  Independent: {stats['independent_total']} urls, {stats['independent_pending']} pending")


def list_urls_cmd(show_stats=False):
    """Command logic to list all tracked URLs or summary statistics."""
    if show_stats:
        stats_cmd()
        return

    db_path = get_db_path()
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT url, added_at, last_checked_at, last_viewed_at,
               last_checked_hash, last_viewed_hash,
               last_checked_etag, last_viewed_etag,
               last_checked_modified, last_viewed_modified,
               title, favicon_path, notification_active
        FROM urls
    """)
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        print("No URLs tracked.")
        return

    print(f"{'URL':<45} | {'Title':<30} | {'Last Checked':<20} | {'Pending'}")
    print("-" * 105)
    for row in rows:
        url = row[0]
        title = row[10] or "No Title"
        last_checked = row[2] or "Never"

        has_pending = False
        if row[4] != row[5]:
            has_pending = True
        elif row[6] and row[6] != row[7]:
            has_pending = True
        elif row[8] and row[8] != row[9]:
            has_pending = True

        pending_str = "YES" if has_pending else "NO"

        url_disp = url[:42] + "..." if len(url) > 45 else url
        title_disp = title[:27] + "..." if len(title) > 30 else title

        print(f"{url_disp:<45} | {title_disp:<30} | {last_checked:<20} | {pending_str}")


def list_exclusions_cmd():
    """Command logic to list active exclusions."""
    db_path = get_db_path()
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT id, scope_type, browser, target, created_at FROM exclusions")
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        print("No exclusions registered.")
        return

    print(f"{'ID':<5} | {'Type':<10} | {'Browser':<15} | {'Target':<45}")
    print("-" * 80)
    for r in rows:
        b_name = r[2] or "All"
        print(f"{r[0]:<5} | {r[1]:<10} | {b_name:<15} | {r[3]:<45}")


def check_urls_cmd(only_pending=False):
    """Command logic to run update verification across all URLs and browser bookmarks."""
    global loop
    start_dashboard_server()

    # 1. Sync & add browser bookmarks into tracking database if not present
    browser_data = scan_all_browser_bookmarks()
    all_bookmark_urls = set()

    for b_name, b_info in browser_data.items():
        for bm_item in b_info["items"]:
            b_url = bm_item[0]
            all_bookmark_urls.add(b_url)
            db_path = get_db_path()
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            cursor.execute("SELECT url FROM urls WHERE url = ?", (b_url,))
            if not cursor.fetchone():
                conn.close()
                if not only_pending:
                    add_url_cmd(b_url)
            else:
                conn.close()

    # 2. Check tracked URLs
    db_path = get_db_path()
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT url, added_at, last_checked_at, last_viewed_at,
               last_checked_etag, last_checked_modified, last_checked_hash,
               last_viewed_etag, last_viewed_modified, last_viewed_hash,
               title, favicon_path, notification_active
        FROM urls
    """)
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        print("No URLs registered for checking.")
        return

    pending_notifications = []
    if only_pending:
        print("Filtering already pending web page updates...")
        for row in rows:
            url = row[0]
            if is_excluded(url):
                continue

            checked_etag, checked_mod, checked_hash = row[4], row[5], row[6]
            viewed_etag, viewed_mod, viewed_hash = row[7], row[8], row[9]
            title, favicon_path = row[10], row[11]

            has_pending_view = (checked_hash != viewed_hash) or \
                               (checked_etag and checked_etag != viewed_etag) or \
                               (checked_mod and checked_mod != viewed_mod)
            if has_pending_view:
                pending_notifications.append((url, title, favicon_path))
    else:
        print("Checking web page updates...")
        for row in rows:
            url = row[0]
            if is_excluded(url):
                continue

            has_unviewed_change, url, title, favicon_path = check_url(row)
            if has_unviewed_change:
                pending_notifications.append((url, title, favicon_path))

    # 3. Process notifications
    if pending_notifications:
        print(f"\nPending changes detected in {len(pending_notifications)} page(s).")
        pending_urls = {item[0] for item in pending_notifications}

        # Show single grouped notification per browser for bookmark updates
        for b_name, b_info in browser_data.items():
            b_cmd = b_info["command"]
            b_items = b_info["items"]
            b_urls = {item[0] for item in b_items}
            count = len(b_urls.intersection(pending_urls))
            if count > 0:
                show_browser_bookmark_notification(b_name, count, b_cmd)

        # Show direct notifications ONLY for non-bookmark URLs
        for url, title, favicon_path in pending_notifications:
            if url not in all_bookmark_urls:
                show_notification(url, title, favicon_path)

        if HAS_PYGOBJECT and active_notifications:
            print("Keeping active notifications. Press Ctrl+C to exit...")
            loop = GLib.MainLoop()
            try:
                loop.run()
            except KeyboardInterrupt:
                print("\nExiting...")
    else:
        print("\nNo new changes detected.")


def daemon_periodic_check():
    """Callback for GLib periodic daemon check."""
    print("\n[Daemon] Executing periodic check...")
    check_urls_cmd()
    return True


def run_daemon_cmd():
    """Run persistent background daemon process."""
    global loop
    print("Starting Web Update Notifier Daemon...")
    init_db()
    start_dashboard_server()

    # Initial check at daemon startup
    check_urls_cmd()

    # Schedule periodic check every 4 hours (14400 seconds)
    if HAS_PYGOBJECT:
        GLib.timeout_add_seconds(14400, daemon_periodic_check)
        loop = GLib.MainLoop()
        try:
            loop.run()
        except KeyboardInterrupt:
            print("\nDaemon stopped.")


def main():
    init_db()

    parser = argparse.ArgumentParser(
        description="Web Update Notifier - Monitor web document and browser bookmark changes with desktop notifications."
    )
    subparsers = parser.add_subparsers(dest="command", help="Command to execute")

    # Install subcommand
    subparsers.add_parser("install", help="Perform system installation and browser configuration")

    # Add URL subcommand
    parser_add = subparsers.add_parser("add", help="Add a URL to monitor")
    parser_add.add_argument("url", help="The web page URL (http:// or https://)")

    # Remove URL subcommand
    parser_remove = subparsers.add_parser("remove", help="Remove a URL from monitoring")
    parser_remove.add_argument("url", help="The URL to remove")

    # Mark-read subcommand
    parser_mark = subparsers.add_parser("mark-read", help="Mark a URL as read manually")
    parser_mark.add_argument("url", help="The URL to mark as read")

    # Exclude subcommand
    parser_exc = subparsers.add_parser("exclude", help="Add an exclusion rule")
    parser_exc.add_argument("--scope", choices=["url", "domain", "browser"], required=True, help="Exclusion scope")
    parser_exc.add_argument("--browser", help="Browser name (optional)")
    parser_exc.add_argument("target", help="Target (URL, domain, or browser name)")

    # List exclusions subcommand
    subparsers.add_parser("list-exclusions", help="List active exclusion rules")

    # Stats subcommand
    subparsers.add_parser("stats", help="Display summary statistics")

    # List URLs subcommand
    parser_list = subparsers.add_parser("list", help="List tracked URLs")
    parser_list.add_argument("--stats", "--summary", action="store_true", help="Display summary statistics instead of full table")

    # Check URLs subcommand
    parser_check = subparsers.add_parser("check", help="Check if tracked URLs have changed")
    parser_check.add_argument("--pending", "--only-pending", "-p", action="store_true", help="Launch notifications only for currently pending URLs without re-checking")

    # Notify subcommand
    subparsers.add_parser("notify", help="Launch desktop notifications for currently pending URLs")

    # Daemon subcommand
    subparsers.add_parser("daemon", help="Run background service daemon")

    args = parser.parse_args()

    if args.command == "install":
        install_cmd()
    elif args.command == "add":
        add_url_cmd(args.url)
    elif args.command == "remove":
        remove_url_cmd(args.url)
    elif args.command == "mark-read":
        mark_read_cmd(args.url)
    elif args.command == "exclude":
        add_exclusion(args.scope, args.target, args.browser)
    elif args.command == "list-exclusions":
        list_exclusions_cmd()
    elif args.command == "stats":
        stats_cmd()
    elif args.command == "list":
        list_urls_cmd(show_stats=args.stats)
    elif args.command == "check":
        setup_signal_handlers()
        kill_previous_instances()
        check_urls_cmd(only_pending=args.pending)
    elif args.command == "notify":
        setup_signal_handlers()
        kill_previous_instances()
        check_urls_cmd(only_pending=True)
    elif args.command == "daemon":
        setup_signal_handlers()
        kill_previous_instances()
        run_daemon_cmd()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
