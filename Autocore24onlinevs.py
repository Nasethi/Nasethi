import os
import sys

APP_DIR = os.path.dirname(__file__)
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

import tkinter as tk
from tkinter import ttk
import psycopg2
import psycopg2.extras
import requests
import re
import textwrap
import unicodedata
import importlib.util
import webbrowser
import csv
import json
import threading
import logging
import subprocess
import tempfile
import configparser
import socket
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from typing import Dict, List, Any
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed

# ---------- scanner_entry_utils.py ----------
def normalize_barcode(value):
    """Normalize barcode values so case and extra whitespace do not create duplicates."""
    if value is None:
        return ""
    return re.sub(r"\s+", "", str(value).strip()).upper()


def bind_scan_submit(entry_widget, callback):
    """Bind both Enter and numeric keypad Enter to the same submit callback."""
    entry_widget.bind("<Return>", callback)
    entry_widget.bind("<KP_Enter>", callback)


def focus_scanner_entry(window, entry_widget, delay_ms=20):
    """Focus the scanner entry and select its contents after a short delay."""
    def _focus():
        if not hasattr(entry_widget, "winfo_exists") or entry_widget.winfo_exists():
            if hasattr(entry_widget, "focus_force"):
                entry_widget.focus_force()
            if hasattr(entry_widget, "select_range"):
                entry_widget.select_range(0, tk.END)
            if hasattr(entry_widget, "icursor"):
                entry_widget.icursor(tk.END)

    window.after(delay_ms, _focus)
    window.after(delay_ms + 120, _focus)

# ---------- quote_template.py ----------
def find_quote_template(search_dir=None):
    """Find a quote template HTML file in the given folder or app directory."""
    base_dir = Path(search_dir or Path(__file__).resolve().parent)
    candidates = [
        base_dir / 'quote_template.html',
        base_dir / 'quote-template.html',
        base_dir / 'wycena_template.html',
        base_dir / 'quote_template.htm',
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return None


def build_quote_html(context=None, template_path=None, **kwargs):
    """Build an HTML quote document using a template if available."""
    if kwargs:
        context = kwargs
    if context is None:
        context = {}
    if template_path:
        template_file = Path(template_path)
        if template_file.exists():
            content = template_file.read_text(encoding='utf-8')
            for key, value in context.items():
                replacement = value if key == 'items_table' else escape(str(value))
                content = content.replace(f'{{{{{key}}}}}', replacement)
            return content
    return render_quote_preview(context)


def render_quote_preview(context):
    """Fallback HTML preview that can be printed or converted to PDF."""
    title = escape(context.get('title', 'WYCENA'))
    customer_name = escape(context.get('customer_name', ''))
    phone = escape(context.get('phone', ''))
    address = escape(context.get('address', ''))
    email = escape(context.get('email', ''))
    doc_type = escape(context.get('doc_type', ''))
    delivery = escape(context.get('delivery', ''))
    extra_info = escape(context.get('extra_info', ''))
    items_table = context.get('items_table', '')
    total_price = escape(str(context.get('total_price', '0.00')))

    return f"""
    <html><head><meta charset='utf-8'><title>{title}</title>
    <style>
      body{{font-family:Arial,sans-serif;padding:24px; color:#222; background:#f8f8f8;}}
      .container{{max-width:900px;margin:0 auto;background:#fff;padding:24px;border:1px solid #ddd;}}
      table{{border-collapse:collapse;width:100%;margin-top:18px;}}
      th,td{{border:1px solid #ccc;padding:10px;text-align:left;}}
      th{{background:#f0f0f0;}}
      h1{{margin-bottom:8px;}}
      .meta p{{margin:4px 0;}}
      .footer{{margin-top:16px;padding-top:16px;border-top:1px solid #ddd;}}
    </style></head>
    <body>
      <div class="container">
        <h1>{title}</h1>
        <div class="meta">
          <p><strong>Klient:</strong> {customer_name}</p>
          <p><strong>Telefon:</strong> {phone}</p>
          <p><strong>Adres:</strong> {address}</p>
          <p><strong>Email:</strong> {email}</p>
          <p><strong>Typ dokumentu:</strong> {doc_type}</p>
          <p><strong>Typ dostawy:</strong> {delivery}</p>
          <p><strong>Uwagi:</strong> {extra_info}</p>
        </div>
        <table>
          <thead>
            <tr><th>Pozycja</th><th>Miejsce</th><th>OEM</th><th>Typ</th><th>Cena</th></tr>
          </thead>
          <tbody>{items_table}</tbody>
        </table>
        <div class="footer"><strong>SUMA BRUTTO:</strong> {total_price} zł</div>
      </div>
    </body></html>
    """

# ---------- inventory_reservations.py ----------
SPECIAL_BARCODES = {"RABAT", "CUSTOM", "RABAT_FORCED", "DOPLATA_FORCED"}


def validate_stock_adjustment(current_stock: int, delta: int) -> tuple[bool, int]:
    """Validate whether applying a stock delta is allowed."""
    current = int(current_stock or 0)
    change = int(delta or 0)
    new_stock = current + change
    if new_stock < 0:
        return False, new_stock
    return True, new_stock


def summarize_reservations(rows: List[Dict[str, Any]]) -> Dict[str, int]:
    summary = {"active": 0, "consumed": 0, "released": 0}
    for row in rows:
        status = str(row.get("status") or "").strip().lower()
        if status in summary:
            summary[status] += 1
    return summary


def should_release_reservations_for_status(status: str) -> bool:
    normalized = str(status or "").strip().upper()
    return normalized in {"ARCHIVED", "CANCELLED", "DELETED", "REJECTED", "VOID"}


def build_reservation_plan(items: List[Dict[str, Any]], stock_by_barcode: Dict[str, int]) -> Dict[str, Any]:
    """Build reservation plan for order items.

    Items that are special or have sufficient stock are allowed.
    Products without enough stock are blocked and returned in the response.
    """
    reservations = []
    blocked_items = []

    for item in items:
        barcode = str(item.get("barcode", "") or "").strip().upper()
        if not barcode or barcode in SPECIAL_BARCODES:
            continue

        stock = int(stock_by_barcode.get(barcode, 0) or 0)
        if stock <= 0:
            blocked_items.append({
                "order_item_id": item.get("order_item_id"),
                "barcode": item.get("barcode"),
                "reason": "insufficient_stock",
            })
            continue

        reservations.append({
            "order_item_id": item.get("order_item_id"),
            "barcode": barcode,
            "qty": 1,
        })

    return {
        "ok": not blocked_items,
        "reservations": reservations,
        "blocked_items": blocked_items,
    }

# ---------- reports.py ----------

def export_stock_report(conn, output_path=None):
    if output_path is None:
        output_path = f"stock_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"

    cur = conn.cursor()
    cur.execute("""
        SELECT barcode, name, stock, product_type, category, side, position
        FROM products
        WHERE barcode NOT IN ('RABAT','CUSTOM')
        ORDER BY stock ASC, name ASC
    """)
    rows = cur.fetchall()

    with open(output_path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(["barcode", "name", "stock", "product_type", "category", "side", "position"])
        for row in rows:
            writer.writerow(row)

    return output_path

# ---------- KONFIGURACJA LOGOWANIA ----------
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setLevel(logging.DEBUG)
console_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
# UTF-8 encoding dla konsoli żeby obsługiwać polskie znaki
if sys.platform == "win32":
    try:
        import io
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
        console_handler.stream = sys.stdout
    except:
        pass
logging.basicConfig(
    level=logging.DEBUG,
    handlers=[
        logging.FileHandler('autocore.log', encoding='utf-8'),
        console_handler
    ],
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logging.debug(f"Python executable: {sys.executable}")
logging.debug(f"Python sys.path: {sys.path}")

# ---------- GLOBALNE ----------
APP_NAME = "AutoCore v3.3 (PostgreSQL)"

# ---------- KONFIGURACJA POSTGRESQL ----------
CONFIG_PATH = os.path.join(os.path.dirname(__file__), 'config.ini')
config = configparser.ConfigParser()
config.read(CONFIG_PATH)
pg_config = config['PostgreSQL'] if config.has_section('PostgreSQL') else {}

PG_HOST = os.getenv('PG_HOST', pg_config.get('host', '192.168.100.183'))
PG_PORT = int(os.getenv('PG_PORT', pg_config.get('port', '5432')))
PG_DBNAME = os.getenv('PG_DBNAME', pg_config.get('dbname', 'autocore'))
PG_USER = os.getenv('PG_USER', pg_config.get('user', 'postgres'))
PG_PASSWORD = os.getenv('PG_PASSWORD', pg_config.get('password', 'admin'))

# ---------- DWA WEBHOOKI DISCORD ----------
DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/1515797318681755839/XmQOlPzheQMxAFrLvY8R06iJrwyV0imEogAQLQ2EFFPReNlh5VD70XYE9NPE8zVmGqWk"
DISCORD_WEBHOOK2_URL = "https://discord.com/api/webhooks/1515837791924125722/ffkZrC4BM6hJAWKFc_w32xExcZdFjT6_pjvOUrInP_UcKjEaAqCg0T_Wx6ZB9wHR73vu"

SELECTED_PG_HOST = PG_HOST
SELECTED_SALESPERSON_ID = None
SELECTED_SALESPERSON_TEXT = None
CURRENT_USER_ROLE = None
CURRENT_USER_ID = None
CURRENT_USER_NAME = None
RUN_MIGRATE_ON_START = True

# ---------- POMOCNICZE ----------
def clean_product_name(barcode, name):
    if not name:
        return ""
    clean = re.sub(r'(?i)eda\s*parts', '', name).strip()
    clean = re.sub(rf'(?i){re.escape(str(barcode))}', '', clean).strip()
    clean = re.sub(r'\s+', ' ', clean)
    return clean

def normalize_price(value):
    if value is None:
        return 0.0
    s = str(value).replace(' ', '').replace(',', '.')
    s = re.sub(r'[^0-9.-]', '', s)
    try:
        return float(s)
    except:
        return 0.0

def current_timestamp():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')

def stock_tag(stock_str):
    s = str(stock_str).strip()
    if s == "0":
        return "red"
    if ">" in s:
        return "green"
    try:
        num = int(re.sub(r"[^0-9]", "", s))
        if num > 5:
            return "green"
        if num > 0:
            return "yellow"
    except:
        pass
    return "red"

def get_photos_base_dir():
    cwd_photos = os.path.abspath("photos")
    try:
        if os.path.exists(cwd_photos):
            if os.access(cwd_photos, os.W_OK):
                return cwd_photos
        else:
            os.makedirs(cwd_photos, exist_ok=True)
            return cwd_photos
    except Exception:
        pass

    home_photos = os.path.join(os.path.expanduser("~"), "autocore_photos")
    try:
        os.makedirs(home_photos, exist_ok=True)
        return home_photos
    except Exception as e:
        logging.error(f"Cannot create home photos folder {home_photos}: {e}")
        return cwd_photos


def open_folder(path=None):
    if path is None:
        path = get_photos_base_dir()
    else:
        path = os.path.abspath(path)
    if not os.path.exists(path):
        show_topmost_warning("Uwaga", f"Folder nie istnieje:\n{path}")
        return
    try:
        if sys.platform == "win32":
            subprocess.Popen(["explorer", path])
        elif sys.platform == "darwin":
            subprocess.run(["open", path])
        else:
            subprocess.run(["xdg-open", path])
    except Exception as e:
            show_topmost_error("Error", f"Cannot open folder: {e}")
            logging.error(f"Error opening folder {path}: {e}")

def get_order_dict(order_id):
    cur = db.conn.cursor()
    cur.execute("SELECT * FROM orders WHERE id=%s", (order_id,))
    row = cur.fetchone()
    if not row:
        return None
    columns = [desc[0] for desc in cur.description]
    return dict(zip(columns, row))

def safe_get(order, key, default=''):
    val = order.get(key, default)
    return '' if val is None else str(val)


def find_existing_product_by_barcode(barcode, exclude_barcode=None):
    barcode = (barcode or "").strip()
    if not barcode:
        return None
    cur = db.conn.cursor()
    cur.execute("""
        SELECT barcode, name
        FROM products
        WHERE UPPER(COALESCE(barcode, '')) = UPPER(%s)
          AND (%s IS NULL OR UPPER(COALESCE(barcode, '')) != UPPER(%s))
        ORDER BY id
        LIMIT 1
    """, (barcode, exclude_barcode, exclude_barcode))
    return cur.fetchone()

# ---------- DIALOGI Z TOPMOST ----------
def safe_grab_window(win):
    try:
        win.update_idletasks()
        win.deiconify()
        win.lift()
        win.grab_set()
    except tk.TclError as e:
        logging.warning(f"grab_set failed for window {win}: {e}")
        try:
            win.lift()
            win.focus_force()
        except Exception:
            pass


def show_topmost_info(title, message, parent=None):
    win = tk.Toplevel(parent if parent else root)
    win.title(title)
    win.attributes('-topmost', True)
    win.geometry("400x150")
    tk.Label(win, text=message, wraplength=380).pack(pady=20)
    tk.Button(win, text="OK", command=win.destroy).pack()
    safe_grab_window(win)
    win.wait_window()


def show_topmost_error(title, message, parent=None):
    win = tk.Toplevel(parent if parent else root)
    win.title(title)
    win.attributes('-topmost', True)
    win.geometry("400x150")
    tk.Label(win, text=message, wraplength=380, fg="red").pack(pady=20)
    tk.Button(win, text="OK", command=win.destroy).pack()
    safe_grab_window(win)
    win.wait_window()


def show_topmost_warning(title, message, parent=None):
    win = tk.Toplevel(parent if parent else root)
    win.title(title)
    win.attributes('-topmost', True)
    win.geometry("400x150")
    tk.Label(win, text=message, wraplength=380, fg="orange").pack(pady=20)
    tk.Button(win, text="OK", command=win.destroy).pack()
    safe_grab_window(win)
    win.wait_window()


def ask_topmost_yesno(title, message, parent=None):
    result = False
    win = tk.Toplevel(parent if parent else root)
    win.title(title)
    win.attributes('-topmost', True)
    win.geometry("400x150")
    tk.Label(win, text=message, wraplength=380).pack(pady=20)
    def yes():
        nonlocal result
        result = True
        win.destroy()
    def no():
        nonlocal result
        result = False
        win.destroy()
    tk.Button(win, text="Tak", command=yes).pack(side="left", padx=20, pady=10)
    tk.Button(win, text="Nie", command=no).pack(side="right", padx=20, pady=10)
    safe_grab_window(win)
    win.wait_window()
    return result


def ask_topmost_string(title, prompt, parent=None):
    result = None
    win = tk.Toplevel(parent if parent else root)
    win.title(title)
    win.attributes('-topmost', True)
    win.geometry("400x150")
    tk.Label(win, text=prompt).pack(pady=10)
    entry = tk.Entry(win, width=40)
    entry.pack(pady=5)
    def ok():
        nonlocal result
        result = entry.get()
        win.destroy()
    tk.Button(win, text="OK", command=ok).pack(pady=10)
    safe_grab_window(win)
    win.wait_window()
    return result


def ask_topmost_number(title, prompt, parent=None, default=0, min_val=0, max_val=9999):
    result = None
    win = tk.Toplevel(parent if parent else root)
    win.title(title)
    win.attributes('-topmost', True)
    win.geometry("400x200")
    tk.Label(win, text=prompt).pack(pady=10)
    entry = tk.Entry(win, width=15)
    entry.insert(0, str(default))
    entry.pack(pady=5)
    def ok():
        nonlocal result
        try:
            val = float(entry.get().replace(',', '.'))
            if val < min_val or val > max_val:
                show_topmost_warning("Uwaga", f"Wartość musi być między {min_val} a {max_val}", parent=win)
                return
            result = val
            win.destroy()
        except:
            show_topmost_warning("Uwaga", "Podaj poprawną liczbę", parent=win)
    tk.Button(win, text="OK", command=ok).pack(pady=10)
    safe_grab_window(win)
    win.wait_window()
    return result

# ---------- EKRAN LOGOWANIA ----------
def show_login_window(parent):
    global SELECTED_PG_HOST, PG_HOST, SELECTED_SALESPERSON_ID, CURRENT_USER_ROLE, CURRENT_USER_ID, CURRENT_USER_NAME
    # Utwórz okno logowania najpierw (używane przez host UI)
    win = tk.Toplevel(parent)
    win.title("Logowanie AutoCore")
    win.geometry("480x540")
    win.attributes('-topmost', True)
    win.resizable(True, True)

    # Wybór hosta (domowy / firmowy / inny) w oknie logowania
    # Pole IP serwera (pozostawione jako główne pole do wpisu)
    ip_var = tk.StringVar(value=SELECTED_PG_HOST)
    ttk.Label(win, text="Adres IP serwera PostgreSQL:").pack(pady=(15, 5))
    ip_entry = ttk.Entry(win, textvariable=ip_var, width=35)
    ip_entry.pack(pady=5)

    # Wybór hosta (domowy / firmowy / inny) w oknie logowania (radiobuttons + opcja custom)
    host_var = tk.StringVar()
    host_frame = ttk.Frame(win)
    host_frame.pack(pady=(5,5))
    custom_host_e = ttk.Entry(host_frame, width=30)

    def _init_host_choice():
        current = (SELECTED_PG_HOST or PG_HOST) or ''
        if current == '192.168.1.12':
            host_var.set('192.168.1.12')
            ip_var.set('192.168.1.12')
            custom_host_e.config(state='disabled')
        elif current == '192.168.100.183':
            host_var.set('192.168.100.183')
            ip_var.set('192.168.100.183')
            custom_host_e.config(state='disabled')
        else:
            host_var.set('custom')
            custom_host_e.config(state='normal')
            custom_host_e.delete(0, tk.END)
            custom_host_e.insert(0, current)
            if current:
                ip_var.set(current)

    def select_host(v):
        if v == 'custom':
            custom_host_e.config(state='normal')
            ip_var.set(custom_host_e.get().strip())
        else:
            custom_host_e.delete(0, tk.END)
            custom_host_e.config(state='disabled')
            ip_var.set(v)

    rb1 = ttk.Radiobutton(host_frame, text='Domowy (192.168.1.12)', variable=host_var, value='192.168.1.12', command=lambda: select_host('192.168.1.12'))
    rb1.pack(anchor='w')
    rb2 = ttk.Radiobutton(host_frame, text='Firmowy (192.168.100.183)', variable=host_var, value='192.168.100.183', command=lambda: select_host('192.168.100.183'))
    rb2.pack(anchor='w')
    rb3 = ttk.Radiobutton(host_frame, text='Inny', variable=host_var, value='custom', command=lambda: select_host('custom'))
    rb3.pack(anchor='w')
    custom_host_e.pack(padx=5, pady=2)

    def on_custom_change(event=None):
        if host_var.get() == 'custom':
            ip_var.set(custom_host_e.get().strip())

    custom_host_e.bind('<KeyRelease>', on_custom_change)
    _init_host_choice()

    # Najpierw spróbuj automatycznego logowania po adresie IP klienta
    def try_autologin_with_host(host_to_try, local_ip):
        if not host_to_try:
            return None
        try:
            conn = psycopg2.connect(host=host_to_try, port=PG_PORT, dbname=PG_DBNAME, user=PG_USER, password=PG_PASSWORD, connect_timeout=3)
            cur = conn.cursor()
            cur.execute("SELECT salesperson_id FROM salesperson_ips WHERE ip_address=%s LIMIT 1", (local_ip.strip(),))
            row = cur.fetchone()
            if row:
                sid = row[0]
                cur.execute("SELECT name FROM salespersons WHERE id=%s", (sid,))
                srow = cur.fetchone()
                if srow:
                    return sid, srow[0]
        except Exception as e:
            logging.debug(f"[AUTOLOGIN] Host {host_to_try} autologin failed: {e}")
        finally:
            try:
                conn.close()
            except Exception:
                pass
        return None

    try:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]
            s.close()
        except Exception:
            local_ip = socket.gethostbyname(socket.gethostname())
        host_candidates = []
        if SELECTED_PG_HOST and SELECTED_PG_HOST not in host_candidates:
            host_candidates.append(SELECTED_PG_HOST)
        if PG_HOST and PG_HOST not in host_candidates:
            host_candidates.append(PG_HOST)
        for preset_host in ("192.168.1.12", "192.168.100.183"):
            if preset_host not in host_candidates:
                host_candidates.append(preset_host)

        for host in host_candidates:
            logging.info(f"[AUTOLOGIN] Checking IP mapping for {local_ip} on DB host {host}")
            result = try_autologin_with_host(host, local_ip)
            if result:
                sid, name = result
                CURRENT_USER_ROLE = "Handlowiec"
                CURRENT_USER_ID = sid
                CURRENT_USER_NAME = name
                SELECTED_SALESPERSON_ID = CURRENT_USER_ID
                SELECTED_PG_HOST = host
                PG_HOST = host
                logging.info(f"[AUTOLOGIN] Auto-logged as {CURRENT_USER_NAME} ({CURRENT_USER_ID}) for IP {local_ip} using host {host}")
                return True
    except Exception as e:
        logging.debug(f"[AUTOLOGIN] Failed to determine local IP: {e}")

    # kontynuuj budowę UI (pole IP i host selector już utworzone powyżej)
    ttk.Label(win, text="Rola użytkownika:").pack(pady=(15, 5))
    role_var = tk.StringVar(value=CURRENT_USER_ROLE or "Handlowiec")
    role_combo = ttk.Combobox(win, textvariable=role_var, values=["Handlowiec", "Magazynier"], state="readonly", width=32)
    role_combo.pack(pady=5)
    if CURRENT_USER_ROLE in ("Handlowiec", "Magazynier"):
        role_combo.current(["Handlowiec", "Magazynier"].index(CURRENT_USER_ROLE))
    else:
        role_combo.current(0)

    ttk.Label(win, text="Wybierz osobę:").pack(pady=(15, 5))
    users = []
    users_combo = ttk.Combobox(win, state="readonly", width=32)
    users_combo.pack(pady=5)

    status_label = ttk.Label(win, text="", foreground="red")
    status_label.pack(pady=(5, 0))

    login_info_var = tk.StringVar(value=f"Rola: {role_var.get()}\nUżytkownik: -")
    login_info_label = ttk.Label(win, textvariable=login_info_var, foreground="blue", justify="left")
    login_info_label.pack(pady=(5, 5))

    migrate_var = tk.BooleanVar(value=RUN_MIGRATE_ON_START)
    migrate_check = ttk.Checkbutton(win, text="Wykonaj migrację bazy danych przed wejściem do aplikacji", variable=migrate_var)
    migrate_check.pack(pady=(0, 5))

    def update_login_info():
        selected_user = users_combo.get().strip() or '-'
        login_info_var.set(f"Rola: {role_var.get()}\nUżytkownik: {selected_user}")

    def load_users():
        nonlocal users
        host = ip_var.get().strip() or SELECTED_PG_HOST
        role = role_var.get()
        table = "salespersons" if role == "Handlowiec" else "warehouse_workers"
        previous_selection = users_combo.get().strip()
        logging.info(f"[LOGIN] Loading users. Host: {host}, Role: {role}, Table: {table}")
        status_label.config(text="Ładowanie użytkowników...", foreground="blue")
        try:
            logging.debug(f"[LOGIN] Connecting to db: {host}:{PG_PORT}/{PG_DBNAME}")
            conn = psycopg2.connect(
                host=host,
                port=PG_PORT,
                dbname=PG_DBNAME,
                user=PG_USER,
                password=PG_PASSWORD
            )
            logging.info(f"[LOGIN] Connected")
            cur = conn.cursor()
            cur.execute(f"SELECT id, name FROM {table} ORDER BY name")
            users = cur.fetchall()
            conn.close()
            logging.info(f"[LOGIN] Loaded {len(users)} users")
            values = [f"{pid}: {name}" for pid, name in users]
            users_combo['values'] = values
            users_combo.set("")
            if users:
                selected_value = None
                if previous_selection and previous_selection in values:
                    selected_value = previous_selection
                elif CURRENT_USER_ROLE == role and CURRENT_USER_ID:
                    candidate = f"{CURRENT_USER_ID}: {CURRENT_USER_NAME}"
                    if candidate in values:
                        selected_value = candidate
                if selected_value:
                    users_combo.set(selected_value)
                    users_combo.current(values.index(selected_value))
                else:
                    users_combo.current(0)
                update_login_info()
                status_label.config(text="Gotowe")
                logging.debug(f"[LOGIN] User: {selected_value}")
            else:
                users_combo.set("")
                msg = f"Brak {'handlowców' if table=='salespersons' else 'magazynierów'} w bazie."
                status_label.config(text=msg, foreground="red")
                logging.warning(f"[LOGIN] {msg}")
        except Exception as e:
            users = []
            users_combo['values'] = []
            users_combo.set("")
            status_label.config(text=f"Błąd: Brak połączenia (timeout?)", foreground="red")
            logging.error(f"[LOGIN] Error loading users: {e}")

    def on_role_change(event=None):
        users_combo.set("")
        load_users()
        update_login_info()

    role_combo.bind("<<ComboboxSelected>>", on_role_change)
    users_combo.bind("<<ComboboxSelected>>", lambda e: update_login_info())

    def on_ok():
        global SELECTED_PG_HOST, PG_HOST, SELECTED_SALESPERSON_ID, CURRENT_USER_ROLE, CURRENT_USER_ID, CURRENT_USER_NAME, RUN_MIGRATE_ON_START
        ip = ip_var.get().strip()
        if not ip:
            show_topmost_error("Błąd", "Wprowadź adres IP serwera PostgreSQL", parent=win)
            return
        if not users_combo.get().strip():
            load_users()
        if not users_combo.get().strip():
            show_topmost_error("Błąd", "Wybierz osobę", parent=win)
            return
        SELECTED_PG_HOST = ip
        PG_HOST = ip
        CURRENT_USER_ROLE = role_var.get()
        selected_text = users_combo.get().strip()
        if ":" in selected_text:
            selected = selected_text.split(":", 1)[0].strip()
            CURRENT_USER_ID = int(selected)
            CURRENT_USER_NAME = selected_text.split(":", 1)[1].strip()
        else:
            CURRENT_USER_ID = None
            CURRENT_USER_NAME = selected_text
        if CURRENT_USER_ROLE == "Handlowiec":
            SELECTED_SALESPERSON_ID = CURRENT_USER_ID
        else:
            SELECTED_SALESPERSON_ID = None
        RUN_MIGRATE_ON_START = migrate_var.get()
        win.destroy()

    def on_cancel():
        if ask_topmost_yesno("Zamknij", "Czy zakończyć działanie programu?", parent=win):
            parent.destroy()
            sys.exit(0)

    btn_frame = ttk.Frame(win)
    btn_frame.pack(pady=15, fill='x')
    ttk.Button(btn_frame, text="Odśwież", command=load_users).pack(side="left", padx=10, expand=True)
    ttk.Button(btn_frame, text="Kontynuuj", command=on_ok).pack(side="left", padx=10, expand=True)
    ttk.Button(btn_frame, text="Anuluj", command=on_cancel).pack(side="left", padx=10, expand=True)

    win.protocol("WM_DELETE_WINDOW", on_cancel)
    win.grab_set()
    # Nie ładuj użytkowników automatycznie - pozwól użytkownikowi kliknąć "Odśwież"
    # load_users()  # <-- USUNIĘTE
    win.wait_window()

    return True

# ---------- POBIERANIE ZDJĘĆ ----------
def download_product_photo(barcode, photo_url):
    if not photo_url:
        return None
    try:
        if not photo_url.startswith('http'):
            photo_url = 'https://www.signeda.pl' + photo_url
        folder = os.path.join("photos", str(barcode))
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, "main.jpg")
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
        r = requests.get(photo_url, headers=headers, timeout=15)
        if r.status_code == 200:
            content_type = r.headers.get('content-type', '')
            if 'image' not in content_type.lower():
                logging.warning(f"Otrzymano niepoprawny typ zawartości dla {barcode}: {content_type}")
                return None
            with open(path, 'wb') as f:
                f.write(r.content)
            return folder
    except Exception as e:
        logging.error(f"Error fetching product page for {barcode}: {e}")
    return None

# ---------- WYSYŁKA NA DISCORDA ----------
def send_order_to_discord(order_data):
    def send():
        try:
            thread_name = f"Zlecenie #{order_data['order_id']} - {order_data['customer_name'][:70]}"
            if len(thread_name) > 100:
                thread_name = thread_name[:97] + "..."
            content = (
                f"📦 **Nowe zlecenie #{order_data['order_id']}**\n"
                f"Klient: {order_data['customer_name']}\n"
                f"Telefon: {order_data['phone'] or 'brak'}\n"
                f"Dostawa: {order_data['delivery_type']}\n"
                f"Handlowiec: {order_data['salesperson']}\n"
                f"Kwota: {order_data['total']:.2f} zł\n"
                f"**Samochód / uwagi:** {order_data['extra_info'] or 'brak'}"
            )
            products_summary = order_data['products_summary']
            if len(products_summary) > 900:
                products_summary = products_summary[:897] + "..."
                products_summary += "\n*(pełne zlecenie dostępne w aplikacji)*"
            embed_fields = [
                {"name": "Produkty", "value": products_summary, "inline": False}
            ]
            payload = {
                "content": content,
                "thread_name": thread_name,
                "embeds": [{
                    "title": "Produkty",
                    "color": 0x00ff00,
                    "fields": embed_fields,
                    "footer": {"text": "AutoCore v3.3"},
                    "timestamp": datetime.now(timezone.utc).isoformat()
                }],
                "wait": True
            }
            response = requests.post(DISCORD_WEBHOOK_URL + "?wait=true", json=payload, timeout=10)
            if response.status_code == 200:
                print(f"[Discord] Utworzono wątek dla zlecenia #{order_data['order_id']}")
            else:
                logging.error(f"[Discord] Błąd: {response.status_code} {response.text}")
                print(f"[Discord] Błąd: {response.status_code}")
        except requests.exceptions.RequestException as e:
            logging.error(f"[Discord] Błąd sieci: {e}")
            print(f"[Discord] Błąd sieci: {e}")
        except Exception as e:
            logging.error(f"[Discord] Wyjątek: {e}")
            print(f"[Discord] Wyjątek: {e}")
    threading.Thread(target=send, daemon=True).start()

def send_summary_to_discord(order_id, worker_name, packages_text, total_price, shipping_free,
                            customer_name, address, phone, delivery_type, invoice_type, cod_type, email, nip="",
                            products_text=None):
    def send():
        try:
            delivery_display = delivery_type or "brak"
            if "NIESTANDARDOWA" in delivery_display.upper():
                pkg_type = "Niestandardowa"
            elif "PACZKA" in delivery_display.upper():
                pkg_type = "Standardowa"
            else:
                pkg_type = delivery_display

            cod_display = cod_type or "brak"
            if cod_display == "z_wysylka":
                cod_text = "z wysyłką"
            elif cod_display == "plus_wysylka":
                cod_text = "plus wysyłka"
            else:
                cod_text = "brak"

            invoice_display = invoice_type or "brak"
            if invoice_type and "FAKTURA" in invoice_type.upper() and nip:
                invoice_line = f"**Fv:** {invoice_display}, **NIP:** {nip}"
            else:
                invoice_line = f"**Fv czy bez:** {invoice_display}"

            content_lines = [
                f"📦 **Zlecenie #{order_id} – gotowe do wysyłki**",
                f"{customer_name or 'brak'} | {phone or 'brak'} | {address or 'brak'}",
                f"Magazynier: {worker_name or 'nieprzypisany'} | Typ paczki: {pkg_type}",
                invoice_line,
                f"Pobranie: {cod_text}",
            ]
            if email:
                content_lines.append(f"**Email:** {email}")
            content_lines.append(f"**Wartość zamówienia:** {total_price:.2f} zł")

            if packages_text:
                content_lines.append(f"\n**Paczki:**\n{packages_text}")
            else:
                content_lines.append("\n**Brak paczek**")

            if products_text:
                content_lines.append("\n📎 **Lista produktów w załączniku**")

            content = "\n".join(content_lines)

            if products_text:
                temp = tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False, encoding='utf-8')
                temp.write(products_text)
                temp.close()
                filename = f"produkty_zamowienia_{order_id}.txt"
                files = {'file': (filename, open(temp.name, 'rb'), 'text/plain')}
                payload = {'content': content}
                response = requests.post(DISCORD_WEBHOOK2_URL, data=payload, files=files, timeout=15)
                try:
                    os.unlink(temp.name)
                except:
                    pass
            else:
                payload = {"content": content}
                response = requests.post(DISCORD_WEBHOOK2_URL, json=payload, timeout=10)

            if response.status_code == 204:
                print(f"[Discord] Podsumowanie wysłane dla zlecenia #{order_id}")
            else:
                logging.error(f"[Discord] Błąd wysyłki podsumowania: {response.status_code} {response.text}")
                print(f"[Discord] Błąd wysyłki: {response.status_code}")
        except requests.exceptions.RequestException as e:
            logging.error(f"[Discord] Błąd sieci: {e}")
            print(f"[Discord] Błąd sieci: {e}")
        except Exception as e:
            logging.error(f"[Discord] Wyjątek: {e}")
            print(f"[Discord] Wyjątek: {e}")
    threading.Thread(target=send, daemon=True).start()

