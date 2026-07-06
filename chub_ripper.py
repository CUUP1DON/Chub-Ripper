#!/usr/bin/env python3
"""
Chub Ripper — downloads your Chub.ai cards, lorebooks, and chats.

Setup:
    pip install Pillow PyQt6 requests playwright
    playwright install chromium
"""

import base64
import ctypes
import ctypes.wintypes
import datetime as _dt
import io
import json
import os
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
        try:
            return path.read_text(encoding="utf-8").strip()
        except Exception:
            return ""

# ─── config ───────────────────────────────────────────────────────────────────

CONFIG_FILE = Path(__file__).parent / ".chub_config.json"

def _config_load() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}

def _config_save(updates: dict) -> None:
    try:
        cfg = _config_load()
        cfg.update(updates)
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass

# ─── dependency check ─────────────────────────────────────────────────────────

def _check_deps():
    missing = []
    errors  = []
    for pkg, pip_name in [("PIL", "Pillow"), ("PyQt6", "PyQt6"),
                           ("requests", "requests"), ("playwright", "playwright")]:
        try:
            __import__(pkg)
        except ImportError as e:
            missing.append(pip_name)
            errors.append(f"{pip_name}: {e}")
    if missing:
        import tkinter as _tk, tkinter.messagebox as _mb
        _r = _tk.Tk(); _r.withdraw()
        _mb.showerror("Missing packages",
            f"pip install {' '.join(missing)}"
            + ("\n  playwright install chromium" if "playwright" in missing else "")
            + f"\n\nRunning under: {sys.executable}"
            + "\n\nIf you already ran run.bat/run.sh and still see this, the pip\n"
              "install likely targeted a different Python than the one shown\n"
              "above, or failed silently. Re-run run.bat/run.sh and check the\n"
              "console output for the actual pip error.\n\nDetails:\n"
            + "\n".join(errors))
        sys.exit(1)

_check_deps()

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QGridLayout, QLabel, QPushButton, QLineEdit, QCheckBox,
    QProgressBar, QScrollArea, QFrame, QStackedWidget, QMenu, QDialog,
    QSizePolicy, QSpacerItem,
)
from PyQt6.QtCore import Qt, QTimer, QSize, QPoint, pyqtSignal
from PyQt6.QtGui import QPixmap, QImage, QColor, QFont, QCursor, QIcon

from PIL import Image, ImageDraw

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
    "queued":       DIM,
    "downloading":  ACCENT,
    "done":         GREEN,
    "skipped":      DIM,
    "failed":       RED,
    "unavailable":  "#e3b341",
}
STATUS_LABEL = {
    "queued":       "Queued",
    "downloading":  "Downloading…",
    "done":         "✓  Done",
    "skipped":      "Already saved",
    "failed":       "✗  Failed",
    "unavailable":  "⚠  Unavailable",
}

TW, TH = 128, 168   # image dimensions
COLS    = 6
PAGE    = 100        # tiles per lazy-load batch

# ─── QSS stylesheet ───────────────────────────────────────────────────────────

STYLESHEET = f"""
QMainWindow, QDialog, QWidget {{
    background-color: {BG};
    color: {TEXT};
    font-family: "Segoe UI";
    font-size: 10pt;
}}
QLabel {{ background-color: transparent; color: {TEXT}; }}
QLabel#appTitle {{
    font-size: 20pt;
    font-weight: bold;
    letter-spacing: -0.5px;
}}
QLabel#appSubtitle {{
    color: {MUTED};
    font-size: 10pt;
}}
QLabel#sectionLabel {{
    color: {DIM};
    font-size: 8pt;
    font-weight: bold;
    letter-spacing: 1.2px;
}}
QLabel#fmtLabel {{
    color: {MUTED};
    font-size: 9pt;
}}
QLabel#statusLbl {{ color: {MUTED}; font-size: 9pt; }}
QLabel#badge {{
    background-color: {SURFACE};
    color: {DIM};
    border-radius: 5px;
    padding: 2px 8px;
    font-size: 9pt;
}}
QLabel#progLbl {{ color: {MUTED}; }}

QPushButton {{
    background-color: {SURFACE};
    color: {MUTED};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 4px 10px;
    min-height: 28px;
    font-family: "Segoe UI";
    font-size: 10pt;
}}
QPushButton:hover {{ background-color: {CARD}; color: {TEXT}; }}
QPushButton:disabled {{ color: {DIM}; }}
QPushButton:pressed {{ background-color: {BG}; }}

QPushButton#fetchBtn {{
    background-color: {ACCENT}; color: {TEXT};
    border: none; font-weight: bold; font-size: 10pt;
    padding: 6px 18px;
}}
QPushButton#fetchBtn:hover {{ background-color: {A_HOVER}; }}
QPushButton#fetchBtn:disabled {{ background-color: {SURFACE}; color: {DIM}; }}

QPushButton#dlBtn {{
    background-color: {GREEN}; color: {BG};
    border: none; font-weight: bold; font-size: 11pt;
}}
QPushButton#dlBtn:hover {{ background-color: #2ea843; }}

QPushButton#retryBtn {{
    background-color: {RED}; color: {TEXT};
    border: none; font-weight: bold;
}}
QPushButton#retryBtn:hover {{ background-color: #c93f3a; }}

QPushButton#tabBtn {{
    background-color: transparent;
    color: {MUTED};
    border: none;
    border-bottom: 2px solid transparent;
    border-radius: 0;
    padding: 8px 14px 6px 14px;
    min-height: 32px;
    font-size: 10pt;
    text-align: left;
}}
QPushButton#tabBtn:hover {{
    color: {TEXT};
    background-color: {CARD};
}}
QPushButton#tabBtn[active="true"] {{
    color: {TEXT};
    border-bottom: 2px solid {ACCENT};
    font-weight: bold;
}}

QPushButton#folderBtn {{
    background-color: transparent;
    color: {DIM};
    border: none;
    border-radius: 4px;
    padding: 0;
    font-size: 11pt;
    min-height: 26px;
}}
QPushButton#folderBtn:hover {{ background-color: {CARD}; color: {ACCENT}; }}

QPushButton#loadMoreBtn {{
    background-color: {SURFACE}; color: {MUTED};
    border: 1px solid {BORDER}; border-radius: 8px;
    font-size: 10pt; min-height: 34px;
}}
QPushButton#loadMoreBtn:hover {{ background-color: {CARD}; color: {TEXT}; }}

QLineEdit {{
    background-color: {SURFACE};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 6px 10px;
    font-size: 10pt;
    selection-background-color: {ACCENT};
}}
QLineEdit:focus {{ border-color: {ACCENT}; }}
QLineEdit#tokenEntry {{
    font-family: "Consolas", "Menlo", monospace;
    font-size: 10pt;
    padding: 8px 12px;
}}
QLineEdit#searchEntry {{
    padding: 4px 10px;
}}

QCheckBox {{ color: {TEXT}; spacing: 8px; font-size: 10pt; }}
QFrame#tile QCheckBox {{ spacing: 0px; }}
QCheckBox::indicator {{
    width: 16px; height: 16px;
    border: 1px solid {BORDER};
    border-radius: 3px;
    background-color: {SURFACE};
}}
QCheckBox::indicator:checked {{
    background-color: {ACCENT};
    border-color: {ACCENT};
}}
QCheckBox::indicator:hover {{ border-color: {ACCENT}; }}

QPushButton#dropBtn {{
    background-color: {SURFACE};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 4px 12px;
    min-height: 28px;
    text-align: left;
}}
QPushButton#dropBtn:hover {{
    background-color: {CARD};
    border-color: {MUTED};
    color: {TEXT};
}}

QProgressBar {{
    background-color: {BORDER};
    border: none;
    border-radius: 3px;
    max-height: 5px;
    text-align: center;
}}
QProgressBar::chunk {{
    background-color: {ACCENT};
    border-radius: 3px;
}}

QScrollArea {{ background-color: {BG}; border: none; }}
QScrollBar:vertical {{
    background-color: {SURFACE};
    width: 10px; margin: 0;
}}
QScrollBar::handle:vertical {{
    background-color: {BORDER};
    border-radius: 5px;
    min-height: 30px; margin: 2px;
}}
QScrollBar::handle:vertical:hover {{ background-color: {DIM}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; border: none; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: none; }}
QScrollBar:horizontal {{ height: 0; }}

QFrame#divider {{
    background-color: {BORDER};
    border: none;
    max-height: 1px;
}}
QFrame#vsep {{
    background-color: {BORDER};
    border: none;
    max-width: 1px;
}}
QWidget#surface {{ background-color: {SURFACE}; }}
QWidget#header {{ background-color: {BG}; }}
QWidget#settingsBar {{ background-color: {BG}; }}
QWidget#statusStrip {{ background-color: {BG}; }}
QWidget#toolBar {{ background-color: {BG}; }}
QWidget#footerBar {{ background-color: {BG}; border-top: 1px solid {BORDER}; }}
QWidget#tileContainer {{ background-color: {BG}; }}

QMenu {{
    background-color: {SURFACE};
    color: {TEXT};
    border: 1px solid {BORDER};
}}
QMenu::item {{ padding: 4px 20px; }}
QMenu::item:selected {{ background-color: {ACCENT}; }}

QToolTip {{
    background-color: {SURFACE};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 4px;
    padding: 4px 8px;
    font-size: 10pt;
}}
"""

_FORCE_ON = (
    f"QPushButton {{ background-color: {ACCENT}; color: {TEXT}; border: none; "
    f"border-radius: 6px; padding: 4px 10px; min-height: 28px; font-size: 10pt; }}"
    f"QPushButton:hover {{ background-color: {A_HOVER}; }}"
)

# Tile frame stylesheets
_TILE_NORMAL = (
    f"QFrame#tile {{ background-color: {CARD}; border: 1px solid {BORDER}; border-radius: 10px; }}"
)
_TILE_HOVER = (
    f"QFrame#tile {{ background-color: {CARD}; border: 1px solid {ACCENT}; border-radius: 10px; }}"
)

# ─── placeholder pixmap ───────────────────────────────────────────────────────

_PH_PIX: QPixmap | None = None

def _hex_to_rgb(h: str) -> tuple:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)

def _placeholder() -> QPixmap:
    global _PH_PIX
    if _PH_PIX is None:
        cr, cg, cb = _hex_to_rgb(CARD)
        br, bg_c, bb = _hex_to_rgb(BORDER)
        dr, dg, db = _hex_to_rgb(DIM)
        img = Image.new("RGB", (TW, TH), (cr, cg, cb))
        draw = ImageDraw.Draw(img)
        draw.rectangle([2, 2, TW - 3, TH - 3], outline=(br, bg_c, bb), width=1)
        cx, cy = TW // 2, TH // 2
        r = 20
        draw.ellipse([cx-r, cy-r-12, cx+r, cy+r-12], outline=(dr, dg, db), width=2)
        draw.rectangle([cx-r, cy+r-6, cx+r, cy+r+28], outline=(dr, dg, db), width=2)
        _PH_PIX = _pil_to_pixmap(img)
    return _PH_PIX

def _pil_to_pixmap(img: Image.Image) -> QPixmap:
    rgb = img.convert("RGB")
    data = rgb.tobytes("raw", "RGB")
    qimg = QImage(data, rgb.width, rgb.height, rgb.width * 3, QImage.Format.Format_RGB888)
    return QPixmap.fromImage(qimg)

