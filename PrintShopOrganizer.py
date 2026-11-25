"""
Print Shop Organizer
--------------------
* Watches your Downloads folder. New files wait in the INBOX: you type the
  customer name once (for many files at the same time), click Save, and the
  file goes to:   Library / <Type> / <Year-Month> / <Date>_<Customer>_<Note>_<Old name>
* Cleans old messy folders with one click (with Undo)
* PROTECTED folders: the program never touches them (auto, or sub-folder mode)
* Very fast search (SQLite full-text index) - searches names, customers AND PDF text

Needs only Python 3 (tkinter is included). Optional: pip install pillow pypdf
"""
import os
import re
import sys
import json
import time
import shutil
import sqlite3
import threading
import tempfile
import subprocess
import urllib.request
from datetime import datetime
from pathlib import Path

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

try:
    from PIL import Image          # optional: image size
except Exception:
    Image = None
try:
    from pypdf import PdfReader    # optional: PDF text search
except Exception:
    PdfReader = None

# GitHub fills these two lines in automatically when it builds the .exe.
APP_VERSION = "0"
GITHUB_REPO = ""

# ----------------------------------------------------------------- settings
# You can edit these two tables to fit your shop.
CATEGORIES = {
    "PDF":          {".pdf"},
    "Design Files": {".psd", ".ai", ".cdr", ".indd", ".eps", ".xd", ".fig",
                     ".sketch", ".afdesign", ".svg", ".dxf"},
    "Images":       {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".gif",
                     ".webp", ".heic", ".raw"},
    "Documents":    {".doc", ".docx", ".txt", ".rtf", ".odt", ".ppt", ".pptx"},
    "Spreadsheets": {".xls", ".xlsx", ".csv", ".ods"},
    "Fonts":        {".ttf", ".otf", ".woff", ".woff2"},
    "Archives":     {".zip", ".rar", ".7z"},
    "Videos":       {".mp4", ".mov", ".avi", ".mkv"},
    "Audio":        {".mp3", ".wav"},
    "Installers":   {".exe", ".msi"},
}

# Job type  ->  words that identify it in a file name (used only for search)
JOBS = {
    "Banner":        ["banner", "flex", "bunting"],
    "Business-Card": ["card", "visiting"],
    "Sticker":       ["sticker", "label", "decal"],
    "Invoice":       ["invoice", "bill", "receipt", "challan", "voucher"],
    "Quotation":     ["quotation", "quote", "estimate"],
    "Brochure":      ["brochure", "flyer", "leaflet", "catalog", "catalogue"],
    "Poster":        ["poster"],
    "Packaging":     ["box", "package", "packaging", "carton"],
    "T-Shirt":       ["tshirt", "t-shirt", "shirt", "jersey"],
    "Mug":           ["mug"],
    "Logo":          ["logo"],
    "Letterhead":    ["letterhead"],
    "Calendar":      ["calendar"],
    "Invitation":    ["invitation", "wedding"],
    "ID-Card":       ["idcard", "id card", "id-card"],
}

TEMP_EXT = {".crdownload", ".tmp", ".part", ".partial", ".download", ".opdownload"}
APP_DIR = Path.home() / ".printshop_organizer"
APP_DIR.mkdir(exist_ok=True)
CONFIG_FILE = APP_DIR / "config.json"
DB_FILE = APP_DIR / "index.db"

DONE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_")   # already named by this app
GENERIC_RE = re.compile(
    r"^(img|dsc|dscn|image|photo|scan|scanned|document|download|untitled|new|file|"
    r"whatsapp image|screenshot|pic)[\s]*\d*$", re.I)


def load_config():
    cfg = {
        "watch_folders": [str(Path.home() / "Downloads")],
        "protected_folders": [],
        "library": str(Path.home() / "Documents" / "PrintShop Library"),
        "auto": True,          # watch the folders at all
        "ask": True,           # new files wait in the Inbox for a customer name
    }
    try:
        saved = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        old = saved.pop("watch", None)             # upgrade from older version
        if old and "watch_folders" not in saved:
            saved["watch_folders"] = [old]
        cfg.update(saved)
    except Exception:
        pass
    return cfg


def save_config(cfg):
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def norm(s):
    return re.sub(r"[_\-\.\s]+", " ", s or "").strip()


def human_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def get_category(ext):
    for cat, exts in CATEGORIES.items():
        if ext in exts:
            return cat
    return "Other"


def detect_job(text):
    t = text.lower()
    for job, words in JOBS.items():
        if any(w in t for w in words):
            return job
    return ""


def clean_stem(stem):
    s = re.sub(r"\s*\(\d+\)\s*$", "", stem)                 # "file (1)"
    s = re.sub(r"[\s_\-]+copy(\s*\d+)?$", "", s, flags=re.I)  # "file - Copy"
    s = re.sub(r'[<>:"/\\|?*]', "", s)
    s = re.sub(r"[_\.\s]+", " ", s).strip(" -")
    return s[:60]


def slug(s, limit=40):
    """Customer / note text -> safe piece of a file name (Bangla letters are kept)."""
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", s or "")
    s = re.sub(r"[\s_\.]+", "-", s).strip(" -")
    return s[:limit]