def resend_order_to_discord(order_id, parent_win=None):
    cur = db.conn.cursor()
    order = get_order_dict(order_id)
    if not order:
        show_topmost_error("Błąd", f"Nie znaleziono zamówienia #{order_id}", parent=parent_win)
        return
    status = order.get('status')
    if status not in ('NEW', 'READY'):
        show_topmost_warning("Uwaga", f"Zlecenie #{order_id} ma status '{status}' – nie można wysłać ponownie.", parent=parent_win)
        return

    cur.execute("""
        SELECT oi.barcode,
               COALESCE(oi.custom_name, p.name) AS name,
               COALESCE(oi.unit_price, p.price) AS price,
               oi.side, oi.position
        FROM order_items oi
        LEFT JOIN products p ON oi.barcode = p.barcode
        WHERE oi.order_id=%s
    """, (order_id,))
    items = cur.fetchall()

    if not items:
        show_topmost_warning("Uwaga", "Brak produktów w zleceniu", parent=parent_win)
        return

    products_summary_lines = []
    for barcode, name, price, side, position in items:
        side_str = f" {side}" if side else ""
        pos_str = f" {position}" if position else ""
        clean_name = clean_product_name(barcode, name) if barcode not in ("CUSTOM", "RABAT_FORCED", "DOPLATA_FORCED") else name
        display_name = clean_name if barcode not in ("CUSTOM", "RABAT_FORCED", "DOPLATA_FORCED") else f"[{barcode}] {name}"
        products_summary_lines.append(f"- `{barcode}` {display_name}{side_str}{pos_str} – {price:.2f} zł")
    products_summary = "\n".join(products_summary_lines)

    sp_name = ""
    sp_id = order.get('salesperson_id')
    if sp_id:
        cur.execute("SELECT name FROM salespersons WHERE id=%s", (sp_id,))
        sp = cur.fetchone()
        if sp:
            sp_name = sp[0]

    if status == 'NEW':
        order_data = {
            "order_id": order_id,
            "customer_name": safe_get(order, 'customer_name'),
            "phone": safe_get(order, 'phone'),
            "delivery_type": safe_get(order, 'delivery_type'),
            "salesperson": sp_name,
            "extra_info": safe_get(order, 'extra_info'),
            "products_summary": products_summary,
            "total": order.get('total_price', 0)
        }
        send_order_to_discord(order_data)
        show_topmost_info("Wysyłka", f"Zlecenie #{order_id} wysłane ponownie na Discord (NEW).", parent=parent_win)
    elif status == 'READY':
        worker_id = order.get('warehouse_worker_id')
        worker_name = ""
        if worker_id:
            cur.execute("SELECT name FROM warehouse_workers WHERE id=%s", (worker_id,))
            w = cur.fetchone()
            if w:
                worker_name = w[0]

        cur.execute("SELECT weight, length, width, height FROM packages WHERE order_id=%s", (order_id,))
        packages = cur.fetchall()
        packages_text = ""
        if packages:
            pkg_lines = []
            for i, (weight, length, width, height) in enumerate(packages, 1):
                dims = []
                if length: dims.append(f"{length}cm")
                if width: dims.append(f"{width}cm")
                if height: dims.append(f"{height}cm")
                dims_str = " x ".join(dims) if dims else "brak wymiarów"
                weight_str = f"{weight}kg" if weight else "brak wagi"
                pkg_lines.append(f"{i}) {dims_str}, {weight_str}")
            packages_text = "\n".join(pkg_lines)

        total_price = order.get('total_price', 0)
        shipping_free = order.get('shipping_free', 0) == 1
        customer_name = safe_get(order, 'customer_name')
        address = safe_get(order, 'address')
        phone = safe_get(order, 'phone')
        delivery_type = safe_get(order, 'delivery_type')
        invoice_type = safe_get(order, 'document_type')
        cod_type = safe_get(order, 'cod_type')
        email = safe_get(order, 'email')
        nip = safe_get(order, 'nip')
        products_text = "\n".join(products_summary_lines)
        send_summary_to_discord(order_id, worker_name, packages_text, total_price,
                                shipping_free, customer_name, address, phone, delivery_type,
                                invoice_type, cod_type, email, nip, products_text)
        show_topmost_info("Wysyłka", f"Podsumowanie zlecenia #{order_id} wysłane ponownie na Discord (READY) z załącznikiem.", parent=parent_win)

# ---------- SCRAPER SIGNEDA ----------
class SignedaScraper:
    HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36", "X-Requested-With": "XMLHttpRequest"}
    SEARCH_URL = "https://www.signeda.pl/index.php?route=product/search&search={}&nlog=true"
    PRICE_URL = "https://www.signeda.pl/index.php?route=product/category/prices_redesign"
    CARS_URL = "https://www.signeda.pl/index.php?route=product/product/getCars"

    def search_product(self, code):
        try:
            r = requests.get(self.SEARCH_URL.format(code), headers=self.HEADERS, timeout=20)
            r.raise_for_status()
            match = re.search(r"product-card-(\d+)", r.text)
            if not match:
                return None
            product_id = match.group(1)
            soup = BeautifulSoup(r.text, "html.parser")
            name = ""
            link = ""
            name_el = soup.select_one("a.product-cards-holder__product-card__product-details__product-name")
            if not name_el:
                name_el = soup.select_one(".product-name")
            if not name_el:
                name_el = soup.select_one("h3")
            if name_el:
                name = name_el.get_text(" ", strip=True)
                link = name_el.get("href", "")
            desc_el = soup.select_one(".product-cards-holder__product-card__product-details__product-description")
            description = desc_el.get_text(" ", strip=True).replace(",", "\n") if desc_el else ""
            oe_code = ""
            side = ""
            position = ""
            product_type = ""
            for row in soup.select("tr"):
                th = row.find("th")
                td = row.find("td")
                if not th or not td:
                    continue
                header = th.get_text(" ", strip=True)
                value = td.get_text(" ", strip=True)
                if "Kod Oryginalu" in header:
                    oe_code = value
                elif "Miejsce w Pojeździe" in header:
                    if value.lower() in ["lewa", "prawa"]:
                        side = value
                    if value.lower() in ["przod", "tyl"]:
                        position = value
                elif "Typ produktu" in header:
                    product_type = value
            price, stock = self._get_price_stock(product_id, code)
            models = self._get_models(product_id)
            photo_url = None
            if link:
                try:
                    prod_r = requests.get(link, headers=self.HEADERS, timeout=15)
                    prod_r.raise_for_status()
                    prod_soup = BeautifulSoup(prod_r.text, "html.parser")
                    img_el = prod_soup.select_one("img.zoom-image")
                    if img_el:
                        photo_url = img_el.get("data-zoom-image") or img_el.get("src")
                except requests.exceptions.RequestException as e:
                    logging.warning(f"Error fetching product page for {code}: {e}")
            if not photo_url:
                img_el = soup.select_one(".product-cards-holder__product-card__product-image__image-link img")
                if img_el:
                    photo_url = img_el.get("src")
            if photo_url and not photo_url.startswith("http"):
                photo_url = "https://www.signeda.pl" + photo_url

            return {
                "external_id": product_id,
                "name": name,
                "description": description,
                "link": link,
                "oe_code": oe_code,
                "side": side,
                "position": position,
                "product_type": product_type,
                "price": price,
                "stock": stock,
                "models": models,
                "photo_url": photo_url
            }
        except requests.exceptions.RequestException as e:
            logging.error(f"Network error in search_product for {code}: {e}")
            raise
        except Exception as e:
            logging.error(f"Error in search_product for {code}: {e}")
            raise

    def _get_price_stock(self, product_id, code):
        try:
            data = {"route": "product/category/prices_redesign", "products": product_id, "search": code}
            r = requests.post(self.PRICE_URL, headers=self.HEADERS, data=data, timeout=20)
            r.raise_for_status()
            js = r.json()
            html = js["prices_html"][product_id]
            soup = BeautifulSoup(html, "html.parser")
            price = ""
            regular = soup.select_one(".price-other span")
            if regular:
                price = regular.get_text(strip=True)
            else:
                promo = soup.select_one(".price")
                if promo:
                    price = promo.get_text(strip=True)
            stock = "0"
            qty = soup.select_one(".quantity span")
            if qty:
                stock = qty.get_text(strip=True)
            return normalize_price(price), stock
        except requests.exceptions.RequestException as e:
            logging.error(f"Błąd sieci w _get_price_stock dla {product_id}: {e}")
            return 0.0, "0"
        except Exception as e:
            logging.error(f"Błąd w _get_price_stock dla {product_id}: {e}")
            return 0.0, "0"

    def _get_models(self, product_id):
        try:
            data = {"route": "product/product/getCars", "product_id": product_id}
            r = requests.post(self.CARS_URL, headers=self.HEADERS, data=data, timeout=20)
            r.raise_for_status()
            html = r.json().get("html", "")
            soup = BeautifulSoup(html, "html.parser")
            models = [li.get_text(" ", strip=True) for li in soup.select("li") if li.get_text(" ", strip=True)]
            return "\n".join(models)
        except requests.exceptions.RequestException as e:
            logging.error(f"Błąd sieci w _get_models dla {product_id}: {e}")
            return ""
        except Exception as e:
            logging.error(f"Błąd w _get_models dla {product_id}: {e}")
            return ""

