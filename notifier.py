#!/usr/bin/env python3
"""
Web Update Notifier - A lightweight script to monitor changes in web documents
and send Gnome desktop notifications with action callbacks.

Licensed under the BSD 3-Clause License.
"""

import argparse
import hashlib
import os
import sqlite3
import sys
import urllib.request
import urllib.error
import webbrowser
from html.parser import HTMLParser

# Try importing GObject Introspection for native desktop notifications
try:
    import gi
    gi.require_version('Notify', '0.7')
    from gi.repository import Notify, GLib
    HAS_PYGOBJECT = True
except (ImportError, ValueError):
    HAS_PYGOBJECT = False

# Global state for notification event loop tracking
active_notifications = {}
loop = None


class TextExtractor(HTMLParser):
    """
    Parser to extract visible text from HTML content, skipping scripts, styles,
    head tags, metadata, etc.
    """
    def __init__(self):
        super().__init__()
        self.text_parts = []
        self.ignore_stack = []
        self.ignore_tags = {'script', 'style', 'head', 'meta', 'link', 'noscript'}

    def handle_starttag(self, tag, attrs):
        tag_lower = tag.lower()
        if tag_lower in self.ignore_tags:
            self.ignore_stack.append(tag_lower)

    def handle_endtag(self, tag):
        tag_lower = tag.lower()
        if tag_lower in self.ignore_stack:
            self.ignore_stack.remove(tag_lower)

    def handle_data(self, data):
        if not self.ignore_stack:
            self.text_parts.append(data)

    def get_text(self):
        return " ".join(" ".join(self.text_parts).split())


def calculate_hash(text):
    """Calculate SHA-256 checksum of a given text string."""
    return hashlib.sha256(text.encode('utf-8', errors='replace')).hexdigest()


def get_db_path():
    """Retrieve the path to the SQLite tracking database."""
    config_dir = os.path.expanduser("~/.config/web-update-notifier")
    os.makedirs(config_dir, exist_ok=True)
    return os.path.join(config_dir, "notifier.db")


def init_db():
    """Initialize the SQLite database schema if it does not already exist."""
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
            last_viewed_hash TEXT
        )
    """)
    conn.commit()
    conn.close()


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
    """Update the checked metadata and timestamp for a given URL."""
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


def update_last_viewed(url):
    """
    Update the last viewed state to match the currently checked state,
    indicating the user has seen the update.
    """
    db_path = get_db_path()
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE urls
        SET last_viewed_at = CURRENT_TIMESTAMP,
            last_viewed_etag = last_checked_etag,
            last_viewed_modified = last_checked_modified,
            last_viewed_hash = last_checked_hash
        WHERE url = ?
    """, (url,))
    conn.commit()
    conn.close()


def fetch_page(url, etag=None, last_modified=None):
    """
    Perform an HTTP GET request for a URL.
    Supports standard conditional requests using If-None-Match and If-Modified-Since.
    """
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
            return 304, None, None
        raise e


def check_url(url_row):
    """
    Check if a tracked URL has been modified since it was last checked,
    and returns whether it represents a new change compared to what the user viewed.
    """
    (url, added_at, last_checked_at, last_viewed_at,
     checked_etag, checked_mod, checked_hash,
     viewed_etag, viewed_mod, viewed_hash) = url_row

    print(f"Comprobando {url}... ", end="", flush=True)
    try:
        status, headers, html_content = fetch_page(url, checked_etag, checked_mod)
    except Exception as e:
        print(f"ERROR: No se pudo obtener la página ({e})")
        return False, None

    if status == 304:
        print("Sin cambios (304).")
        update_checked_timestamp(url)
        return False, None

    # Process 200 OK response
    new_etag = headers.get("ETag")
    new_mod = headers.get("Last-Modified")

    try:
        parser = TextExtractor()
        parser.feed(html_content)
        cleaned_text = parser.get_text()
    except Exception:
        cleaned_text = html_content

    new_hash = calculate_hash(cleaned_text)

    # Determine if there is a change compared to last_checked
    has_changed_since_check = False
    if new_hash != checked_hash:
        has_changed_since_check = True
    elif new_etag and new_etag != checked_etag:
        has_changed_since_check = True
    elif new_mod and new_mod != checked_mod:
        has_changed_since_check = True

    if has_changed_since_check:
        print("¡MODIFICADA!")
        # Save change details as the latest check status
        update_checked_state(url, new_etag, new_mod, new_hash)

        # Determine if this change is unviewed compared to user's viewed version
        has_changed_since_view = False
        if new_hash != viewed_hash:
            has_changed_since_view = True
        elif new_etag and new_etag != viewed_etag:
            has_changed_since_view = True
        elif new_mod and new_mod != viewed_mod:
            has_changed_since_view = True

        return has_changed_since_view, url
    else:
        print("Sin cambios.")
        update_checked_timestamp(url)
        return False, None


