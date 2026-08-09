import os
import sys
import tkinter as tk
from tkinter import ttk
import psycopg2
import configparser
from datetime import datetime
import traceback
from tkinter import messagebox
import logging
import threading

# Setup basic logging to a file for diagnostics
LOG_PATH = os.path.join(os.path.dirname(__file__), 'admin_panel.log')
logging.basicConfig(level=logging.DEBUG, filename=LOG_PATH, filemode='a', format='%(asctime)s - %(levelname)s - %(message)s')
logging.info('admin_panel.py starting')

# Load DB config from parent folder config.ini
BASE_DIR = os.path.dirname(os.path.dirname(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, 'config.ini')
config = configparser.ConfigParser()
config.read(CONFIG_PATH)
pg_config = config['PostgreSQL'] if config.has_section('PostgreSQL') else {}

PG_HOST = pg_config.get('host', '127.0.0.1')
PG_PORT = int(pg_config.get('port', '5432'))
PG_DBNAME = pg_config.get('dbname', 'autocore')
PG_USER = pg_config.get('user', 'postgres')
PG_PASSWORD = pg_config.get('password', 'admin')

class DB:
    def __init__(self, host=None):
        self.conn = None
        self.host = host
        self.connect()

    def connect(self):
        # add short connect_timeout to avoid long blocking if DB is unreachable
        host_to_use = self.host if self.host else PG_HOST
        self.conn = psycopg2.connect(host=host_to_use, port=PG_PORT, dbname=PG_DBNAME, user=PG_USER, password=PG_PASSWORD, connect_timeout=5)
        self.conn.autocommit = True

    def get_salespersons(self):
        cur = self.conn.cursor()
        cur.execute("SELECT id, name FROM salespersons ORDER BY name")
        return cur.fetchall()

    def get_salespersons_with_ip(self):
        cur = self.conn.cursor()
        cur.execute("""
            SELECT s.id, s.name, si.ip_address, si.note
            FROM salespersons s
            LEFT JOIN salesperson_ips si ON si.salesperson_id = s.id
            ORDER BY s.name
        """)
        return cur.fetchall()

    def get_salesperson(self, pid):
        cur = self.conn.cursor()
        cur.execute("SELECT id, name FROM salespersons WHERE id=%s", (pid,))
        return cur.fetchone()

    def update_salesperson_name(self, pid, name):
        cur = self.conn.cursor()
        cur.execute("UPDATE salespersons SET name=%s WHERE id=%s", (name, pid))

    def get_salesperson_ip_mapping(self, pid):
        cur = self.conn.cursor()
        cur.execute("SELECT id, ip_address, note FROM salesperson_ips WHERE salesperson_id=%s LIMIT 1", (pid,))
        return cur.fetchone()

    def upsert_ip_mapping(self, salesperson_id, ip_address, note=None):
        existing = self.get_salesperson_ip_mapping(salesperson_id)
        if existing:
            mid = existing[0]
            if ip_address:
                cur = self.conn.cursor()
                cur.execute("UPDATE salesperson_ips SET ip_address=%s, note=%s WHERE id=%s", (ip_address, note, mid))
                self.conn.commit()
            else:
                self.delete_ip_mapping(mid)
        else:
            if ip_address:
                self.add_ip_mapping(salesperson_id, ip_address, note)

    def add_salesperson(self, name):
        cur = self.conn.cursor()
        cur.execute("INSERT INTO salespersons (name) VALUES (%s) RETURNING id", (name,))
        return cur.fetchone()[0]

    def delete_salesperson(self, pid):
        cur = self.conn.cursor()
        cur.execute("DELETE FROM salespersons WHERE id=%s", (pid,))

    def get_warehouse_workers(self):
        cur = self.conn.cursor()
        cur.execute("SELECT id, name FROM warehouse_workers ORDER BY name")
        return cur.fetchall()

    def add_warehouse_worker(self, name):
        cur = self.conn.cursor()
        cur.execute("INSERT INTO warehouse_workers (name) VALUES (%s) RETURNING id", (name,))
        return cur.fetchone()[0]

    def delete_warehouse_worker(self, pid):
        cur = self.conn.cursor()
        cur.execute("DELETE FROM warehouse_workers WHERE id=%s", (pid,))

    def list_ip_mappings(self):
        cur = self.conn.cursor()
        cur.execute("SELECT id, salesperson_id, ip_address, note FROM salesperson_ips ORDER BY id")
        return cur.fetchall()

    def add_ip_mapping(self, salesperson_id, ip_address, note=None):
        cur = self.conn.cursor()
        cur.execute("INSERT INTO salesperson_ips (salesperson_id, ip_address, note) VALUES (%s, %s, %s) RETURNING id", (salesperson_id, ip_address, note))
        return cur.fetchone()[0]

    def delete_ip_mapping(self, mid):
        cur = self.conn.cursor()
        cur.execute("DELETE FROM salesperson_ips WHERE id=%s", (mid,))

    def get_salesperson_report(self, sp_id, date_from=None, date_to=None):
        cur = self.conn.cursor()
        query = "SELECT id, customer_name, created_at, total_price FROM orders WHERE status='READY' AND salesperson_id=%s"
        params = [sp_id]
        if date_from:
            query += " AND date(created_at) >= %s"
            params.append(date_from)
        if date_to:
            query += " AND date(created_at) <= %s"
            params.append(date_to)
        query += " ORDER BY created_at DESC"
        cur.execute(query, params)
        return cur.fetchall()


class AdminApp:
    def __init__(self, root):
        self.root = root
        self.root.title("AutoCore - Admin Panel")
        self.db = None
        self.create_ui()

    def init_db(self, host=None):
        try:
            self.db = DB(host=host)
            logging.info('DB connected in background')
            # schedule UI refreshes in main thread
            self.root.after(0, self.post_db_init)
        except Exception as e:
            tb = traceback.format_exc()
            logging.error('DB init failed: %s', tb)
            try:
                self.root.after(0, lambda: messagebox.showerror('Błąd DB', f'Nie udało się połączyć z bazą:\n{e}'))
            except Exception:
                pass

    def create_ui(self):
        # Host selection frame
        host_frame = ttk.Frame(self.root)
        host_frame.pack(fill='x', padx=10, pady=5)
        ttk.Label(host_frame, text='Wybierz serwer PostgreSQL:').pack(side='left')
        self.host_var = tk.StringVar(value=PG_HOST)
        ttk.Radiobutton(host_frame, text='Domowy (192.168.1.12)', variable=self.host_var, value='192.168.1.12').pack(side='left', padx=5)
        ttk.Radiobutton(host_frame, text='Firmowy (192.168.100.183)', variable=self.host_var, value='192.168.100.183').pack(side='left', padx=5)
        ttk.Radiobutton(host_frame, text='Inny', variable=self.host_var, value='custom').pack(side='left', padx=5)
        self.custom_host_e = ttk.Entry(host_frame, width=20)
        self.custom_host_e.pack(side='left', padx=5)
        ttk.Button(host_frame, text='Connect', command=self.on_connect).pack(side='left', padx=5)
        self.host_status_label = ttk.Label(host_frame, text='Not connected', foreground='blue')
        self.host_status_label.pack(side='left', padx=10)

        def update_custom_state(*args):
            if self.host_var.get() == 'custom':
                self.custom_host_e.config(state='normal')
            else:
                self.custom_host_e.delete(0, tk.END)
                self.custom_host_e.config(state='disabled')
        self.host_var.trace_add('write', update_custom_state)
        update_custom_state()

        nb = ttk.Notebook(self.root)
        nb.pack(fill='both', expand=True)

        # Salespersons tab
        self.sales_frame = ttk.Frame(nb)
        nb.add(self.sales_frame, text='Handlowcy')
        self.sales_tree = ttk.Treeview(self.sales_frame, columns=("id","name"), show='headings')
        self.sales_tree.heading("id", text="ID")
        self.sales_tree.heading("name", text="Nazwa")
        self.sales_tree.pack(fill='both', expand=True, padx=10, pady=10)
        self.sales_tree.bind('<Button-3>', self.on_sales_right_click)
        btnf = ttk.Frame(self.sales_frame)
        btnf.pack(pady=5)
        self.sales_refresh_btn = ttk.Button(btnf, text='Odśwież', command=self.refresh_sales, state='disabled')
        self.sales_refresh_btn.pack(side='left', padx=5)
        self.sales_add_btn = ttk.Button(btnf, text='Dodaj', command=self.add_salesperson, state='disabled')
        self.sales_add_btn.pack(side='left', padx=5)
        self.sales_del_btn = ttk.Button(btnf, text='Usuń', command=self.delete_salesperson, state='disabled')
        self.sales_del_btn.pack(side='left', padx=5)

        # Warehouse tab
        self.work_frame = ttk.Frame(nb)
        nb.add(self.work_frame, text='Magazynierzy')
        self.work_tree = ttk.Treeview(self.work_frame, columns=("id","name"), show='headings')
        self.work_tree.heading("id", text="ID")
        self.work_tree.heading("name", text="Nazwa")
        self.work_tree.pack(fill='both', expand=True, padx=10, pady=10)
        wbtnf = ttk.Frame(self.work_frame)
        wbtnf.pack(pady=5)
        self.work_refresh_btn = ttk.Button(wbtnf, text='Odśwież', command=self.refresh_workers, state='disabled')
        self.work_refresh_btn.pack(side='left', padx=5)
        self.work_add_btn = ttk.Button(wbtnf, text='Dodaj', command=self.add_worker, state='disabled')
        self.work_add_btn.pack(side='left', padx=5)
        self.work_del_btn = ttk.Button(wbtnf, text='Usuń', command=self.delete_worker, state='disabled')
        self.work_del_btn.pack(side='left', padx=5)

        # IP mappings tab
        self.ip_frame = ttk.Frame(nb)
        nb.add(self.ip_frame, text='Przypisania IP')
        self.ip_tree = ttk.Treeview(self.ip_frame, columns=("id","salesperson","ip","note"), show='headings')
        for col, text in [("id","ID"),("salesperson","Handlowiec"),("ip","IP"),("note","Notatka")]:
            self.ip_tree.heading(col, text=text)
        self.ip_tree.pack(fill='both', expand=True, padx=10, pady=10)
        self.ip_tree.bind('<Button-3>', self.on_ip_right_click)
        ipbtnf = ttk.Frame(self.ip_frame)
        ipbtnf.pack(pady=5)
        self.ip_refresh_btn = ttk.Button(ipbtnf, text='Odśwież', command=self.refresh_ips, state='disabled')
        self.ip_refresh_btn.pack(side='left', padx=5)
        self.ip_add_btn = ttk.Button(ipbtnf, text='Dodaj', command=self.add_ip, state='disabled')
        self.ip_add_btn.pack(side='left', padx=5)
        self.ip_del_btn = ttk.Button(ipbtnf, text='Usuń', command=self.delete_ip, state='disabled')
        self.ip_del_btn.pack(side='left', padx=5)

        # Reports tab
        self.rep_frame = ttk.Frame(nb)
        nb.add(self.rep_frame, text='Raporty')
        topf = ttk.Frame(self.rep_frame)
        topf.pack(fill='x', padx=10, pady=5)
        ttk.Label(topf, text='Handlowiec:').pack(side='left')
        self.rep_combo = ttk.Combobox(topf, width=40)
        self.rep_combo.pack(side='left', padx=5)
        ttk.Label(topf, text='Od (YYYY-MM-DD):').pack(side='left', padx=5)
        self.rfrom = ttk.Entry(topf, width=12)
        self.rfrom.pack(side='left')
        ttk.Label(topf, text='Do:').pack(side='left', padx=5)
        self.rto = ttk.Entry(topf, width=12)
        self.rto.pack(side='left')
        ttk.Button(topf, text='Generuj', command=self.generate_report).pack(side='left', padx=5)

        self.rep_tree = ttk.Treeview(self.rep_frame, columns=("id","client","date","sum"), show='headings')
        for c,t in [("id","Zlecenie ID"),("client","Klient"),("date","Data"),("sum","Suma")]:
            self.rep_tree.heading(c, text=t)
        self.rep_tree.pack(fill='both', expand=True, padx=10, pady=10)
        self.total_label = ttk.Label(self.rep_frame, text='')
        self.total_label.pack(pady=5)

        # Initial load will be performed after DB initialization

    def post_db_init(self):
        try:
            self.refresh_sales()
            self.refresh_workers()
            self.refresh_ips()
            self.refresh_report_combo()
        except Exception as e:
            logging.error('Error during post_db_init: %s', traceback.format_exc())
        # enable buttons now
        try:
            self.sales_refresh_btn.config(state='normal')
            self.sales_add_btn.config(state='normal')
            self.sales_del_btn.config(state='normal')
            self.work_refresh_btn.config(state='normal')
            self.work_add_btn.config(state='normal')
            self.work_del_btn.config(state='normal')
            self.ip_refresh_btn.config(state='normal')
            self.ip_add_btn.config(state='normal')
            self.ip_del_btn.config(state='normal')
            self.host_status_label.config(text=f'Connected to {self.db.conn.dsn.split()[0]}')
        except Exception:
            pass

    def on_connect(self):
        sel = self.host_var.get()
        host = None
        if sel == 'custom':
            host = self.custom_host_e.get().strip()
            if not host:
                show_error(self.root, 'Podaj adres IP serwera')
                return
        else:
            host = sel
        self.host_status_label.config(text=f'Connecting to {host}...')
        threading.Thread(target=self.init_db, args=(host,), daemon=True).start()

    def refresh_sales(self):
        for r in self.sales_tree.get_children():
            self.sales_tree.delete(r)
        for pid, name in self.db.get_salespersons():
            self.sales_tree.insert('', 'end', values=(pid, name))

    def add_salesperson(self):
        name = simple_input(self.root, 'Dodaj handlowca', 'Imię i nazwisko:')
        if name:
            try:
                self.db.add_salesperson(name.strip())
                self.refresh_sales()
                self.refresh_report_combo()
            except Exception as e:
                show_error(self.root, str(e))

    def on_sales_right_click(self, event):
        iid = self.sales_tree.identify_row(event.y)
        if not iid:
            return
        self.sales_tree.selection_set(iid)
        values = self.sales_tree.item(iid, 'values')
        if not values:
            return
        pid = int(values[0])
        self.edit_salesperson(pid)

    def edit_salesperson(self, pid):
        sp = self.db.get_salesperson(pid)
        if not sp:
            show_error(self.root, 'Nie znaleziono handlowca')
            return
        current_name = sp[1]
        current_mapping = self.db.get_salesperson_ip_mapping(pid)
        current_ip = current_mapping[1] if current_mapping else ''
        current_note = current_mapping[2] if current_mapping else ''

        win = tk.Toplevel(self.root)
        win.title('Edycja handlowca')
        win.geometry('420x260')
        ttk.Label(win, text='Imię i nazwisko:').pack(padx=10, pady=(10, 2), anchor='w')
        name_entry = ttk.Entry(win, width=50)
        name_entry.pack(padx=10, pady=5)
        name_entry.insert(0, current_name)

        ttk.Label(win, text='IP urządzenia pracownika (opcjonalne):').pack(padx=10, pady=(10, 2), anchor='w')
        ip_entry = ttk.Entry(win, width=50)
        ip_entry.pack(padx=10, pady=5)
        ip_entry.insert(0, current_ip)

        ttk.Label(win, text='Notatka do IP:').pack(padx=10, pady=(10, 2), anchor='w')
        note_entry = ttk.Entry(win, width=50)
        note_entry.pack(padx=10, pady=5)
        note_entry.insert(0, current_note)

        def save():
            name = name_entry.get().strip()
            ip = ip_entry.get().strip()
            note = note_entry.get().strip() or None
            if not name:
                show_error(win, 'Nazwa nie może być pusta')
                return
            try:
                self.db.update_salesperson_name(pid, name)
                self.db.upsert_ip_mapping(pid, ip if ip else None, note)
                self.refresh_sales()
                self.refresh_ips()
                self.refresh_report_combo()
                win.destroy()
            except Exception as e:
                show_error(win, str(e))

        btn_frame = ttk.Frame(win)
        btn_frame.pack(pady=10)
        ttk.Button(btn_frame, text='Zapisz', command=save).pack(side='left', padx=5)
        ttk.Button(btn_frame, text='Anuluj', command=win.destroy).pack(side='left', padx=5)

    def delete_salesperson(self):
        sel = self.sales_tree.selection()
        if not sel:
            return
        pid = int(self.sales_tree.item(sel[0])['values'][0])
        if ask_yesno(self.root, 'Usuń', f'Usunąć handlowca ID {pid}?'):
            self.db.delete_salesperson(pid)
            self.refresh_sales()
            self.refresh_report_combo()

    def refresh_workers(self):
        for r in self.work_tree.get_children():
            self.work_tree.delete(r)
        for pid, name in self.db.get_warehouse_workers():
            self.work_tree.insert('', 'end', values=(pid, name))

    def add_worker(self):
        name = simple_input(self.root, 'Dodaj magazyniera', 'Imię i nazwisko:')
        if name:
            self.db.add_warehouse_worker(name.strip())
            self.refresh_workers()

    def delete_worker(self):
        sel = self.work_tree.selection()
        if not sel:
            return
        pid = int(self.work_tree.item(sel[0])['values'][0])
        if ask_yesno(self.root, 'Usuń', f'Usunąć magazyniera ID {pid}?'):
            self.db.delete_warehouse_worker(pid)
            self.refresh_workers()

    def refresh_ips(self):
        for r in self.ip_tree.get_children():
            self.ip_tree.delete(r)
        for mid, sid, ip, note in self.db.list_ip_mappings():
            cur = self.db.conn.cursor()
            cur.execute('SELECT name FROM salespersons WHERE id=%s', (sid,))
            row = cur.fetchone()
            name = row[0] if row else str(sid)
            self.ip_tree.insert('', 'end', values=(mid, f"{sid}: {name}", ip, note or ''))

    def add_ip(self):
        sales = self.db.get_salespersons()
        if not sales:
            show_error(self.root, 'Brak handlowców. Dodaj najpierw handlowca.')
            return
        sel = simple_input(self.root, 'Wybierz handlowca', 'Wklej "ID: Nazwa" z listy:\n' + '\n'.join([f"{p}: {n}" for p,n in sales]))
        if not sel:
            return
        try:
            sid = int(sel.split(':')[0])
        except Exception:
            show_error(self.root, 'Niepoprawny format handlowca')
            return
        ip = simple_input(self.root, 'Adres IP', 'Wpisz adres IP (np. 192.168.1.45):')
        if not ip:
            return
        note = simple_input(self.root, 'Notatka', 'Notatka (opcjonalnie):')
        self.db.add_ip_mapping(sid, ip.strip(), note)
        self.refresh_ips()

    def delete_ip(self):
        sel = self.ip_tree.selection()
        if not sel:
            return
        mid = int(self.ip_tree.item(sel[0])['values'][0])
        if ask_yesno(self.root, 'Usuń', f'Usunąć przypisanie ID {mid}?'):
            self.db.delete_ip_mapping(mid)
            self.refresh_ips()

    def on_ip_right_click(self, event):
        iid = self.ip_tree.identify_row(event.y)
        if not iid:
            return
        self.ip_tree.selection_set(iid)
        values = self.ip_tree.item(iid, 'values')
        if not values:
            return
        mid = int(values[0])
        self.edit_ip_mapping(mid)

    def edit_ip_mapping(self, mid):
        cur = self.db.conn.cursor()
        cur.execute('SELECT salesperson_id, ip_address, note FROM salesperson_ips WHERE id=%s', (mid,))
        row = cur.fetchone()
        if not row:
            show_error(self.root, 'Nie znaleziono przypisania IP')
            return
        sid, ip_address, note = row
        cur.execute('SELECT name FROM salespersons WHERE id=%s', (sid,))
        salesperson_name = cur.fetchone()
        salesperson_text = f"{sid}: {salesperson_name[0]}" if salesperson_name else str(sid)

        win = tk.Toplevel(self.root)
        win.title('Edycja przypisania IP')
        win.geometry('420x240')

        ttk.Label(win, text='Handlowiec:').pack(padx=10, pady=(10, 2), anchor='w')
        ttk.Label(win, text=salesperson_text).pack(padx=10, pady=5, anchor='w')

        ttk.Label(win, text='Adres IP:').pack(padx=10, pady=(10, 2), anchor='w')
        ip_entry = ttk.Entry(win, width=50)
        ip_entry.pack(padx=10, pady=5)
        ip_entry.insert(0, ip_address or '')

        ttk.Label(win, text='Notatka:').pack(padx=10, pady=(10, 2), anchor='w')
        note_entry = ttk.Entry(win, width=50)
        note_entry.pack(padx=10, pady=5)
        note_entry.insert(0, note or '')

        def save():
            new_ip = ip_entry.get().strip()
            new_note = note_entry.get().strip() or None
            try:
                if new_ip:
                    cur2 = self.db.conn.cursor()
                    cur2.execute('UPDATE salesperson_ips SET ip_address=%s, note=%s WHERE id=%s', (new_ip, new_note, mid))
                else:
                    self.db.delete_ip_mapping(mid)
                self.refresh_ips()
                win.destroy()
            except Exception as e:
                show_error(win, str(e))

        btn_frame = ttk.Frame(win)
        btn_frame.pack(pady=10)
        ttk.Button(btn_frame, text='Zapisz', command=save).pack(side='left', padx=5)
        ttk.Button(btn_frame, text='Anuluj', command=win.destroy).pack(side='left', padx=5)

    def refresh_report_combo(self):
        sales = self.db.get_salespersons()
        self.rep_combo['values'] = [f"{p}: {n}" for p,n in sales]

    def generate_report(self):
        sel = self.rep_combo.get()
        if not sel:
            show_error(self.root, 'Wybierz handlowca')
            return
        sp_id = int(sel.split(':')[0])
        df = self.rfrom.get().strip() or None
        dt = self.rto.get().strip() or None
        rows = self.db.get_salesperson_report(sp_id, df, dt)
        for r in self.rep_tree.get_children():
            self.rep_tree.delete(r)
        total = 0.0
        for row in rows:
            oid, client, created_at, total_price = row
            created_s = created_at.strftime('%Y-%m-%d %H:%M:%S') if isinstance(created_at, datetime) else str(created_at)
            self.rep_tree.insert('', 'end', values=(oid, client, created_s, f"{total_price:.2f}"))
            total += float(total_price or 0)
        self.total_label.config(text=f"Łączna wartość: {total:.2f} zł")


# Simple helper dialogs

def simple_input(parent, title, prompt):
    win = tk.Toplevel(parent)
    win.title(title)
    tk.Label(win, text=prompt).pack(padx=10, pady=10)
    e = tk.Entry(win, width=60)
    e.pack(padx=10, pady=5)
    res = {'val': None}
    def ok():
        res['val'] = e.get()
        win.destroy()
    tk.Button(win, text='OK', command=ok).pack(pady=10)
    win.grab_set()
    win.wait_window()
    return res['val']


def ask_yesno(parent, title, prompt):
    win = tk.Toplevel(parent)
    win.title(title)
    tk.Label(win, text=prompt).pack(padx=10, pady=10)
    res = {'val': False}
    def yes():
        res['val'] = True
        win.destroy()
    def no():
        res['val'] = False
        win.destroy()
    tk.Button(win, text='Tak', command=yes).pack(side='left', padx=10, pady=10)
    tk.Button(win, text='Nie', command=no).pack(side='right', padx=10, pady=10)
    win.grab_set()
    win.wait_window()
    return res['val']


def show_error(parent, message):
    win = tk.Toplevel(parent)
    win.title('Błąd')
    tk.Label(win, text=message, fg='red').pack(padx=10, pady=10)
    tk.Button(win, text='OK', command=win.destroy).pack(pady=10)
    win.grab_set()
    win.wait_window()


if __name__ == '__main__':
    logging.info('Creating Tk root')
    root = tk.Tk()
    root.geometry('900x700')
    # Try to ensure the window shows on top (helps when window is off-screen/behind console)
    root.attributes('-topmost', True)
    try:
        app = AdminApp(root)
        logging.info('AdminApp initialized')
    except Exception as e:
        tb = traceback.format_exc()
        logging.error('Exception during AdminApp initialization: %s', tb)
        try:
            messagebox.showerror('Błąd', f'Nie udało się uruchomić panelu admina:\n{e}')
        except Exception:
            pass
        sys.exit(1)
    # lift and remove topmost shortly after to allow normal window behavior
    root.lift()
    root.after(200, lambda: root.attributes('-topmost', False))
    try:
        root.mainloop()
    except Exception:
        logging.error('Exception in mainloop:\n%s', traceback.format_exc())
    logging.info('Mainloop ended')