# ---------- BAZA DANYCH (POSTGRESQL) ----------
class Database:
    def __init__(self, splash=None, status_label=None, progress_bar=None, progress_var=None, detail_label=None):
        self.conn = None
        self.splash_widgets = (splash, status_label, progress_bar, progress_var, detail_label)
        logging.info("[DB] Starting Database initialization")

        if splash:
            update_splash(splash, status_label, progress_bar, progress_var, detail_label,
                         "Connecting to database...", 15, "")
        self.connect()
        logging.info("[DB] Connected, creating tables...")

        if splash:
            update_splash(splash, status_label, progress_bar, progress_var, detail_label,
                         "Creating tables...", 35, "")
        self.create_tables()

        if RUN_MIGRATE_ON_START:
            logging.info("[DB] Tables created, migrating...")
            if splash:
                update_splash(splash, status_label, progress_bar, progress_var, detail_label,
                             "Migrating database...", 55, "")
            migration_ok = self.migrate_tables()
            if migration_ok:
                logging.info("[DB] Migration done, initializing special products...")
            else:
                logging.warning("[DB] Migration skipped due to lock/error, continuing startup")
                if splash:
                    update_splash(splash, status_label, progress_bar, progress_var, detail_label,
                                 "Migracja pominięta z powodu blokady lub błędu", 55, "")
        else:
            logging.info("[DB] Skipping database migration on startup")
            if splash:
                update_splash(splash, status_label, progress_bar, progress_var, detail_label,
                             "Pominięto migrację bazy danych", 55, "")

        if splash:
            update_splash(splash, status_label, progress_bar, progress_var, detail_label,
                         "Initializing special products...", 75, "")
        self.ensure_special_products()
        logging.info("[DB] Database initialization completed successfully")

    def connect(self):
        try:
            logging.info(f"[DB] Starting PostgreSQL connection")
            logging.debug(f"[DB] Connecting to: {PG_HOST}:{PG_PORT}/{PG_DBNAME}")
            self.conn = psycopg2.connect(
                host=PG_HOST,
                port=PG_PORT,
                dbname=PG_DBNAME,
                user=PG_USER,
                password=PG_PASSWORD,
                connect_timeout=30
            )
            self.conn.autocommit = False
            logging.info(f"[DB] Connected successfully")
        except psycopg2.Error as e:
            logging.error(f"[DB] PostgreSQL connection error: {e}")
            raise

    def ensure_connection(self):
        try:
            if self.conn is None or self.conn.closed:
                self.connect()
        except Exception:
            self.connect()

    def create_tables(self):
        self.ensure_connection()
        logging.debug("[DB] Creating tables...")
        cur = self.conn.cursor()
        cur.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id SERIAL PRIMARY KEY,
            barcode TEXT UNIQUE,
            product_code TEXT,
            oe_code TEXT,
            name TEXT,
            models TEXT,
            category TEXT,
            product_type TEXT,
            side TEXT,
            position TEXT,
            description TEXT,
            price REAL DEFAULT 0,
            stock INTEGER DEFAULT 0,
            signeda_stock TEXT DEFAULT '0',
            condition_rating INTEGER DEFAULT 0,
            damage_description TEXT,
            source TEXT DEFAULT 'manual',
            external_id TEXT,
            last_sync TIMESTAMP,
            force_price INTEGER DEFAULT 0,
            custom_price REAL DEFAULT 0,
            olx_offer_id TEXT,
            photos_folder TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS archived_products (
            id SERIAL PRIMARY KEY,
            barcode TEXT UNIQUE,
            product_code TEXT,
            oe_code TEXT,
            name TEXT,
            models TEXT,
            category TEXT,
            product_type TEXT,
            side TEXT,
            position TEXT,
            description TEXT,
            price REAL DEFAULT 0,
            stock INTEGER DEFAULT 0,
            signeda_stock TEXT DEFAULT '0',
            condition_rating INTEGER DEFAULT 0,
            damage_description TEXT,
            source TEXT DEFAULT 'manual',
            external_id TEXT,
            last_sync TIMESTAMP,
            force_price INTEGER DEFAULT 0,
            custom_price REAL DEFAULT 0,
            olx_offer_id TEXT,
            photos_folder TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            archived_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id SERIAL PRIMARY KEY,
            customer_name TEXT,
            phone TEXT,
            address TEXT,
            nip TEXT,
            document_type TEXT,
            delivery_type TEXT,
            status TEXT,
            package_weight REAL,
            package_length REAL,
            package_width REAL,
            package_height REAL,
            shipping_free INTEGER DEFAULT 0,
            discount REAL DEFAULT 0,
            total_price REAL DEFAULT 0,
            salesperson_id INTEGER,
            warehouse_worker_id INTEGER,
            extra_info TEXT,
            email TEXT,
            cod_type TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS packages (
            id SERIAL PRIMARY KEY,
            order_id INTEGER NOT NULL,
            type TEXT DEFAULT 'PACZKA',
            weight REAL,
            length REAL,
            width REAL,
            height REAL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS order_items (
            id SERIAL PRIMARY KEY,
            order_id INTEGER,
            barcode TEXT,
            product_name TEXT,
            side TEXT,
            position TEXT,
            picked INTEGER DEFAULT 0,
            to_order INTEGER DEFAULT 0,
            unit_price REAL DEFAULT NULL,
            custom_name TEXT DEFAULT NULL
        )
        """)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS salespersons (
            id SERIAL PRIMARY KEY,
            name TEXT UNIQUE NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS warehouse_workers (
            id SERIAL PRIMARY KEY,
            name TEXT UNIQUE NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS salesperson_ips (
            id SERIAL PRIMARY KEY,
            salesperson_id INTEGER NOT NULL,
            ip_address TEXT NOT NULL,
            note TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS customers (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            phone TEXT,
            address TEXT,
            nip TEXT,
            email TEXT,
            notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS order_templates (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            model TEXT,
            description TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS order_template_items (
            id SERIAL PRIMARY KEY,
            template_id INTEGER NOT NULL,
            barcode TEXT NOT NULL,
            quantity INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS inventory_reservations (
            id SERIAL PRIMARY KEY,
            order_id INTEGER NOT NULL,
            order_item_id INTEGER,
            product_id INTEGER NOT NULL,
            barcode TEXT NOT NULL,
            qty INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            consumed_at TIMESTAMP,
            released_at TIMESTAMP,
            created_by INTEGER,
            note TEXT
        )
        """)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS inventory_movements (
            id SERIAL PRIMARY KEY,
            product_id INTEGER NOT NULL,
            barcode TEXT NOT NULL,
            delta INTEGER NOT NULL,
            reason TEXT NOT NULL,
            order_id INTEGER,
            order_item_id INTEGER,
            reservation_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            created_by INTEGER,
            details JSONB
        )
        """)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS audit_log (
            id SERIAL PRIMARY KEY,
            entity_type TEXT NOT NULL,
            entity_id INTEGER,
            action TEXT NOT NULL,
            actor_id INTEGER,
            details JSONB,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS favorite_products (
            id SERIAL PRIMARY KEY,
            barcode TEXT NOT NULL UNIQUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            note TEXT
        )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_inventory_reservations_order ON inventory_reservations(order_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_inventory_reservations_product_status ON inventory_reservations(product_id, status)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_inventory_movements_product_time ON inventory_movements(product_id, created_at)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_audit_log_entity_time ON audit_log(entity_type, entity_id, created_at)")
        self.conn.commit()

    def migrate_tables(self):
        self.ensure_connection()
        logging.debug("[DB] Migrating tables...")
        cur = self.conn.cursor()
        try:
            cur.execute("SET LOCAL lock_timeout = '2000ms'")
            columns_to_add = [
                ('products', 'force_price', 'INTEGER DEFAULT 0'),
                ('products', 'custom_price', 'REAL DEFAULT 0'),
                ('products', 'olx_offer_id', 'TEXT'),
                ('products', 'source', "TEXT DEFAULT 'manual'"),
                ('products', 'external_id', 'TEXT'),
                ('products', 'last_sync', 'TIMESTAMP'),
                ('products', 'photos_folder', 'TEXT'),
                ('archived_products', 'force_price', 'INTEGER DEFAULT 0'),
                ('archived_products', 'custom_price', 'REAL DEFAULT 0'),
                ('archived_products', 'olx_offer_id', 'TEXT'),
                ('archived_products', 'source', "TEXT DEFAULT 'manual'"),
                ('archived_products', 'external_id', 'TEXT'),
                ('archived_products', 'last_sync', 'TIMESTAMP'),
                ('archived_products', 'photos_folder', 'TEXT'),
                ('orders', 'salesperson_id', 'INTEGER'),
                ('orders', 'warehouse_worker_id', 'INTEGER'),
                ('orders', 'email', 'TEXT'),
                ('orders', 'cod_type', 'TEXT'),
                ('orders', 'extra_info', 'TEXT'),
                ('order_items', 'side', 'TEXT'),
                ('order_items', 'position', 'TEXT'),
                ('order_items', 'unit_price', 'REAL DEFAULT NULL'),
                ('order_items', 'custom_name', 'TEXT DEFAULT NULL'),
                ('packages', 'type', 'TEXT DEFAULT \'PACZKA\'')
            ]
            for table, col, col_type in columns_to_add:
                try:
                    cur.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {col_type}")
                except psycopg2.Error as e:
                    logging.debug(f"Migracja {table}.{col}: {e}")
            self.conn.commit()
            logging.debug("[DB] Tables migrated successfully")
            return True
        except Exception as e:
            self.conn.rollback()
            logging.warning(f"[DB] Migration skipped due to lock/error: {e}")
            return False

    def log_audit_event(self, entity_type, entity_id, action, details=None, actor_id=None):
        self.ensure_connection()
        cur = self.conn.cursor()
        cur.execute("""
            INSERT INTO audit_log(entity_type, entity_id, action, actor_id, details)
            VALUES (%s, %s, %s, %s, %s)
        """, (entity_type, entity_id, action, actor_id, json.dumps(details) if details is not None else None))

    def reserve_order_items(self, order_id, order_items, actor_id=None):
        self.ensure_connection()
        if not order_items:
            return []

        stock_lookup = {}
        cur = self.conn.cursor()
        for item in order_items:
            barcode = str(item.get("barcode", "") or "").strip()
            if not barcode or item.get("to_order"):
                continue
            normalized = barcode.upper()
            if normalized in {"RABAT", "CUSTOM", "RABAT_FORCED", "DOPLATA_FORCED"}:
                continue
            if normalized not in stock_lookup:
                cur.execute("SELECT id, stock FROM products WHERE UPPER(barcode)=UPPER(%s)", (barcode,))
                row = cur.fetchone()
                if row:
                    stock_lookup[normalized] = {"product_id": row[0], "stock": row[1] or 0}
                else:
                    stock_lookup[normalized] = {"product_id": None, "stock": 0}

        plan = build_reservation_plan(order_items, {k: v["stock"] for k, v in stock_lookup.items()})
        if not plan["ok"]:
            raise ValueError("Brak dostępności dla części pozycji")

        reservations = []
        for reservation in plan["reservations"]:
            barcode = str(reservation.get("barcode", "") or "").strip()
            normalized = barcode.upper()
            product_info = stock_lookup.get(normalized, {})
            product_id = product_info.get("product_id")
            if not product_id:
                raise ValueError(f"Produkt {barcode} nie istnieje w bazie")
            cur.execute("UPDATE products SET stock=stock-1 WHERE id=%s", (product_id,))
            cur.execute("""
                INSERT INTO inventory_reservations(order_id, order_item_id, product_id, barcode, qty, status, created_by)
                VALUES (%s, %s, %s, %s, %s, 'active', %s) RETURNING id
            """, (order_id, reservation.get("order_item_id"), product_id, barcode, reservation.get("qty", 1), actor_id))
            reservation_id = cur.fetchone()[0]
            cur.execute("""
                INSERT INTO inventory_movements(product_id, barcode, delta, reason, order_id, order_item_id, reservation_id, created_by, details)
                VALUES (%s, %s, -1, 'reserve', %s, %s, %s, %s, %s)
            """, (product_id, barcode, order_id, reservation.get("order_item_id"), reservation_id, actor_id, json.dumps({"source": "order_creation"})))
            self.log_audit_event("reservation", reservation_id, "created", {"order_id": order_id, "barcode": barcode, "qty": reservation.get("qty", 1)}, actor_id)
            reservations.append(reservation_id)
        return reservations

    def consume_order_item_reservation(self, order_item_id, actor_id=None):
        self.ensure_connection()
        cur = self.conn.cursor()
        cur.execute("SELECT id, product_id, barcode FROM inventory_reservations WHERE order_item_id=%s AND status='active' ORDER BY id LIMIT 1", (order_item_id,))
        reservation = cur.fetchone()
        if not reservation:
            return False
        reservation_id, product_id, barcode = reservation
        cur.execute("UPDATE inventory_reservations SET status='consumed', consumed_at=NOW() WHERE id=%s", (reservation_id,))
        self.log_audit_event("reservation", reservation_id, "consumed", {"order_item_id": order_item_id, "barcode": barcode}, actor_id)
        return True

    def release_order_reservations(self, order_id, actor_id=None):
        self.ensure_connection()
        cur = self.conn.cursor()
        cur.execute("SELECT id, product_id, barcode, qty FROM inventory_reservations WHERE order_id=%s AND status='active'", (order_id,))
        reservations = cur.fetchall()
        for reservation_id, product_id, barcode, qty in reservations:
            cur.execute("UPDATE products SET stock=stock+%s WHERE id=%s", (qty, product_id))
            cur.execute("UPDATE inventory_reservations SET status='released', released_at=NOW() WHERE id=%s", (reservation_id,))
            cur.execute("""
                INSERT INTO inventory_movements(product_id, barcode, delta, reason, order_id, reservation_id, created_by, details)
                VALUES (%s, %s, %s, 'release', %s, %s, %s, %s)
            """, (product_id, barcode, qty, order_id, reservation_id, actor_id, json.dumps({"source": "order_cancelled"})))
            self.log_audit_event("reservation", reservation_id, "released", {"order_id": order_id, "barcode": barcode, "qty": qty}, actor_id)
        return len(reservations)

    def release_order_reservations_if_needed(self, order_id, new_status, actor_id=None):
        if should_release_reservations_for_status(new_status):
            return self.release_order_reservations(order_id, actor_id=actor_id)
        return 0

    def ensure_special_products(self):
        self.ensure_connection()
        logging.debug("[DB] Initializing special products...")
        cur = self.conn.cursor()
        specials = [
            ("RABAT", "Rabat", 0.0, "inne", "Rabat naliczony na zamówieniu"),
            ("CUSTOM", "Usługa / inne", 0.0, "inne", "Pozycja niestandardowa")
        ]
        for barcode, name, price, ptype, desc in specials:
            cur.execute("SELECT id FROM products WHERE barcode=%s", (barcode,))
            exists = cur.fetchone()
            if not exists:
                cur.execute("""
                    INSERT INTO products (barcode, name, price, product_type, description, source)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """, (barcode, name, price, ptype, desc, "special"))
        self.conn.commit()
        logging.debug("[DB] Produkty specjalne zainicjalizowane")

    def archive_product(self, barcode):
        self.ensure_connection()
        cur = self.conn.cursor()
        cur.execute("SELECT * FROM products WHERE barcode=%s", (barcode,))
        row = cur.fetchone()
        if not row:
            return False
        cur.execute("""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name='products'
            ORDER BY ordinal_position
        """)
        cols = [c[0] for c in cur.fetchall() if c[0] != 'id']
        placeholders = ','.join(['%s'] * len(cols))
        col_names = ','.join(cols)
        values = [row[i] for i in range(1, len(row))]
        cur.execute(f"INSERT INTO archived_products ({col_names}) VALUES ({placeholders})", values)
        cur.execute("DELETE FROM products WHERE barcode=%s", (barcode,))
        self.conn.commit()
        return True

    def restore_product(self, barcode):
        self.ensure_connection()
        cur = self.conn.cursor()
        cur.execute("SELECT * FROM archived_products WHERE barcode=%s", (barcode,))
        row = cur.fetchone()
        if not row:
            return False
        cur.execute("""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name='archived_products'
            ORDER BY ordinal_position
        """)
        all_cols = [c[0] for c in cur.fetchall()]
        cols = [c for c in all_cols if c not in ('id', 'archived_date')]
        placeholders = ','.join(['%s'] * len(cols))
        col_names = ','.join(cols)
        values = []
        for i, c in enumerate(all_cols):
            if c not in ('id', 'archived_date'):
                values.append(row[i])
        cur.execute(f"INSERT INTO products ({col_names}) VALUES ({placeholders})", values)
        cur.execute("DELETE FROM archived_products WHERE barcode=%s", (barcode,))
        self.conn.commit()
        return True

    def archive_sold_unique_product(self, barcode, actor_id=None):
        self.ensure_connection()
        cur = self.conn.cursor()
        cur.execute("SELECT id, stock, product_type FROM products WHERE UPPER(barcode)=UPPER(%s)", (barcode,))
        row = cur.fetchone()
        if not row:
            return False
        product_id, stock, product_type = row
        if str(product_type or "").strip() != "używane unikat":
            return False
        if int(stock or 0) > 0:
            return False
        self.archive_product(barcode)
        self.log_audit_event("product", product_id, "archived_due_to_zero_stock", {"barcode": barcode}, actor_id)
        self.conn.commit()
        return True

    def get_archived_products(self):
        self.ensure_connection()
        cur = self.conn.cursor()
        cur.execute("SELECT barcode, name, product_type, price, archived_date FROM archived_products ORDER BY archived_date DESC")
        return cur.fetchall()

    def delete_archived_product_permanently(self, barcode):
        self.ensure_connection()
        cur = self.conn.cursor()
        cur.execute("DELETE FROM archived_products WHERE barcode=%s", (barcode,))
        self.conn.commit()

    def get_categories(self):
        self.ensure_connection()
        cur = self.conn.cursor()
        cur.execute("SELECT DISTINCT category FROM products WHERE category IS NOT NULL AND category!='' AND category NOT IN ('RABAT','CUSTOM')")
        rows = cur.fetchall()
        return [r[0] for r in rows]

    def get_all_models(self):
        self.ensure_connection()
        cur = self.conn.cursor()
        cur.execute("SELECT models FROM products WHERE models IS NOT NULL AND models!=''")
        rows = cur.fetchall()
        models = set()
        for row in rows:
            if row[0]:
                for line in row[0].split("\n"):
                    line = line.strip()
                    if line:
                        models.add(line)
        return sorted(models)

    def get_stats(self):
        self.ensure_connection()
        cur = self.conn.cursor()
        cur.execute("SELECT COUNT(*) FROM products WHERE barcode NOT IN ('RABAT','CUSTOM')")
        product_count = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM orders WHERE status='NEW'")
        active_orders = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM products WHERE product_type='używane wielokrotne' AND stock<=2 AND stock>0 AND barcode NOT IN ('RABAT','CUSTOM')")
        low_stock = cur.fetchone()[0]
        return product_count, active_orders, low_stock

    def get_salespersons(self):
        self.ensure_connection()
        cur = self.conn.cursor()
        cur.execute("SELECT id, name FROM salespersons ORDER BY name")
        return cur.fetchall()

    def get_salesperson_name(self, pid):
        self.ensure_connection()
        cur = self.conn.cursor()
        cur.execute("SELECT name FROM salespersons WHERE id=%s", (pid,))
        row = cur.fetchone()
        return row[0] if row else None

    def get_salesperson_id_by_ip(self, ip_address):
        self.ensure_connection()
        cur = self.conn.cursor()
        cur.execute("SELECT salesperson_id FROM salesperson_ips WHERE ip_address=%s LIMIT 1", (ip_address,))
        row = cur.fetchone()
        return row[0] if row else None

    def list_ip_mappings(self):
        self.ensure_connection()
        cur = self.conn.cursor()
        cur.execute("SELECT id, salesperson_id, ip_address, note FROM salesperson_ips ORDER BY id")
        return cur.fetchall()

    def add_ip_mapping(self, salesperson_id, ip_address, note=None):
        try:
            self.ensure_connection()
            cur = self.conn.cursor()
            cur.execute("INSERT INTO salesperson_ips (salesperson_id, ip_address, note) VALUES (%s, %s, %s)", (salesperson_id, ip_address, note))
            self.conn.commit()
            return True
        except Exception as e:
            logging.error(f"Error adding IP mapping: {e}")
            self.conn.rollback()
            return False

    def delete_ip_mapping(self, mapping_id):
        self.ensure_connection()
        cur = self.conn.cursor()
        cur.execute("DELETE FROM salesperson_ips WHERE id=%s", (mapping_id,))
        self.conn.commit()

    def get_warehouse_workers(self):
        self.ensure_connection()
        cur = self.conn.cursor()
        cur.execute("SELECT id, name FROM warehouse_workers ORDER BY name")
        return cur.fetchall()

    def add_salesperson(self, name):
        try:
            self.ensure_connection()
            cur = self.conn.cursor()
            cur.execute("INSERT INTO salespersons (name) VALUES (%s)", (name,))
            self.conn.commit()
            return True
        except psycopg2.Error:
            return False

    def add_warehouse_worker(self, name):
        try:
            self.ensure_connection()
            cur = self.conn.cursor()
            cur.execute("INSERT INTO warehouse_workers (name) VALUES (%s)", (name,))
            self.conn.commit()
            return True
        except psycopg2.Error:
            return False

    def delete_salesperson(self, pid):
        self.ensure_connection()
        cur = self.conn.cursor()
        cur.execute("DELETE FROM salespersons WHERE id=%s", (pid,))
        self.conn.commit()

    def delete_warehouse_worker(self, pid):
        self.ensure_connection()
        cur = self.conn.cursor()
        cur.execute("DELETE FROM warehouse_workers WHERE id=%s", (pid,))
        self.conn.commit()

    def get_customers(self):
        self.ensure_connection()
        cur = self.conn.cursor()
        cur.execute("SELECT id, name, phone, address, nip, email, notes FROM customers ORDER BY name")
        return cur.fetchall()

    def get_customer(self, customer_id):
        self.ensure_connection()
        cur = self.conn.cursor()
        cur.execute("SELECT id, name, phone, address, nip, email, notes FROM customers WHERE id=%s", (customer_id,))
        return cur.fetchone()

    def add_customer(self, name, phone, address, nip, email, notes):
        try:
            self.ensure_connection()
            cur = self.conn.cursor()
            cur.execute(
                "INSERT INTO customers (name, phone, address, nip, email, notes) VALUES (%s, %s, %s, %s, %s, %s)",
                (name, phone, address, nip, email, notes)
            )
            self.conn.commit()
            return True
        except psycopg2.Error as e:
            logging.error(f"Error adding customer: {e}")
            self.conn.rollback()
            return False

    def update_customer(self, customer_id, name, phone, address, nip, email, notes):
        self.ensure_connection()
        cur = self.conn.cursor()
        cur.execute(
            "UPDATE customers SET name=%s, phone=%s, address=%s, nip=%s, email=%s, notes=%s WHERE id=%s",
            (name, phone, address, nip, email, notes, customer_id)
        )
        self.conn.commit()

    def delete_customer(self, customer_id):
        self.ensure_connection()
        cur = self.conn.cursor()
        cur.execute("DELETE FROM customers WHERE id=%s", (customer_id,))
        self.conn.commit()

    def get_order_templates(self, model_filter=None):
        self.ensure_connection()
        cur = self.conn.cursor()
        if model_filter:
            cur.execute("SELECT id, name, model, description FROM order_templates WHERE model=%s ORDER BY name", (model_filter,))
        else:
            cur.execute("SELECT id, name, model, description FROM order_templates ORDER BY name")
        return cur.fetchall()

    def get_template_items(self, template_id):
        self.ensure_connection()
        cur = self.conn.cursor()
        cur.execute("SELECT barcode, quantity FROM order_template_items WHERE template_id=%s ORDER BY id", (template_id,))
        return cur.fetchall()

    def add_order_template(self, name, model, description):
        self.ensure_connection()
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO order_templates (name, model, description) VALUES (%s, %s, %s) RETURNING id",
            (name, model, description)
        )
        template_id = cur.fetchone()[0]
        self.conn.commit()
        return template_id

    def delete_template_items(self, template_id):
        self.ensure_connection()
        cur = self.conn.cursor()
        cur.execute("DELETE FROM order_template_items WHERE template_id=%s", (template_id,))
        self.conn.commit()

    def add_template_item(self, template_id, barcode, quantity=1):
        self.ensure_connection()
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO order_template_items (template_id, barcode, quantity) VALUES (%s, %s, %s)",
            (template_id, barcode, quantity)
        )
        self.conn.commit()

    def delete_order_template(self, template_id):
        self.ensure_connection()
        cur = self.conn.cursor()
        self.delete_template_items(template_id)
        cur.execute("DELETE FROM order_templates WHERE id=%s", (template_id,))
        self.conn.commit()

    def get_templates_for_model(self, model):
        self.ensure_connection()
        cur = self.conn.cursor()
        cur.execute("SELECT id, name, model, description FROM order_templates WHERE model=%s ORDER BY name", (model,))
        return cur.fetchall()

    def get_all_template_models(self):
        self.ensure_connection()
        cur = self.conn.cursor()
        cur.execute("SELECT DISTINCT model FROM order_templates WHERE model IS NOT NULL AND model!='' ORDER BY model")
        return [r[0] for r in cur.fetchall()]

    def get_template_by_id(self, template_id):
        self.ensure_connection()
        cur = self.conn.cursor()
        cur.execute("SELECT id, name, model, description FROM order_templates WHERE id=%s", (template_id,))
        return cur.fetchone()

    def delete_template_item(self, item_id):
        self.ensure_connection()
        cur = self.conn.cursor()
        cur.execute("DELETE FROM order_template_items WHERE id=%s", (item_id,))
        self.conn.commit()

    def get_template_item_id(self, template_id, barcode):
        self.ensure_connection()
        cur = self.conn.cursor()
        cur.execute("SELECT id FROM order_template_items WHERE template_id=%s AND barcode=%s", (template_id, barcode))
        return cur.fetchone()

    def template_item_exists(self, template_id, barcode):
        return self.get_template_item_id(template_id, barcode) is not None

    def get_templates_by_model(self, model):
        return self.get_templates_for_model(model)

    def get_templates(self, model=None):
        return self.get_order_templates(model)

    def get_template_item_count(self, template_id):
        self.ensure_connection()
        cur = self.conn.cursor()
        cur.execute("SELECT COUNT(*) FROM order_template_items WHERE template_id=%s", (template_id,))
        return cur.fetchone()[0]

    def clear_template_items(self, template_id):
        self.delete_template_items(template_id)

    def template_has_items(self, template_id):
        self.ensure_connection()
        cur = self.conn.cursor()
        cur.execute("SELECT 1 FROM order_template_items WHERE template_id=%s LIMIT 1", (template_id,))
        return cur.fetchone() is not None

    def list_template_items(self, template_id):
        return self.get_template_items(template_id)

    def get_customer_by_name(self, name):
        self.ensure_connection()
        cur = self.conn.cursor()
        cur.execute("SELECT id, name, phone, address, nip, email, notes FROM customers WHERE name=%s", (name,))
        return cur.fetchone()

    def get_customer_names(self):
        self.ensure_connection()
        cur = self.conn.cursor()
        cur.execute("SELECT id, name FROM customers ORDER BY name")
        return cur.fetchall()

    def get_order_template_names(self):
        self.ensure_connection()
        cur = self.conn.cursor()
        cur.execute("SELECT id, name FROM order_templates ORDER BY name")
        return cur.fetchall()

    def add_or_update_customer(self, name, phone, address, nip, email, notes):
        existing = self.get_customer_by_name(name)
        if existing:
            self.update_customer(existing[0], name, phone, address, nip, email, notes)
            return existing[0]
        return self.add_customer(name, phone, address, nip, email, notes)

    def get_templates_by_name(self, name):
        self.ensure_connection()
        cur = self.conn.cursor()
        cur.execute("SELECT id, name, model, description FROM order_templates WHERE name=%s", (name,))
        return cur.fetchall()

    def rename_template(self, template_id, name, model, description):
        self.ensure_connection()
        cur = self.conn.cursor()
        cur.execute("UPDATE order_templates SET name=%s, model=%s, description=%s WHERE id=%s", (name, model, description, template_id))
        self.conn.commit()

    def get_template_item_row(self, item_id):
        self.ensure_connection()
        cur = self.conn.cursor()
        cur.execute("SELECT id, template_id, barcode, quantity FROM order_template_items WHERE id=%s", (item_id,))
        return cur.fetchone()

    def update_template_item_quantity(self, item_id, quantity):
        self.ensure_connection()
        cur = self.conn.cursor()
        cur.execute("UPDATE order_template_items SET quantity=%s WHERE id=%s", (quantity, item_id))
        self.conn.commit()

    def remove_template_item(self, item_id):
        self.delete_template_item(item_id)

    def add_template_items(self, template_id, items):
        for barcode, quantity in items:
            self.add_template_item(template_id, barcode, quantity)

    def delete_templates_without_items(self):
        self.ensure_connection()
        cur = self.conn.cursor()
        cur.execute("DELETE FROM order_templates WHERE id NOT IN (SELECT DISTINCT template_id FROM order_template_items)")
        self.conn.commit()

    def get_templates_for_model_prefix(self, prefix):
        self.ensure_connection()
        cur = self.conn.cursor()
        cur.execute("SELECT id, name, model, description FROM order_templates WHERE model ILIKE %s ORDER BY name", (prefix + '%',))
        return cur.fetchall()

    def get_templates_by_search(self, search):
        self.ensure_connection()
        cur = self.conn.cursor()
        like = f"%{search}%"
        cur.execute("SELECT id, name, model, description FROM order_templates WHERE name ILIKE %s OR model ILIKE %s ORDER BY name", (like, like))
        return cur.fetchall()

    def get_templates_with_model(self, model):
        return self.get_templates_for_model(model)

    def get_templates_by_model_or_all(self, model=None):
        if model:
            return self.get_templates_for_model(model)
        return self.get_order_templates()

    def get_customer_list(self):
        return self.get_customer_names()

    def get_customer_by_phone(self, phone):
        self.ensure_connection()
        cur = self.conn.cursor()
        cur.execute("SELECT id, name, phone, address, nip, email, notes FROM customers WHERE phone=%s", (phone,))
        return cur.fetchone()

    def get_customer_by_email(self, email):
        self.ensure_connection()
        cur = self.conn.cursor()
        cur.execute("SELECT id, name, phone, address, nip, email, notes FROM customers WHERE email=%s", (email,))
        return cur.fetchone()

    def get_order_template_by_name(self, name):
        self.ensure_connection()
        cur = self.conn.cursor()
        cur.execute("SELECT id, name, model, description FROM order_templates WHERE name=%s", (name,))
        return cur.fetchone()

    def get_template_item_ids(self, template_id):
        self.ensure_connection()
        cur = self.conn.cursor()
        cur.execute("SELECT id FROM order_template_items WHERE template_id=%s ORDER BY id", (template_id,))
        return [r[0] for r in cur.fetchall()]

    def find_templates(self, query):
        return self.get_templates_by_search(query)

    def get_customer_summary(self, customer_id):
        self.ensure_connection()
        cur = self.conn.cursor()
        cur.execute("SELECT name, phone, address, nip, email, notes FROM customers WHERE id=%s", (customer_id,))
        return cur.fetchone()
        self.ensure_connection()
        cur = self.conn.cursor()
        cur.execute("SELECT id, type, weight, length, width, height FROM packages WHERE order_id=%s ORDER BY created_at", (order_id,))
        return cur.fetchall()

    def get_packages(self, order_id):
        self.ensure_connection()
        cur = self.conn.cursor()
        cur.execute("SELECT id, type, weight, length, width, height FROM packages WHERE order_id=%s ORDER BY created_at", (order_id,))
        return cur.fetchall()

    def add_package(self, order_id, pkg_type, weight, length, width, height):
        self.ensure_connection()
        cur = self.conn.cursor()
        cur.execute("""
            INSERT INTO packages (order_id, type, weight, length, width, height)
            VALUES (%s, %s, %s, %s, %s, %s) RETURNING id
        """, (order_id, pkg_type, weight, length, width, height))
        new_id = cur.fetchone()[0]
        self.conn.commit()
        return new_id

    def delete_package(self, package_id):
        self.ensure_connection()
        cur = self.conn.cursor()
        cur.execute("DELETE FROM packages WHERE id=%s", (package_id,))
        self.conn.commit()

    def get_packages_text(self, order_id):
        packages = self.get_packages(order_id)
        if not packages:
            return ""
        lines = []
        for i, (pid, pkg_type, weight, length, width, height) in enumerate(packages, 1):
            dims = []
            if length: dims.append(f"{length}cm")
            if width: dims.append(f"{width}cm")
            if height: dims.append(f"{height}cm")
            dims_str = " x ".join(dims) if dims else "brak wymiarów"
            weight_str = f"{weight}kg" if weight else "brak wagi"
            type_str = pkg_type if pkg_type else "PACZKA"
            lines.append(f"{i}) {type_str}: {dims_str}, {weight_str}")
        return "\n".join(lines)

db = None
signeda_scraper = None

# ---------- SPLASH SCREEN Z PASKIEM POSTĘPU ----------
def create_splash_screen():
    """Tworzy splash screen z paskiem postępu"""
    splash = tk.Toplevel()
    splash.title("Ładowanie aplikacji...")
    splash.geometry("500x200")
    splash.resizable(False, False)
    splash.attributes('-topmost', True)
    
    ttk.Label(splash, text="AutoCore v3.3", font=("Arial", 16, "bold")).pack(pady=20)
    
    status_label = ttk.Label(splash, text="Łączenie z bazą danych...", foreground="blue")
    status_label.pack(pady=10)
    
    progress_var = tk.DoubleVar()
    progress_bar = ttk.Progressbar(splash, length=400, variable=progress_var, maximum=100)
    progress_bar.pack(pady=10)
    
    detail_label = ttk.Label(splash, text="", foreground="gray")
    detail_label.pack(pady=5)
    
    splash.update()
    return splash, status_label, progress_bar, progress_var, detail_label

def update_splash(splash, status_label, progress_bar, progress_var, detail_label, message, percentage, detail=""):
    """Aktualizuje splash screen"""
    status_label.config(text=message)
    progress_var.set(percentage)
    detail_label.config(text=detail)
    splash.update()

# ---------- INICJALIZACJA BAZY PO WYBORZE ADRESU IP ----------
def initialize_database(splash=None, status_label=None, progress_bar=None, progress_var=None, detail_label=None):
    global db, signeda_scraper
    try:
        if splash:
            update_splash(splash, status_label, progress_bar, progress_var, detail_label, 
                         "Łączenie z bazą danych...", 10, f"Serwer: {SELECTED_PG_HOST}")
            logging.info(f"[SPLASH] Łączenie do {SELECTED_PG_HOST}:{PG_PORT}/{PG_DBNAME}")
        
        db = Database(splash, status_label, progress_bar, progress_var, detail_label)
        
        if splash:
            update_splash(splash, status_label, progress_bar, progress_var, detail_label, 
                         "Ładowanie scrapera Signeda...", 75, "")
        
        signeda_scraper = SignedaScraper()
        
        if splash:
            update_splash(splash, status_label, progress_bar, progress_var, detail_label, 
                         "Gotowe!", 100, "")
        
        logging.info("[STARTUP] Database and scraper initialized successfully")
        return True
    except Exception as e:
        logging.exception("Błąd podczas inicjalizacji bazy lub scrapera")
        try:
            show_topmost_error("Błąd połączenia", f"Nie udało się połączyć z bazą PostgreSQL:\n{e}")
        except Exception:
            pass
        return False

# ---------- NOWA FUNKCJA: FORMULARZ DODAWANIA PACZKI Z WYBOREM TYPU ----------
def ask_package_details(parent, order_id=None, allow_skip=False):
    result = None
    win = tk.Toplevel(parent if parent else root)
    win.title("Dodaj paczkę")
    win.geometry("500x400")
    win.attributes('-topmost', True)

    ttk.Label(win, text="Typ paczki:").pack(pady=5)
    pkg_type_var = tk.StringVar(value="PACZKA")
    type_combo = ttk.Combobox(win, textvariable=pkg_type_var, values=["PACZKA", "PALETA", "PALETA NIESTANDARDOWA", "NIESTANDARDOWA"], state="readonly")
    type_combo.pack(pady=5)

    frame_dims = ttk.Frame(win)
    frame_dims.pack(pady=10)

    ttk.Label(frame_dims, text="Waga (kg):").grid(row=0, column=0, sticky="e")
    weight_entry = ttk.Entry(frame_dims, width=10)
    weight_entry.insert(0, "0")
    weight_entry.grid(row=0, column=1, padx=5)

    ttk.Label(frame_dims, text="Długość (cm):").grid(row=1, column=0, sticky="e")
    length_entry = ttk.Entry(frame_dims, width=10)
    length_entry.grid(row=1, column=1, padx=5)

    ttk.Label(frame_dims, text="Szerokość (cm):").grid(row=2, column=0, sticky="e")
    width_entry = ttk.Entry(frame_dims, width=10)
    width_entry.grid(row=2, column=1, padx=5)

    ttk.Label(frame_dims, text="Wysokość (cm):").grid(row=3, column=0, sticky="e")
    height_entry = ttk.Entry(frame_dims, width=10)
    height_entry.grid(row=3, column=1, padx=5)

    info_label = ttk.Label(win, text="", foreground="blue", wraplength=450)
    info_label.pack(pady=5)

    def update_fields(*args):
        pkg_type = pkg_type_var.get()
        if pkg_type == "PALETA":
            length_entry.config(state="disabled")
            width_entry.config(state="disabled")
            length_entry.delete(0, tk.END)
            length_entry.insert(0, "120")
            width_entry.delete(0, tk.END)
            width_entry.insert(0, "80")
            height_entry.config(state="normal")
            info_label.config(text="Europaleta: wymiary 120x80 cm, podaj tylko wysokość.")
        else:
            length_entry.config(state="normal")
            width_entry.config(state="normal")
            height_entry.config(state="normal")
            if pkg_type == "PACZKA":
                info_label.config(text="Standardowa paczka – podaj wszystkie wymiary.")
            elif pkg_type == "PALETA NIESTANDARDOWA":
                info_label.config(text="Paleta niestandardowa – podaj wszystkie wymiary.")
            elif pkg_type == "NIESTANDARDOWA":
                info_label.config(text="Paczka niestandardowa – podaj wszystkie wymiary.")

    type_combo.bind("<<ComboboxSelected>>", update_fields)
    update_fields()

    def on_ok():
        nonlocal result
        pkg_type = pkg_type_var.get()
        try:
            weight = float(weight_entry.get().replace(',', '.'))
        except:
            show_topmost_warning("Uwaga", "Nieprawidłowa waga", parent=win)
            return
        if weight < 0:
            show_topmost_warning("Uwaga", "Waga nie może być ujemna", parent=win)
            return
        try:
            length = float(length_entry.get().replace(',', '.'))
        except:
            length = 0.0
        try:
            width = float(width_entry.get().replace(',', '.'))
        except:
            width = 0.0
        try:
            height = float(height_entry.get().replace(',', '.'))
        except:
            height = 0.0

        if pkg_type != "PALETA":
            if length <= 0 or width <= 0 or height <= 0:
                show_topmost_warning("Uwaga", "Wszystkie wymiary muszą być większe od 0", parent=win)
                return
        else:
            if height <= 0:
                show_topmost_warning("Uwaga", "Wysokość musi być większa od 0", parent=win)
                return
        result = {
            'type': pkg_type,
            'weight': weight,
            'length': length,
            'width': width,
            'height': height
        }
        win.destroy()

    def on_cancel():
        nonlocal result
        result = None
        win.destroy()

    btn_frame = ttk.Frame(win)
    btn_frame.pack(pady=20)
    ttk.Button(btn_frame, text="Dodaj", command=on_ok).pack(side="left", padx=10)
    ttk.Button(btn_frame, text="Anuluj", command=on_cancel).pack(side="left", padx=10)

    safe_grab_window(win)
    win.wait_window()
    return result

# ---------- ZARZĄDZANIE PACZKAMI (OKNO) ----------
def manage_packages_window(order_id, parent_win):
    win = tk.Toplevel(parent_win)
    win.title(f"Paczki dla zlecenia #{order_id}")
    win.geometry("700x500")
    win.attributes('-topmost', True)

    tree = ttk.Treeview(win, columns=("Lp", "Typ", "Waga (kg)", "Długość (cm)", "Szerokość (cm)", "Wysokość (cm)"), show="headings")
    for col in ("Lp", "Typ", "Waga (kg)", "Długość (cm)", "Szerokość (cm)", "Wysokość (cm)"):
        tree.heading(col, text=col)
        tree.column(col, width=100)
    tree.pack(fill="both", expand=True, padx=10, pady=10)

    def refresh_packages():
        for row in tree.get_children():
            tree.delete(row)
        packages = db.get_packages(order_id)
        for pid, pkg_type, weight, length, width, height in packages:
            tree.insert("", "end", iid=pid, values=(pid, pkg_type or "PACZKA", weight or "", length or "", width or "", height or ""))

    refresh_packages()

    def add_package():
        pkg = ask_package_details(win)
        if pkg:
            db.add_package(order_id, pkg['type'], pkg['weight'], pkg['length'], pkg['width'], pkg['height'])
            refresh_packages()
            show_topmost_info("OK", "Dodano paczkę", parent=win)

    def delete_package():
        sel = tree.selection()
        if not sel:
            show_topmost_warning("Uwaga", "Wybierz paczkę do usunięcia", parent=win)
            return
        pid = int(sel[0])
        if ask_topmost_yesno("Usuń", f"Czy usunąć paczkę #{pid}?", parent=win):
            db.delete_package(pid)
            refresh_packages()
            show_topmost_info("OK", "Usunięto paczkę", parent=win)

    btn_frame = ttk.Frame(win)
    btn_frame.pack(pady=10)
    ttk.Button(btn_frame, text="Dodaj paczkę", command=add_package).pack(side="left", padx=5)
    ttk.Button(btn_frame, text="Usuń paczkę", command=delete_package).pack(side="left", padx=5)
    ttk.Button(btn_frame, text="Zamknij", command=win.destroy).pack(side="left", padx=5)

# ---------- FUNKCJE GUI ----------
def refresh_stats(stats_label):
    try:
        prod, active, low = db.get_stats()
        stats_label.config(text=f"Produkty: {prod}  |  Aktywne zlecenia: {active}  |  Niskie stany: {low}")
    except Exception as e:
        logging.error(f"Error refresh_stats: {e}")
        stats_label.config(text="Błąd ładowania statystyk")

def load_products_into_tree(tree, search_text="", filter_category="", filter_model=""):
    logging.info(f"[PRODUCTS] Loading products (search='{search_text}', category='{filter_category}', model='{filter_model}')")
    for row in tree.get_children():
        tree.delete(row)
    cur = db.conn.cursor()
    try:
        logging.debug("[PRODUCTS] Fetching data from database...")
        cur.execute("""
            SELECT barcode, oe_code, name, product_type, category, side, position,
                   stock, price, signeda_stock, models, source
            FROM products
            WHERE barcode NOT IN ('RABAT','CUSTOM')
        """)
        rows = cur.fetchall()
        logging.info(f"[PRODUCTS] Retrieved {len(rows)} products from database")
        
        filtered_count = 0
        for row in rows:
            if search_text:
                joined = " ".join(str(x).lower() for x in row)
                if search_text.lower() not in joined:
                    continue
            if filter_category and filter_category != "Wszystkie":
                if row[4] != filter_category:
                    continue
            if filter_model and filter_model != "Wszystkie":
                if filter_model not in str(row[10]):
                    continue
            filtered_count += 1
            stock = row[7]
            signeda_val = row[9] if len(row) > 9 else "0"
            if stock > 0:
                tag = 'blue'
            else:
                tag = stock_tag(signeda_val)
            tree.insert("", "end", values=row[:11], tags=(tag,))
        
        logging.info(f"[PRODUCTS] Displayed {filtered_count} products in table")
        tree.tag_configure('green', background='#d8ffd8')
        tree.tag_configure('yellow', background='#fff7c7')
        tree.tag_configure('red', background='#ffd8d8')
        tree.tag_configure('blue', background='#cce5ff')
    except Exception as e:
        logging.error(f"[PRODUCTS] Error load_products_into_tree: {e}")
        show_topmost_error("Error", f"Cannot load products: {e}")

def refresh_filters(category_cb, model_cb):
    try:
        categories = ["Wszystkie"] + db.get_categories()
        category_cb['values'] = categories
        models = ["Wszystkie"] + db.get_all_models()
        model_cb['values'] = models
    except Exception as e:
        logging.error(f"Error refresh_filters: {e}")

def export_to_csv(tree, filename="raport.csv"):
    try:
        rows = []
        cols = [tree.heading(c)['text'] for c in tree['columns']]
        rows.append(cols)
        for child in tree.get_children():
            values = tree.item(child)['values']
            rows.append(values)
        with open(filename, 'w', newline='', encoding='utf-8-sig') as f:
            writer = csv.writer(f)
            writer.writerows(rows)
        show_topmost_info("Eksport", f"Dane wyeksportowane do {filename}")
    except PermissionError:
        show_topmost_error("Błąd", f"Brak uprawnień do zapisu pliku {filename}")
        logging.error(f"Brak uprawnień do zapisu {filename}")
    except Exception as e:
        show_topmost_error("Błąd", f"Nie udało się wyeksportować: {e}")
        logging.error(f"Błąd eksportu: {e}")

def manage_salespersons_window(parent):
    win = tk.Toplevel(parent)
    win.title("Handlowcy")
    win.geometry("400x400")
    win.attributes('-topmost', True)
    listbox = tk.Listbox(win)
    listbox.pack(fill="both", expand=True, padx=10, pady=10)
    def refresh():
        listbox.delete(0, tk.END)
        for pid, name in db.get_salespersons():
            listbox.insert(tk.END, f"{pid}: {name}")
    refresh()
    def add():
        name = ask_topmost_string("Dodaj", "Imię i nazwisko handlowca:", parent=win)
        if name and db.add_salesperson(name):
            refresh()
        elif name:
            show_topmost_error("Błąd", "Już istnieje", parent=win)
    def delete():
        sel = listbox.curselection()
        if not sel:
            return
        line = listbox.get(sel[0])
        pid = int(line.split(":")[0])
        if ask_topmost_yesno("Usuń", f"Usunąć {line}?", parent=win):
            db.delete_salesperson(pid)
            refresh()
    ttk.Button(win, text="Dodaj", command=add).pack(side="left", padx=10, pady=5)
    ttk.Button(win, text="Usuń", command=delete).pack(side="left", padx=10, pady=5)

def manage_ip_mappings_window(parent):
    win = tk.Toplevel(parent)
    win.title("Przypisania IP do handlowców")
    win.geometry("600x400")
    win.attributes('-topmost', True)
    tree = ttk.Treeview(win, columns=("id","salesperson","ip","note"), show="headings")
    tree.heading("id", text="ID")
    tree.heading("salesperson", text="Handlowiec")
    tree.heading("ip", text="IP")
    tree.heading("note", text="Notatka")
    tree.column("id", width=40)
    tree.pack(fill="both", expand=True, padx=10, pady=10)

    def refresh():
        for r in tree.get_children():
            tree.delete(r)
        for mid, sid, ip, note in db.list_ip_mappings():
            name = db.get_salesperson_name(sid) or str(sid)
            tree.insert("", "end", values=(mid, f"{sid}: {name}", ip, note or ""))
    refresh()

    def add_mapping():
        sales = db.get_salespersons()
        if not sales:
            show_topmost_warning("Uwaga", "Brak handlowców. Najpierw dodaj handlowców.", parent=win)
            return
        options = [f"{pid}: {name}" for pid, name in sales]
        sel = ask_topmost_string("Wybierz handlowca", "Wklej wybranego handlowca z listy:\n" + "\n".join(options), parent=win)
        if not sel:
            return
        try:
            sid = int(sel.split(":")[0])
        except:
            show_topmost_error("Błąd", "Niepoprawny format handlowca", parent=win)
            return
        ip = ask_topmost_string("Adres IP", "Wpisz adres IP urządzenia (np. 192.168.1.45):", parent=win)
        if not ip:
            return
        note = ask_topmost_string("Notatka", "Dodatkowa notatka (opcjonalnie):", parent=win)
        if db.add_ip_mapping(sid, ip.strip(), note):
            refresh()
        else:
            show_topmost_error("Błąd", "Nie udało się dodać przypisania", parent=win)

    def delete_mapping():
        sel = tree.selection()
        if not sel:
            return
        vals = tree.item(sel[0])['values']
        mid = vals[0]
        if ask_topmost_yesno("Usuń", f"Usunąć przypisanie {vals[1]} -> {vals[2]}?", parent=win):
            db.delete_ip_mapping(mid)
            refresh()

    btnf = ttk.Frame(win)
    btnf.pack(pady=5)
    ttk.Button(btnf, text="Dodaj", command=add_mapping).pack(side="left", padx=10)
    ttk.Button(btnf, text="Usuń", command=delete_mapping).pack(side="left", padx=10)
    ttk.Button(btnf, text="Szczegóły", command=lambda: (
        salesperson_report_window(win, preselected_id=int(tree.item(tree.selection()[0])['values'][1].split(':')[0]))
    )).pack(side="left", padx=10)

def manage_workers_window(parent):
    win = tk.Toplevel(parent)
    win.title("Magazynierzy")
    win.geometry("400x400")
    win.attributes('-topmost', True)
    listbox = tk.Listbox(win)
    listbox.pack(fill="both", expand=True, padx=10, pady=10)
    def refresh():
        listbox.delete(0, tk.END)
        for pid, name in db.get_warehouse_workers():
            listbox.insert(tk.END, f"{pid}: {name}")
    refresh()
    def add():
        name = ask_topmost_string("Dodaj", "Imię i nazwisko magazyniera:", parent=win)
        if name and db.add_warehouse_worker(name):
            refresh()
        elif name:
            show_topmost_error("Błąd", "Już istnieje", parent=win)
    def delete():
        sel = listbox.curselection()
        if not sel:
            return
        line = listbox.get(sel[0])
        pid = int(line.split(":")[0])
        if ask_topmost_yesno("Usuń", f"Usunąć {line}?", parent=win):
            db.delete_warehouse_worker(pid)
            refresh()
    ttk.Button(win, text="Dodaj", command=add).pack(side="left", padx=10, pady=5)
    ttk.Button(win, text="Usuń", command=delete).pack(side="left", padx=10, pady=5)


def manage_customers_window(parent):
    win = tk.Toplevel(parent)
    win.title("Klienci")
    win.geometry("640x520")
    win.attributes('-topmost', True)

    main_frame = ttk.Frame(win)
    main_frame.pack(fill="both", expand=True, padx=10, pady=10)

    listbox = tk.Listbox(main_frame)
    listbox.grid(row=0, column=0, rowspan=8, sticky="nsew", padx=(0,10), pady=5)

    ttk.Label(main_frame, text="Nazwa:").grid(row=0, column=1, sticky="w")
    name_var = tk.StringVar()
    name_entry = ttk.Entry(main_frame, textvariable=name_var, width=40)
    name_entry.grid(row=0, column=2, sticky="ew", pady=2)

    ttk.Label(main_frame, text="Telefon:").grid(row=1, column=1, sticky="w")
    phone_var = tk.StringVar()
    ttk.Entry(main_frame, textvariable=phone_var, width=40).grid(row=1, column=2, sticky="ew", pady=2)

    ttk.Label(main_frame, text="Adres:").grid(row=2, column=1, sticky="w")
    address_var = tk.StringVar()
    ttk.Entry(main_frame, textvariable=address_var, width=40).grid(row=2, column=2, sticky="ew", pady=2)

    ttk.Label(main_frame, text="NIP:").grid(row=3, column=1, sticky="w")
    nip_var = tk.StringVar()
    ttk.Entry(main_frame, textvariable=nip_var, width=40).grid(row=3, column=2, sticky="ew", pady=2)

    ttk.Label(main_frame, text="Email:").grid(row=4, column=1, sticky="w")
    email_var = tk.StringVar()
    ttk.Entry(main_frame, textvariable=email_var, width=40).grid(row=4, column=2, sticky="ew", pady=2)

    ttk.Label(main_frame, text="Notatki:").grid(row=5, column=1, sticky="nw")
    notes_text = tk.Text(main_frame, width=40, height=8)
    notes_text.grid(row=5, column=2, sticky="ew", pady=2)

    main_frame.columnconfigure(2, weight=1)
    main_frame.rowconfigure(7, weight=1)

    def refresh():
        listbox.delete(0, tk.END)
        for cid, name, *_ in db.get_customers():
            listbox.insert(tk.END, f"{cid}: {name}")

    def show_selected():
        sel = listbox.curselection()
        if not sel:
            return
        cid = int(listbox.get(sel[0]).split(":")[0])
        customer = db.get_customer(cid)
        if customer:
            _, name, phone, address, nip, email, notes = customer
            name_var.set(name)
            phone_var.set(phone or "")
            address_var.set(address or "")
            nip_var.set(nip or "")
            email_var.set(email or "")
            notes_text.delete("1.0", tk.END)
            notes_text.insert("1.0", notes or "")

    def save():
        name = name_var.get().strip()
        if not name:
            show_topmost_error("Błąd", "Nazwa klienta jest wymagana", parent=win)
            return
        phone = phone_var.get().strip()
        address = address_var.get().strip()
        nip = nip_var.get().strip()
        email = email_var.get().strip()
        notes = notes_text.get("1.0", tk.END).strip()
        sel = listbox.curselection()
        if sel:
            cid = int(listbox.get(sel[0]).split(":")[0])
            db.update_customer(cid, name, phone, address, nip, email, notes)
        else:
            db.add_customer(name, phone, address, nip, email, notes)
        refresh()

    def delete_customer():
        sel = listbox.curselection()
        if not sel:
            return
        cid = int(listbox.get(sel[0]).split(":")[0])
        if ask_topmost_yesno("Usuń", "Usunąć tego klienta?", parent=win):
            db.delete_customer(cid)
            refresh()
            name_var.set("")
            phone_var.set("")
            address_var.set("")
            nip_var.set("")
            email_var.set("")
            notes_text.delete("1.0", tk.END)

    btn_frame = ttk.Frame(win)
    btn_frame.pack(fill="x", padx=10, pady=10)
    ttk.Button(btn_frame, text="Zapisz", command=save).pack(side="left", padx=5)
    ttk.Button(btn_frame, text="Usuń", command=delete_customer).pack(side="left", padx=5)
    ttk.Button(btn_frame, text="Zamknij", command=win.destroy).pack(side="right", padx=5)

    listbox.bind("<<ListboxSelect>>", lambda e: show_selected())
    refresh()


def manage_order_templates_window(parent):
    global active_template_window
    win = tk.Toplevel(parent)
    active_template_window = win
    win.title("Szablony zamówień")
    win.geometry("760x620")
    win.attributes('-topmost', True)

    left_frame = ttk.Frame(win)
    left_frame.pack(side="left", fill="both", expand=False, padx=10, pady=10)
    right_frame = ttk.Frame(win)
    right_frame.pack(side="right", fill="both", expand=True, padx=10, pady=10)

    template_list = tk.Listbox(left_frame, width=30)
    template_list.pack(fill="both", expand=True)

    ttk.Label(right_frame, text="Nazwa szablonu:").pack(anchor="w")
    name_var = tk.StringVar()
    name_entry = ttk.Entry(right_frame, textvariable=name_var, width=50)
    name_entry.pack(fill="x", pady=2)

    ttk.Label(right_frame, text="Model (opcjonalnie):").pack(anchor="w")
    model_var = tk.StringVar()
    model_entry = ttk.Entry(right_frame, textvariable=model_var, width=50)
    model_entry.pack(fill="x", pady=2)

    ttk.Label(right_frame, text="Opis:").pack(anchor="w")
    description_var = tk.StringVar()
    description_entry = ttk.Entry(right_frame, textvariable=description_var, width=50)
    description_entry.pack(fill="x", pady=2)

    ttk.Label(right_frame, text="Pozycje szablonu (kod, ilość w osobnej linii):").pack(anchor="w", pady=(10,0))
    items_text = tk.Text(right_frame, height=12)
    items_text.pack(fill="both", expand=True, pady=2)

    add_frame = ttk.Frame(right_frame)
    add_frame.pack(fill="x", pady=5)
    ttk.Label(add_frame, text="Kod produktu:").pack(side="left")
    product_barcode_var = tk.StringVar()
    product_barcode_entry = ttk.Entry(add_frame, textvariable=product_barcode_var, width=20)
    product_barcode_entry.pack(side="left", padx=5)
    ttk.Label(add_frame, text="Ilość:").pack(side="left")
    product_qty_var = tk.StringVar(value="1")
    product_qty_entry = ttk.Entry(add_frame, textvariable=product_qty_var, width=5)
    product_qty_entry.pack(side="left", padx=5)

    def add_template_product():
        barcode = product_barcode_var.get().strip()
        if not barcode:
            show_topmost_error("Błąd", "Podaj kod produktu", parent=win)
            return
        try:
            quantity = int(product_qty_var.get().strip())
            if quantity <= 0:
                quantity = 1
        except:
            quantity = 1
        cur = db.conn.cursor()
        cur.execute("SELECT name FROM products WHERE barcode=%s", (barcode,))
        row = cur.fetchone()
        if not row:
            show_topmost_error("Błąd", f"Produkt {barcode} nie istnieje w bazie", parent=win)
            return
        items_text.insert(tk.END, f"{barcode},{quantity}\n")
        product_barcode_var.set("")
        product_qty_var.set("1")

    def add_template_product_line(barcode, name, oe, price):
        items_text.insert(tk.END, f"{barcode},1\n")

    win.add_template_product_line = add_template_product_line
    product_barcode_entry.bind("<Return>", lambda e: add_template_product())
    ttk.Button(add_frame, text="Dodaj produkt", command=add_template_product).pack(side="left", padx=5)

    def refresh_templates():
        template_list.delete(0, tk.END)
        for tid, name, model, _ in db.get_order_templates():
            template_list.insert(tk.END, f"{tid}: {name} [{model or 'brak'}]")

    def load_template():
        sel = template_list.curselection()
        if not sel:
            return
        tid = int(template_list.get(sel[0]).split(":")[0])
        template = db.get_template_by_id(tid)
        if not template:
            return
        _id, name, model, description = template
        name_var.set(name)
        model_var.set(model or "")
        description_var.set(description or "")
        items_text.delete("1.0", tk.END)
        for barcode, qty in db.get_template_items(tid):
            items_text.insert(tk.END, f"{barcode},{qty}\n")

    def parse_template_items():
        lines = items_text.get("1.0", tk.END).splitlines()
        parsed = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            parts = [p.strip() for p in line.split(",")]
            if len(parts) == 1:
                parsed.append((parts[0], 1))
            else:
                try:
                    qty = int(parts[1])
                except:
                    qty = 1
                parsed.append((parts[0], qty))
        return parsed

    def save_template():
        name = name_var.get().strip()
        if not name:
            show_topmost_error("Błąd", "Nazwa szablonu jest wymagana", parent=win)
            return
        model = model_var.get().strip() or None
        description = description_var.get().strip() or None
        sel = template_list.curselection()
        if sel:
            tid = int(template_list.get(sel[0]).split(":")[0])
            db.rename_template(tid, name, model, description)
            db.delete_template_items(tid)
            for barcode, qty in parse_template_items():
                db.add_template_item(tid, barcode, qty)
        else:
            tid = db.add_order_template(name, model, description)
            for barcode, qty in parse_template_items():
                db.add_template_item(tid, barcode, qty)
        refresh_templates()
        show_topmost_info("OK", "Szablon zapisany", parent=win)

    def delete_template():
        sel = template_list.curselection()
        if not sel:
            return
        tid = int(template_list.get(sel[0]).split(":")[0])
        if ask_topmost_yesno("Usuń", "Usunąć szablon?", parent=win):
            db.delete_order_template(tid)
            refresh_templates()

    def on_close_template():
        global active_template_window
        active_template_window = None
        win.destroy()

    btn_frame = ttk.Frame(right_frame)
    btn_frame.pack(fill="x", pady=10)
    ttk.Button(btn_frame, text="Wczytaj szablon", command=load_template).pack(side="left", padx=5)
    ttk.Button(btn_frame, text="Zapisz szablon", command=save_template).pack(side="left", padx=5)
    ttk.Button(btn_frame, text="Usuń szablon", command=delete_template).pack(side="left", padx=5)
    ttk.Button(btn_frame, text="Zamknij", command=on_close_template).pack(side="right", padx=5)

    refresh_templates()
    win.protocol("WM_DELETE_WINDOW", on_close_template)


def salesperson_report_window(parent, preselected_id=None):
    win = tk.Toplevel(parent)
    win.title("Raport dla handlowca")
    win.geometry("900x700")
    win.attributes('-topmost', True)
    ttk.Label(win, text="Wybierz handlowca:").pack(pady=5)
    salespersons = db.get_salespersons()
    if not salespersons:
        show_topmost_info("Info", "Brak handlowców. Dodaj ich w opcjach.", parent=win)
        win.destroy()
        return
    sp_names = [f"{pid}: {name}" for pid, name in salespersons]
    sp_combo = ttk.Combobox(win, values=sp_names, state="readonly")
    sp_combo.pack(pady=5)
    # jeśli przekazano preselected_id, ustaw wartość w comboboxie
    if preselected_id is not None:
        for i, val in enumerate(sp_names):
            if val.split(":")[0] == str(preselected_id):
                sp_combo.current(i)
                break

    date_frame = ttk.Frame(win)
    date_frame.pack(pady=5)
    ttk.Label(date_frame, text="Data od (RRRR-MM-DD):").pack(side="left", padx=5)
    from_date = ttk.Entry(date_frame, width=12)
    from_date.pack(side="left", padx=5)
    ttk.Label(date_frame, text="Data do (RRRR-MM-DD):").pack(side="left", padx=5)
    to_date = ttk.Entry(date_frame, width=12)
    to_date.pack(side="left", padx=5)

    tree = ttk.Treeview(win, columns=("ID", "Klient", "Data", "Suma"), show="headings")
    tree.heading("ID", text="Zlecenie ID")
    tree.heading("Klient", text="Klient")
    tree.heading("Data", text="Data utworzenia")
    tree.heading("Suma", text="Suma (zł)")
    tree.pack(fill="both", expand=True, padx=10, pady=10)

    total_label = ttk.Label(win, text="", font=("Arial", 10, "bold"))
    total_label.pack(pady=5)

    def generate():
        sel = sp_combo.get()
        if not sel:
            show_topmost_warning("Uwaga", "Wybierz handlowca", parent=win)
            return
        sp_id = int(sel.split(":")[0])
        from_date_str = from_date.get().strip()
        to_date_str = to_date.get().strip()
        query = """
            SELECT id, customer_name, created_at, total_price
            FROM orders
            WHERE status='READY' AND salesperson_id=%s
        """
        params = [sp_id]
        if from_date_str:
            query += " AND date(created_at) >= %s"
            params.append(from_date_str)
        if to_date_str:
            query += " AND date(created_at) <= %s"
            params.append(to_date_str)
        query += " ORDER BY created_at DESC"
        cur = db.conn.cursor()
        cur.execute(query, params)
        rows = cur.fetchall()
        for row in tree.get_children():
            tree.delete(row)
        total_all = 0.0
        for r in rows:
            tree.insert("", "end", values=(r[0], r[1], r[2], f"{r[3]:.2f}"))
            total_all += r[3]
        total_label.config(text=f"Łączna wartość: {total_all:.2f} zł")
    ttk.Button(win, text="Generuj", command=generate).pack(pady=5)

def add_signeda_window(root, tree, category_cb, model_cb, stats_label):
    win = tk.Toplevel(root)
    win.title("Dodaj część Signeda")
    win.geometry("400x200")
    win.attributes('-topmost', True)
    ttk.Label(win, text="Kod produktu").pack(pady=10)
    entry = ttk.Entry(win, width=40)
    entry.pack()
    def download():
        code = entry.get().strip()
        if not code:
            return
        try:
            data = signeda_scraper.search_product(code)
            if not data:
                show_topmost_error("Błąd", "Nie znaleziono", parent=win)
                return
            cur = db.conn.cursor()
            cur.execute("SELECT id FROM products WHERE barcode=%s", (code,))
            if cur.fetchone():
                show_topmost_error("Błąd", "Już istnieje", parent=win)
                return
            price_win = tk.Toplevel(win)
            price_win.title("Cena produktu")
            price_win.geometry("400x200")
            price_win.attributes('-topmost', True)
            ttk.Label(price_win, text=f"Cena regularna: {data['price']:.2f} zł").pack(pady=10)
            ttk.Label(price_win, text="Czy chcesz ustawić własną cenę?").pack()
            custom_price_var = tk.StringVar()
            ttk.Entry(price_win, textvariable=custom_price_var, width=15).pack(pady=5)
            ttk.Label(price_win, text="Pozostaw puste, aby użyć regularnej").pack()
            use_custom = tk.BooleanVar(value=False)
            def set_custom():
                use_custom.set(True)
                price_win.destroy()
            def set_regular():
                use_custom.set(False)
                price_win.destroy()
            ttk.Button(price_win, text="Użyj własnej ceny", command=set_custom).pack(side="left", padx=10, pady=10)
            ttk.Button(price_win, text="Użyj regularnej ceny", command=set_regular).pack(side="right", padx=10, pady=10)
            safe_grab_window(price_win)
            price_win.wait_window()
            final_price = data['price']
            force = 0
            custom = 0
            if use_custom.get():
                try:
                    final_price = float(custom_price_var.get().replace(',', '.'))
                except:
                    show_topmost_error("Błąd", "Nieprawidłowa cena, użyto regularnej", parent=win)
                    final_price = data['price']
                else:
                    force = 1
                    custom = final_price
            try:
                cur.execute("""
                    INSERT INTO products(barcode, product_code, oe_code, name, models, product_type,
                                         side, position, description, price, stock, signeda_stock,
                                         source, external_id, last_sync, force_price, custom_price)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (code, code, data["oe_code"], data["name"], data["models"],
                      data["product_type"], data["side"], data["position"], data["description"],
                      final_price, 0, data["stock"], "signeda", data["external_id"], current_timestamp(),
                      force, custom))
            except Exception as seq_error:
                if "products_pkey" in str(seq_error):
                    db.conn.rollback()
                    cur.execute("SELECT MAX(id) FROM products")
                    max_id = cur.fetchone()[0] or 0
                    cur.execute(f"ALTER SEQUENCE products_id_seq RESTART WITH {max_id + 1}")
                    db.conn.commit()
                    cur.execute("""
                        INSERT INTO products(barcode, product_code, oe_code, name, models, product_type,
                                             side, position, description, price, stock, signeda_stock,
                                             source, external_id, last_sync, force_price, custom_price)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """, (code, code, data["oe_code"], data["name"], data["models"],
                          data["product_type"], data["side"], data["position"], data["description"],
                          final_price, 0, data["stock"], "signeda", data["external_id"], current_timestamp(),
                          force, custom))
                else:
                    raise seq_error
            if data.get("photo_url"):
                folder = download_product_photo(code, data["photo_url"])
                if folder:
                    cur.execute("UPDATE products SET photos_folder=%s WHERE barcode=%s", (folder, code))
            db.conn.commit()
            load_products_into_tree(tree, "", "", "")
            refresh_filters(category_cb, model_cb)
            refresh_stats(stats_label)
            show_topmost_info("OK", "Dodano (stan 0)", parent=win)
            win.destroy()
        except requests.exceptions.RequestException as e:
            show_topmost_error("Błąd sieci", f"Nie można połączyć z Signeda: {e}", parent=win)
            logging.error(f"Błąd sieci w add_signeda: {e}")
        except Exception as e:
            db.conn.rollback()
            show_topmost_error("Błąd", str(e), parent=win)
            logging.error(f"Błąd w add_signeda: {e}")
    ttk.Button(win, text="Pobierz i zapisz", command=download).pack(pady=20)