def on_action_clicked(notification, action, url):
    """Callback triggered when the user clicks 'Abrir página' on a notification."""
    global loop
    print(f"Abriendo navegador para: {url}")
    webbrowser.open(url)
    update_last_viewed(url)
    if url in active_notifications:
        del active_notifications[url]
    if not active_notifications and loop:
        loop.quit()


def on_notification_closed(notification, url):
    """Callback triggered when a desktop notification is closed or dismissed."""
    global loop
    if url in active_notifications:
        del active_notifications[url]
    if not active_notifications and loop:
        loop.quit()


def on_timeout():
    """Timer callback to prevent hanging if notifications are ignored."""
    global loop
    print("Se alcanzó el tiempo de espera. Cerrando notificaciones...")
    for url, n in list(active_notifications.items()):
        try:
            n.close()
        except Exception:
            pass
    active_notifications.clear()
    if loop:
        loop.quit()
    return False


def show_notification(url):
    """Display a native desktop notification or fallback to notify-send."""
    global loop
    if not HAS_PYGOBJECT:
        # Fallback implementation using notify-send utility
        import subprocess
        try:
            subprocess.run([
                "notify-send",
                "Web Modificada",
                f"La página {url} ha sido modificada. Abre la lista para ver el cambio."
            ], check=True)
        except Exception as e:
            print(f"Error al enviar notificación con notify-send: {e}", file=sys.stderr)
        return

    try:
        if not Notify.is_initted():
            Notify.init("Web Update Notifier")

        n = Notify.Notification.new(
            "Página Web Modificada",
            f"La página {url} ha sido modificada.",
            "document-properties"
        )
        n.add_action("open", "Abrir página", on_action_clicked, url)
        n.connect("closed", on_notification_closed, url)
        n.show()
        active_notifications[url] = n
    except Exception as e:
        print(f"Error al mostrar notificación PyGObject: {e}", file=sys.stderr)
        # Fallback to notify-send on exception
        import subprocess
        try:
            subprocess.run([
                "notify-send",
                "Web Modificada",
                f"La página {url} ha sido modificada."
            ], check=True)
        except Exception as e2:
            print(f"Error al enviar notificación alternativa: {e2}", file=sys.stderr)


