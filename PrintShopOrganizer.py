"""
Print Shop Organizer
--------------------
* Watches your Downloads folder and files everything automatically:
      Library / <Type> / <Year-Month> / <meaningful name>
* Renames files:  2026-11-24_Banner_Rahman-Traders_3p.pdf
* Cleans old messy folders with one click (with Undo)
* Very fast search (SQLite full-text index) - searches names AND PDF text

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
    from PIL import Image          # optional: image size in the name
except Exception:
    Image = None
try:
    from pypdf import PdfReader    # optional: PDF page count + text search
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

# Job type  ->  words that identify it in a file name
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
        "library": str(Path.home() / "Documents" / "PrintShop Library"),
        "auto": True,
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
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS moves(id INTEGER PRIMARY KEY, batch TEXT, src TEXT, dst TEXT)")
        try:
            self.db.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(name, orig, cat, tag, content)")
            self.fts = True
        except sqlite3.OperationalError:
            self.fts = False
        self.db.commit()

    def add(self, path, orig, cat, tag, date, size, content=""):
        name = os.path.basename(path)
        with self.lock:
            row = self.db.execute("SELECT id FROM files WHERE path=?", (path,)).fetchone()
            if row:
                fid = row[0]
                self.db.execute(
                    "UPDATE files SET name=?,orig=?,cat=?,tag=?,date=?,size=?,content=? WHERE id=?",
                    (name, orig, cat, tag, date, size, content, fid))
                if self.fts:
                    self.db.execute("DELETE FROM fts WHERE rowid=?", (fid,))
            else:
                fid = self.db.execute(
                    "INSERT INTO files(path,name,orig,cat,tag,date,size,content) VALUES(?,?,?,?,?,?,?,?)",
                    (path, name, orig, cat, tag, date, size, content)).lastrowid
            if self.fts:
                self.db.execute(
                    "INSERT INTO fts(rowid,name,orig,cat,tag,content) VALUES(?,?,?,?,?,?)",
                    (fid, norm(name), norm(orig), cat, tag, content))
            self.db.commit()

    def remove(self, path):
        with self.lock:
            row = self.db.execute("SELECT id FROM files WHERE path=?", (path,)).fetchone()
            if row:
                if self.fts:
                    self.db.execute("DELETE FROM fts WHERE rowid=?", (row[0],))
                self.db.execute("DELETE FROM files WHERE id=?", (row[0],))
                self.db.commit()

    def known(self, path):
        with self.lock:
            return self.db.execute("SELECT 1 FROM files WHERE path=?", (path,)).fetchone() is not None

    def all_paths(self):
        with self.lock:
            return [r[0] for r in self.db.execute("SELECT path FROM files")]

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
        cols = "f.name,f.cat,f.tag,f.date,f.size,f.path"
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
                        f"SELECT {cols} FROM fts JOIN files f ON f.id=fts.rowid "
                        f"WHERE fts MATCH ? ORDER BY f.date DESC LIMIT ?", (match, limit)).fetchall()
                    if rows:
                        return rows
                except sqlite3.OperationalError:
                    pass
            where = " AND ".join("(f.name LIKE ? OR f.orig LIKE ?)" for _ in terms)
            params = []
            for t in terms:
                params += [f"%{t}%", f"%{t}%"]
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

    def _ready(self, p):
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

    def process(self, src, batch):
        name = src.name
        ext = src.suffix.lower()
        if (ext in TEMP_EXT or name.startswith(("~$", ".")) or
                name.lower() in ("desktop.ini", "thumbs.db") or not self._ready(src)):
            return False
        st = src.stat()
        d = datetime.fromtimestamp(st.st_mtime)
        date = d.strftime("%Y-%m-%d")
        cat = get_category(ext)
        extra, text = extract_info(src, cat)

        if DONE_RE.match(src.stem):
            new_stem, tag = src.stem, detect_job(src.stem)
        else:
            clean = clean_stem(src.stem)
            if GENERIC_RE.match(clean) and text.strip():
                clean = " ".join(re.findall(r"\w+", text)[:6])      # name from PDF text
            tag = detect_job(clean) or detect_job(text[:300])
            parts = [date]
            if tag and tag.lower().replace("-", "") not in clean.lower().replace("-", "").replace(" ", ""):
                parts.append(tag)
            parts.append(clean.replace(" ", "-") or "file")
            if extra:
                parts.append(extra)
            new_stem = "_".join(parts)

        dest_dir = self.library / cat / d.strftime("%Y-%m")
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = self._unique(dest_dir / (new_stem + ext))
        try:
            shutil.move(str(src), str(dest))
        except Exception:
            return False
        self.index.log_move(batch, str(src), str(dest))
        self.index.add(str(dest), src.name, cat, tag, date, st.st_size, text)
        return True

    def sweep(self, folder, recursive=False):
        """Organize every file in `folder`. Returns number of files moved."""
        folder = Path(folder)
        if not folder.exists() or not self.lock.acquire(blocking=False):
            return 0
        count = 0
        try:
            batch = datetime.now().strftime("%Y%m%d%H%M%S%f")
            lib = self.library.resolve()
            if recursive:
                for root, dirs, files in os.walk(folder):
                    rp = Path(root).resolve()
                    if rp == lib or lib in rp.parents:
                        dirs[:] = []
                        continue
                    dirs[:] = [x for x in dirs if not x.startswith(".")]
                    for f in files:
                        count += self.process(Path(root) / f, batch)
                for root, dirs, files in os.walk(folder, topdown=False):
                    if Path(root) != folder and Path(root).resolve() != lib:
                        try:
                            os.rmdir(root)              # remove folders left empty
                        except OSError:
                            pass
            else:
                for p in folder.iterdir():
                    if p.is_file():
                        count += self.process(p, batch)
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
    COLS = [("name", "Name", 430), ("cat", "Type", 110), ("date", "Date", 90),
            ("size", "Size", 80), ("folder", "Folder", 230)]

    def __init__(self):
        super().__init__()
        self.title("Print Shop Organizer" + (f"  v{APP_VERSION}" if APP_VERSION != "0" else ""))
        self.geometry("1050x620")
        self.cfg = load_config()
        self.index = Index()
        self.org = Organizer(self.cfg, self.index)
        self.rows = []
        self._job = None
        self._asked = set()
        self._build()
        self.auto.set(bool(self.cfg.get("auto", True)))
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
        ttk.Checkbutton(bar, text="Auto-organize Downloads", variable=self.auto,
                        command=self._save_auto).pack(side="right")

        wf = ttk.LabelFrame(self, text="Watched folders (new files here are organized automatically)",
                            padding=6)
        wf.pack(fill="x", padx=8, pady=(0, 6))
        self.watch_list = tk.Listbox(wf, height=3, activestyle="none")
        self.watch_list.pack(side="left", fill="x", expand=True)
        wb = ttk.Frame(wf)
        wb.pack(side="left", padx=(8, 0))
        ttk.Button(wb, text="+ Add folder...", command=self.add_watch).pack(fill="x")
        ttk.Button(wb, text="Remove selected", command=self.remove_watch).pack(fill="x", pady=(4, 0))
        self.refresh_watch_list()

        frame = ttk.Frame(self)
        frame.pack(fill="both", expand=True, padx=8)
        self.tree = ttk.Treeview(frame, columns=[c[0] for c in self.COLS], show="headings")
        for key, label, w in self.COLS:
            self.tree.heading(key, text=label, command=lambda k=key: self.sort_by(k))
            self.tree.column(key, width=w, anchor="w")
        sb = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
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
        for i, (name, cat, tag, date, size, path) in enumerate(self.rows):
            self.tree.insert("", "end", iid=str(i),
                             values=(name, cat, date, human_size(size or 0),
                                     os.path.basename(os.path.dirname(path))))

    def sort_by(self, col):
        idx = {"name": 0, "cat": 1, "date": 3, "size": 4, "folder": 5}[col]
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

    # ---- background work
    def _watch_loop(self):
        while True:
            if self.auto.get():
                try:
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

    # ---- watched folders
    def refresh_watch_list(self):
        self.watch_list.delete(0, "end")
        for f in self.cfg["watch_folders"]:
            self.watch_list.insert("end", f)

    def add_watch(self):
        folder = filedialog.askdirectory(title="Choose a folder to watch")
        if not folder:
            return
        folder = os.path.normpath(folder)
        lib = os.path.normpath(self.cfg["library"])
        if folder.lower() == lib.lower():
            messagebox.showwarning("Not allowed", "The library folder cannot also be a watched folder.")
            return
        if folder not in self.cfg["watch_folders"]:
            self.cfg["watch_folders"].append(folder)
            save_config(self.cfg)
            self.refresh_watch_list()
            self.status.set(f"Now watching: {folder}")

    def remove_watch(self):
        sel = self.watch_list.curselection()
        if not sel:
            messagebox.showinfo("Remove", "Click a folder in the list first.")
            return
        del self.cfg["watch_folders"][sel[0]]
        save_config(self.cfg)
        self.refresh_watch_list()

    # ---- buttons
    def organize_folder(self):
        folder = filedialog.askdirectory(title="Choose the messy folder to organize")
        if not folder:
            return
        sub = messagebox.askyesnocancel(
            "Include sub-folders?",
            "Also organize files inside sub-folders?\n\n"
            "Yes = every file inside is moved to the library (empty folders are removed).\n"
            "No = only files directly in this folder.\n\n"
            "You can undo with 'Undo last organize'.")
        if sub is None:
            return
        self._run_bg(lambda: self.org.sweep(folder, recursive=sub), "Organized {} file(s)")

    def undo(self):
        if messagebox.askyesno("Undo", "Move the last organized batch back to where it was?"):
            self._run_bg(self.org.undo_last, "Restored {} file(s)")

    def rescan(self):
        self._run_bg(self.org.reindex, "Library scanned - {} new file(s) added")

    def _save_auto(self):
        self.cfg["auto"] = bool(self.auto.get())
        save_config(self.cfg)

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