def add_manual_window(root, tree, category_cb, model_cb, stats_label):
    win = tk.Toplevel(root)
    win.title("Dodaj część ręcznie")
    win.geometry("700x850")
    win.attributes('-topmost', True)

    main_frame = ttk.Frame(win)
    main_frame.pack(fill="both", expand=True)

    canvas = tk.Canvas(main_frame)
    scrollbar = ttk.Scrollbar(main_frame, orient="vertical", command=canvas.yview)
    scrollable_frame = ttk.Frame(canvas)

    scrollable_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
    canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
    canvas.configure(yscrollcommand=scrollbar.set)

    canvas.pack(side="left", fill="both", expand=True)
    scrollbar.pack(side="right", fill="y")

    fields = {}
    ttk.Label(scrollable_frame, text="Kod kreskowy").pack()
    barcode_e = ttk.Entry(scrollable_frame, width=70)
    barcode_e.pack(pady=2)
    fields["barcode"] = barcode_e
    frame_oem = ttk.Frame(scrollable_frame)
    frame_oem.pack(fill="x", pady=5)
    ttk.Label(frame_oem, text="Kod OEM").pack(side="left")
    oem_e = ttk.Entry(frame_oem, width=50)
    oem_e.pack(side="left", padx=5)
    fields["oem"] = oem_e
    def fetch_by_oem():
        code = oem_e.get().strip()
        if not code:
            return
        try:
            data = signeda_scraper.search_product(code)
            if data:
                fields["name"].delete(0, tk.END)
                fields["name"].insert(0, data["name"])
                fields["models"].delete("1.0", tk.END)
                fields["models"].insert("1.0", data["models"])
                side_var.set(data["side"])
                position_var.set(data["position"])
                if data["product_type"]:
                    type_combo.set(data["product_type"])
                price_e.delete(0, tk.END)
                price_e.insert(0, str(data["price"]))
                additional_info = []
                if data.get("product_type"):
                    additional_info.append(f"Typ produktu: {data['product_type']}")
                if data.get("side"):
                    additional_info.append(f"Strona: {data['side']}")
                if data.get("position"):
                    additional_info.append(f"Pozycja: {data['position']}")
                if data.get("oe_code"):
                    additional_info.append(f"Kod OEM: {data['oe_code']}")
                if data.get("description"):
                    additional_info.append(f"Opis: {data['description']}")
                if data.get("models"):
                    additional_info.append(f"Modele:\n{data['models']}")
                desc_t.delete("1.0", tk.END)
                desc_t.insert("1.0", "\n\n".join(additional_info).strip())
            else:
                show_topmost_warning("Uwaga", "Nie znaleziono", parent=win)
        except Exception as e:
            show_topmost_error("Błąd", str(e), parent=win)
    ttk.Button(frame_oem, text="Pobierz dane", command=fetch_by_oem).pack(side="left")
    ttk.Label(scrollable_frame, text="Nazwa").pack()
    name_e = ttk.Entry(scrollable_frame, width=70)
    name_e.pack(pady=2)
    fields["name"] = name_e
    ttk.Label(scrollable_frame, text="Modele (linie)").pack()
    models_t = tk.Text(scrollable_frame, height=5, width=70)
    models_t.pack(pady=2)
    fields["models"] = models_t

    def normalize_for_barcode(text):
        value = str(text or "").strip()
        if not value:
            return ""
        normalized = unicodedata.normalize('NFKD', value)
        normalized = re.sub(r'[^A-Za-z0-9 ]', '', normalized)
        return normalized

    def generate_barcode_value():
        oe_code = str(fields["oem"].get() or "").strip()
        if not oe_code:
            return ""
        type_text = normalize_for_barcode(type_var.get())
        category_text = normalize_for_barcode(fields["category"].get())
        side_text = normalize_for_barcode(side_var.get())
        position_text = normalize_for_barcode(position_var.get())
        type_part = "".join([w[0] for w in type_text.split()[:2]]).upper()
        if not type_part and type_text:
            type_part = type_text[0].upper()
        category_part = category_text[:1].upper() if category_text else ""
        side_part = side_text[:1].upper() if side_text else ""
        pos_part = position_text[:1].upper() if position_text else ""
        oe_part = re.sub(r'[^A-Za-z0-9]', '', oe_code)
        if not oe_part:
            return ""
        base_code = f"{type_part}{category_part}{side_part}{pos_part}{oe_part}"
        product_type_value = (type_var.get() or "").strip().lower()
        if product_type_value != "używane unikat":
            return base_code

        candidate = base_code
        suffix = 1
        while True:
            cur = db.conn.cursor()
            cur.execute("SELECT 1 FROM products WHERE UPPER(barcode)=UPPER(%s)", (candidate,))
            if not cur.fetchone():
                return candidate
            candidate = f"{base_code}-{suffix}"
            suffix += 1

    ttk.Label(scrollable_frame, text="Cena").pack()
    price_e = ttk.Entry(scrollable_frame, width=20)
    price_e.pack(pady=2)
    fields["price"] = price_e
    ttk.Label(scrollable_frame, text="Ilość początkowa").pack()
    stock_e = ttk.Entry(scrollable_frame, width=10)
    stock_e.insert(0, "1")
    stock_e.pack(pady=2)
    ttk.Label(scrollable_frame, text="Typ").pack()
    type_var = tk.StringVar()
    type_combo = ttk.Combobox(scrollable_frame, textvariable=type_var, values=["nowe", "używane unikat", "używane wielokrotne", "inne"])
    type_combo.pack(pady=2)
    ttk.Label(scrollable_frame, text="Kategoria").pack()
    cat_e = ttk.Entry(scrollable_frame, width=50)
    cat_e.pack(pady=2)
    fields["category"] = cat_e
    ttk.Label(scrollable_frame, text="Źródło (manual/polcar/intercars)").pack()
    source_var = tk.StringVar(value="manual")
    source_combo = ttk.Combobox(scrollable_frame, textvariable=source_var, values=["manual", "polcar", "intercars"], state="readonly")
    source_combo.pack(pady=2)
    ttk.Label(scrollable_frame, text="Strona").pack()
    side_var = tk.StringVar()
    side_combo = ttk.Combobox(scrollable_frame, textvariable=side_var, values=["", "lewa", "prawa"])
    side_combo.pack(pady=2)
    ttk.Label(scrollable_frame, text="Pozycja").pack()
    position_var = tk.StringVar()
    pos_combo = ttk.Combobox(scrollable_frame, textvariable=position_var, values=["", "przod", "tyl"])
    pos_combo.pack(pady=2)
    ttk.Label(scrollable_frame, text="Opis").pack()
    desc_t = tk.Text(scrollable_frame, height=5, width=70)
    desc_t.pack(pady=2)
    ttk.Label(scrollable_frame, text="Ocena (1-5)").pack()
    rating_e = ttk.Entry(scrollable_frame, width=5)
    rating_e.pack()
    ttk.Label(scrollable_frame, text="Opis uszkodzeń").pack()
    damage_t = tk.Text(scrollable_frame, height=3, width=70)
    damage_t.pack(pady=2)
    def save():
        barcode = fields["barcode"].get().strip()
        if not barcode:
            barcode = generate_barcode_value()
            if barcode:
                fields["barcode"].delete(0, tk.END)
                fields["barcode"].insert(0, barcode)
        if not barcode:
            show_topmost_error("Błąd", "Kod wymagany", parent=win)
            return
        cur = db.conn.cursor()
        cur.execute("SELECT id FROM products WHERE barcode=%s", (barcode,))
        if cur.fetchone():
            show_topmost_error("Błąd", "Istnieje", parent=win)
            return
        duplicate = find_existing_product_by_barcode(barcode)
        if duplicate:
            existing_barcode, existing_name = duplicate
            show_topmost_error("Błąd", f"Produkt z kodem kreskowym {barcode} już istnieje: {existing_barcode} - {existing_name}", parent=win)
            return
        try:
            stock = int(stock_e.get())
        except:
            stock = 1
        price_val = normalize_price(price_e.get())
        cur.execute("""
            INSERT INTO products(barcode, oe_code, name, models, category, product_type,
                                 side, position, description, price, stock, condition_rating, damage_description, source)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (barcode, fields["oem"].get(), fields["name"].get(), models_t.get("1.0", tk.END).strip(),
              fields["category"].get(), type_var.get(), side_var.get(), position_var.get(),
              desc_t.get("1.0", tk.END).strip(), price_val, stock,
              rating_e.get() or 0, damage_t.get("1.0", tk.END).strip(), source_var.get() or "manual"))
        db.conn.commit()
        load_products_into_tree(tree, "", "", "")
        refresh_filters(category_cb, model_cb)
        refresh_stats(stats_label)
        show_topmost_info("OK", "Dodano", parent=win)
        win.destroy()
    ttk.Button(scrollable_frame, text="Zapisz", command=save).pack(pady=20)

def receive_delivery_window(root, tree, stats_label, category_cb, model_cb):
    win = tk.Toplevel(root)
    win.title("Przyjmij dostawę")
    win.geometry("500x500")
    win.attributes('-topmost', True)
    ttk.Label(win, text="Zeskanuj kod produktu").pack(pady=10)
    entry = ttk.Entry(win, width=50)
    entry.pack(pady=5)
    focus_scanner_entry(win, entry)
    info_label = ttk.Label(win, text="", wraplength=450)
    info_label.pack(pady=10)
    qty_frame = ttk.Frame(win)
    qty_frame.pack(pady=5)
    ttk.Label(qty_frame, text="Ilość:").pack(side="left")
    qty_var = tk.IntVar(value=1)
    qty_spin = ttk.Spinbox(qty_frame, from_=1, to=99, textvariable=qty_var, width=5)
    qty_spin.pack(side="left", padx=5)
    auto_signeda_var = tk.BooleanVar(value=True)
    ttk.Checkbutton(win, text="Automatycznie dodać brakujący produkt z Signeda", variable=auto_signeda_var).pack(anchor="w", padx=20, pady=5)

    def add_manual_entry(barcode, qty):
        sub = tk.Toplevel(win)
        sub.title("Dodaj ręcznie")
        sub.geometry("500x600")
        sub.attributes('-topmost', True)
        ttk.Label(sub, text=f"Dodawanie: {barcode}").pack(pady=5)
        name_e = ttk.Entry(sub, width=50)
        name_e.pack(pady=2)
        oe_e = ttk.Entry(sub, width=50)
        oe_e.pack(pady=2)
        price_e = ttk.Entry(sub, width=20)
        price_e.pack(pady=2)
        type_var = tk.StringVar()
        type_combo = ttk.Combobox(sub, textvariable=type_var, values=["nowe", "używane unikat", "używane wielokrotne", "inne"])
        type_combo.pack(pady=2)
        cat_e = ttk.Entry(sub, width=50)
        cat_e.pack(pady=2)

        def save_manual():
            name_val = name_e.get().strip()
            oe_val = oe_e.get().strip()
            price_val = normalize_price(price_e.get())
            if not name_val:
                show_topmost_error("Błąd", "Nazwa wymagana", parent=sub)
                return
            duplicate = find_existing_product_by_barcode(barcode)
            if duplicate:
                existing_barcode, existing_name = duplicate
                show_topmost_error("Błąd", f"Produkt z kodem kreskowym {barcode} już istnieje: {existing_barcode} - {existing_name}", parent=sub)
                return
            cur.execute("""
                INSERT INTO products(barcode, name, oe_code, price, product_type, category, stock, source)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """, (barcode, name_val, oe_val, price_val, type_var.get(), cat_e.get(), qty, "manual"))
            db.conn.commit()
            load_products_into_tree(tree, "", "", "")
            refresh_filters(category_cb, model_cb)
            refresh_stats(stats_label)
            show_topmost_info("OK", "Dodano ręcznie", parent=sub)
            sub.destroy()
            win.destroy()

        ttk.Button(sub, text="Zapisz", command=save_manual).pack(pady=10)

    def add_or_update():
        barcode = normalize_barcode(entry.get().strip())
        if not barcode:
            return
        qty = qty_var.get()
        cur = db.conn.cursor()
        cur.execute("SELECT id, name, stock, product_type FROM products WHERE UPPER(barcode)=UPPER(%s)", (barcode,))
        row = cur.fetchone()
        if row:
            prod_id, name, current_stock, ptype = row
            if ptype == "używane unikat" and current_stock > 0:
                show_topmost_error("Błąd", "Produkt unikat już na stanie", parent=win)
                return
            new_stock = current_stock + qty
            cur.execute("UPDATE products SET stock=%s WHERE id=%s", (new_stock, prod_id))
            db.conn.commit()
            info_label.config(text=f"Dodano {qty} szt. {name}\nNowy stan: {new_stock}")
            cur.execute("""
                SELECT oi.order_id, o.customer_name
                FROM order_items oi JOIN orders o ON oi.order_id=o.id
                WHERE UPPER(oi.barcode)=UPPER(%s) AND oi.picked=0 AND o.status IN ('NEW','READY')
            """, (barcode,))
            pending = cur.fetchall()
            if pending:
                order_list = "\n".join([f"ID: {p[0]} - {p[1]}" for p in pending])
                if ask_topmost_yesno("Oczekujące zlecenia", f"Produkt potrzebny w:\n{order_list}\nCzy odkliknąć w tych zleceniach?", parent=win):
                    for order_id, _ in pending:
                        cur.execute("UPDATE order_items SET picked=1 WHERE order_id=%s AND UPPER(barcode)=UPPER(%s)", (order_id, barcode))
                        cur.execute("UPDATE products SET stock=stock-1 WHERE UPPER(barcode)=UPPER(%s)", (barcode,))
                    db.conn.commit()
                    show_topmost_info("OK", "Zaktualizowano zlecenia", parent=win)
        else:
            if auto_signeda_var.get():
                try:
                    data = signeda_scraper.search_product(barcode)
                    if data:
                        duplicate = find_existing_product_by_barcode(barcode)
                        if duplicate:
                            existing_barcode, existing_name = duplicate
                            show_topmost_error("Błąd", f"Produkt z kodem kreskowym {barcode} już istnieje: {existing_barcode} - {existing_name}", parent=win)
                            return
                        cur.execute("""
                            INSERT INTO products(barcode, product_code, oe_code, name, models, category,
                                                 product_type, side, position, description, price, stock, signeda_stock,
                                                 source, external_id, last_sync)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """, (barcode, barcode, data["oe_code"], data["name"], data["models"],
                              "", data["product_type"], data["side"], data["position"], data["description"],
                              data["price"], qty, data["stock"], "signeda", data["external_id"], current_timestamp()))
                        if data.get("photo_url"):
                            folder = download_product_photo(barcode, data["photo_url"])
                            if folder:
                                cur.execute("UPDATE products SET photos_folder=%s WHERE barcode=%s", (folder, barcode))
                        db.conn.commit()
                        info_label.config(text=f"Dodano z Signeda: {data['name']}\nStan: {qty}")
                    else:
                        ans = ask_topmost_yesno("Nowy produkt", "Nie znaleziono w Signeda. Czy dodać ręcznie?", parent=win)
                        if ans:
                            add_manual_entry(barcode, qty)
                        else:
                            show_topmost_info("Info", "Produkt nie został dodany.", parent=win)
                            return
                except Exception as e:
                    db.conn.rollback()
                    if ask_topmost_yesno("Błąd", f"Błąd Signeda: {e}\nDodać ręcznie?", parent=win):
                        add_manual_entry(barcode, qty)
                    else:
                        return
            else:
                add_manual_entry(barcode, qty)
        load_products_into_tree(tree, "", "", "")
        refresh_filters(category_cb, model_cb)
        refresh_stats(stats_label)
        entry.delete(0, tk.END)
        entry.focus()

    ttk.Button(win, text="Przyjmij", command=add_or_update).pack(pady=10)
    bind_scan_submit(entry, lambda e=None: add_or_update())

def check_product_window(root, tree, category_cb, model_cb, stats_label):
    win = tk.Toplevel(root)
    win.title("Sprawdź produkt")
    win.geometry("800x600")
    win.attributes('-topmost', True)
    ttk.Label(win, text="Zeskanuj kod").pack(pady=10)
    entry = ttk.Entry(win, width=50)
    entry.pack(pady=5)
    entry.focus()
    result_text = tk.Text(win, wrap="word")
    result_text.pack(fill="both", expand=True, padx=10, pady=10)
    def search():
        barcode = entry.get().strip()
        if not barcode:
            return
        cur = db.conn.cursor()
        cur.execute("SELECT * FROM products WHERE barcode=%s", (barcode,))
        row = cur.fetchone()
        result_text.delete("1.0", tk.END)
        if row:
            cols = [desc[0] for desc in cur.description]
            for i, col in enumerate(cols):
                result_text.insert(tk.END, f"{col}: {row[i]}\n")
        else:
            result_text.insert(tk.END, "BRAK. Możesz dodać.")
    entry.bind("<Return>", lambda e: search())
    ttk.Button(win, text="Szukaj", command=search).pack(pady=5)
    btn_frame = ttk.Frame(win)
    btn_frame.pack(pady=10)
    ttk.Button(btn_frame, text="Dodaj z Signeda", command=lambda: add_signeda_window(root, tree, category_cb, model_cb, stats_label)).pack(side="left", padx=5)
    ttk.Button(btn_frame, text="Dodaj ręcznie", command=lambda: add_manual_window(root, tree, category_cb, model_cb, stats_label)).pack(side="left", padx=5)

def edit_product_all_window(barcode, master_tree, category_cb, model_cb, stats_label, parent_win):
    cur = db.conn.cursor()
    cur.execute("SELECT * FROM products WHERE barcode=%s", (barcode,))
    row = cur.fetchone()
    if not row:
        return
    win = tk.Toplevel(parent_win)
    win.title(f"Edytuj produkt - {barcode}")
    win.geometry("800x600")
    win.attributes('-topmost', True)
    main_frame = ttk.Frame(win)
    main_frame.pack(fill="both", expand=True)
    canvas = tk.Canvas(main_frame)
    scrollbar = ttk.Scrollbar(main_frame, orient="vertical", command=canvas.yview)
    scrollable_frame = ttk.Frame(canvas)
    scrollable_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
    canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
    canvas.configure(yscrollcommand=scrollbar.set)
    canvas.pack(side="left", fill="both", expand=True)
    scrollbar.pack(side="right", fill="y")
    cols = [desc[0] for desc in cur.description]
    entries = {}
    skip = ['id', 'created_at', 'last_sync']
    for i, col in enumerate(cols):
        if col in skip:
            continue
        frame = ttk.Frame(scrollable_frame)
        frame.pack(fill="x", padx=10, pady=5)
        ttk.Label(frame, text=col, width=20).pack(side="left")
        if col in ['models', 'description', 'damage_description']:
            txt = tk.Text(frame, height=4, width=60)
            txt.insert('1.0', str(row[i]) if row[i] else '')
            txt.pack(side="left", fill="x", expand=True)
            entries[col] = txt
        else:
            e = ttk.Entry(frame, width=60)
            e.insert(0, str(row[i]) if row[i] else '')
            e.pack(side="left", fill="x", expand=True)
            entries[col] = e
    def save_edit():
        update_fields = []
        values = []
        for i, col in enumerate(cols):
            if col in skip:
                continue
            if col in ['models', 'description', 'damage_description']:
                val = entries[col].get('1.0', tk.END).strip()
            else:
                val = entries[col].get()
            if col in ['price', 'custom_price', 'condition_rating', 'stock', 'force_price']:
                try:
                    if col == 'force_price':
                        val = int(val) if val else 0
                    else:
                        val = float(val) if val else 0
                except:
                    val = 0
            update_fields.append(f"{col}=%s")
            values.append(val)
        values.append(barcode)
        query = f"UPDATE products SET {', '.join(update_fields)} WHERE barcode=%s"
        cur.execute(query, values)
        db.conn.commit()
        load_products_into_tree(master_tree, "", "", "")
        refresh_filters(category_cb, model_cb)
        refresh_stats(stats_label)
        show_topmost_info("OK", "Produkt zaktualizowany", parent=win)
        win.destroy()
        parent_win.destroy()
        show_product_details_window(barcode, master_tree, category_cb, model_cb, stats_label)
    ttk.Button(scrollable_frame, text="Zapisz wszystkie zmiany", command=save_edit).pack(pady=20)

def show_product_details_window(barcode, master_tree, category_cb, model_cb, stats_label):
    barcode = str(barcode)
    cur = db.conn.cursor()
    try:
        cur.execute("SELECT * FROM products WHERE barcode=%s", (barcode,))
    except psycopg2.Error as e:
        db.conn.rollback()
        show_topmost_error("Błąd bazy danych", f"Nieprawidłowy kod produktu: {e}")
        return
    row = cur.fetchone()
    if not row:
        show_topmost_error("Błąd", "Produkt nie istnieje")
        return
    win = tk.Toplevel()
    win.title(f"Szczegóły - {barcode}")
    win.geometry("850x750")
    win.attributes('-topmost', True)
    main_frame = ttk.Frame(win)
    main_frame.pack(fill="both", expand=True)
    left_frame = ttk.Frame(main_frame, width=250)
    left_frame.pack(side="left", fill="y", padx=10, pady=10)
    right_frame = ttk.Frame(main_frame)
    right_frame.pack(side="right", fill="both", expand=True, padx=10, pady=10)

    photos_base_dir = get_photos_base_dir()
    expected_path = os.path.join(photos_base_dir, barcode)
    photo_path = None
    if os.path.exists(expected_path):
        for candidate_ext in ("jpg", "jpeg", "png", "gif", "bmp", "webp"):
            candidate = os.path.join(expected_path, f"main.{candidate_ext}")
            if os.path.exists(candidate):
                photo_path = candidate
                break
        if not photo_path:
            for filename in os.listdir(expected_path):
                lower = filename.lower()
                if lower.startswith("main.") and lower.split('.', 1)[1] in ("jpg", "jpeg", "png", "gif", "bmp", "webp"):
                    photo_path = os.path.join(expected_path, filename)
                    break
        if not photo_path:
            for filename in os.listdir(expected_path):
                lower = filename.lower()
                if lower.endswith((".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp")):
                    photo_path = os.path.join(expected_path, filename)
                    break

    photo_label = ttk.Label(left_frame, text="Brak zdjęcia", foreground="gray")
    photo_label.pack(pady=10)

    if photo_path and os.path.exists(photo_path):
        try:
            from PIL import Image, ImageTk
            img = Image.open(photo_path)
            img.thumbnail((250, 250))
            photo = ImageTk.PhotoImage(img)
            photo_label.config(image=photo, text="")
            photo_label.image = photo
        except ImportError as e:
            photo_label.config(text=f"Błąd importu Pillow: {e}", foreground="red")
        except Exception as e:
            photo_label.config(text=f"Błąd: {e}", foreground="red")
    else:
        if os.path.exists(expected_path):
            photo_label.config(text="Brak pliku main.*", foreground="orange")
        else:
            photo_label.config(text="Brak zdjęcia", foreground="gray")

    def open_photo_folder():
        try:
            os.makedirs(expected_path, exist_ok=True)
        except PermissionError as e:
            logging.error(f"Cannot create photos folder {expected_path}: {e}")
            show_topmost_error(
                "Błąd uprawnień",
                f"Brak uprawnień do utworzenia katalogu zdjęć:\n{expected_path}\n\n" \
                "Ustaw poprawne prawa do katalogu photos lub uruchom program w katalogu, do którego masz zapis.",
                parent=win
            )
            return
        open_folder(expected_path)
    ttk.Button(left_frame, text="Otwórz folder", command=open_photo_folder).pack(pady=5)

    def download_photo_from_signeda():
        try:
            data = signeda_scraper.search_product(barcode)
            if not data:
                show_topmost_error("Błąd", "Nie znaleziono produktu na Signeda", parent=win)
                return
            photo_url = data.get('photo_url')
            if not photo_url:
                show_topmost_warning("Uwaga", "Signeda nie zwróciła adresu zdjęcia", parent=win)
                return
            base_dir = get_photos_base_dir()
            if not os.path.exists(base_dir):
                show_topmost_error(
                    "Błąd",
                    f"Nie można uzyskać dostępu do katalogu zdjęć:\n{base_dir}",
                    parent=win
                )
                return
            expected_path_local = os.path.join(base_dir, barcode)
            try:
                os.makedirs(expected_path_local, exist_ok=True)
            except PermissionError as e:
                logging.error(f"Cannot create photos folder {expected_path_local}: {e}")
                show_topmost_error(
                    "Błąd uprawnień",
                    f"Brak uprawnień do utworzenia katalogu zdjęć:\n{expected_path_local}\n\n" \
                    "Ustaw poprawne prawa do katalogu zdjęć lub uruchom program w katalogu, do którego masz zapis.",
                    parent=win
                )
                return
            folder = download_product_photo(barcode, photo_url)
            if folder:
                cur.execute("UPDATE products SET photos_folder=%s WHERE barcode=%s", (folder, barcode))
                db.conn.commit()
                show_topmost_info("OK", "Pobrano zdjęcie produktu", parent=win)
                try:
                    from PIL import Image, ImageTk
                    img = Image.open(os.path.join(folder, 'main.jpg'))
                    img.thumbnail((250, 250))
                    photo = ImageTk.PhotoImage(img)
                    photo_label.config(image=photo, text="")
                    photo_label.image = photo
                except Exception:
                    pass
            else:
                show_topmost_error("Błąd", "Nie udało się pobrać zdjęcia", parent=win)
        except requests.exceptions.RequestException as e:
            show_topmost_error("Błąd sieci", f"Nie można połączyć się z Signeda: {e}", parent=win)
        except Exception as e:
            show_topmost_error("Błąd", f"Nie udało się pobrać zdjęcia: {e}", parent=win)

    ttk.Button(left_frame, text="Pobierz zdjęcie", command=download_photo_from_signeda).pack(pady=5)

    txt = tk.Text(right_frame, wrap="word")
    txt.pack(fill="both", expand=True)
    cols = [desc[0] for desc in cur.description]
    for i, col in enumerate(cols):
        txt.insert(tk.END, f"{col}: {row[i]}\n")
    btn_frame = ttk.Frame(win)
    btn_frame.pack(fill="x", pady=10)
    def create_label_pdf(barcode_value, lines):
        barcode_value = str(barcode_value)
        logging.debug(f"create_label_pdf called for barcode={barcode_value}")
        logging.debug(f"label lines: {lines}")
        def normalize_line(text):
            if text is None:
                return ""
            cleaned = re.sub(r'[\r\n\t]+', ' ', str(text))
            cleaned = re.sub(r' {2,}', ' ', cleaned).strip()
            return cleaned

        def wrap_lines(text, max_width, canvas_obj=None, draw_obj=None, font_name='Helvetica', font_size=8, font=None):
            if not text:
                return []
            words = text.split(' ')
            wrapped = []
            current = ""
            for word in words:
                candidate = word if not current else current + ' ' + word
                width = None
                if canvas_obj is not None:
                    width = canvas_obj.stringWidth(candidate, font_name, font_size)
                elif draw_obj is not None and font is not None:
                    width = draw_obj.textlength(candidate, font=font)
                else:
                    width = len(candidate) * font_size
                if width <= max_width:
                    current = candidate
                else:
                    if current:
                        wrapped.append(current)
                    if width <= max_width:
                        current = word
                    else:
                        # split long words
                        part = ""
                        for ch in word:
                            cand = part + ch
                            ch_width = canvas_obj.stringWidth(cand, font_name, font_size) if canvas_obj is not None else draw_obj.textlength(cand, font=font)
                            if ch_width <= max_width:
                                part = cand
                            else:
                                if part:
                                    wrapped.append(part)
                                part = ch
                        if part:
                            current = part
                        else:
                            current = ""
            if current:
                wrapped.append(current)
            return wrapped

        filtered_lines = []
        for line in lines:
            normalized = normalize_line(line)
            if not normalized or 'None' in normalized or ': ' not in normalized:
                continue
            filtered_lines.append(normalized)
        logging.debug(f"filtered label lines: {filtered_lines}")

        pdf_path = os.path.join(tempfile.gettempdir(), f"label_{barcode_value}_{datetime.now().strftime('%Y%m%d%H%M%S')}.pdf")
        errors = []
        try:
            from reportlab.lib.units import mm
            from reportlab.pdfgen import canvas
            from reportlab.graphics.barcode import code128
            width = 80 * mm
            height = 110 * mm
            c = canvas.Canvas(pdf_path, pagesize=(width, height))
            barcode_obj = code128.Code128(barcode_value, barHeight=25 * mm, barWidth=0.5 * mm)
            barcode_x = (width - barcode_obj.width) / 2
            barcode_y = height - 40 * mm
            barcode_obj.drawOn(c, barcode_x, barcode_y)
            c.setFont('Helvetica', 8)
            text_y = barcode_y - 8 * mm
            max_text_width = width - 10 * mm
            wrapped = []
            for line in filtered_lines:
                wrapped.extend(wrap_lines(line, max_text_width, canvas_obj=c, font_name='Helvetica', font_size=8))
            for line in wrapped:
                c.drawString(5 * mm, text_y, line)
                text_y -= 4.5 * mm
            c.showPage()
            c.save()
            return pdf_path, None
        except Exception as e:
            errors.append(f"reportlab: {type(e).__name__}: {e}")
            logging.debug(f"ReportLab import/generation failed: {e}", exc_info=True)
        try:
            import barcode as barcode_lib
            from barcode.writer import ImageWriter
            from PIL import Image, ImageDraw, ImageFont
            barcode_filename = os.path.join(tempfile.gettempdir(), f"barcode_{barcode_value}_{datetime.now().strftime('%Y%m%d%H%M%S')}")
            barcode_obj = barcode_lib.get('code128', barcode_value, writer=ImageWriter())
            image_path = barcode_obj.save(barcode_filename)
            barcode_img = Image.open(image_path).convert('RGB')
            font = ImageFont.load_default()
            padding = 10
            max_width = max(barcode_img.width + padding * 2, 400)
            text_draw = ImageDraw.Draw(barcode_img)
            wrapped = []
            for line in filtered_lines:
                wrapped.extend(wrap_lines(line, max_width - padding * 2, draw_obj=text_draw, font=font))
            line_height = font.getsize('A')[1] + 4
            text_height = line_height * len(wrapped) + padding * 2
            out_img = Image.new('RGB', (int(max_width), int(barcode_img.height + text_height + padding)), 'white')
            draw = ImageDraw.Draw(out_img)
            x = int((max_width - barcode_img.width) / 2)
            out_img.paste(barcode_img, (x, padding))
            text_y = barcode_img.height + padding * 1.5
            for line in wrapped:
                draw.text((padding, text_y), line, fill='black', font=font)
                text_y += line_height
            out_img.save(pdf_path, 'PDF', resolution=100.0)
            return pdf_path, None
        except Exception as e:
            errors.append(f"barcode/Pillow: {type(e).__name__}: {e}")
            logging.debug(f"python-barcode/Pillow generation failed: {e}", exc_info=True)
        errors.append(f"Interpreter: {sys.executable}")
        return None, '\n'.join(errors)

    def print_label():
        product = dict(zip(cols, row))
        barcode_value = product.get('barcode') or product.get('product_code') or 'none'
        lines = []
        if product.get('barcode'):
            lines.append(f"Kod: {product.get('barcode')}")
        if product.get('name'):
            lines.append(f"Nazwa: {product.get('name')}")
        if product.get('product_type'):
            lines.append(f"Typ: {product.get('product_type')}")
        if product.get('category'):
            lines.append(f"Kategoria: {product.get('category')}")
        if product.get('side'):
            lines.append(f"Strona: {product.get('side')}")
        if product.get('position'):
            lines.append(f"Pozycja: {product.get('position')}")
        if product.get('condition_rating'):
            lines.append(f"Ocena: {product.get('condition_rating')}")
        if product.get('description'):
            lines.append(f"Opis: {product.get('description')}")
        if product.get('damage_description'):
            lines.append(f"Uszkodzenia: {product.get('damage_description')}")
        
        pdf_path, error_message = create_label_pdf(barcode_value, lines)
        if pdf_path:
            try:
                if sys.platform == "win32":
                    os.startfile(pdf_path)
                elif sys.platform == "darwin":
                    subprocess.run(["open", pdf_path])
                else:
                    subprocess.run(["xdg-open", pdf_path])
                return
            except Exception as e:
                show_topmost_error('Błąd', f'Nie udało się otworzyć PDF: {e}', parent=win)
                return
        if error_message:
            show_topmost_error('Błąd', f'Nie wygenerowano PDF z kodem kreskowym:\n{error_message}', parent=win)
        else:
            show_topmost_error('Błąd', 'Nie udało się wygenerować PDF z etykietą. Zainstaluj reportlab lub python-barcode i Pillow.', parent=win)

    ttk.Button(btn_frame, text="Etykieta (PDF)", command=print_label).pack(side="left", padx=5)
    def delete_product():
        if ask_topmost_yesno("Usuń", f"Czy na pewno usunąć produkt {barcode}?", parent=win):
            cur.execute("DELETE FROM products WHERE barcode=%s", (barcode,))
            db.conn.commit()
            load_products_into_tree(master_tree, "", "", "")
            refresh_filters(category_cb, model_cb)
            refresh_stats(stats_label)
            win.destroy()
            show_topmost_info("OK", "Usunięto", parent=win)
    ttk.Button(btn_frame, text="Usuń z bazy", command=delete_product).pack(side="left", padx=5)
    def edit_all():
        edit_product_all_window(barcode, master_tree, category_cb, model_cb, stats_label, win)
    ttk.Button(btn_frame, text="Edytuj wszystkie dane", command=edit_all).pack(side="left", padx=5)
    def open_link():
        url = f"https://www.signeda.pl/index.php?route=product/search&search={barcode}"
        webbrowser.open(url)
    ttk.Button(btn_frame, text="Link do Signeda", command=open_link).pack(side="left", padx=5)
    force_var = tk.BooleanVar(value=bool(row[19]) if len(row) > 19 else False)
    custom_price_var = tk.StringVar(value=row[20] if len(row) > 20 else "")
    def toggle_force():
        if force_var.get():
            price_frame.pack(fill="x", pady=5)
        else:
            price_frame.pack_forget()
    force_cb = ttk.Checkbutton(btn_frame, text="Wymuś cenę", variable=force_var, command=toggle_force)
    force_cb.pack(side="left", padx=5)
    price_frame = ttk.Frame(win)
    ttk.Label(price_frame, text="Cena wymuszona:").pack(side="left")
    custom_price_e = ttk.Entry(price_frame, textvariable=custom_price_var, width=10)
    custom_price_e.pack(side="left", padx=5)
    def save_force():
        cur.execute("UPDATE products SET force_price=%s, custom_price=%s WHERE barcode=%s", (1 if force_var.get() else 0, normalize_price(custom_price_var.get()), barcode))
        db.conn.commit()
        show_topmost_info("OK", "Zapisano wymuszenie ceny", parent=win)
        win.destroy()
        show_product_details_window(barcode, master_tree, category_cb, model_cb, stats_label)
    ttk.Button(btn_frame, text="Zapisz wymuszenie", command=save_force).pack(side="left", padx=5)

    def preview_olx():
        name = row[4]
        oe = row[3]
        price = row[11] if not force_var.get() else normalize_price(custom_price_var.get())
        product_type = row[7]
        models = row[5]
        title = f"{name} - {models.split(chr(10))[0] if models else ''}".strip()
        description = f"Kod OEM: {oe}\n"
        if product_type == "nowe":
            description += "Produkt nowy, zamiennik OEM.\n"
        else:
            description += f"Produkt używany. Stan: {row[14]}/5. Uszkodzenia: {row[15]}\n"
        description += f"Cena: {price} zł\nKontakt przez OLX."
        preview = tk.Toplevel(win)
        preview.title("Podgląd ogłoszenia OLX")
        preview.geometry("600x500")
        preview.attributes('-topmost', True)
        ttk.Label(preview, text="Tytuł:").pack(anchor="w")
        ttk.Label(preview, text=title, wraplength=550).pack(anchor="w", pady=5)
        ttk.Label(preview, text="Opis:").pack(anchor="w")
        txt_desc = tk.Text(preview, height=15, wrap="word")
        txt_desc.insert("1.0", description)
        txt_desc.pack(fill="both", expand=True)
        ttk.Label(preview, text="Cena:").pack(anchor="w")
        ttk.Label(preview, text=f"{price} zł").pack(anchor="w")
        ttk.Button(preview, text="Zamknij", command=preview.destroy).pack(pady=10)
    ttk.Button(btn_frame, text="Dodaj ogłoszenie OLX", command=preview_olx).pack(side="left", padx=5)

    product_type = row[7]
    if product_type == "używane unikat":
        def archive_product():
            if ask_topmost_yesno("Archiwizuj", f"Czy przenieść produkt {barcode} do archiwum?\nPo przeniesieniu będzie dostępny w archiwum.", parent=win):
                if db.archive_product(barcode):
                    load_products_into_tree(master_tree, "", "", "")
                    refresh_filters(category_cb, model_cb)
                    refresh_stats(stats_label)
                    show_topmost_info("OK", "Produkt przeniesiony do archiwum", parent=win)
                    win.destroy()
                else:
                    show_topmost_error("Błąd", "Nie udało się przenieść", parent=win)
        ttk.Button(btn_frame, text="Przenieś do archiwum", command=archive_product).pack(side="left", padx=5)

    stock = row[12]
    signeda_stock = row[13] if len(row) > 13 else "0"
    def is_available():
        if stock > 0:
            return True
        s = str(signeda_stock).strip()
        if s == "0":
            return False
        if ">" in s:
            return True
        try:
            if int(re.sub(r"[^0-9]", "", s)) > 0:
                return True
        except:
            pass
        return False
    olx_status = "✔️ Ogłoszenie OLX może być aktywne" if is_available() else "❌ Ogłoszenie OLX nieaktywne"
    olx_color = "green" if is_available() else "red"
    olx_label = ttk.Label(btn_frame, text=olx_status, foreground=olx_color)
    olx_label.pack(side="left", padx=10)

def archived_products_window(root, master_tree, category_cb, model_cb, stats_label):
    win = tk.Toplevel(root)
    win.title("Archiwum produktów")
    win.geometry("1000x600")
    win.attributes('-topmost', True)
    columns = ("Kod", "Nazwa", "Typ", "Cena", "Data archiwizacji")
    tree = ttk.Treeview(win, columns=columns, show="headings")
    for col in columns:
        tree.heading(col, text=col)
        tree.column(col, width=150)
    tree.pack(fill="both", expand=True, padx=10, pady=10)
    def refresh_archive():
        for row in tree.get_children():
            tree.delete(row)
        rows = db.get_archived_products()
        for r in rows:
            tree.insert("", "end", values=(r[0], r[1], r[2], f"{r[3]:.2f}", r[4]))
    refresh_archive()
    btn_frame = ttk.Frame(win)
    btn_frame.pack(pady=10)
    def restore():
        sel = tree.selection()
        if not sel:
            return
        barcode = tree.item(sel[0])['values'][0]
        if ask_topmost_yesno("Przywróć", f"Czy przywrócić produkt {barcode} do głównej bazy?", parent=win):
            if db.restore_product(barcode):
                refresh_archive()
                load_products_into_tree(master_tree, "", "", "")
                refresh_filters(category_cb, model_cb)
                refresh_stats(stats_label)
                show_topmost_info("OK", "Produkt przywrócony", parent=win)
            else:
                show_topmost_error("Błąd", "Nie udało się przywrócić", parent=win)
    def delete_permanent():
        sel = tree.selection()
        if not sel:
            return
        barcode = tree.item(sel[0])['values'][0]
        if ask_topmost_yesno("Usuń trwale", f"Czy trwale usunąć produkt {barcode} z archiwum?\nTej operacji nie można cofnąć.", parent=win):
            db.delete_archived_product_permanently(barcode)
            refresh_archive()
            show_topmost_info("OK", "Produkt trwale usunięty", parent=win)
    ttk.Button(btn_frame, text="Przywróć", command=restore).pack(side="left", padx=5)
    ttk.Button(btn_frame, text="Usuń trwale", command=delete_permanent).pack(side="left", padx=5)
    ttk.Button(btn_frame, text="Zamknij", command=win.destroy).pack(side="left", padx=5)

# ---------- ZLECENIA ----------
active_order_window = None
active_edit_window = None
active_template_window = None

def add_product_to_active_order(barcode, name, oe, price):
    global active_order_window
    if active_order_window and active_order_window.winfo_exists():
        active_order_window.add_product_line(barcode, name, oe, price)
        return True
    return False

def add_product_to_edit_order(barcode, name, oe, price):
    global active_edit_window
    if active_edit_window and active_edit_window.winfo_exists():
        if hasattr(active_edit_window, 'add_product'):
            active_edit_window.add_product(barcode, name, oe, price)
            return True
    return False

def add_product_to_template(barcode, name, oe, price):
    global active_template_window
    if active_template_window and active_template_window.winfo_exists():
        if hasattr(active_template_window, 'add_template_product_line'):
            active_template_window.add_template_product_line(barcode, name, oe, price)
            return True
    return False

def show_quote_preview(order_id):
    cur = db.conn.cursor()
    cur.execute("SELECT customer_name, extra_info, salesperson_id, total_price FROM orders WHERE id=%s", (order_id,))
    order = cur.fetchone()
    if not order:
        show_topmost_warning("Uwaga", "Nie znaleziono zamówienia")
        return
    customer_name, extra_info, sp_id, total_price = order
    salesperson_name = ""
    if sp_id:
        cur.execute("SELECT name FROM salespersons WHERE id=%s", (sp_id,))
        sp = cur.fetchone()
        if sp:
            salesperson_name = sp[0]
    cur.execute("""
        SELECT oi.barcode,
               COALESCE(oi.custom_name, p.name) AS name,
               p.oe_code, p.product_type,
               COALESCE(oi.unit_price, p.price) AS price,
               p.side, p.position
        FROM order_items oi
        JOIN products p ON oi.barcode = p.barcode
        WHERE oi.order_id = %s
    """, (order_id,))
    items = cur.fetchall()
    if not items:
        show_topmost_warning("Uwaga", "Brak produktów w zleceniu")
        return
    template_path = find_quote_template(APP_DIR)
    rows = []
    for barcode, name, oe, ptype, price, side, position in items:
        clean_name = clean_product_name(barcode, name) if barcode not in ("CUSTOM", "RABAT_FORCED", "DOPLATA_FORCED") else name
        miejsce = ""
        if side and position:
            miejsce = f"{side} {position}"
        elif side:
            miejsce = side
        elif position:
            miejsce = position
        else:
            miejsce = "brak"
        if ptype == "nowe":
            client_type = "Aftermarket"
        elif ptype in ("używane unikat", "używane wielokrotne"):
            client_type = "Używany"
        elif ptype == "oryginał":
            client_type = "Oryginał"
        else:
            client_type = "Inny"
        price_float = normalize_price(price)
        rows.append(f"<tr><td>{clean_name}</td><td>{miejsce}</td><td>{oe}</td><td>{client_type}</td><td>{price_float:.2f} zł</td></tr>")
    items_table = "".join(rows)
    quote_html = build_quote_html(
        {
            'title': f"WYCENA ZLECENIA #{order_id}",
            'customer_name': customer_name,
            'items_table': items_table,
            'total_price': f"{total_price:.2f}",
        },
        template_path=template_path,
    )
    win = tk.Toplevel()
    win.title(f"Wycena dla klienta - zlecenie #{order_id}")
    win.geometry("800x600")
    win.attributes('-topmost', True)
    text_area = tk.Text(win, wrap="word")
    text_area.insert("1.0", quote_html)
    text_area.pack(fill="both", expand=True, padx=10, pady=10)
    btn_frame = ttk.Frame(win)
    btn_frame.pack(pady=10)
    def copy_to_clip():
        win.clipboard_clear()
        win.clipboard_append(quote_html)
        show_topmost_info("Kopiowanie", "Wycena skopiowana do schowka", parent=win)
    def print_quote():
        try:
            import tempfile
            with tempfile.NamedTemporaryFile(mode='w', suffix='.html', delete=False, encoding='utf-8') as f:
                f.write(quote_html)
                temp_name = f.name
            if sys.platform == "win32":
                webbrowser.open(temp_name)
            else:
                webbrowser.open(temp_name)
        except Exception as e:
            show_topmost_error("Błąd", f"Nie udało się wydrukować: {e}", parent=win)
    ttk.Button(btn_frame, text="Kopiuj do schowka", command=copy_to_clip).pack(side="left", padx=5)
    ttk.Button(btn_frame, text="Drukuj", command=print_quote).pack(side="left", padx=5)
    ttk.Button(btn_frame, text="Zamknij", command=win.destroy).pack(side="left", padx=5)

def create_order_window(root, stats_label, master_tree, category_cb, model_cb):
    global active_order_window, SELECTED_SALESPERSON_ID, CURRENT_USER_ID, CURRENT_USER_ROLE, CURRENT_USER_NAME
    if CURRENT_USER_ROLE == "Magazynier":
        show_topmost_error("Brak uprawnień", "Magazynier nie może tworzyć zleceń", parent=root)
        return
    if active_order_window and active_order_window.winfo_exists():
        active_order_window.lift()
        return
    win = tk.Toplevel(root)
    active_order_window = win
    win.title("Nowe zlecenie")
    win.geometry("1000x900")
    win.attributes('-topmost', True)

    main_frame = ttk.Frame(win)
    main_frame.pack(fill="both", expand=True)

    canvas = tk.Canvas(main_frame)
    scrollbar = ttk.Scrollbar(main_frame, orient="vertical", command=canvas.yview)
    scrollable_frame = ttk.Frame(canvas)

    scrollable_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
    canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
    canvas.configure(yscrollcommand=scrollbar.set)

    canvas.pack(side="left", fill="both", expand=True)
    scrollbar.pack(side="right", fill="y")

    entries = {}
    ttk.Label(scrollable_frame, text="Handlowiec:").pack(pady=(10,0))
    salespersons = db.get_salespersons()
    if not salespersons:
        show_topmost_warning("Uwaga", "Brak handlowców. Dodaj ich w opcjach.", parent=win)
        win.destroy()
        return
    selected_salesperson_id = SELECTED_SALESPERSON_ID if SELECTED_SALESPERSON_ID is not None else CURRENT_USER_ID
    if selected_salesperson_id is None and CURRENT_USER_ROLE == "Handlowiec":
        selected_salesperson_id = CURRENT_USER_ID
    if selected_salesperson_id is None:
        selected_salesperson_id = salespersons[0][0]
    if SELECTED_SALESPERSON_ID != selected_salesperson_id:
        SELECTED_SALESPERSON_ID = selected_salesperson_id
    selected_salesperson_name = None
    for pid, name in salespersons:
        if pid == selected_salesperson_id:
            selected_salesperson_name = name
            break
    if selected_salesperson_name is None:
        selected_salesperson_id, selected_salesperson_name = salespersons[0]
    selected_salesperson_label = ttk.Label(scrollable_frame, text=f"{selected_salesperson_id}: {selected_salesperson_name}", font=("Arial", 10, "bold"))
    selected_salesperson_label.pack(pady=5)
    values = [f"{pid}: {name}" for pid, name in salespersons]
    selected_salesperson_value = f"{selected_salesperson_id}: {selected_salesperson_name}"
    selected_salesperson_name = selected_salesperson_name

    customers = db.get_customer_names()
    customer_selection_values = ["Wybierz klienta"] + [f"{cid}: {name}" for cid, name in customers]
    customer_var = tk.StringVar(value=customer_selection_values[0])
    ttk.Label(scrollable_frame, text="Wczytaj klienta:").pack(pady=(10,0))
    customer_combo = ttk.Combobox(scrollable_frame, textvariable=customer_var, values=customer_selection_values, state="readonly", width=67)
    customer_combo.pack(pady=2)
    def load_customer():
        selected = customer_combo.get()
        if not selected or selected == "Wybierz klienta":
            return
        cid = int(selected.split(":")[0])
        customer = db.get_customer(cid)
        if not customer:
            return
        _, name, phone, address, nip, email, notes = customer
        entries["Imię i nazwisko"].delete(0, tk.END)
        entries["Imię i nazwisko"].insert(0, name or "")
        entries["Telefon"].delete(0, tk.END)
        entries["Telefon"].insert(0, phone or "")
        entries["Adres"].delete(0, tk.END)
        entries["Adres"].insert(0, address or "")
        entries["NIP"].delete(0, tk.END)
        entries["NIP"].insert(0, nip or "")
        entries["Email"].delete(0, tk.END)
        entries["Email"].insert(0, email or "")
    ttk.Button(scrollable_frame, text="Wczytaj klienta", command=load_customer).pack(pady=2)

    templates = db.get_order_template_names()
    template_selection_values = ["Wybierz szablon"] + [f"{tid}: {name}" for tid, name in templates]
    template_var = tk.StringVar(value=template_selection_values[0])
    ttk.Label(scrollable_frame, text="Wczytaj szablon zamówienia:").pack(pady=(10,0))
    template_combo = ttk.Combobox(scrollable_frame, textvariable=template_var, values=template_selection_values, state="readonly", width=67)
    template_combo.pack(pady=2)
    def load_template_into_order():
        selected = template_combo.get()
        if not selected or selected == "Wybierz szablon":
            return
        tid = int(selected.split(":")[0])
        template_items = db.get_template_items(tid)
        if not template_items:
            show_topmost_warning("Uwaga", "Szablon nie zawiera pozycji", parent=win)
            return
        if items_text.get("1.0", tk.END).strip():
            if not ask_topmost_yesno("Potwierdź", "Załadować szablon do istniejącego zamówienia? Istniejące pozycje pozostaną.", parent=win):
                return
        for barcode, qty in template_items:
            cur = db.conn.cursor()
            cur.execute("SELECT name, oe_code, price FROM products WHERE barcode=%s", (barcode,))
            row = cur.fetchone()
            if row:
                name, oe, price = row
            else:
                name, oe, price = barcode, "", 0.0
            for _ in range(max(1, qty)):
                add_product_line(barcode, name, oe, price)
        update_total()
        show_topmost_info("OK", "Szablon załadowany do zamówienia", parent=win)
    ttk.Button(scrollable_frame, text="Wczytaj szablon", command=load_template_into_order).pack(pady=2)

    labels = ["Imię i nazwisko", "Telefon", "Adres", "NIP", "Email"]
    for label in labels:
        ttk.Label(scrollable_frame, text=label).pack()
        e = ttk.Entry(scrollable_frame, width=70)
        e.pack(pady=2)
        entries[label] = e

    ttk.Label(scrollable_frame, text="Typ dokumentu").pack()
    doc_var = tk.StringVar()
    doc_combo = ttk.Combobox(scrollable_frame, textvariable=doc_var, values=["FAKTURA", "SPRZEDAŻ INNA"])
    doc_combo.pack(pady=2)

    ttk.Label(scrollable_frame, text="Typ dostawy").pack()
    delivery_var = tk.StringVar()
    delivery_combo = ttk.Combobox(scrollable_frame, textvariable=delivery_var,
                                  values=["PACZKA", "PACZKA NIESTANDARDOWA", "PALETA", "PALETA NIESTANDARDOWA",
                                          "ODBIÓR OSOBISTY", "INNE"])
    delivery_combo.pack(pady=2)

    ttk.Label(scrollable_frame, text="Pobranie (cena z wysyłką czy plus wysyłka)").pack(pady=(10,0))
    cod_var = tk.StringVar(value="z_wysylka")
    cod_frame = ttk.Frame(scrollable_frame)
    cod_frame.pack(pady=5)
    ttk.Radiobutton(cod_frame, text="z wysyłką", variable=cod_var, value="z_wysylka").pack(side="left", padx=10)
    ttk.Radiobutton(cod_frame, text="plus wysyłka", variable=cod_var, value="plus_wysylka").pack(side="left", padx=10)

    ttk.Label(scrollable_frame, text="Samochód / dodatkowe informacje (dowolny tekst)").pack(pady=(10,0))
    extra_info_entry = ttk.Entry(scrollable_frame, width=70)
    extra_info_entry.pack(pady=2)

    ttk.Label(scrollable_frame, text="Magazynier odpowiedzialny:").pack(pady=(10,0))
    warehouse_workers = db.get_warehouse_workers()
    worker_combo = None
    if warehouse_workers:
        worker_combo = ttk.Combobox(scrollable_frame, values=[f"{pid}: {name}" for pid, name in warehouse_workers], state="readonly")
        worker_combo.pack(pady=5)
    else:
        ttk.Label(scrollable_frame, text="Brak magazynierów – dodaj ich w opcjach", foreground="red").pack()

    ttk.Label(scrollable_frame, text="Produkty (każdy w nowej linii, format: kod | nazwa | OEM | cena)").pack()
    items_text = tk.Text(scrollable_frame, height=12)
    items_text.pack(fill="both", expand=True, padx=10, pady=5)

    total_frame = ttk.Frame(scrollable_frame)
    total_frame.pack(fill="x", padx=10, pady=5)
    ttk.Label(total_frame, text="Suma: ").pack(side="left")
    total_var = tk.StringVar(value="0.00")
    total_label = ttk.Label(total_frame, textvariable=total_var, font=("Arial", 12, "bold"))
    total_label.pack(side="left", padx=5)

    def update_total():
        lines = items_text.get("1.0", tk.END).splitlines()
        total = 0.0
        for line in lines:
            line = line.strip()
            if not line:
                continue
            parts = line.split("|")
            if len(parts) >= 4:
                price_str = parts[3].strip()
                total += normalize_price(price_str)
        total_var.set(f"{total:.2f}")

    def force_total():
        lines = items_text.get("1.0", tk.END).splitlines()
        total = 0.0
        for line in lines:
            line = line.strip()
            if not line:
                continue
            parts = line.split("|")
            if len(parts) >= 4:
                price_str = parts[3].strip()
                total += normalize_price(price_str)
        force_win = tk.Toplevel(win)
        force_win.title("Wymuś wartość zamówienia")
        force_win.geometry("300x150")
        force_win.attributes('-topmost', True)
        ttk.Label(force_win, text=f"Aktualna suma: {total:.2f} zł").pack(pady=5)
        ttk.Label(force_win, text="Docelowa wartość (zł):").pack()
        target_entry = ttk.Entry(force_win, width=15)
        target_entry.pack(pady=5)
        def apply_force():
            try:
                target = float(target_entry.get().replace(',', '.'))
            except:
                show_topmost_error("Błąd", "Nieprawidłowa liczba", parent=force_win)
                return
            diff = target - total
            if abs(diff) < 0.01:
                show_topmost_info("Info", "Wartość już się zgadza", parent=force_win)
                force_win.destroy()
                return
            if diff < 0:
                name = "Rabat wymuszony"
                barcode = "RABAT_FORCED"
            else:
                name = "Dopłata wymuszona"
                barcode = "DOPLATA_FORCED"
            add_product_line(barcode, name, "", diff)
            force_win.destroy()
            update_total()
            show_topmost_info("OK", f"Dodano pozycję korygującą: {diff:.2f} zł", parent=win)
        ttk.Button(force_win, text="Zastosuj", command=apply_force).pack(pady=10)
        safe_grab_window(force_win)
        force_win.wait_window()

    ttk.Button(total_frame, text="Wymuś wartość", command=force_total).pack(side="left", padx=10)

    temp_frame = ttk.Frame(scrollable_frame)
    temp_frame.pack(fill="x", padx=10, pady=5)
    ttk.Label(temp_frame, text="Skanuj kod:").pack(side="left")
    temp_entry = ttk.Entry(temp_frame, width=50)
    temp_entry.pack(side="left", padx=5)

    def add_product_line(barcode, name, oe, price):
        line = f"{barcode} | {name} | {oe} | {price}\n"
        items_text.insert(tk.END, line)
        update_total()
    win.add_product_line = add_product_line

    def process_barcode():
        barcode = temp_entry.get().strip()
        if not barcode:
            return
        cur = db.conn.cursor()
        cur.execute("SELECT name, oe_code, price, stock, side, position FROM products WHERE barcode=%s", (barcode,))
        row = cur.fetchone()
        if row:
            name, oe, price, stock, side, position = row
            add_product_line(barcode, name, oe, price)
            temp_entry.delete(0, tk.END)
        else:
            ans = ask_topmost_yesno("Nowy produkt", "Nie ma go w bazie. Czy dodać teraz?", parent=win)
            if not ans:
                return
            show_topmost_info("Info", "Dodaj produkt przez opcję 'Dodaj część Signeda' lub 'Dodaj ręcznie', a następnie wróć do zlecenia.", parent=win)
    temp_entry.bind("<Return>", lambda e: process_barcode())
    ttk.Button(temp_frame, text="Dodaj", command=process_barcode).pack(side="left", padx=5)

    def add_loose_item_create():
        sub = tk.Toplevel(win)
        sub.title("Dodaj pozycję niestandardową")
        sub.geometry("400x250")
        sub.attributes('-topmost', True)
        ttk.Label(sub, text="Nazwa pozycji:").pack(pady=5)
        name_entry2 = ttk.Entry(sub, width=50)
        name_entry2.pack(pady=5)
        ttk.Label(sub, text="Cena (zł):").pack(pady=5)
        price_entry2 = ttk.Entry(sub, width=20)
        price_entry2.pack(pady=5)
        def save_loose():
            cname = name_entry2.get().strip()
            if not cname:
                show_topmost_error("Błąd", "Nazwa wymagana", parent=sub)
                return
            try:
                cprice = float(price_entry2.get().replace(',', '.'))
            except:
                show_topmost_error("Błąd", "Nieprawidłowa cena", parent=sub)
                return
            add_product_line("CUSTOM", cname, "", cprice)
            sub.destroy()
        ttk.Button(sub, text="Zapisz", command=save_loose).pack(pady=10)
    ttk.Button(scrollable_frame, text="➕ Dodaj pozycję niestandardową", command=add_loose_item_create).pack(pady=5)

    def add_package_now():
        pkg = ask_package_details(win)
        if pkg:
            if not hasattr(win, 'pending_packages'):
                win.pending_packages = []
            win.pending_packages.append(pkg)
            show_topmost_info("OK", "Dodano paczkę do zamówienia", parent=win)
    ttk.Button(scrollable_frame, text="📦 Dodaj paczkę do zamówienia", command=add_package_now).pack(pady=5)

    def export_quote():
        lines = items_text.get("1.0", tk.END).splitlines()
        if not lines:
            show_topmost_warning("Uwaga", "Brak produktów do eksportu", parent=win)
            return
        customer = entries["Imię i nazwisko"].get().strip() or "brak"
        phone = entries["Telefon"].get().strip() or "brak"
        address = entries["Adres"].get().strip() or "brak"
        nip = entries["NIP"].get().strip() or "brak"
        email = entries["Email"].get().strip() or "brak"
        doc_type = doc_var.get() or "brak"
        delivery = delivery_var.get() or "brak"
        extra = extra_info_entry.get().strip() or "brak"
        sp_name = selected_salesperson_name
        cod_display = cod_var.get() or "brak"
        cod_text = "z wysyłką" if cod_display == "z_wysylka" else "plus wysyłka"

        header = f"WYCENA (przed zapisem)\n"
        header += f"Klient: {customer}\n"
        header += f"Telefon: {phone}\n"
        header += f"Adres: {address}\n"
        header += f"NIP: {nip}\n"
        header += f"Email: {email}\n"
        header += f"Handlowiec: {sp_name}\n"
        header += f"Typ dokumentu: {doc_type}\n"
        header += f"Typ dostawy: {delivery}\n"
        header += f"Pobranie: {cod_text}\n"
        header += f"Samochód/uwagi: {extra}\n"
        header += "-" * 50 + "\n"

        export_lines = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            parts = line.split("|")
            if len(parts) < 4:
                continue
            barcode = parts[0].strip()
            name = parts[1].strip()
            oe = parts[2].strip()
            price_str = parts[3].strip()
            cur = db.conn.cursor()
            cur.execute("SELECT side, position, product_type FROM products WHERE barcode=%s", (barcode,))
            row = cur.fetchone()
            if row:
                side, position, ptype = row
                miejsce = f"{side} {position}".strip() if side or position else "brak"
                if ptype == "nowe":
                    client_type = "Aftermarket"
                elif ptype in ("używane unikat", "używane wielokrotne"):
                    client_type = "Używany"
                elif ptype == "oryginał":
                    client_type = "Oryginał"
                else:
                    client_type = "Inny"
            else:
                miejsce = "brak"
                client_type = "Inny"
            clean_name = clean_product_name(barcode, name) if barcode not in ("CUSTOM", "RABAT_FORCED", "DOPLATA_FORCED") else name
            price_float = normalize_price(price_str)
            export_lines.append(f"{clean_name} | {miejsce} | {oe} | {client_type} | {price_float:.2f} zł")
        if not export_lines:
            return
        quote_text = header + "\n".join(export_lines)
        filename = f"wycena_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        with open(filename, 'w', encoding='utf-8') as f:
            f.write(quote_text)
        show_topmost_info("Eksport", f"Wycena zapisana jako {filename}\n(dostosuj ręcznie przed wysłaniem)", parent=win)
        win.clipboard_clear()
        win.clipboard_append(quote_text)
        show_topmost_info("Kopiowanie", "Wycena skopiowana do schowka", parent=win)
    ttk.Button(scrollable_frame, text="Eksportuj wycenę", command=export_quote).pack(pady=5)

    def save_order(hold=False):
        if not selected_salesperson_id:
            show_topmost_error("Błąd", "Brak przypisanego handlowca", parent=win)
            return
        if not entries["Imię i nazwisko"].get().strip():
            show_topmost_error("Błąd", "Imię i nazwisko wymagane", parent=win)
            return
        lines = items_text.get("1.0", tk.END).splitlines()
        products = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            parts = line.split("|")
            if len(parts) < 1:
                continue
            barcode = parts[0].strip()
            if barcode == "CUSTOM":
                if len(parts) >= 4:
                    name = parts[1].strip()
                    oe = parts[2].strip()
                    price = normalize_price(parts[3].strip())
                    products.append((barcode, name, price, 0, "", ""))
                continue
            if barcode in ("RABAT_FORCED", "DOPLATA_FORCED"):
                if len(parts) >= 4:
                    name = parts[1].strip()
                    price = normalize_price(parts[3].strip())
                else:
                    name = ""
                    price = 0.0
                products.append((barcode, name, price, 0, "", ""))
                continue
            cur = db.conn.cursor()
            cur.execute("SELECT name, price, stock, side, position FROM products WHERE barcode=%s", (barcode,))
            row = cur.fetchone()
            if not row:
                show_topmost_error("Błąd", f"Produkt {barcode} nie istnieje w bazie", parent=win)
                return
            name, price, stock, side, position = row
            to_order = 1 if stock == 0 else 0
            products.append((barcode, name, price, to_order, side, position))
        if not products:
            show_topmost_error("Błąd", "Brak produktów", parent=win)
            return
        sp_id = selected_salesperson_id
        sp_name = selected_salesperson_name
        worker_id = None
        worker_name = None
        if worker_combo and worker_combo.get():
            worker_id = int(worker_combo.get().split(":")[0])
            worker_name = worker_combo.get().split(": ")[1] if ": " in worker_combo.get() else ""
        extra_info = extra_info_entry.get().strip()
        email = entries["Email"].get().strip() or None
        cod_type = cod_var.get() or None

        customer_name = entries["Imię i nazwisko"].get().strip()
        customer_phone = entries["Telefon"].get().strip()
        customer_address = entries["Adres"].get().strip()
        customer_nip = entries["NIP"].get().strip()
        customer_email = entries["Email"].get().strip() or None
        db.add_or_update_customer(customer_name, customer_phone, customer_address, customer_nip, customer_email, None)

        cur = db.conn.cursor()
        status = "HOLD" if hold else "NEW"
        cur.execute("""
            INSERT INTO orders(customer_name, phone, address, nip, document_type, delivery_type,
                               status, shipping_free, salesperson_id, warehouse_worker_id,
                               extra_info, email, cod_type)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
        """, (entries["Imię i nazwisko"].get(), entries["Telefon"].get(), entries["Adres"].get(),
              entries["NIP"].get(), doc_var.get(), delivery_var.get(), status,
              0, sp_id, worker_id,
              extra_info, email, cod_type))
        order_id = cur.fetchone()[0]
        created_items = []
        total = 0.0
        for barcode, name, price, to_order, side, position in products:
            if barcode == "CUSTOM":
                cur.execute("""
                    INSERT INTO order_items(order_id, barcode, product_name, side, position, to_order, unit_price, custom_name)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
                """, (order_id, barcode, "", "", "", 0, price, name))
            elif barcode in ("RABAT_FORCED", "DOPLATA_FORCED"):
                cur.execute("""
                    INSERT INTO order_items(order_id, barcode, product_name, side, position, to_order, unit_price, custom_name)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
                """, (order_id, barcode, "", "", "", 0, price, name))
            else:
                cur.execute("""
                    INSERT INTO order_items(order_id, barcode, product_name, side, position, to_order, unit_price)
                    VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id
                """, (order_id, barcode, name, side, position, to_order, price))
            item_id = cur.fetchone()[0]
            created_items.append({"order_item_id": item_id, "barcode": barcode, "to_order": bool(to_order)})
            total += normalize_price(price)

        if hasattr(win, 'pending_packages') and win.pending_packages:
            for pkg in win.pending_packages:
                db.add_package(order_id, pkg['type'], pkg['weight'], pkg['length'], pkg['width'], pkg['height'])

        try:
            db.reserve_order_items(order_id, created_items, actor_id=CURRENT_USER_ID)
        except ValueError as exc:
            db.conn.rollback()
            show_topmost_warning("Brak dostępności", str(exc), parent=win)
            return

        cur.execute("UPDATE orders SET total_price=%s WHERE id=%s", (total, order_id))
        db.conn.commit()
        db.log_audit_event("order", order_id, "created", {"total_price": total, "status": status}, actor_id=CURRENT_USER_ID)
        db.conn.commit()
        refresh_stats(stats_label)
        if hold:
            show_topmost_info("AutoCore", f"Wycena zapisana jako zlecenie #{order_id} (status: wstrzymane)\nMożesz ją później zatwierdzić.", parent=win)
            show_quote_preview(order_id)
        else:
            show_topmost_info("AutoCore", f"Zlecenie #{order_id} zapisane\nWartość: {total:.2f} zł", parent=win)
            products_summary_lines = []
            for barcode, name, price, to_order, side, position in products:
                side_str = f" {side}" if side else ""
                pos_str = f" {position}" if position else ""
                clean_name = clean_product_name(barcode, name) if barcode not in ("CUSTOM", "RABAT_FORCED", "DOPLATA_FORCED") else name
                display_name = clean_name if barcode not in ("CUSTOM", "RABAT_FORCED", "DOPLATA_FORCED") else f"[{barcode}] {name}"
                products_summary_lines.append(f"- `{barcode}` {display_name}{side_str}{pos_str} – {price:.2f} zł")
            products_summary = "\n".join(products_summary_lines)
            if len(products) > 20:
                products_summary += f"\n... i {len(products)-20} więcej"
            order_data = {
                "order_id": order_id,
                "customer_name": entries["Imię i nazwisko"].get(),
                "phone": entries["Telefon"].get(),
                "delivery_type": delivery_var.get(),
                "salesperson": sp_name,
                "extra_info": extra_info,
                "products_summary": products_summary,
                "total": total
            }
            send_order_to_discord(order_data)
        win.destroy()
        global active_order_window
        active_order_window = None
    ttk.Button(scrollable_frame, text="Zatwierdź zlecenie", command=lambda: save_order(hold=False)).pack(pady=5)
    ttk.Button(scrollable_frame, text="Zapisz jako wycenę (wstrzymane)", command=lambda: save_order(hold=True)).pack(pady=5)
    def on_close():
        global active_order_window
        active_order_window = None
        win.destroy()
    win.protocol("WM_DELETE_WINDOW", on_close)

def edit_order_window(order_id, parent_win, stats_label, master_tree, category_cb, model_cb):
    global active_edit_window

    order = get_order_dict(order_id)
    if not order:
        show_topmost_error("Błąd", f"Nie znaleziono zamówienia #{order_id}")
        return

    win = tk.Toplevel(parent_win)
    active_edit_window = win
    win.title(f"Edytuj zlecenie #{order_id}")
    win.geometry("1000x900")
    win.attributes('-topmost', True)

    main_frame = ttk.Frame(win)
    main_frame.pack(fill="both", expand=True)

    canvas = tk.Canvas(main_frame)
    scrollbar = ttk.Scrollbar(main_frame, orient="vertical", command=canvas.yview)
    scrollable_frame = ttk.Frame(canvas)

    scrollable_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
    canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
    canvas.configure(yscrollcommand=scrollbar.set)

    canvas.pack(side="left", fill="both", expand=True)
    scrollbar.pack(side="right", fill="y")

    ttk.Label(scrollable_frame, text="Dane klienta").pack()
    frame_client = ttk.Frame(scrollable_frame)
    frame_client.pack(fill="x", padx=10, pady=5)

    ttk.Label(frame_client, text="Imię i nazwisko:").grid(row=0, column=0, sticky="e")
    name_entry = ttk.Entry(frame_client, width=40)
    name_entry.insert(0, safe_get(order, 'customer_name'))
    name_entry.grid(row=0, column=1, padx=5)

    ttk.Label(frame_client, text="Telefon:").grid(row=1, column=0, sticky="e")
    phone_entry = ttk.Entry(frame_client, width=40)
    phone_entry.insert(0, safe_get(order, 'phone'))
    phone_entry.grid(row=1, column=1, padx=5)

    ttk.Label(frame_client, text="Adres:").grid(row=2, column=0, sticky="e")
    address_entry = ttk.Entry(frame_client, width=40)
    address_entry.insert(0, safe_get(order, 'address'))
    address_entry.grid(row=2, column=1, padx=5)

    ttk.Label(frame_client, text="NIP:").grid(row=3, column=0, sticky="e")
    nip_entry = ttk.Entry(frame_client, width=40)
    nip_entry.insert(0, safe_get(order, 'nip'))
    nip_entry.grid(row=3, column=1, padx=5)

    ttk.Label(frame_client, text="Email:").grid(row=4, column=0, sticky="e")
    email_entry = ttk.Entry(frame_client, width=40)
    email_entry.insert(0, safe_get(order, 'email'))
    email_entry.grid(row=4, column=1, padx=5)

    frame_doc = ttk.Frame(scrollable_frame)
    frame_doc.pack(fill="x", padx=10, pady=5)
    ttk.Label(frame_doc, text="Typ dokumentu:").grid(row=0, column=0, sticky="e")
    doc_var = tk.StringVar(value=safe_get(order, 'document_type'))
    doc_combo = ttk.Combobox(frame_doc, textvariable=doc_var, values=["FAKTURA", "SPRZEDAŻ INNA"], width=30)
    doc_combo.grid(row=0, column=1, padx=5)
    ttk.Label(frame_doc, text="Typ dostawy:").grid(row=1, column=0, sticky="e")
    delivery_var = tk.StringVar(value=safe_get(order, 'delivery_type'))
    delivery_combo = ttk.Combobox(frame_doc, textvariable=delivery_var,
                                  values=["PACZKA", "PACZKA NIESTANDARDOWA", "PALETA", "PALETA NIESTANDARDOWA",
                                          "ODBIÓR OSOBISTY", "INNE"], width=30)
    delivery_combo.grid(row=1, column=1, padx=5)

    cod_var = tk.StringVar(value=safe_get(order, 'cod_type', 'z_wysylka'))
    ttk.Label(frame_doc, text="Pobranie:").grid(row=2, column=0, sticky="e")
    cod_frame = ttk.Frame(frame_doc)
    cod_frame.grid(row=2, column=1, sticky="w")
    ttk.Radiobutton(cod_frame, text="z wysyłką", variable=cod_var, value="z_wysylka").pack(side="left", padx=5)
    ttk.Radiobutton(cod_frame, text="plus wysyłka", variable=cod_var, value="plus_wysylka").pack(side="left", padx=5)

    ttk.Label(scrollable_frame, text="Samochód / uwagi:").pack(anchor='w', padx=10)
    extra_entry = ttk.Entry(scrollable_frame, width=80)
    extra_entry.insert(0, safe_get(order, 'extra_info'))
    extra_entry.pack(padx=10, pady=5)

    frame_people = ttk.Frame(scrollable_frame)
    frame_people.pack(fill="x", padx=10, pady=5)
    ttk.Label(frame_people, text="Handlowiec:").grid(row=0, column=0, sticky="e")
    salespersons = db.get_salespersons()
    sp_names = [f"{pid}: {name}" for pid, name in salespersons]
    sp_var = tk.StringVar()
    sp_combo = ttk.Combobox(frame_people, textvariable=sp_var, values=sp_names, state="readonly", width=40)
    if order.get('salesperson_id'):
        for sp in salespersons:
            if sp[0] == order['salesperson_id']:
                sp_var.set(f"{sp[0]}: {sp[1]}")
                break
    sp_combo.grid(row=0, column=1, padx=5)
    ttk.Label(frame_people, text="Magazynier:").grid(row=1, column=0, sticky="e")
    workers = db.get_warehouse_workers()
    w_names = [f"{pid}: {name}" for pid, name in workers]
    w_var = tk.StringVar()
    w_combo = ttk.Combobox(frame_people, textvariable=w_var, values=w_names, state="readonly", width=40)
    if order.get('warehouse_worker_id'):
        for w in workers:
            if w[0] == order['warehouse_worker_id']:
                w_var.set(f"{w[0]}: {w[1]}")
                break
    w_combo.grid(row=1, column=1, padx=5)

    ttk.Label(scrollable_frame, text="Produkty (edytuj listę)").pack(anchor='w', padx=10)
    items_frame = ttk.Frame(scrollable_frame)
    items_frame.pack(fill="both", expand=True, padx=10, pady=5)
    items_tree = ttk.Treeview(items_frame, columns=("Kod", "Nazwa", "Strona", "Pozycja", "Skontrolowany", "Do zamówienia", "Cena"), show="headings")
    for col in ("Kod", "Nazwa", "Strona", "Pozycja", "Skontrolowany", "Do zamówienia", "Cena"):
        items_tree.heading(col, text=col)
        items_tree.column(col, width=100)
    items_tree.pack(fill="both", expand=True)

    cur = db.conn.cursor()
    cur.execute("""
        SELECT oi.id, oi.barcode,
               COALESCE(oi.custom_name, p.name) AS name,
               oi.side, oi.position, oi.picked, oi.to_order,
               COALESCE(oi.unit_price, p.price) AS price,
               oi.unit_price, oi.custom_name
        FROM order_items oi
        LEFT JOIN products p ON oi.barcode = p.barcode
        WHERE oi.order_id = %s
    """, (order_id,))
    items = cur.fetchall()
    for it in items:
        items_tree.insert("", "end", iid=it[0],
                          values=(it[1], it[2], it[3] or "", it[4] or "",
                                  "TAK" if it[5] else "NIE", "TAK" if it[6] else "",
                                  f"{normalize_price(it[7]):.2f}"))

    total_frame = ttk.Frame(scrollable_frame)
    total_frame.pack(fill="x", padx=10, pady=5)
    ttk.Label(total_frame, text="Suma: ").pack(side="left")
    total_var = tk.StringVar(value="0.00")
    total_label = ttk.Label(total_frame, textvariable=total_var, font=("Arial", 12, "bold"))
    total_label.pack(side="left", padx=5)

    def update_total_edit():
        total = 0.0
        for item in items_tree.get_children():
            values = items_tree.item(item)['values']
            price_str = values[6].replace(" zł", "").strip()
            total += normalize_price(price_str)
        total_var.set(f"{total:.2f}")

    def force_total_edit():
        total = 0.0
        for item in items_tree.get_children():
            values = items_tree.item(item)['values']
            price_str = values[6].replace(" zł", "").strip()
            total += normalize_price(price_str)
        force_win = tk.Toplevel(win)
        force_win.title("Wymuś wartość zamówienia")
        force_win.geometry("300x150")
        force_win.attributes('-topmost', True)
        ttk.Label(force_win, text=f"Aktualna suma: {total:.2f} zł").pack(pady=5)
        ttk.Label(force_win, text="Docelowa wartość (zł):").pack()
        target_entry = ttk.Entry(force_win, width=15)
        target_entry.pack(pady=5)
        def apply_force_edit():
            try:
                target = float(target_entry.get().replace(',', '.'))
            except:
                show_topmost_error("Błąd", "Nieprawidłowa liczba", parent=force_win)
                return
            diff = target - total
            if abs(diff) < 0.01:
                show_topmost_info("Info", "Wartość już się zgadza", parent=force_win)
                force_win.destroy()
                return
            if diff < 0:
                name = "Rabat wymuszony"
                barcode = "RABAT_FORCED"
            else:
                name = "Dopłata wymuszona"
                barcode = "DOPLATA_FORCED"
            cur2 = db.conn.cursor()
            cur2.execute("""
                INSERT INTO order_items(order_id, barcode, product_name, side, position, to_order, unit_price, custom_name)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
            """, (order_id, barcode, "", "", "", 0, diff, name))
            new_id = cur2.fetchone()[0]
            db.conn.commit()
            items_tree.insert("", "end", iid=new_id,
                              values=(barcode, name, "", "", "NIE", "", f"{diff:.2f}"))
            update_total_edit()
            force_win.destroy()
            show_topmost_info("OK", f"Dodano pozycję korygującą: {diff:.2f} zł", parent=win)
        ttk.Button(force_win, text="Zastosuj", command=apply_force_edit).pack(pady=10)
        safe_grab_window(force_win)
        force_win.wait_window()

    ttk.Button(total_frame, text="Wymuś wartość", command=force_total_edit).pack(side="left", padx=10)

    add_frame = ttk.Frame(scrollable_frame)
    add_frame.pack(fill="x", padx=10, pady=5)
    ttk.Label(add_frame, text="Dodaj produkt (kod):").pack(side="left")
    add_entry = ttk.Entry(add_frame, width=30)
    add_entry.pack(side="left", padx=5)

    def add_product(barcode=None, name=None, oe=None, price=None):
        if barcode is None:
            barcode = add_entry.get().strip()
        if not barcode:
            return
        cur2 = db.conn.cursor()
        if barcode == "CUSTOM":
            return
        cur2.execute("SELECT name, side, position, price, stock FROM products WHERE barcode=%s", (barcode,))
        prod = cur2.fetchone()
        if not prod:
            show_topmost_error("Błąd", f"Produkt {barcode} nie istnieje w bazie", parent=win)
            return
        prod_name, side, position, price, stock = prod
        to_order = 1 if stock == 0 else 0
        cur2.execute("""
            INSERT INTO order_items(order_id, barcode, product_name, side, position, to_order, unit_price)
            VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id
        """, (order_id, barcode, prod_name, side, position, to_order, price))
        new_id = cur2.fetchone()[0]
        db.conn.commit()
        items_tree.insert("", "end", iid=new_id,
                          values=(barcode, prod_name, side or "", position or "",
                                  "NIE", "TAK" if to_order else "", f"{price:.2f}"))
        add_entry.delete(0, tk.END)
        update_total_edit()

    win.add_product = add_product

    ttk.Button(add_frame, text="Dodaj produkt", command=add_product).pack(side="left")

    def add_loose_item():
        sub = tk.Toplevel(win)
        sub.title("Dodaj pozycję niestandardową")
        sub.geometry("400x250")
        sub.attributes('-topmost', True)
        ttk.Label(sub, text="Nazwa pozycji:").pack(pady=5)
        name_entry2 = ttk.Entry(sub, width=50)
        name_entry2.pack(pady=5)
        ttk.Label(sub, text="Cena (zł):").pack(pady=5)
        price_entry2 = ttk.Entry(sub, width=20)
        price_entry2.pack(pady=5)
        def save_loose():
            cname = name_entry2.get().strip()
            if not cname:
                show_topmost_error("Błąd", "Nazwa wymagana", parent=sub)
                return
            try:
                cprice = float(price_entry2.get().replace(',', '.'))
            except:
                show_topmost_error("Błąd", "Nieprawidłowa cena", parent=sub)
                return
            cur2 = db.conn.cursor()
            cur2.execute("""
                INSERT INTO order_items(order_id, barcode, product_name, side, position, to_order, unit_price, custom_name)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
            """, (order_id, "CUSTOM", "", "", "", 0, cprice, cname))
            new_id = cur2.fetchone()[0]
            db.conn.commit()
            items_tree.insert("", "end", iid=new_id,
                              values=("CUSTOM", cname, "", "", "NIE", "", f"{cprice:.2f}"))
            sub.destroy()
            update_total_edit()
        ttk.Button(sub, text="Zapisz", command=save_loose).pack(pady=10)
    ttk.Button(add_frame, text="➕ Dodaj pozycję niestandardową", command=add_loose_item).pack(side="left", padx=10)

    def add_package_to_order():
        pkg = ask_package_details(win)
        if pkg:
            db.add_package(order_id, pkg['type'], pkg['weight'], pkg['length'], pkg['width'], pkg['height'])
            show_topmost_info("OK", "Dodano paczkę do zamówienia", parent=win)
    ttk.Button(add_frame, text="📦 Dodaj paczkę", command=add_package_to_order).pack(side="left", padx=10)

    def remove_product():
        sel = items_tree.selection()
        if not sel:
            return
        item_id = int(sel[0])
        if ask_topmost_yesno("Usuń produkt", f"Czy usunąć produkt z zamówienia?", parent=win):
            cur.execute("DELETE FROM order_items WHERE id=%s", (item_id,))
            db.conn.commit()
            items_tree.delete(sel[0])
            update_total_edit()
    ttk.Button(add_frame, text="Usuń zaznaczony produkt", command=remove_product).pack(side="left", padx=10)

    def export_quote():
        customer = name_entry.get().strip() or "brak"
        phone = phone_entry.get().strip() or "brak"
        address = address_entry.get().strip() or "brak"
        nip = nip_entry.get().strip() or "brak"
        email = email_entry.get().strip() or "brak"
        doc_type = doc_var.get() or "brak"
        delivery = delivery_var.get() or "brak"
        extra = extra_entry.get().strip() or "brak"
        sp_name = sp_combo.get().split(": ")[1] if ": " in sp_combo.get() else "brak"
        cod_display = cod_var.get() or "brak"
        cod_text = "z wysyłką" if cod_display == "z_wysylka" else "plus wysyłka"

        header = f"EDYCJA ZLECENIA #{order_id}\n"
        header += f"Klient: {customer}\n"
        header += f"Telefon: {phone}\n"
        header += f"Adres: {address}\n"
        header += f"NIP: {nip}\n"
        header += f"Email: {email}\n"
        header += f"Handlowiec: {sp_name}\n"
        header += f"Typ dokumentu: {doc_type}\n"
        header += f"Typ dostawy: {delivery}\n"
        header += f"Pobranie: {cod_text}\n"
        header += f"Samochód/uwagi: {extra}\n"
        header += "-" * 50 + "\n"

        export_lines = []
        for item in items_tree.get_children():
            values = items_tree.item(item)['values']
            barcode = values[0]
            name = values[1]
            cur2 = db.conn.cursor()
            cur2.execute("SELECT oe_code, side, position, product_type FROM products WHERE barcode=%s", (barcode,))
            prod = cur2.fetchone()
            if prod:
                oe = prod[0] or "brak"
                side = prod[1] or ""
                position = prod[2] or ""
                ptype = prod[3]
                miejsce = f"{side} {position}".strip() if side or position else "brak"
                if ptype == "nowe":
                    client_type = "Aftermarket"
                elif ptype in ("używane unikat", "używane wielokrotne"):
                    client_type = "Używany"
                elif ptype == "oryginał":
                    client_type = "Oryginał"
                else:
                    client_type = "Inny"
            else:
                oe = "brak"
                miejsce = "brak"
                client_type = "Inny"
            price_str = values[6].replace(" zł", "").strip()
            price = normalize_price(price_str)
            clean_name = clean_product_name(barcode, name) if barcode not in ("CUSTOM", "RABAT_FORCED", "DOPLATA_FORCED") else name
            export_lines.append(f"{clean_name} | {miejsce} | {oe} | {client_type} | {price:.2f} zł")
        if not export_lines:
            show_topmost_warning("Uwaga", "Brak produktów do eksportu", parent=win)
            return
        quote_text = header + "\n".join(export_lines)
        filename = f"wycena_edycja_{order_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        with open(filename, 'w', encoding='utf-8') as f:
            f.write(quote_text)
        show_topmost_info("Eksport", f"Wycena zapisana jako {filename}", parent=win)
        win.clipboard_clear()
        win.clipboard_append(quote_text)
        show_topmost_info("Kopiowanie", "Wycena skopiowana do schowka", parent=win)
    ttk.Button(scrollable_frame, text="Eksportuj wycenę", command=export_quote).pack(pady=5)

    def save_order_changes():
        new_sp_id = None
        if sp_combo.get():
            new_sp_id = int(sp_combo.get().split(":")[0])
        new_worker_id = None
        if w_combo.get():
            new_worker_id = int(w_combo.get().split(":")[0])
        cur.execute("""
            UPDATE orders SET
                customer_name=%s, phone=%s, address=%s, nip=%s, document_type=%s, delivery_type=%s,
                shipping_free=%s, extra_info=%s, salesperson_id=%s, warehouse_worker_id=%s, email=%s, cod_type=%s
            WHERE id=%s
        """, (name_entry.get(), phone_entry.get(), address_entry.get(), nip_entry.get(),
              doc_var.get(), delivery_var.get(),
              0,
              extra_entry.get(), new_sp_id, new_worker_id, email_entry.get() or None, cod_var.get() or None, order_id))
        total = 0.0
        for item in items_tree.get_children():
            price_str = items_tree.item(item)['values'][6].replace(" zł", "").strip()
            total += normalize_price(price_str)
        cur.execute("UPDATE orders SET total_price=%s WHERE id=%s", (total, order_id))
        db.conn.commit()
        refresh_stats(stats_label)
        show_topmost_info("OK", "Zlecenie zaktualizowane", parent=win)
        win.destroy()
        parent_win.destroy()
        show_order_details(order_id, parent_win, stats_label, master_tree, category_cb, model_cb)
    ttk.Button(scrollable_frame, text="Zapisz zmiany", command=save_order_changes).pack(pady=10)

    update_total_edit()

    def on_close():
        global active_edit_window
        active_edit_window = None
        win.destroy()
    win.protocol("WM_DELETE_WINDOW", on_close)

def orders_list_window(root, stats_label, master_tree, category_cb, model_cb):
    win = tk.Toplevel(root)
    win.title("Zlecenia")
    win.geometry("1200x600")
    win.attributes('-topmost', True)
    filter_frame = ttk.Frame(win)
    filter_frame.pack(fill="x", padx=10, pady=5)
    filter_frame_top = ttk.Frame(win)
    filter_frame_top.pack(fill="x", padx=10, pady=5)
    ttk.Label(filter_frame_top, text="Handlowiec:").pack(side="left")
    salespersons = db.get_salespersons()
    sp_names = ["Wszyscy"] + [f"{pid}: {name}" for pid, name in salespersons]
    sp_filter_var = tk.StringVar(value="Wszyscy")
    sp_combo = ttk.Combobox(filter_frame_top, textvariable=sp_filter_var, values=sp_names, state="readonly", width=30)
    sp_combo.pack(side="left", padx=5)

    def status_color(status):
        if status == "NEW":
            return "green"
        if status == "READY":
            return "blue"
        if status == "HOLD":
            return "yellow"
        return "red"

    def show_status(status):
        for row in tree.get_children():
            tree.delete(row)
        cur = db.conn.cursor()
        query = "SELECT id, customer_name, phone, delivery_type, status FROM orders"
        params = []
        conditions = []
        if status:
            conditions.append("status=%s")
            params.append(status)
        selected_sp = sp_filter_var.get()
        if selected_sp != "Wszyscy":
            sp_id = int(selected_sp.split(":")[0])
            conditions.append("salesperson_id=%s")
            params.append(sp_id)
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        try:
            cur.execute(query, params)
            rows = cur.fetchall()
            for row in rows:
                order_id = row[0]
                cur2 = db.conn.cursor()
                cur2.execute("SELECT COUNT(*) FROM order_items WHERE order_id=%s", (order_id,))
                total_items = cur2.fetchone()[0]
                cur2.execute("SELECT COUNT(*) FROM order_items WHERE order_id=%s AND picked=1", (order_id,))
                picked_items = cur2.fetchone()[0]
                if total_items == 0:
                    tag = 'red'
                elif picked_items == total_items:
                    tag = 'green'
                else:
                    tag = 'yellow'
                tree.insert("", "end", values=row, tags=(tag,))
        except Exception as exc:
            show_topmost_error("Błąd bazy", f"Nie udało się pobrać zleceń:\n{exc}", parent=win)

    ttk.Button(filter_frame, text="Wszystkie", command=lambda: show_status(None)).pack(side="left", padx=2)
    ttk.Button(filter_frame, text="Nowe (NEW)", command=lambda: show_status("NEW")).pack(side="left", padx=2)
    ttk.Button(filter_frame, text="Gotowe (READY)", command=lambda: show_status("READY")).pack(side="left", padx=2)
    ttk.Button(filter_frame, text="Wstrzymane (HOLD)", command=lambda: show_status("HOLD")).pack(side="left", padx=2)
    ttk.Button(filter_frame, text="Archiwum (ARCHIVED)", command=lambda: show_status("ARCHIVED")).pack(side="left", padx=2)
    columns = ("ID", "Klient", "Telefon", "Dostawa", "Status")
    tree = ttk.Treeview(win, columns=columns, show="headings")
    for col in columns:
        tree.heading(col, text=col)
        tree.column(col, width=150)
    tree.pack(fill="both", expand=True, padx=10, pady=5)
    tree.tag_configure('green', background='#d8ffd8')
    tree.tag_configure('yellow', background='#fff7c7')
    tree.tag_configure('red', background='#ffd8d8')
    tree.tag_configure('blue', background='#cce5ff')
    def on_double_click(event):
        sel = tree.selection()
        if sel:
            order_id = tree.item(sel[0])["values"][0]
            show_order_details(order_id, win, stats_label, master_tree, category_cb, model_cb)
    tree.bind("<Double-1>", on_double_click)
    def on_sp_filter(*args):
        show_status(None)
    sp_filter_var.trace("w", on_sp_filter)
    show_status("NEW")

def show_order_details(order_id, parent_win=None, stats_label=None, master_tree=None, category_cb=None, model_cb=None):
    win = tk.Toplevel(parent_win if parent_win else root)
    win.title(f"Zlecenie #{order_id}")
    win.geometry("1100x850")
    win.attributes('-topmost', True)
    cur = db.conn.cursor()

    order = get_order_dict(order_id)
    if not order:
        win.destroy()
        return

    info_frame = ttk.LabelFrame(win, text="Dane zamówienia")
    info_frame.pack(fill="x", padx=10, pady=5)
    ttk.Label(info_frame, text=f"ID: {order_id}").grid(row=0, column=0, sticky="w")
    ttk.Label(info_frame, text=f"Klient: {safe_get(order, 'customer_name')}").grid(row=0, column=1, sticky="w")
    ttk.Label(info_frame, text=f"Tel: {safe_get(order, 'phone')}").grid(row=1, column=0, sticky="w")
    ttk.Label(info_frame, text=f"Dostawa: {safe_get(order, 'delivery_type')}").grid(row=1, column=1, sticky="w")
    ttk.Label(info_frame, text=f"Status: {safe_get(order, 'status')}").grid(row=2, column=0, sticky="w")
    ttk.Label(info_frame, text=f"Email: {safe_get(order, 'email', 'brak')}").grid(row=2, column=1, sticky="w")
    cod_display = safe_get(order, 'cod_type', 'brak')
    cod_text = "z wysyłką" if cod_display == "z_wysylka" else "plus wysyłka" if cod_display == "plus_wysylka" else cod_display
    ttk.Label(info_frame, text=f"Pobranie: {cod_text}").grid(row=3, column=0, sticky="w")
    if order.get('extra_info'):
        ttk.Label(info_frame, text=f"Samochód / uwagi: {safe_get(order, 'extra_info')}").grid(row=4, column=0, columnspan=2, sticky="w")

    worker_id = order.get('warehouse_worker_id')
    worker_name = ""
    if worker_id:
        cur.execute("SELECT name FROM warehouse_workers WHERE id=%s", (worker_id,))
        w = cur.fetchone()
        if w:
            worker_name = w[0]
    ttk.Label(info_frame, text=f"Magazynier: {worker_name}").grid(row=4, column=0, sticky="w")

    packages = db.get_packages(order_id)
    if packages:
        pkg_text = []
        for i, (pid, pkg_type, weight, length, width, height) in enumerate(packages, 1):
            dims = []
            if length: dims.append(f"{length}cm")
            if width: dims.append(f"{width}cm")
            if height: dims.append(f"{height}cm")
            dims_str = " x ".join(dims) if dims else "brak wymiarów"
            weight_str = f"{weight}kg" if weight else "brak wagi"
            type_str = pkg_type if pkg_type else "PACZKA"
            pkg_text.append(f"Paczka {i} ({type_str}): {dims_str}, {weight_str}")
        ttk.Label(info_frame, text="\n".join(pkg_text), wraplength=500).grid(row=5, column=0, columnspan=2, sticky="w", pady=5)

    items_frame = ttk.LabelFrame(win, text="Produkty")
    items_frame.pack(fill="both", expand=True, padx=10, pady=5)
    columns = ("Kod", "Nazwa", "Strona", "Pozycja", "Skontrolowany", "Do zamówienia", "Źródło", "Cena")
    tree_items = ttk.Treeview(items_frame, columns=columns, show="headings")
    for col in columns:
        tree_items.heading(col, text=col)
        tree_items.column(col, width=110)
    tree_items.pack(fill="both", expand=True)

    scan_frame = ttk.Frame(win)
    scan_frame.pack(fill="x", padx=10, pady=5)
    ttk.Label(scan_frame, text="Zeskanuj kod produktu:").pack(side="left")
    scan_entry = ttk.Entry(scan_frame, width=40)
    scan_entry.pack(side="left", padx=5)
    focus_scanner_entry(win, scan_entry)
    scan_result_var = tk.StringVar(value="")
    ttk.Label(scan_frame, textvariable=scan_result_var).pack(side="left", padx=10)

    def mark_barcode_as_picked(barcode):
        barcode = normalize_barcode(barcode)
        cur.execute("SELECT id, picked FROM order_items WHERE order_id=%s AND UPPER(barcode)=UPPER(%s)", (order_id, barcode))
        item = cur.fetchone()
        if not item:
            return False, "Produkt nie znajduje się na zleceniu"
        item_id, picked = item
        if picked:
            return False, "Produkt już jest skompletowany"
        cur.execute("SELECT stock, name FROM products WHERE UPPER(barcode)=UPPER(%s)", (barcode,))
        stock_row = cur.fetchone()
        if stock_row:
            stock, name = stock_row
            if stock <= 0:
                if not ask_topmost_yesno("Brak na stanie", f"Produkt {name} ma stan 0. Oznaczyć jako skompletowany?", parent=win):
                    return False, "Anulowano"
        db.consume_order_item_reservation(item_id, actor_id=CURRENT_USER_ID)
        if barcode not in ("RABAT", "CUSTOM", "RABAT_FORCED", "DOPLATA_FORCED"):
            db.archive_sold_unique_product(barcode, actor_id=CURRENT_USER_ID)
        cur.execute("UPDATE order_items SET picked=1 WHERE id=%s", (item_id,))
        db.conn.commit()
        if stats_label:
            refresh_stats(stats_label)
        return True, f"Produkt {barcode} oznaczono jako skompletowany"

    def scan_barcode(event=None):
        barcode = normalize_barcode(scan_entry.get().strip())
        if not barcode:
            return
        ok, message = mark_barcode_as_picked(barcode)
        scan_result_var.set(message)
        scan_entry.delete(0, tk.END)
        if ok:
            win.destroy()
            show_order_details(order_id, parent_win, stats_label, master_tree, category_cb, model_cb)

    bind_scan_submit(scan_entry, scan_barcode)
    ttk.Button(scan_frame, text="Skanuj", command=scan_barcode).pack(side="left")

    cur.execute("""
        SELECT oi.id, oi.barcode,
               COALESCE(oi.custom_name, oi.product_name, p.name) AS name,
               oi.side, oi.position, oi.picked, oi.to_order,
               COALESCE(oi.unit_price, p.price) AS price,
               p.barcode AS product_barcode
        FROM order_items oi
        LEFT JOIN products p ON oi.barcode = p.barcode
        WHERE oi.order_id = %s
    """, (order_id,))
    items = cur.fetchall()
    total_sum = 0.0
    for item in items:
        picked_str = "TAK" if item[5] else "NIE"
        to_order_str = "TAK" if item[6] else ""
        price = normalize_price(item[7])
        total_sum += price
        source_str = "manual" if item[8] is None else "db"
        tree_items.insert("", "end", iid=item[0],
                          values=(item[1], item[2], item[3] or "", item[4] or "",
                                  picked_str, to_order_str, source_str, f"{price:.2f}"))

    sum_frame = ttk.Frame(win)
    sum_frame.pack(fill="x", padx=10, pady=5)
    ttk.Label(sum_frame, text=f"Suma brutto: {total_sum:.2f} zł").pack(side="left", padx=10)
    ttk.Label(sum_frame, text="Rabat (kwota):").pack(side="left")
    discount_var = tk.StringVar(value=str(order.get('discount', 0)))
    discount_entry = ttk.Entry(sum_frame, textvariable=discount_var, width=10)
    discount_entry.pack(side="left", padx=5)
    final_price_var = tk.StringVar(value=f"{total_sum - (order.get('discount', 0)):.2f}")
    ttk.Label(sum_frame, text="Do zapłaty:").pack(side="left", padx=10)
    ttk.Label(sum_frame, textvariable=final_price_var).pack(side="left")

    def apply_discount():
        try:
            disc = float(discount_var.get().replace(',', '.'))
        except:
            disc = 0
        final = total_sum - disc
        if final < 0:
            final = 0
        final_price_var.set(f"{final:.2f}")
        cur.execute("SELECT id FROM order_items WHERE order_id=%s AND barcode='RABAT'", (order_id,))
        existing = cur.fetchone()
        if existing:
            cur.execute("UPDATE order_items SET unit_price=%s WHERE id=%s", (-disc, existing[0]))
        else:
            cur.execute("""
                INSERT INTO order_items(order_id, barcode, product_name, side, position, picked, to_order, unit_price, custom_name)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (order_id, "RABAT", "Rabat", "", "", 1, 0, -disc, "Rabat"))
        cur.execute("UPDATE orders SET discount=%s, total_price=%s WHERE id=%s", (disc, final, order_id))
        db.conn.commit()
        win.destroy()
        show_order_details(order_id, parent_win, stats_label, master_tree, category_cb, model_cb)
    ttk.Button(sum_frame, text="Nalicz rabat", command=apply_discount).pack(side="left", padx=5)

    btn_frame = ttk.Frame(win)
    btn_frame.pack(fill="x", pady=10)

    def toggle_picked():
        sel = tree_items.selection()
        if not sel:
            show_topmost_warning("Uwaga", "Wybierz produkt", parent=win)
            return
        item_id = int(sel[0])
        cur.execute("SELECT picked, barcode FROM order_items WHERE id=%s", (item_id,))
        current = cur.fetchone()
        if current[0]:
            show_topmost_info("Info", "Już skontrolowany", parent=win)
            return
        barcode = normalize_barcode(current[1])
        if barcode not in ("RABAT", "CUSTOM", "RABAT_FORCED", "DOPLATA_FORCED"):
            cur.execute("SELECT stock, name FROM products WHERE UPPER(barcode)=UPPER(%s)", (barcode,))
            stock_row = cur.fetchone()
            if not stock_row:
                show_topmost_error("Błąd", "Brak produktu", parent=win)
                return
            stock, name = stock_row
            if stock <= 0:
                if not ask_topmost_yesno("Brak na stanie", f"Produkt {name} ma stan 0. Czy mimo to oznaczyć jako skompletowany?", parent=win):
                    return
        db.consume_order_item_reservation(item_id, actor_id=CURRENT_USER_ID)
        cur.execute("UPDATE order_items SET picked=1 WHERE id=%s", (item_id,))
        db.conn.commit()
        if stats_label:
            refresh_stats(stats_label)
        win.destroy()
        show_order_details(order_id, parent_win, stats_label, master_tree, category_cb, model_cb)
    ttk.Button(btn_frame, text="Odkliknij produkt", command=toggle_picked).pack(side="left", padx=5)

    def mark_ready():
        cur.execute("SELECT COUNT(*) FROM order_items WHERE order_id=%s", (order_id,))
        total = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM order_items WHERE order_id=%s AND picked=1 AND barcode NOT IN ('RABAT','CUSTOM','RABAT_FORCED','DOPLATA_FORCED')", (order_id,))
        picked = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM order_items WHERE order_id=%s AND barcode NOT IN ('RABAT','CUSTOM','RABAT_FORCED','DOPLATA_FORCED')", (order_id,))
        total_real = cur.fetchone()[0]
        if total_real == 0:
            show_topmost_warning("Uwaga", "Brak produktów (poza rabatem/niestandardowymi)", parent=win)
            return
        if picked != total_real:
            show_topmost_warning("Uwaga", f"Brakuje {total_real-picked} produktów", parent=win)
            return

        packages = db.get_packages(order_id)
        if not packages:
            if ask_topmost_yesno("Brak paczek", "Nie dodano żadnych paczek. Czy chcesz dodać paczkę przed wysłaniem podsumowania?", parent=win):
                pkg = ask_package_details(win)
                if pkg:
                    db.add_package(order_id, pkg['type'], pkg['weight'], pkg['length'], pkg['width'], pkg['height'])
                    show_topmost_info("OK", "Dodano paczkę", parent=win)
                else:
                    show_topmost_warning("Uwaga", "Nie dodano paczki. Podsumowanie zostanie wysłane bez paczek.", parent=win)

        cur.execute("UPDATE orders SET status='READY' WHERE id=%s", (order_id,))
        db.conn.commit()
        if stats_label:
            refresh_stats(stats_label)
        cur.execute("""
            SELECT total_price, shipping_free, address, phone, delivery_type, warehouse_worker_id, customer_name,
                   document_type, cod_type, email, nip
            FROM orders WHERE id=%s
        """, (order_id,))
        row = cur.fetchone()
        if row:
            total_price = row[0] if row[0] else 0
            shipping_free = row[1] == 1
            address = row[2] or ""
            phone = row[3] or ""
            delivery_type = row[4] or ""
            worker_id = row[5]
            customer_name = row[6] or ""
            invoice_type = row[7] if len(row) > 7 else ""
            cod_type = row[8] if len(row) > 8 else None
            email = row[9] if len(row) > 9 else None
            nip = row[10] if len(row) > 10 else ""
            worker_name = ""
            if worker_id:
                cur.execute("SELECT name FROM warehouse_workers WHERE id=%s", (worker_id,))
                w = cur.fetchone()
                if w:
                    worker_name = w[0]

            packages_text = db.get_packages_text(order_id)

            product_lines = []
            for item in items:
                barcode = item[1]
                name = item[2]
                side = item[3] or ""
                position = item[4] or ""
                price = normalize_price(item[7])
                clean_name = clean_product_name(barcode, name) if barcode not in ("CUSTOM", "RABAT_FORCED", "DOPLATA_FORCED") else name
                display_name = clean_name if barcode not in ("CUSTOM", "RABAT_FORCED", "DOPLATA_FORCED") else f"[{barcode}] {name}"
                side_str = f" {side}" if side else ""
                pos_str = f" {position}" if position else ""
                product_lines.append(f"- `{barcode}` {display_name}{side_str}{pos_str} – {price:.2f} zł")
            products_text = "\n".join(product_lines)

            send_summary_to_discord(order_id, worker_name, packages_text, total_price,
                                    shipping_free, customer_name, address, phone, delivery_type,
                                    invoice_type, cod_type, email, nip, products_text)
        show_topmost_info("AutoCore", "Zlecenie gotowe", parent=win)
        win.destroy()
        if parent_win:
            parent_win.destroy()
    ttk.Button(btn_frame, text="Zlecenie gotowe", command=mark_ready).pack(side="left", padx=5)

    def archive():
        db.release_order_reservations_if_needed(order_id, "ARCHIVED", actor_id=CURRENT_USER_ID)
        cur.execute("UPDATE orders SET status='ARCHIVED' WHERE id=%s", (order_id,))
        db.conn.commit()
        if stats_label:
            refresh_stats(stats_label)
        show_topmost_info("AutoCore", "Archiwum", parent=win)
        win.destroy()
        if parent_win:
            parent_win.destroy()
    ttk.Button(btn_frame, text="Archiwizuj", command=archive).pack(side="left", padx=5)

    def delete_order():
        if ask_topmost_yesno("Usuń zlecenie", f"Czy usunąć zlecenie #{order_id}?", parent=win):
            db.release_order_reservations(order_id, actor_id=CURRENT_USER_ID)
            cur.execute("DELETE FROM order_items WHERE order_id=%s", (order_id,))
            cur.execute("DELETE FROM packages WHERE order_id=%s", (order_id,))
            cur.execute("DELETE FROM orders WHERE id=%s", (order_id,))
            db.conn.commit()
            if stats_label:
                refresh_stats(stats_label)
            show_topmost_info("AutoCore", "Zlecenie usunięte", parent=win)
            win.destroy()
            if parent_win:
                parent_win.destroy()
    ttk.Button(btn_frame, text="Usuń zlecenie", command=delete_order).pack(side="left", padx=5)

    def edit_order():
        edit_order_window(order_id, win, stats_label, master_tree, category_cb, model_cb)
    ttk.Button(btn_frame, text="Edytuj zlecenie", command=edit_order).pack(side="left", padx=5)

    def manage_packages():
        manage_packages_window(order_id, win)
    ttk.Button(btn_frame, text="Zarządzaj paczkami", command=manage_packages).pack(side="left", padx=5)

    def add_package_quick():
        pkg = ask_package_details(win)
        if pkg:
            db.add_package(order_id, pkg['type'], pkg['weight'], pkg['length'], pkg['width'], pkg['height'])
            show_topmost_info("OK", "Dodano paczkę", parent=win)
            win.destroy()
            show_order_details(order_id, parent_win, stats_label, master_tree, category_cb, model_cb)
    ttk.Button(btn_frame, text="➕ Dodaj paczkę", command=add_package_quick).pack(side="left", padx=5)

    def resend():
        resend_order_to_discord(order_id, win)
    ttk.Button(btn_frame, text="Wyślij ponownie na Discord", command=resend).pack(side="left", padx=5)

    if order.get('status') == "HOLD":
        def approve_hold():
            cur.execute("UPDATE orders SET status='NEW' WHERE id=%s", (order_id,))
            db.conn.commit()
            sp_id = order.get('salesperson_id')
            sp_name = ""
            if sp_id:
                cur.execute("SELECT name FROM salespersons WHERE id=%s", (sp_id,))
                sp = cur.fetchone()
                if sp:
                    sp_name = sp[0]
            cur.execute("""
                SELECT oi.barcode,
                       COALESCE(oi.custom_name, p.name) AS name,
                       COALESCE(oi.unit_price, p.price) AS price,
                       oi.side, oi.position
                FROM order_items oi
                JOIN products p ON oi.barcode = p.barcode
                WHERE oi.order_id=%s
            """, (order_id,))
            items_for_discord = cur.fetchall()
            products_summary_lines = []
            for barcode, name, price, side, position in items_for_discord:
                side_str = f" {side}" if side else ""
                pos_str = f" {position}" if position else ""
                clean_name = clean_product_name(barcode, name) if barcode not in ("CUSTOM", "RABAT_FORCED", "DOPLATA_FORCED") else name
                display_name = clean_name if barcode not in ("CUSTOM", "RABAT_FORCED", "DOPLATA_FORCED") else f"[{barcode}] {name}"
                products_summary_lines.append(f"- `{barcode}` {display_name}{side_str}{pos_str} – {price:.2f} zł")
            products_summary = "\n".join(products_summary_lines)
            order_data = {
                "order_id": order_id,
                "customer_name": safe_get(order, 'customer_name'),
                "phone": safe_get(order, 'phone'),
                "delivery_type": safe_get(order, 'delivery_type'),
                "salesperson": sp_name,
                "extra_info": safe_get(order, 'extra_info'),
                "products_summary": products_summary,
                "total": order.get('total_price', 0)
            }
            send_order_to_discord(order_data)
            show_topmost_info("AutoCore", "Zlecenie zatwierdzone. Trafiło do nowych zleceń.", parent=win)
            win.destroy()
            if parent_win:
                parent_win.destroy()
        ttk.Button(btn_frame, text="Zatwierdź zlecenie", command=approve_hold).pack(side="left", padx=5)