def add_url_cmd(url):
    """Command logic to add a new URL to monitoring."""
    if not (url.startswith("http://") or url.startswith("https://")):
        print("Error: La URL debe comenzar con http:// o https://")
        return

    db_path = get_db_path()
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT url FROM urls WHERE url = ?", (url,))
    if cursor.fetchone():
        print(f"La URL '{url}' ya está siendo monitoreada.")
        conn.close()
        return
    conn.close()

    print(f"Obteniendo versión inicial de {url}...")
    try:
        status, headers, html_content = fetch_page(url)
    except Exception as e:
        print(f"Error al obtener la URL: {e}")
        return

    etag = headers.get("ETag")
    modified = headers.get("Last-Modified")
    try:
        parser = TextExtractor()
        parser.feed(html_content)
        cleaned_text = parser.get_text()
    except Exception:
        cleaned_text = html_content
    content_hash = calculate_hash(cleaned_text)

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO urls (
            url, last_checked_at, last_viewed_at,
            last_checked_etag, last_checked_modified, last_checked_hash,
            last_viewed_etag, last_viewed_modified, last_viewed_hash
        ) VALUES (?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, ?, ?, ?, ?, ?, ?)
    """, (url, etag, modified, content_hash, etag, modified, content_hash))
    conn.commit()
    conn.close()
    print(f"URL '{url}' añadida correctamente.")


def remove_url_cmd(url):
    """Command logic to remove a URL from monitoring."""
    db_path = get_db_path()
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT url FROM urls WHERE url = ?", (url,))
    if not cursor.fetchone():
        print(f"Error: La URL '{url}' no está en la lista.")
        conn.close()
        return

    cursor.execute("DELETE FROM urls WHERE url = ?", (url,))
    conn.commit()
    conn.close()
    print(f"URL '{url}' eliminada de la monitorización.")


def list_urls_cmd():
    """Command logic to list all tracked URLs."""
    db_path = get_db_path()
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT url, added_at, last_checked_at, last_viewed_at,
               last_checked_hash, last_viewed_hash,
               last_checked_etag, last_viewed_etag,
               last_checked_modified, last_viewed_modified
        FROM urls
    """)
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        print("No hay URLs registradas en la lista.")
        return

    print(f"{'URL':<55} | {'Última Comprobación':<20} | {'Cambios Pendientes'}")
    print("-" * 96)
    for row in rows:
        url = row[0]
        last_checked = row[2] or "Nunca"

        has_pending = False
        if row[4] != row[5]:
            has_pending = True
        elif row[6] and row[6] != row[7]:
            has_pending = True
        elif row[8] and row[8] != row[9]:
            has_pending = True

        pending_str = "SÍ" if has_pending else "NO"

        url_disp = url
        if len(url_disp) > 55:
            url_disp = url_disp[:52] + "..."

        print(f"{url_disp:<55} | {last_checked:<20} | {pending_str}")


def check_urls_cmd():
    """Command logic to run update verification across all URLs."""
    global loop
    db_path = get_db_path()
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT url, added_at, last_checked_at, last_viewed_at,
               last_checked_etag, last_checked_modified, last_checked_hash,
               last_viewed_etag, last_viewed_modified, last_viewed_hash
        FROM urls
    """)
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        print("No hay URLs registradas para comprobar.")
        return

    modified_urls = []
    print("Comprobando actualizaciones de páginas web...")
    for row in rows:
        has_new_change, url = check_url(row)
        if has_new_change:
            modified_urls.append(url)

    if modified_urls:
        print(f"\nSe detectaron cambios en {len(modified_urls)} página(s).")
        for url in modified_urls:
            show_notification(url)

        if HAS_PYGOBJECT and active_notifications:
            print("Mostrando notificaciones de escritorio. Esperando respuesta del usuario (máximo 60s)...")
            loop = GLib.MainLoop()
            GLib.timeout_add_seconds(60, on_timeout)
            try:
                loop.run()
            except KeyboardInterrupt:
                print("\nInterrupción recibida. Saliendo...")
    else:
        print("\nNo se detectaron nuevos cambios.")


def main():
    init_db()

    parser = argparse.ArgumentParser(
        description="Web Update Notifier - Monitorea cambios en páginas web con notificaciones de escritorio."
    )
    subparsers = parser.add_subparsers(dest="command", help="Comando a ejecutar")

    # Add URL subcommand
    parser_add = subparsers.add_parser("add", help="Añade una URL para monitorear")
    parser_add.add_argument("url", help="La URL de la página web (http:// o https://)")

    # Remove URL subcommand
    parser_remove = subparsers.add_parser("remove", help="Elimina una URL de la monitorización")
    parser_remove.add_argument("url", help="La URL a eliminar")

    # List URLs subcommand
    subparsers.add_parser("list", help="Muestra la lista de URLs monitoreadas")

    # Check URLs subcommand
    subparsers.add_parser("check", help="Comprueba si las URLs monitoreadas han cambiado")

    args = parser.parse_args()

    if args.command == "add":
        add_url_cmd(args.url)
    elif args.command == "remove":
        remove_url_cmd(args.url)
    elif args.command == "list":
        list_urls_cmd()
    elif args.command == "check":
        check_urls_cmd()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