def _make_app_icon() -> QIcon:
    """Build the app/window icon at runtime so we don't ship a binary file.
    Rounded accent square with a white download arrow + baseline bar."""
    icon = QIcon()
    cr, cg, cb = _hex_to_rgb(ACCENT)
    white = (255, 255, 255, 255)
    for s in (16, 24, 32, 48, 64, 128, 256):
        img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        radius = max(2, int(s * 0.22))
        try:
            d.rounded_rectangle([0, 0, s - 1, s - 1], radius=radius,
                                fill=(cr, cg, cb, 255))
        except AttributeError:
            d.rectangle([0, 0, s - 1, s - 1], fill=(cr, cg, cb, 255))

        # Arrow shaft (centered vertical bar).
        shaft_w   = max(2, int(s * 0.14))
        shaft_top = int(s * 0.22)
        shaft_bot = int(s * 0.52)
        d.rectangle(
            [s // 2 - shaft_w // 2, shaft_top,
             s // 2 + shaft_w // 2, shaft_bot],
            fill=white,
        )
        # Arrowhead triangle.
        head_top = shaft_bot - max(1, int(s * 0.02))
        head_bot = int(s * 0.72)
        head_w   = max(4, int(s * 0.38))
        d.polygon(
            [(s // 2 - head_w // 2, head_top),
             (s // 2 + head_w // 2, head_top),
             (s // 2,               head_bot)],
            fill=white,
        )
        # Baseline bar (the “save-to-disk” line).
        bar_pad = int(s * 0.22)
        bar_y   = int(s * 0.82)
        bar_h   = max(2, int(s * 0.07))
        d.rectangle([bar_pad, bar_y, s - bar_pad, bar_y + bar_h], fill=white)

        qimg = QImage(
            img.tobytes("raw", "RGBA"),
            s, s, s * 4,
            QImage.Format.Format_RGBA8888,
        )
        icon.addPixmap(QPixmap.fromImage(qimg))
    return icon

# ─── text helpers ─────────────────────────────────────────────────────────────

def _safe(s: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', "_", str(s)).strip() or "unknown"

def _lc_author(path: str) -> str:
    parts = path.split("/", 1)
    if len(parts) == 2:
        return parts[0].lower() + "/" + parts[1]
    return path.lower()

# ─── message / branch helpers ─────────────────────────────────────────────────

def _extract_messages(body) -> list:
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
            msgs.sort(key=lambda m: int(m.get("id", 0)))
            return msgs
    return []

def _build_branches(msg_map: dict) -> list:
    if not msg_map:
        return []
    children: dict = {}
    for mid, msg in msg_map.items():
        pid = msg.get("parent_id")
        if pid is None:
            continue
        try:
            pid = int(pid)
        except (ValueError, TypeError):
            continue
        if pid in msg_map:
            children.setdefault(pid, []).append(mid)

    def _is_root(msg):
        pid = msg.get("parent_id")
        if pid is None:
            return True
        try:
            return int(pid) not in msg_map
        except (ValueError, TypeError):
            return True

    root_ids = sorted(mid for mid, msg in msg_map.items() if _is_root(msg))
    if not root_ids:
        return [sorted(msg_map.values(), key=lambda m: int(m.get("id", 0)))]

    branches = []
    stack = [(rid, []) for rid in reversed(root_ids)]
    while stack:
        node_id, path = stack.pop()
        if node_id not in msg_map:
            continue
        new_path = path + [msg_map[node_id]]
        kids = sorted(children.get(node_id, []))
        if not kids:
            branches.append(new_path)
        else:
            for kid in reversed(kids):
                stack.append((kid, new_path))
    return branches or [sorted(msg_map.values(), key=lambda m: int(m.get("id", 0)))]

def _build_main_path(msg_map: dict) -> list:
    """Walk msg_map along the is_main path, returning one entry per message:
    (node, siblings, index_of_node_in_siblings). Siblings are every message
    that shares the same parent (i.e. alternate branches at that point)."""
    if not msg_map:
        return []
    children: dict = {}
    for mid, msg in msg_map.items():
        pid = msg.get("parent_id")
        if pid is None:
            continue
        try:
            pid = int(pid)
        except (ValueError, TypeError):
            continue
        if pid in msg_map:
            children.setdefault(pid, []).append(mid)
    for kids in children.values():
        kids.sort()

    def _is_root(msg):
        pid = msg.get("parent_id")
        if pid is None:
            return True
        try:
            return int(pid) not in msg_map
        except (ValueError, TypeError):
            return True

    roots = sorted(mid for mid, msg in msg_map.items() if _is_root(msg))
    if not roots:
        return []

    def _pick_main(ids):
        for i in ids:
            if msg_map[i].get("is_main"):
                return i
        return ids[0]

    path = []
    cur = _pick_main(roots)
    while cur is not None:
        node = msg_map[cur]
        pid = node.get("parent_id")
        try:
            pid = int(pid) if pid is not None else None
        except (ValueError, TypeError):
            pid = None
        sibling_ids = roots if (pid is None or pid not in msg_map) else children.get(pid, [cur])
        siblings = [msg_map[s] for s in sibling_ids]
        idx = sibling_ids.index(cur)
        path.append((node, siblings, idx))
        kid_ids = children.get(cur, [])
        cur = _pick_main(kid_ids) if kid_ids else None
    return path

def _fmt_chat_date(iso: str) -> str:
    try:
        dt = _dt.datetime.fromisoformat((iso or "").replace("Z", "+00:00"))
    except Exception:
        return iso or ""
    return dt.strftime("%B %d, %Y %I:%M%p")

def _merged_st_lines(path: list, char_name: str, user_name: str) -> list:
    lines = []
    for node, siblings, idx in path:
        if node.get("message") is None:
            continue
        is_bot = bool(node.get("is_bot"))
        entry = {
            "name": char_name if is_bot else user_name,
            "is_user": not is_bot,
            "is_system": False,
            "send_date": _fmt_chat_date(node.get("created_at", "")),
            "mes": node.get("message") or "",
            "extra": {},
        }
        if len(siblings) > 1:
            entry["swipe_id"] = idx
            entry["swipes"] = [s.get("message") or "" for s in siblings]
        lines.append(entry)
    return lines

def _merged_chat_text(path: list, char_name: str, user_name: str, chat_fmt: str) -> str:
    lines = _merged_st_lines(path, char_name, user_name)
    if chat_fmt == "JSON":
        header = {"user_name": user_name, "character_name": char_name,
                  "create_date": _dt.datetime.now().strftime("%B %d, %Y %I:%M%p"),
                  "chat_metadata": {}}
        return json.dumps({"header": header, "messages": lines}, indent=2, ensure_ascii=False)
    if chat_fmt == "TXT":
        out = [f"Character: {char_name}", "─" * 40, ""]
        for entry in lines:
            out.append(f"{entry['name']}: {entry['mes']}")
            if entry.get("swipes"):
                for si, alt in enumerate(entry["swipes"]):
                    if si == entry["swipe_id"]:
                        continue
                    out.append(f"  ↳ [swipe {si+1}/{len(entry['swipes'])}] {alt}")
            out.append("")
        return "\n".join(out)
    # JSONL (SillyTavern chat format): header line + one message per line
    header = {"user_name": user_name, "character_name": char_name,
              "create_date": _dt.datetime.now().strftime("%B %d, %Y %I:%M%p"),
              "chat_metadata": {}}
    out_lines = [json.dumps(header, ensure_ascii=False)]
    out_lines.extend(json.dumps(e, ensure_ascii=False) for e in lines)
    return "\n".join(out_lines)

def _req(sess, method: str, url: str, max_retries: int = 3, **kwargs):
    for attempt in range(max_retries + 1):
        r = getattr(sess, method)(url, **kwargs)
        if r.status_code != 429:
            return r
        time.sleep(2 ** attempt)
    return r

def _item_already_saved(content_type: str, key: str, node: dict, card_fmt: str) -> bool:
    if content_type == "cards":
        fname = _safe(key.replace("/", "_"))
        if card_fmt != "JSON" and (CARDS_DIR / f"{fname}.png").exists():  return True
        if card_fmt != "PNG"  and (CARDS_DIR / f"{fname}.json").exists(): return True
        return False
    if content_type == "lorebooks":
        return (LORE_DIR / f"{_safe(node.get('name', key))}.json").exists()
    if content_type == "presets":
        return (PRESETS_DIR / f"{_safe(key.replace('/', '_'))}.json").exists()
    if content_type == "personas":
        chub_id = str(node.get("id", ""))
        pname   = node.get("name") or chub_id
        folder  = f"{_safe(pname)}_{chub_id}" if chub_id else _safe(pname)
        return (PERSONAS_DIR / folder / f"{_safe(pname)}.json").exists()
    if content_type == "chats":
        d = CHATS_DIR / key
        return d.exists() and any(d.iterdir())
    return False

def _dir_size_str(path: Path) -> str:
    try:
        total = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
        for unit in ("B", "KB", "MB", "GB"):
            if total < 1024:
                return f"{total:.0f} {unit}"
            total /= 1024
        return f"{total:.1f} TB"
    except Exception:
        return "?"

# ─── chat helpers ─────────────────────────────────────────────────────────────

def _chat_name(s: dict) -> str:
    for f in ("character_name", "title"):
        if s.get(f): return s[f]
    if s.get("name"): return s["name"]
    for nk in ("characters", "node", "character", "char"):
        n = s.get(nk)
        if isinstance(n, dict):
            for f in ("name", "full_name", "displayName", "title", "project_name"):
                if n.get(f): return n[f]
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
    for f in ("primary_image_path", "primary_image_url", "avatar_url", "avatar", "image_url"):
        v = s.get(f)
        if isinstance(v, str) and v.startswith("http"):
            return v
    for nk in ("characters", "node", "character", "char"):
        n = s.get(nk)
        if isinstance(n, dict):
            av = n.get("avatar_url") or n.get("avatar")
            if isinstance(av, str) and av.startswith("http"):
                return av
            fp = n.get("fullPath") or n.get("full_path")
            if fp:
                return f"{CDN_BASE}/{fp}/chara_card_v2.png"
    return None

# ─── Tile ─────────────────────────────────────────────────────────────────────

class Tile(QFrame):
    clicked = pyqtSignal()

    def __init__(self, name: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("tile")
        self.setStyleSheet(_TILE_NORMAL)
        self.setFixedWidth(TW + 20)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 8, 6, 6)
        layout.setSpacing(2)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)

        self._img_lbl = QLabel()
        self._img_lbl.setFixedSize(TW, TH)
        self._img_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._img_lbl.setPixmap(_placeholder())
        layout.addWidget(self._img_lbl, alignment=Qt.AlignmentFlag.AlignHCenter)

        short = (name[:15] + "…") if len(name) > 16 else name
        name_lbl = QLabel(short)
        name_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        name_lbl.setWordWrap(True)
        name_lbl.setStyleSheet(f"color: {TEXT}; font-size: 11pt;")
        layout.addWidget(name_lbl)

        self._badge = QLabel("Queued")
        self._badge.setObjectName("badge")
        self._badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._badge)

        self._chk = QCheckBox()
        self._chk.setChecked(True)
        layout.addWidget(self._chk, alignment=Qt.AlignmentFlag.AlignHCenter)

        self.setToolTip(name)

    def set_status(self, key: str):
        self._badge.setText(STATUS_LABEL.get(key, key))
        self._badge.setStyleSheet(
            f"background-color: {SURFACE}; color: {STATUS_FG.get(key, MUTED)}; "
            f"border-radius: 5px; padding: 2px 8px; font-size: 9pt;"
        )

    def set_badge_text(self, text: str):
        self._badge.setText(text)

    def set_image(self, raw: bytes):
        try:
            pil = Image.open(io.BytesIO(raw))
            pil.thumbnail((TW, TH), Image.LANCZOS)
            cr, cg, cb = _hex_to_rgb(CARD)
            bg = Image.new("RGB", (TW, TH), (cr, cg, cb))
            bg.paste(pil, ((TW - pil.width) // 2, (TH - pil.height) // 2))
            self._img_lbl.setPixmap(_pil_to_pixmap(bg))
        except Exception:
            pass

    def is_checked(self) -> bool:
        return self._chk.isChecked()

    def set_checked(self, val: bool):
        self._chk.setChecked(val)

    def enterEvent(self, event):
        self.setStyleSheet(_TILE_HOVER)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.setStyleSheet(_TILE_NORMAL)
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)

# ─── TileGrid ─────────────────────────────────────────────────────────────────

class TileGrid(QScrollArea):
    def __init__(self, kind: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.kind     = kind
        self._click_cb = None

        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        self._container = QWidget()
        self._container.setObjectName("tileContainer")
        self._layout = QGridLayout(self._container)
        self._layout.setContentsMargins(10, 10, 10, 10)
        self._layout.setHorizontalSpacing(8)
        self._layout.setVerticalSpacing(8)
        self._layout.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)
        self.setWidget(self._container)

        self._tiles:      dict[str, Tile]  = {}
        self._names:      dict[str, str]   = {}
        self._order:      list[str]        = []
        self._visible:    int              = 0
        self._cols:       int              = COLS
        self._col:        int              = 0
        self._row:        int              = 0
        self._bulk_state: bool             = True
        self._sort_keys:  dict[str, str]   = {}
        self._statuses:   dict[str, str]   = {}
        self._msg_counts: dict[str, int]   = {}
        self._force_keys: set[str]         = set()
        self._load_btn:   QPushButton | None = None

        self._reflow_timer = QTimer(self)
        self._reflow_timer.setSingleShot(True)
        self._reflow_timer.timeout.connect(self._reflow)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        sb_w = self.verticalScrollBar().width() if self.verticalScrollBar().isVisible() else 12
        slot_w = TW + 32
        new_cols = max(1, (event.size().width() - sb_w - 12) // slot_w)
        if new_cols != self._cols:
            self._cols = new_cols
            self._reflow_timer.start(120)

    def _reflow(self):
        self._container.setUpdatesEnabled(False)
        try:
            while self._layout.count():
                self._layout.takeAt(0)
            self._col = self._row = 0
            for key in self._order[:self._visible]:
                t = self._tiles[key]
                if t.isVisible():
                    self._layout.addWidget(t, self._row, self._col)
                    self._col += 1
                    if self._col >= self._cols:
                        self._col = 0; self._row += 1
            self._sync_load_btn()
        finally:
            self._container.setUpdatesEnabled(True)

    def _place(self, t: Tile):
        self._layout.addWidget(t, self._row, self._col)
        self._col += 1
        if self._col >= self._cols:
            self._col = 0; self._row += 1

    def _sync_load_btn(self):
        remaining = len(self._order) - self._visible
        if remaining <= 0:
            if self._load_btn:
                self._layout.removeWidget(self._load_btn)
                self._load_btn.hide()
            return
        if self._load_btn is None:
            self._load_btn = QPushButton()
            self._load_btn.setObjectName("loadMoreBtn")
            self._load_btn.clicked.connect(self._load_more)
            self._load_btn.setParent(self._container)
        n = min(PAGE, remaining)
        self._load_btn.setText(f"Load {n} more  ({remaining} remaining)")
        self._load_btn.show()
        btn_row = self._row + (1 if self._col > 0 else 0)
        self._layout.addWidget(self._load_btn, btn_row, 0, 1, max(self._cols, 1))

    def _load_more(self):
        if self._load_btn:
            self._layout.removeWidget(self._load_btn)
            self._load_btn.hide()
        start = self._visible
        end   = min(start + PAGE, len(self._order))
        self._container.setUpdatesEnabled(False)
        try:
            for i in range(start, end):
                key = self._order[i]
                t   = self._tiles[key]
                t.show()
                self._place(t)
            self._visible = end
            self._sync_load_btn()
        finally:
            self._container.setUpdatesEnabled(True)

    def add(self, key: str, name: str):
        if key in self._tiles:
            return
        t = Tile(name, self._container)
        t.set_checked(self._bulk_state)

        # Right-click context menu
        t.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        def _ctx(pos, _k=key, _t=t):
            menu = QMenu(_t)
            if _k in self._force_keys:
                act = menu.addAction("✓ Force re-download (on — click to disable)")
                act.triggered.connect(lambda: self._force_keys.discard(_k))
            else:
                act = menu.addAction("↺ Force re-download this item")
                act.triggered.connect(lambda: self._force_keys.add(_k))
            menu.exec(_t.mapToGlobal(pos))
        t.customContextMenuRequested.connect(_ctx)

        if self.kind == "chats" and self._click_cb is not None:
            t.clicked.connect(lambda _k=key: self._click_cb(_k))

        self._tiles[key] = t
        self._names[key] = name.lower()
        self._order.append(key)

        if self._visible < PAGE:
            self._place(t)
            self._visible += 1
        else:
            t.hide()
            self._sync_load_btn()

    def set_status(self, key: str, s: str):
        self._statuses[key] = s
        if t := self._tiles.get(key):
            t.set_status(s)

    def set_image(self, key: str, raw: bytes):
        if t := self._tiles.get(key):
            t.set_image(raw)

    def set_badge(self, key: str, text: str):
        if t := self._tiles.get(key):
            t.set_badge_text(text)

    def set_sort_key(self, key: str, date_str: str):
        self._sort_keys[key] = date_str

    def set_msg_count(self, key: str, count: int):
        self._msg_counts[key] = count

    def count(self) -> int:
        return len(self._tiles)

    def select_all(self, val: bool = True):
        self._bulk_state = val
        for t in self._tiles.values():
            t.set_checked(val)

    def get_checked(self) -> set[str]:
        rendered  = set(self._order[:self._visible])
        checked   = {k for k in rendered if self._tiles[k].is_checked()}
        if self._bulk_state:
            checked |= set(self._order[self._visible:])
        return checked

    def filter(self, query: str):
        q = query.strip().lower()
        if q and self._visible < len(self._order):
            self._container.setUpdatesEnabled(False)
            try:
                for i in range(self._visible, len(self._order)):
                    t = self._tiles[self._order[i]]
                    t.show()
                    self._place(t)
                self._visible = len(self._order)
            finally:
                self._container.setUpdatesEnabled(True)
        if not q:
            for t in self._tiles.values():
                t.show()
            self._reflow()
            return
        while self._layout.count():
            self._layout.takeAt(0)
        self._col = self._row = 0
        for key in self._order:
            t = self._tiles[key]
            if q in self._names.get(key, ""):
                t.show()
                self._layout.addWidget(t, self._row, self._col)
                self._col += 1
                if self._col >= self._cols:
                    self._col = 0; self._row += 1
            else:
                t.hide()
        if self._load_btn:
            self._layout.removeWidget(self._load_btn)
            self._load_btn.hide()

    def filter_by_status(self, allowed: set | None):
        if self._visible < len(self._order):
            self._container.setUpdatesEnabled(False)
            try:
                for i in range(self._visible, len(self._order)):
                    t = self._tiles[self._order[i]]
                    t.show()
                    self._place(t)
                self._visible = len(self._order)
            finally:
                self._container.setUpdatesEnabled(True)
        for key in self._order:
            t  = self._tiles[key]
            st = self._statuses.get(key, "queued")
            if allowed is None or st in allowed:
                t.show()
            else:
                t.hide()
        self._reflow()

    def sort_by_date(self, newest_first: bool = True):
        self._order.sort(key=lambda k: self._sort_keys.get(k, ""), reverse=newest_first)
        self._force_render_all()
        self._reflow()

    def sort_by_msgs(self, descending: bool = True):
        self._order.sort(key=lambda k: self._msg_counts.get(k, 0), reverse=descending)
        self._force_render_all()
        self._reflow()

    def _force_render_all(self):
        if self._visible < len(self._order):
            self._container.setUpdatesEnabled(False)
            try:
                for i in range(self._visible, len(self._order)):
                    t = self._tiles[self._order[i]]
                    t.show()
                self._visible = len(self._order)
            finally:
                self._container.setUpdatesEnabled(True)

    def clear(self):
        self._container.setUpdatesEnabled(False)
        try:
            while self._layout.count():
                self._layout.takeAt(0)
            for t in self._tiles.values():
                t.deleteLater()
            self._tiles.clear()
            self._names.clear()
            self._order.clear()
            self._visible    = 0
            self._col        = 0
            self._row        = 0
            self._bulk_state = True
            self._sort_keys.clear()
            self._statuses.clear()
            self._msg_counts.clear()
            self._force_keys.clear()
            if self._load_btn:
                self._load_btn.hide()
        finally:
            self._container.setUpdatesEnabled(True)

# ─── Dropdown button (QPushButton + QMenu, visible ▾) ─────────────────────────

class _DropBtn(QPushButton):
    currentTextChanged = pyqtSignal(str)

    def __init__(self, items: list[str], parent=None):
        super().__init__(parent)
        self._items   = list(items)
        self._current = items[0] if items else ""
        self.setObjectName("dropBtn")
        self._update_text()
        self.clicked.connect(self._open_menu)

    def _update_text(self):
        self.setText(f"{self._current}  ▾")

    def _open_menu(self):
        menu = QMenu(self)
        for item in self._items:
            act = menu.addAction(item)
            act.setCheckable(True)
            act.setChecked(item == self._current)
            act.triggered.connect(lambda _c, i=item: self._select(i))
        menu.exec(self.mapToGlobal(QPoint(0, self.height())))

    def _select(self, item: str):
        if item != self._current:
            self._current = item
            self._update_text()
            self.currentTextChanged.emit(item)

    def currentText(self) -> str:
        return self._current

    def setCurrentText(self, text: str):
        if text in self._items and text != self._current:
            self._current = text
            self._update_text()

    def setCurrentIndex(self, idx: int):
        if 0 <= idx < len(self._items):
            self.setCurrentText(self._items[idx])

# ─── Main application ─────────────────────────────────────────────────────────

class App(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Chub Ripper")
        self.resize(1100, 780)
        self.setMinimumSize(820, 560)

        self._q                     = queue.Queue()
        self._download_ready        = threading.Event()
        self._cancel_event          = threading.Event()
        self._cancel_download_event = threading.Event()
        self._tok_snapshot          = _token_load()
        self._total                 = 0
        self._done                  = 0
        self._worker_thread:   threading.Thread | None = None
        self._download_thread: threading.Thread | None = None
        self._fetch_sess            = None
        self._fetched_cards:    list[dict] = []
        self._fetched_lore:     list[dict] = []
        self._fetched_presets:  list[dict] = []
        self._fetched_personas: list[dict] = []
        self._fetched_sessions: list[dict] = []
        self._fetched_char_groups: dict    = {}
        self._active_tab            = "cards"
        self._force_redownload      = False
        self._sort_newest           = True
        self._sort_msgs_desc        = True

        self._grids:      dict[str, TileGrid]    = {}
        self._tab_btns:   dict[str, QPushButton] = {}
        self._tab_counts: dict[str, int]         = {}

        self._build()

        self._pump_timer = QTimer(self)
        self._pump_timer.timeout.connect(self._pump)
        self._pump_timer.start(50)

    # ── build ─────────────────────────────────────────────────────────────────

    def _build(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── header (title + subtitle) ─────────────────────────────────────────
        hdr = QWidget()
        hdr.setObjectName("header")
        hdr_l = QVBoxLayout(hdr)
        hdr_l.setContentsMargins(24, 18, 24, 10)
        hdr_l.setSpacing(2)

        title = QLabel("Chub Ripper")
        title.setObjectName("appTitle")
        hdr_l.addWidget(title)

        sub = QLabel("Paste your Ch-Api-Key and click Fetch.")
        sub.setObjectName("appSubtitle")
        hdr_l.addWidget(sub)
        # The subtitle doubles as the live status line — keeps the top tight.
        self._status_lbl = sub
        root.addWidget(hdr)

        # ── settings bar (auth + include + format) ───────────────────────────
        settings = QWidget()
        settings.setObjectName("settingsBar")
        s_l = QVBoxLayout(settings)
        s_l.setContentsMargins(24, 6, 24, 6)
        s_l.setSpacing(10)

        # row 1: API key + Fetch (one focused row)
        auth_row = QHBoxLayout()
        auth_row.setSpacing(10)

        auth_lbl = QLabel("API KEY")
        auth_lbl.setObjectName("sectionLabel")
        auth_lbl.setFixedWidth(76)
        auth_row.addWidget(auth_lbl)

        self._token_entry = QLineEdit(self._tok_snapshot)
        self._token_entry.setObjectName("tokenEntry")
        self._token_entry.setEchoMode(QLineEdit.EchoMode.Password)
        self._token_entry.setPlaceholderText(
            "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx  ·  "
            "DevTools → Network → ro.chub.ai → Headers → Ch-Api-Key")
        self._token_entry.setMinimumHeight(36)
        auth_row.addWidget(self._token_entry, 1)

        self._show_btn = QPushButton("Show")
        self._show_btn.setFixedSize(64, 36)
        self._show_btn.clicked.connect(self._toggle_token_visibility)
        auth_row.addWidget(self._show_btn)

        self._fetch_btn = QPushButton("Fetch")
        self._fetch_btn.setObjectName("fetchBtn")
        self._fetch_btn.setFixedSize(110, 36)
        self._fetch_btn.clicked.connect(self._start)
        auth_row.addWidget(self._fetch_btn)
        s_l.addLayout(auth_row)

        # row 2: Include + Format on the same line, separated
        opts_row = QHBoxLayout()
        opts_row.setSpacing(10)

        inc_lbl = QLabel("INCLUDE")
        inc_lbl.setObjectName("sectionLabel")
        inc_lbl.setFixedWidth(76)
        opts_row.addWidget(inc_lbl)

        self._chk_cards     = QCheckBox("Cards");     self._chk_cards.setChecked(True)
        self._chk_lorebooks = QCheckBox("Lorebooks"); self._chk_lorebooks.setChecked(True)
        self._chk_presets   = QCheckBox("Presets");   self._chk_presets.setChecked(True)
        self._chk_personas  = QCheckBox("Personas");  self._chk_personas.setChecked(True)
        self._chk_chats     = QCheckBox("Chats");     self._chk_chats.setChecked(True)
        for chk in (self._chk_cards, self._chk_lorebooks, self._chk_presets,
                    self._chk_personas, self._chk_chats):
            opts_row.addWidget(chk)
            opts_row.addSpacing(6)

        opts_row.addStretch()

        fmt_lbl = QLabel("FORMAT")
        fmt_lbl.setObjectName("sectionLabel")
        opts_row.addWidget(fmt_lbl)
        opts_row.addSpacing(6)

        card_lbl = QLabel("Card")
        card_lbl.setObjectName("fmtLabel")
        opts_row.addWidget(card_lbl)
        self._card_fmt_cb = _DropBtn(["PNG", "JSON"])
        self._card_fmt_cb.setFixedSize(86, 32)
        opts_row.addWidget(self._card_fmt_cb)
        opts_row.addSpacing(10)

        chat_lbl = QLabel("Chat")
        chat_lbl.setObjectName("fmtLabel")
        opts_row.addWidget(chat_lbl)
        self._chat_fmt_cb = _DropBtn(["JSONL", "JSON", "TXT"])
        self._chat_fmt_cb.setFixedSize(96, 32)
        opts_row.addWidget(self._chat_fmt_cb)
        opts_row.addSpacing(10)

        self._chk_merge_chats = QCheckBox("Merge branches")
        self._chk_merge_chats.setToolTip(
            "Save each chat as ONE file instead of one file per branch.\n"
            "Alternate branches are folded in as SillyTavern-style swipes\n"
            "on the message where they diverge."
        )
        opts_row.addWidget(self._chk_merge_chats)

        s_l.addLayout(opts_row)
        root.addWidget(settings)

        # ── tab bar + stacked content ──────────────────────────────────────────
        tab_area = QWidget()
        tab_area_l = QVBoxLayout(tab_area)
        tab_area_l.setContentsMargins(0, 0, 0, 0)
        tab_area_l.setSpacing(0)

        tab_bar = QWidget()
        tab_bar_l = QHBoxLayout(tab_bar)
        tab_bar_l.setContentsMargins(16, 6, 16, 4)
        tab_bar_l.setSpacing(0)
        tab_bar_l.addStretch(1)  # center the tab group

        _dir_map = {"cards": CARDS_DIR, "lorebooks": LORE_DIR,
                    "presets": PRESETS_DIR, "personas": PERSONAS_DIR, "chats": CHATS_DIR}

        self._stack = QStackedWidget()

        for idx, name in enumerate(("Cards", "Lorebooks", "Presets", "Personas", "Chats")):
            key = name.lower()

            # Thin vertical divider between tab pairs.
            if idx > 0:
                sep = QFrame()
                sep.setFrameShape(QFrame.Shape.VLine)
                sep.setFixedSize(1, 20)
                sep.setStyleSheet(f"background-color: {BORDER}; border: none;")
                tab_bar_l.addSpacing(10)
                tab_bar_l.addWidget(sep, alignment=Qt.AlignmentFlag.AlignVCenter)
                tab_bar_l.addSpacing(10)

            # Pair button + folder icon in a tight horizontal group so they
            # read as one unit instead of two floating elements.
            pair = QWidget()
            pair_l = QHBoxLayout(pair)
            pair_l.setContentsMargins(0, 0, 0, 0)
            pair_l.setSpacing(0)

            btn = QPushButton(name)
            btn.setObjectName("tabBtn")
            btn.setProperty("active", False)
            btn.setMinimumHeight(36)
            btn.clicked.connect(lambda _c, k=key: self._show_tab(k))
            pair_l.addWidget(btn)
            self._tab_btns[key] = btn

            _fdir = _dir_map[key]
            fb = QPushButton("📁")
            fb.setObjectName("folderBtn")
            fb.setFixedSize(28, 36)
            fb.setToolTip(f"Open {name} folder\n{_dir_size_str(_fdir)}")
            fb.clicked.connect(lambda _c, k=key: self._open_tab_folder(k))
            pair_l.addWidget(fb)

            tab_bar_l.addWidget(pair)

            grid = TileGrid(kind=key)
            self._grids[key] = grid
            self._stack.addWidget(grid)

        self._grids["chats"]._click_cb = self._preview_chat
        tab_bar_l.addStretch(1)  # symmetric stretch for centered tabs

        # ── divider between tab row and toolbar (visual grouping) ────────────
        tab_div_top = QFrame()
        tab_div_top.setObjectName("divider")
        tab_div_top.setFixedHeight(1)

        # ── toolbar (its own centered row, sits flush under the tab row) ─────
        # All items share the same width and gap so the cadence is even.
        TOOL_W, TOOL_H, TOOL_GAP = 96, 28, 8

        tool_bar = QWidget()
        tool_bar.setObjectName("toolBar")
        tool_bar_l = QHBoxLayout(tool_bar)
        tool_bar_l.setContentsMargins(16, 6, 16, 6)
        tool_bar_l.setSpacing(TOOL_GAP)
        tool_bar_l.addStretch(1)

        self._search_entry = QLineEdit()
        self._search_entry.setObjectName("searchEntry")
        self._search_entry.setPlaceholderText("Search…")
        self._search_entry.setFixedSize(TOOL_W, TOOL_H)
        self._search_entry.textChanged.connect(self._on_search)
        tool_bar_l.addWidget(self._search_entry)

        self._filter_cb = _DropBtn(["All", "New", "Already saved", "Done", "Failed", "Unavailable"])
        self._filter_cb.setFixedSize(TOOL_W, TOOL_H)
        self._filter_cb.currentTextChanged.connect(self._on_status_filter)
        tool_bar_l.addWidget(self._filter_cb)

        all_btn = QPushButton("✓ All")
        all_btn.setFixedSize(TOOL_W, TOOL_H)
        all_btn.clicked.connect(lambda: self._grids[self._active_tab].select_all(True))
        tool_bar_l.addWidget(all_btn)

        none_btn = QPushButton("✗ None")
        none_btn.setFixedSize(TOOL_W, TOOL_H)
        none_btn.clicked.connect(lambda: self._grids[self._active_tab].select_all(False))
        tool_bar_l.addWidget(none_btn)

        self._force_btn = QPushButton("↺ Overwrite")
        self._force_btn.setFixedSize(TOOL_W, TOOL_H)
        self._force_btn.setToolTip("Re-download items that are already saved")
        self._force_btn.clicked.connect(self._toggle_force_redownload)
        tool_bar_l.addWidget(self._force_btn)

        self._sort_btn = QPushButton("↓ Date")
        self._sort_btn.setFixedSize(TOOL_W, TOOL_H)
        self._sort_btn.clicked.connect(self._toggle_chat_sort)
        tool_bar_l.addWidget(self._sort_btn)

        self._sort_msgs_btn = QPushButton("↓ Msgs")
        self._sort_msgs_btn.setFixedSize(TOOL_W, TOOL_H)
        self._sort_msgs_btn.clicked.connect(self._toggle_chat_sort_msgs)
        tool_bar_l.addWidget(self._sort_msgs_btn)

        tool_bar_l.addStretch(1)

        # _show_tab() expects this; no-op so chat-only buttons can share visibility.
        self._sort_sep = QWidget()
        self._sort_sep.setFixedSize(0, 0)

        # divider below the toolbar row (separates chrome from content)
        tab_div = QFrame()
        tab_div.setObjectName("divider")
        tab_div.setFixedHeight(1)

        tab_area_l.addWidget(tab_bar)
        tab_area_l.addWidget(tab_div_top)
        tab_area_l.addWidget(tool_bar)
        tab_area_l.addWidget(tab_div)
        tab_area_l.addWidget(self._stack, 1)
        root.addWidget(tab_area, 1)

        # ── footer (progress + actions; status lives up top) ──────────────────
        foot = QWidget()
        foot.setObjectName("footerBar")
        foot.setFixedHeight(56)
        foot_l = QHBoxLayout(foot)
        foot_l.setContentsMargins(20, 0, 20, 0)
        foot_l.setSpacing(14)

        # Progress bar + counter (stacked, left side, expands to fill)
        prog_wrap = QWidget()
        prog_wrap_l = QVBoxLayout(prog_wrap)
        prog_wrap_l.setContentsMargins(0, 0, 0, 0)
        prog_wrap_l.setSpacing(4)
        prog_wrap.setMinimumWidth(240)

        self._prog_lbl = QLabel("")
        self._prog_lbl.setObjectName("progLbl")
        self._prog_lbl.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        prog_wrap_l.addWidget(self._prog_lbl)

        self._bar = QProgressBar()
        self._bar.setRange(0, 1000)
        self._bar.setValue(0)
        self._bar.setTextVisible(False)
        prog_wrap_l.addWidget(self._bar)
        foot_l.addWidget(prog_wrap, 1)

        self._dl_btn = QPushButton("⬇  Download All")
        self._dl_btn.setObjectName("dlBtn")
        self._dl_btn.setFixedSize(154, 36)
        self._dl_btn.clicked.connect(self._start_download)
        self._dl_btn.hide()
        foot_l.addWidget(self._dl_btn)

        self._retry_btn = QPushButton("↺ Retry Failed")
        self._retry_btn.setObjectName("retryBtn")
        self._retry_btn.setFixedSize(128, 36)
        self._retry_btn.clicked.connect(self._start_retry)
        self._retry_btn.hide()
        foot_l.addWidget(self._retry_btn)

        root.addWidget(foot)

        cfg = _config_load()
        self._show_tab(cfg.get("last_tab", "cards"))

    # ── tab / UI helpers ──────────────────────────────────────────────────────

    def _set_tab_label(self, key: str, count: int | None = None):
        if count is not None:
            self._tab_counts[key] = count
        n = self._tab_counts.get(key, 0)
        name = key.capitalize()
        if n:
            # Build with rich text so the count looks like a secondary chip.
            self._tab_btns[key].setText(f"{name}  {n}")
        else:
            self._tab_btns[key].setText(name)

    def _show_tab(self, key: str):
        self._active_tab = key
        for k, btn in self._tab_btns.items():
            btn.setProperty("active", k == key)
            # force re-polish so the [active="true"] selector takes effect
            btn.style().unpolish(btn)
            btn.style().polish(btn)
        self._stack.setCurrentWidget(self._grids[key])
        self._search_entry.blockSignals(True)
        self._search_entry.clear()
        self._search_entry.blockSignals(False)
        self._filter_cb.blockSignals(True)
        self._filter_cb.setCurrentIndex(0)
        self._filter_cb.blockSignals(False)
        _config_save({"last_tab": key})
        is_chats = (key == "chats")
        self._sort_sep.setVisible(is_chats)
        self._sort_btn.setVisible(is_chats)
        self._sort_msgs_btn.setVisible(is_chats)

    def _refresh_tab(self, key: str):
        self._set_tab_label(key, self._grids[key].count())

    def _toggle_token_visibility(self):
        hidden = self._token_entry.echoMode() == QLineEdit.EchoMode.Password
        self._token_entry.setEchoMode(
            QLineEdit.EchoMode.Normal if hidden else QLineEdit.EchoMode.Password)
        self._show_btn.setText("Hide" if hidden else "Show")

    def _toggle_chat_sort(self):
        self._sort_newest = not self._sort_newest
        self._grids["chats"].sort_by_date(newest_first=self._sort_newest)
        self._sort_btn.setText("↓ Date" if self._sort_newest else "↑ Date")

    def _toggle_chat_sort_msgs(self):
        self._sort_msgs_desc = not self._sort_msgs_desc
        self._grids["chats"].sort_by_msgs(descending=self._sort_msgs_desc)
        self._sort_msgs_btn.setText("↓ Msgs" if self._sort_msgs_desc else "↑ Msgs")

    def _on_status_filter(self, choice: str):
        STATUS_MAP = {
            "All":           None,
            "New":           {"queued"},
            "Already saved": {"skipped"},
            "Done":          {"done"},
            "Failed":        {"failed"},
            "Unavailable":   {"unavailable"},
        }
        self._grids[self._active_tab].filter_by_status(STATUS_MAP.get(choice))

    def _toggle_force_redownload(self):
        self._force_redownload = not self._force_redownload
        if self._force_redownload:
            self._force_btn.setStyleSheet(_FORCE_ON)
        else:
            self._force_btn.setStyleSheet("")

    def _open_tab_folder(self, key: str):
        import subprocess as _sp
        d = {"cards": CARDS_DIR, "lorebooks": LORE_DIR, "presets": PRESETS_DIR,
             "personas": PERSONAS_DIR, "chats": CHATS_DIR}.get(key)
        if not d:
            return
        d.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(d)
        elif sys.platform == "darwin":
            _sp.run(["open", str(d)])
        else:
            _sp.run(["xdg-open", str(d)])

    def _on_search(self, text: str):
        self._grids[self._active_tab].filter(text)

    def _reset_grids(self):
        for key, grid in self._grids.items():
            grid.clear()
            self._tab_counts[key] = 0
            self._set_tab_label(key, 0)
        self._dl_btn.hide()
        self._retry_btn.hide()
        self._bar.setValue(0)
        self._prog_lbl.setText("")
        self._total = 0
        self._done  = 0

    def _tick(self):
        if self._total:
            self._bar.setValue(int(self._done * 1000 / self._total))
            self._prog_lbl.setText(f"{self._done} / {self._total}")

    # ── actions ───────────────────────────────────────────────────────────────

    def _start(self):
        if self._worker_thread and self._worker_thread.is_alive():
            self._cancel_event.set()
            self._download_ready.set()
            self._fetch_btn.setEnabled(False)
            self._fetch_btn.setText("Cancelling…")
            return

        tok = self._token_entry.text().strip()
        if tok.lower().startswith("bearer "):
            tok = tok[7:].strip()
            self._token_entry.setText(tok)
        if not tok:
            self._status_lbl.setText("Paste your Bearer token first.")
            return

        self._tok_snapshot = tok
        self._cancel_event.clear()
        self._download_ready.clear()
        self._reset_grids()
        self._fetch_btn.setText("✕ Cancel")
        self._status_lbl.setText("Fetching…")

        self._fetch_cards     = self._chk_cards.isChecked()
        self._fetch_lorebooks = self._chk_lorebooks.isChecked()
        self._fetch_presets   = self._chk_presets.isChecked()
        self._fetch_personas  = self._chk_personas.isChecked()
        self._fetch_chats     = self._chk_chats.isChecked()
        self._card_fmt        = self._card_fmt_cb.currentText()
        self._chat_fmt        = self._chat_fmt_cb.currentText()
        self._merge_chats     = self._chk_merge_chats.isChecked()

        self._worker_thread = threading.Thread(target=self._worker, daemon=True)
        self._worker_thread.start()

    def _start_download(self):
        if getattr(self, "_download_thread", None) and self._download_thread.is_alive():
            self._cancel_download_event.set()
            self._dl_btn.setEnabled(False)
            self._dl_btn.setText("Cancelling…")
            return

        self._cancel_download_event.clear()
        self._dl_btn.setEnabled(True)
        self._dl_btn.setText("✕ Cancel Download")
        self._status_lbl.setText("Starting downloads…")

        # Re-read format/merge toggles now, in case the user changed them
        # after Fetch but before clicking Download.
        self._card_fmt    = self._card_fmt_cb.currentText()
        self._chat_fmt    = self._chat_fmt_cb.currentText()
        self._merge_chats = self._chk_merge_chats.isChecked()

        self._checked_snapshot: dict[str, set[str]] = {
            tab: grid.get_checked() for tab, grid in self._grids.items()
        }
        if self._worker_thread and self._worker_thread.is_alive():
            self._download_ready.set()
        elif self._fetch_sess is not None:
            self._download_thread = threading.Thread(target=self._download_phase, daemon=True)
            self._download_thread.start()

    def _start_retry(self):
        fs = getattr(self, "_failed_snapshot", {})
        if not any(fs.values()):
            return
        self._checked_snapshot = {k: set(v) for k, v in fs.items()}
        self._failed_snapshot  = {}
        self._retry_btn.hide()
        self._start_download()

    def _preview_chat(self, fkey: str):
        chat_dir = CHATS_DIR / fkey
        if not chat_dir.exists():
            return
        files = sorted(f for f in chat_dir.iterdir() if f.is_file())
        if not files:
            return

        dlg = QDialog(self)
        dlg.setWindowTitle(fkey.replace("_", " "))
        dlg.resize(620, 520)

        dlg_l = QVBoxLayout(dlg)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        container = QWidget()
        con_l = QVBoxLayout(container)
        con_l.setAlignment(Qt.AlignmentFlag.AlignTop)
        scroll.setWidget(container)
        dlg_l.addWidget(scroll)

        hdr_lbl = QLabel(f"Preview: {files[0].name}")
        hdr_lbl.setStyleSheet(f"color: {MUTED};")
        con_l.addWidget(hdr_lbl)

        try:
            f = files[0]
            msgs = []
            if f.suffix == ".jsonl":
                msgs = [json.loads(l) for l in f.read_text(encoding="utf-8").splitlines()
                        if l.strip()][:20]
            elif f.suffix == ".json":
                data = json.loads(f.read_text(encoding="utf-8"))
                msgs = data[:20] if isinstance(data, list) else []
            elif f.suffix == ".txt":
                lbl = QLabel(f.read_text(encoding="utf-8")[:3000])
                lbl.setWordWrap(True)
                con_l.addWidget(lbl)
            for m in msgs:
                role    = m.get("role", "?")
                content = str(m.get("message") or m.get("content") or "")[:400]
                speaker = "User" if role in ("user", "human") else "AI"
                col     = TEXT if speaker == "AI" else MUTED
                lbl = QLabel(f"[{speaker}]  {content}")
                lbl.setWordWrap(True)
                lbl.setStyleSheet(f"color: {col};")
                con_l.addWidget(lbl)
        except Exception as e:
            err = QLabel(f"Could not read: {e}")
            err.setStyleSheet(f"color: {RED};")
            con_l.addWidget(err)

        dlg.exec()

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
                        break
                elif kind == "IMAGE":
                    images += 1
                    if images >= 4:
                        break
        except queue.Empty:
            pass

    def _handle(self, msg: tuple):
        kind = msg[0]
        if kind == "LOG":
            self._status_lbl.setText(msg[1])
        elif kind == "ADD":
            _, tab, key, name = msg
            self._grids[tab].add(key, name)
            self._refresh_tab(tab)
        elif kind == "STATUS":
            _, tab, key, s = msg
            self._grids[tab].set_status(key, s)
            if s in ("done", "skipped", "failed", "unavailable"):
                self._done += 1
                self._tick()
        elif kind == "IMAGE":
            _, tab, key, raw = msg
            self._grids[tab].set_image(key, raw)
        elif kind == "SORT_KEY":
            _, tab, key, date_str = msg
            self._grids[tab].set_sort_key(key, date_str)
        elif kind == "BADGE":
            _, tab, key, text = msg
            self._grids[tab].set_badge(key, text)
        elif kind == "MSG_COUNT":
            _, tab, key, count = msg
            self._grids[tab].set_msg_count(key, count)
        elif kind == "SHOW_RETRY_BTN":
            self._retry_btn.show()
        elif kind == "SHOW_DL_BTN":
            total, n_sessions, n_chars = msg[1], msg[2], msg[3]
            self._total = total
            self._bar.setValue(0)
            self._prog_lbl.setText(f"0 / {total}")
            self._dl_btn.show()
            self._fetch_btn.setEnabled(True)
            self._fetch_btn.setText("Fetch")
            if n_sessions:
                self._tab_counts["chats"] = n_sessions
                self._tab_btns["chats"].setText(f"Chats  {n_sessions}  ({n_chars} chars)")
        elif kind == "CANCELLED":
            self._status_lbl.setText("Fetch cancelled.")
            self._fetch_btn.setEnabled(True)
            self._fetch_btn.setText("Fetch")
        elif kind == "DOWNLOAD_CANCELLED":
            self._status_lbl.setText("Download cancelled.")
            self._dl_btn.setEnabled(True)
            self._dl_btn.setText("⬇  Download All")
        elif kind == "DONE":
            self._status_lbl.setText(msg[1])
            self._fetch_btn.setEnabled(True)
            self._fetch_btn.setText("Fetch")
            self._dl_btn.setEnabled(True)
            self._dl_btn.setText("⬇  Download All")
            self._bar.setValue(1000)
        elif kind == "ERROR":
            self._status_lbl.setText(f"Error: {msg[1][:120]}")
            self._fetch_btn.setEnabled(True)
            self._fetch_btn.setText("Fetch")
            self._dl_btn.setEnabled(True)
            self._dl_btn.setText("⬇  Download All")

    # ── worker (fetch phase) ──────────────────────────────────────────────────

    def _worker(self):
        import traceback as _tb
        import requests as _rq
        from playwright.sync_api import sync_playwright

        q = self._q
        _log_file = open(ROOT / "debug.log", "a", encoding="utf-8", buffering=1)
        _log_file.write(f"\n{'='*60}\n{time.strftime('%Y-%m-%d %H:%M:%S')}\n{'='*60}\n")
        _log_file.write(f"Python {sys.version}  |  platform: {sys.platform}\n")
        _log_file.write(
            f"fetch: cards={self._fetch_cards} lorebooks={self._fetch_lorebooks} "
            f"presets={self._fetch_presets} personas={self._fetch_personas} "
            f"chats={self._fetch_chats}  "
            f"card_fmt={self._card_fmt}  chat_fmt={self._chat_fmt}\n"
        )
        def log(m):   _log_file.write(m + "\n"); q.put(("LOG", m))
        def flog(m):  _log_file.write(m + "\n")
        def add(t, k, n):    q.put(("ADD",    t, k, n))
        def status(t, k, s): q.put(("STATUS", t, k, s))
        def image(t, k, b):  q.put(("IMAGE",  t, k, b))

        try:
            tok = self._tok_snapshot

            sess = _rq.Session()
            sess.headers.update({
                "Ch-Api-Key": tok,
                "Samwise":    tok,
                "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) "
                               "Chrome/148.0.0.0 Safari/537.36"),
                "Referer": "https://chub.ai/",
                "Origin":  "https://chub.ai",
            })
            _token_save(tok)

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
                                 pj.get("user_name") or pj.get("handle") or
                                 pj.get("name") or "")
                    flog(f"  /api/self keys: {list(pj.keys())[:10]}  handle={my_handle!r}")
            except Exception as e:
                log(f"Key probe error: {e} (continuing)")

            def _collect(nodes, tab, seen):
                for n in nodes:
                    fp = n.get("fullPath", "")
                    if fp and fp not in seen:
                        seen.add(fp)
                        add(tab, fp, n.get("name", fp))
                        try:
                            r = sess.get(
                                f"https://avatars.charhub.io/avatars/{fp}/avatar.webp",
                                timeout=10)
                            if r.ok:
                                image(tab, fp, r.content)
                        except Exception:
                            pass
                        yield n

            def _paginate_nodes(captured_url, first_body, tab, seen):
                from urllib.parse import urlparse, parse_qs, urlunparse
                def _pick_batch(b):
                    d = b.get("data", {}) if isinstance(b, dict) else {}
                    return (d.get("nodes") or d.get("lorebooks") or
                            d.get("characters") or b.get("nodes") or b.get("lorebooks") or [])
                nodes = []
                batch = _pick_batch(first_body)
                total_count = first_body.get("data", {}).get("count", "?")
                log(f"  {tab} page 1: {len(batch)} items  (api total={total_count})")
                for n in _collect(batch, tab, seen):
                    nodes.append(n)
                parsed = urlparse(captured_url)
                qs = {k: v[0] for k, v in parse_qs(parsed.query).items()}
                qs.pop("page", None); qs.pop("cursor", None)
                base = urlunparse(parsed._replace(query=""))
                body = first_body; pg = 2
                while True:
                    if self._cancel_event.is_set(): break
                    if not body.get("data", {}).get("cursor"): break
                    r = sess.get(base, params={**qs, "page": pg, "first": 48}, timeout=30)
                    log(f"  {tab} page {pg} → {r.status_code}")
                    if not r.ok: break
                    body = r.json()
                    batch = _pick_batch(body)
                    if not batch: break
                    prev = len(nodes)
                    for n in _collect(batch, tab, seen):
                        nodes.append(n)
                    if len(nodes) == prev: break
                    log(f"  {tab} page {pg}: +{len(batch)}  total: {len(nodes)}")
                    pg += 1; time.sleep(0.3)
                return nodes

            def _paginate_sessions(captured_url, first_body):
                from urllib.parse import urlparse, parse_qs, urlunparse
                def _extract(body):
                    return (body.get("chats") or body.get("results") or
                            body.get("sessions") or body.get("items") or
                            body.get("data") or (body if isinstance(body, list) else []))
                def _build_char_map(body):
                    cmap = {}
                    for key in ("characters", "nodes", "character_map", "character_data"):
                        val = body.get(key)
                        if isinstance(val, dict):
                            for k, v in val.items():
                                if isinstance(v, dict): cmap[str(k)] = v
                        elif isinstance(val, list):
                            for c in val:
                                if isinstance(c, dict):
                                    cid = c.get("id") or c.get("character_id")
                                    if cid: cmap[str(cid)] = c
                    return cmap
                def _enrich(s, cmap):
                    if not cmap: return s
                    cid = str(s.get("character_id") or "")
                    char = cmap.get(cid)
                    if not char: return s
                    s = dict(s)
                    if not s.get("character_name"):
                        for f in ("name", "full_name", "displayName", "title"):
                            if char.get(f): s["character_name"] = char[f]; break
                    if not s.get("primary_image_path"):
                        fp = char.get("fullPath") or char.get("full_path")
                        if fp: s["primary_image_path"] = fp
                    return s
                char_map = _build_char_map(first_body)
                items = _extract(first_body)
                log(f"  chats page 1: {len(items)} items")
                sessions = [_enrich(s, char_map) for s in items]
                parsed = urlparse(captured_url)
                qs = {k: v[0] for k, v in parse_qs(parsed.query).items()}
                qs.pop("cursor", None); qs.setdefault("include_character", "true")
                base = urlunparse(parsed._replace(query=""))
                cursor = first_body.get("cursor"); pg = 2
                while cursor:
                    r = sess.get(base, params={**qs, "cursor": cursor}, timeout=30)
                    log(f"  chats page {pg} → {r.status_code}")
                    if not r.ok: break
                    body = r.json()
                    page_char_map = _build_char_map(body)
                    items = _extract(body)
                    new_cursor = body.get("cursor")
                    if not items: break
                    sessions.extend(_enrich(s, page_char_map) for s in items)
                    log(f"  chats page {pg}: +{len(items)}  total: {len(sessions)}")
                    cursor = new_cursor; pg += 1; time.sleep(0.3)
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

                def _sniff_page(nav_url, domain_hint, label, wait_ms=6000):
                    candidates = []
                    def on_resp(resp):
                        try:
                            host = resp.url.split("//")[1].split("/")[0].split("?")[0]
                        except Exception:
                            return
                        if domain_hint not in host or resp.status != 200: return
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
                        log(f"  nothing captured for {label}"); return "", {}
                    def _score(body):
                        if isinstance(body, list): return len(body)
                        d = body.get("data", {}) if isinstance(body, dict) else {}
                        for key in ("results", "sessions", "items", "chats", "lorebooks"):
                            val = body.get(key)
                            if val: return len(val)
                        return len(d.get("nodes") or []) or d.get("count", 0)
                    best_url, best_body = max(candidates, key=lambda c: _score(c[1]))
                    log(f"  best: {best_url[:90]}")
                    return best_url, best_body

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

                seen_lore: set[str] = set()
                all_lore_nodes: list[dict] = []
                if self._fetch_lorebooks:
                    def _paginate_lore_direct(extra, label):
                        pg = 1; prev_cursor = None
                        while True:
                            if self._cancel_event.is_set(): break
                            params = {"first": 48, "namespace": "lorebooks",
                                      "nsfw": "true", "nsfl": "false", "chub": "true",
                                      "sort": "created_at", "asc": "false",
                                      "include_forks": "true", "count": "true",
                                      **extra, "page": pg}
                            try:
                                r = sess.get(f"{REPO_API}/search", params=params, timeout=30)
                                log(f"  → {r.url.split('?')[0]}  ({r.status_code})")
                                if not r.ok: break
                                body = r.json()
                                d = body.get("data", {}) if isinstance(body, dict) else {}
                                cursor = d.get("cursor")
                                batch = d.get("nodes") or d.get("lorebooks") or []
                                if not batch: break
                                prev_count = len(all_lore_nodes)
                                for n in _collect(batch, "lorebooks", seen_lore):
                                    all_lore_nodes.append(n)
                                if not cursor or cursor == prev_cursor or len(all_lore_nodes) == prev_count: break
                                prev_cursor = cursor; pg += 1; time.sleep(0.3)
                            except Exception as e:
                                log(f"  lorebooks {label} error: {e}"); break
                    _paginate_lore_direct({"my_favorites": "true"}, "my_favorites")
                    if my_handle:
                        _paginate_lore_direct({"username": my_handle, "only_mine": "all",
                                               "exclude_mine": "false", "include_forks": "true"}, "authored")
                log(f"Lorebooks: {len(all_lore_nodes)}")
                if self._cancel_event.is_set():
                    q.put(("CANCELLED",)); return

                seen_presets: set[str] = set()
                all_preset_nodes: list[dict] = []
                if self._fetch_presets:
                    def _paginate_preset_direct(extra, label):
                        pg = 1; prev_cursor = None
                        while True:
                            if self._cancel_event.is_set(): break
                            params = {"first": 48, "namespace": "presets",
                                      "nsfw": "true", "nsfl": "false", "chub": "true",
                                      "sort": "created_at", "asc": "false",
                                      "include_forks": "true", "count": "true",
                                      **extra, "page": pg}
                            try:
                                r = sess.get(f"{REPO_API}/search", params=params, timeout=30)
                                log(f"  → {r.url.split('?')[0]}  ({r.status_code})")
                                if not r.ok: break
                                body = r.json()
                                d = body.get("data", {}) if isinstance(body, dict) else {}
                                cursor = d.get("cursor")
                                batch = d.get("nodes") or d.get("presets") or []
                                if not batch: break
                                prev_count = len(all_preset_nodes)
                                for n in _collect(batch, "presets", seen_presets):
                                    all_preset_nodes.append(n)
                                if not cursor or cursor == prev_cursor or len(all_preset_nodes) == prev_count: break
                                prev_cursor = cursor; pg += 1; time.sleep(0.3)
                            except Exception as e:
                                log(f"  presets {label} error: {e}"); break
                    _paginate_preset_direct({"my_favorites": "true"}, "my_favorites")
                    if my_handle:
                        _paginate_preset_direct({"username": my_handle, "only_mine": "all",
                                                 "exclude_mine": "false", "include_forks": "true"}, "authored")
                log(f"Presets: {len(all_preset_nodes)}")
                if self._cancel_event.is_set():
                    q.put(("CANCELLED",)); return

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
                                for n in all_persona_nodes:
                                    pid = str(n.get("id") or n.get("name", ""))
                                    label = n.get("name") or n.get("id") or pid
                                    add("personas", pid, label)
                                if all_persona_nodes:
                                    import concurrent.futures as _cf2
                                    def _fetch_pa(n):
                                        pid = str(n.get("id") or n.get("name", ""))
                                        av = n.get("avatar") or n.get("avatar_url") or n.get("image_url")
                                        if not av: return
                                        try:
                                            r2 = sess.get(av, timeout=10)
                                            if r2.ok: image("personas", pid, r2.content)
                                        except Exception: pass
                                    threading.Thread(
                                        target=lambda: _cf2.ThreadPoolExecutor(max_workers=2).map(_fetch_pa, all_persona_nodes),
                                        daemon=True).start()
                    except Exception as e:
                        log(f"  personas error: {e}")
                log(f"Personas: {len(all_persona_nodes)}")
                if self._cancel_event.is_set():
                    q.put(("CANCELLED",)); return

                all_sessions: list[dict] = []
                if self._fetch_chats:
                    url, body = _sniff_page("https://chub.ai/my_chats", "chub.ai", "my_chats", wait_ms=10000)
                    all_sessions = _paginate_sessions(url, body) if url else []

                char_groups: dict[str, list] = {}
                char_names:  dict[str, str]  = {}
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
                    group_date = max(
                        (s.get("updated_at") or s.get("created_at") or s.get("last_message_at") or ""
                         for s in group), default="")
                    if group_date:
                        q.put(("SORT_KEY", "chats", fkey, str(group_date)))
                log(f"Chats: {len(all_sessions)} across {len(char_groups)} character(s)")

                if char_groups:
                    import concurrent.futures as _cf3
                    def _fetch_av(item):
                        fk, grp = item
                        av_url = _chat_avatar_url(grp[0])
                        if not av_url: return
                        try:
                            r = sess.get(av_url, timeout=10)
                            if r.ok: image("chats", fk, r.content)
                        except Exception: pass
                    threading.Thread(
                        target=lambda: _cf3.ThreadPoolExecutor(max_workers=2).map(_fetch_av, char_groups.items()),
                        daemon=True).start()

                browser.close()

            total = (len(all_card_nodes) + len(all_lore_nodes) + len(all_preset_nodes) +
                     len(all_persona_nodes) + len(char_groups))
            q.put(("SHOW_DL_BTN", total, len(all_sessions), len(char_groups)))
            log(f"Found {len(all_card_nodes)} cards · {len(all_lore_nodes)} lorebooks · "
                f"{len(all_preset_nodes)} presets · {len(all_persona_nodes)} personas · "
                f"{len(all_sessions)} chats ({len(char_groups)} characters).  Click ⬇ Download All when ready.")

            _cf_fmt = self._card_fmt
            for _n in all_card_nodes:
                _fp = _n.get("fullPath", "")
                if _fp and _item_already_saved("cards", _fp, _n, _cf_fmt):
                    q.put(("STATUS", "cards", _fp, "skipped"))
            for _n in all_lore_nodes:
                _fp = _n.get("fullPath", "")
                if _fp and _item_already_saved("lorebooks", _fp, _n, _cf_fmt):
                    q.put(("STATUS", "lorebooks", _fp, "skipped"))
            for _n in all_preset_nodes:
                _fp = _n.get("fullPath", "")
                if _fp and _item_already_saved("presets", _fp, _n, _cf_fmt):
                    q.put(("STATUS", "presets", _fp, "skipped"))
            for _n in all_persona_nodes:
                _pid = str(_n.get("id") or _n.get("name", ""))
                if _pid and _item_already_saved("personas", _pid, _n, _cf_fmt):
                    q.put(("STATUS", "personas", _pid, "skipped"))
            for _fkey in char_groups:
                if _item_already_saved("chats", _fkey, {}, _cf_fmt):
                    q.put(("STATUS", "chats", _fkey, "skipped"))

            self._fetch_sess          = sess
            self._fetched_cards       = all_card_nodes
            self._fetched_lore        = all_lore_nodes
            self._fetched_presets     = all_preset_nodes
            self._fetched_personas    = all_persona_nodes
            self._fetched_sessions    = all_sessions
            self._fetched_char_groups = char_groups

            self._download_ready.wait()

        except Exception:
            _log_file.write(_tb.format_exc() + "\n")
            q.put(("ERROR", _tb.format_exc()))
            _log_file.close()
            return
        _log_file.close()

        if self._cancel_event.is_set():
            return

        self._cancel_download_event.clear()
        self._download_thread = threading.current_thread()
        self._download_phase()

    # ── download phase ────────────────────────────────────────────────────────

    def _download_phase(self):
        import traceback as _tb
        q    = self._q
        sess = self._fetch_sess
        all_card_nodes    = self._fetched_cards
        all_lore_nodes    = self._fetched_lore
        all_preset_nodes  = self._fetched_presets
        all_persona_nodes = self._fetched_personas
        char_groups       = self._fetched_char_groups

        _log_file = open(ROOT / "debug.log", "a", encoding="utf-8", buffering=1)
        _log_file.write(f"\n{'─'*40} download {time.strftime('%H:%M:%S')} {'─'*40}\n")

        def log(m):  _log_file.write(m + "\n"); q.put(("LOG", m))
        def flog(m): _log_file.write(m + "\n")
        def status(t, k, s): q.put(("STATUS", t, k, s))
        def image(t, k, b):  q.put(("IMAGE",  t, k, b))

        try:
            import threading as _thr
            from concurrent.futures import ThreadPoolExecutor as _TPE, as_completed as _asc
            _save_lock = _thr.Lock()
            saved_cards = saved_lore = saved_presets = saved_personas = saved_chats = 0
            force_global = self._force_redownload
            force_per    = self._grids

            card_fmt    = self._card_fmt
            chat_fmt    = self._chat_fmt
            merge_chats = self._merge_chats

            snapshot         = getattr(self, "_checked_snapshot", {})
            checked_cards    = snapshot.get("cards",    set())
            checked_lore     = snapshot.get("lorebooks", set())
            checked_presets  = snapshot.get("presets",   set())
            checked_personas = snapshot.get("personas",  set())
            checked_chats    = snapshot.get("chats",     set())

            self._total = (
                sum(1 for n in all_card_nodes    if n.get("fullPath","") in checked_cards) +
                sum(1 for n in all_lore_nodes    if n.get("fullPath","") in checked_lore) +
                sum(1 for n in all_preset_nodes  if n.get("fullPath","") in checked_presets) +
                sum(1 for n in all_persona_nodes if str(n.get("id") or n.get("name","")) in checked_personas) +
                sum(1 for fkey in char_groups    if fkey in checked_chats)
            )
            self._done = 0
            self._failed_snapshot = {t: set() for t in ("cards","lorebooks","presets","personas","chats")}
            q.put(("LOG", f"Downloading {self._total} selected item(s)…"))

            def _dl_cancelled():
                if self._cancel_download_event.is_set():
                    q.put(("DOWNLOAD_CANCELLED",)); return True
                return False

            if _dl_cancelled(): return

            CARDS_DIR.mkdir(parents=True, exist_ok=True)
            _flog_lock = _thr.Lock()
            def _tflog(m):
                with _flog_lock: _log_file.write(m + "\n")

            def _dl_card_worker(n):
                fp = n.get("fullPath", "")
                if not fp or fp not in checked_cards: return None
                force  = force_global or fp in force_per["cards"]._force_keys
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
                if already and not force:
                    if png_p.exists(): image("cards", fp, png_p.read_bytes())
                    status("cards", fp, "skipped")
                    return ("skipped", fp)
                status("cards", fp, "downloading")
                result = _sess_download_card(sess, fp, png_p, json_p, image, "cards",
                                             _tflog, _tflog, fmt=card_fmt)
                outcome = "done" if result == "ok" else result
                status("cards", fp, outcome)
                time.sleep(DELAY)
                return (outcome, fp)

            cards_to_dl = [n for n in all_card_nodes if n.get("fullPath","") in checked_cards]
            with _TPE(max_workers=3) as _ex:
                _futs = {_ex.submit(_dl_card_worker, n): n for n in cards_to_dl}
                for _fut in _asc(_futs):
                    if self._cancel_download_event.is_set(): break
                    _res = _fut.result()
                    if _res is None: continue
                    _outcome, _fp = _res
                    if _outcome in ("done", "skipped"):
                        with _save_lock: saved_cards += 1
                    elif _outcome == "failed":
                        self._failed_snapshot["cards"].add(_fp)

            if _dl_cancelled(): return

            LORE_DIR.mkdir(parents=True, exist_ok=True)
            for n in all_lore_nodes:
                fp   = n.get("fullPath", "")
                name = n.get("name", fp) or fp
                if not fp or fp not in checked_lore: continue
                force = force_global or fp in force_per["lorebooks"]._force_keys
                out_p = LORE_DIR / f"{_safe(name)}.json"
                if out_p.exists() and not force:
                    status("lorebooks", fp, "skipped"); saved_lore += 1; continue
                status("lorebooks", fp, "downloading")
                ok = False
                _lore_codes: set[int] = set()
                _lore_exc = False
                api_path = fp[len("lorebooks/"):] if fp.startswith("lorebooks/") else fp
                lore_lc  = _lc_author(api_path)
                for url in [f"{REPO_API}/api/lorebooks/{api_path}?full=true",
                             f"{REPO_API}/api/lorebooks/{lore_lc}?full=true",
                             f"{REPO_API}/api/lorebooks/{api_path}/raw_definition",
                             f"{REPO_API}/api/lorebooks/{lore_lc}/raw_definition",
                             f"{REPO_API}/api/lorebooks/{api_path}",
                             f"{REPO_API}/api/lorebooks/{lore_lc}"]:
                    try:
                        r = _req(sess, "get", url, timeout=30)
                        if not r.ok:
                            log(f"  {url} → {r.status_code}")
                            _lore_codes.add(r.status_code)
                            continue
                        try: out_p.write_text(json.dumps(r.json(), indent=2, ensure_ascii=False), encoding="utf-8")
                        except Exception: out_p.write_bytes(r.content)
                        ok = True; time.sleep(DELAY); break
                    except Exception as e:
                        log(f"  {url} error: {e}")
                        _lore_exc = True
                _lore_unavail = not ok and not _lore_exc and _lore_codes and all(c == 404 for c in _lore_codes)
                if not ok:
                    log(f"  lorebook {'unavailable' if _lore_unavail else 'failed'}: {fp}")
                    if not _lore_unavail:
                        self._failed_snapshot["lorebooks"].add(fp)
                lore_outcome = "done" if ok else ("unavailable" if _lore_unavail else "failed")
                status("lorebooks", fp, lore_outcome)
                if ok: saved_lore += 1

            if _dl_cancelled(): return

            PRESETS_DIR.mkdir(parents=True, exist_ok=True)
            for n in all_preset_nodes:
                fp   = n.get("fullPath", "")
                if not fp or fp not in checked_presets: continue
                api_path    = fp[len("presets/"):] if fp.startswith("presets/") else fp
                api_path_lc = _lc_author(api_path)
                node_id     = str(n.get("id", ""))
                force = force_global or fp in force_per["presets"]._force_keys
                out_p = PRESETS_DIR / f"{_safe(fp.replace('/', '_'))}.json"
                if out_p.exists() and not force:
                    status("presets", fp, "skipped"); saved_presets += 1; continue
                status("presets", fp, "downloading")
                ok = False
                _pre_codes: set[int] = set()
                _pre_exc = False
                urls = [f"{REPO_API}/api/presets/{api_path}?full=true",
                        f"{REPO_API}/api/presets/{api_path_lc}?full=true",
                        f"{REPO_API}/api/presets/{api_path}",
                        f"{REPO_API}/api/presets/{api_path_lc}"]
                if node_id:
                    urls += [f"{REPO_API}/api/presets/{node_id}?full=true",
                             f"{REPO_API}/api/presets/{node_id}"]
                for url in urls:
                    try:
                        r = _req(sess, "get", url, timeout=30)
                        if not r.ok:
                            log(f"  {url} → {r.status_code}")
                            _pre_codes.add(r.status_code)
                            continue
                        try: out_p.write_text(json.dumps(r.json(), indent=2, ensure_ascii=False), encoding="utf-8")
                        except Exception: out_p.write_bytes(r.content)
                        ok = True; time.sleep(DELAY); break
                    except Exception as e:
                        log(f"  {url} error: {e}")
                        _pre_exc = True
                _pre_unavail = not ok and not _pre_exc and _pre_codes and all(c == 404 for c in _pre_codes)
                if not ok:
                    log(f"  preset {'unavailable' if _pre_unavail else 'failed'}: {fp}")
                    if not _pre_unavail:
                        self._failed_snapshot["presets"].add(fp)
                pre_outcome = "done" if ok else ("unavailable" if _pre_unavail else "failed")
                status("presets", fp, pre_outcome)
                if ok: saved_presets += 1

            if _dl_cancelled(): return

            PERSONAS_DIR.mkdir(parents=True, exist_ok=True)
            for n in all_persona_nodes:
                pid   = str(n.get("id") or n.get("name", ""))
                pname = n.get("name") or pid
                if not pid or pid not in checked_personas: continue
                chub_id     = str(n.get("id", ""))
                folder_name = f"{_safe(pname)}_{chub_id}" if chub_id else _safe(pname)
                persona_dir = PERSONAS_DIR / folder_name
                out_p       = persona_dir / f"{_safe(pname)}.json"
                force = force_global or pid in force_per["personas"]._force_keys
                if out_p.exists() and not force:
                    status("personas", pid, "skipped"); saved_personas += 1; continue
                status("personas", pid, "downloading")
                try:
                    persona_dir.mkdir(parents=True, exist_ok=True)
                    out_p.write_text(json.dumps(n, indent=2, ensure_ascii=False), encoding="utf-8")
                    avatar_url = n.get("avatar_url") or n.get("avatar") or n.get("image_url")
                    if avatar_url:
                        try:
                            r = _req(sess, "get", avatar_url, timeout=20)
                            if r.ok:
                                ext = ".png" if "png" in r.headers.get("content-type","").lower() else ".jpg"
                                (persona_dir / f"{_safe(pname)}{ext}").write_bytes(r.content)
                        except Exception as e:
                            flog(f"  persona avatar error for {pname}: {e}")
                    status("personas", pid, "done"); saved_personas += 1
                    time.sleep(DELAY)
                except Exception as e:
                    log(f"  persona failed: {pname} — {e}")
                    self._failed_snapshot["personas"].add(pid)
                    status("personas", pid, "failed")

            if _dl_cancelled(): return

            _chat_ext = {"JSONL": ".jsonl", "JSON": ".json", "TXT": ".txt"}.get(chat_fmt, ".jsonl")
            _first_chat_logged = False
            for fkey, group in char_groups.items():
                if fkey not in checked_chats: continue
                status("chats", fkey, "downloading")
                out_dir = CHATS_DIR / fkey
                cname, _ = _char_folder_key(group[0])
                group_saved = group_failed = group_total_msgs = group_branched = 0

                for s in group:
                    sid = str(s.get("id") or s.get("session_id", ""))
                    if not sid: continue
                    chat_own_name = s.get("name") or ""
                    file_stem  = _safe(f"{chat_own_name}_{sid}" if chat_own_name else sid)
                    force_chat = force_global or fkey in force_per["chats"]._force_keys
                    if merge_chats:
                        # Only the single merged file counts — leftover *_branch_N
                        # files from a previous non-merged download should NOT
                        # block (re)writing the merged file.
                        already_saved = (out_dir / f"{file_stem}{_chat_ext}").exists() and \
                            not any(out_dir.glob(f"{file_stem}_branch_*{_chat_ext}"))
                    else:
                        already_saved = out_dir.exists() and any(out_dir.glob(f"{file_stem}*{_chat_ext}"))
                    if not force_chat and already_saved:
                        group_saved += 1; continue
                    try:
                        r = _req(sess, "get",
                            f"{GATEWAY_API}/api/core/chats/v2/{sid}",
                            params={"include_messages": "true", "include_config": "false",
                                    "include_meta": "false"}, timeout=30)
                        flog(f"  chat {sid} → {r.status_code}")
                        if r.ok:
                            body = r.json()
                            if not _first_chat_logged:
                                flog(f"  first chat body keys: {list(body.keys())[:10] if isinstance(body, dict) else type(body).__name__}")
                                _first_chat_logged = True
                            msgs = _extract_messages(body)
                            msg_map = {m["id"]: m for m in msgs if "id" in m}
                            null_ids = [m["id"] for m in msgs if m.get("message") is None and "id" in m]
                            if null_ids:
                                flog(f"  chat {sid}: {len(null_ids)} null-content — fetching via POST /messages/content")
                                BATCH = 50; recovered_total = 0
                                for _bi in range(0, len(null_ids), BATCH):
                                    if self._cancel_download_event.is_set(): break
                                    batch_ids = null_ids[_bi:_bi + BATCH]
                                    try:
                                        pr = sess.post(
                                            f"{GATEWAY_API}/api/core/chats/v2/{sid}/messages/content",
                                            json={"ids": batch_ids}, timeout=30)
                                        if not pr.ok: flog(f"  /messages/content error: {pr.text[:300]}"); break
                                        resp_body = pr.json()
                                        messages_val = resp_body.get("messages", {}) if isinstance(resp_body, dict) else resp_body
                                        recovered = 0
                                        if isinstance(messages_val, dict):
                                            for _id_str, _content in messages_val.items():
                                                if _content is None: continue
                                                try: _mid = int(_id_str)
                                                except (ValueError, TypeError): continue
                                                if _mid in msg_map:
                                                    msg_map[_mid]["message"] = _content; recovered += 1
                                        elif isinstance(messages_val, list):
                                            for pm in messages_val:
                                                _mid = pm.get("id")
                                                _content = pm.get("message") if pm.get("message") is not None else pm.get("content")
                                                if _mid is not None and _content is not None:
                                                    _mid = int(_mid) if not isinstance(_mid, int) else _mid
                                                    if _mid in msg_map:
                                                        msg_map[_mid]["message"] = _content; recovered += 1
                                        recovered_total += recovered
                                    except Exception as pe:
                                        flog(f"  /messages/content error: {pe}"); break
                                    time.sleep(DELAY)
                                flog(f"  recovered {recovered_total} of {len(null_ids)}")

                            any_saved = False; saved_branch_count = 0
                            if merge_chats:
                                path = _build_main_path(msg_map)
                                n_msgs = sum(1 for node, _, _ in path if node.get("message") is not None)
                                n_swipe_pts = sum(1 for _, sib, _ in path if len(sib) > 1)
                                if n_msgs:
                                    out_dir.mkdir(parents=True, exist_ok=True)
                                    m_path = out_dir / f"{file_stem}{_chat_ext}"
                                    m_path.write_text(_merged_chat_text(path, cname, "You", chat_fmt), encoding="utf-8")
                                    any_saved = True
                                    group_total_msgs += n_msgs
                                    saved_branch_count = 1
                                    if n_swipe_pts: group_branched += 1
                            else:
                                branches = _build_branches(msg_map)
                                for bi, branch_msgs in enumerate(branches):
                                    branch_msgs = [m for m in branch_msgs if m.get("message") is not None]
                                    if not branch_msgs: continue
                                    if len(branches) == 1:
                                        b_path = out_dir / f"{file_stem}{_chat_ext}"
                                    else:
                                        b_path = out_dir / f"{file_stem}_branch_{bi+1}{_chat_ext}"
                                    out_dir.mkdir(parents=True, exist_ok=True)
                                    if chat_fmt == "JSON":
                                        b_path.write_text(json.dumps(branch_msgs, indent=2, ensure_ascii=False), encoding="utf-8")
                                    elif chat_fmt == "TXT":
                                        b_path.write_text(_msgs_to_txt(branch_msgs, cname), encoding="utf-8")
                                    else:
                                        b_path.write_text("\n".join(json.dumps(m, ensure_ascii=False) for m in branch_msgs), encoding="utf-8")
                                    any_saved = True
                                    group_total_msgs += len(branch_msgs)
                                    saved_branch_count += 1
                            if any_saved:
                                group_saved += 1
                                if saved_branch_count > 1: group_branched += 1
                        else:
                            log(f"  chat {sid} → {r.status_code}"); group_failed += 1
                    except Exception as e:
                        log(f"  chat {sid} error: {e}"); group_failed += 1
                    time.sleep(DELAY)

                if group_failed > 0 and group_saved == 0:
                    status("chats", fkey, "failed")
                    self._failed_snapshot["chats"].add(fkey)
                elif group_saved > 0:
                    status("chats", fkey, "done"); saved_chats += 1
                    badge_parts = [f"{group_total_msgs} msgs"]
                    if group_branched: badge_parts.append(f"{group_branched} branched")
                    q.put(("BADGE",     "chats", fkey, " · ".join(badge_parts)))
                    q.put(("MSG_COUNT", "chats", fkey, group_total_msgs))
                else:
                    status("chats", fkey, "skipped"); saved_chats += 1

            _parts = []
            if saved_cards:    _parts.append(f"{saved_cards} card{'s' if saved_cards != 1 else ''}")
            if saved_lore:     _parts.append(f"{saved_lore} lorebook{'s' if saved_lore != 1 else ''}")
            if saved_presets:  _parts.append(f"{saved_presets} preset{'s' if saved_presets != 1 else ''}")
            if saved_personas: _parts.append(f"{saved_personas} persona{'s' if saved_personas != 1 else ''}")
            if saved_chats:    _parts.append(f"{saved_chats} chat group{'s' if saved_chats != 1 else ''}")
            _summary = ", ".join(_parts) if _parts else "nothing new"
            q.put(("DONE", f"Finished — {_summary}."))

            if any(self._failed_snapshot.values()):
                q.put(("SHOW_RETRY_BTN",))

        except Exception:
            _log_file.write(_tb.format_exc() + "\n")
            q.put(("ERROR", _tb.format_exc()))
        finally:
            _log_file.close()


# ─── PNG / chat helpers ────────────────────────────────────────────────────────

def _msgs_to_txt(msgs: list, char_name: str) -> str:
    lines = []
    if char_name:
        lines.append(f"Character: {char_name}")
        lines.append("─" * 40)
        lines.append("")
    for m in msgs:
        role    = m.get("role", "unknown")
        content = m.get("content", "") or m.get("message", "")
        if isinstance(content, list):
            content = " ".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content)
        speaker = (
            "User" if role in ("user", "human") else
            (char_name or "Assistant") if role in ("assistant", "model", "char", "character") else
            f"[{role}]"
        )
        lines.append(f"{speaker}: {content}")
        lines.append("")
    return "\n".join(lines)


def _extract_chara_chunk(png: bytes) -> bool:
    i = 8
    while i < len(png) - 12:
        length = struct.unpack(">I", png[i:i+4])[0]
        chunk_type = png[i+4:i+8]
        if chunk_type == b"tEXt" and png[i+8:i+13] == b"chara":
            return True
        i += 12 + length
    return False


def _build_st_png(avatar_bytes: bytes, char_json: dict) -> bytes:
    img = Image.open(io.BytesIO(avatar_bytes)).convert("RGB")
    buf = io.BytesIO(); img.save(buf, "PNG"); png = buf.getvalue()
    b64     = base64.b64encode(json.dumps(char_json, ensure_ascii=False).encode()).decode()
    payload = b"chara\x00" + b64.encode()
    crc     = zlib.crc32(b"tEXt" + payload) & 0xFFFFFFFF
    chunk   = struct.pack(">I", len(payload)) + b"tEXt" + payload + struct.pack(">I", crc)
    return png[:-12] + chunk + png[-12:]


def _sess_download_card(sess, fp: str, png_p: Path, json_p: Path,
                        image_cb, tab: str, log, flog=None, fmt: str = "PNG") -> str:
    """Returns 'ok', 'unavailable' (all 404s), or 'failed' (other errors)."""
    if flog is None:
        flog = log
    want_png  = fmt != "JSON"
    want_json = fmt != "PNG"
    png_data: bytes | None = None
    _error_codes: set[int] = set()
    _had_exception = False

    if want_png:
        for cdn_name in ("chara_card_v2.png", "chara_card_v2.webp", "avatar.webp"):
            try:
                r = sess.get(f"{CDN_BASE}/{fp}/{cdn_name}", timeout=30)
                if r.ok and r.content[:4] == b"\x89PNG":
                    png_data = r.content; break
                elif r.ok and cdn_name.endswith(".webp") and not png_data:
                    img = Image.open(io.BytesIO(r.content)).convert("RGB")
                    buf = io.BytesIO(); img.save(buf, "PNG"); png_data = buf.getvalue(); break
                elif not r.ok:
                    _error_codes.add(r.status_code)
            except Exception as e:
                log(f"  CDN {cdn_name} error: {e}")
                _had_exception = True
        if not png_data:
            fp_lc = _lc_author(fp)
            for dl_fp in ([fp] if fp == fp_lc else [fp, fp_lc]):
                try:
                    r = sess.get(f"{REPO_API}/api/characters/{dl_fp}/download", timeout=60)
                    if r.ok and r.content[:4] == b"\x89PNG":
                        png_data = r.content; break
                    else:
                        log(f"  /download → {r.status_code}")
                        _error_codes.add(r.status_code)
                except Exception as e:
                    log(f"  /download error: {e}")
                    _had_exception = True

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
                log(f"  {url} → {r.status_code}")
                _error_codes.add(r.status_code)
                continue
            j = r.json()
            if "spec" in j or "name" in j or "node" in j:
                char_json = j
                if want_json and not json_p.exists():
                    json_p.write_text(json.dumps(char_json, indent=2, ensure_ascii=False), encoding="utf-8")
                time.sleep(DELAY); break
        except Exception as e:
            log(f"  {url} error: {e}")
            _had_exception = True

    if want_png and png_data and char_json and png_data[:4] == b"\x89PNG":
        try:
            if not _extract_chara_chunk(png_data):
                png_data = _build_st_png(png_data, char_json)
        except Exception:
            pass

    if want_png and png_data:
        png_p.write_bytes(png_data)
        image_cb(tab, fp, png_data)

    success = png_p.exists() if want_png else json_p.exists()
    if success:
        return "ok"
    if not _had_exception and _error_codes and all(c == 404 for c in _error_codes):
        return "unavailable"
    return "failed"


# ─── entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    for d in (CARDS_DIR, LORE_DIR, CHATS_DIR, PRESETS_DIR, PERSONAS_DIR):
        d.mkdir(parents=True, exist_ok=True)
    app = QApplication(sys.argv)
    app.setStyleSheet(STYLESHEET)
    window = App()
    window.show()
    sys.exit(app.exec())