def favorite_products_window(root):
    win = tk.Toplevel(root)
    win.title("Ulubione produkty")
    win.geometry("900x650")
    win.attributes('-topmost', True)

    frame = ttk.Frame(win)
    frame.pack(fill="both", expand=True, padx=10, pady=10)
    ttk.Label(frame, text="Ulubione produkty z alertem przy niskim stanie", font=("Arial", 13, "bold")).pack(anchor="w")

    add_frame = ttk.Frame(frame)
    add_frame.pack(fill="x", pady=5)
    ttk.Label(add_frame, text="Kod kreskowy:").pack(side="left")
    barcode_entry = ttk.Entry(add_frame, width=30)
    barcode_entry.pack(side="left", padx=5)
    ttk.Label(add_frame, text="Uwagi:").pack(side="left")
    note_entry = ttk.Entry(add_frame, width=40)
    note_entry.pack(side="left", padx=5)

    columns = ("Kod", "Uwagi", "Akcja")
    tree = ttk.Treeview(frame, columns=columns, show="headings")
    for col in columns:
        tree.heading(col, text=col)
        tree.column(col, width=220)
    tree.pack(fill="both", expand=True)

    def refresh_favorites():
        cur = db.conn.cursor()
        cur.execute("SELECT barcode, note FROM favorite_products ORDER BY barcode")
        rows = cur.fetchall()
        for row in tree.get_children():
            tree.delete(row)
        for barcode, note in rows:
            tree.insert("", "end", values=(barcode, note or "", "Usuń"))

    def add_favorite():
        barcode = barcode_entry.get().strip()
        if not barcode:
            show_topmost_warning("Uwaga", "Wpisz kod kreskowy", parent=win)
            return
        cur = db.conn.cursor()
        cur.execute("SELECT id FROM favorite_products WHERE barcode=%s", (barcode,))
        if cur.fetchone():
            show_topmost_warning("Uwaga", "Produkt jest już na liście ulubionych", parent=win)
            return
        cur.execute("INSERT INTO favorite_products(barcode, note) VALUES (%s, %s)", (barcode, note_entry.get().strip() or None))
        db.conn.commit()
        barcode_entry.delete(0, tk.END)
        note_entry.delete(0, tk.END)
        refresh_favorites()
        show_topmost_info("OK", f"Dodano {barcode} do ulubionych", parent=win)

    def remove_favorite():
        selected = tree.selection()
        if not selected:
            return
        barcode = tree.item(selected[0], "values")[0]
        if ask_topmost_yesno("Usuń", f"Usunąć {barcode} z ulubionych?", parent=win):
            cur = db.conn.cursor()
            cur.execute("DELETE FROM favorite_products WHERE barcode=%s", (barcode,))
            db.conn.commit()
            refresh_favorites()

    ttk.Button(add_frame, text="Dodaj", command=add_favorite).pack(side="left", padx=5)
    ttk.Button(frame, text="Usuń zaznaczony", command=remove_favorite).pack(anchor="e", pady=5)
    tree.bind("<Double-1>", lambda event: remove_favorite())
    refresh_favorites()


