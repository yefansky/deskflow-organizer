"""Smart Desktop Organizer: local-only desktop overlay and control service.

The organizer renders a virtual, editable arrangement and stores its settings
under APPDATA.  Files are never moved; explicit user deletion uses the Windows
Recycle Bin so it remains recoverable.
"""
from __future__ import annotations

import argparse
import atexit
import ctypes
import ctypes.wintypes
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import hashlib
import html
import zipfile
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
import uvicorn
import tkinter as tk
from tkinter import colorchooser, font as tkfont, messagebox, simpledialog, ttk
from PIL import Image, ImageTk, ImageDraw, ImageFont
from win32com.shell import shell, shellcon
import win32api
import win32con
import win32gui
import win32ui
from win32com.client import Dispatch
from layout_engine import Rect, overlaps, pack_rectangles, resize_rectangle
from app_runtime import command, APP_ROOT
try:
    import winreg
except ImportError:
    winreg = None

APP_NAME = "SmartDesktopOrganizer"
DATA_DIR = Path(os.environ.get("APPDATA", Path.home())) / APP_NAME
CONFIG_PATH = DATA_DIR / "layout.json"
NATIVE_ICON_STATE_PATH = DATA_DIR / "native_icon_state.json"
def windows_desktop() -> Path:
    """Respect Desktop folders redirected by Windows or enterprise policy."""
    if winreg:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as key:
                value, _ = winreg.QueryValueEx(key, "Desktop")
                return Path(os.path.expandvars(value))
        except OSError: pass
    return Path(os.environ.get("USERPROFILE", Path.home())) / "Desktop"
DEFAULT_DESKTOP = windows_desktop()

def windows_common_desktop() -> Path:
    return Path(os.environ.get("PUBLIC", r"C:\Users\Public")) / "Desktop"

COMMON_DESKTOP = windows_common_desktop()


def enable_per_monitor_dpi_awareness() -> None:
    """Keep Tk geometry in the same pixel space as Win32 monitor work areas.

    RDP can change both resolution and scaling while the organizer is running.
    Without per-monitor awareness, ``EnumDisplayMonitors`` reports physical
    pixels while Tk can interpret ``geometry`` in virtualised pixels, making a
    valid layout appear several times too wide or tall.
    """
    try:
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2.  It must be set before
        # the first Tk window is created; it is harmless when a launcher has
        # already selected a DPI context.
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
    except (AttributeError, OSError):
        pass


@dataclass
class Category:
    id: str
    title: str
    color: str
    mode: str = "list"  # list | cards | compact


DEFAULT_CATEGORIES = [
    Category("inbox", "新放入 · 收件箱", "#ffbd59"),
    Category("work", "工作文档", "#72d6ff"),
    Category("code", "项目 · 开发", "#ab8dff"),
    Category("devtools", "开发工具", "#a990ff"),
    Category("communication", "通讯工具", "#59d8c5"),
    Category("ai", "AI 工具", "#d38cff"),
    Category("entertainment", "娱乐", "#ffb76b"),
    Category("remote", "远程连接", "#65e1b5"),
    # Kept for system utilities and productivity software.  It is deliberately
    # no longer a catch-all: app categories above take the recognisable tools.
    Category("tools", "系统 · 效率", "#ff8fb1"),
    Category("media", "创作 · 媒体", "#ff9b66"),
    Category("games", "游戏", "#78b8ff"),
    Category("archive", "资料 · 归档", "#b6c1d3"),
]

CATEGORY_PURPOSES = {
    "inbox": "新出现且尚未确认归属的项目",
    "work": "文档、演示、表格、需求、汇报与工作资料",
    "code": "代码、工程文件与项目配置",
    "devtools": "IDE、编辑器、编译调试、版本控制与开发辅助入口",
    "communication": "即时通讯、邮件、协作与局域网传输入口",
    "ai": "AI 对话、模型运行、生成与 AI 辅助工具",
    "entertainment": "音乐、视频、阅读等非游戏娱乐入口",
    "remote": "远程桌面、VPN 与机器连接入口",
    "tools": "系统维护、硬件控制、下载、办公与通用效率工具",
    "media": "图片、音视频、录屏、美术与创作素材",
    "games": "真实游戏、游戏平台与游戏启动入口",
    "archive": "压缩包、旧资料、低频文本与其他归档项目",
}

def all_categories(store: "Store") -> list[Category]:
    custom = [Category(item["id"], item["title"], item.get("color", "#8bd3ff"), "list") for item in store.data.get("custom_blocks", [])]
    return DEFAULT_CATEGORIES + custom

