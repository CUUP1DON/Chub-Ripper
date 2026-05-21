#!/usr/bin/env python3
"""
Chub Ripper — downloads your Chub.ai cards, lorebooks, and chats.

Setup:
    pip install Pillow customtkinter requests playwright
    playwright install chromium
"""

import base64
import ctypes
import ctypes.wintypes
import io
import json
import queue
import re
import struct
import sys
import threading
import time
import zlib
from pathlib import Path

# ─── secure token storage (Windows DPAPI) ─────────────────────────────────────

class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", ctypes.wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_char))]

def _token_save(tok: str) -> None:
    p = Path(__file__).parent
    try:
        buf   = ctypes.create_string_buffer(tok.encode("utf-8"))
        b_in  = _DATA_BLOB(len(tok.encode("utf-8")), buf)
        b_out = _DATA_BLOB()
        if not ctypes.windll.Crypt32.CryptProtectData(
                ctypes.byref(b_in), None, None, None, None, 0,
                ctypes.byref(b_out)):
            raise OSError("CryptProtectData failed")
        enc = ctypes.string_at(b_out.pbData, b_out.cbData)
        ctypes.windll.Kernel32.LocalFree(b_out.pbData)
        (p / ".chub_token").write_bytes(base64.b64encode(enc))
    except Exception:
        # Fallback: plain text (DPAPI unavailable — non-Windows VM, etc.)
        (p / ".chub_token").write_text(tok, encoding="utf-8")

def _token_load() -> str:
    path = Path(__file__).parent / ".chub_token"
    if not path.exists():
        return ""
    try:
        raw   = base64.b64decode(path.read_bytes())
        buf   = ctypes.create_string_buffer(raw)
        b_in  = _DATA_BLOB(len(raw), buf)
        b_out = _DATA_BLOB()
        if not ctypes.windll.Crypt32.CryptUnprotectData(
                ctypes.byref(b_in), None, None, None, None, 0,
                ctypes.byref(b_out)):
            raise OSError("CryptUnprotectData failed")
        dec = ctypes.string_at(b_out.pbData, b_out.cbData)
        ctypes.windll.Kernel32.LocalFree(b_out.pbData)
        return dec.decode("utf-8").strip()
    except Exception:
        # Fallback: may be a plain-text file from an older version
        try:
            return path.read_text(encoding="utf-8").strip()
        except Exception:
            return ""

# ─── dependency check ─────────────────────────────────────────────────────────

def _check_deps():
    missing = []
    for pkg, pip_name in [("PIL", "Pillow"), ("customtkinter", "customtkinter"),
                           ("requests", "requests"), ("playwright", "playwright")]:
        try:
            __import__(pkg)
        except ImportError:
            missing.append(pip_name)
    if missing:
        import tkinter as _tk, tkinter.messagebox as _mb
        _r = _tk.Tk(); _r.withdraw()
        _mb.showerror("Missing packages",
            f"pip install {' '.join(missing)}"
            + ("\n  playwright install chromium" if "playwright" in missing else ""))
        sys.exit(1)

_check_deps()

import customtkinter as ctk
from PIL import Image, ImageDraw
from playwright.sync_api import sync_playwright

# ─── paths ────────────────────────────────────────────────────────────────────

ROOT         = Path(__file__).parent
CARDS_DIR    = ROOT / "Cards"
LORE_DIR     = ROOT / "Lorebooks"
CHATS_DIR    = ROOT / "Chats"
PRESETS_DIR  = ROOT / "Presets"
PERSONAS_DIR = ROOT / "Personas"

REPO_API    = "https://ro.chub.ai"
GATEWAY_API = "https://gateway.chub.ai"
CDN_BASE    = "https://avatars.charhub.io/avatars"
DELAY       = 0.3

# ─── theme ────────────────────────────────────────────────────────────────────

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

BG      = "#0d1117"
SURFACE = "#161b22"
CARD    = "#1c2128"
BORDER  = "#30363d"
TEXT    = "#e6edf3"
MUTED   = "#7d8590"
DIM     = "#484f58"
ACCENT  = "#2f81f7"
A_HOVER = "#1a6fd4"
GREEN   = "#3fb950"
RED     = "#f85149"

STATUS_FG = {
    "queued":      DIM,
    "downloading": ACCENT,
    "done":        GREEN,
    "skipped":     DIM,
    "failed":      RED,
}
STATUS_LABEL = {
    "queued":      "Queued",
    "downloading": "Downloading…",
    "done":        "✓  Done",
    "skipped":     "Already saved",
    "failed":      "✗  Failed",
}

TW, TH = 128, 168
COLS   = 6

# ─── placeholder image (singleton — shared across all tiles) ──────────────────

def _make_placeholder() -> Image.Image:
    img  = Image.new("RGB", (TW, TH), CARD)
    draw = ImageDraw.Draw(img)
    draw.rectangle([2, 2, TW - 3, TH - 3], outline=BORDER, width=1)
    cx, cy = TW // 2, TH // 2
    r = 20
    draw.ellipse([cx-r, cy-r-12, cx+r, cy+r-12], outline=DIM, width=2)
    draw.rectangle([cx-r, cy+r-6, cx+r, cy+r+28], outline=DIM, width=2)
    return img

_PH_PIL: Image.Image | None = None
_PH_CTK: ctk.CTkImage | None = None

def _placeholder() -> ctk.CTkImage:
    global _PH_PIL, _PH_CTK
    if _PH_CTK is None:
        _PH_PIL = _make_placeholder()
        _PH_CTK = ctk.CTkImage(light_image=_PH_PIL, dark_image=_PH_PIL, size=(TW, TH))
    return _PH_CTK