def audit_history_window(root):
    win = tk.Toplevel(root)
    win.title("Historia zmian")
    win.geometry("1200x750")
    win.attributes('-topmost', True)

    frame = ttk.Frame(win)
    frame.pack(fill="both", expand=True, padx=10, pady=10)
    ttk.Label(frame, text="Historia zmian", font=("Arial", 13, "bold")).pack(anchor="w")

    filter_frame = ttk.Frame(frame)
    filter_frame.pack(fill="x", pady=5)
    ttk.Label(filter_frame, text="Typ encji:").pack(side="left")
    entity_var = tk.StringVar(value="all")
    entity_combo = ttk.Combobox(filter_frame, textvariable=entity_var, values=["all", "order", "product", "reservation"], state="readonly", width=20)
    entity_combo.pack(side="left", padx=5)
    ttk.Label(filter_frame, text="ID:").pack(side="left")
    entity_id_entry = ttk.Entry(filter_frame, width=15)
    entity_id_entry.pack(side="left", padx=5)

    columns = ("ID", "Encja", "ID encji", "Akcja", "Użytkownik", "Data", "Szczegóły")
    tree = ttk.Treeview(frame, columns=columns, show="headings")
    for col in columns:
        tree.heading(col, text=col)
        tree.column(col, width=140)
    tree.pack(fill="both", expand=True)

    def refresh_history():
        cur = db.conn.cursor()
        query = "SELECT id, entity_type, entity_id, action, actor_id, created_at, details FROM audit_log"
        params = []
        conditions = []
        entity_filter = entity_var.get()
        if entity_filter != "all":
            conditions.append("entity_type=%s")
            params.append(entity_filter)
        entity_id_value = entity_id_entry.get().strip()
        if entity_id_value:
            conditions.append("entity_id=%s")
            params.append(int(entity_id_value))
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY created_at DESC LIMIT 300"
        cur.execute(query, params)
        rows = cur.fetchall()
        for row in tree.get_children():
            tree.delete(row)
        for row in rows:
            tree.insert("", "end", values=(row[0], row[1], row[2], row[3], row[4], row[5], row[6] or "{}"))

    ttk.Button(filter_frame, text="Pokaż", command=refresh_history).pack(side="left", padx=5)
    ttk.Button(frame, text="Odśwież", command=refresh_history).pack(anchor="e", pady=5)
    refresh_history()