def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Store:
    def __init__(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.data = self._load()

    def _defaults(self) -> dict[str, Any]:
        return {
            "version": 8, "desktop": str(DEFAULT_DESKTOP), "overrides": {}, "visual_assets": {}, "markdown_summaries": {}, "document_summaries": {}, "folder_summaries": {}, "tip_summaries": {}, "item_metadata": {},
            "pending_inbox": {},
            "scanned_roots": [str(DEFAULT_DESKTOP)],
            "seen": {}, "category_modes": {c.id: c.mode for c in DEFAULT_CATEGORIES}, "category_sorting": {}, "item_orders": {}, "custom_blocks": [],
            "ui": {"cell_size": 48, "layout_footprint": 0.82, "font_size": 9, "icon_size": 24, "alpha": 0.22,
                   "background": "#101827", "auto_contrast": True,
                   "geometry": "1180x680+80+110", "panel_positions": {}, "panel_sizes": {},
                   "target_monitor": "primary", "preferred_monitor": None,
                   "preferred_monitor_count": 0, "preferred_monitor_index": -1,
                   "preferred_monitor_device": "", "monitor_fallback_active": False,
                   "category_names": {}, "panel_styles": {}, "auto_layout": True, "layout_profiles": {}},
        }

    def _load(self) -> dict[str, Any]:
        base = self._defaults()
        if CONFIG_PATH.exists():
            try:
                saved = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                for key in base:
                    if isinstance(base[key], dict): base[key].update(saved.get(key, {}))
                    else: base[key] = saved.get(key, base[key])
                if saved.get("version", 1) < 2:
                    # Earlier builds silently forced these modes at render
                    # time, so no real user choice could have been recorded.
                    base["category_modes"].update({"tools": "cards", "games": "cards"})
                    base["version"] = 2
                if saved.get("version", 1) < 3:
                    base["version"] = 3
                if saved.get("version", 1) < 4:
                    # Existing items are already familiar.  Only files first
                    # observed by version 4+ become persistent native inbox items.
                    base["pending_inbox"] = {}
                    base["version"] = 4
                if saved.get("version", 1) < 5:
                    # The old implementation knew only the user's redirected
                    # desktop.  Public Desktop is baseline content, not 55 new files.
                    base["scanned_roots"] = [str(Path(base["desktop"]))]
                    base["version"] = 5
                if saved.get("version", 1) < 6:
                    # v6 splits the former generic tools block.  Only automatic
                    # classifications move; `overrides` remain the user's intent.
                    base["version"] = 6
                if saved.get("version", 1) < 7:
                    # v7 moves the desktop presentation layer from Tk's whole-
                    # window alpha to Qt per-pixel composition.  The old safe
                    # floor (70%) existed only because it also faded icons and
                    # text.  With independently opaque content, a genuinely
                    # glass-like 22% plate is readable and matches the compact
                    # one-column desktop-fence layout requested by the user.
                    base["ui"]["alpha"] = .22
                    base["ui"]["layout_footprint"] = max(.82, float(base["ui"].get("layout_footprint", .82)))
                    base["ui"]["cell_size"] = min(52, int(base["ui"].get("cell_size", 48)))
                    for category in DEFAULT_CATEGORIES:
                        if category.id != "inbox":
                            base["category_modes"][category.id] = "list"
                    base["version"] = 7
                if saved.get("version", 1) < 8:
                    # v8 gives the agent a stable place for user/AI-authored
                    # summaries and tags.  Generated Tip caches remain
                    # disposable; explicit metadata survives rescans.
                    base.setdefault("item_metadata", {})
                    base["version"] = 8
            except (OSError, json.JSONDecodeError):
                pass
        old_default = Path(os.environ.get("USERPROFILE", Path.home())) / "Desktop"
        if Path(base["desktop"]) == old_default and DEFAULT_DESKTOP != old_default:
            base["desktop"] = str(DEFAULT_DESKTOP)
        # Older builds stored only the currently effective HMONITOR.  Keep it
        # as the initial preference so a missing display can fall back without
        # destroying the user's intent.  The first explicit screen choice in a
        # new build records the durable count/index/device metadata below.
        if not base["ui"].get("preferred_monitor"):
            base["ui"]["preferred_monitor"] = base["ui"].get("target_monitor", "primary")
        return base

    def save(self) -> None:
        with self.lock:
            CONFIG_PATH.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")


def shortcut_target(path: Path) -> str:
    """Resolve a Windows shortcut without depending on OrganizerModel state."""
    try:
        if path.suffix.lower() == ".lnk":
            return Dispatch("WScript.Shell").CreateShortcut(str(path)).TargetPath or ""
        if path.suffix.lower() == ".url":
            return next((
                line.split("=", 1)[1]
                for line in path.read_text(encoding="utf-8", errors="ignore").splitlines()
                if line.lower().startswith("url=")
            ), "")
    except Exception:
        pass
    return ""


class Classifier:
    RULES = {
        "remote": (r"\.rdp$|vpn|远程|4090|5090|citygarden|homejyw|j.*_vpn",),
        "games": (r"原神|悟空|剑星|黎明|游戏|steam|boss",),
        "media": (r"\.mp3$|\.mp4$|\.wav$|\.jpg$|\.png$|\.svg$|\.sbsar$|\.ssa$|剪辑|录像|音频|壁纸|photo|配音",),
        # Classify installed apps using the resolved shortcut target as well as
        # its display name.  This avoids treating every .lnk as "tools".
        "ai": (r"cherry studio|lm studio|豆包|西山灵问|chatgpt|claude|ollama|comfyui|stable diffusion",),
        "communication": (r"telegram|微信|wechat|\bqq\b|foxmail|网易邮箱|lan ?share|wps协作|teams|discord|slack",),
        "entertainment": (r"qq音乐|potplayer|京东读书|kindle|bilibili|爱奇艺|youku|网易云音乐",),
        "devtools": (r"visual studio|vscode|cursor|trae|intellij|pycharm|cmake|cygwin|\bgcc\b|notepad\+\+|notepad--|beyond compare|total commander|procmon|unity|egret|nasm|sourcetree|github desktop|\bfork\b|cc switch|cc-haha|tiled",),
        "code": (r"\.py$|\.cpp$|\.md$|\.yaml$|\.yml$|source|server|client",),
        "tools": (r"\.lnk$|\.exe$|百度|迅雷|clash|wps|摄像机|fastcopy|fancontrol|geforce|nvidia|samsung magician|分区助手|windowscleaner|printer|scan|free ?mind|typora|万兴pdf",),
        "work": (r"\.docx?$|\.pptx?$|\.xlsx?$|\.pdf$|需求|项目|工作|汇报|流程|台词",),
        "archive": (r"\.zip$|\.rar$|\.7z$|\.txt$|\.opt$|\.ini$|\.html$",),
    }

    @classmethod
    def category(cls, item: Path, store: Store) -> str:
        key = item.name.lower()
        custom = store.data["overrides"].get(key)
        if custom: return custom
        seen = store.data["seen"].get(key)
        if not seen: return "inbox"
        content_category = cls.content_category(item)
        if content_category: return content_category
        # Office documents remain documents even when their title mentions an
        # application (for example, an AI-IDE tutorial).  They are not launchers.
        if item.suffix.lower() in {".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".pdf"}:
            return "work"
        # “游戏”文档、PPT、需求不属于游戏入口；游戏类只收快捷方式、网页入口或可执行程序。
        if re.search(r"原神|悟空|剑星|黎明|steam|游戏启动器|boss", key, re.I) and item.suffix.lower() in {".lnk", ".url", ".exe"}:
            return "games"
        target = cls._resolved_target(item, store)
        evidence = f"{key} {target}".lower()
        for category, patterns in cls.RULES.items():
            if category == "games": continue
            if any(re.search(pattern, evidence, re.I) for pattern in patterns): return category
        return "archive"

    @staticmethod
    def _resolved_target(item: Path, store: Store) -> str:
        """Use the cached inspected shortcut target where possible."""
        cached = store.data.get("tip_summaries", {}).get(item.name.lower(), {})
        target = cached.get("target", "") if isinstance(cached, dict) else ""
        if target:
            return str(target)
        if item.suffix.lower() == ".lnk":
            return shortcut_target(item)
        return str(item)

    @staticmethod
    def content_category(item: Path) -> str | None:
        """Use only a short local preview of safe plain-text formats as evidence."""
        if item.suffix.lower() not in {".txt", ".md", ".py", ".cpp", ".h", ".html", ".svg", ".yaml", ".yml", ".json", ".ini"}:
            return None
        try:
            raw = item.read_bytes()[:8192]
            text = ""
            for encoding in ("utf-8", "utf-8-sig", "utf-16", "gb18030"):
                try:
                    text = raw.decode(encoding); break
                except UnicodeDecodeError: continue
            if not text: return None
        except OSError: return None
        lowered = text.lower()
        # Never surface or learn from plaintext secrets; keep them out of work/project cards.
        if re.search(r"(?:sk-|api[_-]?key|token|password|secret)\s*[:=]?\s*[\w-]{12,}", text, re.I): return "archive"
        if re.search(r"#include|\bclass\s+\w+|\bdef\s+\w+|\bfunction\s*\(|lua binding|stack traceback", lowered): return "code"
        if re.search(r"proxy-providers|mixed-port|external-controller|subscription|机场订阅", lowered): return "tools"
        if re.search(r"<title>|<!doctype|项目.*汇报|需求|入门指南|npcid|talkcontent|功能制作", lowered): return "work"
        return None


class OrganizerModel:
    def __init__(self, store: Store) -> None:
        self.store = store

    @property
    def desktop(self) -> Path: return Path(self.store.data["desktop"])

    @property
    def desktop_roots(self) -> tuple[Path, ...]:
        roots = (self.desktop, COMMON_DESKTOP)
        return tuple(dict.fromkeys(root for root in roots if root.exists()))

    def scan(self) -> dict[str, list[dict[str, Any]]]:
        result = {c.id: [] for c in all_categories(self.store)}
        entries: list[tuple[Path, Path]] = []
        for root in self.desktop_roots:
            try: entries.extend((root, item) for item in root.iterdir())
            except OSError: pass
        entries.sort(key=lambda pair: (pair[1].name.casefold(), str(pair[0]).casefold()))
        now = datetime.now()
        seen = self.store.data["seen"]
        pending = self.store.data.setdefault("pending_inbox", {})
        scanned_roots = self.store.data.setdefault("scanned_roots", [])
        scanned_root_keys = {str(Path(root)).casefold() for root in scanned_roots}
        baseline_roots = {str(root).casefold() for root in self.desktop_roots if str(root).casefold() not in scanned_root_keys}
        changed = False
        present_keys: set[str] = set()
        for root, item in entries:
            if item.name.lower() in {"desktop.ini", "desktop (1).ini"}: continue
            try:
                stat = item.stat()
            except OSError:
                # Antivirus bait/junctions and vanishing entries must not take
                # the whole organizer down; treat them as invisible.
                continue
            key = item.name.lower()
            present_keys.add(key)
            # Treat a first scan as familiar, except items changed in the last 7 days.
            if key not in seen:
                seen[key] = {"first_seen": now_iso(), "first_seen_epoch": time.time()}
                if str(root).casefold() not in baseline_roots:
                    pending[key] = {"first_seen": now_iso(), "path": str(item)}
                changed = True
                if datetime.fromtimestamp(stat.st_mtime) < now - timedelta(days=7):
                    seen[key]["first_seen_epoch"] = time.time() - 9 * 86400
            category = self.store.data["overrides"].get(key)
            if not category:
                # New files stay on the native desktop indefinitely.  Time must
                # never silently turn an unreviewed file into an organized item.
                category = "inbox" if key in pending else Classifier.category(item, self.store)
            result.setdefault(category, []).append({
                "name": item.name, "path": str(item), "kind": "folder" if item.is_dir() else item.suffix.lower().lstrip(".") or "file",
                "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
                "manual": key in self.store.data["overrides"],
                "native_pending": key in pending and category == "inbox",
            })
            stamp = f"{stat.st_mtime_ns}:{stat.st_size}"
            cached_tip = self.store.data.setdefault("tip_summaries", {}).get(key)
            cached_text = "\n".join(str(cached_tip.get(field, "")) for field in ("summary", "app", "target")) if cached_tip else ""
            if (not cached_tip or cached_tip.get("stamp") != stamp or self.low_signal_tip(cached_text)):
                if item.suffix.lower() in {".md", ".txt", ".docx", ".pptx", ".pptm"}:
                    summary = self.document_summary(item)
                    summaries = self.store.data.setdefault("document_summaries", {})
                    if summary: summaries[key] = summary
                    else: summaries.pop(key, None)
                if item.is_dir():
                    summary = self.folder_summary(item)
                    if summary: self.store.data["folder_summaries"][key] = summary
                    else: self.store.data["folder_summaries"].pop(key, None)
                self.store.data["tip_summaries"][key] = {"stamp": stamp, **self.make_tip_summary(item)}
                changed = True
        stale_pending = set(pending) - present_keys
        if stale_pending:
            for key in stale_pending: pending.pop(key, None)
            changed = True
        if baseline_roots:
            scanned_roots.extend(str(root) for root in self.desktop_roots if str(root).casefold() in baseline_roots)
            changed = True
        if changed: self.store.save()
        return result

    def desktop_signature(self) -> tuple[tuple[str, int, int], ...]:
        """Cheap watcher signature; avoids repainting unchanged panels."""
        try:
            return tuple(sorted((str(item).casefold(), item.stat().st_mtime_ns, item.stat().st_size)
                                for root in self.desktop_roots for item in root.iterdir()
                                if item.name.lower() not in {"desktop.ini", "desktop (1).ini"}))
        except OSError:
            return ()

    @staticmethod
    def _safe_text(item: Path, limit: int = 12288) -> str:
        try:
            raw = item.read_bytes()[:limit]
            for encoding in ("utf-8-sig", "utf-8", "utf-16", "gb18030"):
                try:
                    return raw.decode(encoding)
                except UnicodeDecodeError: continue
        except OSError: pass
        return ""

    @staticmethod
    def _topic_from_text(text: str) -> str | None:
        """Use a likely title/topic, never a raw multi-line preview."""
        if not text or re.search(r"(?:sk-|api[_-]?key|token|password|secret)\s*[:=]?\s*[\w-]{12,}", text, re.I): return None
        lowered = text.lower()
        if "npcid" in lowered and ("talkcontent" in lowered or "npcname" in lowered): return "NPC 台词与语音数据"
        if "debugtracestack" in lowered or "stack traceback" in lowered: return "程序调试堆栈"
        text = re.sub(r"<[^>]+>", " ", text)
        lines = [re.sub(r"^(?:#+|[-*•]+|\d+[.、])\s*", "", line).strip(" \t-—:：") for line in text.splitlines()]
        ignored = re.compile(r"^(?:readme|todo|notes|untitled|export|空白演示|新建文档|公司[（(]|第?\d+[页章节]|目录)$", re.I)
        for topic in lines:
            # Table headers and boilerplate legal/office fragments do not say
            # what the document is about.
            if (not (4 <= len(topic) <= 90) or "\t" in topic or re.fullmatch(r"[\W_\d]+", topic) or ignored.search(topic)
                    or re.match(r"^(?:公司[（(]|单击此处添加|嗨[，,])", topic)): continue
            if re.search(r"_{3,}|（\s*）|\(\s*\)", topic): continue
            return topic[:90]
        return None

    @classmethod
    def document_summary(cls, item: Path) -> str | None:
        suffix = item.suffix.lower()
        if suffix in {".md", ".txt"}: return cls._topic_from_text(cls._safe_text(item))
        if suffix not in {".docx", ".pptx", ".pptm"}: return None
        def title_fallback() -> str | None:
            title = item.stem.strip()
            return title[:90] if 4 <= len(title) <= 90 and not re.fullmatch(r"[\W_\d]+", title) else None
        try:
            with zipfile.ZipFile(item) as package:
                if suffix == ".docx":
                    parts = ["docProps/core.xml", "word/document.xml"]
                else:
                    parts = ["docProps/core.xml", "ppt/slides/slide1.xml"]
                raw = "\n".join(package.read(part).decode("utf-8", "ignore") for part in parts if part in package.namelist())
            if re.search(r"身份证|证件号|联系电话|手机号码|家庭住址", raw, re.I): return title_fallback()
            fragments = re.findall(r"<(?:a:t|w:t|dc:title)[^>]*>(.*?)</(?:a:t|w:t|dc:title)>", raw, re.I | re.S)
            summary = cls._topic_from_text("\n".join(html.unescape(re.sub(r"<[^>]+>", "", value)).strip() for value in fragments))
            if summary: return summary
            # An Office document's filename is generally its supplied title;
            # it is better evidence than Office's default “Export/blank”.
            return title_fallback()
        except (OSError, zipfile.BadZipFile): return None

    @staticmethod
    def folder_summary(folder: Path) -> str | None:
        """Infer one folder purpose from first-layer evidence; never list files."""
        try: entries = list(folder.iterdir())
        except OSError: return None
        if not entries: return None
        visual_assets = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".jfif", ".mp4", ".mov", ".webm"}
        asset_count = sum(entry.is_file() and entry.suffix.lower() in visual_assets for entry in entries)
        # A large pile of memes/photos is self-evidently an asset collection;
        # filenames cannot safely yield a useful folder-level topic.
        if len(entries) >= 20 and asset_count / len(entries) >= .8:
            return None
        def meaningful(entry: Path) -> bool:
            stem = entry.stem
            # Hashes, timestamps and serial-like names describe no usable topic.
            if re.fullmatch(r"[{}0-9a-fA-F_-]{8,}", stem): return False
            return bool(re.search(r"[一-鿿]|[A-Za-z]{3,}", stem))
        named_entries = [entry for entry in entries if meaningful(entry)]
        # A pile of opaque same-type assets (e.g. Joke's hashed images) has no
        # trustworthy semantic summary.  Silence is more useful than filler.
        if not named_entries:
            return None
        names = " ".join(entry.name.lower() for entry in entries)
        child_names = {entry.name.lower() for entry in entries if entry.is_dir()}
        suffixes = [entry.suffix.lower() for entry in entries if entry.is_file()]
        if {"assets", "projectsettings"} <= child_names or "unity" in names: return "Unity 项目工程：存放源文件与资源，用于游戏开发和构建"
        if ".git" in child_names or any(marker in child_names for marker in {"src", "source", "package.json"}): return "软件项目工程：存放代码与配置，用于开发和构建"
        if suffixes and sum(ext in visual_assets for ext in suffixes) / len(suffixes) >= .65: return "图片与视频素材：存放视觉资源，用于创作、剪辑或展示"
        if suffixes and sum(ext in {".ppt", ".pptx", ".doc", ".docx", ".pdf", ".xlsx"} for ext in suffixes) / len(suffixes) >= .65: return "工作文档与演示资料：存放报告、方案和表格，用于查阅与汇报"
        topics = (("参考资料：存放教程与文档，用于学习和查阅", ("reference", "资料", "文档", "教程")),
                  ("项目开发资料：存放项目相关文件，用于开发与维护", ("project", "项目", "开发", "client", "server")),
                  ("游戏资源：存放游戏相关资源，用于内容制作", ("game", "游戏", "unity", "asset")),
                  ("音视频创作素材：存放音频、视频与配音，用于创作", ("audio", "video", "music", "音频", "录像", "配音")))
        for label, markers in topics:
            if sum(marker in names for marker in markers) >= 2: return label
        return None

    @staticmethod
    def folder_profile(folder: Path, limit: int = 400) -> dict[str, Any]:
        """Return a bounded two-level shape for AI folder-purpose summaries."""
        profile: dict[str, Any] = {
            "immediate_files": 0,
            "immediate_folders": 0,
            "sampled_files": 0,
            "extensions": {},
            "top_folders": [],
            "empty": False,
            "truncated": False,
        }
        try:
            top = sorted(folder.iterdir(), key=lambda item: item.name.casefold())
        except OSError:
            profile["unreadable"] = True
            return profile
        profile["empty"] = not top
        profile["immediate_files"] = sum(entry.is_file() for entry in top)
        top_folders = [entry for entry in top if entry.is_dir()]
        profile["immediate_folders"] = len(top_folders)
        profile["top_folders"] = [entry.name[:80] for entry in top_folders[:12]]
        extension_counts: dict[str, int] = {}
        visited = 0
        try:
            for current_root, directories, files in os.walk(folder, followlinks=False):
                depth = len(Path(current_root).relative_to(folder).parts)
                directories[:] = sorted(directories, key=str.casefold)
                if depth >= 2:
                    directories[:] = []
                for filename in sorted(files, key=str.casefold):
                    if visited >= limit:
                        profile["truncated"] = True
                        break
                    visited += 1
                    suffix = Path(filename).suffix.lower() or "[无扩展名]"
                    extension_counts[suffix] = extension_counts.get(suffix, 0) + 1
                if profile["truncated"]:
                    break
        except OSError:
            profile["partially_unreadable"] = True
        profile["sampled_files"] = visited
        profile["extensions"] = dict(sorted(extension_counts.items(), key=lambda pair: (-pair[1], pair[0]))[:12])
        return profile

    def set_category(self, name: str, category: str, manual: bool = True) -> None:
        if category not in {c.id for c in all_categories(self.store)}: raise ValueError("unknown category")
        key = name.lower()
        if manual: self.store.data["overrides"][key] = category
        else: self.store.data["overrides"].pop(key, None)
        # Assigning/resetting is an explicit review action, so this item may now
        # leave the native pending area and be represented by an organizer block.
        self.store.data.setdefault("pending_inbox", {}).pop(key, None)
        self.store.save()

    def rename_item(self, path_text: str, new_label: str) -> dict[str, str]:
        """Rename one desktop item and migrate all filename-keyed presentation data."""
        path = Path(path_text)
        if not path.exists():
            raise ValueError("文件已不存在，请刷新桌面后重试")
        try:
            parent = path.parent.resolve()
            roots = {root.resolve() for root in self.desktop_roots}
        except OSError as exc:
            raise ValueError(f"无法确认文件位置：{exc}") from exc
        if parent not in roots:
            raise ValueError("只能重命名桌面根目录中的文件或文件夹")

        label = str(new_label).strip().rstrip(". ")
        suffix = "" if path.is_dir() else path.suffix
        if suffix and label.casefold().endswith(suffix.casefold()):
            label = label[:-len(suffix)].rstrip()
        if not label:
            raise ValueError("名称不能为空")
        if any(ord(character) < 32 or character in '<>:"/\\|?*' for character in label):
            raise ValueError('名称不能包含 < > : " / \\ | ? * 等字符')
        if len(label + suffix) > 255:
            raise ValueError("名称过长")
        reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
        if label.split(".", 1)[0].upper() in reserved:
            raise ValueError("该名称是 Windows 保留名称")

        target = path.with_name(label + suffix)
        if target.name == path.name:
            return {"old_name": path.name, "new_name": path.name, "path": str(path)}
        if target.exists() and target.name.casefold() != path.name.casefold():
            raise ValueError(f"桌面上已存在“{target.name}”")
        temporary: Path | None = None
        try:
            if target.name.casefold() == path.name.casefold():
                temporary = path.with_name(f".deskflow-rename-{time.time_ns()}{suffix}")
                path.rename(temporary)
                temporary.rename(target)
            else:
                path.rename(target)
        except OSError as exc:
            if temporary and temporary.exists() and not path.exists():
                try: temporary.rename(path)
                except OSError: pass
            raise ValueError(f"Windows 无法重命名该项：{exc}") from exc

        old_key, new_key = path.name.lower(), target.name.lower()
        data = self.store.data
        for field in (
            "overrides", "pending_inbox", "seen", "visual_assets", "markdown_summaries",
            "document_summaries", "folder_summaries", "tip_summaries", "item_metadata",
        ):
            mapping = data.get(field, {})
            if isinstance(mapping, dict) and old_key in mapping:
                mapping[new_key] = mapping.pop(old_key)
        pending = data.get("pending_inbox", {}).get(new_key)
        if isinstance(pending, dict) and "path" in pending:
            pending["path"] = str(target)
        for names in data.get("item_orders", {}).values():
            if isinstance(names, list):
                names[:] = [new_key if name.lower() == old_key else name for name in names]
        self.store.save()
        return {"old_name": path.name, "new_name": target.name, "path": str(target)}

    def promote_new(self, name: str) -> None:
        self.store.data["seen"].setdefault(name.lower(), {})["first_seen_epoch"] = time.time()
        self.store.save()

    @staticmethod
    def low_signal_tip(value: str) -> bool:
        return bool(re.search(r"(?:^|\n)(?:export|空白演示|单击此处添加|公司[（(]|摘要|第一层|\d+\s*项|.*本地桌面软件启动入口)", value or "", re.I))

    @staticmethod
    def shortcut_target(path: Path) -> str:
        return shortcut_target(path)

    @staticmethod
    def software_purpose(executable: Path) -> str | None:
        known = {"unity":"Unity：游戏与交互内容开发", "code":"Visual Studio Code：代码编辑与开发", "cursor":"Cursor：AI 辅助代码开发", "chrome":"Google Chrome：网页浏览", "msedge":"Microsoft Edge：网页浏览", "steam":"Steam：游戏库与启动器", "obs":"OBS Studio：录屏与直播", "potplayer":"PotPlayer：音视频播放", "wps":"WPS Office：文档与演示编辑", "telegram":"Telegram：即时通讯", "clash":"Clash：网络代理管理", "discord":"Discord：社群语音与通讯", "freemind":"FreeMind：思维导图绘制与知识梳理"}
        stem = executable.stem.casefold()
        for token, description in known.items():
            if token in stem: return description
        product = description = ""
        try:
            for codepage in ("040904b0", "080404b0"):
                product = win32api.GetFileVersionInfo(str(executable), fr"\StringFileInfo\{codepage}\ProductName") or product
                description = win32api.GetFileVersionInfo(str(executable), fr"\StringFileInfo\{codepage}\FileDescription") or description
                if product or description: break
        except Exception: pass
        name = (product or executable.stem).strip()[:60]
        if not description or description.strip().casefold() == name.casefold(): return None
        return f"{name}：{description.strip()[:90]}"

    def make_tip_summary(self, path: Path) -> dict[str, Any]:
        target = self.shortcut_target(path)
        if path.suffix.lower() in {".bat", ".cmd", ".ps1"}: target = str(path)
        metadata = self.store.data.get("item_metadata", {}).get(path.name.lower(), {})
        summary = (metadata.get("summary", "") or self.store.data.get("document_summaries", {}).get(path.name.lower(), "") or self.store.data.get("folder_summaries", {}).get(path.name.lower(), "") or self.config_purpose(path))
        launcher = Path(target) if target.lower().endswith((".bat", ".cmd", ".ps1")) else path
        if launcher.suffix.lower() in {".bat", ".cmd", ".ps1"} and not metadata.get("summary"):
            text = self._safe_text(launcher, 4096)
            match = re.search(r'"([^"\r\n]+\.exe)"|\b([A-Za-z]:[^\r\n\s]+\.exe)', text, re.I)
            if match:
                launched = Path(match.group(1) or match.group(2)); purpose = self.software_purpose(launched)
                summary = f"启动 {purpose}" if purpose else f"调用 {launched.stem}"
        actual = Path(target) if target.lower().endswith(".exe") else (path if path.suffix.lower() == ".exe" else None)
        app = self.software_purpose(actual) if actual else ""
        return {"target": target, "summary": summary or "", "app": app or "", "tags": metadata.get("tags", [])}

    def category_catalog(self, groups: dict[str, list[dict[str, Any]]] | None = None) -> list[dict[str, Any]]:
        groups = groups if groups is not None else self.scan()
        names = self.store.data["ui"].get("category_names", {})
        return [
            {
                "id": category.id,
                "title": names.get(category.id, category.title),
                "description": CATEGORY_PURPOSES.get(category.id, "用户自定义分类块"),
                "count": len(groups.get(category.id, [])),
                "mode": self.store.data.get("category_modes", {}).get(category.id, category.mode),
                "sorting": self.store.data.get("category_sorting", {}).get(category.id, "smart"),
                "custom": category.id not in {item.id for item in DEFAULT_CATEGORIES},
            }
            for category in all_categories(self.store)
        ]

    def item_record(self, category: str, item: dict[str, Any], include_path: bool = False) -> dict[str, Any]:
        key = item["name"].lower()
        cached = self.store.data.get("tip_summaries", {}).get(key, {})
        metadata = self.store.data.get("item_metadata", {}).get(key, {})
        record = {
            "name": item["name"],
            "category": category,
            "kind": item["kind"],
            "modified": item["modified"],
            "manual": bool(item.get("manual")),
            "native_pending": bool(item.get("native_pending")),
            "summary": metadata.get("summary") or cached.get("summary") or cached.get("app") or "",
            "tags": metadata.get("tags", []),
        }
        if include_path:
            record.update({"path": item["path"], "target": cached.get("target", ""), "app": cached.get("app", "")})
        return record

    def catalog(self, category: str | None = None, query: str = "", include_items: bool = True,
                include_path: bool = False, limit: int = 300) -> dict[str, Any]:
        groups = self.scan()
        valid = {item.id for item in all_categories(self.store)}
        if category and category not in valid:
            raise ValueError(f"unknown category: {category}")
        needle = query.strip().casefold()
        items: list[dict[str, Any]] = []
        if include_items:
            for group, entries in groups.items():
                if category and group != category:
                    continue
                for item in self.sorted_items(group, entries):
                    record = self.item_record(group, item, include_path=include_path)
                    searchable = " ".join((record["name"], record["summary"], " ".join(record["tags"]))).casefold()
                    if not needle or needle in searchable:
                        items.append(record)
        bounded = max(1, min(500, int(limit)))
        return {
            "categories": self.category_catalog(groups),
            "items": items[:bounded],
            "returned": min(len(items), bounded),
            "total_matches": len(items),
            "truncated": len(items) > bounded,
        }

    def inspect_items(self, names: list[str], refresh: bool = False) -> list[dict[str, Any]]:
        wanted = {str(name).strip().casefold() for name in names if str(name).strip()}
        if not wanted or len(wanted) > 50:
            raise ValueError("provide 1..50 item names")
        if refresh:
            tips = self.store.data.setdefault("tip_summaries", {})
            for key in wanted:
                tips.pop(key, None)
        groups = self.scan()
        matches: dict[str, list[tuple[str, dict[str, Any]]]] = {}
        for category, items in groups.items():
            for item in items:
                if item["name"].casefold() in wanted:
                    matches.setdefault(item["name"].casefold(), []).append((category, item))
        missing = sorted(wanted - set(matches))
        if missing:
            raise ValueError("desktop items not found: " + ", ".join(missing))
        result = []
        for requested in names:
            entries = matches.get(str(requested).strip().casefold(), [])
            for category, item in entries:
                record = self.item_record(category, item, include_path=True)
                record["classification_source"] = "manual" if item.get("manual") else "pending_inbox" if item.get("native_pending") else "automatic"
                record["duplicate_name"] = len(entries) > 1
                item_path = Path(item["path"])
                if item_path.is_dir():
                    record["folder_profile"] = self.folder_profile(item_path)
                result.append(record)
        return result

    @staticmethod
    def _normalise_summary(value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("summary must be a string")
        summary = re.sub(r"\s+", " ", value).strip()
        if len(summary) > 240:
            raise ValueError("summary must be at most 240 characters")
        if re.search(r"(?:sk-|api[_-]?key|token|password|secret)\s*[:=]?\s*[\w-]{12,}", summary, re.I):
            raise ValueError("summary appears to contain a secret")
        return summary

    @staticmethod
    def _normalise_tags(value: Any) -> list[str]:
        if not isinstance(value, list):
            raise ValueError("tags must be an array")
        result: list[str] = []
        for raw in value:
            if not isinstance(raw, str):
                raise ValueError("every tag must be a string")
            tag = re.sub(r"\s+", " ", raw).strip()
            if not tag or len(tag) > 32:
                raise ValueError("tags must contain 1..32 characters")
            if tag.casefold() not in {item.casefold() for item in result}:
                result.append(tag)
        if len(result) > 12:
            raise ValueError("at most 12 tags are allowed per item")
        return result

    def organize_items(self, operations: list[dict[str, Any]], dry_run: bool = False) -> dict[str, Any]:
        if not isinstance(operations, list) or not 1 <= len(operations) <= 200:
            raise ValueError("operations must contain 1..200 items")
        groups = self.scan()
        by_name: dict[str, list[tuple[str, dict[str, Any]]]] = {}
        for category, items in groups.items():
            for item in items:
                by_name.setdefault(item["name"].casefold(), []).append((category, item))
        valid_categories = {item.id for item in all_categories(self.store)}
        prepared: list[dict[str, Any]] = []
        seen_names: set[str] = set()
        for operation in operations:
            if not isinstance(operation, dict) or not isinstance(operation.get("name"), str):
                raise ValueError("every operation requires an item name")
            key = operation["name"].strip().casefold()
            if key in seen_names:
                raise ValueError(f"duplicate operation for: {operation['name']}")
            seen_names.add(key)
            matches = by_name.get(key, [])
            if not matches:
                raise ValueError(f"desktop item not found: {operation['name']}")
            category = operation.get("category")
            reset_category = bool(operation.get("reset_category", False))
            metadata_only = category is None and not reset_category and ("summary" in operation or "tags" in operation)
            if len(matches) != 1 and not metadata_only:
                raise ValueError(f"ambiguous desktop item name: {operation['name']}")
            before_categories = {match_category for match_category, _item in matches}
            before_category = next(iter(before_categories)) if len(before_categories) == 1 else "multiple"
            item = matches[0][1]
            if category is not None and category not in valid_categories:
                raise ValueError(f"unknown category: {category}")
            if category is not None and reset_category:
                raise ValueError("category and reset_category cannot be used together")
            if "tag_mode" in operation and "tags" not in operation:
                raise ValueError("tag_mode requires tags")
            if category is None and not reset_category and "summary" not in operation and "tags" not in operation:
                raise ValueError(f"operation has no changes: {operation['name']}")
            tags = self._normalise_tags(operation["tags"]) if "tags" in operation else None
            tag_mode = operation.get("tag_mode", "replace")
            if tag_mode not in {"replace", "merge", "remove"}:
                raise ValueError("tag_mode must be replace, merge, or remove")
            if tags is not None and tag_mode != "replace":
                existing = self.store.data.get("item_metadata", {}).get(key, {}).get("tags", [])
                if tag_mode == "merge":
                    tags = self._normalise_tags([*existing, *tags])
                else:
                    removed = {tag.casefold() for tag in tags}
                    tags = [tag for tag in existing if tag.casefold() not in removed]
            prepared.append({
                "key": key,
                "name": item["name"],
                "before_category": before_category,
                "category": category,
                "reset_category": reset_category,
                "summary": self._normalise_summary(operation["summary"]) if "summary" in operation else None,
                "has_summary": "summary" in operation,
                "tags": tags,
                "has_tags": "tags" in operation,
                "match_count": len(matches),
            })
        preview = [
            {
                "name": item["name"],
                "from": item["before_category"],
                "to": item["category"] if item["category"] is not None else "automatic" if item["reset_category"] else item["before_category"],
                **({"summary": item["summary"]} if item["has_summary"] else {}),
                **({"tags": item["tags"]} if item["has_tags"] else {}),
                **({"matched_items": item["match_count"]} if item["match_count"] > 1 else {}),
            }
            for item in prepared
        ]
        if dry_run:
            return {"ok": True, "dry_run": True, "changes": preview}
        with self.store.lock:
            metadata = self.store.data.setdefault("item_metadata", {})
            tips = self.store.data.setdefault("tip_summaries", {})
            for item in prepared:
                key = item["key"]
                if item["category"] is not None:
                    self.store.data.setdefault("overrides", {})[key] = item["category"]
                    self.store.data.setdefault("pending_inbox", {}).pop(key, None)
                elif item["reset_category"]:
                    self.store.data.setdefault("overrides", {}).pop(key, None)
                    self.store.data.setdefault("pending_inbox", {}).pop(key, None)
                if item["has_summary"] or item["has_tags"]:
                    entry = metadata.setdefault(key, {})
                    if item["has_summary"]:
                        if item["summary"]: entry["summary"] = item["summary"]
                        else: entry.pop("summary", None)
                    if item["has_tags"]:
                        if item["tags"]: entry["tags"] = item["tags"]
                        else: entry.pop("tags", None)
                    if entry:
                        entry.update({"updated_at": now_iso(), "source": "ai"})
                    else:
                        metadata.pop(key, None)
                    tips.pop(key, None)
            self.store.save()
        after = self.scan()
        final_categories = {item["name"].casefold(): category for category, items in after.items() for item in items}
        for change in preview:
            change["to"] = final_categories.get(change["name"].casefold(), change["to"])
        return {"ok": True, "dry_run": False, "changes": preview}

    def manage_categories(self, actions: list[dict[str, Any]], dry_run: bool = False) -> dict[str, Any]:
        if not isinstance(actions, list) or not 1 <= len(actions) <= 30:
            raise ValueError("actions must contain 1..30 category changes")
        groups = self.scan()
        known = {item.id for item in all_categories(self.store)}
        custom = {item["id"] for item in self.store.data.get("custom_blocks", [])}
        prepared: list[dict[str, Any]] = []
        for action in actions:
            kind = action.get("action") if isinstance(action, dict) else None
            if kind == "create":
                title = re.sub(r"\s+", " ", str(action.get("title", ""))).strip()
                if not 1 <= len(title) <= 40:
                    raise ValueError("category title must contain 1..40 characters")
                color = action.get("color", "#8bd3ff")
                if not isinstance(color, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
                    raise ValueError("category color must be #RRGGBB")
                identifier = "custom-" + hashlib.sha1(f"{title}-{time.time_ns()}".encode()).hexdigest()[:8]
                prepared.append({"action": "create", "category": identifier, "title": title, "color": color})
                known.add(identifier); custom.add(identifier)
            elif kind == "rename":
                identifier = action.get("category")
                title = re.sub(r"\s+", " ", str(action.get("title", ""))).strip()
                if identifier not in known:
                    raise ValueError(f"unknown category: {identifier}")
                if not 1 <= len(title) <= 40:
                    raise ValueError("category title must contain 1..40 characters")
                prepared.append({"action": "rename", "category": identifier, "title": title})
            elif kind == "delete":
                identifier = action.get("category")
                if identifier not in custom:
                    raise ValueError("only custom categories can be deleted")
                if groups.get(identifier):
                    raise ValueError(f"category is not empty: {identifier}")
                prepared.append({"action": "delete", "category": identifier})
                known.discard(identifier); custom.discard(identifier)
            else:
                raise ValueError("action must be create, rename, or delete")
        if dry_run:
            return {"ok": True, "dry_run": True, "changes": prepared}
        with self.store.lock:
            ui = self.store.data["ui"]
            for action in prepared:
                identifier = action["category"]
                if action["action"] == "create":
                    self.store.data.setdefault("custom_blocks", []).append(
                        {"id": identifier, "title": action["title"], "color": action["color"]}
                    )
                    self.store.data.setdefault("category_modes", {})[identifier] = "list"
                elif action["action"] == "rename":
                    ui.setdefault("category_names", {})[identifier] = action["title"]
                else:
                    self.store.data["custom_blocks"] = [
                        block for block in self.store.data.get("custom_blocks", []) if block["id"] != identifier
                    ]
                    self.store.data.get("category_modes", {}).pop(identifier, None)
                    self.store.data.get("category_sorting", {}).pop(identifier, None)
                    self.store.data.get("item_orders", {}).pop(identifier, None)
                    ui.get("category_names", {}).pop(identifier, None)
                    ui.get("panel_styles", {}).pop(identifier, None)
                    for profile in ui.get("layout_profiles", {}).values():
                        profile.get("panel_positions", {}).pop(identifier, None)
                        profile.get("panel_sizes", {}).pop(identifier, None)
            self.store.save()
        return {"ok": True, "dry_run": False, "changes": prepared, "categories": self.category_catalog()}

    @classmethod
    def config_purpose(cls, path: Path) -> str | None:
        """Explain technical/config formats from local, non-secret evidence."""
        suffix = path.suffix.lower()
        fixed = {
            ".ssa": "SSA 字幕文件：存放字幕时间轴与样式，用于视频字幕制作和播放",
            ".ass": "ASS 字幕文件：存放字幕时间轴与样式，用于视频字幕制作和播放",
            ".srt": "SRT 字幕文件：存放字幕时间轴，用于视频字幕播放",
            ".sbsar": "Substance 材质文件：存放可参数化材质，用于 3D 贴图制作",
            ".ini": "应用配置文件：保存软件选项与运行参数",
            ".opt": "软件选项文件：保存运行或工具参数",
        }
        if suffix in fixed: return fixed[suffix]
        if suffix not in {".yaml", ".yml", ".json", ".toml"}: return None
        text = cls._safe_text(path, 8192)
        if not text or re.search(r"(?:sk-|api[_-]?key|token|password|secret)\s*[:=]?\s*[\w-]{12,}", text, re.I): return None
        lowered = text.lower()
        if any(token in lowered for token in ("proxy-providers", "mixed-port", "external-controller", "rule-providers")):
            return "Clash 配置文件：定义网络代理、节点与分流规则"
        if "jobs:" in lowered and re.search(r"^\s*on\s*:", lowered, re.M):
            return "GitHub Actions 工作流：定义自动构建、测试或部署步骤"
        if "services:" in lowered and ("image:" in lowered or "build:" in lowered):
            return "Docker Compose 配置：定义容器服务与启动参数"
        if "apiVersion:".lower() in lowered and "kind:" in lowered:
            return "Kubernetes 配置：定义容器应用的部署资源"
        if "m_dependencies:" in lowered or "productname:" in lowered:
            return "Unity 项目配置：保存工程依赖或编辑器设置"
        name = path.stem.casefold()
        if "clash" in name: return "Clash 配置文件：定义网络代理与分流规则"
        if "docker" in name: return "Docker 配置：定义容器服务与运行参数"
        if "unity" in name: return "Unity 项目配置：保存工程设置"
        return "配置文件：保存软件或项目的运行参数"

    def sorted_items(self, category: str, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Return a stable, meaningful order while preserving explicit user order."""
        mode = self.store.data.setdefault("category_sorting", {}).get(category, "smart")
        def semantic(item: dict[str, Any]) -> tuple[int, str]:
            kind = item["kind"].lower()
            # Similar entry kinds stay together; this is deliberately based on
            # real file type rather than guessing from a display name.
            group = (0 if kind == "folder" else 1 if kind in {"lnk", "url", "exe", "bat", "cmd"}
                     else 2 if kind in {"py", "cpp", "h", "md", "json", "yaml", "yml"}
                     else 3 if kind in {"ppt", "pptx", "doc", "docx", "xls", "xlsx", "pdf"} else 4)
            return group, Path(item["name"]).stem.casefold()
        if mode == "name": return sorted(items, key=lambda item: item["name"].casefold())
        if mode == "modified": return sorted(items, key=lambda item: item["modified"], reverse=True)
        if mode == "custom":
            order = self.store.data.setdefault("item_orders", {}).get(category, [])
            rank = {name: index for index, name in enumerate(order)}
            return sorted(items, key=lambda item: (rank.get(item["name"].lower(), len(rank)), semantic(item)))
        return sorted(items, key=semantic)

    def reorder_item(self, category: str, name: str, before: str | None) -> None:
        """Persist a manual order; names absent from it retain their smart order."""
        current = [item["name"].lower() for item in self.sorted_items(category, self.scan().get(category, []))]
        key = name.lower(); current = [item for item in current if item != key]
        if before and before.lower() in current: current.insert(current.index(before.lower()), key)
        else: current.append(key)
        self.store.data.setdefault("item_orders", {})[category] = current
        self.store.data.setdefault("category_sorting", {})[category] = "custom"
        self.store.save()


class ControlService:
    def __init__(self, model: OrganizerModel, events: queue.Queue[dict[str, Any]]) -> None:
        self.model, self.events = model, events
        self.app = FastAPI(title=APP_NAME, docs_url=None, redoc_url=None)
        self._routes()

    def _routes(self) -> None:
        @self.app.get("/health")
        def health(): return {"ok": True, "pid": os.getpid(), "time": now_iso(), "desktop": str(self.model.desktop)}

        @self.app.get("/layout")
        def layout(): return {"categories": self.model.scan(), "config": self.model.store.data}

        @self.app.post("/catalog")
        def catalog(payload: dict[str, Any]):
            try:
                return self.model.catalog(
                    category=payload.get("category"),
                    query=str(payload.get("query", "")),
                    include_items=bool(payload.get("include_items", True)),
                    include_path=bool(payload.get("include_path", False)),
                    limit=payload.get("limit", 300),
                )
            except (TypeError, ValueError) as exc:
                raise HTTPException(400, str(exc)) from exc

        @self.app.post("/inspect-items")
        def inspect_items(payload: dict[str, Any]):
            try:
                names = payload.get("names", [])
                if not isinstance(names, list):
                    raise ValueError("names must be an array")
                return {"ok": True, "items": self.model.inspect_items(names, bool(payload.get("refresh", False)))}
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc

        @self.app.post("/organize-batch")
        def organize_batch(payload: dict[str, Any]):
            try:
                result = self.model.organize_items(payload.get("operations", []), bool(payload.get("dry_run", False)))
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
            if not result["dry_run"]:
                categories = {
                    value for change in result["changes"] for value in (change.get("from"), change.get("to"))
                    if value and value != "automatic"
                }
                self.events.put({"type": "assign", "categories": sorted(categories)})
            return result

        @self.app.post("/categories")
        def categories(payload: dict[str, Any]):
            try:
                result = self.model.manage_categories(payload.get("actions", []), bool(payload.get("dry_run", False)))
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
            if not result["dry_run"]:
                self.events.put({"type": "render"})
            return result

        @self.app.post("/rescan")
        def rescan():
            self.events.put({"type": "render"}); return {"ok": True, "categories": self.model.scan()}

        @self.app.post("/assign")
        def assign(payload: dict[str, str]):
            name, category = payload.get("name"), payload.get("category")
            if not name or not category: raise HTTPException(400, "name and category are required")
            before = next((group for group, items in self.model.scan().items()
                           if any(item["name"].casefold() == name.casefold() for item in items)), None)
            self.model.set_category(name, category, manual=True)
            self.events.put({"type": "assign", "categories": [value for value in (before, category) if value]})
            return {"ok": True, "name": name, "category": category, "manual": True}

        @self.app.post("/set-sorting")
        def set_sorting(payload: dict[str, str]):
            category, mode = payload.get("category"), payload.get("mode")
            if category not in {item.id for item in all_categories(self.model.store)} or mode not in {"smart", "name", "modified", "custom"}:
                raise HTTPException(400, "valid category and mode are required")
            self.model.store.data.setdefault("category_sorting", {})[category] = mode
            self.model.store.save(); self.events.put({"type": "panel-mode", "category": category})
            return {"ok": True, "category": category, "mode": mode}

        @self.app.post("/panel-mode")
        def panel_mode(payload: dict[str, str]):
            category, mode = payload.get("category"), payload.get("mode")
            if category not in {item.id for item in all_categories(self.model.store)} or mode not in {"list", "compact", "cards"}:
                raise HTTPException(400, "valid category and mode are required")
            self.model.store.data.setdefault("category_modes", {})[category] = mode
            self.model.store.save(); self.events.put({"type": "panel-mode", "category": category})
            return {"ok": True, "category": category, "mode": mode}

        @self.app.post("/reorder-item")
        def reorder_item(payload: dict[str, str]):
            category, name, before = payload.get("category"), payload.get("name"), payload.get("before")
            if category not in {item.id for item in all_categories(self.model.store)} or not name:
                raise HTTPException(400, "valid category and name are required")
            self.model.reorder_item(category, name, before)
            self.events.put({"type": "panel-mode", "category": category})
            return {"ok": True, "category": category, "name": name, "before": before}

        @self.app.post("/settings")
        def settings(payload: dict[str, Any]):
            allowed = {"cell_size", "layout_footprint", "font_size", "icon_size", "alpha", "background", "auto_contrast", "geometry", "target_monitor"}
            ui = self.model.store.data["ui"]
            ui.update({k: v for k, v in payload.items() if k in allowed})
            if "target_monitor" in payload:
                # The API thread cannot enumerate/reconcile GUI monitor state.
                # Mark this as an explicit user choice; the UI thread records
                # its count/index/device before applying the next layout.
                ui["monitor_selection_pending"] = True
            if "cell_size" in payload:
                # Cell size governs density; its icon derivation remains
                # internal, while text has a deliberate readability override.
                cell_size = max(44, min(72, int(payload["cell_size"])))
                ui["cell_size"] = cell_size
                ui["icon_size"] = max(20, min(32, round(cell_size * .48)))
            if "font_size" in payload:
                ui["font_size"] = max(8, min(14, int(payload["font_size"])))
            if "layout_footprint" in payload:
                ui["layout_footprint"] = max(.45, min(.95, float(payload["layout_footprint"])))
            if "alpha" in payload:
                # This is plate opacity only in the Qt renderer.  Text and
                # icons are never multiplied by this value.
                ui["alpha"] = max(.08, min(.70, float(payload["alpha"])))
            self.model.store.save()
            self.events.put({"type": "arrange" if {"cell_size", "layout_footprint"} & payload.keys() else "render"})
            return {"ok": True, "ui": self.model.store.data["ui"]}

        @self.app.post("/panel-style")
        def panel_style(payload: dict[str, Any]):
            category = payload.get("category")
            if category not in {item.id for item in all_categories(self.model.store)}:
                raise HTTPException(400, "valid category is required")
            changes = {key: payload[key] for key in ("background", "alpha") if key in payload}
            if "background" in changes and (not isinstance(changes["background"], str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", changes["background"])):
                raise HTTPException(400, "background must be #RRGGBB")
            if "alpha" in changes and (not isinstance(changes["alpha"], (int, float)) or not .08 <= changes["alpha"] <= .70):
                raise HTTPException(400, "alpha must be 0.08..0.70")
            style = self.model.store.data["ui"].setdefault("panel_styles", {}).setdefault(category, {})
            if payload.get("reset"):
                style.clear()
            style.update(changes); self.model.store.save()
            self.events.put({"type": "panel-style", "category": category})
            return {"ok": True, "category": category, "style": style}

        @self.app.post("/open-settings")
        def open_settings():
            self.events.put({"type": "settings"})
            return {"ok": True}

        @self.app.post("/show-native-icons")
        def show_native_icons():
            self.events.put({"type": "close"})
            return {"ok": True, "message": "DESKFLOW will close and restore Explorer icons."}

        @self.app.post("/arrange-top-right")
        def arrange_top_right():
            self.events.put({"type": "arrange"})
            return {"ok": True, "message": "Panels will move to the selected monitor's top-right layout."}

        @self.app.post("/move-panel")
        def move_panel(payload: dict[str, Any]):
            category, x, y = payload.get("category"), payload.get("x"), payload.get("y")
            if category not in {item.id for item in all_categories(self.model.store)} or not isinstance(x, int) or not isinstance(y, int):
                raise HTTPException(400, "category plus integer x/y are required")
            self.events.put({"type": "move", "category": category, "x": x, "y": y})
            return {"ok": True, "category": category, "x": x, "y": y}

    def serve(self) -> None:
        # Uvicorn normally registers OS signal handlers.  This service runs in
        # a background thread so Tk remains the Windows UI main thread.
        # pythonw deliberately has no console streams.  Disable Uvicorn's
        # default console log setup so the local control service still starts
        # when DESKFLOW is launched from the silent tray entry point.
        config = uvicorn.Config(self.app, host="127.0.0.1", port=8765,
                                log_level="warning", log_config=None, access_log=False)
        server = uvicorn.Server(config)
        server.install_signal_handlers = lambda: None  # type: ignore[method-assign]
        server.run()


class DesktopShell:
    """Attach blocks while preserving Explorer's native system/inbox icons."""
    SW_HIDE, SW_SHOW = 0, 5
    def __init__(self) -> None:
        self.user32 = ctypes.windll.user32
        self.kernel32 = ctypes.windll.kernel32
        self.listview = self._find_listview()
        self.host = self._find_overlay_host()
        self.hidden = False
        self.panel_windows: dict[int, int] = {}
        self.attribute_state: dict[str, int] = self._load_attribute_state()
        self.guard: subprocess.Popen[Any] | None = None
        # Recover a prior forced/crashed session before establishing this one.
        if self.attribute_state:
            self._restore_attributes()
        atexit.register(self.restore_icons)

    @staticmethod
    def _load_attribute_state() -> dict[str, int]:
        try:
            raw = json.loads(NATIVE_ICON_STATE_PATH.read_text(encoding="utf-8"))
            return {str(path): int(attributes) for path, attributes in raw.items()}
        except (OSError, ValueError, json.JSONDecodeError):
            return {}

    def _save_attribute_state(self) -> None:
        NATIVE_ICON_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        temporary = NATIVE_ICON_STATE_PATH.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.attribute_state, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(NATIVE_ICON_STATE_PATH)

    @staticmethod
    def _notify_desktop_changed(paths: list[str]) -> None:
        # Attribute changes are local to these entries. ASSOCCHANGED flushes
        # the system icon/thumbnail cache and makes unrelated icons go blank.
        for path in paths:
            try: shell.SHChangeNotify(shellcon.SHCNE_ATTRIBUTES, shellcon.SHCNF_PATHW, path, None)
            except Exception: pass

    def _restore_attributes(self, paths: set[str] | None = None) -> None:
        invalid = 0xFFFFFFFF
        restored: list[str] = []
        selected = set(self.attribute_state) if paths is None else paths
        for raw_path in list(selected):
            attributes = self.attribute_state.get(raw_path)
            if attributes is None: continue
            if Path(raw_path).exists() and self.kernel32.GetFileAttributesW(raw_path) != invalid:
                current = self.kernel32.GetFileAttributesW(raw_path)
                if current != int(attributes) and not self.kernel32.SetFileAttributesW(raw_path, int(attributes)):
                    continue  # Keep recovery evidence when Windows denies the write.
                if current != int(attributes): restored.append(raw_path)
            self.attribute_state.pop(raw_path, None)
        self._save_attribute_state()
        if restored: self._notify_desktop_changed(restored)

    def _ensure_guard(self) -> None:
        if not self.attribute_state or (self.guard and self.guard.poll() is None): return
        guard_script = Path(__file__).with_name("native_icon_guard.py")
        try:
            self.guard = subprocess.Popen(
                command("guard", "--parent", os.getpid(), "--state", NATIVE_ICON_STATE_PATH),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError:
            self.guard = None

    def _find_listview(self) -> int:
        progman = self.user32.FindWindowW("Progman", None)
        defview = self.user32.FindWindowExW(progman, 0, "SHELLDLL_DefView", None)
        if not defview:
            worker = 0
            while True:
                worker = self.user32.FindWindowExW(0, worker, "WorkerW", None)
                if not worker: break
                defview = self.user32.FindWindowExW(worker, 0, "SHELLDLL_DefView", None)
                if defview: break
        return self.user32.FindWindowExW(defview, 0, "SysListView32", "FolderView") if defview else 0

    def _find_overlay_host(self) -> int:
        """Return Explorer's visible desktop view, never an application top-level."""
        progman = self.user32.FindWindowW("Progman", None)
        if not progman: return 0
        defview = self.user32.FindWindowExW(progman, 0, "SHELLDLL_DefView", None)
        if not defview:
            worker = 0
            while True:
                worker = self.user32.FindWindowExW(0, worker, "WorkerW", None)
                if not worker: break
                defview = self.user32.FindWindowExW(worker, 0, "SHELLDLL_DefView", None)
                if defview: break
        if not defview: return 0
        # This desktop session has no full-screen WorkerW wallpaper host; its
        # WorkerW windows are taskbar-sized.  DefView is the only valid desktop
        # container available for this process.
        return defview

    def hide_icons(self, organized_paths: list[str]) -> None:
        """Hide only organized filesystem entries; keep native virtual/new icons."""
        self.organized_native_paths = list(organized_paths)
        self.listview = self._find_listview()
        if self.listview: self.user32.ShowWindow(self.listview, self.SW_SHOW)
        wanted = {str(Path(path)) for path in organized_paths}
        obsolete = set(self.attribute_state) - wanted
        if obsolete: self._restore_attributes(obsolete)
        invalid = 0xFFFFFFFF
        additions: list[tuple[str, int]] = []
        for raw_path in wanted - set(self.attribute_state):
            attributes = self.kernel32.GetFileAttributesW(raw_path)
            if attributes != invalid: additions.append((raw_path, int(attributes)))
        if additions:
            # Persist the exact original attributes before changing any item.
            self.attribute_state.update(additions); self._save_attribute_state()
        changed: list[str] = []
        self.attribute_errors: dict[str, int] = {}
        hidden_count = 0
        for raw_path in wanted & set(self.attribute_state):
            current = self.kernel32.GetFileAttributesW(raw_path)
            if current == invalid: continue
            if current & 0x06 == 0x06:
                hidden_count += 1
            elif self.kernel32.SetFileAttributesW(raw_path, current | 0x06):
                changed.append(raw_path)
                hidden_count += 1
            else:
                self.attribute_errors[raw_path] = int(self.kernel32.GetLastError())
        if changed: self._notify_desktop_changed(changed)
        self.hidden = bool(hidden_count)
        self._ensure_guard()

    def retry_icon_attributes(self) -> None:
        if getattr(self, 'attribute_errors', None):
            self.hide_icons(self.organized_native_paths)

    def restore_icons(self) -> None:
        if self.attribute_state: self._restore_attributes()
        self.listview = self._find_listview()
        if self.listview: self.user32.ShowWindow(self.listview, self.SW_SHOW)
        self.hidden = False

    def cursor_position(self) -> tuple[int, int]:
        point = ctypes.wintypes.POINT()
        self.user32.GetCursorPos(ctypes.byref(point))
        return point.x, point.y

    def left_button_down(self) -> bool:
        return bool(self.user32.GetAsyncKeyState(0x01) & 0x8000)

    def pointer_over_listview(self) -> bool:
        """True only when a drag began on Explorer's native desktop surface."""
        self.listview = self._find_listview()
        if not self.listview: return False
        point = ctypes.wintypes.POINT(*self.cursor_position())
        hwnd = self.user32.WindowFromPoint(point)
        while hwnd:
            if hwnd == self.listview: return True
            hwnd = self.user32.GetParent(hwnd)
        return False

    def selected_native_labels(self) -> list[str]:
        """Read only Explorer's selected ListView labels; never alter its layout."""
        self.listview = self._find_listview()
        if not self.listview: return []
        pid = ctypes.wintypes.DWORD()
        self.user32.GetWindowThreadProcessId(self.listview, ctypes.byref(pid))
        process = self.kernel32.OpenProcess(0x0008 | 0x0010 | 0x0020 | 0x0400, False, pid.value)
        if not process: return []
        class LVITEMW(ctypes.Structure):
            _fields_ = [("mask", ctypes.wintypes.UINT), ("iItem", ctypes.c_int), ("iSubItem", ctypes.c_int),
                       ("state", ctypes.wintypes.UINT), ("stateMask", ctypes.wintypes.UINT), ("pszText", ctypes.c_void_p),
                       ("cchTextMax", ctypes.c_int), ("iImage", ctypes.c_int), ("lParam", ctypes.c_ssize_t),
                       ("iIndent", ctypes.c_int), ("iGroupId", ctypes.c_int), ("cColumns", ctypes.wintypes.UINT),
                       ("puColumns", ctypes.c_void_p), ("piColFmt", ctypes.c_void_p), ("iGroup", ctypes.c_int)]
        self.kernel32.VirtualAllocEx.restype = ctypes.c_void_p
        remote = self.kernel32.VirtualAllocEx(process, None, 2048, 0x1000 | 0x2000, 0x04)
        if not remote:
            self.kernel32.CloseHandle(process); return []
        labels: list[str] = []; index = -1
        try:
            while True:
                index = self.user32.SendMessageW(self.listview, 0x100C, index, 0x0002)  # LVM_GETNEXTITEM / LVNI_SELECTED
                if index < 0: break
                row = LVITEMW(mask=1, iItem=index, pszText=remote + ctypes.sizeof(LVITEMW), cchTextMax=512)
                self.kernel32.WriteProcessMemory(process, remote, ctypes.byref(row), ctypes.sizeof(row), None)
                self.user32.SendMessageW(self.listview, 0x1073, index, remote)  # LVM_GETITEMTEXTW
                buffer = ctypes.create_unicode_buffer(512)
                self.kernel32.ReadProcessMemory(process, remote + ctypes.sizeof(LVITEMW), buffer, ctypes.sizeof(buffer), None)
                if buffer.value: labels.append(buffer.value)
        finally:
            self.kernel32.VirtualFreeEx(process, remote, 0, 0x8000)
            self.kernel32.CloseHandle(process)
        return labels

    def attach(self, window_id: int) -> bool:
        """Place a real-alpha desktop popup below normal application windows."""
        if not self.host: return False
        # Tk's winfo_id is its client child.  Re-parenting that child leaves the
        # enclosing TkTopLevel unaffected, so take its real root.
        outer_window = self.user32.GetAncestor(window_id, 2) or window_id  # GA_ROOT
        # DefView can itself be a top-level desktop window (or live below a
        # WorkerW).  Its root is the stable owner; GetParent(DefView) is not.
        desktop_owner = self.user32.GetAncestor(self.host, 2) or self.host  # GA_ROOT
        if not desktop_owner: return False
        set_owner = getattr(self.user32, "SetWindowLongPtrW", self.user32.SetWindowLongW)
        set_owner.restype = ctypes.c_ssize_t
        set_owner(outer_window, -8, desktop_owner)  # GWLP_HWNDPARENT / owner
        style = self.user32.GetWindowLongW(outer_window, -16) & 0xFFFFFFFF
        popup_style = (style & ~0x40000000) | 0x80000000  # WS_CHILD -> WS_POPUP
        if popup_style >= 0x80000000: popup_style -= 0x100000000
        self.user32.SetWindowLongW(outer_window, -16, popup_style)
        # A desktop block accepts pointer input but must never become the
        # foreground top-level window.  Without WS_EX_NOACTIVATE, clicking a
        # title bar to drag it promotes the popup above the user's editor or
        # browser until the next maintenance tick.
        ex_style = self.user32.GetWindowLongW(outer_window, -20) & 0xFFFFFFFF
        no_activate_style = ex_style | 0x08000000  # WS_EX_NOACTIVATE
        if no_activate_style >= 0x80000000: no_activate_style -= 0x100000000
        self.user32.SetWindowLongW(outer_window, -20, no_activate_style)
        # `-alpha` intentionally leaves WS_EX_LAYERED on.  Unlike a layered
        # child of DefView, this popup is composed by DWM and remains truly
        # translucent.  Insert directly above the desktop owner's z-band.
        self.user32.SetWindowPos(outer_window, desktop_owner, 0, 0, 0, 0, 0x0003 | 0x0010 | 0x0020 | 0x0200)
        if self.user32.GetWindow(outer_window, 4) != desktop_owner: return False  # GW_OWNER
        self.panel_windows[window_id] = outer_window
        return True

    def keep_panels_above_desktop_backdrop(self) -> None:
        """Keep embedded panels above a live-wallpaper compositor, not apps.

        Some wallpaper managers add a full-screen ATL child to DefView after
        our panels are created.  It silently takes the first sibling Z-order
        slot and covers every panel while the organizer process remains alive.
        Move only that desktop-local compositor below the panels, then restore
        the panels' sibling order.  All windows remain DefView children, so
        normal application windows are still above the organizer.
        """
        if not self.host or not self.panel_windows: return
        desktop_owner = self.user32.GetAncestor(self.host, 2) or self.host
        if not desktop_owner: return
        flags = 0x0003 | 0x0010 | 0x0200  # no move/size/activate/owner-z-order
        for window in self.panel_windows.values():
            self.user32.SetWindowPos(window, desktop_owner, 0, 0, 0, 0, flags)

    def panel_is_desktop_child(self, window_id: int) -> bool:
        outer_window = self.panel_windows.get(window_id)
        desktop_owner = (self.user32.GetAncestor(self.host, 2) or self.host) if self.host else 0
        return bool(outer_window and desktop_owner and self.user32.GetWindow(outer_window, 4) == desktop_owner)

    def panel_is_visible(self, window_id: int) -> bool:
        rect = ctypes.wintypes.RECT()
        outer_window = self.panel_windows.get(window_id, window_id)
        return bool(self.user32.IsWindowVisible(outer_window) and self.user32.GetWindowRect(outer_window, ctypes.byref(rect)) and rect.right > rect.left and rect.bottom > rect.top)

    def monitors(self) -> list[dict[str, Any]]:
        """Return each monitor's usable work area, excluding its taskbar."""
        monitors: list[dict[str, Any]] = []
        for handle, _dc, _rect in win32api.EnumDisplayMonitors():
            info = win32api.GetMonitorInfo(handle)
            screen_left, screen_top, screen_right, screen_bottom = info["Monitor"]
            left, top, right, bottom = info["Work"]
            monitors.append({"handle": int(handle), "left": left, "top": top, "right": right, "bottom": bottom,
                             "screen_left": screen_left, "screen_top": screen_top,
                             "screen_right": screen_right, "screen_bottom": screen_bottom,
                             "device": str(info.get("Device", "")),
                             "primary": bool(info["Flags"] & 1)})
        return monitors or [{"left": 0, "top": 0, "right": self.user32.GetSystemMetrics(0), "bottom": self.user32.GetSystemMetrics(1), "primary": True}]

    @staticmethod
    def _monitor_from_preference(all_monitors: list[dict[str, Any]], preference: str) -> tuple[dict[str, Any], int]:
        """Resolve current API input, including the old monitor-index format."""
        primary_index = next((index for index, item in enumerate(all_monitors) if item.get("primary")), 0)
        if preference == "primary":
            return all_monitors[primary_index], primary_index
        if preference.startswith("monitor:"):
            raw = preference.split(":", 1)[1]
            if raw.isdigit():
                handle = int(raw)
                exact = next(((item, index) for index, item in enumerate(all_monitors) if item.get("handle") == handle), None)
                if exact:
                    return exact
        if preference.startswith("monitor:"):
            try:
                index = int(preference.split(":", 1)[1])
                if 0 <= index < len(all_monitors):
                    return all_monitors[index], index
            except ValueError:
                pass
        return all_monitors[primary_index], primary_index

    def target_monitor(self, preference: str) -> dict[str, Any]:
        all_monitors = self.monitors()
        return self._monitor_from_preference(all_monitors, preference)[0]

    def remember_target_monitor(self, ui: dict[str, Any], preference: str) -> bool:
        """Record an explicit choice separately from its current HMONITOR."""
        all_monitors = self.monitors()
        selected, index = self._monitor_from_preference(all_monitors, preference)
        explicit_primary = preference == "primary"
        effective = "primary" if explicit_primary else f"monitor:{int(selected.get('handle', index))}"
        values = {
            "target_monitor": effective,
            "preferred_monitor": "primary" if explicit_primary else effective,
            "preferred_monitor_count": len(all_monitors),
            "preferred_monitor_index": index,
            "preferred_monitor_device": str(selected.get("device", "")),
            "monitor_fallback_active": False,
        }
        changed = any(ui.get(key) != value for key, value in values.items())
        ui.update(values)
        ui.pop("monitor_selection_pending", None)
        return changed

    def reconcile_target_monitor(self, ui: dict[str, Any]) -> bool:
        """Choose a visible effective screen without overwriting user intent.

        A non-primary choice records the display count at selection time.  RDP
        sessions exposing fewer displays temporarily use the primary screen.
        Once the original count returns, the saved device/index selects the
        corresponding screen even though Windows has issued new HMONITORs.
        """
        if ui.pop("monitor_selection_pending", False):
            return self.remember_target_monitor(ui, str(ui.get("target_monitor", "primary")))

        all_monitors = self.monitors()
        preferred = str(ui.get("preferred_monitor") or ui.get("target_monitor", "primary"))
        updates: dict[str, Any] = {"preferred_monitor": preferred}
        if preferred == "primary":
            updates.update({"target_monitor": "primary", "monitor_fallback_active": False})
        else:
            try:
                selected_count = max(0, int(ui.get("preferred_monitor_count", 0)))
            except (TypeError, ValueError):
                selected_count = 0
            selected: dict[str, Any] | None = None
            selected_index = -1
            if selected_count and len(all_monitors) < selected_count:
                updates.update({"target_monitor": "primary", "monitor_fallback_active": True})
            else:
                device = str(ui.get("preferred_monitor_device", ""))
                if device:
                    match = next(((item, index) for index, item in enumerate(all_monitors)
                                  if item.get("device") == device), None)
                    if match:
                        selected, selected_index = match
                if selected is None:
                    try:
                        index = int(ui.get("preferred_monitor_index", -1))
                    except (TypeError, ValueError):
                        index = -1
                    if 0 <= index < len(all_monitors):
                        selected, selected_index = all_monitors[index], index
                if selected is None:
                    resolved, index = self._monitor_from_preference(all_monitors, preferred)
                    # A missing legacy handle resolves to primary, but that is
                    # a temporary safety fallback rather than a new preference.
                    raw = preferred.split(":", 1)[1] if preferred.startswith("monitor:") else ""
                    if raw.isdigit() and any(item.get("handle") == int(raw) for item in all_monitors):
                        selected, selected_index = resolved, index
                if selected is None:
                    updates.update({"target_monitor": "primary", "monitor_fallback_active": True})
                else:
                    updates.update({
                        "target_monitor": f"monitor:{int(selected.get('handle', selected_index))}",
                        "monitor_fallback_active": False,
                    })
        changed = any(ui.get(key) != value for key, value in updates.items())
        ui.update(updates)
        return changed


class OrganizerUI:
    def __init__(self, model: OrganizerModel, events: queue.Queue[dict[str, Any]]) -> None:
        self.model, self.events = model, events
        ui = model.store.data["ui"]
        self.last_work_bounds: tuple[int, int, int, int] | None = None
        self.root = tk.Tk(className=APP_NAME)
        self.root.withdraw()  # no primary app window: only desktop-owned blocks are visible
        self.panels: dict[str, tk.Toplevel] = {}
        self.settings_dialog: tk.Toplevel | None = None
        self.native_icons: dict[tuple[str, int], ImageTk.PhotoImage] = {}
        self.item_widgets: dict[str, list[tuple[str, tk.Widget]]] = {}
        self.panel_content: dict[str, tuple[tk.Frame, Category, str, str]] = {}
        self.panel_counts: dict[str, tk.Label] = {}
        self.item_drag: dict[str, Any] | None = None
        self.selected_item: tuple[str, str] | None = None
        self.selected_container: tk.Widget | None = None
        self.group_snapshot: dict[str, tuple[tuple[str, str, bool], ...]] = {}
        self.last_desktop_signature: tuple[tuple[str, int, int], ...] = ()
        self.native_mouse_down = False
        self.native_drag: dict[str, Any] | None = None
        self.active_canvas: tk.Canvas | None = None
        self.root.bind_all("<MouseWheel>", self.mouse_wheel, add="+")
        self.shell = DesktopShell()
        self.drag_x = self.drag_y = 0
        if self.shell.reconcile_target_monitor(ui):
            self.model.store.save()
        self.render()
        self.last_desktop_signature = self.model.desktop_signature()
        self.root.after(1200, self.tick)
        self.root.after(75, self.watch_native_drag)

    def tick(self) -> None:
        # This is geometry/Z-order only; it neither recreates cards nor causes
        # hover/layout redraws.  It covers wallpaper managers that reassert
        # their DefView child after the organizer has started.
        self.shell.keep_panels_above_desktop_backdrop()
        rerender = False
        arrange_requested = False
        display_recovery = False
        changed_categories: set[str] = set()
        while True:
            try: event = self.events.get_nowait()
            except queue.Empty: break
            if event.get("type") == "close": self.close(); return
            # Do not return from this callback after a layout operation.  A
            # return used to stop scheduling ``tick`` forever, so tray moves
            # and all later RDP recovery requests were accepted by the API but
            # never applied by the Tk thread.
            if event.get("type") == "arrange": arrange_requested = True; continue
            if event.get("type") == "panel-style": self.render(event["category"]); continue
            if event.get("type") == "panel-mode": self.render(event["category"]); continue
            if event.get("type") == "move": self.move_panel(event["category"], event["x"], event["y"]); continue
            if event.get("type") == "settings": self.settings(); continue
            if event.get("type") == "assign":
                changed_categories.update(event.get("categories", [])); continue
            rerender = rerender or event.get("type") == "render"
        ui = self.model.store.data["ui"]
        monitor_changed = self.shell.reconcile_target_monitor(ui)
        if monitor_changed:
            self.model.store.save()
            arrange_requested = True; display_recovery = True
        monitor = self.shell.target_monitor(ui.get("target_monitor", "primary"))
        work_bounds = (monitor["left"], monitor["top"], monitor["right"], monitor["bottom"])
        # A changed work area (RDP resolution, display add/remove or taskbar)
        # always needs a fresh bounded plan, never old profile coordinates.
        if self.last_work_bounds is not None and work_bounds != self.last_work_bounds:
            arrange_requested = True; display_recovery = True
        signature = self.model.desktop_signature()
        if signature != self.last_desktop_signature:
            groups = self.model.scan(); snapshot = self.snapshot_groups(groups)
            changed_categories.update(category for category in set(snapshot) | set(self.group_snapshot)
                                      if snapshot.get(category) != self.group_snapshot.get(category))
            self.last_desktop_signature = signature
            # Inbox changes are rendered by Explorer, not by a DESKFLOW block.
            self.shell.hide_icons(self.organized_paths(groups))
        if rerender and not arrange_requested:
            self.render()
        elif not arrange_requested:
            for category in changed_categories:
                if category != "inbox": self.render(category)
        if arrange_requested:
            # A resolution/display switch is the one deliberate whole-layer
            # recovery: recreating the desktop children makes Tk pick up the
            # current RDP DPI context, then the same bounded planner used by
            # the toolbar, MCP and tray applies the geometry.
            self.arrange_top_right(rebuild=rerender or display_recovery)
        self.root.after(1200, self.tick)

    @staticmethod
    def snapshot_groups(groups: dict[str, list[dict[str, Any]]]) -> dict[str, tuple[tuple[str, str, bool], ...]]:
        return {category: tuple((item["name"], item["modified"], bool(item.get("manual"))) for item in items)
                for category, items in groups.items()}

    @staticmethod
    def organized_paths(groups: dict[str, list[dict[str, Any]]]) -> list[str]:
        return [item["path"] for category, items in groups.items() if category != "inbox" for item in items]

    def text_color(self, background: str | None = None) -> str:
        bg = (background or self.model.store.data["ui"]["background"]).lstrip("#")
        try: lum = sum(int(bg[i:i+2], 16) * w for i, w in zip((0,2,4), (0.299,0.587,0.114)))
        except ValueError: lum = 0
        return "#101827" if self.model.store.data["ui"]["auto_contrast"] and lum > 165 else "#ffffff"

    def cell_size(self) -> int:
        ui = self.model.store.data["ui"]
        # Existing layout JSONs predate the unified density control.  Derive a
        # sensible compact value once without overwriting the user's file.
        raw = ui.get("cell_size", int(ui.get("icon_size", 28)) + 30)
        return max(44, min(72, int(raw)))

    def layout_footprint(self) -> float:
        return max(.50, min(.75, float(self.model.store.data["ui"].get("layout_footprint", .62))))

    @staticmethod
    def tint(base: str, accent: str, amount: float) -> str:
        """Blend a restrained category hue into a dark desktop surface."""
        source = tuple(int(base[index:index + 2], 16) for index in (1, 3, 5))
        target = tuple(int(accent[index:index + 2], 16) for index in (1, 3, 5))
        return "#" + "".join(f"{round(a + (b - a) * amount):02x}" for a, b in zip(source, target))

    def round_panel(self, panel: tk.Toplevel) -> None:
        if panel.winfo_width() < 20 or panel.winfo_height() < 20: return
        try:
            region = win32gui.CreateRoundRectRgn(0, 0, panel.winfo_width() + 1, panel.winfo_height() + 1, 22, 22)
            win32gui.SetWindowRgn(panel.winfo_id(), region, True)
        except win32gui.error:
            pass

    @staticmethod
    def setup_edge_scrollbar(canvas: tk.Canvas, scrollbar: tk.Scrollbar, body: tk.Frame) -> None:
        """Show a scrollbar only for overflow and only near the right edge."""
        state = {"needed": False, "shown": False, "hide_job": None}

        def cancel_hide(_event=None):
            if state["hide_job"]:
                canvas.after_cancel(state["hide_job"]); state["hide_job"] = None

        def hide_now():
            state["hide_job"] = None
            if state["shown"]:
                scrollbar.pack_forget(); state["shown"] = False

        def schedule_hide(_event=None):
            cancel_hide()
            if state["shown"]:
                state["hide_job"] = canvas.after(280, hide_now)

        def show_now():
            cancel_hide()
            if state["needed"] and not state["shown"]:
                scrollbar.pack(side="right", fill="y", pady=(0, 10), padx=(0, 5)); state["shown"] = True

        def refresh(_event=None):
            box = canvas.bbox("all")
            content_height = (box[3] - box[1]) if box else 0
            state["needed"] = content_height > canvas.winfo_height() + 2
            if not state["needed"]:
                cancel_hide(); hide_now()

        def on_canvas_motion(event):
            if state["needed"] and event.x >= canvas.winfo_width() - 18:
                show_now()
            elif state["shown"]:
                schedule_hide()

        def on_canvas_resize(event):
            canvas.itemconfigure(body.deskflow_window, width=event.width)  # type: ignore[attr-defined]
            canvas.after_idle(refresh)

        body.deskflow_window = canvas.create_window((0, 0), window=body, anchor="nw")  # type: ignore[attr-defined]
        body.deskflow_refresh_scrollbar = refresh  # type: ignore[attr-defined]
        body.bind("<Configure>", lambda _event: (canvas.configure(scrollregion=canvas.bbox("all")), canvas.after_idle(refresh)), add="+")
        canvas.bind("<Configure>", on_canvas_resize, add="+")
        canvas.bind("<Motion>", on_canvas_motion, add="+")
        canvas.bind("<Leave>", schedule_hide, add="+")
        scrollbar.bind("<Enter>", cancel_hide, add="+")
        scrollbar.bind("<Leave>", schedule_hide, add="+")
        canvas.after_idle(refresh)

    def render(self, only: str | None = None) -> None:
        if only is None:
            for panel in self.panels.values(): panel.destroy()
            self.panels = {}; self.panel_content = {}; self.panel_counts = {}
        elif only in self.panels:
            self.panels.pop(only).destroy()
        # There is deliberately no ordinary-Toplevel fallback.  A desktop
        # organizer that cannot live below applications must stay invisible.
        self.shell.host = self.shell._find_overlay_host()
        if not self.shell.host:
            for panel in self.panels.values(): panel.destroy()
            self.panels = {}; self.shell.restore_icons(); return
        ui = self.model.store.data["ui"]
        monitor = self.shell.target_monitor(ui.get("target_monitor", "primary"))
        self.last_work_bounds = (monitor["left"], monitor["top"], monitor["right"], monitor["bottom"])
        groups = self.model.scan()
        self.group_snapshot = self.snapshot_groups(groups)
        auto = self.auto_layout(groups, monitor)
        profile = self.layout_profile(monitor)
        attached = []
        for i, cat in enumerate(all_categories(self.model.store)):
            if cat.id == "inbox": continue
            if only is not None and cat.id != only: continue
            mode = self.model.store.data["category_modes"].get(cat.id, cat.mode)
            if mode == "list": mode = "compact"  # default: two readable columns
            # Themia-like widget hierarchy: one quiet glass surface, with the
            # category expressed only by a slim accent rather than a coloured box.
            style = ui.setdefault("panel_styles", {}).get(cat.id, {})
            base_background = style.get("background", ui["background"])
            fg = self.text_color(base_background)
            panel_bg = self.tint(base_background, cat.color, .055)
            row_bg = self.tint(panel_bg, "#ffffff", .045)
            card = tk.Toplevel(self.root, class_="DeskflowPanel")
            card.overrideredirect(True); card.configure(bg=panel_bg)
            # Emergency Tk fallback only.  Tk applies alpha to the whole
            # top-level (including content), so retain its old readability
            # floor.  Normal launches use Qt and independent plate alpha.
            card.attributes("-alpha", max(.70, float(style.get("alpha", ui["alpha"]))))
            card.attributes("-toolwindow", True)  # keep it out of Alt+Tab/taskbar
            card.attributes("-topmost", False)
            card.bind("<Configure>", lambda _event, p=card: self.round_panel(p), add="+")
            default_x, default_y, default_w, default_h = auto[cat.id]
            if not profile.get("auto_layout", True):
                saved_size = profile.get("panel_sizes", {}).get(cat.id, {})
                default_w = int(saved_size.get("width", default_w))
                default_h = int(saved_size.get("height", default_h))
            position = f"+{default_x}+{default_y}" if profile.get("auto_layout", True) else profile.get("panel_positions", {}).get(cat.id, f"+{default_x}+{default_y}")
            card.geometry(f"{default_w}x{default_h}{position}")
            card.update_idletasks()
            attached.append(self.shell.attach(card.winfo_id()))
            self.panels[cat.id] = card
            header_pad = max(6, self.cell_size() // 8)
            top = tk.Frame(card, bg=panel_bg); top.pack(fill="x", padx=10, pady=(header_pad,4))
            # Only the title bar moves a block.  Content remains free for item
            # clicks, scrollbars, and edge resizing.
            top.bind("<ButtonPress-1>", lambda e, c=cat.id: self.drag_start(e,c))
            top.bind("<B1-Motion>", lambda e, c=cat.id: self.drag_move(e,c))
            top.bind("<ButtonRelease-1>", lambda e, c=cat.id: self.drag_end(e,c))
            title = ui.get("category_names", {}).get(cat.id, cat.title)
            tk.Frame(top, bg=cat.color, width=3, height=16).pack(side="left", padx=(0,6))
            text_size = max(8, min(14, int(ui.get("font_size", 9))))
            title_label = tk.Label(top, text=title, bg=panel_bg, fg=fg, font=("Microsoft YaHei UI", min(15, text_size + 1), "bold"))
            title_label.pack(side="left")
            title_label.bind("<Double-Button-1>", lambda _e, c=cat.id, t=title: self.rename_category(c, t))
            title_label.bind("<Button-3>", lambda e, c=cat.id, t=title: self.panel_menu(e, c, t))
            title_label.bind("<ButtonPress-1>", lambda e, c=cat.id: self.drag_start(e, c))
            title_label.bind("<B1-Motion>", lambda e, c=cat.id: self.drag_move(e, c))
            title_label.bind("<ButtonRelease-1>", lambda e, c=cat.id: self.drag_end(e, c))
            top.bind("<Button-3>", lambda e, c=cat.id, t=title: self.panel_menu(e, c, t))
            button_style = {"bg": panel_bg, "fg": fg, "activebackground": row_bg, "activeforeground": fg, "relief": "flat", "bd": 0, "padx": 4, "pady": 1}
            close_button = tk.Button(top, text="×", fg="#e58a9e", command=self.close, **{k:v for k,v in button_style.items() if k != "fg"}); close_button.pack(side="right"); self.tooltip(close_button, "关闭整理层，恢复原生桌面图标")
            settings_button = tk.Button(top, text="⚙", command=self.settings, **button_style); settings_button.pack(side="right"); self.tooltip(settings_button, "调整图标、字体、背景和目标屏幕")
            arrange_button = tk.Button(top, text="⌘", command=self.arrange_top_right, **button_style); arrange_button.pack(side="right"); self.tooltip(arrange_button, "恢复当前屏幕的自动弹性排版")
            fit_button = tk.Button(top, text="↗", command=lambda c=cat.id, n=len(groups.get(cat.id, [])): self.fit_panel(c, n), **button_style); fit_button.pack(side="right"); self.tooltip(fit_button, "按内容自动拉长此分类块，不超出屏幕")
            mode_button = tk.Button(top, text={"list":"☷","cards":"▦","compact":"≡"}[mode], command=lambda c=cat.id: self.cycle_mode(c), **button_style); mode_button.pack(side="right"); self.tooltip(mode_button, "切换此块的列表、图标网格或紧凑双列显示")
            sort_button = tk.Button(top, text="⇅", command=lambda c=cat.id: self.sort_menu(c), **button_style); sort_button.pack(side="right"); self.tooltip(sort_button, "排序：智能分组、名称、最近修改或手动顺序")
            items = self.model.sorted_items(cat.id, groups.get(cat.id, []))
            count_label = tk.Label(top, text=str(len(items)), bg=row_bg, fg="#bdc9db", padx=7, pady=2, font=("Segoe UI", max(8, text_size - 1), "bold")); count_label.pack(side="right", padx=7)
            self.panel_counts[cat.id] = count_label
            canvas = tk.Canvas(card, bg=panel_bg, highlightthickness=0, bd=0)
            scrollbar = tk.Scrollbar(card, orient="vertical", command=canvas.yview)
            body = tk.Frame(canvas, bg=panel_bg)
            body.deskflow_canvas = canvas  # type: ignore[attr-defined]
            body.deskflow_bg, body.deskflow_row_bg = panel_bg, row_bg  # type: ignore[attr-defined]
            self.panel_content[cat.id] = (body, cat, mode, fg)
            canvas.configure(yscrollcommand=scrollbar.set)
            self.setup_edge_scrollbar(canvas, scrollbar, body)
            canvas.pack(side="left", fill="both", expand=True, padx=(9,0), pady=(0,10))
            # Invisible edge grips preserve the frameless desktop feel; their
            # cursor makes resizing discoverable without adding noisy chrome.
            self.add_resize_grips(card, cat.id)
            self.item_widgets[cat.id] = []
            if mode == "cards":
                card_columns = 4
                for column in range(card_columns): body.grid_columnconfigure(column, weight=1, minsize=72)
                for index, item in enumerate(items): self.item_tile(body, item, cat, index, card_columns)
            elif mode == "compact":
                for column in range(2): body.grid_columnconfigure(column, weight=1)
                for index, item in enumerate(items): self.item_row(body, item, cat, mode, fg, index)
            else:
                for item in items: self.item_row(body, item, cat, mode, fg)
            for surface in (canvas, body):
                surface.bind("<Enter>", lambda _e, c=canvas: setattr(self, "active_canvas", c))
            if not items: tk.Label(body, text="保持清爽", fg="#8ea2bd", bg=panel_bg, anchor="w").pack(fill="x", pady=10)
        # A failed re-parent may leave a normal top-level window for a moment.
        # Tear every block down rather than ever showing it above an application.
        if not all(attached) or not all(self.shell.panel_is_desktop_child(panel.winfo_id()) for panel in self.panels.values()):
            for panel in self.panels.values(): panel.destroy()
            self.panels = {}; self.shell.restore_icons(); return
        self.shell.keep_panels_above_desktop_backdrop()
        # Never hide native icons merely because the program started.  They are
        # hidden only after all blocks are confirmed as desktop children.
        if only is None:
            self.root.update_idletasks()
            # Solve every panel as one layout, rather than asking each panel to
            # avoid a partly-updated set of neighbours.  The latter was the
            # source of restored/manual layouts occasionally overlapping.
            bounds = (monitor["left"], monitor["top"], monitor["right"], monitor["bottom"])
            requested = [Rect(category, panel.winfo_x(), panel.winfo_y(), panel.winfo_width(), panel.winfo_height())
                         for category, panel in self.panels.items()]
            # The elastic plan is already bounded and non-overlapping.  Do not
            # feed it into the legacy dynamic packer, which can have no valid
            # state after a remote desktop shrinks below an old layout.
            packed = ({item.key: item for item in requested} if profile.get("auto_layout", True)
                      else pack_rectangles(requested, bounds, margin=12, gap=10))
            changed = False
            for category, panel in self.panels.items():
                rect = packed[category]
                if (rect.x, rect.y, rect.width, rect.height) != (panel.winfo_x(), panel.winfo_y(), panel.winfo_width(), panel.winfo_height()):
                    panel.geometry(f"{rect.width}x{rect.height}+{rect.x}+{rect.y}")
                    self.active_profile.setdefault("panel_positions", {})[category] = f"+{rect.x}+{rect.y}"
                    self.active_profile.setdefault("panel_sizes", {})[category] = {"width": rect.width, "height": rect.height}
                    changed = True
            if changed:
                # Record the corrected, usable arrangement for this display
                # profile so it stays fixed across the next start/RDP session.
                self.active_profile["auto_layout"] = False
                self.model.store.save()
            if self.panels and all(self.shell.panel_is_visible(p.winfo_id()) for p in self.panels.values()):
                self.shell.hide_icons(self.organized_paths(groups))
            else:
                self.shell.restore_icons()
        elif self.panels:
            self.shell.hide_icons(self.organized_paths(groups))

    def refresh_panel_items(self, category: str, groups: dict[str, list[dict[str, Any]]] | None = None) -> None:
        """Reflow only a panel's item area; never destroy its desktop window."""
        content = self.panel_content.get(category)
        if not content: return
        body, cat, mode, fg = content
        for child in body.winfo_children(): child.destroy()
        self.item_widgets[category] = []
        items = self.model.sorted_items(category, (groups or self.model.scan()).get(category, []))
        count = self.panel_counts.get(category)
        if count and count.winfo_exists(): count.configure(text=str(len(items)))
        if mode == "cards":
            for column in range(4): body.grid_columnconfigure(column, weight=1, minsize=72)
            for index, item in enumerate(items): self.item_tile(body, item, cat, index, 4)
        elif mode == "compact":
            for column in range(2): body.grid_columnconfigure(column, weight=1)
            for index, item in enumerate(items): self.item_row(body, item, cat, mode, fg, index)
        else:
            for item in items: self.item_row(body, item, cat, mode, fg)
        if not items: tk.Label(body, text="保持清爽", fg="#8ea2bd", bg=getattr(body, "deskflow_bg", "#101827"), anchor="w").pack(fill="x", pady=10)
        canvas = getattr(body, "deskflow_canvas", None)
        if canvas:
            canvas.configure(scrollregion=canvas.bbox("all"))
            refresh_scrollbar = getattr(body, "deskflow_refresh_scrollbar", None)
            if refresh_scrollbar: canvas.after_idle(refresh_scrollbar)

    def mouse_wheel(self, event: tk.Event) -> None:
        if self.active_canvas and self.active_canvas.winfo_exists():
            self.active_canvas.yview_scroll(int(-event.delta / 120), "units")

    def paint_item(self, container: tk.Widget, color: str) -> None:
        if not container.winfo_exists(): return
        normal = getattr(container, "deskflow_normal", color)
        colors = {normal, getattr(container, "deskflow_hover", normal), getattr(container, "deskflow_selected", normal)}
        container.configure(bg=color)
        for child in container.winfo_children():
            try:
                if child.cget("bg") in colors: child.configure(bg=color)
            except tk.TclError: pass

    def select_item(self, category: str, name: str, container: tk.Widget) -> None:
        previous = self.selected_container
        if previous and previous.winfo_exists():
            self.paint_item(previous, getattr(previous, "deskflow_normal", previous.cget("bg")))
        self.selected_item = (category, name.casefold()); self.selected_container = container
        self.paint_item(container, getattr(container, "deskflow_selected", container.cget("bg")))

    def bind_item_hover(self, container: tk.Widget, targets: tuple[tk.Widget, ...], normal: str, accent: str,
                        canvas: tk.Canvas | None, selection_key: tuple[str, str]) -> None:
        """Apply the same visual affordance to every item view, never by category."""
        hover = self.tint(normal, accent, .12)
        selected = self.tint(normal, accent, .24)
        container.deskflow_normal = normal  # type: ignore[attr-defined]
        container.deskflow_hover = hover  # type: ignore[attr-defined]
        container.deskflow_selected = selected  # type: ignore[attr-defined]
        def leave_container(_event: tk.Event) -> None:
            # Tk emits Leave/Enter when the pointer crosses a child Label.  Wait
            # for that crossing to settle, then clear only if it truly left the
            # card bounds; otherwise cards flicker continuously under the icon.
            def settle() -> None:
                if not container.winfo_exists(): return
                px, py = container.winfo_pointerxy()
                inside = (container.winfo_rootx() <= px < container.winfo_rootx() + container.winfo_width()
                          and container.winfo_rooty() <= py < container.winfo_rooty() + container.winfo_height())
                if not inside:
                    color = selected if self.selected_item == selection_key else normal
                    self.paint_item(container, color)
            container.after_idle(settle)
        for target in targets:
            target.bind("<Enter>", lambda _event, c=canvas: (setattr(self, "active_canvas", c), self.paint_item(container, hover)), add="+")
            target.bind("<Leave>", leave_container, add="+")

    def item_row(self, parent: tk.Widget, item: dict[str, Any], cat: Category, mode: str, fg: str, column: int | None = None) -> None:
        row_bg = getattr(parent, "deskflow_row_bg", "#223148")
        cell_size = self.cell_size()
        row = tk.Frame(parent, bg=row_bg, cursor="hand2", height=cell_size)
        row.pack_propagate(False); row.grid_propagate(False)
        if column is None: row.pack(fill="x", pady=2)
        else:
            parent.grid_rowconfigure(column // 2, minsize=cell_size)
            row.grid(row=column // 2, column=column % 2, sticky="ew", padx=2, pady=2)
        # Request a nearby native shell size instead of scaling a large bitmap
        # down: compact settings remain crisp and genuinely consume less room.
        requested_size = int(self.model.store.data["ui"]["icon_size"])
        size = 24 if requested_size <= 26 else 28 if requested_size <= 32 else 32
        preview_icon = self.ppt_preview(Path(item["path"]), size)
        badge, badge_color = ("", "") if preview_icon else self.file_badge(item)
        native_icon = preview_icon or (None if badge else self.visual_icon_for(item, size))
        if badge:
            tk.Label(row, text=badge, fg="#ffffff", bg=badge_color, width=4, font=("Segoe UI", max(8, int(self.model.store.data["ui"].get("font_size", 9)) - 1), "bold")).pack(side="left", padx=(7,5), pady=5)
        elif native_icon:
            tk.Label(row, image=native_icon, bg=row_bg).pack(side="left", padx=(6,5), pady=3)
        else:
            tk.Label(row, text=item["kind"].upper()[:4], fg=cat.color, bg=row_bg, width=4, font=("Segoe UI", max(8, int(self.model.store.data["ui"].get("font_size", 9))), "bold")).pack(side="left", padx=(7,5))
        max_len = 13 if mode == "compact" else 34
        display_name = Path(item["name"]).stem if item["kind"] != "folder" else item["name"]
        label = display_name if len(display_name) <= max_len else display_name[:max_len-1] + "…"
        text = label
        widget = tk.Label(row, text=text, fg=fg, bg=row_bg, anchor="w", font=("Microsoft YaHei UI", int(self.model.store.data["ui"]["font_size"])))
        widget.pack(side="left", fill="x", expand=True, pady=4)
        self.item_widgets.setdefault(cat.id, []).append((item["name"], row))
        targets = (row, *row.winfo_children())
        key = (cat.id, item["name"].casefold())
        self.bind_item_hover(row, targets, row_bg, cat.color, getattr(parent, "deskflow_canvas", None), key)
        for target in targets:
            target.bind("<ButtonPress-1>", lambda e, c=cat.id, i=item: self.item_drag_start(e, c, i))
            target.bind("<B1-Motion>", self.item_drag_move)
            target.bind("<ButtonRelease-1>", lambda e, c=cat.id, i=item, w=row: self.item_drag_end(e, c, i, w))
            target.bind("<Double-Button-1>", lambda _e, p=item["path"]: self.open_item(p))
            target.bind("<Button-3>", lambda e, c=cat.id, i=item, w=row: self.assign_menu(e, i, c, w))
        self.tooltip(row, self.item_tip(item))

    def file_badge(self, item: dict[str, Any]) -> tuple[str, str]:
        """Replace generic Windows document icons with instantly readable badges."""
        suffix = Path(item["name"]).suffix.lower()
        badges = {
            ".ppt": ("PPT", "#d65339"), ".pptx": ("PPT", "#d65339"),
            ".doc": ("DOC", "#2f75c9"), ".docx": ("DOC", "#2f75c9"),
            ".xls": ("XLS", "#21855b"), ".xlsx": ("XLS", "#21855b"),
            ".pdf": ("PDF", "#c53c3c"), ".zip": ("ZIP", "#9c6d27"), ".rar": ("RAR", "#9c6d27"),
            ".7z": ("7Z", "#9c6d27"), ".txt": ("TXT", "#60758e"),
            ".md": ("MD", "#536b91"), ".py": ("PY", "#3576a8"), ".cpp": ("C++", "#6e6fce"),
            ".yaml": ("YML", "#8756a8"), ".yml": ("YML", "#8756a8"),
        }
        return badges.get(suffix, ("", ""))

    def item_tile(self, parent: tk.Widget, item: dict[str, Any], cat: Category, column: int, columns: int = 4) -> None:
        row_bg = getattr(parent, "deskflow_row_bg", "#223148")
        cell_size = self.cell_size()
        # Keep card geometry independent of native icon paint/hover events.
        # Without this, a few animated shell icons can ask Tk to recalculate the
        # grid repeatedly while the pointer moves across a tool card.
        tile = tk.Frame(parent, bg=row_bg, cursor="hand2", width=cell_size, height=cell_size)
        # Children use pack(), so disabling only grid propagation does not
        # freeze the card.  Disable both geometry propagation paths and give
        # the containing grid row a fixed minimum to keep hover pixel-stable.
        tile.pack_propagate(False)
        tile.grid_propagate(False)
        row_index = column // columns
        parent.grid_rowconfigure(row_index, minsize=cell_size + 6)
        tile.grid(row=row_index, column=column % columns, padx=2, pady=3, sticky="nsew")
        # Card icons follow the compact preference too, while retaining enough
        # visual recognition for title-less game/application grids.
        card_size = max(26, min(36, round(cell_size * .58)))
        icon = self.visual_icon_for(item, card_size)
        if icon:
            content = tk.Label(tile, image=icon, bg=row_bg, cursor="hand2")
            content.place(relx=.5, rely=.5, anchor="center")
        else:
            content = tk.Label(tile, text="GAME" if cat.id == "games" else "APP", fg=cat.color, bg=row_bg, cursor="hand2", font=("Segoe UI", max(8, int(self.model.store.data["ui"].get("font_size", 9))), "bold"))
            content.place(relx=.5, rely=.5, anchor="center")
        # Game entry names are available on hover, keeping the grid recognisable and compact.
        self.tooltip(content, self.item_tip(item), delay_ms=420)
        self.item_widgets.setdefault(cat.id, []).append((item["name"], tile))
        key = (cat.id, item["name"].casefold())
        self.bind_item_hover(tile, (tile, content), row_bg, cat.color, getattr(parent, "deskflow_canvas", None), key)
        for target in (tile, content):
            target.bind("<ButtonPress-1>", lambda e, c=cat.id, i=item: self.item_drag_start(e, c, i))
            target.bind("<B1-Motion>", self.item_drag_move)
            target.bind("<ButtonRelease-1>", lambda e, c=cat.id, i=item, w=tile: self.item_drag_end(e, c, i, w))
            target.bind("<Double-Button-1>", lambda _e, p=item["path"]: self.open_item(p))
            target.bind("<Button-3>", lambda e, c=cat.id, i=item, w=tile: self.assign_menu(e, i, c, w))

    def tooltip(self, widget: tk.Widget, value: str, delay_ms: int = 250) -> None:
        popup: list[tk.Toplevel | None] = [None]
        pending: list[str | None] = [None]
        def show_now():
            pending[0] = None
            if popup[0] or not widget.winfo_exists(): return
            tip = tk.Toplevel(widget); tip.overrideredirect(True); tip.attributes("-topmost", True)
            tip.configure(bg="#0d1420"); tip.geometry(f"+{widget.winfo_rootx()+10}+{widget.winfo_rooty()+widget.winfo_height()+4}")
            tk.Label(tip, text=value, bg="#0d1420", fg="#ffffff", padx=8, pady=4, font=("Microsoft YaHei UI", 10)).pack(); popup[0] = tip
        def show(_event):
            if not pending[0] and not popup[0]: pending[0] = widget.after(delay_ms, show_now)
        def hide(_event):
            if pending[0]: widget.after_cancel(pending[0]); pending[0] = None
            if popup[0]: popup[0].destroy(); popup[0] = None
        widget.bind("<Enter>", show, add="+"); widget.bind("<Leave>", hide, add="+")

    def item_tip(self, item: dict[str, Any]) -> str:
        cached = self.model.store.data.get("tip_summaries", {}).get(Path(item["path"]).name.lower(), {})
        target, summary, app = cached.get("target", ""), cached.get("summary", ""), cached.get("app", "")
        tags = cached.get("tags", [])
        return item["name"] + (f"\n指向：{target}" if target else "") + (f"\n{summary}" if summary else "") + (f"\n{app}" if app else "") + (f"\n标签：{' · '.join(tags)}" if tags else "")

    @staticmethod
    def software_summary(executable: Path) -> str | None:
        """Name known desktop software by its purpose, not by its file path."""
        known = {
            "unity": "Unity：游戏与交互内容开发", "code": "Visual Studio Code：代码编辑与开发", "cursor": "Cursor：AI 辅助代码开发",
            "chrome": "Google Chrome：网页浏览", "msedge": "Microsoft Edge：网页浏览", "steam": "Steam：游戏库与启动器",
            "obs": "OBS Studio：录屏与直播", "potplayer": "PotPlayer：音视频播放", "wps": "WPS Office：文档与演示编辑",
            "telegram": "Telegram：即时通讯", "clash": "Clash：网络代理管理", "discord": "Discord：社群语音与通讯",
        }
        stem = executable.stem.casefold()
        for token, description in known.items():
            if token in stem: return description
        product = description = ""
        try:
            for codepage in ("040904b0", "080404b0"):
                product = win32api.GetFileVersionInfo(str(executable), fr"\StringFileInfo\{codepage}\ProductName") or product
                description = win32api.GetFileVersionInfo(str(executable), fr"\StringFileInfo\{codepage}\FileDescription") or description
                if product or description: break
        except Exception: pass
        name = (product or executable.stem).strip()[:60]
        purpose = description.strip()[:90] if description and description.strip().casefold() != name.casefold() else "本地桌面软件启动入口"
        return f"{name}：{purpose}"

    def script_summary(self, path: Path) -> str | None:
        """Follow a launcher through .lnk/.bat to describe the called program."""
        actual = path
        try:
            if path.suffix.lower() == ".lnk":
                target = Dispatch("WScript.Shell").CreateShortcut(str(path)).TargetPath
                if target: actual = Path(target)
            if actual.suffix.lower() not in {".bat", ".cmd", ".ps1"}: return None
            text = self.model._safe_text(actual, 4096)
            match = re.search(r'"([^"\r\n]+\.exe)"|\b([A-Za-z]:[^\r\n\s]+\.exe)', text, re.I)
            if not match: return None
            target = Path(match.group(1) or match.group(2))
            description = self.software_summary(target)
            return f"启动 {description}" if description else f"调用 {target.stem}"
        except Exception: return None

    def visual_icon_for(self, item: dict[str, Any], size: int) -> ImageTk.PhotoImage | None:
        path = Path(item["path"])
        preview = self.ppt_preview(path, size)
        if preview: return preview
        generated = self.script_badge(path, size)
        if generated: return generated
        target = self.script_target(path)
        return self.icon_for(str(target or path), size)

    def script_badge(self, path: Path, size: int) -> ImageTk.PhotoImage | None:
        """Create a stable local icon for script launchers that have no executable icon."""
        actual = path
        try:
            if path.suffix.lower() == ".lnk":
                value = Dispatch("WScript.Shell").CreateShortcut(str(path)).TargetPath
                if value: actual = Path(value)
            if actual.suffix.lower() not in {".bat", ".cmd", ".ps1"}: return None
            digest = hashlib.sha1(str(actual).encode("utf-8")).hexdigest()[:16]
            cache_dir = DATA_DIR / "icon_cache"; cache_dir.mkdir(exist_ok=True)
            cache = cache_dir / f"script-{digest}.png"
            if not cache.exists():
                name = actual.stem.replace("startup", "").replace("start", "").strip("-_ .") or actual.stem
                letters = re.sub(r"[^A-Za-z0-9]", "", name).upper()[:2] or "RUN"
                color = (34 + int(digest[:2],16)//3, 50 + int(digest[2:4],16)//4, 90 + int(digest[4:6],16)//5)
                image = Image.new("RGBA", (96, 96), (0, 0, 0, 0)); draw = ImageDraw.Draw(image)
                draw.rounded_rectangle((4, 4, 92, 92), radius=20, fill=color + (255,), outline=(126,230,255,255), width=3)
                try: font = ImageFont.truetype("arialbd.ttf", 34)
                except OSError: font = ImageFont.load_default()
                box = draw.textbbox((0,0), letters, font=font); draw.text(((96-(box[2]-box[0]))/2, (96-(box[3]-box[1]))/2-2), letters, font=font, fill="white")
                image.save(cache)
            key = (str(cache), size)
            if key not in self.native_icons:
                image = Image.open(cache).convert("RGBA").resize((size,size), Image.Resampling.LANCZOS)
                self.native_icons[key] = ImageTk.PhotoImage(image)
            self.model.store.data["visual_assets"][path.name.lower()] = {"kind": "generated-script-icon", "cache": str(cache)}
            self.model.store.save(); return self.native_icons[key]
        except Exception: return None

    def script_target(self, path: Path) -> Path | None:
        """Give .bat/.cmd entries the icon of the program they actually run."""
        target = path
        try:
            if path.suffix.lower() == ".lnk":
                target_path = Dispatch("WScript.Shell").CreateShortcut(str(path)).TargetPath
                if target_path: target = Path(target_path)
            if target.suffix.lower() not in {".bat", ".cmd"}: return None
            text = target.read_text(encoding="utf-8", errors="ignore")[:4096]
            match = re.search(r'"([^"\r\n]+\.exe)"|\b([A-Za-z]:[^\r\n\s]+\.exe)', text, re.I)
            if match and Path(match.group(1) or match.group(2)).exists():
                resolved = Path(match.group(1) or match.group(2))
                self.model.store.data["visual_assets"][path.name.lower()] = {"kind": "command-target", "source": str(resolved)}
                self.model.store.save(); return resolved
        except Exception: pass
        return None

    def ppt_preview(self, path: Path, size: int) -> ImageTk.PhotoImage | None:
        if path.suffix.lower() not in {".pptx", ".pptm"}: return None
        digest = hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:16]
        cache_dir = DATA_DIR / "icon_cache"; cache_dir.mkdir(exist_ok=True)
        cached = cache_dir / f"ppt-{digest}.png"
        try:
            if not cached.exists() or cached.stat().st_mtime < path.stat().st_mtime:
                with zipfile.ZipFile(path) as package:
                    candidate = next((n for n in ("docProps/thumbnail.jpeg", "docProps/thumbnail.png") if n in package.namelist()), None)
                    if not candidate:
                        candidate = next((n for n in package.namelist() if n.startswith("ppt/media/") and n.lower().endswith((".png", ".jpg", ".jpeg"))), None)
                    if not candidate: return None
                    cached.write_bytes(package.read(candidate))
                self.model.store.data["visual_assets"][path.name.lower()] = {"kind": "ppt-preview", "cache": str(cached)}
                self.model.store.save()
            key = (str(cached), size)
            if key in self.native_icons: return self.native_icons[key]
            image = Image.open(cached).convert("RGBA"); image.thumbnail((size, size), Image.Resampling.LANCZOS)
            canvas = Image.new("RGBA", (size, size), "#223148"); canvas.paste(image, ((size-image.width)//2, (size-image.height)//2))
            photo = ImageTk.PhotoImage(canvas); self.native_icons[key] = photo; return photo
        except (OSError, zipfile.BadZipFile): return None

    def icon_for(self, path: str, size: int) -> ImageTk.PhotoImage | None:
        """Use the actual Windows shell icon (including shortcuts and folders)."""
        key = (path, size)
        if key in self.native_icons: return self.native_icons[key]
        try:
            handle = shell.SHGetFileInfo(path, 0, shellcon.SHGFI_ICON | shellcon.SHGFI_LARGEICON)[1][0]
            screen = win32ui.CreateDCFromHandle(win32gui.GetDC(0)); memory = screen.CreateCompatibleDC()
            bitmap = win32ui.CreateBitmap(); bitmap.CreateCompatibleBitmap(screen, size, size); memory.SelectObject(bitmap)
            memory.FillSolidRect((0, 0, size, size), win32api.RGB(34, 49, 72))
            win32gui.DrawIconEx(memory.GetSafeHdc(), 0, 0, handle, size, size, 0, None, win32con.DI_NORMAL)
            info, bits = bitmap.GetInfo(), bitmap.GetBitmapBits(True)
            image = Image.frombuffer("RGBA", (info["bmWidth"], info["bmHeight"]), bits, "raw", "BGRA", 0, 1)
            icon = ImageTk.PhotoImage(image)
            self.native_icons[key] = icon
            # Native cleanup differs between pywin32 builds.  A cleanup error
            # must never discard a successfully created visible icon.
            for cleanup in (lambda: win32gui.DestroyIcon(handle), lambda: bitmap.DeleteObject(), lambda: memory.DeleteDC(), lambda: screen.DeleteDC()):
                try: cleanup()
                except Exception: pass
            return icon
        except Exception:
            return None

    def assign_item(self, name: str, before: str, category: str) -> None:
        self.model.set_category(name, category)
        if before != "inbox" and before != category: self.render(before)
        if category != "inbox": self.render(category)
        else: self.shell.hide_icons(self.organized_paths(self.model.scan()))

    def reset_item_classification(self, name: str, before: str) -> None:
        self.model.set_category(name, "archive", False)
        after = next((category for category, items in self.model.scan().items()
                      if any(item["name"].casefold() == name.casefold() for item in items)), "archive")
        if before != "inbox" and before != after: self.render(before)
        if after != "inbox": self.render(after)

    def recycle_item(self, item: dict[str, Any], before: str) -> None:
        """Move one user-selected desktop item to the Windows Recycle Bin."""
        path = Path(item["path"])
        if not path.exists():
            messagebox.showerror("无法删除", "文件已不存在或无法访问。", parent=self.root)
            self.render(before)
            return
        kind = "文件夹" if path.is_dir() else "文件"
        if not messagebox.askyesno("移到回收站", f"确定将{kind}“{path.name}”移到回收站吗？\n\n可在 Windows 回收站中恢复。", parent=self.root):
            return
        try:
            result, aborted = shell.SHFileOperation((0, shellcon.FO_DELETE, str(path), None,
                                                     shellcon.FOF_ALLOWUNDO | shellcon.FOF_NOCONFIRMATION | shellcon.FOF_SILENT,
                                                     None, None))
            if result or aborted:
                raise OSError(result or "操作被取消")
        except Exception as exc:
            messagebox.showerror("无法删除", f"无法将该项移到回收站：{exc}", parent=self.root)
            return
        # Remove stale per-item presentation data now; the next scan removes
        # the item from its block and restores the native desktop if needed.
        key = item["name"].casefold()
        data = self.model.store.data
        data.get("overrides", {}).pop(key, None)
        data.get("pending_inbox", {}).pop(key, None)
        data.get("seen", {}).pop(key, None)
        data.get("visual_assets", {}).pop(key, None)
        data.get("tip_summaries", {}).pop(key, None)
        for names in data.get("item_orders", {}).values():
            if isinstance(names, list): names[:] = [name for name in names if name.casefold() != key]
        self.model.store.save()
        self.render()

    def open_location(self, path: str) -> None:
        try: subprocess.Popen(["explorer.exe", "/select,", str(Path(path))])
        except OSError as exc: messagebox.showerror("无法定位", str(exc), parent=self.root)

    def rename_item(self, item: dict[str, Any]) -> None:
        path = Path(item["path"])
        current = path.name if item["kind"] == "folder" else path.stem
        suffix_note = "" if item["kind"] == "folder" or not path.suffix else f"（扩展名 {path.suffix} 会保留）"
        value = simpledialog.askstring("重命名", f"请输入新名称{suffix_note}：", initialvalue=current, parent=self.root)
        if value is None: return
        try: self.model.rename_item(item["path"], value)
        except ValueError as exc:
            messagebox.showerror("无法重命名", str(exc), parent=self.root); return
        self.render()

    def assign_menu(self, event: tk.Event, item: dict[str, Any], before: str, container: tk.Widget) -> None:
        self.select_item(before, item["name"], container)
        from native_context_menu import show_file_menu
        actions = [
            ("打开所在位置", lambda: self.open_location(item["path"])),
            ("放回原生桌面，等待整理", lambda: self.assign_item(item["name"], before, "inbox")),
        ]
        for cat in all_categories(self.model.store):
            if cat.id not in ("inbox", before):
                actions.append((f"归到 {cat.title}", lambda target=cat.id: self.assign_item(item["name"], before, target)))
        actions.extend([
            ("取消手动归类，让 AI 按规则判断", lambda: self.reset_item_classification(item["name"], before)),
            ("重命名（保留整理记录）", lambda: self.rename_item(item)),
            ("移到回收站", lambda: self.recycle_item(item, before)),
        ])
        try:
            callback = show_file_menu(item["path"], event.x_root, event.y_root, actions, lambda: self.rename_item(item))
        except Exception as exc:
            messagebox.showwarning("系统右键菜单无法加载", f"{exc}\n将显示备用菜单；可通过‘打开所在位置’使用资源管理器菜单。", parent=self.root)
        else:
            if callback:
                self.root.after_idle(callback)
            return
        menu = tk.Menu(self.root, tearoff=False, bg="#1d2a3e", fg="#f4f8ff")
        menu.add_command(label="打开（也可双击）", command=lambda: self.open_item(item["path"]))
        menu.add_command(label="打开所在位置", command=lambda: self.open_location(item["path"]))
        menu.add_command(label="重命名文件夹" if item["kind"] == "folder" else "重命名文件", command=lambda: self.rename_item(item))
        menu.add_separator()
        menu.add_command(label="放回原生桌面，等待整理", command=lambda: self.assign_item(item["name"], before, "inbox"))
        for cat in all_categories(self.model.store):
            if cat.id != "inbox":
                menu.add_command(label=f"归到 {cat.title}", command=lambda c=cat.id: self.assign_item(item["name"], before, c))
        menu.add_separator()
        menu.add_command(label="取消手动归类，让 AI 按规则判断", command=lambda: self.reset_item_classification(item["name"], before))
        menu.add_separator()
        menu.add_command(label="移到回收站", command=lambda: self.recycle_item(item, before))
        try: menu.tk_popup(event.x_root, event.y_root)
        finally: menu.grab_release()

    def sort_menu(self, category: str) -> None:
        menu = tk.Menu(self.root, tearoff=False, bg="#1d2a3e", fg="#f4f8ff")
        labels = (("smart", "智能分组（默认）"), ("name", "按名称"), ("modified", "按最近修改"), ("custom", "手动顺序"))
        current = self.model.store.data.setdefault("category_sorting", {}).get(category, "smart")
        for value, label in labels:
            prefix = "✓ " if value == current else "    "
            menu.add_command(label=prefix + label, command=lambda v=value, c=category: self.set_sorting(c, v))
        panel = self.panels.get(category)
        if panel: menu.tk_popup(panel.winfo_rootx() + 44, panel.winfo_rooty() + 38)

    def set_sorting(self, category: str, mode: str) -> None:
        self.model.store.data.setdefault("category_sorting", {})[category] = mode
        self.model.store.save(); self.render(category)

    def item_drag_start(self, event: tk.Event, category: str, item: dict[str, Any]) -> None:
        self.item_drag = {"category": category, "item": item, "x": event.x_root, "y": event.y_root, "moved": False}

    def item_drag_move(self, event: tk.Event) -> None:
        if self.item_drag and abs(event.x_root - self.item_drag["x"]) + abs(event.y_root - self.item_drag["y"]) > 8:
            self.item_drag["moved"] = True

    def panel_at(self, x: int, y: int) -> str | None:
        """Return a block under a screen coordinate; inbox intentionally has no block."""
        for category, panel in self.panels.items():
            if not panel.winfo_exists(): continue
            left, top = panel.winfo_rootx(), panel.winfo_rooty()
            if left <= x < left + panel.winfo_width() and top <= y < top + panel.winfo_height(): return category
        return None

    @staticmethod
    def explorer_display_name(item: dict[str, Any]) -> str:
        try: return shell.SHGetFileInfo(item["path"], 0, shellcon.SHGFI_DISPLAYNAME)[1][3]
        except Exception:
            return item["name"] if item["kind"] == "folder" else Path(item["name"]).stem

    def move_items_to_category(self, entries: list[tuple[str, str]], target: str) -> None:
        """Move visual ownership only; the underlying desktop files stay put."""
        affected = {target}
        for name, source in entries:
            affected.add(source)
            self.model.set_category(name, target)
        groups = self.model.scan()
        # Attribute update makes an item immediately disappear from / reappear on
        # the native desktop.  Reflow only the two content areas: panel HWNDs,
        # glass, title bars and scroll positions stay alive throughout the drop.
        self.shell.hide_icons(self.organized_paths(groups))
        for category in affected:
            if category != "inbox": self.refresh_panel_items(category, groups)

    def drop_native_labels(self, labels: list[str], target: str) -> None:
        groups = self.model.scan()
        wanted = {label.casefold() for label in labels}
        entries = [(item["name"], "inbox") for item in groups.get("inbox", [])
                   if self.explorer_display_name(item).casefold() in wanted]
        if entries: self.move_items_to_category(entries, target)

    def watch_native_drag(self) -> None:
        """Bridge Explorer's native desktop drag into a DESKFLOW block drop."""
        down = self.shell.left_button_down()
        if down and not self.native_mouse_down:
            self.native_drag = None
            if self.shell.pointer_over_listview():
                x, y = self.shell.cursor_position()
                self.native_drag = {"x": x, "y": y, "labels": []}
        elif down and self.native_drag is not None and not self.native_drag["labels"]:
            # Explorer updates selection just after ButtonDown, so retry while
            # the pointer is held instead of guessing a filename from pixels.
            self.native_drag["labels"] = self.shell.selected_native_labels()
        elif not down and self.native_mouse_down and self.native_drag is not None:
            x, y = self.shell.cursor_position(); state = self.native_drag; self.native_drag = None
            moved = abs(x - state["x"]) + abs(y - state["y"]) > 8
            target = self.panel_at(x, y)
            if moved and target and state["labels"]:
                self.drop_native_labels(state["labels"], target)
        self.native_mouse_down = down
        self.root.after(75, self.watch_native_drag)

    def item_drag_end(self, event: tk.Event, category: str, item: dict[str, Any], container: tk.Widget) -> None:
        state, self.item_drag = self.item_drag, None
        if not state or state["item"]["name"] != item["name"]: return
        if not state["moved"]:
            self.select_item(category, item["name"], container); return
        destination = self.panel_at(event.x_root, event.y_root)
        if destination is None:
            # Dropping outside every block restores this entry to Explorer as a
            # native desktop icon.  It remains there until explicitly organized.
            self.move_items_to_category([(item["name"], category)], "inbox"); return
        if destination != category:
            self.move_items_to_category([(item["name"], category)], destination); return
        candidates = self.item_widgets.get(category, [])
        if not candidates: return
        # The nearest rendered item's centre is the drop insertion point.  It
        # works for both rows and the compact/card grids without any separate
        # hit-test geometry, and becomes an explicit persisted custom order.
        target_name, _target = min(candidates, key=lambda pair: (pair[1].winfo_rootx() + pair[1].winfo_width() / 2 - event.x_root) ** 2 + (pair[1].winfo_rooty() + pair[1].winfo_height() / 2 - event.y_root) ** 2)
        before = target_name if target_name.lower() != item["name"].lower() else None
        self.model.reorder_item(category, item["name"], before)
        self.render(category)

    def open_item(self, path: str) -> None:
        try: os.startfile(path)  # type: ignore[attr-defined]
        except OSError as exc: messagebox.showerror("无法打开", str(exc), parent=self.root)

    def cycle_mode(self, category: str) -> None:
        modes = ["list", "cards", "compact"]; current = self.model.store.data["category_modes"].get(category, "list")
        self.model.store.data["category_modes"][category] = modes[(modes.index(current)+1) % len(modes)]
        self.model.store.save(); self.render(category)

    def rename_category(self, category: str, current: str) -> None:
        value = simpledialog.askstring("修改分类名称", "名称：", initialvalue=current, parent=self.panels[category])
        if value and value.strip():
            self.model.store.data["ui"].setdefault("category_names", {})[category] = value.strip()
            self.model.store.save(); self.render(category)

    def panel_menu(self, event: tk.Event, category: str, title: str) -> None:
        menu = tk.Menu(self.root, tearoff=False, bg="#1d2a3e", fg="#f4f8ff")
        menu.add_command(label="新建块", command=self.create_block)
        menu.add_command(label="重命名此块", command=lambda: self.rename_category(category, title))
        menu.add_separator()
        menu.add_command(label="在当前目标屏幕弹性排版", command=self.arrange_top_right)
        move_menu = tk.Menu(menu, tearoff=False, bg="#1d2a3e", fg="#f4f8ff")
        move_menu.add_command(label="主屏幕", command=lambda: self.choose_monitor_and_arrange("primary"))
        for index, monitor in enumerate(self.shell.monitors(), start=1):
            if monitor.get("primary"):
                continue
            monitor_width = monitor["right"] - monitor["left"]
            monitor_height = monitor["bottom"] - monitor["top"]
            preference = f"monitor:{int(monitor.get('handle', index))}"
            move_menu.add_command(
                label=f"屏幕 {index}（{monitor_width}×{monitor_height}）",
                command=lambda target=preference: self.choose_monitor_and_arrange(target),
            )
        menu.add_cascade(label="移动全部到指定屏幕并弹性排版", menu=move_menu)
        custom_ids = {block["id"] for block in self.model.store.data.get("custom_blocks", [])}
        if category in custom_ids and not self.model.scan().get(category, []):
            menu.add_separator()
            menu.add_command(label="删除空块", command=lambda: self.delete_empty_block(category))
        try: menu.tk_popup(event.x_root, event.y_root)
        finally: menu.grab_release()

    def delete_empty_block(self, category: str) -> None:
        """Destroy only this empty custom panel; neighbours never redraw."""
        if self.model.scan().get(category): return
        panel = self.panels.pop(category, None)
        if panel:
            self.shell.panel_windows.pop(panel.winfo_id(), None)
            panel.destroy()
        self.panel_content.pop(category, None); self.panel_counts.pop(category, None); self.item_widgets.pop(category, None)
        self.model.store.data["custom_blocks"] = [block for block in self.model.store.data.get("custom_blocks", []) if block["id"] != category]
        self.model.store.data.get("category_modes", {}).pop(category, None)
        ui = self.model.store.data["ui"]
        ui.get("category_names", {}).pop(category, None); ui.get("panel_styles", {}).pop(category, None)
        for profile in ui.get("layout_profiles", {}).values():
            profile.get("panel_positions", {}).pop(category, None); profile.get("panel_sizes", {}).pop(category, None)
        self.model.store.save()

    def fit_panel(self, category: str, count: int) -> None:
        # Resizing only this rectangle used to overlap its neighbour: bounds
        # checks keep a panel on-screen but do not reserve space for the other
        # panels.  Content-fit is therefore a constrained global reflow.  The
        # planner calculates this category's required rows and shrinks/moves
        # the surrounding blocks before applying any geometry.
        del category, count
        self.arrange_top_right()

    def default_position(self, index: int, monitor: dict[str, Any]) -> tuple[int, int]:
        # Two neat columns at the right, leaving the left desktop area open.
        col, row = index % 2, index // 2
        return monitor["right"] - 640 + col * 320, monitor["top"] + 35 + row * 270

    def auto_layout(self, groups: dict[str, list[dict[str, Any]]], monitor: dict[str, Any]) -> dict[str, tuple[int, int, int, int]]:
        """Pack content-aware columns from the right edge towards the left.

        A uniform grid wastes most of a monitor on sparse blocks: a four-item
        tool block receives the same height as a 37-item block.  This is a
        first-fit decreasing-style column pack.  Each block first receives its
        readable content height; columns are filled top-to-bottom on the right,
        then continue left.  Any remaining height is awarded to the denser
        blocks in that column, so every used column reaches the screen bottom.
        """
        categories = [category for category in all_categories(self.model.store) if category.id != "inbox"]
        gap, margin = 10, 12
        screen_width = monitor["right"] - monitor["left"]
        screen_height = monitor["bottom"] - monitor["top"]
        usable_height = max(1, screen_height - 2 * margin)
        cell_size = self.cell_size()

        def preferred_height(category: Category) -> int:
            count = len(groups.get(category.id, []))
            mode = self.model.store.data.get("category_modes", {}).get(category.id, category.mode)
            if mode == "cards":
                rows, row_height, minimum = max(1, (count + 3) // 4), cell_size + 6, 145
            elif mode == "compact" or mode == "list":
                rows, row_height, minimum = max(1, (count + 1) // 2), cell_size + 4, 185
            else:
                rows, row_height, minimum = max(1, count), cell_size + 4, 185
            return min(usable_height, max(minimum, 62 + rows * row_height + 16))

        desired = [(category, preferred_height(category)) for category in categories]
        # Panels have a readable natural width.  Do not divide the whole
        # monitor by the number of columns: that was what made every frame
        # balloon to fill a 2K/4K desktop.  Keep the arrangement right-aligned
        # and grow left only as content requires it.
        footprint_width = max(260, int(screen_width * self.layout_footprint()))
        panel_width = min(320, max(240, screen_width - 2 * margin))
        max_columns = max(1, (footprint_width + gap) // (panel_width + gap))
        # Start with only the columns needed for readable content.  Add a
        # column when the right-to-left fill would otherwise overflow.
        columns = max(1, min(max_columns, (sum(height for _, height in desired) + gap * len(desired)) // max(1, usable_height) + 1))

        def pack(column_count: int) -> list[list[tuple[Category, int]]] | None:
            packed: list[list[tuple[Category, int]]] = [[] for _ in range(column_count)]
            used = [0] * column_count
            column = 0
            for category, panel_height in desired:
                needed = panel_height + (gap if packed[column] else 0)
                if used[column] + needed > usable_height and packed[column]:
                    column += 1
                    if column == column_count:
                        return None
                    needed = panel_height
                packed[column].append((category, panel_height))
                used[column] += needed
            return packed

        packed = pack(columns)
        while packed is None and columns < max_columns:
            columns += 1
            packed = pack(columns)
        if packed is None:
            # A very small remote desktop cannot preserve every preferred
            # height.  Keep all blocks reachable; their existing scrollbars
            # present overflowed content inside the compacted panel.
            rows_per_column = max(1, (len(categories) + max_columns - 1) // max_columns)
            compact_height = max(96, (usable_height - gap * max(0, rows_per_column - 1)) // rows_per_column)
            desired = [(category, min(panel_height, compact_height)) for category, panel_height in desired]
            columns = max_columns
            packed = pack(columns)
            if packed is None:
                # Deterministic last-resort distribution: never create more
                # columns than the footprint can hold, even on a tiny RDP view.
                packed = [[] for _ in range(columns)]
                for index, item in enumerate(desired): packed[min(columns - 1, index // rows_per_column)].append(item)

        cell_width = panel_width
        result: dict[str, tuple[int, int, int, int]] = {}
        for column, items in enumerate(packed):
            base_height = sum(panel_height for _, panel_height in items) + gap * max(0, len(items) - 1)
            y = monitor["top"] + margin
            # Column 0 is the rightmost column.  This keeps the user's visual
            # rule: fill the current screen from right to left.
            x = monitor["right"] - margin - (column + 1) * cell_width - column * gap
            for category, panel_height in items:
                result[category.id] = (x, y, cell_width, panel_height)
                y += panel_height + gap
        return result

    def layout_profile(self, monitor: dict[str, Any]) -> dict[str, Any]:
        ui = self.model.store.data["ui"]
        key = f"{monitor.get('screen_right', monitor['right'])-monitor.get('screen_left', monitor['left'])}x{monitor.get('screen_bottom', monitor['bottom'])-monitor.get('screen_top', monitor['top'])}@{monitor.get('screen_left', monitor['left'])},{monitor.get('screen_top', monitor['top'])}"
        profiles = ui.setdefault("layout_profiles", {})
        if key not in profiles:
            # A new resolution must never inherit coordinates from another
            # screen: they are the primary cause of remote-session overlap.
            profiles[key] = {"auto_layout": True, "panel_positions": {}, "panel_sizes": {}}
            self.model.store.save()
        self.active_profile = profiles[key]
        return self.active_profile

    def arrange_top_right(self, rebuild: bool = False) -> None:
        ui = self.model.store.data["ui"]; monitor = self.shell.target_monitor(ui.get("target_monitor", "primary")); profile = self.layout_profile(monitor)
        self.last_work_bounds = (monitor["left"], monitor["top"], monitor["right"], monitor["bottom"])
        profile["auto_layout"] = True
        # A monitor/resolution transition must not reuse a stale manual size or
        # logical-pixel window.  Normal toolbar/MCP reflow remains geometry
        # only, preserving hover state and avoiding unnecessary redraws.
        if rebuild:
            profile["panel_positions"] = {}
            profile["panel_sizes"] = {}
            self.render()
            if not self.panels:
                return
        auto = self.auto_layout(self.model.scan(), monitor)
        # Geometry-only update: every planned rectangle is already bounded and
        # non-overlapping for this monitor, even after an RDP resize.
        for category, panel in self.panels.items():
            x, y, width, height = auto[category]
            panel.geometry(f"{width}x{height}+{x}+{y}")
            profile.setdefault("panel_positions", {})[category] = f"+{x}+{y}"
            profile.setdefault("panel_sizes", {})[category] = {"width": width, "height": height}
        profile["auto_layout"] = False
        self.model.store.save()
        self.root.update_idletasks()
        self.shell.keep_panels_above_desktop_backdrop()

    def choose_monitor_and_arrange(self, preference: str) -> None:
        self.shell.remember_target_monitor(self.model.store.data["ui"], preference)
        self.model.store.save()
        self.arrange_top_right()

    def move_panel(self, category: str, x: int, y: int) -> None:
        panel = self.panels.get(category)
        if not panel: return
        width, height = panel.winfo_width(), panel.winfo_height()
        x, y = self.safe_position(category, x, y, width, height)
        panel.geometry(f"{width}x{height}+{x}+{y}")
        self.active_profile["auto_layout"] = False; self.active_profile.setdefault("panel_positions", {})[category] = f"+{x}+{y}"
        self.model.store.save()

    def safe_position(self, category: str, wanted_x: int, wanted_y: int, width: int, height: int) -> tuple[int, int]:
        """Keep blocks reachable and select the nearest non-overlapping slot."""
        monitor = self.shell.target_monitor(self.model.store.data["ui"].get("target_monitor", "primary")); margin, gap = 12, 10
        max_x, max_y = monitor["right"] - width - margin, monitor["bottom"] - height - margin
        base_x = max(monitor["left"] + margin, min(wanted_x, max_x)); base_y = max(monitor["top"] + margin, min(wanted_y, max_y))
        occupied = [(panel.winfo_x(), panel.winfo_y(), panel.winfo_width(), panel.winfo_height()) for name, panel in self.panels.items() if name != category and panel.winfo_exists()]
        def free(x: int, y: int) -> bool:
            return all(x + width + gap <= ox or ox + ow + gap <= x or y + height + gap <= oy or oy + oh + gap <= y for ox, oy, ow, oh in occupied)
        if free(base_x, base_y): return base_x, base_y
        candidates = [(x, y) for y in range(monitor["top"] + margin, max_y + 1, 28) for x in range(monitor["left"] + margin, max_x + 1, 28)]
        candidates.sort(key=lambda point: (point[0] - base_x) ** 2 + (point[1] - base_y) ** 2)
        return next(((x, y) for x, y in candidates if free(x, y)), (base_x, base_y))

    def dismiss_settings(self, dialog: tk.Toplevel) -> None:
        if self.settings_dialog is dialog:
            self.settings_dialog = None
        if dialog.winfo_exists():
            dialog.destroy()

    def release_dialog_topmost(self, dialog: tk.Toplevel) -> None:
        """Return a presented dialog to ordinary application Z-order."""
        if dialog.winfo_exists():
            dialog.attributes("-topmost", False)

    def present_settings_dialog(self, dialog: tk.Toplevel) -> None:
        """Show settings as an independent app window above the desktop band.

        The Tk root is intentionally withdrawn because DESKFLOW's everyday
        windows are Explorer-owned panels.  Making this dialog transient to
        that hidden root causes Windows to hide the dialog with its owner.  A
        short topmost pulse also lets a click from a WS_EX_NOACTIVATE desktop
        panel present the dialog before it returns to normal application
        Z-order.
        """
        dialog.update_idletasks()
        monitor = self.shell.target_monitor(self.model.store.data["ui"].get("target_monitor", "primary"))
        width = max(dialog.winfo_width(), dialog.winfo_reqwidth())
        height = max(dialog.winfo_height(), dialog.winfo_reqheight())
        x = monitor["left"] + max(0, (monitor["right"] - monitor["left"] - width) // 2)
        y = monitor["top"] + max(0, (monitor["bottom"] - monitor["top"] - height) // 2)
        dialog.geometry(f"+{x}+{y}")
        dialog.deiconify()
        dialog.attributes("-topmost", True)
        dialog.lift()
        dialog.focus_force()
        dialog.after(180, lambda current=dialog: self.release_dialog_topmost(current))

    def settings(self) -> None:
        if self.settings_dialog is not None and self.settings_dialog.winfo_exists():
            self.present_settings_dialog(self.settings_dialog)
            return
        dialog = tk.Toplevel(self.root)
        self.settings_dialog = dialog
        dialog.title("DESKFLOW 设置")
        dialog.configure(bg="#172238")
        dialog.resizable(False, False)
        dialog.protocol("WM_DELETE_WINDOW", lambda current=dialog: self.dismiss_settings(current))
        dialog.bind("<Escape>", lambda _event, current=dialog: self.dismiss_settings(current))
        ui = self.model.store.data["ui"]
        tk.Label(dialog, text="单元格大小", fg="white", bg="#172238").grid(row=0,column=0,padx=12,pady=9,sticky="w")
        cell = tk.Scale(dialog, from_=44,to=72,orient="horizontal",bg="#172238",fg="white",highlightthickness=0); cell.set(self.cell_size()); cell.grid(row=0,column=1)
        tk.Label(dialog, text="文字大小", fg="white", bg="#172238").grid(row=1,column=0,padx=12,pady=9,sticky="w")
        font = tk.Scale(dialog, from_=8,to=14,orient="horizontal",bg="#172238",fg="white",highlightthickness=0); font.set(max(8, min(14, ui.get("font_size", 9)))); font.grid(row=1,column=1)
        tk.Label(dialog, text="底板不透明度（Qt 模式内容不变）", fg="white", bg="#172238").grid(row=2,column=0,padx=12,pady=9,sticky="w")
        alpha = tk.Scale(dialog, from_=8,to=70,orient="horizontal",bg="#172238",fg="white",highlightthickness=0); alpha.set(round(float(ui.get("alpha", .22)) * 100)); alpha.grid(row=2,column=1)
        def choose():
            color = colorchooser.askcolor(color=ui["background"], parent=dialog)[1]
            if color: ui["background"] = color
        tk.Label(dialog, text="所有分组显示在", fg="white", bg="#172238").grid(row=3,column=0,padx=12,pady=9,sticky="w")
        monitors = self.shell.monitors(); labels = ["主屏幕"] + [f"屏幕 {i + 1} · {m.get('screen_right', m['right'])-m.get('screen_left', m['left'])}×{m.get('screen_bottom', m['bottom'])-m.get('screen_top', m['top'])}" for i, m in enumerate(monitors)]
        current = ui.get("target_monitor", "primary"); selected = tk.StringVar(value="主屏幕" if current == "primary" else labels[min(len(labels)-1, int(current.split(':')[1])+1)])
        monitor_select = ttk.Combobox(dialog, textvariable=selected, values=labels, state="readonly", width=25); monitor_select.grid(row=3,column=1,padx=12,pady=9)
        contrast = tk.BooleanVar(value=bool(ui.get("auto_contrast", True)))
        tk.Checkbutton(dialog, text="自动提高文字对比度", variable=contrast, fg="white", bg="#172238", selectcolor="#243451", activebackground="#172238", activeforeground="white").grid(row=4,column=0,columnspan=2,padx=12,pady=5,sticky="w")
        tk.Button(dialog,text="选择底色",command=choose).grid(row=5,column=0,padx=12,pady=9)
        tk.Button(dialog,text="保存并应用",command=lambda: self.save_settings(dialog, cell.get(), font.get(), alpha.get() / 100, contrast.get(), "primary" if selected.get() == "主屏幕" else f"monitor:{labels.index(selected.get())-1}")).grid(row=5,column=1,padx=12,pady=9)
        tk.Button(dialog,text="新建分组块",command=self.create_block).grid(row=6,column=0,columnspan=2,padx=12,pady=9,sticky="ew")
        self.present_settings_dialog(dialog)

    def create_block(self) -> None:
        clean = "未命名"
        existing = {block["id"] for block in self.model.store.data.get("custom_blocks", [])}
        identifier = "custom-" + hashlib.sha1(f"{clean}-{time.time_ns()}".encode("utf-8")).hexdigest()[:8]
        while identifier in existing:
            identifier = "custom-" + hashlib.sha1(f"{clean}-{time.time_ns()}".encode("utf-8")).hexdigest()[:8]
        palette = ["#8bd3ff", "#e3a7ff", "#8ee6bc", "#ffd37b"]
        self.model.store.data.setdefault("custom_blocks", []).append({"id": identifier, "title": clean, "color": palette[len(self.model.store.data["custom_blocks"]) % len(palette)]})
        self.model.store.data.setdefault("category_modes", {})[identifier] = "list"
        self.model.store.save()
        # A new empty block is independent: create just its own HWND/content.
        self.render(identifier)

    def save_settings(self, dialog: tk.Toplevel, cell_size: int, font_size: int, alpha: float, auto_contrast: bool, target_monitor: str) -> None:
        cell_size = max(44, min(72, int(cell_size)))
        ui = self.model.store.data["ui"]; ui.update({"cell_size": cell_size, "icon_size": max(20, min(32, round(cell_size * .48))), "font_size": max(8, min(14, int(font_size))), "alpha": alpha, "auto_contrast": auto_contrast, "geometry": self.root.geometry()})
        self.shell.remember_target_monitor(ui, target_monitor)
        # Explicit screen choice moves every block to that one monitor; never split a layout across displays.
        ui["auto_layout"] = True
        self.model.store.save(); self.dismiss_settings(dialog); self.render()

    def drag_start(self, event: tk.Event, category: str) -> None:
        panel = self.panels.get(category)
        if not panel: return
        # Reassert desktop-band order before the first drag frame.  Tk may
        # otherwise activate its top-level popup as a side effect of the click.
        self.shell.keep_panels_above_desktop_backdrop()
        self.drag_x, self.drag_y, self.drag_category = event.x_root, event.y_root, category
        self.drag_panel_x, self.drag_panel_y = panel.winfo_x(), panel.winfo_y()

    def snap_position(self, category: str, x: int, y: int, width: int, height: int) -> tuple[int, int]:
        """Magnetically align a dragged block with work-area and neighbour edges."""
        monitor = self.shell.target_monitor(self.model.store.data["ui"].get("target_monitor", "primary"))
        margin, gap, threshold = 12, 10, 18
        x_targets = [monitor["left"] + margin, monitor["right"] - width - margin]
        y_targets = [monitor["top"] + margin, monitor["bottom"] - height - margin]
        for name, panel in self.panels.items():
            if name == category or not panel.winfo_exists(): continue
            px, py, pw, ph = panel.winfo_x(), panel.winfo_y(), panel.winfo_width(), panel.winfo_height()
            # Shared edges plus the two non-overlapping, gap-preserving slots.
            x_targets.extend((px, px + pw - width, px - width - gap, px + pw + gap))
            y_targets.extend((py, py + ph - height, py - height - gap, py + ph + gap))
        nearest_x = min(x_targets, key=lambda target: abs(target - x))
        nearest_y = min(y_targets, key=lambda target: abs(target - y))
        return (nearest_x if abs(nearest_x - x) <= threshold else x,
                nearest_y if abs(nearest_y - y) <= threshold else y)

    def drag_move(self, event: tk.Event, category: str) -> None:
        if category != getattr(self, "drag_category", None): return
        panel = self.panels[category]
        geometry = f"{panel.winfo_width()}x{panel.winfo_height()}"
        wanted_x = self.drag_panel_x + event.x_root - self.drag_x
        wanted_y = self.drag_panel_y + event.y_root - self.drag_y
        x, y = self.snap_position(category, wanted_x, wanted_y, panel.winfo_width(), panel.winfo_height())
        monitor = self.shell.target_monitor(self.model.store.data["ui"].get("target_monitor", "primary")); margin = 12
        x = max(monitor["left"] + margin, min(x, monitor["right"] - panel.winfo_width() - margin))
        y = max(monitor["top"] + margin, min(y, monitor["bottom"] - panel.winfo_height() - margin))
        panel.geometry(f"{geometry}+{x}+{y}")
        # Geometry updates during a pressed drag can reorder an owned popup on
        # some Windows/RDP builds.  Keep this synchronous rather than waiting
        # for the 1.2s maintenance tick, so it never covers another app.
        self.shell.keep_panels_above_desktop_backdrop()

    def drag_end(self, _event: tk.Event, category: str) -> None:
        if category != getattr(self, "drag_category", None): return
        self.drag_category = None
        panel = self.panels.get(category)
        if not panel: return
        self.root.update_idletasks()
        self.active_profile["auto_layout"] = False
        self.active_profile.setdefault("panel_positions", {})[category] = f"+{panel.winfo_x()}+{panel.winfo_y()}"
        self.active_profile.setdefault("panel_sizes", {})[category] = {"width": panel.winfo_width(), "height": panel.winfo_height()}
        self.resolve_geometry_conflicts(category)
        self.model.store.save()
        self.shell.keep_panels_above_desktop_backdrop()

    def add_resize_grips(self, panel: tk.Toplevel, category: str) -> None:
        grips = (
            ("left", {"relx": 0, "rely": 0, "width": 5, "relheight": 1}, "sb_h_double_arrow"),
            ("right", {"relx": 1, "rely": 0, "x": -5, "width": 5, "relheight": 1}, "sb_h_double_arrow"),
            ("top", {"relx": 0, "rely": 0, "height": 5, "relwidth": 1}, "sb_v_double_arrow"),
            ("bottom", {"relx": 0, "rely": 1, "y": -5, "height": 5, "relwidth": 1}, "sb_v_double_arrow"),
            ("top_left", {"relx": 0, "rely": 0, "width": 10, "height": 10}, "size_nw_se"),
            ("top_right", {"relx": 1, "rely": 0, "x": -10, "width": 10, "height": 10}, "size_ne_sw"),
            ("bottom_left", {"relx": 0, "rely": 1, "y": -10, "width": 10, "height": 10}, "size_ne_sw"),
            ("bottom_right", {"relx": 1, "rely": 1, "x": -10, "y": -10, "width": 10, "height": 10}, "size_nw_se"),
        )
        for edge, place, cursor in grips:
            grip = tk.Frame(panel, bg="#19263a", cursor=cursor)
            grip.place(**place)
            grip.bind("<ButtonPress-1>", lambda event, c=category, e=edge: self.resize_start(event, c, e))
            grip.bind("<B1-Motion>", self.resize_move)
            grip.bind("<ButtonRelease-1>", self.resize_end)
            self.tooltip(grip, "拖动调整此块的宽度和高度")

    def resize_start(self, event: tk.Event, category: str, edge: str) -> None:
        panel = self.panels.get(category)
        if not panel: return
        self.shell.keep_panels_above_desktop_backdrop()
        self.resize_state = {
            "category": category, "edge": edge, "root_x": event.x_root, "root_y": event.y_root,
            "x": panel.winfo_x(), "y": panel.winfo_y(), "width": panel.winfo_width(), "height": panel.winfo_height(),
        }

    def resize_move(self, event: tk.Event) -> None:
        state = getattr(self, "resize_state", None)
        if not state: return
        panel = self.panels.get(state["category"])
        if not panel: return
        monitor = self.shell.target_monitor(self.model.store.data["ui"].get("target_monitor", "primary"))
        dx, dy = event.x_root - state["root_x"], event.y_root - state["root_y"]
        resized = resize_rectangle(Rect(state["category"], state["x"], state["y"], state["width"], state["height"]), state["edge"], dx, dy,
                                   (monitor["left"], monitor["top"], monitor["right"], monitor["bottom"]))
        panel.geometry(f"{resized.width}x{resized.height}+{resized.x}+{resized.y}")
        self.shell.keep_panels_above_desktop_backdrop()

    def resize_end(self, _event: tk.Event) -> None:
        state = getattr(self, "resize_state", None)
        self.resize_state = None
        if not state: return
        category = state["category"]; panel = self.panels.get(category)
        if not panel: return
        self.root.update_idletasks()
        self.active_profile["auto_layout"] = False
        self.active_profile.setdefault("panel_positions", {})[category] = f"+{panel.winfo_x()}+{panel.winfo_y()}"
        self.active_profile.setdefault("panel_sizes", {})[category] = {"width": panel.winfo_width(), "height": panel.winfo_height()}
        self.resolve_geometry_conflicts(category)
        self.model.store.save()
        self.shell.keep_panels_above_desktop_backdrop()

    def resolve_geometry_conflicts(self, anchor: str) -> list[str]:
        """Move only panels touched by a drag/resize; never recreate widgets."""
        moved: list[str] = []
        limit = max(1, len(self.panels) * 2)
        for _pass in range(limit):
            self.root.update_idletasks()
            rects = {name: Rect(name, panel.winfo_x(), panel.winfo_y(), panel.winfo_width(), panel.winfo_height())
                     for name, panel in self.panels.items() if panel.winfo_exists()}
            conflict: tuple[str, str] | None = None
            names = list(rects)
            for index, first in enumerate(names):
                for second in names[index + 1:]:
                    if overlaps(rects[first], rects[second], gap=0):
                        conflict = (first, second); break
                if conflict: break
            if not conflict: break
            first, second = conflict
            victim = second if first == anchor else first if second == anchor else second
            panel = self.panels[victim]; old = rects[victim]
            x, y = self.safe_position(victim, old.x, old.y, old.width, old.height)
            if (x, y) == (old.x, old.y): break
            panel.geometry(f"{old.width}x{old.height}+{x}+{y}")
            self.active_profile.setdefault("panel_positions", {})[victim] = f"+{x}+{y}"
            self.active_profile.setdefault("panel_sizes", {})[victim] = {"width": old.width, "height": old.height}
            if victim not in moved: moved.append(victim)
        return moved

    def close(self) -> None:
        self.model.store.save(); self.shell.restore_icons(); self.root.destroy()
    def run(self) -> None: self.root.mainloop()


def main() -> None:
    parser = argparse.ArgumentParser(); parser.add_argument("--headless", action="store_true")
    args = parser.parse_args()
    if not args.headless:
        enable_per_monitor_dpi_awareness()
    # The overlay is occasionally launched directly by legacy shortcuts or
    # tools.  It must never leave the user without the tray manager: hand the
    # launch back to the manager, which then owns the overlay lifecycle.
    if not args.headless:
        kernel32 = ctypes.windll.kernel32; kernel32.OpenMutexW.restype = ctypes.c_void_p
        manager_mutex = kernel32.OpenMutexW(0x00100000, False, "Local\\DeskflowOrganizer.Manager")
        if manager_mutex:
            kernel32.CloseHandle(manager_mutex)
        else:
            manager_script = Path(__file__).with_name("deskflow_manager.py")
            gui_executable = Path(sys.executable).with_name("pythonw.exe")
            subprocess.Popen(command("manager", "--start"), cwd=APP_ROOT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return
    # The BAT may be double-clicked repeatedly.  Only the visible overlay owns
    # this mutex; headless inspection/service commands remain independently
    # usable by the local MCP bridge.
    mutex = None
    if not args.headless:
        kernel32 = ctypes.windll.kernel32; kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.SetLastError(0); mutex = kernel32.CreateMutexW(None, False, "Local\\SmartDesktopOrganizer.Overlay")
        if not mutex or kernel32.GetLastError() == 183: return  # ERROR_ALREADY_EXISTS
    store = Store(); model = OrganizerModel(store); events: queue.Queue[dict[str, Any]] = queue.Queue()
    service = ControlService(model, events)
    threading.Thread(target=service.serve, name="deskflow-api", daemon=True).start()
    if args.headless:
        print(json.dumps({"service": "http://127.0.0.1:8765", "categories": model.scan()}, ensure_ascii=False)); threading.Event().wait()
    else:
        # Qt paints the plate and content at different alpha values.  Keep the
        # mature Tk renderer available only as an explicit emergency rollback;
        # it cannot satisfy independent background/content transparency.
        if os.environ.get("DESKFLOW_UI", "qt").casefold() == "tk":
            OrganizerUI(model, events).run()
        else:
            from qt_desktop import QtOrganizerUI
            QtOrganizerUI(model, events, DesktopShell(), all_categories).run()

if __name__ == "__main__": main()