def _safe(s: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', "_", str(s)).strip() or "unknown"

def _lc_author(path: str) -> str:
    """Return path with the first segment (author) lowercased.
    Chub's API is case-sensitive; search returns 'Anonymous' but the API expects 'anonymous'.
    """
    parts = path.split("/", 1)
    if len(parts) == 2:
        return parts[0].lower() + "/" + parts[1]
    return path.lower()

def _extract_messages(body) -> list:
    """Pull a flat, chronologically-sorted list of message dicts from the chat API response.

    Chub returns chatMessages as a dict keyed by node ID:
    {"966625321": {"id": ..., "role": ..., "content": ..., "parent_id": ...}, ...}
    """
    if isinstance(body, list):
        return body
    if not isinstance(body, dict):
        return []
    for key in ("chatMessages", "messages", "results", "items"):
        raw = body.get(key)
        if isinstance(raw, list) and raw:
            return raw
        if isinstance(raw, dict) and raw:
            msgs = [v for v in raw.values() if isinstance(v, dict)]
            # Sort by numeric id — IDs are assigned sequentially so this is chronological
            msgs.sort(key=lambda m: int(m.get("id", 0)))
            return msgs
    return []

def _chat_name(s: dict) -> str:
    # User-set session name takes priority
    for f in ("character_name", "title"):
        if s.get(f): return s[f]
    # "name" is set by the user and overrides the character name — skip if null
    if s.get("name"): return s["name"]
    # Look in nested character objects (various key names the API uses)
    for nk in ("characters", "node", "character", "char"):
        n = s.get(nk)
        if isinstance(n, dict):
            for f in ("name", "full_name", "displayName", "title", "project_name"):
                if n.get(f): return n[f]
    # Derive from full_path slug
    pip = s.get("primary_image_path")
    if not pip:
        chars = s.get("characters")
        if isinstance(chars, dict):
            pip = chars.get("full_path") or chars.get("fullPath")
    if isinstance(pip, str) and pip:
        slug = pip.rstrip("/").split("/")[-1]
        if slug:
            return slug.replace("-", " ").replace("_", " ").title()
    return str(s.get("id", "unknown"))

def _char_folder_key(s: dict) -> tuple[str, str]:
    """Return (char_name, folder_key) for a chat session.
    folder_key is safe for filesystem use and unique per Chub character ID."""
    char_id   = str(s.get("character_id", "") or "")
    chars_obj = s.get("characters")
    char_name = ""
    if isinstance(chars_obj, dict):
        char_name = chars_obj.get("name") or ""
        if not char_name:
            fp = chars_obj.get("full_path") or chars_obj.get("fullPath") or ""
            if fp:
                char_name = fp.rstrip("/").split("/")[-1].replace("-", " ").replace("_", " ").title()
    if not char_name:
        char_name = char_id or "unknown"
    folder_key = _safe(f"{char_name}_{char_id}" if char_id else char_name)
    return char_name, folder_key

def _chat_avatar_url(s: dict) -> str | None:
    for f in ("primary_image_path", "primary_image_url",
              "avatar_url", "avatar", "image_url"):
        v = s.get(f)
        if isinstance(v, str) and v.startswith("http"):
            return v
    for nk in ("characters", "node", "character", "char"):
        n = s.get(nk)
        if isinstance(n, dict):
            # Direct avatar URL
            av = n.get("avatar_url") or n.get("avatar")
            if isinstance(av, str) and av.startswith("http"):
                return av
            # Build URL from full_path
            fp = n.get("fullPath") or n.get("full_path")
            if fp:
                return f"{CDN_BASE}/{fp}/chara_card_v2.png"
    return None

# ─── Tile ─────────────────────────────────────────────────────────────────────

class Tile(ctk.CTkFrame):
    def __init__(self, parent, name: str):
        super().__init__(parent, fg_color=CARD, border_color=BORDER,
                         border_width=1, corner_radius=10)

        self._img     = _placeholder()
        self._img_lbl = ctk.CTkLabel(self, image=self._img, text="")
        self._img_lbl.pack(padx=6, pady=(8, 4))

        short = (name[:15] + "…") if len(name) > 16 else name
        ctk.CTkLabel(self, text=short,
                     font=ctk.CTkFont("Segoe UI", 11), text_color=TEXT,
                     wraplength=TW).pack(padx=4, pady=(0, 2))

        self._badge = ctk.CTkLabel(self, text="Queued",
                                    font=ctk.CTkFont("Segoe UI", 9),
                                    text_color=DIM, fg_color=SURFACE,
                                    corner_radius=5, padx=8, pady=2)
        self._badge.pack(pady=(0, 4))

        self._selected = ctk.BooleanVar(value=True)
        self._chk = ctk.CTkCheckBox(self, text="", variable=self._selected,
                                    checkbox_height=14, checkbox_width=14,
                                    fg_color=ACCENT, hover_color=A_HOVER,
                                    border_color=BORDER, corner_radius=3)
        self._chk.pack(pady=(0, 6))

        self.bind("<Enter>", lambda _: self.configure(border_color=ACCENT))
        self.bind("<Leave>", lambda _: self.configure(border_color=BORDER))

    def set_status(self, key: str):
        self._badge.configure(text=STATUS_LABEL.get(key, key),
                              text_color=STATUS_FG.get(key, MUTED))

    def set_image(self, raw: bytes):
        try:
            pil = Image.open(io.BytesIO(raw))
            pil.thumbnail((TW, TH), Image.LANCZOS)
            bg = Image.new("RGB", (TW, TH), CARD)
            bg.paste(pil, ((TW - pil.width) // 2, (TH - pil.height) // 2))
            img = ctk.CTkImage(light_image=bg, dark_image=bg, size=(TW, TH))
            self._img = img
            # Defer the label update to idle so it never fires mid-scroll
            self._img_lbl.after_idle(lambda i=img: self._img_lbl.configure(image=i))
        except Exception:
            pass

# ─── TileGrid ─────────────────────────────────────────────────────────────────

PAGE = 100  # tiles rendered at once before "Load more" button appears

class TileGrid:
    def __init__(self, parent: ctk.CTkScrollableFrame, kind: str):
        self.kind    = kind
        self._p      = parent
        self._tiles: dict[str, Tile] = {}
        self._names: dict[str, str]  = {}
        self._order: list[str]       = []
        self._visible = 0  # how many are currently gridded
        self._col  = 0
        self._row  = 0
        self._cols = COLS  # current column count — updated on resize
        self._load_btn: ctk.CTkButton | None = None
        # Tracks the desired state for tiles that haven't been rendered yet.
        # True  → unrendered tiles count as checked (default: download everything)
        # False → unrendered tiles are excluded (set by ✗ None)
        self._bulk_state: bool = True
        parent.bind("<Configure>", self._on_resize, add=True)
        try:
            c = parent._canvas
            c.configure(bg=BG)
            # Force a repaint after every scroll input so Windows GDI
            # doesn't leave ghost pixels where the old content was.
            def _repaint():
                try: c.update_idletasks()
                except Exception: pass
            c.bind("<MouseWheel>",
                   lambda _: parent.after(1, _repaint), add=True)
            parent._scrollbar.bind("<B1-Motion>",
                   lambda _: parent.after(1, _repaint), add=True)
            parent._scrollbar.bind("<ButtonRelease-1>",
                   lambda _: parent.after(1, _repaint), add=True)
        except Exception:
            pass

    def _on_resize(self, event):
        # Subtract scrollbar's actual rendered width so tiles never overflow
        try:
            sb = self._p._scrollbar.winfo_width()
            sb = sb if sb > 1 else 16
        except Exception:
            sb = 16
        # Slot = TW + label padx*2 (12) + grid padx*2 (12) = TW + 24
        old_cols  = self._cols
        new_cols  = max(1, (event.width - sb) // (TW + 24))
        if new_cols == old_cols:
            return
        self._cols = new_cols
        # Cancel any pending debounced reflow
        if getattr(self, "_reflow_job", None):
            try: self._p.after_cancel(self._reflow_job)
            except Exception: pass
            self._reflow_job = None
        if new_cols < old_cols:
            # Window is shrinking — reflow immediately so content never overflows
            # the canvas (overflow breaks CTkScrollableFrame's scroll state)
            self._reflow()
        else:
            # Window is growing — debounce to avoid reflowing on every pixel
            self._reflow_job = self._p.after(150, self._reflow)

    def _reflow(self):
        self._col = 0
        self._row = 0
        for key in self._order[:self._visible]:
            t = self._tiles[key]
            t.grid(row=self._row, column=self._col, padx=6, pady=6, sticky="nw")
            self._col += 1
            if self._col >= self._cols:
                self._col = 0
                self._row += 1
        self._sync_load_btn()

    def _place(self, t: Tile):
        t.grid(row=self._row, column=self._col, padx=6, pady=6, sticky="nw")
        self._col += 1
        if self._col >= self._cols:
            self._col = 0
            self._row += 1

    def _sync_load_btn(self):
        remaining = len(self._order) - self._visible
        btn_row = self._row + (1 if self._col > 0 else 0)
        if remaining <= 0:
            if self._load_btn:
                self._load_btn.grid_remove()
            return
        if self._load_btn is None:
            self._load_btn = ctk.CTkButton(
                self._p, width=220, height=34,
                font=ctk.CTkFont("Segoe UI", 10),
                fg_color=SURFACE, hover_color=CARD, text_color=MUTED,
                border_color=BORDER, border_width=1, corner_radius=8,
                command=self._load_more,
            )
        assert self._load_btn is not None
        n = min(PAGE, remaining)
        self._load_btn.configure(text=f"Load {n} more  ({remaining} remaining)")
        self._load_btn.grid(row=btn_row, column=0, columnspan=self._cols, pady=12)

    def _load_more(self):
        start = self._visible
        end   = min(start + PAGE, len(self._order))
        for i in range(start, end):
            self._place(self._tiles[self._order[i]])
        self._visible = end
        self._sync_load_btn()

    def add(self, key: str, name: str):
        if key in self._tiles:
            return
        t = Tile(self._p, name)
        t._selected.set(self._bulk_state)  # inherit current bulk selection state
        self._tiles[key] = t
        self._names[key] = name.lower()
        self._order.append(key)
        if self._visible < PAGE:
            self._place(t)
            self._visible += 1
        else:
            self._sync_load_btn()

    def set_status(self, key: str, s: str):
        if t := self._tiles.get(key):
            t.set_status(s)

    def set_image(self, key: str, raw: bytes):
        if t := self._tiles.get(key):
            t.set_image(raw)

    def count(self) -> int:
        return len(self._tiles)

    def select_all(self, val: bool = True):
        self._bulk_state = val
        for t in self._tiles.values():
            t._selected.set(val)

    def get_checked(self) -> set[str]:
        rendered  = set(self._order[:self._visible])
        checked   = {k for k in rendered if self._tiles[k]._selected.get()}
        if self._bulk_state:
            # unrendered tiles follow bulk state → include them all
            unrendered = set(self._order[self._visible:])
            checked |= unrendered
        # if bulk_state is False, unrendered tiles are excluded entirely
        return checked

    def filter(self, query: str):
        q = query.strip().lower()
        # Render any unrendered tiles so we can show/hide them
        if q and self._visible < len(self._order):
            for i in range(self._visible, len(self._order)):
                self._place(self._tiles[self._order[i]])
            self._visible = len(self._order)
        if not q:
            self._reflow()
            self._sync_load_btn()
            return
        self._col = 0
        self._row = 0
        for key in self._order:
            t = self._tiles[key]
            if q in self._names.get(key, ""):
                t.grid(row=self._row, column=self._col, padx=6, pady=6, sticky="nw")
                self._col += 1
                if self._col >= self._cols:
                    self._col = 0
                    self._row += 1
            else:
                t.grid_remove()
        if self._load_btn:
            self._load_btn.grid_remove()

    def clear(self):
        """Destroy all tiles and reset grid state for a fresh fetch."""
        for t in self._tiles.values():
            t.destroy()
        self._tiles.clear()
        self._names.clear()
        self._order.clear()
        self._visible  = 0
        self._col      = 0
        self._row      = 0
        self._bulk_state = True
        if self._load_btn:
            self._load_btn.grid_remove()
            self._load_btn = None

# ─── Main application ─────────────────────────────────────────────────────────

class App(ctk.CTk):
    def __init__(self):
        super().__init__(fg_color=BG)
        self.title("Chub Ripper")
        self.geometry("980x740")
        self.minsize(720, 520)

        self._q              = queue.Queue()
        self._download_ready       = threading.Event()
        self._cancel_event         = threading.Event()
        self._cancel_download_event = threading.Event()
        self._token_var      = ctk.StringVar()
        self._do_cards       = ctk.BooleanVar(value=True)
        self._do_lorebooks   = ctk.BooleanVar(value=True)
        self._do_presets     = ctk.BooleanVar(value=True)
        self._do_personas    = ctk.BooleanVar(value=True)
        self._do_chats       = ctk.BooleanVar(value=True)
        self._card_fmt_var   = ctk.StringVar(value="PNG")
        self._chat_fmt_var   = ctk.StringVar(value="JSONL")
        self._total          = 0
        self._done           = 0
        self._worker_thread: threading.Thread | None = None
        self._fetch_sess     = None
        self._fetched_cards:    list[dict] = []
        self._fetched_lore:     list[dict] = []
        self._fetched_presets:  list[dict] = []
        self._fetched_personas: list[dict] = []
        self._fetched_sessions: list[dict] = []

        # Pre-fill token if saved from last run
        self._token_var.set(_token_load())

        self._grids:      dict[str, TileGrid]               = {}
        self._sframes:    dict[str, ctk.CTkScrollableFrame] = {}
        self._active_tab: str                               = "cards"

        self._build()
        self.after(100, self._pump)

    # ── build ─────────────────────────────────────────────────────────────────

    def _build(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(5, weight=1)

        # header
        hdr = ctk.CTkFrame(self, fg_color="transparent")
        hdr.grid(row=0, column=0, sticky="ew", padx=20, pady=(18, 6))
        hdr.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(hdr, text="Chub Ripper",
                     font=ctk.CTkFont("Segoe UI", 22, weight="bold"),
                     text_color=TEXT).grid(row=0, column=0, sticky="w")

        right = ctk.CTkFrame(hdr, fg_color="transparent")
        right.grid(row=0, column=2, sticky="e")

        self._fetch_btn = ctk.CTkButton(
            right, text="Fetch",
            width=90, height=36,
            font=ctk.CTkFont("Segoe UI", 11, weight="bold"),
            fg_color=ACCENT, hover_color=A_HOVER,
            corner_radius=8, command=self._start,
        )
        self._fetch_btn.pack(side="left")

        # token entry row (always visible)
        tok_row = ctk.CTkFrame(self, fg_color="transparent")
        tok_row.grid(row=1, column=0, sticky="ew", padx=20, pady=(0, 4))
        tok_row.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(tok_row, text="Ch-Api-Key",
                     font=ctk.CTkFont("Segoe UI", 10), text_color=MUTED,
                     ).grid(row=0, column=0, padx=(0, 10))

        self._token_entry = ctk.CTkEntry(
                     tok_row, textvariable=self._token_var, height=32,
                     fg_color=SURFACE, border_color=BORDER, text_color=TEXT,
                     font=ctk.CTkFont("Segoe UI", 10),
                     placeholder_text="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx  (DevTools → Network → any ro.chub.ai request → Headers → Ch-Api-Key)",
                     show="*")
        self._token_entry.grid(row=0, column=1, sticky="ew", padx=(0, 10))

        ctk.CTkButton(tok_row, text="Show", width=56, height=32,
                      fg_color=SURFACE, hover_color=CARD, text_color=MUTED,
                      border_color=BORDER, border_width=1,
                      font=ctk.CTkFont("Segoe UI", 10),
                      command=self._toggle_token_visibility,
                      ).grid(row=0, column=2)

        # fetch-type checkboxes + format pickers
        chk_row = ctk.CTkFrame(self, fg_color="transparent")
        chk_row.grid(row=2, column=0, sticky="w", padx=20, pady=(0, 2))
        for var, lbl in [(self._do_cards, "Cards"),
                         (self._do_lorebooks, "Lorebooks"),
                         (self._do_presets, "Presets"),
                         (self._do_personas, "Personas"),
                         (self._do_chats, "Chats")]:
            ctk.CTkCheckBox(chk_row, text=lbl, variable=var,
                            font=ctk.CTkFont("Segoe UI", 10),
                            text_color=MUTED, checkbox_height=16, checkbox_width=16,
                            fg_color=ACCENT, hover_color=A_HOVER,
                            ).pack(side="left", padx=(0, 18))
        ctk.CTkLabel(chk_row, text="|", font=ctk.CTkFont("Segoe UI", 10),
                     text_color=DIM).pack(side="left", padx=(0, 10))
        ctk.CTkLabel(chk_row, text="Card fmt:", font=ctk.CTkFont("Segoe UI", 10),
                     text_color=MUTED).pack(side="left")
        ctk.CTkOptionMenu(chk_row, variable=self._card_fmt_var,
                          values=["PNG", "JSON"],
                          width=110, height=26, font=ctk.CTkFont("Segoe UI", 10),
                          fg_color=SURFACE, button_color=BORDER,
                          button_hover_color=CARD, text_color=TEXT,
                          dropdown_fg_color=SURFACE, dropdown_text_color=TEXT,
                          ).pack(side="left", padx=(4, 14))
        ctk.CTkLabel(chk_row, text="Chat fmt:", font=ctk.CTkFont("Segoe UI", 10),
                     text_color=MUTED).pack(side="left")
        ctk.CTkOptionMenu(chk_row, variable=self._chat_fmt_var,
                          values=["JSONL", "JSON", "TXT"],
                          width=80, height=26, font=ctk.CTkFont("Segoe UI", 10),
                          fg_color=SURFACE, button_color=BORDER,
                          button_hover_color=CARD, text_color=TEXT,
                          dropdown_fg_color=SURFACE, dropdown_text_color=TEXT,
                          ).pack(side="left", padx=(4, 0))

        # status
        self._status_var = ctk.StringVar(value="Paste your Ch-Api-Key and click Fetch.")
        ctk.CTkLabel(self, textvariable=self._status_var,
                     font=ctk.CTkFont("Segoe UI", 10),
                     text_color=MUTED, anchor="w",
                     ).grid(row=3, column=0, sticky="ew", padx=22, pady=(0, 2))

        # divider + tabs
        ctk.CTkFrame(self, fg_color=BORDER, height=1, corner_radius=0
                     ).grid(row=4, column=0, sticky="ew")

        tab_row = ctk.CTkFrame(self, fg_color=SURFACE, corner_radius=0)
        tab_row.grid(row=4, column=0, sticky="ew")

        self._tab_btns: dict[str, ctk.CTkButton] = {}
        for name in ("Cards", "Lorebooks", "Presets", "Personas", "Chats"):
            key = name.lower()
            btn = ctk.CTkButton(tab_row, text=name, width=110, height=36,
                                font=ctk.CTkFont("Segoe UI", 11),
                                fg_color="transparent", hover_color=CARD,
                                text_color=MUTED, corner_radius=0,
                                command=lambda k=key: self._show_tab(k))
            btn.pack(side="left", padx=2, pady=4)
            self._tab_btns[key] = btn

            sf = ctk.CTkScrollableFrame(self, fg_color=BG,
                                         scrollbar_button_color=SURFACE,
                                         scrollbar_button_hover_color=BORDER)
            self._sframes[key] = sf
            self._grids[key]   = TileGrid(sf, key)

        # Search bar + Select All / None buttons (right side of tab row)
        sel_fr = ctk.CTkFrame(tab_row, fg_color="transparent")
        sel_fr.pack(side="right", padx=8, pady=4)

        self._search_var = ctk.StringVar()
        self._search_var.trace_add("write", lambda *_: self._on_search())
        ctk.CTkEntry(sel_fr, textvariable=self._search_var,
                     placeholder_text="Search…", width=160, height=28,
                     fg_color=SURFACE, border_color=BORDER, text_color=TEXT,
                     font=ctk.CTkFont("Segoe UI", 10),
                     ).pack(side="left", padx=(0, 10))
        ctk.CTkButton(sel_fr, text="✓ All", width=62, height=28,
                      font=ctk.CTkFont("Segoe UI", 10),
                      fg_color=SURFACE, hover_color=CARD, text_color=MUTED,
                      border_color=BORDER, border_width=1,
                      command=lambda: self._grids[self._active_tab].select_all(True),
                      ).pack(side="left", padx=(0, 4))
        ctk.CTkButton(sel_fr, text="✗ None", width=62, height=28,
                      font=ctk.CTkFont("Segoe UI", 10),
                      fg_color=SURFACE, hover_color=CARD, text_color=MUTED,
                      border_color=BORDER, border_width=1,
                      command=lambda: self._grids[self._active_tab].select_all(False),
                      ).pack(side="left")

        self._show_tab("cards")

        # footer
        foot = ctk.CTkFrame(self, fg_color=SURFACE, corner_radius=0, height=48)
        foot.grid(row=6, column=0, sticky="ew")
        foot.grid_columnconfigure(0, weight=1)
        foot.grid_propagate(False)

        self._bar = ctk.CTkProgressBar(foot, height=5, fg_color=BORDER,
                                        progress_color=ACCENT, corner_radius=3)
        self._bar.grid(row=0, column=0, sticky="ew", padx=(16, 12), pady=16)
        self._bar.set(0)

        self._prog_lbl = ctk.CTkLabel(foot, text="", width=80, anchor="e",
                                       font=ctk.CTkFont("Segoe UI", 10),
                                       text_color=MUTED)
        self._prog_lbl.grid(row=0, column=1, padx=(0, 8))

        self._dl_btn = ctk.CTkButton(
            foot, text="⬇  Download All",
            width=148, height=34,
            font=ctk.CTkFont("Segoe UI", 11, weight="bold"),
            fg_color=GREEN, hover_color="#2ea843", text_color="#0d1117",
            corner_radius=8, command=self._start_download,
        )
        # Hidden until fetch completes

    def _show_tab(self, key: str):
        self._active_tab = key
        for k, sf in self._sframes.items():
            sf.grid_remove()
            self._tab_btns[k].configure(text_color=MUTED, fg_color="transparent")
        self._sframes[key].grid(row=5, column=0, sticky="nsew")
        self._tab_btns[key].configure(text_color=TEXT, fg_color=CARD)
        if hasattr(self, "_search_var"):
            self._search_var.set("")

    def _on_search(self):
        self._grids[self._active_tab].filter(self._search_var.get())

    def _refresh_tab(self, key: str):
        name  = key.capitalize()
        count = self._grids[key].count()
        self._tab_btns[key].configure(text=f"{name}  {count}")

    # ── actions ───────────────────────────────────────────────────────────────

    def _toggle_token_visibility(self):
        hidden = self._token_entry.cget("show") == "*"
        self._token_entry.configure(show="" if hidden else "*")

    def _reset_grids(self):
        """Clear all tile grids and reset tab labels before a fresh fetch."""
        for key, grid in self._grids.items():
            grid.clear()
            self._tab_btns[key].configure(text=key.capitalize())
        self._dl_btn.grid_remove()
        self._bar.set(0)
        self._prog_lbl.configure(text="")
        self._total = 0
        self._done  = 0

    def _start(self):
        # If currently fetching — cancel
        if self._worker_thread and self._worker_thread.is_alive():
            self._cancel_event.set()
            self._download_ready.set()  # unblock any wait() inside worker
            self._fetch_btn.configure(state="disabled", text="Cancelling…")
            return

        tok = self._token_var.get().strip()
        if tok.lower().startswith("bearer "):
            tok = tok[7:].strip()
            self._token_var.set(tok)
        if not tok:
            self._status_var.set("Paste your Bearer token first.")
            return

        self._cancel_event.clear()
        self._download_ready.clear()
        self._reset_grids()
        self._fetch_btn.configure(state="normal", text="✕ Cancel")
        self._status_var.set("Fetching…")
        # snapshot checkbox state before handing off to worker thread
        self._fetch_cards     = self._do_cards.get()
        self._fetch_lorebooks = self._do_lorebooks.get()
        self._fetch_presets   = self._do_presets.get()
        self._fetch_personas  = self._do_personas.get()
        self._fetch_chats     = self._do_chats.get()
        self._card_fmt        = self._card_fmt_var.get()
        self._chat_fmt        = self._chat_fmt_var.get()
        self._worker_thread = threading.Thread(target=self._worker, daemon=True)
        self._worker_thread.start()

    def _start_download(self):
        # If a download is already running — cancel it
        if getattr(self, "_download_thread", None) and self._download_thread.is_alive():
            self._cancel_download_event.set()
            self._dl_btn.configure(state="disabled", text="Cancelling…")
            return

        self._cancel_download_event.clear()
        self._dl_btn.configure(state="normal", text="✕ Cancel Download")
        self._status_var.set("Starting downloads…")
        # Snapshot selection in the UI thread — worker thread must NOT read tkinter vars
        self._checked_snapshot: dict[str, set[str]] = {
            tab: grid.get_checked() for tab, grid in self._grids.items()
        }
        if self._worker_thread and self._worker_thread.is_alive():
            # Worker is waiting at _download_ready.wait() — unblock it
            self._download_ready.set()
        elif self._fetch_sess is not None:
            # Worker already finished; re-run the download phase directly
            self._download_thread = threading.Thread(target=self._download_phase, daemon=True)
            self._download_thread.start()

    # ── queue pump ────────────────────────────────────────────────────────────

    def _pump(self):
        try:
            adds = images = 0
            while True:
                msg = self._q.get_nowait()
                self._handle(msg)
                kind = msg[0]
                if kind == "ADD":
                    adds += 1
                    if adds >= 20:
                        break  # yield to UI
                elif kind == "IMAGE":
                    images += 1
                    if images >= 4:
                        break  # don't flood tkinter with image updates
        except queue.Empty:
            pass
        self.after(50, self._pump)

    def _handle(self, msg: tuple):
        kind = msg[0]
        if kind == "LOG":
            self._status_var.set(msg[1])
        elif kind == "ADD":
            _, tab, key, name = msg
            self._grids[tab].add(key, name)
            self._refresh_tab(tab)
        elif kind == "STATUS":
            _, tab, key, s = msg
            self._grids[tab].set_status(key, s)
            if s in ("done", "skipped", "failed"):
                self._done += 1
                self._tick()
        elif kind == "IMAGE":
            _, tab, key, raw = msg
            self._grids[tab].set_image(key, raw)
        elif kind == "SHOW_DL_BTN":
            total, n_sessions, n_chars = msg[1], msg[2], msg[3]
            self._total = total
            self._bar.set(0)
            self._prog_lbl.configure(text=f"0 / {total}")
            self._dl_btn.grid(row=0, column=2, padx=(0, 16), pady=7)
            self._fetch_btn.configure(state="normal", text="Fetch")  # re-enable for re-fetch
            if n_sessions:
                self._tab_btns["chats"].configure(
                    text=f"Chats  {n_sessions}  ({n_chars} chars)")
        elif kind == "CANCELLED":
            self._status_var.set("Fetch cancelled.")
            self._fetch_btn.configure(state="normal", text="Fetch")
        elif kind == "DOWNLOAD_CANCELLED":
            self._status_var.set("Download cancelled.")
            self._dl_btn.configure(state="normal", text="⬇  Download All")
        elif kind == "DONE":
            n = msg[1]
            self._status_var.set(f"Finished — {n} item(s) saved.")
            self._fetch_btn.configure(state="normal", text="Fetch")
            self._dl_btn.configure(state="normal", text="⬇  Download All")
            self._bar.set(1)
        elif kind == "ERROR":
            self._status_var.set(f"Error: {msg[1]}")
            self._fetch_btn.configure(state="normal", text="Fetch")
            self._dl_btn.configure(state="normal", text="⬇  Download All")

    def _tick(self):
        if self._total:
            self._bar.set(self._done / self._total)
            self._prog_lbl.configure(text=f"{self._done} / {self._total}")

    # ── worker ────────────────────────────────────────────────────────────────

    def _worker(self):
        import traceback as _tb
        import requests as _req

        q = self._q
        _log_file = open(ROOT / "debug.log", "a", encoding="utf-8", buffering=1)
        _log_file.write(f"\n{'='*60}\n{time.strftime('%Y-%m-%d %H:%M:%S')}\n{'='*60}\n")
        _log_file.write(f"Python {sys.version}  |  platform: {sys.platform}\n")
        _log_file.write(f"fetch: cards={self._fetch_cards} lorebooks={self._fetch_lorebooks} "
                        f"presets={self._fetch_presets} personas={self._fetch_personas} "
                        f"chats={self._fetch_chats}  "
                        f"card_fmt={getattr(self,'_card_fmt','?')}  chat_fmt={getattr(self,'_chat_fmt','?')}\n")
        def log(m):   # status bar + file
            _log_file.write(m + "\n")
            q.put(("LOG", m))
        def flog(m):  # file only (debug details)
            _log_file.write(m + "\n")
        def add(t, k, n):    q.put(("ADD",    t, k, n))
        def status(t, k, s): q.put(("STATUS", t, k, s))
        def image(t, k, b):  q.put(("IMAGE",  t, k, b))

        try:
            tok = self._token_var.get().strip()

            sess = _req.Session()
            sess.headers.update({
                "Ch-Api-Key": tok,
                "Samwise":    tok,
                "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) "
                               "Chrome/148.0.0.0 Safari/537.36"),
                "Referer": "https://chub.ai/",
                "Origin":  "https://chub.ai",
            })

            # Save for next launch
            _token_save(tok)

            # Verify key + grab username for user-scoped queries
            my_handle: str = ""
            try:
                probe = sess.get(f"{REPO_API}/api/self", timeout=10)
                if probe.status_code in (401, 403):
                    q.put(("ERROR",
                           f"API key rejected ({probe.status_code}).\n"
                           "DevTools → Network → any ro.chub.ai request → Headers → Ch-Api-Key"))
                    return
                log(f"Key accepted ({probe.status_code})")
                if probe.ok:
                    pj = probe.json()
                    my_handle = (pj.get("data", {}).get("user_name") or
                                 pj.get("data", {}).get("handle") or
                                 pj.get("user_name") or
                                 pj.get("handle") or
                                 pj.get("name") or "")
                    flog(f"  /api/self keys: {list(pj.keys())[:10]}  handle={my_handle!r}")
            except Exception as e:
                log(f"Key probe error: {e} (continuing)")

            # ── Use headless browser with key pre-injected to sniff real API calls ──
            def _collect(nodes, tab, seen):
                for n in nodes:
                    fp = n.get("fullPath", "")
                    if fp and fp not in seen:
                        seen.add(fp)
                        add(tab, fp, n.get("name", fp))
                        try:
                            r = sess.get(f"https://avatars.charhub.io/avatars/{fp}/avatar.webp",
                                         timeout=10)
                            if r.ok:
                                image(tab, fp, r.content)
                        except Exception:
                            pass
                        yield n

            def _paginate_nodes(captured_url: str, first_body: dict,
                                tab: str, seen: set) -> list[dict]:
                from urllib.parse import urlparse, parse_qs, urlunparse
                def _pick_batch(b: dict) -> list:
                    d = b.get("data", {}) if isinstance(b, dict) else {}
                    return (d.get("nodes") or d.get("lorebooks") or d.get("characters") or
                            b.get("nodes") or b.get("lorebooks") or [])
                nodes: list[dict] = []
                batch = _pick_batch(first_body)
                total_count = first_body.get("data", {}).get("count", "?")
                log(f"  {tab} page 1: {len(batch)} items  (api total={total_count})")
                flog(f"  {tab} first_body keys: {list(first_body.keys())[:10]}")
                for n in _collect(batch, tab, seen):
                    nodes.append(n)
                parsed = urlparse(captured_url)
                qs = {k: v[0] for k, v in parse_qs(parsed.query).items()}
                qs.pop("page", None); qs.pop("cursor", None)
                base = urlunparse(parsed._replace(query=""))
                flog(f"  {tab} base url: {base}  fixed qs: {qs}")
                body = first_body
                pg = 2
                while True:
                    if self._cancel_event.is_set():
                        flog(f"  {tab} pagination cancelled at page {pg}"); break
                    cursor = body.get("data", {}).get("cursor")
                    flog(f"  {tab} page {pg} cursor: {str(cursor)[:40] if cursor else None}")
                    if not cursor:
                        flog(f"  {tab} pagination done — no cursor after page {pg - 1}"); break
                    r = sess.get(base, params={**qs, "page": pg, "first": 48, "cursor": cursor}, timeout=30)
                    log(f"  {tab} page {pg} → {r.status_code}")
                    flog(f"  {tab} page {pg} url: {r.url}")
                    if not r.ok:
                        flog(f"  {tab} stopping — non-OK status {r.status_code}"); break
                    body = r.json()
                    new_cursor = body.get("data", {}).get("cursor")
                    batch = _pick_batch(body)
                    flog(f"  {tab} page {pg} batch={len(batch)}  new_cursor={str(new_cursor)[:40] if new_cursor else None}")
                    if not batch:
                        flog(f"  {tab} stopping — empty batch on page {pg}"); break
                    prev_count = len(nodes)
                    for n in _collect(batch, tab, seen):
                        nodes.append(n)
                    if new_cursor == cursor:
                        flog(f"  {tab} stopping — cursor unchanged (stuck): {str(cursor)[:40]}"); break
                    if len(nodes) == prev_count:
                        flog(f"  {tab} stopping — no new unique items on page {pg}"); break
                    log(f"  {tab} page {pg}: +{len(batch)}  total so far: {len(nodes)}")
                    pg += 1
                    time.sleep(0.3)
                flog(f"  {tab} pagination finished: {len(nodes)} unique nodes across {pg - 1} page(s)")
                return nodes

            def _paginate_sessions(captured_url: str, first_body: dict) -> list[dict]:
                from urllib.parse import urlparse, parse_qs, urlunparse

                def _extract(body):
                    return (body.get("chats") or body.get("results") or
                            body.get("sessions") or body.get("items") or
                            body.get("data") or
                            (body if isinstance(body, list) else []))

                def _build_char_map(body: dict) -> dict:
                    """Extract an id→character-dict map from any inline character data."""
                    cmap: dict = {}
                    for key in ("characters", "nodes", "character_map", "character_data"):
                        val = body.get(key)
                        if isinstance(val, dict):
                            for k, v in val.items():
                                if isinstance(v, dict):
                                    cmap[str(k)] = v
                        elif isinstance(val, list):
                            for c in val:
                                if isinstance(c, dict):
                                    cid = c.get("id") or c.get("character_id")
                                    if cid:
                                        cmap[str(cid)] = c
                    return cmap

                def _enrich(s: dict, cmap: dict) -> dict:
                    """Fill in character_name / primary_image_path from inline char map."""
                    if not cmap:
                        return s
                    cid = str(s.get("character_id") or "")
                    char = cmap.get(cid)
                    if not char:
                        return s
                    s = dict(s)
                    if not s.get("character_name"):
                        for f in ("name", "full_name", "displayName", "title"):
                            if char.get(f):
                                s["character_name"] = char[f]
                                break
                    if not s.get("primary_image_path"):
                        fp = char.get("fullPath") or char.get("full_path")
                        if fp:
                            s["primary_image_path"] = fp
                    return s

                char_map = _build_char_map(first_body)
                flog(f"  first_body keys: {list(first_body.keys())[:12]}  char_map size: {len(char_map)}")

                items = _extract(first_body)
                log(f"  chats page 1: {len(items)} items")
                sessions = [_enrich(s, char_map) for s in items]

                # cursor-based pagination (ro.chub.ai/api/core/chats)
                parsed = urlparse(captured_url)
                qs = {k: v[0] for k, v in parse_qs(parsed.query).items()}
                qs.pop("cursor", None)
                # Request inline character data on subsequent pages
                qs.setdefault("include_character", "true")
                base = urlunparse(parsed._replace(query=""))
                cursor = first_body.get("cursor")
                flog(f"  chats initial cursor: {str(cursor)[:40] if cursor else None}")
                pg = 2
                while cursor:
                    r = sess.get(base, params={**qs, "cursor": cursor}, timeout=30)
                    log(f"  chats page {pg} → {r.status_code}")
                    flog(f"  chats page {pg} url: {r.url}")
                    if not r.ok:
                        flog(f"  chats stopping — non-OK status {r.status_code}"); break
                    body = r.json()
                    page_char_map = _build_char_map(body)
                    items = _extract(body)
                    new_cursor = body.get("cursor")
                    flog(f"  chats page {pg} items={len(items)}  new_cursor={str(new_cursor)[:40] if new_cursor else None}")
                    if not items:
                        flog(f"  chats stopping — empty page {pg}"); break
                    sessions.extend(_enrich(s, page_char_map) for s in items)
                    log(f"  chats page {pg}: +{len(items)}  total so far: {len(sessions)}")
                    cursor = new_cursor
                    pg += 1
                    time.sleep(0.3)
                flog(f"  chats pagination finished: {len(sessions)} sessions across {pg - 1} page(s)")
                return sessions

            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                ctx = browser.new_context(
                    extra_http_headers={"Ch-Api-Key": tok, "Samwise": tok},
                    user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                "AppleWebKit/537.36 (KHTML, like Gecko) "
                                "Chrome/148.0.0.0 Safari/537.36"),
                )
                page = ctx.new_page()

                def _sniff_page(nav_url: str, domain_hint: str,
                                label: str, wait_ms: int = 6000):
                    """Navigate headless, capture all JSON responses from domain,
                    return the one that looks most like a list."""
                    candidates: list = []

                    def on_resp(resp):
                        # Check hostname only — not the full URL — so query-param
                        # values (e.g. origin=https://chub.ai) can't smuggle in
                        # responses from unrelated domains like accounts.google.com.
                        try:
                            host = resp.url.split("//")[1].split("/")[0].split("?")[0]
                        except Exception:
                            return
                        if domain_hint not in host: return
                        if resp.status != 200: return
                        try:
                            body = resp.json()
                            if isinstance(body, (dict, list)):
                                candidates.append((resp.url, body))
                                keys = list(body.keys())[:6] if isinstance(body, dict) else f"list[{len(body)}]"
                                log(f"  → {resp.url.split('?')[0][-80:]}  keys={keys}")
                        except Exception:
                            pass

                    page.on("response", on_resp)
                    log(f"Navigating to {label}…")
                    try:
                        page.goto(nav_url, wait_until="domcontentloaded", timeout=30000)
                        page.wait_for_timeout(wait_ms)
                    except Exception as e:
                        log(f"  nav error: {e}")
                    page.remove_listener("response", on_resp)

                    if not candidates:
                        log(f"  nothing captured for {label}")
                        return "", {}

                    def _score(body):
                        if isinstance(body, list):
                            return len(body)
                        d = body.get("data", {}) if isinstance(body, dict) else {}
                        nodes = d.get("nodes") or []
                        count = d.get("count", 0)
                        for key in ("results", "sessions", "items", "chats", "lorebooks"):
                            val = body.get(key)
                            if val:
                                return len(val)
                        return len(nodes) or (count if count > 0 else 0)

                    best_url, best_body = max(candidates, key=lambda c: _score(c[1]))
                    flog(f"  best (full url): {best_url}")
                    log(f"  best: {best_url[:90]}  (score={_score(best_body)})")
                    return best_url, best_body

                # Cards
                seen_fps: set[str] = set()
                all_card_nodes: list[dict] = []
                if self._fetch_cards:
                    for nav_url, label in [("https://chub.ai/my_characters", "my_characters"),
                                           ("https://chub.ai/favorites",     "favorites")]:
                        if self._cancel_event.is_set(): break
                        url, body = _sniff_page(nav_url, "chub.ai", label)
                        if url:
                            all_card_nodes.extend(_paginate_nodes(url, body, "cards", seen_fps))
                log(f"Cards: {len(all_card_nodes)}")
                if self._cancel_event.is_set():
                    q.put(("CANCELLED",)); return

                # Lorebooks — direct authenticated API calls using sess (has API key).
                # The my_lorebooks sniff revealed the exact params: my_favorites + only_mine.
                seen_lore: set[str] = set()
                all_lore_nodes: list[dict] = []
                if self._fetch_lorebooks:
                    def _paginate_lore_direct(extra: dict, label: str):
                        pg = 1
                        prev_cursor = None
                        while True:
                            if self._cancel_event.is_set():
                                flog(f"  lorebooks {label} cancelled at page {pg}"); break
                            params = {
                                "first": 48, "namespace": "lorebooks",
                                "nsfw": "true", "nsfl": "false", "chub": "true",
                                "sort": "created_at", "asc": "false",
                                "include_forks": "true", "count": "true",
                                **extra, "page": pg,
                            }
                            try:
                                r = sess.get(f"{REPO_API}/search", params=params, timeout=30)
                                log(f"  → {r.url.split('?')[0]}  ({r.status_code})")
                                if not r.ok:
                                    break
                                body = r.json()
                                d = body.get("data", {}) if isinstance(body, dict) else {}
                                cursor = d.get("cursor")
                                batch = d.get("nodes") or d.get("lorebooks") or []
                                flog(f"  lorebooks {label} p{pg}: {len(batch)} nodes  count={d.get('count','?')}  cursor={str(cursor)[:40] if cursor else None}")
                                if not batch:
                                    flog(f"  lorebooks {label} stopping — empty batch"); break
                                prev_count = len(all_lore_nodes)
                                for n in _collect(batch, "lorebooks", seen_lore):
                                    all_lore_nodes.append(n)
                                if not cursor:
                                    flog(f"  lorebooks {label} stopping — no cursor after page {pg}"); break
                                if cursor == prev_cursor:
                                    flog(f"  lorebooks {label} stopping — cursor stuck: {str(cursor)[:40]}"); break
                                if len(all_lore_nodes) == prev_count:
                                    flog(f"  lorebooks {label} stopping — no new unique items on page {pg}"); break
                                prev_cursor = cursor
                                pg += 1
                                time.sleep(0.3)
                            except Exception as e:
                                log(f"  lorebooks {label} error: {e}")
                                break

                    # Lorebooks the user has saved/favorited (includes others' lorebooks)
                    _paginate_lore_direct({"my_favorites": "true"}, "my_favorites")
                    # Lorebooks authored/forked by this user (includes private ones).
                    # The site uses username= + only_mine=all; "only_mine" values: all/false/true
                    # (only_mine=true returns 400, "all" with username= scopes to that user's content)
                    if my_handle:
                        _paginate_lore_direct({
                            "username": my_handle,
                            "only_mine": "all",
                            "exclude_mine": "false",
                            "include_forks": "true",
                        }, "authored")

                log(f"Lorebooks: {len(all_lore_nodes)}")
                if all_lore_nodes:
                    flog(f"  first 5 lorebooks: {[n.get('name', n.get('fullPath','?')) for n in all_lore_nodes[:5]]}")
                if self._cancel_event.is_set():
                    q.put(("CANCELLED",)); return

                # Presets — same direct-API approach as lorebooks; namespace=presets
                seen_presets: set[str] = set()
                all_preset_nodes: list[dict] = []
                if self._fetch_presets:
                    def _paginate_preset_direct(extra: dict, label: str):
                        pg = 1
                        prev_cursor = None
                        while True:
                            if self._cancel_event.is_set():
                                flog(f"  presets {label} cancelled at page {pg}"); break
                            params = {
                                "first": 48, "namespace": "presets",
                                "nsfw": "true", "nsfl": "false", "chub": "true",
                                "sort": "created_at", "asc": "false",
                                "include_forks": "true", "count": "true",
                                **extra, "page": pg,
                            }
                            try:
                                r = sess.get(f"{REPO_API}/search", params=params, timeout=30)
                                log(f"  → {r.url.split('?')[0]}  ({r.status_code})")
                                if not r.ok:
                                    break
                                body = r.json()
                                d = body.get("data", {}) if isinstance(body, dict) else {}
                                cursor = d.get("cursor")
                                batch = d.get("nodes") or d.get("presets") or []
                                flog(f"  presets {label} p{pg}: {len(batch)} nodes  count={d.get('count','?')}  cursor={str(cursor)[:40] if cursor else None}")
                                if not batch:
                                    flog(f"  presets {label} stopping — empty batch"); break
                                prev_count = len(all_preset_nodes)
                                for n in _collect(batch, "presets", seen_presets):
                                    all_preset_nodes.append(n)
                                if not cursor:
                                    flog(f"  presets {label} stopping — no cursor after page {pg}"); break
                                if cursor == prev_cursor:
                                    flog(f"  presets {label} stopping — cursor stuck: {str(cursor)[:40]}"); break
                                if len(all_preset_nodes) == prev_count:
                                    flog(f"  presets {label} stopping — no new unique items on page {pg}"); break
                                prev_cursor = cursor
                                pg += 1
                                time.sleep(0.3)
                            except Exception as e:
                                log(f"  presets {label} error: {e}")
                                break

                    # Presets the user has saved/favorited
                    _paginate_preset_direct({"my_favorites": "true"}, "my_favorites")
                    # Presets authored/forked by this user (includes private ones)
                    if my_handle:
                        _paginate_preset_direct({
                            "username": my_handle,
                            "only_mine": "all",
                            "exclude_mine": "false",
                            "include_forks": "true",
                        }, "authored")

                log(f"Presets: {len(all_preset_nodes)}")
                if all_preset_nodes:
                    flog(f"  first 5 presets: {[n.get('name', n.get('fullPath','?')) for n in all_preset_nodes[:5]]}")
                if self._cancel_event.is_set():
                    q.put(("CANCELLED",)); return

                # Personas — direct authenticated call to gateway API
                all_persona_nodes: list[dict] = []
                if self._fetch_personas:
                    try:
                        r = sess.get(f"{GATEWAY_API}/api/personas", timeout=30)
                        log(f"  → {r.url.split('?')[0]}  ({r.status_code})")
                        if r.ok:
                            body = r.json()
                            nodes = body if isinstance(body, list) else body.get("personas", body.get("data", []))
                            if isinstance(nodes, list):
                                all_persona_nodes = nodes
                                flog(f"  personas: {len(all_persona_nodes)}")
                                if all_persona_nodes:
                                    flog(f"  first persona keys: {list(all_persona_nodes[0].keys())}")
                                for n in all_persona_nodes:
                                    pid = str(n.get("id") or n.get("name", ""))
                                    label = n.get("name") or n.get("id") or pid
                                    add("personas", pid, label)

                                # Load persona avatars concurrently
                                if all_persona_nodes:
                                    import concurrent.futures as _cf
                                    import threading as _th
                                    def _fetch_persona_avatar(n):
                                        pid = str(n.get("id") or n.get("name", ""))
                                        av = n.get("avatar") or n.get("avatar_url") or n.get("image_url")
                                        if not av: return
                                        try:
                                            r = sess.get(av, timeout=10)
                                            if r.ok:
                                                image("personas", pid, r.content)
                                        except Exception:
                                            pass
                                    def _load_persona_avatars():
                                        with _cf.ThreadPoolExecutor(max_workers=2) as ex:
                                            ex.map(_fetch_persona_avatar, all_persona_nodes)
                                    _th.Thread(target=_load_persona_avatars, daemon=True).start()
                    except Exception as e:
                        log(f"  personas error: {e}")

                log(f"Personas: {len(all_persona_nodes)}")
                if self._cancel_event.is_set():
                    q.put(("CANCELLED",)); return

                # Chats
                all_sessions: list[dict] = []
                if self._fetch_chats:
                    url, body = _sniff_page("https://chub.ai/my_chats", "chub.ai", "my_chats", wait_ms=10000)
                    all_sessions = _paginate_sessions(url, body) if url else []
                if all_sessions:
                    flog(f"  first chat item: {json.dumps(all_sessions[0], ensure_ascii=False, default=str)[:2000]}")

                # Group sessions by character — one tile per character
                char_groups: dict[str, list] = {}   # folder_key → [session, ...]
                char_names:  dict[str, str]  = {}   # folder_key → display name
                for s in all_sessions:
                    sid = str(s.get("id") or s.get("session_id", ""))
                    if not sid: continue
                    cname, fkey = _char_folder_key(s)
                    char_groups.setdefault(fkey, []).append(s)
                    char_names[fkey] = cname

                for fkey, group in char_groups.items():
                    n     = len(group)
                    label = f"{char_names[fkey]}  ({n} chat{'s' if n != 1 else ''})"
                    add("chats", fkey, label)
                log(f"Chats: {len(all_sessions)} across {len(char_groups)} character(s)")

                # Load one avatar per character group concurrently
                if char_groups:
                    import concurrent.futures as _cf
                    def _fetch_avatar(item):
                        fkey, group = item
                        url = _chat_avatar_url(group[0])
                        if not url: return
                        try:
                            r = sess.get(url, timeout=10)
                            if r.ok:
                                image("chats", fkey, r.content)
                        except Exception:
                            pass
                    def _load_avatars():
                        with _cf.ThreadPoolExecutor(max_workers=2) as ex:
                            ex.map(_fetch_avatar, char_groups.items())
                    import threading as _th
                    _th.Thread(target=_load_avatars, daemon=True).start()

                browser.close()

            total = len(all_card_nodes) + len(all_lore_nodes) + len(all_preset_nodes) + len(all_persona_nodes) + len(char_groups)
            q.put(("SHOW_DL_BTN", total, len(all_sessions), len(char_groups)))
            log(f"Found {len(all_card_nodes)} cards · {len(all_lore_nodes)} lorebooks · "
                f"{len(all_preset_nodes)} presets · {len(all_persona_nodes)} personas · "
                f"{len(all_sessions)} chats ({len(char_groups)} characters).  Click ⬇ Download All when ready.")

            # Store fetched data for the download phase (and for re-downloads)
            self._fetch_sess          = sess
            self._fetched_cards       = all_card_nodes
            self._fetched_lore        = all_lore_nodes
            self._fetched_presets     = all_preset_nodes
            self._fetched_personas    = all_persona_nodes
            self._fetched_sessions    = all_sessions
            self._fetched_char_groups = char_groups

            # Wait for the user to click ⬇ Download All
            self._download_ready.wait()

        except Exception:
            _log_file.write(_tb.format_exc() + "\n")
            q.put(("ERROR", _tb.format_exc()))
            _log_file.close()
            return
        _log_file.close()

        # If _download_ready was set by a cancel (not a genuine Download click),
        # skip the download phase entirely.
        if self._cancel_event.is_set():
            return

        # Hand off to the download phase (runs in this same thread)
        self._cancel_download_event.clear()
        self._download_thread = threading.current_thread()
        self._download_phase()

    # ── download phase ────────────────────────────────────────────────────────

    def _download_phase(self):
        import traceback as _tb
        q    = self._q
        sess = self._fetch_sess
        all_card_nodes   = self._fetched_cards
        all_lore_nodes   = self._fetched_lore
        all_preset_nodes = self._fetched_presets
        all_persona_nodes = self._fetched_personas
        char_groups      = self._fetched_char_groups

        _log_file = open(ROOT / "debug.log", "a", encoding="utf-8", buffering=1)
        _log_file.write(f"\n{'─'*40} download {time.strftime('%H:%M:%S')} {'─'*40}\n")

        def log(m):
            _log_file.write(m + "\n")
            q.put(("LOG", m))
        def flog(m):
            _log_file.write(m + "\n")
        def status(t, k, s): q.put(("STATUS", t, k, s))
        def image(t, k, b):  q.put(("IMAGE",  t, k, b))

        try:
            saved = 0
            card_fmt = getattr(self, "_card_fmt", "PNG")
            chat_fmt = getattr(self, "_chat_fmt", "JSONL")

            snapshot         = getattr(self, "_checked_snapshot", {})
            checked_cards    = snapshot.get("cards",     set())
            checked_lore     = snapshot.get("lorebooks",  set())
            checked_presets  = snapshot.get("presets",    set())
            checked_personas = snapshot.get("personas",   set())
            checked_chats    = snapshot.get("chats",      set())
            self._total = (
                sum(1 for n in all_card_nodes    if n.get("fullPath","") in checked_cards) +
                sum(1 for n in all_lore_nodes    if n.get("fullPath","") in checked_lore) +
                sum(1 for n in all_preset_nodes  if n.get("fullPath","") in checked_presets) +
                sum(1 for n in all_persona_nodes if str(n.get("id") or n.get("name","")) in checked_personas) +
                sum(1 for fkey in char_groups    if fkey in checked_chats)
            )
            self._done = 0
            q.put(("LOG", f"Downloading {self._total} selected item(s)…"))

            def _dl_cancelled():
                if self._cancel_download_event.is_set():
                    q.put(("DOWNLOAD_CANCELLED",)); return True
                return False

            if _dl_cancelled(): return

            for n in all_card_nodes:
                fp   = n.get("fullPath", "")
                name = n.get("name", fp) or fp
                if not fp: continue
                if fp not in checked_cards: continue
                # Use fullPath-based filename to prevent collisions between same-named cards
                fname  = _safe(fp.replace("/", "_"))
                png_p  = CARDS_DIR / f"{fname}.png"
                json_p = CARDS_DIR / f"{fname}.json"
                want_png  = card_fmt != "JSON"
                want_json = card_fmt != "PNG"
                already = (
                    (want_png and want_json and png_p.exists() and json_p.exists()) or
                    (want_png and not want_json and png_p.exists()) or
                    (not want_png and want_json and json_p.exists())
                )
                if already:
                    if png_p.exists(): image("cards", fp, png_p.read_bytes())
                    status("cards", fp, "skipped"); saved += 1; continue
                status("cards", fp, "downloading")
                ok = _sess_download_card(sess, fp, png_p, json_p, image, "cards", log, flog,
                                         fmt=card_fmt)
                status("cards", fp, "done" if ok else "failed")
                if ok: saved += 1
                time.sleep(DELAY)

            if _dl_cancelled(): return

            for n in all_lore_nodes:
                fp   = n.get("fullPath", "")
                name = n.get("name", fp) or fp
                if not fp: continue
                if fp not in checked_lore: continue
                out_p = LORE_DIR / f"{_safe(name)}.json"
                if out_p.exists():
                    status("lorebooks", fp, "skipped"); saved += 1; continue
                status("lorebooks", fp, "downloading")
                ok = False
                # fullPath from search includes "lorebooks/" namespace prefix — strip it
                api_path = fp[len("lorebooks/"):] if fp.startswith("lorebooks/") else fp
                # Try original case then lowercase-author fallback (API is case-sensitive)
                lore_lc = _lc_author(api_path)
                for url in [f"{REPO_API}/api/lorebooks/{api_path}?full=true",
                             f"{REPO_API}/api/lorebooks/{lore_lc}?full=true",
                             f"{REPO_API}/api/lorebooks/{api_path}/raw_definition",
                             f"{REPO_API}/api/lorebooks/{lore_lc}/raw_definition",
                             f"{REPO_API}/api/lorebooks/{api_path}",
                             f"{REPO_API}/api/lorebooks/{lore_lc}"]:
                    try:
                        r = sess.get(url, timeout=30)
                        if not r.ok:
                            log(f"  {url} → {r.status_code}"); continue
                        try:
                            out_p.write_text(
                                json.dumps(r.json(), indent=2, ensure_ascii=False),
                                encoding="utf-8")
                        except Exception:
                            out_p.write_bytes(r.content)
                        ok = True; time.sleep(DELAY); break
                    except Exception as e:
                        log(f"  {url} error: {e}")
                if not ok:
                    log(f"  lorebook failed: {fp}")
                status("lorebooks", fp, "done" if ok else "failed")
                if ok: saved += 1

            if _dl_cancelled(): return

            PRESETS_DIR.mkdir(parents=True, exist_ok=True)
            for n in all_preset_nodes:
                fp   = n.get("fullPath", "")
                name = n.get("name", fp) or fp
                if not fp: continue
                if fp not in checked_presets: continue
                # fullPath from search includes "presets/" namespace prefix — strip it
                api_path = fp[len("presets/"):] if fp.startswith("presets/") else fp
                # Chub's API is case-sensitive — normalise author to lowercase
                api_path_lc = _lc_author(api_path)
                node_id = str(n.get("id", ""))
                out_p = PRESETS_DIR / f"{_safe(fp.replace('/', '_'))}.json"
                if out_p.exists():
                    status("presets", fp, "skipped"); saved += 1; continue
                status("presets", fp, "downloading")
                ok = False
                url_candidates = [
                    f"{REPO_API}/api/presets/{api_path}?full=true",
                    f"{REPO_API}/api/presets/{api_path_lc}?full=true",
                    f"{REPO_API}/api/presets/{api_path}",
                    f"{REPO_API}/api/presets/{api_path_lc}",
                ]
                if node_id:
                    url_candidates += [
                        f"{REPO_API}/api/presets/{node_id}?full=true",
                        f"{REPO_API}/api/presets/{node_id}",
                    ]
                for url in url_candidates:
                    try:
                        r = sess.get(url, timeout=30)
                        if not r.ok:
                            log(f"  {url} → {r.status_code}"); continue
                        try:
                            out_p.write_text(
                                json.dumps(r.json(), indent=2, ensure_ascii=False),
                                encoding="utf-8")
                        except Exception:
                            out_p.write_bytes(r.content)
                        ok = True; time.sleep(DELAY); break
                    except Exception as e:
                        log(f"  {url} error: {e}")
                if not ok:
                    log(f"  preset failed: {fp}")
                    flog(f"  preset node keys: {list(n.keys())}  id={node_id!r}  fp={fp!r}")
                status("presets", fp, "done" if ok else "failed")
                if ok: saved += 1

            if _dl_cancelled(): return

            # Personas
            PERSONAS_DIR.mkdir(parents=True, exist_ok=True)
            for n in all_persona_nodes:
                pid   = str(n.get("id") or n.get("name", ""))
                pname = n.get("name") or pid
                if not pid: continue
                if pid not in checked_personas: continue
                # Each persona gets its own folder: Personas/{Name}_{ID}/
                chub_id = str(n.get("id", ""))
                folder_name = f"{_safe(pname)}_{chub_id}" if chub_id else _safe(pname)
                persona_dir = PERSONAS_DIR / folder_name
                out_p = persona_dir / f"{_safe(pname)}.json"
                if out_p.exists():
                    status("personas", pid, "skipped"); saved += 1; continue
                status("personas", pid, "downloading")
                try:
                    persona_dir.mkdir(parents=True, exist_ok=True)
                    out_p.write_text(
                        json.dumps(n, indent=2, ensure_ascii=False),
                        encoding="utf-8")
                    # Try to download avatar if present
                    avatar_url = n.get("avatar_url") or n.get("avatar") or n.get("image_url")
                    if avatar_url:
                        try:
                            r = sess.get(avatar_url, timeout=20)
                            if r.ok:
                                ext = ".png" if "png" in r.headers.get("content-type","").lower() else ".jpg"
                                (persona_dir / f"{_safe(pname)}{ext}").write_bytes(r.content)
                        except Exception as e:
                            flog(f"  persona avatar error for {pname}: {e}")
                    status("personas", pid, "done"); saved += 1
                    time.sleep(DELAY)
                except Exception as e:
                    log(f"  persona failed: {pname} — {e}")
                    status("personas", pid, "failed")

            if _dl_cancelled(): return

            flog(f"checked_chats groups: {len(checked_chats)}")
            _first_chat_logged = False
            _chat_ext = {"JSONL": ".jsonl", "JSON": ".json", "TXT": ".txt"}.get(chat_fmt, ".jsonl")
            for fkey, group in char_groups.items():
                if fkey not in checked_chats: continue
                status("chats", fkey, "downloading")
                out_dir = CHATS_DIR / fkey
                # Derive character name from first session in group
                cname, _ = _char_folder_key(group[0])
                group_saved = 0
                group_failed = 0
                for s in group:
                    sid = str(s.get("id") or s.get("session_id", ""))
                    if not sid: continue
                    chat_own_name = s.get("name") or ""
                    file_stem = _safe(f"{chat_own_name}_{sid}" if chat_own_name else sid)
                    out_p = out_dir / f"{file_stem}{_chat_ext}"
                    if out_p.exists():
                        group_saved += 1; continue
                    try:
                        r = sess.get(
                            f"{GATEWAY_API}/api/core/chats/v2/{sid}",
                            params={"include_messages": "true",
                                    "include_config": "false",
                                    "include_meta": "false"},
                            timeout=30)
                        flog(f"  chat {sid} → {r.status_code}")
                        if r.ok:
                            body = r.json()
                            if not _first_chat_logged:
                                flog(f"  first chat body keys: {list(body.keys())[:10] if isinstance(body, dict) else type(body).__name__}")
                                _first_chat_logged = True
                            msgs = _extract_messages(body)
                            flog(f"  chat {sid} msgs (raw): {len(msgs)}")

                            # Chub only populates message content for the most recently
                            # loaded messages. For large chats the rest come back as
                            # {"message": null, ...}. Try paginating backwards by ID
                            # to recover the missing content.
                            msg_map = {m["id"]: m for m in msgs if "id" in m}
                            null_ids = [m["id"] for m in msgs if m.get("message") is None and "id" in m]
                            if null_ids:
                                flog(f"  chat {sid}: {len(null_ids)} null-content messages — fetching via POST /messages/content")
                                BATCH = 50
                                recovered_total = 0
                                for _bi in range(0, len(null_ids), BATCH):
                                    if self._cancel_download_event.is_set(): break
                                    batch_ids = null_ids[_bi:_bi + BATCH]
                                    try:
                                        pr = sess.post(
                                            f"{GATEWAY_API}/api/core/chats/v2/{sid}/messages/content",
                                            json={"ids": batch_ids},
                                            timeout=30)
                                        flog(f"  /messages/content batch {_bi//BATCH+1}: {len(batch_ids)} ids → {pr.status_code}")
                                        if not pr.ok:
                                            flog(f"  /messages/content error body: {pr.text[:300]}"); break
                                        resp_body = pr.json()
                                        flog(f"  /messages/content resp type={type(resp_body).__name__} "
                                             f"keys={list(resp_body.keys())[:6] if isinstance(resp_body, dict) else f'list[{len(resp_body)}]'}")
                                        # Response: {"messages": {"id_str": "content text", ...}, "extensions": ...}
                                        messages_val = resp_body.get("messages", {}) if isinstance(resp_body, dict) else resp_body
                                        recovered = 0
                                        if isinstance(messages_val, dict):
                                            for _id_str, _content in messages_val.items():
                                                if _content is None:
                                                    continue
                                                try:
                                                    _mid = int(_id_str)
                                                except (ValueError, TypeError):
                                                    continue
                                                if _mid in msg_map:
                                                    msg_map[_mid]["message"] = _content
                                                    recovered += 1
                                        elif isinstance(messages_val, list):
                                            for pm in messages_val:
                                                _mid = pm.get("id")
                                                _content = pm.get("message") if pm.get("message") is not None else pm.get("content")
                                                if _mid is not None and _content is not None:
                                                    _mid = int(_mid) if not isinstance(_mid, int) else _mid
                                                    if _mid in msg_map:
                                                        msg_map[_mid]["message"] = _content
                                                        recovered += 1
                                        flog(f"  /messages/content batch {_bi//BATCH+1}: {len(messages_val)} returned, {recovered} recovered")
                                        recovered_total += recovered
                                    except Exception as pe:
                                        flog(f"  /messages/content batch error: {pe}"); break
                                    time.sleep(DELAY)
                                flog(f"  /messages/content total recovered: {recovered_total} of {len(null_ids)}")

                            msgs = sorted(msg_map.values(), key=lambda m: int(m.get("id", 0)))
                            # Drop any remaining stubs that still have no content
                            null_remaining = sum(1 for m in msgs if m.get("message") is None)
                            if null_remaining:
                                log(f"  chat {sid}: {null_remaining} messages still missing content after pagination — dropping stubs")
                                msgs = [m for m in msgs if m.get("message") is not None]
                            flog(f"  chat {sid} msgs (final): {len(msgs)}")

                            if msgs:
                                out_dir.mkdir(parents=True, exist_ok=True)
                                if chat_fmt == "JSON":
                                    out_p.write_text(
                                        json.dumps(msgs, indent=2, ensure_ascii=False),
                                        encoding="utf-8")
                                elif chat_fmt == "TXT":
                                    out_p.write_text(
                                        _msgs_to_txt(msgs, cname),
                                        encoding="utf-8")
                                else:  # JSONL (default)
                                    out_p.write_text(
                                        "\n".join(json.dumps(m, ensure_ascii=False) for m in msgs),
                                        encoding="utf-8")
                                group_saved += 1
                            # else: empty chat — skip silently
                        else:
                            log(f"  chat {sid} → {r.status_code}")
                            group_failed += 1
                    except Exception as e:
                        log(f"  chat {sid} error: {e}")
                        group_failed += 1
                    time.sleep(DELAY)

                if group_failed > 0 and group_saved == 0:
                    status("chats", fkey, "failed")
                elif group_saved > 0:
                    status("chats", fkey, "done"); saved += 1
                else:
                    status("chats", fkey, "skipped"); saved += 1

            q.put(("DONE", saved))

        except Exception:
            _log_file.write(_tb.format_exc() + "\n")
            q.put(("ERROR", _tb.format_exc()))
        finally:
            _log_file.close()


# ─── requests-based API helpers ────────────────────────────────────────────────

def _sess_search(sess, namespace: str, extra: dict, log) -> list[dict]:
    """Paginate through /search using a requests.Session (browser cookies)."""
    nodes: list[dict] = []
    pg = 1
    while True:
        params = {
            "namespace": namespace,
            "first": 48, "page": pg,
            "sort": "id", "asc": "false",
            "venus": "false", "chub": "true", "nsfw": "true",
            **extra,
        }
        url = f"{REPO_API}/search"
        log(f"  GET {url} | ns={namespace} params={dict(list(params.items())[:4])}…")
        try:
            r = sess.get(url, params=params, timeout=30)
        except Exception as e:
            log(f"  search p{pg} error: {e}"); break
        log(f"  → {r.status_code}")
        if not r.ok:
            log(f"  body preview: {r.text[:300]}"); break
        body  = r.json()
        data_block = body.get("data", {})
        batch = data_block.get("nodes", [])
        if not batch:
            count = data_block.get("count", "?")
            log(f"  empty nodes (count={count}) — data keys: {list(data_block.keys())}")
            break
        nodes.extend(batch)
        log(f"  page {pg}: +{len(batch)} (total {len(nodes)})")
        if not body.get("cursor"):
            break
        pg += 1
        time.sleep(0.3)
    return nodes


def _sess_get_sessions(sess, log) -> list[dict]:
    # Try known paths on gateway.chub.ai
    candidate_bases = [
        f"{GATEWAY_API}/api/core/chats",
        f"{GATEWAY_API}/api/chats",
        f"{REPO_API}/api/chats",
    ]
    working_base: str | None = None
    for base in candidate_bases:
        try:
            r = sess.get(base, params={"page": 1, "first": 1}, timeout=10)
            log(f"  probe {base} → {r.status_code}")
            if r.ok:
                working_base = base
                break
        except Exception as e:
            log(f"  probe {base} error: {e}")

    if not working_base:
        log("  could not find sessions endpoint — chats skipped")
        return []

    sessions: list[dict] = []
    pg = 1
    while True:
        try:
            r = sess.get(working_base, params={"page": pg, "first": 50}, timeout=30)
        except Exception as e:
            log(f"  sessions p{pg} error: {e}"); break
        if not r.ok:
            log(f"  sessions p{pg} → HTTP {r.status_code} | {r.text[:200]}"); break
        data  = r.json()
        log(f"  sessions p{pg} keys: {list(data.keys()) if isinstance(data, dict) else type(data).__name__}")
        items = (data.get("results") or data.get("data") or
                 data.get("sessions") or data.get("items") or
                 (data if isinstance(data, list) else []))
        if not items:
            break
        sessions.extend(items)
        log(f"  sessions p{pg}: +{len(items)} (total {len(sessions)})")
        if len(items) < 50:
            break
        pg += 1
        time.sleep(0.3)
    return sessions


def _msgs_to_txt(msgs: list, char_name: str) -> str:
    """Convert chat messages to a human-readable transcript."""
    lines = []
    if char_name:
        lines.append(f"Character: {char_name}")
        lines.append("─" * 40)
        lines.append("")
    for m in msgs:
        role    = m.get("role", "unknown")
        content = m.get("content", "")
        if isinstance(content, list):
            content = " ".join(
                p.get("text", "") if isinstance(p, dict) else str(p) for p in content)
        speaker = (
            "User" if role in ("user", "human") else
            (char_name or "Assistant") if role in ("assistant", "model", "char", "character") else
            f"[{role}]"
        )
        lines.append(f"{speaker}: {content}")
        lines.append("")
    return "\n".join(lines)


def _sess_download_card(sess, fp: str, png_p: Path, json_p: Path,
                        image_cb, tab: str, log, flog=None,
                        fmt: str = "PNG") -> bool:
    if flog is None:
        flog = log  # fallback: debug lines go to status bar too
    want_png  = fmt != "JSON"
    want_json = fmt != "PNG"
    png_data: bytes | None = None

    # 1. CDN card — the full SillyTavern PNG lives here for most cards
    if want_png:
        for cdn_name in ("chara_card_v2.png", "chara_card_v2.webp", "avatar.webp"):
            try:
                r = sess.get(f"{CDN_BASE}/{fp}/{cdn_name}", timeout=30)
                if r.ok and r.content[:4] == b"\x89PNG":
                    png_data = r.content
                    break
                elif r.ok and cdn_name.endswith(".webp") and not png_data:
                    # webp avatar — convert to PNG, embed JSON later
                    img = Image.open(io.BytesIO(r.content)).convert("RGB")
                    buf = io.BytesIO(); img.save(buf, "PNG")
                    png_data = buf.getvalue()
                    break
            except Exception as e:
                log(f"  CDN {cdn_name} error: {e}")

        # 2. API download endpoint (authenticated, better for private cards)
        if not png_data:
            fp_lc = _lc_author(fp)
            for dl_fp in ([fp] if fp == fp_lc else [fp, fp_lc]):
                try:
                    r = sess.get(f"{REPO_API}/api/characters/{dl_fp}/download", timeout=60)
                    if r.ok and r.content[:4] == b"\x89PNG":
                        png_data = r.content; break
                    else:
                        log(f"  /download → {r.status_code}")
                except Exception as e:
                    log(f"  /download error: {e}")

    # 3. Fetch JSON definition (always needed for PNG embedding; optionally saved)
    char_json: dict | None = None
    fp_lc = _lc_author(fp)
    json_urls = [f"{REPO_API}/api/characters/{fp}/raw_definition",
                 f"{REPO_API}/api/characters/{fp}"]
    if fp != fp_lc:
        json_urls += [f"{REPO_API}/api/characters/{fp_lc}/raw_definition",
                      f"{REPO_API}/api/characters/{fp_lc}"]
    for url in json_urls:
        try:
            r = sess.get(url, timeout=30)
            if not r.ok:
                log(f"  {url} → {r.status_code}"); continue
            j = r.json()
            if "spec" in j or "name" in j or "node" in j:
                char_json = j
                if want_json and not json_p.exists():
                    json_p.write_text(json.dumps(char_json, indent=2, ensure_ascii=False),
                                      encoding="utf-8")
                time.sleep(DELAY); break
        except Exception as e:
            log(f"  {url} error: {e}")

    # 4. If we got a plain PNG/webp (no embedded JSON) but now have the JSON, embed it
    if want_png and png_data and char_json and png_data[:4] == b"\x89PNG":
        try:
            existing_chara = _extract_chara_chunk(png_data)
            if not existing_chara:
                png_data = _build_st_png(png_data, char_json)
        except Exception:
            pass

    if want_png and png_data:
        png_p.write_bytes(png_data)
        image_cb(tab, fp, png_data)

    if want_png:
        return png_p.exists()
    else:
        return json_p.exists()


def _extract_chara_chunk(png: bytes) -> bool:
    """Return True if this PNG already has a tEXt chara chunk."""
    i = 8
    while i < len(png) - 12:
        length = struct.unpack(">I", png[i:i+4])[0]
        chunk_type = png[i+4:i+8]
        if chunk_type == b"tEXt" and png[i+8:i+13] == b"chara":
            return True
        i += 12 + length
    return False


# ─── (legacy placeholders — kept so nothing breaks if referenced) ──────────────

def _browser_search(page, extra_params: dict, namespace: str, log) -> list[dict]:
    """Paginate through all pages of /search using browser fetch."""
    base = {
        "namespace": namespace,
        "first": 48, "sort": "id", "asc": "false",
        "venus": "false", "chub": "true", "nsfw": "true",
        **extra_params,
    }
    nodes: list[dict] = []
    pg = 1
    while True:
        r = _bfetch(page, f"{REPO_API}/search", {**base, "page": pg})
        if not r.get("ok"):
            log(f"  search p{pg} → {r.get('status')} {r.get('error','')}")
            break
        batch = (r.get("data") or {}).get("data", {}).get("nodes", [])
        if not batch:
            break
        nodes.extend(batch)
        log(f"  page {pg}: +{len(batch)} (running total {len(nodes)})")
        if not (r.get("data") or {}).get("cursor"):
            break
        pg += 1
        time.sleep(0.3)
    return nodes


def _browser_get_sessions(page, log) -> list[dict]:
    """Fetch all chat sessions via browser fetch (gateway.chub.ai)."""
    sessions: list[dict] = []
    pg = 1
    while True:
        r = _bfetch(page, f"{GATEWAY_API}/api/core/chats", {"page": pg, "first": 50})
        if not r.get("ok"):
            log(f"  sessions p{pg} → {r.get('status')} {r.get('error','')}")
            break
        data  = r.get("data") or {}
        items = data.get("results") or data.get("data") or []
        if not items:
            break
        sessions.extend(items)
        if len(items) < 50:
            break
        pg += 1
        time.sleep(0.3)
    return sessions


# ─── PNG builder ──────────────────────────────────────────────────────────────

def _build_st_png(avatar_bytes: bytes, char_json: dict) -> bytes:
    """Return SillyTavern-compatible PNG: avatar image with 'chara' tEXt chunk."""
    img = Image.open(io.BytesIO(avatar_bytes)).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    png = buf.getvalue()

    b64 = base64.b64encode(json.dumps(char_json, ensure_ascii=False).encode()).decode()
    payload = b"chara\x00" + b64.encode()
    crc     = zlib.crc32(b"tEXt" + payload) & 0xFFFFFFFF
    chunk   = struct.pack(">I", len(payload)) + b"tEXt" + payload + struct.pack(">I", crc)

    # Insert the tEXt chunk just before IEND (last 12 bytes = len+IEND+crc)
    return png[:-12] + chunk + png[-12:]


def _download_card(page, req, fp: str, png_p: Path, json_p: Path,
                   image_cb, tab: str, log) -> bool:
    """Download character PNG + JSON. Returns True if anything was saved."""
    avatar_url = f"https://avatars.charhub.io/avatars/{fp}/avatar.webp"
    ok = False

    # ── 1. Try the official download endpoint (SillyTavern PNG) ───────────────
    png_data: bytes | None = None
    dl_url = f"{REPO_API}/api/characters/{fp}/download"

    # Try via browser first (guaranteed auth), then ctx.request fallback
    br = _bfetch(page, dl_url, binary=True)
    if br.get("ok"):
        raw = base64.b64decode(br["b64"])
        if raw[:4] == b"\x89PNG":
            png_data = raw
        else:
            log(f"  /download (browser) → non-PNG ({len(raw)} bytes)")
    else:
        log(f"  /download (browser) → {br.get('status')} {br.get('error','')}")
        try:
            r = req.get(dl_url)
            if r.ok:
                body = r.body()
                if body[:4] == b"\x89PNG":
                    png_data = body
                else:
                    log(f"  /download (req) → non-PNG ({len(body)} bytes, {body[:8]})")
            else:
                log(f"  /download (req) → {r.status}")
        except Exception as e:
            log(f"  /download error: {e}")

    # ── 2. Get the raw character JSON ──────────────────────────────────────────
    char_json: dict | None = None
    for url in [f"{REPO_API}/api/characters/{fp}/raw_definition",
                f"{REPO_API}/api/characters/{fp}"]:
        try:
            r = req.get(url)
            if not r.ok:
                log(f"  {url} → {r.status}")
                continue
            body = r.body()
            j = json.loads(body)
            # raw_definition → might be the V2 card JSON directly, or wrapped
            if "spec" in j or "name" in j or "node" in j:
                char_json = j
            if char_json:
                if not json_p.exists():
                    json_p.write_text(json.dumps(char_json, indent=2, ensure_ascii=False),
                                      encoding="utf-8")
                time.sleep(DELAY)
                break
        except Exception as e:
            log(f"  {url} error: {e}")

    # ── 3. If no official PNG, build one from avatar + char JSON ──────────────
    if not png_data:
        try:
            r = req.get(avatar_url)
            if r.ok:
                av_bytes = r.body()
                if char_json:
                    png_data = _build_st_png(av_bytes, char_json)
                else:
                    # No char JSON → save plain converted PNG (not SillyTavern embedded)
                    img = Image.open(io.BytesIO(av_bytes)).convert("RGB")
                    buf = io.BytesIO()
                    img.save(buf, "PNG")
                    png_data = buf.getvalue()
            else:
                log(f"  {avatar_url} → {r.status}")
        except Exception as e:
            log(f"  avatar error: {e}")

    # ── 4. Save PNG and update tile image ─────────────────────────────────────
    if png_data:
        png_p.write_bytes(png_data)
        image_cb(tab, fp, png_data)
        ok = True

    # At minimum, if JSON was saved count it as ok
    if json_p.exists():
        ok = True

    return ok


# ─── API helpers (using Playwright APIRequestContext) ─────────────────────────

def _search(req, kind: str, extra: dict, log) -> list[dict]:
    """Paginate through /search and return all matching nodes."""
    namespace = "characters" if kind == "characters" else "lorebooks"
    nodes: list[dict] = []
    page = 1
    while True:
        params = {
            "search": "", "first": 48, "page": page,
            "sort": "id", "asc": "false",
            "venus": "false", "chub": "true", "nsfw": "true",
            "namespace": namespace,
            **extra,
        }
        r = req.get(f"{REPO_API}/search", params=params)
        if not r.ok:
            log(f"  /search → HTTP {r.status}")
            break
        body  = r.json()
        batch = body.get("data", {}).get("nodes", [])
        if not batch:
            break
        nodes.extend(batch)
        if not body.get("cursor"):
            break
        page += 1
        time.sleep(0.3)
    return nodes


def _get_sessions(req, log) -> list[dict]:
    sessions: list[dict] = []
    page = 1
    while True:
        r = req.get(f"{GATEWAY_API}/api/core/chats",
                    params={"page": page, "first": 50})
        if not r.ok:
            log(f"  /api/core/chats → HTTP {r.status_code}")
            break
        try:
            data  = r.json()
            items = data.get("results") or data.get("data") or []
        except Exception:
            break
        if not items:
            break
        sessions.extend(items)
        if len(items) < 50:
            break
        page += 1
        time.sleep(0.3)
    return sessions

# ─── entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    for d in (CARDS_DIR, LORE_DIR, CHATS_DIR, PRESETS_DIR, PERSONAS_DIR):
        d.mkdir(parents=True, exist_ok=True)
    App().mainloop()