def archived_products_window(root):
    win = tk.Toplevel(root)
    win.title("Archiwum używanych unikatów")
    win.geometry("1000x700")
    win.attributes('-topmost', True)

    frame = ttk.Frame(win)
    frame.pack(fill="both", expand=True, padx=10, pady=10)
    ttk.Label(frame, text="Archiwum używanych unikatów", font=("Arial", 13, "bold")).pack(anchor="w")

    columns = ("Kod", "Nazwa", "Typ", "Cena", "Data archiwizacji", "Akcja")
    tree = ttk.Treeview(frame, columns=columns, show="headings")
    for col in columns:
        tree.heading(col, text=col)
        tree.column(col, width=140)
    tree.pack(fill="both", expand=True)

    def refresh_archived():
        rows = db.get_archived_products()
        for row in tree.get_children():
            tree.delete(row)
        for barcode, name, product_type, price, archived_date in rows:
            tree.insert("", "end", values=(barcode, name or "-", product_type or "-", f"{price:.2f}" if price else "0.00", archived_date, "Przywróć"))

    def restore_selected():
        selected = tree.selection()
        if not selected:
            return
        barcode = tree.item(selected[0], "values")[0]
        if ask_topmost_yesno("Przywróć", f"Przywrócić {barcode} z archiwum do bazy?", parent=win):
            if db.restore_product(barcode):
                db.log_audit_event("product", None, "restored_from_archive", {"barcode": barcode}, CURRENT_USER_ID)
                db.conn.commit()
                show_topmost_info("OK", f"Przywrócono {barcode}", parent=win)
                refresh_archived()
            else:
                show_topmost_warning("Uwaga", "Nie udało się przywrócić produktu", parent=win)

    ttk.Button(frame, text="Przywróć zaznaczony", command=restore_selected).pack(anchor="e", pady=5)
    tree.bind("<Double-1>", lambda event: restore_selected())
    refresh_archived()


def inventory_dashboard_window(root):
    win = tk.Toplevel(root)
    win.title("Dashboard magazynowy")
    win.geometry("1200x800")
    win.attributes('-topmost', True)

    frame = ttk.Frame(win)
    frame.pack(fill="both", expand=True, padx=10, pady=10)

    ttk.Label(frame, text="Dashboard magazynowy", font=("Arial", 13, "bold")).pack(anchor="w")
    metrics_frame = ttk.Frame(frame)
    metrics_frame.pack(fill="x", pady=10)

    metrics = {}
    for title, key in [("Aktywne zlecenia", "active_orders"), ("Aktywne rezerwacje", "active_reservations"), ("Produkty niskie", "low_stock"), ("Produkty w bazie", "total_products")]:
        card = ttk.LabelFrame(metrics_frame, text=title)
        card.pack(side="left", padx=5, fill="both")
        var = tk.StringVar(value="0")
        ttk.Label(card, textvariable=var, font=("Arial", 14, "bold")).pack(padx=10, pady=10)
        metrics[key] = var

    def refresh_dashboard():
        cur = db.conn.cursor()
        cur.execute("SELECT COUNT(*) FROM orders WHERE status='NEW'")
        metrics["active_orders"].set(cur.fetchone()[0])
        cur.execute("SELECT COUNT(*) FROM inventory_reservations WHERE status='active'")
        metrics["active_reservations"].set(cur.fetchone()[0])
        cur.execute("SELECT COUNT(*) FROM products WHERE product_type='używane wielokrotne' AND stock<=2 AND stock>0 AND barcode NOT IN ('RABAT','CUSTOM')")
        metrics["low_stock"].set(cur.fetchone()[0])
        cur.execute("SELECT COUNT(*) FROM products WHERE barcode NOT IN ('RABAT','CUSTOM')")
        metrics["total_products"].set(cur.fetchone()[0])

    ttk.Button(frame, text="Odśwież", command=refresh_dashboard).pack(anchor="e")

    detail_frame = ttk.LabelFrame(frame, text="Produkty wymagające uwagi")
    detail_frame.pack(fill="both", expand=True, pady=10)
    columns = ("Kod", "Nazwa", "Stan", "Typ", "Kategoria")
    tree = ttk.Treeview(detail_frame, columns=columns, show="headings")
    for col in columns:
        tree.heading(col, text=col)
        tree.column(col, width=160)
    tree.pack(fill="both", expand=True)

    def load_attention_items():
        cur = db.conn.cursor()
        cur.execute("""
            SELECT barcode, name, stock, product_type, category
            FROM products
            WHERE barcode NOT IN ('RABAT','CUSTOM')
              AND (stock <= 2 OR product_type='używane wielokrotne')
            ORDER BY stock ASC, name ASC
        """)
        rows = cur.fetchall()
        for row in tree.get_children():
            tree.delete(row)
        for row in rows:
            tree.insert("", "end", values=(row[0], row[1] or "-", row[2] or 0, row[3] or "-", row[4] or "-"))

    ttk.Button(frame, text="Pokaż produkty wymagające uwagi", command=load_attention_items).pack(anchor="e", pady=5)
    refresh_dashboard()
    load_attention_items()


def inventory_reports_window(root):
    win = tk.Toplevel(root)
    win.title("Raporty magazynowe")
    win.geometry("1000x700")
    win.attributes('-topmost', True)

    frame = ttk.Frame(win)
    frame.pack(fill="both", expand=True, padx=10, pady=10)

    ttk.Label(frame, text="Raport stanu magazynowego", font=("Arial", 12, "bold")).pack(anchor="w")
    filter_frame = ttk.Frame(frame)
    filter_frame.pack(fill="x", pady=5)
    ttk.Button(filter_frame, text="Wszystkie", command=lambda: refresh_report("all")).pack(side="left")
    ttk.Button(filter_frame, text="Zero", command=lambda: refresh_report("zero")).pack(side="left", padx=3)
    ttk.Button(filter_frame, text="Niski stan", command=lambda: refresh_report("low")).pack(side="left", padx=3)
    columns = ("Kod", "Nazwa", "Stan", "Typ", "Kategoria")
    tree = ttk.Treeview(frame, columns=columns, show="headings")
    for col in columns:
        tree.heading(col, text=col)
        tree.column(col, width=150)
    tree.pack(fill="both", expand=True, pady=10)

    def refresh_report(mode="all"):
        cur = db.conn.cursor()
        query = """
            SELECT barcode, name, stock, product_type, category
            FROM products
            WHERE barcode NOT IN ('RABAT','CUSTOM')
        """
        if mode == "zero":
            query += " AND COALESCE(stock,0) = 0"
        elif mode == "low":
            query += " AND COALESCE(stock,0) <= 2 AND COALESCE(stock,0) > 0"
        query += " ORDER BY stock ASC, name ASC"
        cur.execute(query)
        rows = cur.fetchall()
        for row in tree.get_children():
            tree.delete(row)
        for row in rows:
            tree.insert("", "end", values=(row[0], row[1] or "-", row[2] or 0, row[3] or "-", row[4] or "-"))

    ttk.Button(frame, text="Odśwież", command=lambda: refresh_report("all")).pack(anchor="e")
    ttk.Button(frame, text="Eksportuj CSV", command=lambda: show_topmost_info("Eksport", export_stock_report(db.conn), parent=win)).pack(anchor="e", pady=5)

    refresh_report("all")