def same_path(a, b):
    return os.path.normcase(os.path.realpath(str(a))) == os.path.normcase(os.path.realpath(str(b)))


def is_under(path, root):
    """True if `path` is `root` itself or anywhere inside it."""
    p = os.path.normcase(os.path.realpath(str(path)))
    r = os.path.normcase(os.path.realpath(str(root))).rstrip("\\/")
    return p == r or p.startswith(r + os.sep)


def extract_info(path, cat):
    """Return (extra_label, searchable_text)."""
    ext = path.suffix.lower()
    extra, text = "", ""
    try:
        if ext == ".pdf" and PdfReader:
            r = PdfReader(str(path))
            extra = f"{len(r.pages)}p"
            for p in r.pages[:3]:
                text += (p.extract_text() or "")[:2000] + " "
        elif cat == "Images" and Image and ext not in (".heic", ".raw"):
            with Image.open(path) as im:
                extra = f"{im.width}x{im.height}px"
        elif ext in (".txt", ".csv"):
            text = path.read_text(encoding="utf-8", errors="ignore")[:5000]
    except Exception:
        pass
    return extra, text


# -------------------------------------------------------------------- index
class Index:
    def __init__(self):
        self.lock = threading.Lock()
        self.db = sqlite3.connect(DB_FILE, check_same_thread=False)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY, path TEXT UNIQUE,"
            " name TEXT, orig TEXT, cat TEXT, tag TEXT, date TEXT, size INTEGER, content TEXT)")
        cols = [r[1] for r in self.db.execute("PRAGMA table_info(files)")]
        for col in ("customer", "note"):                 # upgrade older databases
            if col not in cols:
                self.db.execute(f"ALTER TABLE files ADD COLUMN {col} TEXT DEFAULT ''")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS moves(id INTEGER PRIMARY KEY, batch TEXT, src TEXT, dst TEXT)")
        self.fts = False
        try:
            fresh = self.db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='fts2'").fetchone() is None
            self.db.execute("DROP TABLE IF EXISTS fts")  # old layout
            self.db.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS fts2 USING "
                "fts5(name, orig, cat, tag, content, customer, note)")
            self.fts = True
            if fresh:                                    # fill it from existing rows
                for r in self.db.execute(
                        "SELECT id,name,orig,cat,tag,content,customer,note FROM files").fetchall():
                    self.db.execute(
                        "INSERT INTO fts2(rowid,name,orig,cat,tag,content,customer,note)"
                        " VALUES(?,?,?,?,?,?,?,?)",
                        (r[0], norm(r[1]), norm(r[2]), r[3], r[4], r[5], r[6] or "", r[7] or ""))
        except sqlite3.OperationalError:
            self.fts = False
        self.db.commit()

    def add(self, path, orig, cat, tag, date, size, content="", customer="", note=""):
        name = os.path.basename(path)
        with self.lock:
            row = self.db.execute("SELECT id FROM files WHERE path=?", (path,)).fetchone()
            if row:
                fid = row[0]
                self.db.execute(
                    "UPDATE files SET name=?,orig=?,cat=?,tag=?,date=?,size=?,content=?,"
                    "customer=?,note=? WHERE id=?",
                    (name, orig, cat, tag, date, size, content, customer, note, fid))
                if self.fts:
                    self.db.execute("DELETE FROM fts2 WHERE rowid=?", (fid,))
            else:
                fid = self.db.execute(
                    "INSERT INTO files(path,name,orig,cat,tag,date,size,content,customer,note)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (path, name, orig, cat, tag, date, size, content, customer, note)).lastrowid
            if self.fts:
                self.db.execute(
                    "INSERT INTO fts2(rowid,name,orig,cat,tag,content,customer,note)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (fid, norm(name), norm(orig), cat, tag, content, customer, note))
            self.db.commit()

    def remove(self, path):
        with self.lock:
            row = self.db.execute("SELECT id FROM files WHERE path=?", (path,)).fetchone()
            if row:
                if self.fts:
                    self.db.execute("DELETE FROM fts2 WHERE rowid=?", (row[0],))
                self.db.execute("DELETE FROM files WHERE id=?", (row[0],))
                self.db.commit()

    def known(self, path):
        with self.lock:
            return self.db.execute("SELECT 1 FROM files WHERE path=?", (path,)).fetchone() is not None

    def all_paths(self):
        with self.lock:
            return [r[0] for r in self.db.execute("SELECT path FROM files")]

    def recent_customers(self, limit=40):
        with self.lock:
            return [r[0] for r in self.db.execute(
                "SELECT customer FROM files WHERE customer<>'' "
                "GROUP BY customer ORDER BY MAX(id) DESC LIMIT ?", (limit,))]

    def log_move(self, batch, src, dst):
        with self.lock:
            self.db.execute("INSERT INTO moves(batch,src,dst) VALUES(?,?,?)", (batch, src, dst))
            self.db.commit()

    def last_batch(self):
        with self.lock:
            r = self.db.execute("SELECT batch FROM moves ORDER BY id DESC LIMIT 1").fetchone()
            if not r:
                return []
            rows = self.db.execute(
                "SELECT id,src,dst FROM moves WHERE batch=? ORDER BY id DESC", (r[0],)).fetchall()
            return rows

    def delete_moves(self, ids):
        with self.lock:
            self.db.executemany("DELETE FROM moves WHERE id=?", [(i,) for i in ids])
            self.db.commit()

    def search(self, q, limit=500):
        cols = "f.name,f.cat,f.customer,f.date,f.size,f.path"
        terms = re.findall(r"\w+", q)
        with self.lock:
            if not terms:
                return self.db.execute(
                    f"SELECT {cols} FROM files f ORDER BY f.date DESC, f.id DESC LIMIT ?",
                    (limit,)).fetchall()
            if self.fts:
                match = " ".join(f'"{t}"*' for t in terms)
                try:
                    rows = self.db.execute(
                        f"SELECT {cols} FROM fts2 JOIN files f ON f.id=fts2.rowid "
                        f"WHERE fts2 MATCH ? ORDER BY f.date DESC LIMIT ?", (match, limit)).fetchall()
                    if rows:
                        return rows
                except sqlite3.OperationalError:
                    pass
            where = " AND ".join("(f.name LIKE ? OR f.orig LIKE ? OR f.customer LIKE ?)"
                                 for _ in terms)
            params = []
            for t in terms:
                params += [f"%{t}%"] * 3
            return self.db.execute(
                f"SELECT {cols} FROM files f WHERE {where} ORDER BY f.date DESC LIMIT ?",
                params + [limit]).fetchall()


# ---------------------------------------------------------------- organizer
class Organizer:
    def __init__(self, cfg, index):
        self.cfg, self.index = cfg, index
        self.lock = threading.Lock()

    @property
    def library(self):
        return Path(self.cfg["library"])

    # ---- protection: folders the program must never touch
    def is_protected(self, path):
        if is_under(path, self.library):
            return True
        return any(is_under(path, f) for f in self.cfg.get("protected_folders", []))

    def _holds_protected(self, folder):
        """True if a protected folder sits inside `folder` (so it must not be removed)."""
        return any(is_under(f, folder) for f in self.cfg.get("protected_folders", []))

    # ---- helpers
    @staticmethod
    def skip_name(p):
        name = p.name
        return (p.suffix.lower() in TEMP_EXT or name.startswith(("~$", ".")) or
                name.lower() in ("desktop.ini", "thumbs.db"))

    def ready(self, p):
        try:
            if time.time() - p.stat().st_mtime < 3:
                return False
            with open(p, "ab"):
                pass
            return True
        except OSError:
            return False

    @staticmethod
    def _unique(path):
        if not path.exists():
            return path
        i = 2
        while True:
            cand = path.with_name(f"{path.stem}_{i}{path.suffix}")
            if not cand.exists():
                return cand
            i += 1

    def build_stem(self, src, date, customer, note, text):
        """Date_Customer_Note_OldName"""
        if DONE_RE.match(src.stem):
            return src.stem
        clean = clean_stem(src.stem)
        if GENERIC_RE.match(clean) and text.strip():
            clean = " ".join(re.findall(r"\w+", text)[:6])          # name from PDF text
        parts = [date]
        cs, ns = slug(customer), slug(note)
        if cs:
            parts.append(cs)
        if ns:
            parts.append(ns)
        old = clean.replace(" ", "-") or "file"
        if old.lower() != cs.lower():
            parts.append(old)
        return "_".join(parts)

    # ---- moving
    def process(self, src, batch, customer="", note=""):
        if self.skip_name(src) or self.is_protected(src.parent) or not self.ready(src):
            return False
        st = src.stat()
        d = datetime.fromtimestamp(st.st_mtime)
        date = d.strftime("%Y-%m-%d")
        cat = get_category(src.suffix.lower())
        _, text = extract_info(src, cat)
        tag = detect_job(clean_stem(src.stem)) or detect_job(text[:300])
        new_stem = self.build_stem(src, date, customer, note, text)

        dest_dir = self.library / cat / d.strftime("%Y-%m")
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = self._unique(dest_dir / (new_stem + src.suffix.lower()))
        try:
            shutil.move(str(src), str(dest))
        except Exception:
            return False
        self.index.log_move(batch, str(src), str(dest))
        self.index.add(str(dest), src.name, cat, tag, date, st.st_size, text,
                       slug(customer), slug(note))
        return True

    def process_many(self, items):
        """items: list of (path, customer, note). Returns number of files moved."""
        n = 0
        with self.lock:
            batch = datetime.now().strftime("%Y%m%d%H%M%S%f")
            for path, customer, note in items:
                n += bool(self.process(Path(path), batch, customer, note))
        return n

    def _collect(self, folder, recursive):
        """Files that would be organized + number of protected folders skipped."""
        folder = Path(folder)
        files, skipped = [], 0
        if self.is_protected(folder):
            return files, 1
        if not recursive:
            for p in folder.iterdir():
                if p.is_file() and not self.skip_name(p):
                    files.append(p)
            return files, 0
        for root, dirs, names in os.walk(folder):
            keep = []
            for x in dirs:
                if x.startswith("."):
                    continue
                if self.is_protected(os.path.join(root, x)):
                    skipped += 1
                    continue
                keep.append(x)
            dirs[:] = keep
            for f in names:
                p = Path(root) / f
                if not self.skip_name(p):
                    files.append(p)
        return files, skipped

    def count_candidates(self, folder, recursive):
        files, skipped = self._collect(folder, recursive)
        return len(files), skipped

    def sweep(self, folder, recursive=False):
        """Organize every file in `folder`. Returns number of files moved."""
        folder = Path(folder)
        if not folder.exists() or not self.lock.acquire(blocking=False):
            return 0
        count = 0
        try:
            batch = datetime.now().strftime("%Y%m%d%H%M%S%f")
            files, _ = self._collect(folder, recursive)
            for p in files:
                count += bool(self.process(p, batch))
            if recursive:
                for root, dirs, names in os.walk(folder, topdown=False):
                    if (Path(root) == folder or self.is_protected(root)
                            or self._holds_protected(root)):
                        continue
                    try:
                        os.rmdir(root)              # remove folders left empty
                    except OSError:
                        pass
        finally:
            self.lock.release()
        return count

    def reindex(self):
        """Index files already in the library; drop entries for missing files."""
        added = 0
        for path in self.index.all_paths():
            if not os.path.exists(path):
                self.index.remove(path)
        lib = self.library
        if not lib.exists():
            return 0
        for root, _, files in os.walk(lib):
            for f in files:
                p = Path(root) / f
                if self.index.known(str(p)):
                    continue
                try:
                    st = p.stat()
                    rel = p.relative_to(lib).parts
                    cat = rel[0] if len(rel) > 1 else get_category(p.suffix.lower())
                    _, text = extract_info(p, cat)
                    self.index.add(str(p), f, cat, detect_job(f),
                                   datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d"),
                                   st.st_size, text)
                    added += 1
                except Exception:
                    pass
        return added

    def undo_last(self):
        rows = self.index.last_batch()
        n = 0
        for mid, src, dst in rows:
            try:
                if os.path.exists(dst):
                    target = self._unique(Path(src))
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(dst, str(target))
                    self.index.remove(dst)
                    n += 1
            except Exception:
                pass
        self.index.delete_moves([r[0] for r in rows])
        return n


# ---------------------------------------------------------------------- GUI
def open_path(path):
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception as e:
        messagebox.showerror("Cannot open", str(e))


def show_in_folder(path):
    if sys.platform.startswith("win"):
        subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
    else:
        open_path(os.path.dirname(path))


# ------------------------------------------------------------- auto-update
def is_frozen():
    return bool(getattr(sys, "frozen", False))


def _get(url, timeout=15):
    return urllib.request.urlopen(
        urllib.request.Request(url, headers={"User-Agent": "PrintShopOrganizer"}), timeout=timeout)


def check_update():
    """Return info dict if a newer release exists on GitHub, else None."""
    if not (is_frozen() and GITHUB_REPO):
        return None
    with _get(f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest") as r:
        data = json.load(r)
    tag = str(data.get("tag_name", "")).lstrip("vV")
    if not tag.isdigit() or not APP_VERSION.isdigit() or int(tag) <= int(APP_VERSION):
        return None
    for a in data.get("assets", []):
        if a.get("name", "").lower().endswith(".exe"):
            return {"tag": tag, "url": a["browser_download_url"],
                    "size": a.get("size", 0), "notes": data.get("body") or ""}
    return None


class App(tk.Tk):
    COLS = [("name", "Name", 380), ("customer", "Customer", 120), ("cat", "Type", 90),
            ("date", "Date", 85), ("size", "Size", 70), ("folder", "Folder", 190)]

    def __init__(self):
        super().__init__()
        self.title("Print Shop Organizer" + (f"  v{APP_VERSION}" if APP_VERSION != "0" else ""))
        self.geometry("1100x720")
        self.cfg = load_config()
        self.index = Index()
        self.org = Organizer(self.cfg, self.index)
        self.rows = []
        self._job = None
        self._asked = set()
        self.pending = {}                  # path -> {"customer": "", "note": ""}
        self.plock = threading.Lock()
        self._build()
        self.auto.set(bool(self.cfg.get("auto", True)))
        self.ask.set(bool(self.cfg.get("ask", True)))
        threading.Thread(target=self._watch_loop, daemon=True).start()
        threading.Thread(target=self._initial_index, daemon=True).start()
        threading.Thread(target=self._update_loop, daemon=True).start()
        self.do_search()

    # ---- layout
    def _build(self):
        top = ttk.Frame(self, padding=8)
        top.pack(fill="x")
        ttk.Label(top, text="Search:", font=("Segoe UI", 11)).pack(side="left")
        self.q = tk.StringVar()
        e = ttk.Entry(top, textvariable=self.q, font=("Segoe UI", 12))
        e.pack(side="left", fill="x", expand=True, padx=8)
        e.focus()
        self.q.trace_add("write", lambda *_: self._debounce())
        ttk.Button(top, text="Clear", command=lambda: self.q.set("")).pack(side="left")

        bar = ttk.Frame(self, padding=(8, 0, 8, 6))
        bar.pack(fill="x")
        ttk.Button(bar, text="Organize a folder...", command=self.organize_folder).pack(side="left")
        ttk.Button(bar, text="Undo last organize", command=self.undo).pack(side="left", padx=6)
        ttk.Button(bar, text="Re-scan library", command=self.rescan).pack(side="left")
        ttk.Button(bar, text="Settings", command=self.settings).pack(side="left", padx=6)
        ttk.Button(bar, text="Check for updates",
                   command=lambda: threading.Thread(target=self._check_update, args=(True,),
                                                    daemon=True).start()).pack(side="left")
        self.auto = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="Watch folders", variable=self.auto,
                        command=self._save_flags).pack(side="right")
        self.ask = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="Ask customer name first", variable=self.ask,
                        command=self._save_flags).pack(side="right", padx=12)

        # ---- INBOX (only visible while new files are waiting)
        self.inbox = ttk.Frame(self, padding=6, relief="groove", borderwidth=2)
        self.inbox_shown = False
        self.inbox_title = tk.StringVar()
        ttk.Label(self.inbox, textvariable=self.inbox_title,
                  font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(0, 4))
        itree_frame = ttk.Frame(self.inbox)
        itree_frame.pack(fill="x")
        self.itree = ttk.Treeview(itree_frame, columns=("file", "size", "customer", "note"),
                                  show="headings", height=5, selectmode="extended")
        for key, label, w in (("file", "New file (waiting in its folder)", 430),
                              ("size", "Size", 80), ("customer", "Customer", 200),
                              ("note", "Note", 200)):
            self.itree.heading(key, text=label)
            self.itree.column(key, width=w, anchor="w")
        isb = ttk.Scrollbar(itree_frame, orient="vertical", command=self.itree.yview)
        self.itree.configure(yscrollcommand=isb.set)
        self.itree.pack(side="left", fill="x", expand=True)
        isb.pack(side="right", fill="y")
        self.itree.bind("<Double-1>", lambda e: self._inbox_open())
        self.itree.bind("<Control-a>", lambda e: (self.itree.selection_set(self.itree.get_children()),
                                                  "break")[1])

        form = ttk.Frame(self.inbox)
        form.pack(fill="x", pady=(6, 0))
        ttk.Label(form, text="Customer:").pack(side="left")
        self.cust_var = tk.StringVar()
        self.cust_box = ttk.Combobox(form, textvariable=self.cust_var, width=26)
        self.cust_box.pack(side="left", padx=(4, 10))
        ttk.Label(form, text="Note (optional):").pack(side="left")
        self.note_var = tk.StringVar()
        ttk.Entry(form, textvariable=self.note_var, width=18).pack(side="left", padx=(4, 10))
        ttk.Button(form, text="Apply to selected", command=self._inbox_apply).pack(side="left")
        ttk.Button(form, text="Select all",
                   command=lambda: self.itree.selection_set(self.itree.get_children())
                   ).pack(side="left", padx=6)
        ttk.Button(form, text="Open file", command=self._inbox_open).pack(side="left")

        acts = ttk.Frame(self.inbox)
        acts.pack(fill="x", pady=(6, 0))
        ttk.Button(acts, text="Save files that have a customer name",
                   command=lambda: self._inbox_save(only_named=True)).pack(side="left")
        ttk.Button(acts, text="Save ALL without names",
                   command=lambda: self._inbox_save(only_named=False)).pack(side="left", padx=8)
        ttk.Label(acts, text="Files that are not saved stay safely in their folder.",
                  foreground="#666").pack(side="left", padx=8)

        # ---- watched + protected folders, side by side
        self.folders_row = ttk.Frame(self)
        self.folders_row.pack(fill="x", padx=8, pady=(0, 6))
        wf = ttk.LabelFrame(self.folders_row, text="Watched folders (new files here are handled)",
                            padding=6)
        wf.pack(side="left", fill="both", expand=True, padx=(0, 4))
        self.watch_list = tk.Listbox(wf, height=3, activestyle="none", exportselection=False)
        self.watch_list.pack(side="left", fill="x", expand=True)
        wb = ttk.Frame(wf)
        wb.pack(side="left", padx=(8, 0))
        ttk.Button(wb, text="+ Add folder...", command=self.add_watch).pack(fill="x")
        ttk.Button(wb, text="Remove selected", command=self.remove_watch).pack(fill="x", pady=(4, 0))

        pf = ttk.LabelFrame(self.folders_row, text="Protected folders (never touched, even in sub-folder mode)",
                            padding=6)
        pf.pack(side="left", fill="both", expand=True, padx=(4, 0))
        self.prot_list = tk.Listbox(pf, height=3, activestyle="none", exportselection=False)
        self.prot_list.pack(side="left", fill="x", expand=True)
        pb = ttk.Frame(pf)
        pb.pack(side="left", padx=(8, 0))
        ttk.Button(pb, text="+ Add folder...", command=self.add_protected).pack(fill="x")
        ttk.Button(pb, text="Remove selected", command=self.remove_protected).pack(fill="x", pady=(4, 0))
        self.refresh_folder_lists()

        self.results = ttk.Frame(self)
        self.results.pack(fill="both", expand=True, padx=8)
        self.tree = ttk.Treeview(self.results, columns=[c[0] for c in self.COLS], show="headings")
        for key, label, w in self.COLS:
            self.tree.heading(key, text=label, command=lambda k=key: self.sort_by(k))
            self.tree.column(key, width=w, anchor="w")
        sb = ttk.Scrollbar(self.results, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", lambda e: self._open_selected())
        self.tree.bind("<Return>", lambda e: self._open_selected())
        self.tree.bind("<Button-3>", self._menu)
        self.menu = tk.Menu(self, tearoff=0)
        self.menu.add_command(label="Open", command=self._open_selected)
        self.menu.add_command(label="Show in folder", command=self._folder_selected)
        self.menu.add_command(label="Copy path", command=self._copy_path)

        self.status = tk.StringVar(value="Ready")
        ttk.Label(self, textvariable=self.status, anchor="w", padding=6).pack(fill="x")

    # ---- search
    def _debounce(self):
        if self._job:
            self.after_cancel(self._job)
        self._job = self.after(120, self.do_search)

    def do_search(self):
        t0 = time.time()
        self.rows = self.index.search(self.q.get())
        self._render()
        self.status.set(f"{len(self.rows)} file(s) shown  -  {(time.time() - t0) * 1000:.0f} ms"
                        f"  -  Library: {self.cfg['library']}")

    def _render(self):
        self.tree.delete(*self.tree.get_children())
        for i, (name, cat, customer, date, size, path) in enumerate(self.rows):
            self.tree.insert("", "end", iid=str(i),
                             values=(name, customer or "", cat, date, human_size(size or 0),
                                     os.path.basename(os.path.dirname(path))))

    def sort_by(self, col):
        idx = {"name": 0, "customer": 2, "cat": 1, "date": 3, "size": 4, "folder": 5}[col]
        rev = getattr(self, "_rev", {}).get(col, False)
        self.rows.sort(key=lambda r: (r[idx] or "") if col != "size" else (r[idx] or 0), reverse=rev)
        self._rev = {col: not rev}
        self._render()

    # ---- row actions
    def _selected_path(self):
        sel = self.tree.selection()
        return self.rows[int(sel[0])][5] if sel else None

    def _open_selected(self):
        p = self._selected_path()
        if p:
            if os.path.exists(p):
                open_path(p)
            else:
                messagebox.showinfo("Missing", "This file was moved or deleted. Use 'Re-scan library'.")

    def _folder_selected(self):
        p = self._selected_path()
        if p:
            show_in_folder(p)

    def _copy_path(self):
        p = self._selected_path()
        if p:
            self.clipboard_clear()
            self.clipboard_append(p)

    def _menu(self, event):
        iid = self.tree.identify_row(event.y)
        if iid:
            self.tree.selection_set(iid)
            self.menu.tk_popup(event.x_root, event.y_root)

    # ---- INBOX
    def refresh_inbox(self):
        with self.plock:
            for p in [p for p in self.pending if not os.path.exists(p)]:
                del self.pending[p]
            items = list(self.pending.items())
        keep = set(self.itree.selection())
        self.itree.delete(*self.itree.get_children())
        for path, info in items:
            try:
                size = human_size(os.path.getsize(path))
            except OSError:
                size = ""
            self.itree.insert("", "end", iid=path,
                              values=(os.path.basename(path), size, info["customer"], info["note"]))
        still = [p for p in keep if self.itree.exists(p)]
        if still:
            self.itree.selection_set(still)
        n = len(items)
        base = "Print Shop Organizer" + (f"  v{APP_VERSION}" if APP_VERSION != "0" else "")
        if n:
            self.inbox_title.set(f"INBOX: {n} new file(s) waiting - select files, type the customer "
                                 f"name, click Apply, then Save")
            self.cust_box["values"] = self.index.recent_customers()
            if not self.inbox_shown:
                self.inbox.pack(fill="x", padx=8, pady=(0, 6), before=self.folders_row)
                self.inbox_shown = True
            self.title(f"({n} new)  {base}")
        else:
            if self.inbox_shown:
                self.inbox.pack_forget()
                self.inbox_shown = False
            self.title(base)

    def _inbox_apply(self):
        sel = self.itree.selection()
        cust, note = self.cust_var.get().strip(), self.note_var.get().strip()
        if not sel:
            messagebox.showinfo("Apply", "Click the file(s) in the Inbox first "
                                         "(Ctrl+A or 'Select all' for every file).")
            return
        if not cust and not note:
            messagebox.showinfo("Apply", "Type a customer name (or a note) first.")
            return
        with self.plock:
            for p in sel:
                if p in self.pending:
                    if cust:
                        self.pending[p]["customer"] = cust
                    if note:
                        self.pending[p]["note"] = note
        self.refresh_inbox()
        self.status.set(f"Name set for {len(sel)} file(s). Now click 'Save files that have a customer name'.")

    def _inbox_open(self):
        sel = self.itree.selection()
        if sel and os.path.exists(sel[0]):
            open_path(sel[0])

    def _inbox_save(self, only_named):
        with self.plock:
            items = [(p, i["customer"], i["note"]) for p, i in self.pending.items()
                     if (i["customer"] or not only_named)]
        if not items:
            messagebox.showinfo("Save", "No file has a customer name yet.\n\n"
                                        "Select file(s), type the customer name, click Apply.")
            return
        if not only_named and not messagebox.askyesno(
                "Save all", f"Save {len(items)} file(s) WITHOUT customer names?\n\n"
                            "They are renamed with the date and moved to the Library."):
            return
        self.status.set("Saving...")

        def job():
            n = self.org.process_many(items)
            self.after(0, self._saved, n)
        threading.Thread(target=job, daemon=True).start()

    def _saved(self, n):
        self.refresh_inbox()
        self.do_search()
        self.status.set(f"Saved {n} file(s) to the Library")

    # ---- background work
    def _scan_pending(self):
        found = []
        for folder in list(self.cfg["watch_folders"]):
            folder = Path(folder)
            try:
                if not folder.is_dir() or self.org.is_protected(folder):
                    continue
                for p in folder.iterdir():
                    if p.is_file() and not self.org.skip_name(p) and self.org.ready(p):
                        found.append(str(p))
            except OSError:
                continue
        changed = False
        with self.plock:
            for s in [s for s in self.pending if not os.path.exists(s)]:
                del self.pending[s]
                changed = True
            for s in found:
                if s not in self.pending:
                    self.pending[s] = {"customer": "", "note": ""}
                    changed = True
        if changed:
            self.after(0, self.refresh_inbox)

    def _watch_loop(self):
        while True:
            if self.cfg.get("auto", True):
                try:
                    if self.cfg.get("ask", True):
                        self._scan_pending()
                    else:
                        n = 0
                        for folder in list(self.cfg["watch_folders"]):
                            n += self.org.sweep(folder, recursive=False)
                        if n:
                            self.after(0, self._done, f"Auto-organized {n} new file(s)")
                except Exception:
                    pass
            time.sleep(4)

    def _initial_index(self):
        self.org.reindex()
        self.after(0, self.do_search)

    def _done(self, msg):
        self.do_search()
        self.status.set(msg)

    def _run_bg(self, fn, msg_fmt):
        self.status.set("Working... please wait")
        def job():
            n = fn()
            self.after(0, self._done, msg_fmt.format(n))
        threading.Thread(target=job, daemon=True).start()

    # ---- updates
    def _update_loop(self):
        time.sleep(5)
        while True:
            self._check_update(manual=False)
            time.sleep(6 * 3600)                     # check again every 6 hours

    def _check_update(self, manual):
        try:
            info = check_update()
        except Exception as e:
            if manual:
                msg = str(e)
                self.after(0, lambda: messagebox.showwarning(
                    "Update", "Could not check for updates (is the internet on?).\n\n" + msg))
            return
        if info is None:
            if manual:
                text = ("You already have the latest version." if is_frozen() and GITHUB_REPO else
                        "Auto-update only works in the installed .exe version.")
                self.after(0, lambda: messagebox.showinfo("Update", text))
            return
        if info["tag"] in self._asked and not manual:
            return                                   # already asked this session
        self._asked.add(info["tag"])
        self.after(0, lambda: self._offer(info))

    def _offer(self, info):
        notes = info["notes"].strip()[:400]
        msg = f"A new version (v{info['tag']}) is available.\nYou have v{APP_VERSION}."
        if notes:
            msg += f"\n\nWhat's new:\n{notes}"
        msg += "\n\nUpdate now? The program will close and open again by itself."
        if messagebox.askyesno("Update available", msg):
            self.status.set("Downloading update...")
            threading.Thread(target=self._do_update, args=(info,), daemon=True).start()

    def _do_update(self, info):
        try:
            cur = Path(sys.executable)
            new = cur.with_name(cur.stem + "_new.exe")
            with _get(info["url"], timeout=60) as r, open(new, "wb") as f:
                total = int(r.headers.get("Content-Length") or info["size"] or 0)
                done = 0
                while True:
                    chunk = r.read(256 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    if total:
                        self.after(0, self.status.set, f"Downloading update... {done * 100 // total}%")
            if info["size"] and new.stat().st_size != info["size"]:
                raise IOError("The downloaded file is incomplete. Please try again.")
            bat = Path(tempfile.gettempdir()) / "printshop_update.bat"
            bat.write_text(
                "@echo off\n"
                "for /l %%i in (1,1,30) do (\n"
                f'  move /y "{new}" "{cur}" >nul 2>&1 && goto done\n'
                "  timeout /t 1 /nobreak >nul\n"
                ")\n"
                "exit /b 1\n"
                ":done\n"
                f'start "" "{cur}"\n'
                '(goto) 2>nul & del "%~f0"\n')
            subprocess.Popen(["cmd", "/c", str(bat)], creationflags=0x08000000)  # no black window
            self.after(0, self._quit_now)
        except Exception as e:
            msg = str(e)
            self.after(0, lambda: messagebox.showerror("Update failed", msg))

    def _quit_now(self):
        self.destroy()
        os._exit(0)

    # ---- watched + protected folders
    def refresh_folder_lists(self):
        for box, key in ((self.watch_list, "watch_folders"), (self.prot_list, "protected_folders")):
            box.delete(0, "end")
            for f in self.cfg.get(key, []):
                box.insert("end", f)

    def add_watch(self):
        folder = filedialog.askdirectory(title="Choose a folder to watch")
        if not folder:
            return
        folder = os.path.normpath(folder)
        if is_under(folder, self.cfg["library"]):
            messagebox.showwarning("Not allowed", "The library folder cannot also be a watched folder.")
            return
        if self.org.is_protected(folder):
            messagebox.showwarning("Protected", "This folder is in the Protected list, "
                                                "so it cannot be watched. Remove it from Protected first.")
            return
        if folder not in self.cfg["watch_folders"]:
            self.cfg["watch_folders"].append(folder)
            save_config(self.cfg)
            self.refresh_folder_lists()
            self.status.set(f"Now watching: {folder}")

    def remove_watch(self):
        sel = self.watch_list.curselection()
        if not sel:
            messagebox.showinfo("Remove", "Click a folder in the Watched list first.")
            return
        del self.cfg["watch_folders"][sel[0]]
        save_config(self.cfg)
        self.refresh_folder_lists()

    def add_protected(self):
        folder = filedialog.askdirectory(title="Choose a folder the program must NEVER touch")
        if not folder:
            return
        folder = os.path.normpath(folder)
        prot = self.cfg.setdefault("protected_folders", [])
        if folder not in prot:
            prot.append(folder)
            save_config(self.cfg)
            self.refresh_folder_lists()
            msg = f"Protected: {folder}"
            if any(is_under(folder, w) or is_under(w, folder) for w in self.cfg["watch_folders"]):
                msg += "   (it is inside/around a watched folder - its files will be left alone)"
            self.status.set(msg)

    def remove_protected(self):
        sel = self.prot_list.curselection()
        if not sel:
            messagebox.showinfo("Remove", "Click a folder in the Protected list first.")
            return
        if messagebox.askyesno("Remove protection",
                               "Remove protection from this folder?\n\n"
                               "The program may organize its files again."):
            del self.cfg["protected_folders"][sel[0]]
            save_config(self.cfg)
            self.refresh_folder_lists()

    # ---- buttons
    def organize_folder(self):
        folder = filedialog.askdirectory(title="Choose the messy folder to organize")
        if not folder:
            return
        if self.org.is_protected(folder):
            messagebox.showwarning("Protected", "This folder is protected (or is the Library), "
                                                "so nothing was done.")
            return
        sub = messagebox.askyesnocancel(
            "Include sub-folders?",
            "Also organize files inside sub-folders?\n\n"
            "Yes = every file inside is moved to the library (empty folders are removed).\n"
            "No = only files directly in this folder.\n\n"
            "Protected folders are never touched.\n"
            "You can undo with 'Undo last organize'.")
        if sub is None:
            return
        n, skipped = self.org.count_candidates(folder, sub)
        if n == 0:
            messagebox.showinfo("Organize", "No files to organize here.")
            return
        extra = f"\n{skipped} protected folder(s) will be left untouched." if skipped else ""
        if not messagebox.askyesno("Please confirm",
                                   f"{n} file(s) will be moved to the Library.{extra}\n\n"
                                   "Continue? (You can undo.)"):
            return
        self._run_bg(lambda: self.org.sweep(folder, recursive=sub), "Organized {} file(s)")

    def undo(self):
        if messagebox.askyesno("Undo", "Move the last organized batch back to where it was?"):
            self._run_bg(self.org.undo_last, "Restored {} file(s)")

    def rescan(self):
        self._run_bg(self.org.reindex, "Library scanned - {} new file(s) added")

    def _save_flags(self):
        self.cfg["auto"] = bool(self.auto.get())
        self.cfg["ask"] = bool(self.ask.get())
        save_config(self.cfg)
        if not self.cfg["ask"] or not self.cfg["auto"]:
            self.status.set("New files will not wait in the Inbox." if not self.cfg["ask"]
                            else "Watching is OFF.")

    def settings(self):
        w = tk.Toplevel(self)
        w.title("Settings")
        w.geometry("640x130")
        w.transient(self)
        vars_ = {}
        for r, (key, label) in enumerate([("library", "Library folder (sorted files):")]):
            ttk.Label(w, text=label).grid(row=r, column=0, sticky="w", padx=10, pady=12)
            v = tk.StringVar(value=self.cfg[key])
            vars_[key] = v
            ttk.Entry(w, textvariable=v, width=50).grid(row=r, column=1, padx=4)
            ttk.Button(w, text="Browse",
                       command=lambda v=v: v.set(filedialog.askdirectory() or v.get())
                       ).grid(row=r, column=2, padx=4)

        def save():
            for k, v in vars_.items():
                self.cfg[k] = v.get()
            save_config(self.cfg)
            self.org.cfg = self.cfg
            w.destroy()
            self.rescan()
        ttk.Button(w, text="Save", command=save).grid(row=1, column=1, pady=8)


if __name__ == "__main__":
    App().mainloop()