# ---------- INWENTARYZACJA ----------
def inventory_window(root):
    win = tk.Toplevel(root)
    win.title("Inwentaryzacja i rezerwacje")
    win.geometry("1100x800")
    win.attributes('-topmost', True)

    notebook = ttk.Notebook(win)
    notebook.pack(fill="both", expand=True, padx=10, pady=10)

    inventory_tab = ttk.Frame(notebook)
    reservations_tab = ttk.Frame(notebook)
    movements_tab = ttk.Frame(notebook)
    corrections_tab = ttk.Frame(notebook)
    notebook.add(inventory_tab, text="Inwentaryzacja")
    notebook.add(reservations_tab, text="Rezerwacje")
    notebook.add(movements_tab, text="Historia ruchów")
    notebook.add(corrections_tab, text="Korekta stanu")

    def refresh_reservation_view():
        cur = db.conn.cursor()
        cur.execute("""
            SELECT ir.id, ir.order_id, ir.barcode, ir.qty, ir.status, ir.created_at,
                   o.customer_name, p.name
            FROM inventory_reservations ir
            LEFT JOIN orders o ON o.id = ir.order_id
            LEFT JOIN products p ON p.barcode = ir.barcode
            ORDER BY ir.created_at DESC
        """)
        rows = cur.fetchall()
        for row in reservations_tree.get_children():
            reservations_tree.delete(row)
        for row in rows:
            reservations_tree.insert("", "end", values=(row[0], row[1], row[2], row[3], row[4], row[5], row[6] or "-", row[7] or "-"))

        summary = summarize_reservations([{"status": row[4]} for row in rows])
        summary_var.set(f"Aktywne: {summary['active']} | Zużyte: {summary['consumed']} | Zwolnione: {summary['released']}")

    def refresh_movements_view():
        cur = db.conn.cursor()
        cur.execute("""
            SELECT id, product_id, barcode, delta, reason, order_id, created_at, details
            FROM inventory_movements
            ORDER BY created_at DESC
            LIMIT 200
        """)
        rows = cur.fetchall()
        for row in movements_tree.get_children():
            movements_tree.delete(row)
        for row in rows:
            movements_tree.insert("", "end", values=(row[0], row[1], row[2], row[3], row[4], row[5], row[6], row[7] or "{}"))

    inventory_tab_frame = ttk.Frame(inventory_tab)
    inventory_tab_frame.pack(fill="both", expand=True, padx=10, pady=10)
    ttk.Label(inventory_tab_frame, text="Typ produktu:").grid(row=0, column=0, sticky="e")
    type_var = tk.StringVar()
    type_combo = ttk.Combobox(inventory_tab_frame, textvariable=type_var, values=["nowe", "używane unikat", "używane wielokrotne", "inne"])
    type_combo.grid(row=0, column=1, padx=5)
    ttk.Label(inventory_tab_frame, text="Kategoria:").grid(row=1, column=0, sticky="e")
    categories = db.get_categories()
    cat_var = tk.StringVar()
    cat_combo = ttk.Combobox(inventory_tab_frame, textvariable=cat_var, values=categories, width=40)
    cat_combo.grid(row=1, column=1, padx=5)
    ttk.Label(inventory_tab_frame, text="(możesz wpisać nową)").grid(row=1, column=2, sticky="w")
    def generate_list():
        ptype = type_var.get()
        category = cat_var.get()
        if not ptype:
            show_topmost_error("Błąd", "Wybierz typ produktu", parent=win)
            return
        cur = db.conn.cursor()
        query = "SELECT barcode, name FROM products WHERE product_type=%s AND barcode NOT IN ('RABAT','CUSTOM')"
        params = [ptype]
        if category:
            query += " AND category=%s"
            params.append(category)
        cur.execute(query, params)
        rows = cur.fetchall()
        if not rows:
            show_topmost_info("Info", "Brak produktów spełniających kryteria", parent=win)
            return
        list_frame = ttk.LabelFrame(inventory_tab_frame, text="Produkty do zeskanowania")
        list_frame.grid(row=2, column=0, columnspan=3, sticky="nsew", padx=10, pady=10)
        listbox = tk.Listbox(list_frame)
        listbox.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(list_frame, orient="vertical", command=listbox.yview)
        scroll.pack(side="right", fill="y")
        listbox.configure(yscrollcommand=scroll.set)
        items_dict = {row[0]: row[1] for row in rows}
        for barcode, name in items_dict.items():
            listbox.insert(tk.END, f"{barcode} - {name}")
        scan_frame = ttk.Frame(inventory_tab_frame)
        scan_frame.grid(row=3, column=0, columnspan=3, sticky="ew", padx=10, pady=10)
        ttk.Label(scan_frame, text="Zeskanuj kod:").pack(side="left")
        scan_entry = ttk.Entry(scan_frame, width=40)
        scan_entry.pack(side="left", padx=5)
        scan_entry.focus()
        status_label = ttk.Label(inventory_tab_frame, text="Pozostało: {} produktów".format(len(items_dict)))
        status_label.grid(row=4, column=0, columnspan=3, pady=5)
        def scan_barcode(event=None):
            barcode = scan_entry.get().strip()
            if not barcode:
                return
            if barcode in items_dict:
                for i in range(listbox.size()):
                    if listbox.get(i).startswith(barcode):
                        listbox.delete(i)
                        break
                del items_dict[barcode]
                status_label.config(text=f"Pozostało: {len(items_dict)} produktów")
                scan_entry.delete(0, tk.END)
                if len(items_dict) == 0:
                    show_topmost_info("Sukces", "Wszystkie produkty zostały zeskanowane!", parent=win)
            else:
                show_topmost_warning("Uwaga", f"Produkt {barcode} nie znajduje się na liście inwentaryzacyjnej", parent=win)
        scan_entry.bind("<Return>", scan_barcode)
        ttk.Button(scan_frame, text="Skanuj", command=scan_barcode).pack(side="left")
        def generate_missing():
            if items_dict:
                filename = f"braki_inwentaryzacja_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
                with open(filename, 'w', newline='', encoding='utf-8-sig') as f:
                    writer = csv.writer(f)
                    writer.writerow(["Kod kreskowy", "Nazwa"])
                    for barcode, name in items_dict.items():
                        writer.writerow([barcode, name])
                show_topmost_info("Eksport", f"Lista brakujących produktów zapisana jako {filename}", parent=win)
            else:
                show_topmost_info("Info", "Brak brakujących produktów", parent=win)
        ttk.Button(inventory_tab_frame, text="Wygeneruj listę brakujących pozycji", command=generate_missing).grid(row=5, column=0, columnspan=3, pady=10)
    ttk.Button(inventory_tab_frame, text="Generuj listę inwentaryzacyjną", command=generate_list).grid(row=6, column=0, columnspan=3, pady=10)

    reservations_frame = ttk.Frame(reservations_tab)
    reservations_frame.pack(fill="both", expand=True, padx=10, pady=10)
    summary_var = tk.StringVar(value="")
    ttk.Label(reservations_frame, textvariable=summary_var, font=("Arial", 10, "bold")).pack(anchor="w")
    columns = ("ID", "Zlecenie", "Kod", "Ilość", "Status", "Data", "Klient", "Nazwa")
    reservations_tree = ttk.Treeview(reservations_frame, columns=columns, show="headings")
    for col in columns:
        reservations_tree.heading(col, text=col)
        reservations_tree.column(col, width=110)
    reservations_tree.pack(fill="both", expand=True)
    ttk.Button(reservations_frame, text="Odśwież", command=refresh_reservation_view).pack(pady=5, anchor="e")

    movements_frame = ttk.Frame(movements_tab)
    movements_frame.pack(fill="both", expand=True, padx=10, pady=10)
    columns2 = ("ID", "Produkt", "Kod", "Delta", "Powód", "Zlecenie", "Data", "Szczegóły")
    movements_tree = ttk.Treeview(movements_frame, columns=columns2, show="headings")
    for col in columns2:
        movements_tree.heading(col, text=col)
        movements_tree.column(col, width=110)
    movements_tree.pack(fill="both", expand=True)
    ttk.Button(movements_frame, text="Odśwież", command=refresh_movements_view).pack(pady=5, anchor="e")

    corrections_frame = ttk.Frame(corrections_tab)
    corrections_frame.pack(fill="both", expand=True, padx=20, pady=20)
    ttk.Label(corrections_frame, text="Kod kreskowy:").grid(row=0, column=0, sticky="e", pady=5)
    stock_barcode_entry = ttk.Entry(corrections_frame, width=40)
    stock_barcode_entry.grid(row=0, column=1, padx=5, pady=5)
    ttk.Label(corrections_frame, text="Korekta (+/-):").grid(row=1, column=0, sticky="e", pady=5)
    stock_delta_entry = ttk.Entry(corrections_frame, width=20)
    stock_delta_entry.grid(row=1, column=1, padx=5, pady=5)
    stock_delta_entry.insert(0, "0")
    ttk.Label(corrections_frame, text="Powód:").grid(row=2, column=0, sticky="e", pady=5)
    stock_reason_var = tk.StringVar(value="korekta ręczna")
    stock_reason_entry = ttk.Entry(corrections_frame, textvariable=stock_reason_var, width=40)
    stock_reason_entry.grid(row=2, column=1, padx=5, pady=5)
    stock_status_var = tk.StringVar(value="")
    ttk.Label(corrections_frame, textvariable=stock_status_var, wraplength=500).grid(row=3, column=0, columnspan=2, sticky="w", pady=10)

    def inspect_stock():
        barcode = stock_barcode_entry.get().strip()
        if not barcode:
            stock_status_var.set("Wpisz kod kreskowy")
            return
        cur = db.conn.cursor()
        cur.execute("SELECT id, name, stock FROM products WHERE UPPER(barcode)=UPPER(%s)", (barcode,))
        row = cur.fetchone()
        if not row:
            stock_status_var.set("Produkt nie istnieje w bazie")
            return
        product_id, name, stock = row
        stock_status_var.set(f"Produkt: {name}\nAktualny stan: {stock}")

    def apply_stock_correction():
        barcode = stock_barcode_entry.get().strip()
        if not barcode:
            show_topmost_warning("Uwaga", "Wpisz kod kreskowy", parent=win)
            return
        try:
            delta = int(stock_delta_entry.get().replace(',', '.'))
        except Exception:
            show_topmost_warning("Uwaga", "Nieprawidłowa wartość korekty", parent=win)
            return
        cur = db.conn.cursor()
        cur.execute("SELECT id, name, stock FROM products WHERE UPPER(barcode)=UPPER(%s)", (barcode,))
        row = cur.fetchone()
        if not row:
            show_topmost_warning("Uwaga", "Produkt nie istnieje w bazie", parent=win)
            return
        product_id, name, current_stock = row
        ok, new_stock = validate_stock_adjustment(current_stock, delta)
        if not ok:
            show_topmost_warning("Uwaga", "Korekta spowodowałaby ujemny stan", parent=win)
            return
        cur.execute("UPDATE products SET stock=%s WHERE id=%s", (new_stock, product_id))
        cur.execute("""
            INSERT INTO inventory_movements(product_id, barcode, delta, reason, created_by, details)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (product_id, barcode, delta, stock_reason_var.get() or "korekta ręczna", CURRENT_USER_ID, {"source": "manual_correction"}))
        db.log_audit_event("product", product_id, "stock_adjusted", {"barcode": barcode, "delta": delta, "new_stock": new_stock}, CURRENT_USER_ID)
        db.conn.commit()
        stock_status_var.set(f"Zaktualizowano stan: {name}\nNowy stan: {new_stock}")
        show_topmost_info("OK", f"Stan produktu {barcode} zaktualizowano do {new_stock}", parent=win)

    ttk.Button(corrections_frame, text="Sprawdź stan", command=inspect_stock).grid(row=4, column=0, padx=5, pady=10)
    ttk.Button(corrections_frame, text="Zastosuj korektę", command=apply_stock_correction).grid(row=4, column=1, padx=5, pady=10)

    refresh_reservation_view()
    refresh_movements_view()

    return win
    frame_filter = ttk.Frame(win)
    frame_filter.pack(fill="x", padx=10, pady=10)
    ttk.Label(frame_filter, text="Typ produktu:").grid(row=0, column=0, sticky="e")
    type_var = tk.StringVar()
    type_combo = ttk.Combobox(frame_filter, textvariable=type_var, values=["nowe", "używane unikat", "używane wielokrotne", "inne"])
    type_combo.grid(row=0, column=1, padx=5)
    ttk.Label(frame_filter, text="Kategoria:").grid(row=1, column=0, sticky="e")
    categories = db.get_categories()
    cat_var = tk.StringVar()
    cat_combo = ttk.Combobox(frame_filter, textvariable=cat_var, values=categories, width=40)
    cat_combo.grid(row=1, column=1, padx=5)
    ttk.Label(frame_filter, text="(możesz wpisać nową)").grid(row=1, column=2, sticky="w")
    def generate_list():
        ptype = type_var.get()
        category = cat_var.get()
        if not ptype:
            show_topmost_error("Błąd", "Wybierz typ produktu", parent=win)
            return
        cur = db.conn.cursor()
        query = "SELECT barcode, name FROM products WHERE product_type=%s AND barcode NOT IN ('RABAT','CUSTOM')"
        params = [ptype]
        if category:
            query += " AND category=%s"
            params.append(category)
        cur.execute(query, params)
        rows = cur.fetchall()
        if not rows:
            show_topmost_info("Info", "Brak produktów spełniających kryteria", parent=win)
            return
        list_frame = ttk.LabelFrame(win, text="Produkty do zeskanowania")
        list_frame.pack(fill="both", expand=True, padx=10, pady=10)
        listbox = tk.Listbox(list_frame)
        listbox.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(list_frame, orient="vertical", command=listbox.yview)
        scroll.pack(side="right", fill="y")
        listbox.configure(yscrollcommand=scroll.set)
        items_dict = {row[0]: row[1] for row in rows}
        for barcode, name in items_dict.items():
            listbox.insert(tk.END, f"{barcode} - {name}")
        scan_frame = ttk.Frame(win)
        scan_frame.pack(fill="x", padx=10, pady=10)
        ttk.Label(scan_frame, text="Zeskanuj kod:").pack(side="left")
        scan_entry = ttk.Entry(scan_frame, width=40)
        scan_entry.pack(side="left", padx=5)
        scan_entry.focus()
        status_label = ttk.Label(win, text="Pozostało: {} produktów".format(len(items_dict)))
        status_label.pack(pady=5)
        def scan_barcode(event=None):
            barcode = scan_entry.get().strip()
            if not barcode:
                return
            if barcode in items_dict:
                for i in range(listbox.size()):
                    if listbox.get(i).startswith(barcode):
                        listbox.delete(i)
                        break
                del items_dict[barcode]
                status_label.config(text=f"Pozostało: {len(items_dict)} produktów")
                scan_entry.delete(0, tk.END)
                if len(items_dict) == 0:
                    show_topmost_info("Sukces", "Wszystkie produkty zostały zeskanowane!", parent=win)
            else:
                show_topmost_warning("Uwaga", f"Produkt {barcode} nie znajduje się na liście inwentaryzacyjnej", parent=win)
        scan_entry.bind("<Return>", scan_barcode)
        ttk.Button(scan_frame, text="Skanuj", command=scan_barcode).pack(side="left")
        def generate_missing():
            if items_dict:
                filename = f"braki_inwentaryzacja_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
                with open(filename, 'w', newline='', encoding='utf-8-sig') as f:
                    writer = csv.writer(f)
                    writer.writerow(["Kod kreskowy", "Nazwa"])
                    for barcode, name in items_dict.items():
                        writer.writerow([barcode, name])
                show_topmost_info("Eksport", f"Lista brakujących produktów zapisana jako {filename}", parent=win)
            else:
                show_topmost_info("Info", "Brak brakujących produktów", parent=win)
        ttk.Button(win, text="Wygeneruj listę brakujących pozycji", command=generate_missing).pack(pady=10)
    ttk.Button(win, text="Generuj listę inwentaryzacyjną", command=generate_list).pack(pady=10)

# ---------- AKTUALIZACJA SIGNEDA ----------
def update_signeda_window(root, tree, stats_label, category_cb, model_cb):
    win = tk.Toplevel(root)
    win.title("Aktualizacja bazy Signeda")
    win.geometry("620x620")
    win.attributes('-topmost', True)
    mode_var = tk.StringVar(value="older")
    mode_frame = ttk.LabelFrame(win, text="Tryb aktualizacji")
    mode_frame.pack(fill="x", padx=20, pady=10)
    ttk.Radiobutton(mode_frame, text="Starsze niż", variable=mode_var, value="older").pack(anchor="w", padx=10, pady=2)
    ttk.Radiobutton(mode_frame, text="Nowsze niż", variable=mode_var, value="newer").pack(anchor="w", padx=10, pady=2)
    ttk.Radiobutton(mode_frame, text="Aktualizuj wszystkie", variable=mode_var, value="all").pack(anchor="w", padx=10, pady=2)

    today_var = tk.BooleanVar(value=False)
    ttk.Checkbutton(win, text="Aktualizuj dzisiaj", variable=today_var).pack(anchor="w", padx=30, pady=(0,10))

    days_label = ttk.Label(win, text="Liczba dni do porównania:")
    days_label.pack(pady=(10, 0))
    days_var = tk.IntVar(value=7)
    days_spin = ttk.Spinbox(win, from_=0, to=365, textvariable=days_var, width=10)
    days_spin.pack()
    ttk.Label(win, text="(0 = wszystkie dla trybu starsze/nowsze)").pack()
    ttk.Label(win, text="Liczba wątków równoległych:").pack(pady=10)
    threads_var = tk.IntVar(value=5)
    threads_spin = ttk.Spinbox(win, from_=1, to=20, textvariable=threads_var, width=5)
    threads_spin.pack()
    progress_var = tk.DoubleVar()
    progress_bar = ttk.Progressbar(win, variable=progress_var, maximum=100)
    progress_bar.pack(fill="x", padx=20, pady=20)
    status_label = ttk.Label(win, text="")
    status_label.pack(pady=10)

    stop_update = False
    changes = []
    updated_data = {}
    lock = threading.Lock()

    def update_one_safe(barcode):
        nonlocal stop_update
        if stop_update:
            return
        conn = None
        try:
            conn = psycopg2.connect(
                host=PG_HOST,
                port=PG_PORT,
                dbname=PG_DBNAME,
                user=PG_USER,
                password=PG_PASSWORD
            )
            conn.autocommit = False
            cur = conn.cursor()
            cur.execute("SELECT price, signeda_stock, name, oe_code, side, position, product_type, models FROM products WHERE barcode=%s", (barcode,))
            old = cur.fetchone()
            if not old:
                return
            old_price, old_signeda_stock, old_name, old_oe, old_side, old_pos, old_type, old_models = old
            data = signeda_scraper.search_product(barcode)
            if not data:
                return
            cur.execute("UPDATE products SET signeda_stock=%s, last_sync=%s WHERE barcode=%s",
                        (data['stock'], current_timestamp(), barcode))
            if data.get("photo_url"):
                folder = download_product_photo(barcode, data["photo_url"])
                if folder:
                    cur.execute("UPDATE products SET photos_folder=%s WHERE barcode=%s", (folder, barcode))
            price_changed = abs(data['price'] - old_price) > 0.01
            with lock:
                updated_data[barcode] = {
                    'name': data['name'],
                    'oe_code': data['oe_code'],
                    'side': data['side'],
                    'position': data['position'],
                    'product_type': data['product_type'],
                    'models': data['models'],
                    'new_price': data['price']
                }
                if price_changed:
                    changes.append((barcode, old_price, data['price']))
            conn.commit()
        except requests.exceptions.RequestException as e:
            logging.error(f"Błąd sieci w update_one_safe dla {barcode}: {e}")
        except Exception as e:
            logging.error(f"Błąd w update_one_safe dla {barcode}: {e}")
        finally:
            if conn:
                conn.close()

    def start_update():
        nonlocal stop_update, changes, updated_data
        stop_update = False
        days = days_var.get()
        main_cur = db.conn.cursor()
        mode = mode_var.get()
        days = days_var.get()
        main_cur = db.conn.cursor()
        if today_var.get():
            mode = 'today'
        if mode == 'older':
            if days > 0:
                cutoff = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d %H:%M:%S')
                main_cur.execute("SELECT barcode FROM products WHERE source='signeda' AND (force_price=0 OR force_price IS NULL) AND (last_sync IS NULL OR last_sync < %s) AND barcode NOT IN ('RABAT','CUSTOM')", (cutoff,))
            else:
                main_cur.execute("SELECT barcode FROM products WHERE source='signeda' AND (force_price=0 OR force_price IS NULL) AND barcode NOT IN ('RABAT','CUSTOM')")
        elif mode == 'newer':
            if days > 0:
                cutoff = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d %H:%M:%S')
                main_cur.execute("SELECT barcode FROM products WHERE source='signeda' AND (force_price=0 OR force_price IS NULL) AND (last_sync IS NULL OR last_sync >= %s) AND barcode NOT IN ('RABAT','CUSTOM')", (cutoff,))
            else:
                main_cur.execute("SELECT barcode FROM products WHERE source='signeda' AND (force_price=0 OR force_price IS NULL) AND barcode NOT IN ('RABAT','CUSTOM')")
        elif mode == 'today':
            today = datetime.now().strftime('%Y-%m-%d')
            main_cur.execute("SELECT barcode FROM products WHERE source='signeda' AND (force_price=0 OR force_price IS NULL) AND date(last_sync) = %s AND barcode NOT IN ('RABAT','CUSTOM')", (today,))
        else:
            main_cur.execute("SELECT barcode FROM products WHERE source='signeda' AND (force_price=0 OR force_price IS NULL) AND barcode NOT IN ('RABAT','CUSTOM')")
        rows = main_cur.fetchall()
        total = len(rows)
        if total == 0:
            show_topmost_info("Info", "Brak produktów do aktualizacji", parent=win)
            win.destroy()
            return
        status_label.config(text=f"Rozpoczynanie aktualizacji {total} produktów...")
        win.update_idletasks()
        processed = 0
        def on_finish():
            if stop_update:
                show_topmost_info("Anulowano", "Aktualizacja została przerwana przez użytkownika", parent=win)
                win.destroy()
                return
            if changes:
                show_price_choice_window(win, changes, updated_data, tree, stats_label, category_cb, model_cb)
            else:
                cur = db.conn.cursor()
                for barcode, info in updated_data.items():
                    cur.execute("""
                        UPDATE products SET name=%s, oe_code=%s, side=%s, position=%s, product_type=%s, models=%s
                        WHERE barcode=%s
                    """, (info['name'], info['oe_code'], info['side'], info['position'], info['product_type'], info['models'], barcode))
                db.conn.commit()
                load_products_into_tree(tree, "", "", "")
                refresh_filters(category_cb, model_cb)
                refresh_stats(stats_label)
                show_topmost_info("Koniec", f"Zaktualizowano {total} produktów. Brak zmian cen.", parent=win)
                win.destroy()
        with ThreadPoolExecutor(max_workers=threads_var.get()) as executor:
            futures = [executor.submit(update_one_safe, barcode[0]) for barcode in rows]
            for future in as_completed(futures):
                processed += 1
                progress_var.set((processed / total) * 100)
                status_label.config(text=f"Aktualizacja: {processed}/{total}")
                win.update_idletasks()
                if stop_update:
                    for f in futures:
                        f.cancel()
                    break
        win.after(0, on_finish)

    def show_price_choice_window(parent, changes_list, updated_info, tree, stats_label, category_cb, model_cb):
        choice_win = tk.Toplevel(parent)
        choice_win.title("Zmiany cen - wybór")
        choice_win.geometry("900x600")
        choice_win.attributes('-topmost', True)
        ttk.Label(choice_win, text="Dla następujących produktów cena uległa zmianie. Wybierz, którą cenę zachować:").pack(pady=10)
        frame = ttk.Frame(choice_win)
        frame.pack(fill="both", expand=True)
        canvas = tk.Canvas(frame)
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=canvas.yview)
        scrollable_frame = ttk.Frame(canvas)
        scrollable_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0,0), window=scrollable_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        vars_dict = {}
        for barcode, old_price, new_price in changes_list:
            frame_prod = ttk.LabelFrame(scrollable_frame, text=barcode)
            frame_prod.pack(fill="x", padx=10, pady=5)
            ttk.Label(frame_prod, text=f"Nazwa: {updated_info[barcode]['name']}").pack(anchor="w")
            ttk.Label(frame_prod, text=f"Stara cena: {old_price:.2f} zł").pack(anchor="w")
            ttk.Label(frame_prod, text=f"Nowa cena: {new_price:.2f} zł").pack(anchor="w")
            var = tk.StringVar(value="old")
            ttk.Radiobutton(frame_prod, text="Zachowaj starą", variable=var, value="old").pack(side="left", padx=10)
            ttk.Radiobutton(frame_prod, text="Użyj nowej", variable=var, value="new").pack(side="left", padx=10)
            vars_dict[barcode] = var
        def apply_choices():
            cur = db.conn.cursor()
            for barcode, var in vars_dict.items():
                info = updated_info[barcode]
                cur.execute("""
                    UPDATE products SET name=%s, oe_code=%s, side=%s, position=%s, product_type=%s, models=%s
                    WHERE barcode=%s
                """, (info['name'], info['oe_code'], info['side'], info['position'], info['product_type'], info['models'], barcode))
                if var.get() == "new":
                    cur.execute("UPDATE products SET price=%s WHERE barcode=%s", (info['new_price'], barcode))
            db.conn.commit()
            load_products_into_tree(tree, "", "", "")
            refresh_filters(category_cb, model_cb)
            refresh_stats(stats_label)
            show_topmost_info("OK", "Zaktualizowano ceny i dane", parent=choice_win)
            choice_win.destroy()
            parent.destroy()
        ttk.Button(choice_win, text="Zatwierdź wybór", command=apply_choices).pack(pady=10)

    def cancel_update():
        nonlocal stop_update
        stop_update = True
        status_label.config(text="Anulowanie...")
        win.update_idletasks()

    ttk.Button(win, text="Rozpocznij aktualizację", command=start_update).pack(pady=10)
    ttk.Button(win, text="Anuluj", command=cancel_update).pack(pady=5)

# ---------- DODATKOWE OPCJE ----------
def additional_options_window(root, tree, stats_label, category_cb, model_cb):
    win = tk.Toplevel(root)
    win.title("Dodatkowe opcje")
    win.geometry("600x800")
    win.attributes('-topmost', True)

    ttk.Button(win, text="Zarządzaj handlowcami", command=lambda: manage_salespersons_window(win)).pack(fill="x", padx=20, pady=5)
    ttk.Button(win, text="Zarządzaj przypisaniami IP", command=lambda: manage_ip_mappings_window(win)).pack(fill="x", padx=20, pady=5)
    ttk.Button(win, text="Zarządzaj magazynierami", command=lambda: manage_workers_window(win)).pack(fill="x", padx=20, pady=5)
    ttk.Button(win, text="Archiwum produktów", command=lambda: archived_products_window(root, tree, category_cb, model_cb, stats_label)).pack(fill="x", padx=20, pady=5)

    def open_photos_folder():
        open_folder()
    ttk.Button(win, text="Otwórz folder zdjęć", command=open_photos_folder).pack(fill="x", padx=20, pady=5)

    def open_product_folder():
        barcode = ask_topmost_string("Otwórz folder produktu", "Podaj kod produktu:", parent=win)
        if barcode:
            folder = os.path.join("photos", barcode.strip())
            if os.path.exists(folder):
                open_folder(folder)
            else:
                show_topmost_warning("Uwaga", f"Folder dla produktu {barcode} nie istnieje.", parent=win)
    ttk.Button(win, text="Otwórz folder produktu (wpisz kod)", command=open_product_folder).pack(fill="x", padx=20, pady=5)
    ttk.Button(win, text="Klienci", command=lambda: manage_customers_window(win)).pack(fill="x", padx=20, pady=5)
    ttk.Button(win, text="Szablony zamówień", command=lambda: manage_order_templates_window(win)).pack(fill="x", padx=20, pady=5)

    def missing_parts_list():
        cur = db.conn.cursor()
        cur.execute("""
            SELECT o.created_at,
                   o.id,
                   string_agg(DISTINCT oi.barcode, E'\n' ORDER BY oi.barcode) AS barcodes,
                   o.document_type
            FROM order_items oi
            JOIN orders o ON oi.order_id=o.id
            JOIN products p ON oi.barcode=p.barcode
            WHERE o.status IN ('NEW','READY')
              AND oi.picked=0
              AND (p.stock=0 OR oi.to_order=1)
              AND oi.barcode NOT IN ('RABAT','CUSTOM','RABAT_FORCED','DOPLATA_FORCED')
              AND p.source='signeda'
            GROUP BY o.created_at, o.id, o.document_type
            ORDER BY o.created_at, o.id
        """)
        rows = cur.fetchall()
        if not rows:
            show_topmost_info("Info", "Brak brakujących części z Signeda", parent=win)
            return
        try:
            filename = f"brakujace_czesci_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
            with open(filename, 'w', encoding='utf-8') as f:
                for idx, (created_at, order_id, barcodes, document_type) in enumerate(rows):
                    typ = 'FV' if document_type and 'FAKTURA' in document_type.upper() else 'WZ'
                    f.write(f"{created_at.strftime('%Y-%m-%d %H:%M:%S') if created_at else ''} | {order_id} | {typ}\n")
                    if barcodes:
                        f.write(f"{barcodes.strip()}\n")
                    if idx < len(rows) - 1:
                        f.write("\n")
            show_topmost_info("Eksport", f"Wygenerowano plik {filename}", parent=win)
        except PermissionError:
            show_topmost_error("Błąd", "Brak uprawnień do zapisu pliku", parent=win)
            logging.error("Brak uprawnień do zapisu missing_parts_list")
    ttk.Button(win, text="Lista brakujących części do zleceń", command=missing_parts_list).pack(fill="x", padx=20, pady=5)

    def bulk_upload_window():
        bulk_win = tk.Toplevel(win)
        bulk_win.title("Dodawanie kodów hurtowo")
        bulk_win.geometry("1200x800")
        bulk_win.attributes('-topmost', True)

        ttk.Label(bulk_win, text="Wklej kody produktów (każdy w nowej linii):").pack(pady=10)
        text_area = tk.Text(bulk_win, height=8)
        text_area.pack(fill="x", padx=10, pady=5)

        btn_frame = ttk.Frame(bulk_win)
        btn_frame.pack(pady=5)
        ttk.Button(btn_frame, text="Pobierz dane", command=lambda: fetch_codes()).pack(side="left", padx=5)
        ttk.Button(btn_frame, text="Dodaj do bazy danych", command=lambda: add_to_database()).pack(side="left", padx=5)
        ttk.Button(btn_frame, text="Wyczyść", command=lambda: clear_all()).pack(side="left", padx=5)

        columns = ("Kod", "Cena", "Stan", "OE", "Nazwa", "Typ", "Status")
        tree_bulk = ttk.Treeview(bulk_win, columns=columns, show="headings")
        for col in columns:
            tree_bulk.heading(col, text=col)
            tree_bulk.column(col, width=120)
        tree_bulk.pack(fill="both", expand=True, padx=10, pady=10)

        status_label = ttk.Label(bulk_win, text="")
        status_label.pack(pady=5)

        fetched_data = {}

        def clear_all():
            for row in tree_bulk.get_children():
                tree_bulk.delete(row)
            fetched_data.clear()
            status_label.config(text="")

        def fetch_codes():
            clear_all()
            codes = text_area.get("1.0", tk.END).splitlines()
            codes = [c.strip() for c in codes if c.strip()]
            if not codes:
                show_topmost_warning("Uwaga", "Brak kodów do pobrania", parent=bulk_win)
                return
            total = 0
            status_label.config(text=f"Pobieranie... (0/{len(codes)})")
            bulk_win.update_idletasks()

            for code in codes:
                try:
                    data = signeda_scraper.search_product(code)
                    if data:
                        cur = db.conn.cursor()
                        cur.execute("SELECT id FROM products WHERE barcode=%s", (code,))
                        exists = cur.fetchone() is not None
                        status = "istnieje" if exists else "nowy"
                        tree_bulk.insert("", "end", values=(
                            code,
                            f"{data['price']:.2f}" if data['price'] else "",
                            data['stock'],
                            data['oe_code'],
                            data['name'],
                            data['product_type'] or "",
                            status
                        ))
                        fetched_data[code] = data
                        total += 1
                    else:
                        tree_bulk.insert("", "end", values=(code, "", "", "", "", "", "nie znaleziono"))
                except Exception as e:
                    tree_bulk.insert("", "end", values=(code, "", "", "", "", "", f"Błąd: {e}"))
                status_label.config(text=f"Pobieranie... ({total}/{len(codes)})")
                bulk_win.update_idletasks()
            status_label.config(text=f"Pobrano {total} produktów. Kliknij 'Dodaj do bazy danych' aby zapisać.")

        def add_to_database():
            if not fetched_data:
                show_topmost_warning("Uwaga", "Najpierw pobierz dane.", parent=bulk_win)
                return
            added = 0
            skipped = 0
            for code, data in fetched_data.items():
                cur = db.conn.cursor()
                cur.execute("SELECT id FROM products WHERE barcode=%s", (code,))
                if cur.fetchone():
                    skipped += 1
                    for child in tree_bulk.get_children():
                        if tree_bulk.item(child)['values'][0] == code:
                            vals = list(tree_bulk.item(child)['values'])
                            vals[6] = "już istnieje"
                            tree_bulk.item(child, values=vals)
                            break
                    continue
                cur.execute("""
                    INSERT INTO products(barcode, product_code, oe_code, name, models, product_type,
                                         side, position, description, price, stock, signeda_stock,
                                         source, external_id, last_sync)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (code, code, data['oe_code'], data['name'], data['models'],
                      data['product_type'], data['side'], data['position'], data['description'],
                      data['price'], 0, data['stock'], "signeda", data['external_id'], current_timestamp()))
                if data.get('photo_url'):
                    folder = download_product_photo(code, data['photo_url'])
                    if folder:
                        cur.execute("UPDATE products SET photos_folder=%s WHERE barcode=%s", (folder, code))
                db.conn.commit()
                added += 1
                for child in tree_bulk.get_children():
                    if tree_bulk.item(child)['values'][0] == code:
                        vals = list(tree_bulk.item(child)['values'])
                        vals[6] = "dodano"
                        tree_bulk.item(child, values=vals)
                        break
            show_topmost_info("Bulk upload", f"Dodano {added} produktów, pominięto {skipped} (istniejące).", parent=bulk_win)
            load_products_into_tree(tree, "", "", "")
            refresh_filters(category_cb, model_cb)
            refresh_stats(stats_label)
            fetched_data.clear()

    ttk.Button(win, text="Dodaj kody hurtowo (bulk)", command=bulk_upload_window).pack(fill="x", padx=20, pady=5)

    ttk.Button(win, text="Aktualizuj bazę Signeda", command=lambda: update_signeda_window(root, tree, stats_label, category_cb, model_cb)).pack(fill="x", padx=20, pady=5)
    ttk.Button(win, text="Inwentaryzacja", command=lambda: inventory_window(root)).pack(fill="x", padx=20, pady=5)
    ttk.Button(win, text="Dashboard magazynowy", command=lambda: inventory_dashboard_window(root)).pack(fill="x", padx=20, pady=5)
    ttk.Button(win, text="Archiwum używanych unikatów", command=lambda: archived_products_window(root)).pack(fill="x", padx=20, pady=5)
    ttk.Button(win, text="Ulubione produkty", command=lambda: favorite_products_window(root)).pack(fill="x", padx=20, pady=5)
    ttk.Button(win, text="Historia zmian", command=lambda: audit_history_window(root)).pack(fill="x", padx=20, pady=5)
    ttk.Button(win, text="Raporty magazynowe", command=lambda: inventory_reports_window(root)).pack(fill="x", padx=20, pady=5)
    ttk.Button(win, text="Eksport raportu magazynowego", command=lambda: export_stock_report(db.conn)).pack(fill="x", padx=20, pady=5)

# ---------- GŁÓWNE OKNO ----------
logging.info(f"[STARTUP] {APP_NAME} - starting application...")
root = tk.Tk()
root.title(APP_NAME)
root.geometry("1800x900")
root.withdraw()

logging.info(f"[STARTUP] Main window created. Showing login window...")
while True:
    if not show_login_window(root):
        sys.exit(0)
    logging.info(f"[STARTUP] Login completed. Initializing database...")
    
    # Pokazanie splash screen z paskiem progresu
    splash, status_label, progress_bar, progress_var, detail_label = create_splash_screen()
    
    if initialize_database(splash, status_label, progress_bar, progress_var, detail_label):
        logging.info(f"[STARTUP] Database initialized successfully!")
        splash.destroy()
        break
    
    splash.destroy()
    logging.error(f"[STARTUP] Database connection error!")
    if not ask_topmost_yesno("Błąd", "Nie udało się połączyć z bazą. Chcesz spróbować ponownie?", parent=root):
        sys.exit(0)
logging.info(f"[STARTUP] Showing main window...")
root.deiconify()

def bind_shortcuts():
    root.bind_all("<Control-d>", lambda e: add_signeda_window(root, tree, category_cb, model_cb, stats_label))
    root.bind_all("<Control-m>", lambda e: add_manual_window(root, tree, category_cb, model_cb, stats_label))
    def create_order_shortcut(event=None):
        if CURRENT_USER_ROLE == "Magazynier":
            show_topmost_error("Brak uprawnień", "Magazynier nie może tworzyć zleceń", parent=root)
            return
        create_order_window(root, stats_label, tree, category_cb, model_cb)
    root.bind_all("<Control-z>", create_order_shortcut)
    root.bind_all("<F5>", lambda e: refresh_all())

def refresh_all():
    load_products_into_tree(tree, search_var.get(), category_cb.get(), model_cb.get())
    refresh_stats(stats_label)


left_panel = tk.Frame(root, width=250, bg="#e5e5e5")
left_panel.pack(side="left", fill="y")
stats_label = ttk.Label(left_panel, text="", font=("Arial", 10), background="#e5e5e5")
stats_label.pack(pady=10, padx=10, fill="x")
server_info = ttk.Label(left_panel, text=f"Rola: {CURRENT_USER_ROLE or '-'}\nUżytkownik: {CURRENT_USER_NAME or '-'}\nSerwer: {SELECTED_PG_HOST}", font=("Arial", 9), background="#e5e5e5", justify="left")
server_info.pack(pady=(0,10), padx=10, fill="x")
refresh_stats(stats_label)

buttons = [
    ("Przyjmij dostawę", lambda: receive_delivery_window(root, tree, stats_label, category_cb, model_cb)),
    ("Dodaj część Signeda (Ctrl+D)", lambda: add_signeda_window(root, tree, category_cb, model_cb, stats_label)),
    ("Dodaj część ręcznie (Ctrl+M)", lambda: add_manual_window(root, tree, category_cb, model_cb, stats_label)),
    ("Stwórz zlecenie (Ctrl+Z)", lambda: create_order_window(root, stats_label, tree, category_cb, model_cb), "disabled" if CURRENT_USER_ROLE == "Magazynier" else "normal"),
    ("Zlecenia", lambda: orders_list_window(root, stats_label, tree, category_cb, model_cb)),
    ("Sprawdź w bazie", lambda: check_product_window(root, tree, category_cb, model_cb, stats_label)),
    ("Dodatkowe opcje", lambda: additional_options_window(root, tree, stats_label, category_cb, model_cb)),
    ("Raport dla sprzedawcy", lambda: salesperson_report_window(root)),
    ("Eksportuj do CSV", lambda: export_to_csv(tree))
]
for item in buttons:
    text = item[0]
    cmd = item[1]
    state = item[2] if len(item) > 2 else "normal"
    ttk.Button(left_panel, text=text, command=cmd, state=state).pack(fill="x", padx=10, pady=5)

right_panel = tk.Frame(root)
right_panel.pack(side="right", fill="both", expand=True)
top_bar = ttk.Frame(right_panel)
top_bar.pack(fill="x", padx=5, pady=5)
ttk.Label(top_bar, text="Szukaj:").pack(side="left")
search_var = tk.StringVar()
search_entry = ttk.Entry(top_bar, textvariable=search_var, width=30)
search_entry.pack(side="left", padx=5)
ttk.Button(top_bar, text="Odśwież", command=refresh_all).pack(side="left", padx=5)
ttk.Button(top_bar, text="Eksportuj CSV", command=lambda: export_to_csv(tree)).pack(side="right")

filter_frame = tk.Frame(right_panel)
filter_frame.pack(fill="x", padx=5, pady=5)
tk.Label(filter_frame, text="Model").pack(side="left")
model_cb = ttk.Combobox(filter_frame, width=40)
model_cb.pack(side="left", padx=5)
tk.Label(filter_frame, text="Kategoria").pack(side="left")
category_cb = ttk.Combobox(filter_frame, width=30)
category_cb.pack(side="left", padx=5)

columns = ("Kod", "OEM", "Nazwa", "Typ", "Kategoria", "Strona", "Pozycja", "Stan", "Cena", "Signeda", "Modele")
tree = ttk.Treeview(right_panel, columns=columns, show="headings")
for col in columns:
    tree.heading(col, text=col)
    tree.column(col, width=120)
tree.pack(fill="both", expand=True)

def sort_column(tree, col, reverse):
    data = [(tree.set(k, col), k) for k in tree.get_children("")]
    data.sort(reverse=reverse)
    for index, (val, k) in enumerate(data):
        tree.move(k, "", index)
    tree.heading(col, command=lambda: sort_column(tree, col, not reverse))
for col in columns:
    tree.heading(col, command=lambda c=col: sort_column(tree, c, False))

def apply_filters(*args):
    load_products_into_tree(tree, search_var.get(), category_cb.get(), model_cb.get())
search_var.trace("w", apply_filters)
category_cb.bind("<<ComboboxSelected>>", apply_filters)
model_cb.bind("<<ComboboxSelected>>", apply_filters)

def on_tree_double_click(event):
    region = tree.identify_region(event.x, event.y)
    if region != "cell":
        return
    item = tree.identify_row(event.y)
    if item:
        values = tree.item(item)["values"]
        if values:
            barcode = values[0]
            name = values[2]
            oe = values[1]
            price = values[8]
            if active_order_window and active_order_window.winfo_exists():
                add_product_to_active_order(barcode, name, oe, price)
            elif active_edit_window and active_edit_window.winfo_exists():
                add_product_to_edit_order(barcode, name, oe, price)
            elif active_template_window and active_template_window.winfo_exists():
                add_product_to_template(barcode, name, oe, price)
            else:
                show_topmost_warning("Uwaga", "Nie ma otwartego okna zlecenia ani szablonu.\nOtwórz 'Stwórz zlecenie', 'Edytuj zlecenie' lub 'Szablony zamówień'.")

def on_tree_right_click(event):
    region = tree.identify_region(event.x, event.y)
    if region != "cell":
        return
    item = tree.identify_row(event.y)
    if item:
        values = tree.item(item)["values"]
        if values:
            barcode = values[0]
            show_product_details_window(barcode, tree, category_cb, model_cb, stats_label)

# Przypisz podwójne kliknięcie i kliknięcie prawym przyciskiem do drzewa produktów
tree.bind("<Double-1>", on_tree_double_click)
tree.bind("<Button-3>", on_tree_right_click)

logging.info(f"[STARTUP] Loading products in background thread...")

# Load products in a background thread to avoid blocking the UI
def _load_products_background():
    try:
        load_products_into_tree(tree, "", "", "")
        logging.info(f"[STARTUP] Products loaded asynchronously")
    except Exception as e:
        logging.error(f"[STARTUP] Error loading products: {e}")

products_thread = threading.Thread(target=_load_products_background, daemon=True)
products_thread.start()

logging.info(f"[STARTUP] Binding shortcuts...")
bind_shortcuts()
logging.info(f"[STARTUP] Application ready!")
root.mainloop()
