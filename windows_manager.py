import ctypes
import json
import os
import struct
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
import tkinter as tk
import tkinter.font as tkFont
from tkinter import filedialog, messagebox, ttk

try:
    from PIL import Image, ImageDraw, ImageTk
except ImportError:
    Image = ImageDraw = ImageTk = None


CONFIG_FILE = Path.home() / ".windows_manager.json"
RESULT_CACHE_FILE = Path.home() / ".windows_manager_results.json"
DEFAULT_BLOCKED_KEYWORDS = (
    "unins", "update", "patch", "setup", "config", "helper", "crash",
    "service", "cli", "sdk",
)
IMAGE_SUBSYSTEM_WINDOWS_GUI = 2
IMAGE_SUBSYSTEM_WINDOWS_CUI = 3
CREATE_NO_WINDOW = 0x08000000
SCAN_WORKERS = max(4, min(16, (os.cpu_count() or 4) * 2))
SCAN_CACHE = {}
SCAN_CACHE_LOCK = threading.Lock()
ICON_CACHE = {}
ICON_CACHE_LOCK = threading.Lock()


@dataclass(frozen=True)
class ExecutableInfo:
    file_description: str = ""
    product_name: str = ""
    company_name: str = ""
    subsystem: int | None = None
    has_icon: bool = False

    @property
    def display_name(self):
        return self.product_name or self.file_description or ""

    @property
    def has_version_metadata(self):
        return bool(self.file_description or self.product_name or self.company_name)

    @property
    def has_required_identity(self):
        return bool(self.product_name or self.company_name)

    @property
    def subsystem_text(self):
        return {IMAGE_SUBSYSTEM_WINDOWS_GUI: "GUI", IMAGE_SUBSYSTEM_WINDOWS_CUI: "CUI"}.get(
            self.subsystem, "未知"
        )

    @property
    def confidence_score(self):
        score = 0
        if self.has_version_metadata:
            score += 40
        if self.subsystem == IMAGE_SUBSYSTEM_WINDOWS_GUI:
            score += 30
        if self.has_icon:
            score += 20
        return score + 10


@dataclass(frozen=True)
class Application:
    name: str
    path: str
    size: int
    modified: float
    metadata: ExecutableInfo = field(default_factory=ExecutableInfo)
    filter_note: str = ""

    @property
    def modified_text(self):
        return datetime.fromtimestamp(self.modified).strftime("%Y-%m-%d %H:%M")

    @property
    def size_text(self):
        size = float(self.size)
        for unit in ("B", "KB", "MB", "GB"):
            if size < 1024 or unit == "GB":
                return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
            size /= 1024


def load_config():
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_config(folders, excludes, filters=None, app_folders=None,
                desktop_order=None, dock_paths=None, desktop_settings=None,
                hidden_paths=None, badges=None):
    data = {"folders": folders, "excludes": excludes, "filters": filters or {}}
    if app_folders is not None:
        data["app_folders"] = {name: sorted(paths) for name, paths in app_folders.items()}
    if desktop_order is not None:
        data["desktop_order"] = list(desktop_order)
    if dock_paths is not None:
        data["dock_paths"] = list(dock_paths)
    if desktop_settings is not None:
        data["desktop_settings"] = desktop_settings
    if hidden_paths is not None:
        data["hidden_paths"] = sorted(hidden_paths)
    if badges is not None:
        data["badges"] = dict(badges)
    try:
        CONFIG_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def _scan_context(folders, excludes, filters):
    return {
        "folders": sorted(os.path.normcase(os.path.abspath(path)) for path in folders),
        "excludes": sorted(os.path.normcase(os.path.abspath(path)) for path in excludes),
        "filters": filters,
    }


def _application_to_dict(app):
    return {
        "name": app.name,
        "path": app.path,
        "size": app.size,
        "modified": app.modified,
        "filter_note": app.filter_note,
        "metadata": {
            "file_description": app.metadata.file_description,
            "product_name": app.metadata.product_name,
            "company_name": app.metadata.company_name,
            "subsystem": app.metadata.subsystem,
            "has_icon": app.metadata.has_icon,
        },
    }


def _application_from_dict(data):
    metadata = data.get("metadata", {})
    return Application(
        name=str(data["name"]), path=str(data["path"]), size=int(data["size"]),
        modified=float(data["modified"]), filter_note=str(data.get("filter_note", "")),
        metadata=ExecutableInfo(
            file_description=str(metadata.get("file_description", "")),
            product_name=str(metadata.get("product_name", "")),
            company_name=str(metadata.get("company_name", "")),
            subsystem=metadata.get("subsystem"),
            has_icon=bool(metadata.get("has_icon", False)),
        ),
    )


def save_scan_cache(folders, excludes, filters, apps):
    payload = {"context": _scan_context(folders, excludes, filters),
               "applications": [_application_to_dict(app) for app in apps]}
    try:
        RESULT_CACHE_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def load_scan_cache(folders, excludes, filters):
    try:
        payload = json.loads(RESULT_CACHE_FILE.read_text(encoding="utf-8"))
        if payload.get("context") != _scan_context(folders, excludes, filters):
            return []
        apps = []
        for item in payload.get("applications", []):
            app = _application_from_dict(item)
            try:
                stat = os.stat(app.path)
                if stat.st_size == app.size and stat.st_mtime == app.modified:
                    apps.append(app)
            except (OSError, ValueError, TypeError, KeyError):
                continue
        return apps
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return []


def _read_version_resource(path):
    result = {"FileDescription": "", "ProductName": "", "CompanyName": ""}
    if os.name != "nt":
        return result
    try:
        version = ctypes.windll.version
        version.GetFileVersionInfoSizeW.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_uint)]
        version.GetFileVersionInfoSizeW.restype = ctypes.c_uint
        size = version.GetFileVersionInfoSizeW(path, None)
        if not size:
            return result
        buffer = ctypes.create_string_buffer(size)
        if not version.GetFileVersionInfoW(path, 0, size, buffer):
            return result
        value = ctypes.c_void_p()
        length = ctypes.c_uint()
        query = ctypes.c_wchar_p("\\VarFileInfo\\Translation")
        if not version.VerQueryValueW(buffer, query, ctypes.byref(value), ctypes.byref(length)) or not length.value:
            return result
        language, codepage = struct.unpack_from("<HH", ctypes.string_at(value, 4))
        for key in result:
            query = f"\\StringFileInfo\\{language:04x}{codepage:04x}\\{key}"
            value = ctypes.c_void_p()
            length = ctypes.c_uint()
            if version.VerQueryValueW(buffer, query, ctypes.byref(value), ctypes.byref(length)) and value.value:
                result[key] = ctypes.wstring_at(value.value).strip()
    except (AttributeError, OSError, ValueError, struct.error):
        pass
    return result


def _read_pe_subsystem(path):
    try:
        with open(path, "rb") as stream:
            header = stream.read(4096)
        if len(header) < 0x40 or header[:2] != b"MZ":
            return None
        pe_offset = struct.unpack_from("<I", header, 0x3C)[0]
        if pe_offset + 26 > len(header) or header[pe_offset:pe_offset + 4] != b"PE\0\0":
            return None
        optional_offset = pe_offset + 24
        magic = struct.unpack_from("<H", header, optional_offset)[0]
        if magic not in (0x10B, 0x20B) or optional_offset + 70 > len(header):
            return None
        return struct.unpack_from("<H", header, optional_offset + 68)[0]
    except (OSError, struct.error):
        return None


def _has_executable_icon(path):
    if os.name != "nt":
        return False
    try:
        class ShFileInfo(ctypes.Structure):
            _fields_ = [("hIcon", ctypes.c_void_p), ("iIcon", ctypes.c_int),
                        ("dwAttributes", ctypes.c_uint), ("szDisplayName", ctypes.c_wchar * 260),
                        ("szTypeName", ctypes.c_wchar * 80)]
        info = ShFileInfo()
        flags = 0x000000100 | 0x000000001
        result = ctypes.windll.shell32.SHGetFileInfoW(path, 0, ctypes.byref(info), ctypes.sizeof(info), flags)
        if not result or not info.hIcon:
            return False
        ctypes.windll.user32.DestroyIcon(info.hIcon)
        return True
    except (AttributeError, OSError):
        return False


def _extract_icon_image(path, size=96):
    if os.name != "nt" or Image is None:
        return None
    try:
        class IconInfo(ctypes.Structure):
            _fields_ = [("fIcon", ctypes.c_int), ("xHotspot", ctypes.c_uint),
                        ("yHotspot", ctypes.c_uint), ("hbmMask", ctypes.c_void_p),
                        ("hbmColor", ctypes.c_void_p)]

        class BitmapInfoHeader(ctypes.Structure):
            _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32),
                        ("biHeight", ctypes.c_int32), ("biPlanes", ctypes.c_uint16),
                        ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
                        ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_int32),
                        ("biYPelsPerMeter", ctypes.c_int32), ("biClrUsed", ctypes.c_uint32),
                        ("biClrImportant", ctypes.c_uint32)]

        class BitmapInfo(ctypes.Structure):
            _fields_ = [("bmiHeader", BitmapInfoHeader), ("bmiColors", ctypes.c_uint32 * 3)]

        icons = (ctypes.c_void_p * 1)()
        extract_icon = ctypes.windll.shell32.ExtractIconExW
        extract_icon.argtypes = [ctypes.c_wchar_p, ctypes.c_int,
                                 ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
                                 ctypes.c_uint]
        extract_icon.restype = ctypes.c_uint
        if not extract_icon(path, 0, icons, None, 1):
            return None
        hicon = icons[0]
        if not hicon:
            return None
        icon_info = IconInfo()
        if not ctypes.windll.user32.GetIconInfo(hicon, ctypes.byref(icon_info)):
            ctypes.windll.user32.DestroyIcon(hicon)
            return None
        bitmap = BitmapInfo()
        bitmap.bmiHeader.biSize = ctypes.sizeof(BitmapInfoHeader)
        bitmap.bmiHeader.biWidth = size
        bitmap.bmiHeader.biHeight = -size
        bitmap.bmiHeader.biPlanes = 1
        bitmap.bmiHeader.biBitCount = 32
        pixels = ctypes.create_string_buffer(size * size * 4)
        screen_dc = ctypes.windll.user32.GetDC(0)
        memory_dc = ctypes.windll.gdi32.CreateCompatibleDC(screen_dc)
        copied = ctypes.windll.gdi32.GetDIBits(memory_dc, icon_info.hbmColor, 0, size,
                                               pixels, ctypes.byref(bitmap), 0)
        ctypes.windll.gdi32.DeleteDC(memory_dc)
        ctypes.windll.user32.ReleaseDC(0, screen_dc)
        ctypes.windll.gdi32.DeleteObject(icon_info.hbmColor)
        ctypes.windll.gdi32.DeleteObject(icon_info.hbmMask)
        ctypes.windll.user32.DestroyIcon(hicon)
        if not copied:
            return None
        return Image.frombuffer("RGBA", (size, size), pixels, "raw", "BGRA", 0, 1).copy()
    except (AttributeError, OSError, ctypes.ArgumentError):
        return None


def get_icon_image(app, size=96):
    key = (os.path.normcase(app.path), app.size, app.modified, size)
    with ICON_CACHE_LOCK:
        if key in ICON_CACHE:
            return ICON_CACHE[key]
    image = _extract_icon_image(app.path, size)
    with ICON_CACHE_LOCK:
        ICON_CACHE[key] = image
    return image


def inspect_executable(path, size):
    version = _read_version_resource(path)
    return ExecutableInfo(
        file_description=version["FileDescription"],
        product_name=version["ProductName"],
        company_name=version["CompanyName"],
        subsystem=_read_pe_subsystem(path),
        has_icon=_has_executable_icon(path),
    )


def _matches_list(path, entries):
    normalized_path = os.path.normcase(os.path.abspath(path))
    filename = os.path.basename(path).lower()
    return any(item and (os.path.normcase(os.path.abspath(item)) == normalized_path
                         or item.lower() in filename) for item in entries)


def filter_executable(path, size, metadata, filters):
    normalized_path = os.path.normcase(os.path.abspath(path)).lower()
    denylist = [item.lower() for item in filters.get("denylist", []) if item]
    blocked = tuple(dict.fromkeys(DEFAULT_BLOCKED_KEYWORDS + tuple(denylist)))
    if any(keyword in normalized_path for keyword in blocked):
        return False, "文件名特征"
    if not metadata.has_icon:
        return False, "无可提取图标"
    if size < int(filters.get("min_size", 100 * 1024)):
        return False, "体积过小"
    if not metadata.has_required_identity:
        return False, "缺少产品名或公司名"
    if metadata.subsystem != IMAGE_SUBSYSTEM_WINDOWS_GUI:
        return False, "非 GUI 程序"
    return True, "GUI 应用"


def _scan_one_executable(path, filters):
    try:
        stat = os.stat(path)
        filter_key = json.dumps(filters, sort_keys=True, ensure_ascii=False)
        cache_key = (os.path.normcase(path), stat.st_size, stat.st_mtime_ns, filter_key)
        with SCAN_CACHE_LOCK:
            if cache_key in SCAN_CACHE:
                return SCAN_CACHE[cache_key]
        metadata = inspect_executable(path, stat.st_size)
        include, note = filter_executable(path, stat.st_size, metadata, filters)
        if not include:
            result = None
        else:
            result = Application(metadata.display_name or Path(path).stem, path, stat.st_size,
                                 stat.st_mtime, metadata, note)
        with SCAN_CACHE_LOCK:
            SCAN_CACHE[cache_key] = result
        return result
    except Exception:
        return None


def scan_executables(folders, excludes, filters=None, on_item=None, on_progress=None):
    filters = filters or {}
    excluded = {os.path.normcase(os.path.abspath(path)) for path in excludes}
    paths = {}
    for folder in folders:
        root = os.path.abspath(folder)
        if not os.path.isdir(root):
            continue
        for current, directories, files in os.walk(root, topdown=True, onerror=lambda _: None):
            current_normalized = os.path.normcase(os.path.abspath(current))
            if any(current_normalized == item or current_normalized.startswith(item + os.sep)
                   for item in excluded):
                directories[:] = []
                continue
            directories[:] = [directory for directory in directories
                              if os.path.normcase(os.path.join(current, directory)) not in excluded]
            for filename in files:
                if not filename.lower().endswith(".exe"):
                    continue
                path = os.path.abspath(os.path.join(current, filename))
                key = os.path.normcase(path)
                paths[key] = path

    if on_progress:
        on_progress(0, len(paths))
    found = {}
    with ThreadPoolExecutor(max_workers=SCAN_WORKERS, thread_name_prefix="exe-scan") as executor:
        futures = [executor.submit(_scan_one_executable, path, filters) for path in paths.values()]
        for completed, future in enumerate(as_completed(futures), 1):
            app = future.result()
            if app is not None:
                found[os.path.normcase(app.path)] = app
            if on_progress:
                on_progress(completed, len(paths))
    merged = {}
    for app in found.values():
        name_key = app.name.strip().casefold()
        existing = merged.get(name_key)
        if existing is None or (app.metadata.confidence_score, app.size) > \
                (existing.metadata.confidence_score, existing.size):
            merged[name_key] = app
    results = list(merged.values())
    if on_item:
        for app in results:
            on_item(app)
    return results


class ApplicationManager(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Windows 应用资源库")
        self.geometry("1250x720")
        self.config_data = load_config()
        self.folders = list(self.config_data.get("folders", []))
        self.excludes = list(self.config_data.get("excludes", []))
        saved_filters = self.config_data.get("filters", {})
        self.filters = {
            "gui_only": saved_filters.get("gui_only", True),
            "require_version": saved_filters.get("require_version", True),
            "min_size": self._read_min_size(saved_filters.get("min_size", 100 * 1024)),
            "allowlist": list(saved_filters.get("allowlist", [])),
            "denylist": list(saved_filters.get("denylist", [])),
        }
        self.apps = []
        self.filtered_apps = []
        self.app_by_iid = {}
        self.selected_app_path = None
        self.android_app_cards = []
        self._android_items = []
        self.app_folders = {
            str(name): set(paths)
            for name, paths in self.config_data.get("app_folders", {}).items()
            if isinstance(paths, list)
        }
        self.desktop_order = list(self.config_data.get("desktop_order", []))
        self.dock_paths = list(self.config_data.get("dock_paths", []))
        self.android_page = 0
        self.android_page_size = 20
        self.android_last_width = 0
        self.drag_app_path = None
        self.android_press_after = None
        self.android_dragging = False
        self.android_drag_path = None
        self.android_drag_iid = None
        self.android_drag_target = None
        desktop_settings = self.config_data.get("desktop_settings", {})
        if not isinstance(desktop_settings, dict):
            desktop_settings = {}
        self.desktop_columns = self._read_grid_value(desktop_settings.get("columns", 5), 4, 8, 5)
        self.desktop_rows = self._read_grid_value(desktop_settings.get("rows", 4), 3, 8, 4)
        self.layout_mode = desktop_settings.get("layout_mode", "grid")
        if self.layout_mode not in ("grid", "free"):
            self.layout_mode = "grid"
        self.icon_shape = desktop_settings.get("icon_shape", "rounded")
        if self.icon_shape not in ("square", "rounded", "circle"):
            self.icon_shape = "rounded"
        saved_positions = desktop_settings.get("positions", {})
        self.desktop_positions = {
            str(path): (int(position[0]), int(position[1]))
            for path, position in (saved_positions.items()
                                   if isinstance(saved_positions, dict) else [])
            if isinstance(position, (list, tuple)) and len(position) == 2
        }
        self.android_drag_offset = (0, 0)
        saved_hidden = self.config_data.get("hidden_paths", [])
        self.hidden_paths = set(saved_hidden if isinstance(saved_hidden, list) else [])
        saved_badges = self.config_data.get("badges", {})
        self.badges = {str(path): str(value) for path, value in
                       (saved_badges.items() if isinstance(saved_badges, dict) else [])}
        self.build_ui()
        self.refresh_folder_list()
        self.apps = load_scan_cache(self.folders, self.excludes, self.filters)
        if self.apps:
            self.status.config(text=f"已加载缓存：{len(self.apps)} 个应用（点击「扫描」刷新）")
            self.render_apps()
        else:
            self.status.config(text="暂无缓存结果，请点击「扫描」开始")

    @staticmethod
    def _read_min_size(value):
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return 100 * 1024

    @staticmethod
    def _read_grid_value(value, minimum, maximum, fallback):
        try:
            return max(minimum, min(maximum, int(value)))
        except (TypeError, ValueError):
            return fallback

    def build_ui(self):
        self._setup_theme()
        # 顶部：仅保留设置按钮（毛玻璃质感深色栏）
        top_bar = tk.Frame(self, bg=self.COLORS["nav"], height=48)
        top_bar.pack(fill=tk.X)
        top_bar.pack_propagate(False)
        ttk.Button(top_bar, text="设置", style="Nav.TButton",
                   command=self.open_settings).pack(side=tk.RIGHT, padx=16)

        # 应用显示区
        app_area = tk.Frame(self, bg=self.COLORS["bg"])
        app_area.pack(fill=tk.BOTH, expand=True, padx=14, pady=(0, 10))

        # 扫描进度条（仅扫描时显示）
        self.scan_progress_frame = tk.Frame(app_area, bg=self.COLORS["bg"])
        self.scan_progress_frame.pack(fill=tk.X)
        self.progress = ttk.Progressbar(self.scan_progress_frame, mode="determinate", maximum=1, value=0)
        self.status = ttk.Label(self.scan_progress_frame, text="准备就绪",
                                anchor=tk.CENTER, foreground=self.COLORS["label_secondary"])
        self.status.pack(fill=tk.X, pady=(4, 0))

        # 应用网格
        grid_frame = tk.Frame(app_area, bg=self.COLORS["bg"])
        grid_frame.pack(fill=tk.BOTH, expand=True, pady=(8, 0))

        self.card_canvas = tk.Canvas(grid_frame, highlightthickness=0, borderwidth=0,
                                     bg=self.COLORS["bg"])
        card_scroll = ttk.Scrollbar(grid_frame, orient=tk.VERTICAL, command=self.card_canvas.yview)
        self.card_canvas.configure(yscrollcommand=card_scroll.set)
        card_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.card_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.card_frame = tk.Frame(self.card_canvas, bg=self.COLORS["bg"])
        self.card_window = self.card_canvas.create_window((0, 0), window=self.card_frame, anchor="nw")
        self.card_frame.bind("<Configure>", lambda _: self.card_canvas.configure(
            scrollregion=self.card_canvas.bbox("all")))
        self.card_canvas.bind("<Configure>", self._resize_card_frame)

        # 底部分页圆点指示 + Dock（毛玻璃）
        self.page_indicator = tk.Frame(app_area, bg=self.COLORS["bg"])
        self.page_indicator.pack(pady=(8, 0))
        self.dock_frame = tk.Frame(app_area, bg=self.COLORS["dock"],
                                   highlightthickness=0)
        self.dock_frame.pack(fill=tk.X, pady=(10, 0))

        # 左右滑动切换分页
        self._swipe_x = 0
        self._swipe_y = 0
        self._swipe_active = False
        self.bind("<ButtonPress-1>", self._swipe_press, add="+")
        self.bind("<ButtonRelease-1>", self._swipe_release, add="+")

    def _setup_theme(self):
        # iOS HIG 深色模式设计令牌
        self.COLORS = {
            "bg": "#000000",              # systemBackground
            "nav": "#0A0A0A",             # 导航栏（毛玻璃深色）
            "card": "#1C1C1E",            # secondarySystemGroupedBackground
            "card_alt": "#2C2C2E",        # tertiarySystemGroupedBackground
            "card_pressed": "#3A3A3C",    # 按压态
            "label": "#FFFFFF",           # label
            "label_secondary": "#8E8E93",  # secondaryLabel
            "label_tertiary": "#48484A",  # tertiaryLabel
            "accent": "#007AFF",          # systemBlue
            "red": "#FF453A",             # systemRed
            "separator": "#38383A",       # opaqueSeparator
            "dock": "#1C1C1E",            # Dock 毛玻璃
        }
        # 字体：优先 SF Pro，回退 Segoe UI，中文自动回退至系统中文字体
        families = set(tkFont.families())
        for candidate in ("SF Pro Display", "SF Pro Text", "Segoe UI Variable",
                          "Segoe UI", "Microsoft YaHei UI"):
            if candidate in families:
                self.font_family = candidate
                break
        else:
            self.font_family = "TkDefaultFont"

        self.configure(bg=self.COLORS["bg"])

        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        ff = self.font_family
        style.configure(".", background=self.COLORS["bg"], foreground=self.COLORS["label"],
                        font=(ff, 10))
        style.configure("TFrame", background=self.COLORS["bg"])
        style.configure("TLabel", background=self.COLORS["bg"], foreground=self.COLORS["label"],
                        font=(ff, 10))
        style.configure("Card.TLabel", background=self.COLORS["card"], foreground=self.COLORS["label"])
        style.configure("Secondary.TLabel", background=self.COLORS["bg"],
                        foreground=self.COLORS["label_secondary"], font=(ff, 9))
        style.configure("TButton", background=self.COLORS["card"], foreground=self.COLORS["label"],
                        font=(ff, 10, "bold"), borderwidth=0, padding=(14, 8),
                        focusthickness=0, anchor="center")
        style.map("TButton",
                  background=[("active", self.COLORS["card_alt"]),
                              ("pressed", self.COLORS["card_pressed"])],
                  foreground=[("disabled", self.COLORS["label_tertiary"])])
        style.configure("Nav.TButton", background=self.COLORS["nav"], foreground=self.COLORS["accent"],
                        font=(ff, 10, "bold"), borderwidth=0, padding=(6, 4), focusthickness=0)
        style.map("Nav.TButton",
                  background=[("active", self.COLORS["card"]), ("pressed", self.COLORS["card_alt"])])
        style.configure("Accent.TButton", background=self.COLORS["accent"], foreground="#FFFFFF",
                        font=(ff, 10, "bold"), borderwidth=0, padding=(16, 8), focusthickness=0)
        style.map("Accent.TButton",
                  background=[("active", "#0A84FF"), ("pressed", "#0062CC")])
        style.configure("TEntry", fieldbackground=self.COLORS["card"], foreground=self.COLORS["label"],
                        insertcolor=self.COLORS["label"], borderwidth=0, padding=6)
        style.configure("TCombobox", fieldbackground=self.COLORS["card"], foreground=self.COLORS["label"],
                        background=self.COLORS["card"], arrowcolor=self.COLORS["label_secondary"],
                        borderwidth=0, padding=4)
        style.map("TCombobox", fieldbackground=[("readonly", self.COLORS["card"])])
        style.configure("Horizontal.TProgressbar", background=self.COLORS["accent"],
                        troughcolor=self.COLORS["card_alt"], borderwidth=0, thickness=6)
        style.configure("Vertical.TScrollbar", background=self.COLORS["card"],
                        troughcolor=self.COLORS["bg"], arrowcolor=self.COLORS["label_secondary"],
                        borderwidth=0)
        style.map("Vertical.TScrollbar", background=[("active", self.COLORS["card_alt"])])

        # 下拉框弹出列表深色化
        self.option_add("*TCombobox*Listbox.background", self.COLORS["card"])
        self.option_add("*TCombobox*Listbox.foreground", self.COLORS["label"])
        self.option_add("*TCombobox*Listbox.selectBackground", self.COLORS["accent"])
        self.option_add("*TCombobox*Listbox.selectForeground", "#FFFFFF")
        self.option_add("*TCombobox*Listbox.borderWidth", 0)

    def _make_rounded_image(self, width, height, radius, color, inset=0):
        """生成带透明背景的圆角矩形 PIL 图像，用于卡片/徽章背景。"""
        if Image is None:
            return None
        image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        if ImageDraw:
            draw = ImageDraw.Draw(image)
            x1, y1 = inset, inset
            x2, y2 = width - 1 - inset, height - 1 - inset
            draw.rounded_rectangle((x1, y1, x2, y2),
                                   radius=max(1, radius - inset), fill=color)
        return image

    def _set_card_background(self, card, card_w, card_h, radius):
        """设置卡片背景；PIL 可用时用 Squircle 圆角图，否则回退为画布矩形。"""
        if ImageTk is not None and Image is not None:
            card._bg_normal = ImageTk.PhotoImage(
                self._make_rounded_image(card_w, card_h, radius, self.COLORS["card"]))
            card._bg_pressed = ImageTk.PhotoImage(
                self._make_rounded_image(card_w, card_h, radius,
                                         self.COLORS["card_pressed"], inset=2))
            card.bg_id = card.create_image(card_w / 2, card_h / 2, image=card._bg_normal)
            card._bg_kind = "image"
        else:
            card.bg_id = card.create_rectangle(1, 1, max(2, card_w - 1), max(2, card_h - 1),
                                               fill=self.COLORS["card"], outline="")
            card._bg_kind = "rect"

    def _card_press(self, card):
        """按压反馈：内缩深色背景，模拟 scale(0.97)。"""
        if not getattr(card, "bg_id", None):
            return
        if getattr(card, "_bg_kind", "image") == "image":
            if hasattr(card, "_bg_pressed"):
                card.itemconfigure(card.bg_id, image=card._bg_pressed)
        else:
            card.itemconfigure(card.bg_id, fill=self.COLORS["card_pressed"])

    def _card_release(self, card):
        if not getattr(card, "bg_id", None):
            return
        if getattr(card, "_bg_kind", "image") == "image":
            if hasattr(card, "_bg_normal"):
                card.itemconfigure(card.bg_id, image=card._bg_normal)
        else:
            card.itemconfigure(card.bg_id, fill=self.COLORS["card"])


    def refresh_folder_list(self):
        # 扫描目录已迁移至设置窗口，主界面不再维护列表
        pass

    def save_user_config(self):
        save_config(self.folders, self.excludes, self.filters, self.app_folders,
                    self.desktop_order, self.dock_paths, {
                        "columns": self.desktop_columns,
                        "rows": self.desktop_rows,
                        "layout_mode": self.layout_mode,
                        "icon_shape": self.icon_shape,
                        "positions": self.desktop_positions,
                    }, self.hidden_paths, self.badges)

    def change_android_page(self, delta):
        page_size = self.desktop_columns * self.desktop_rows
        page_count = max(1, (len(self._android_items) + page_size - 1) // page_size)
        self.android_page = max(0, min(self.android_page + delta, page_count - 1))
        self._render_android_page()

    def _update_page_indicator(self):
        for child in self.page_indicator.winfo_children():
            child.destroy()
        page_size = self.desktop_columns * self.desktop_rows
        page_count = max(1, (len(self._android_items) + page_size - 1) // page_size)
        for index in range(page_count):
            color = self.COLORS["accent"] if index == self.android_page else self.COLORS["label_tertiary"]
            dot = tk.Label(self.page_indicator, text="●",
                           font=(self.font_family, 9), fg=color,
                           bg=self.COLORS["bg"])
            dot.pack(side=tk.LEFT, padx=3)

    def _in_card_area(self, event):
        try:
            x, y = event.x_root, event.y_root
            cx = self.card_canvas.winfo_rootx()
            cy = self.card_canvas.winfo_rooty()
            cw = self.card_canvas.winfo_width()
            ch = self.card_canvas.winfo_height()
            return cx <= x <= cx + cw and cy <= y <= cy + ch
        except tk.TclError:
            return False

    def _swipe_press(self, event):
        if not self._in_card_area(event):
            self._swipe_active = False
            return
        self._swipe_x = event.x_root
        self._swipe_y = event.y_root
        self._swipe_active = True

    def _swipe_release(self, event):
        if not self._swipe_active:
            return
        self._swipe_active = False
        # 正在拖拽应用卡片时不触发翻页
        if self.android_dragging:
            return
        delta_x = event.x_root - self._swipe_x
        delta_y = event.y_root - self._swipe_y
        if abs(delta_x) < 60 or abs(delta_x) < abs(delta_y) * 1.5:
            return
        # 向右滑动回到上一页，向左滑动进入下一页
        self.change_android_page(-1 if delta_x > 0 else 1)

    def add_folder(self):
        folder = filedialog.askdirectory(title="选择扫描目录")
        if folder and folder not in self.folders:
            self.folders.append(folder)
            self.save_user_config()
            self.refresh_folder_list()

    def remove_folder(self):
        # 目录管理已迁移至设置窗口，主界面不再提供删除入口
        return

    def start_scan(self):
        if not self.folders:
            self.status.config(text="请先在「设置」中添加扫描目录")
            return
        if hasattr(self, "scan_button") and self.scan_button.winfo_exists():
            self.scan_button.config(state=tk.DISABLED)
        self.progress.pack(fill=tk.X, before=self.status)
        self.progress.configure(mode="indeterminate", value=0)
        self.progress.start(12)
        self.status.config(text="正在扫描并进行降噪筛选……")
        self.apps = []
        self.render_apps()

        def worker():
            def progress(done, total):
                self.after(0, lambda: self.update_scan_progress(done, total))

            results = scan_executables(self.folders, self.excludes, self.filters,
                                       on_progress=progress)
            self.after(0, lambda: self.scan_finished(results))

        threading.Thread(target=worker, daemon=True).start()

    def scan_finished(self, results):
        self.progress.stop()
        self.progress.pack_forget()
        self.progress.configure(mode="determinate", maximum=1, value=1)
        if hasattr(self, "scan_button") and self.scan_button.winfo_exists():
            self.scan_button.config(state=tk.NORMAL)
        self.apps = results
        save_scan_cache(self.folders, self.excludes, self.filters, results)
        self.status.config(text=f"筛选完成：保留 {len(results)} 个 GUI 应用")
        self.render_apps()

    def update_scan_progress(self, done, total):
        if total <= 0:
            self.progress.stop()
            self.progress.configure(mode="determinate", maximum=1, value=1)
            self.status.config(text="未找到 EXE 文件")
            return
        if str(self.progress.cget("mode")) == "indeterminate":
            self.progress.stop()
            self.progress.configure(mode="determinate", maximum=total, value=0)
        self.progress.configure(value=done)
        self.status.config(text=f"正在扫描：{done}/{total} 个 EXE，进行降噪筛选……")

    def render_apps(self):
        self.filtered_apps = [app for app in self.apps if not self._is_hidden(app.path)]
        self.filtered_apps.sort(key=lambda app: app.name.lower())
        self._render_android_apps()
        self.status.config(text=f"共 {len(self.filtered_apps)} 个应用")

    def _render_android_apps(self):
        folder_items = []
        for folder_name in sorted(self.app_folders, key=str.casefold):
            folder_apps = [app for app in self.filtered_apps
                            if app.path in self.app_folders[folder_name]]
            if folder_apps:
                folder_items.append(("folder", folder_name))

        assigned = {path for paths in self.app_folders.values() for path in paths}
        apps = [app for app in self.filtered_apps if app.path not in assigned]
        order = {path: index for index, path in enumerate(self.desktop_order)}
        apps.sort(key=lambda app: (order.get(app.path, len(order)), app.name.casefold()))
        self._android_items = folder_items + [("app", app) for app in apps]
        page_size = self.desktop_columns * self.desktop_rows
        page_count = max(1, (len(self._android_items) + page_size - 1) // page_size)
        self.android_page = min(self.android_page, page_count - 1)
        self._render_android_page()

    def _render_android_page(self):
        for child in self.card_frame.winfo_children():
            child.destroy()
        self.app_by_iid = {}
        self.selected_app_path = None

        page_size = self.desktop_columns * self.desktop_rows
        page_items = self._android_items[self.android_page * page_size:
                                         (self.android_page + 1) * page_size]

        for column in range(self.desktop_columns):
            self.card_frame.grid_columnconfigure(column, weight=1)

        for index, (kind, value) in enumerate(page_items):
            row, column = divmod(index, self.desktop_columns)
            if kind == "folder":
                self._create_android_folder(row, column, value)
            else:
                self._create_android_app(row, column, value)

        page_count = max(1, (len(self._android_items) + page_size - 1) // page_size)
        if not self._android_items:
            empty = tk.Frame(self.card_frame, bg=self.COLORS["bg"])
            empty.grid(row=0, column=0, columnspan=self.desktop_columns, pady=80)
            tk.Label(empty, text="暂无应用", bg=self.COLORS["bg"],
                     fg=self.COLORS["label"],
                     font=(self.font_family, 17, "bold")).pack()
            tk.Label(empty, text="前往「设置」添加扫描目录后扫描", bg=self.COLORS["bg"],
                     fg=self.COLORS["label_secondary"],
                     font=(self.font_family, 10)).pack(pady=(6, 16))
            self.scan_button = ttk.Button(empty, text="扫描应用", style="Accent.TButton",
                                          command=self.start_scan)
            self.scan_button.pack()
        self._update_page_indicator()
        self._render_android_dock()

    def _create_android_app(self, row, column, app):
        iid = f"android-app-{row}-{column}"
        self.app_by_iid[iid] = app
        icon_size = self._android_icon_size()
        card_w = icon_size + 36
        card_h = icon_size + 52
        radius = min(22, max(16, card_w // 5))  # Squircle 连续曲率圆角

        card = tk.Canvas(self.card_frame, width=card_w, height=card_h,
                         bg=self.COLORS["bg"], highlightthickness=0, bd=0)
        card.grid(row=row, column=column, padx=8, pady=8)

        if self.layout_mode == "free":
            x, y = self.desktop_positions.get(app.path, (column * 146, row * 194))
            card.grid_forget()
            card.place(x=x, y=y)

        # 圆角卡片背景（Squircle）
        self._set_card_background(card, card_w, card_h, radius)

        # 应用图标（含 iOS 风格阴影）
        icon_x = card_w // 2
        icon_y = icon_size // 2 + 12
        if ImageTk is not None and Image is not None:
            shadow = self._make_rounded_image(icon_size + 4, icon_size + 4,
                                              max(8, int(icon_size * 0.22)),
                                              (0, 0, 0, 90))
            if shadow:
                card._icon_shadow = ImageTk.PhotoImage(shadow)
                card.create_image(icon_x, icon_y + 2, image=card._icon_shadow)
        placeholder = self._placeholder_icon(app, icon_size)
        if ImageTk and placeholder:
            card._icon_photo = ImageTk.PhotoImage(placeholder)
            card.icon_id = card.create_image(icon_x, icon_y, image=card._icon_photo)
        else:
            card.icon_id = card.create_text(
                icon_x, icon_y, text=(app.name[:1] or "A").upper(),
                fill=self.COLORS["label"], font=(self.font_family, icon_size // 3, "bold"))

        # 应用名称
        name_y = icon_size + 30
        card.name_id = card.create_text(
            icon_x, name_y, text=app.name, fill=self.COLORS["label"],
            font=(self.font_family, 10), width=card_w - 18, justify="center")

        # 角标（iOS 红色药丸）
        badge_value = self.badges.get(app.path, "")
        if badge_value and badge_value not in ("0", "None"):
            bw, bh = 18, 16
            bx = card_w - bw // 2 - 4
            by = bh // 2 + 4
            if ImageTk is not None and Image is not None:
                card._badge_photo = ImageTk.PhotoImage(
                    self._make_rounded_image(bw, bh, 8, self.COLORS["red"]))
                card.create_image(bx, by, image=card._badge_photo)
            else:
                card.create_rectangle(bx - bw // 2, by - bh // 2, bx + bw // 2, by + bh // 2,
                                      fill=self.COLORS["red"], outline="")
            card.create_text(bx, by, text=str(badge_value), fill="white",
                             font=(self.font_family, 8, "bold"))

        self.android_app_cards.append((card, app.path, iid))

        def on_press(event, path=app.path, key=iid):
            self._card_press(card)
            self._android_press(event, path, key)

        def on_release(event):
            self._card_release(card)
            self._android_release(event)

        card.bind("<Button-1>", lambda _, key=iid: self.select_card(key))
        card.bind("<Double-1>", lambda _, key=iid: self.select_card(key, launch=True))
        card.bind("<Button-3>", lambda event, key=iid: self.show_app_menu(event, key))
        card.bind("<ButtonPress-1>", on_press)
        card.bind("<B1-Motion>", self._android_motion)
        card.bind("<ButtonRelease-1>", on_release)

        self.after(10, lambda: self._load_card_icon(app, card, icon_size))

    def _android_icon_size(self):
        available_width = self.card_canvas.winfo_width()
        if available_width <= 1:
            available_width = 850
        return max(44, min(88, (available_width // self.desktop_columns) - 38))

    def _create_android_folder(self, row, column, folder_name):
        iid = f"android-folder-{folder_name}"
        icon_size = self._android_icon_size()
        card_w = icon_size + 36
        card_h = icon_size + 52
        radius = min(22, max(16, card_w // 5))

        card = tk.Canvas(self.card_frame, width=card_w, height=card_h,
                         bg=self.COLORS["bg"], highlightthickness=0, bd=0)
        card.grid(row=row, column=column, padx=8, pady=8)

        self._set_card_background(card, card_w, card_h, radius)

        # 文件夹图标（半透明深色药丸 + 叠放效果）
        icon_x = card_w // 2
        icon_y = icon_size // 2 + 12
        folder_r = icon_size // 2
        fw = folder_r * 2
        if ImageTk is not None and Image is not None:
            card._folder_back = ImageTk.PhotoImage(
                self._make_rounded_image(fw, fw, max(6, folder_r // 3), self.COLORS["card_alt"]))
            card.create_image(icon_x, icon_y + 3, image=card._folder_back)
            card._folder_front = ImageTk.PhotoImage(
                self._make_rounded_image(fw - 6, fw - 6, max(6, folder_r // 3), self.COLORS["accent"]))
            card.create_image(icon_x, icon_y - 3, image=card._folder_front)
        else:
            card.create_rectangle(icon_x - folder_r, icon_y - folder_r + 4,
                                  icon_x + folder_r, icon_y + folder_r,
                                  fill=self.COLORS["card_alt"], outline="")
            card.create_rectangle(icon_x - folder_r + 3, icon_y - folder_r,
                                  icon_x + folder_r - 3, icon_y + folder_r - 4,
                                  fill=self.COLORS["accent"], outline="")

        name_y = icon_size + 30
        card.create_text(icon_x, name_y, text=folder_name, fill=self.COLORS["label"],
                         font=(self.font_family, 10), width=card_w - 18, justify="center")

        def on_press(_event):
            self._card_press(card)

        def on_release(_event):
            self._card_release(card)

        card.bind("<Double-1>", lambda _, value=folder_name: self.open_folder(value))
        card.bind("<ButtonRelease-1>", lambda _: self._drop_on_folder(folder_name))
        card.bind("<ButtonPress-1>", on_press)
        card.bind("<ButtonRelease-1>", on_release, add="+")

    def _render_android_dock(self):
        for child in self.dock_frame.winfo_children():
            child.destroy()
        available = {app.path: app for app in self.apps}
        dock_apps = [available[path] for path in self.dock_paths if path in available]
        if not dock_apps:
            dock_apps = self.filtered_apps[:5]
        icon_size = 40
        for app in dock_apps[:5]:
            tile = tk.Canvas(self.dock_frame, width=icon_size + 16, height=icon_size + 16,
                             bg=self.COLORS["dock"], highlightthickness=0, bd=0)
            tile.pack(side=tk.LEFT, padx=10, pady=10)
            placeholder = self._placeholder_icon(app, icon_size)
            if ImageTk and placeholder:
                tile._icon_photo = ImageTk.PhotoImage(placeholder)
                tile.create_image((icon_size + 16) // 2, (icon_size + 16) // 2,
                                  image=tile._icon_photo)
            tile.bind("<Button-1>", lambda _, path=app.path: self.launch_path(path))
            self.after(10, lambda app=app, tile=tile: self._load_dock_icon(app, tile, icon_size))

    def _load_dock_icon(self, app, tile, size):
        if not tile.winfo_exists():
            return
        image = get_icon_image(app, size)
        if image is None:
            return
        if ImageTk:
            tile._icon_photo = ImageTk.PhotoImage(self._shape_icon_image(image))
            tile.delete("all")
            tile.create_image((size + 16) // 2, (size + 16) // 2, image=tile._icon_photo)

    def _android_press(self, event, path, iid):
        if self.android_press_after is not None:
            self.after_cancel(self.android_press_after)
        self.select_card(iid)
        self.drag_app_path = path
        self.android_drag_path = path
        self.android_drag_iid = iid
        self.android_dragging = False
        self.android_drag_target = None
        card = self.app_by_iid.get(iid)
        if card is not None:
            self.android_drag_offset = (event.x_root - card.winfo_rootx(),
                                        event.y_root - card.winfo_rooty())
        self.android_press_after = self.after(
            800, lambda: self._start_android_drag(path, iid))

    def _start_android_drag(self, path, iid):
        self.android_press_after = None
        if self.android_drag_path != path:
            return
        self.android_dragging = True
        card = self.app_by_iid.get(iid)
        if card is not None:
            card_x, card_y = card.winfo_x(), card.winfo_y()
            card.grid_forget()
            card.place(x=card_x, y=card_y)

    def _android_motion(self, event):
        if not self.android_dragging:
            return
        card = self.app_by_iid.get(self.android_drag_iid)
        if card is not None:
            root_x, root_y = self.card_frame.winfo_rootx(), self.card_frame.winfo_rooty()
            x = max(0, event.x_root - root_x - self.android_drag_offset[0])
            y = max(0, event.y_root - root_y - self.android_drag_offset[1])
            card.place(x=x, y=y)
        target = self._android_target_path(event)
        if target == self.android_drag_path:
            target = None
        if target == self.android_drag_target:
            return
        self.android_drag_target = target

    def _android_release(self, event):
        if self.android_press_after is not None:
            self.after_cancel(self.android_press_after)
            self.android_press_after = None
        if self.android_dragging:
            target = self._android_target_path(event)
            source = self.android_drag_path
            source_iid = self.android_drag_iid
            self._reset_android_drag()
            if target and target != source:
                self._create_android_folder_from_apps(source, target)
            elif source and self.layout_mode == "free":
                card = self.app_by_iid.get(source_iid)
                if card is not None:
                    self.desktop_positions[source] = (card.winfo_x(), card.winfo_y())
                    self.save_user_config()
            elif source:
                self.render_apps()
        else:
            self._reset_android_drag()

    def _android_target_path(self, event):
        for card, path, _ in self.android_app_cards:
            left, top = card.winfo_rootx(), card.winfo_rooty()
            right = left + card.winfo_width()
            bottom = top + card.winfo_height()
            if left <= event.x_root <= right and top <= event.y_root <= bottom:
                return path
        return None

    def _reset_android_drag(self):
        self.android_dragging = False
        self.android_drag_path = None
        self.android_drag_iid = None
        self.android_drag_target = None
        self.drag_app_path = None

    def _create_android_folder_from_apps(self, source, target):
        folder_name = "新建文件夹"
        suffix = 2
        while folder_name in self.app_folders:
            folder_name = f"新建文件夹 {suffix}"
            suffix += 1
        self.remove_from_folders(source, refresh=False)
        self.remove_from_folders(target, refresh=False)
        self.app_folders[folder_name] = {source, target}
        self.save_user_config()
        self.render_apps()

    def _drop_on_folder(self, folder_name):
        if self.drag_app_path:
            path = self.drag_app_path
            self._reset_android_drag()
            self.move_to_folder(folder_name, path)

    def show_app_menu(self, event, iid):
        app = self.app_by_iid.get(iid)
        if app is None:
            return
        menu = tk.Menu(self, tearoff=False, bg=self.COLORS["card"], fg=self.COLORS["label"],
                       activebackground=self.COLORS["accent"], activeforeground="#FFFFFF",
                       borderwidth=0, font=(self.font_family, 10))
        if self.app_folders:
            for folder_name in sorted(self.app_folders, key=str.casefold):
                menu.add_command(label=f"移动到：{folder_name}",
                                 command=lambda name=folder_name, path=app.path: self.move_to_folder(name, path))
            menu.add_separator()
        if app.path in self.dock_paths:
            menu.add_command(label="从 Dock 移除",
                             command=lambda path=app.path: self.remove_from_dock(path))
        else:
            menu.add_command(label="固定到 Dock",
                             command=lambda path=app.path: self.pin_to_dock(path))
        menu.add_separator()
        menu.add_command(label="从所有文件夹移除", command=lambda path=app.path: self.remove_from_folders(path))
        menu.add_command(label="启动应用", command=lambda path=app.path: self.launch_path(path))
        menu.add_command(label="以管理员身份运行",
                         command=lambda path=app.path: self.launch_path(path, as_admin=True))
        menu.add_command(label="隐藏图标", command=lambda path=app.path: self.hide_app(path))
        menu.add_command(label="卸载/打开应用管理", command=self.open_uninstall_settings)
        menu.add_command(label="打开所在位置", command=lambda path=app.path: self.open_path_location(path))
        menu.tk_popup(event.x_root, event.y_root)

    def move_to_folder(self, folder_name, path):
        self.remove_from_folders(path, refresh=False)
        self.app_folders.setdefault(folder_name, set()).add(path)
        self.save_user_config()
        self.render_apps()

    def hide_app(self, path):
        self.hidden_paths.add(path)
        self.save_user_config()
        self.render_apps()

    @staticmethod
    def open_uninstall_settings():
        try:
            os.startfile("ms-settings:appsfeatures")
        except (OSError, AttributeError):
            subprocess.Popen(["control.exe", "/name", "Microsoft.ProgramsAndFeatures"])

    def pin_to_dock(self, path):
        if path not in self.dock_paths:
            self.dock_paths.append(path)
            self.dock_paths = self.dock_paths[-5:]
            self.save_user_config()
            self._render_android_dock()

    def remove_from_dock(self, path):
        if path in self.dock_paths:
            self.dock_paths.remove(path)
            self.save_user_config()
            self._render_android_dock()

    def remove_from_folders(self, path, refresh=True):
        for paths in self.app_folders.values():
            paths.discard(path)
        if refresh:
            self.save_user_config()
            self.render_apps()

    def open_folder(self, folder_name):
        dialog = tk.Toplevel(self)
        dialog.title(folder_name)
        dialog.geometry("620x500")
        dialog.transient(self)
        dialog.configure(bg=self.COLORS["bg"])
        title_row = ttk.Frame(dialog)
        title_row.pack(fill=tk.X, padx=12, pady=10)
        name_var = tk.StringVar(value=folder_name)
        ttk.Entry(title_row, textvariable=name_var, width=25).pack(side=tk.LEFT)
        search_var = tk.StringVar()
        ttk.Label(title_row, text="搜索：").pack(side=tk.RIGHT, padx=4)
        ttk.Entry(title_row, textvariable=search_var, width=25).pack(side=tk.RIGHT)

        canvas = tk.Canvas(dialog, highlightthickness=0, bg=self.COLORS["bg"])
        scrollbar = ttk.Scrollbar(dialog, orient=tk.VERTICAL, command=canvas.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(12, 0), pady=(0, 12))
        content = tk.Frame(canvas, bg=self.COLORS["bg"])
        canvas.create_window((0, 0), window=content, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        content.bind("<Configure>", lambda _: canvas.configure(scrollregion=canvas.bbox("all")))

        def render_folder(*_):
            for child in content.winfo_children():
                child.destroy()
            query = search_var.get().strip().casefold()
            apps = [app for app in self.apps
                    if app.path in self.app_folders.get(folder_name, set())
                    and (not query or query in app.name.casefold() or query in app.path.casefold())]
            icon_size = 58
            card_w = icon_size + 32
            card_h = icon_size + 46
            radius = min(20, max(14, card_w // 5))
            for index, app in enumerate(apps):
                row, column = divmod(index, 4)
                card = tk.Canvas(content, width=card_w, height=card_h,
                                 bg=self.COLORS["bg"], highlightthickness=0, bd=0)
                card.grid(row=row, column=column, padx=8, pady=8)
                self._set_card_background(card, card_w, card_h, radius)
                placeholder = self._placeholder_icon(app, icon_size)
                if ImageTk and placeholder:
                    card._icon_photo = ImageTk.PhotoImage(placeholder)
                    card.icon_id = card.create_image(card_w // 2, icon_size // 2 + 10,
                                                     image=card._icon_photo)
                else:
                    card.icon_id = card.create_text(card_w // 2, icon_size // 2 + 10,
                                                    text=(app.name[:1] or "A").upper(),
                                                    fill=self.COLORS["label"],
                                                    font=(self.font_family, 16, "bold"))
                card.create_text(card_w // 2, icon_size + 26, text=app.name,
                                 fill=self.COLORS["label"], font=(self.font_family, 9),
                                 width=card_w - 16, justify="center")
                card.bind("<Double-1>", lambda _, path=app.path: self.launch_path(path))
                card.bind("<Button-3>", lambda event, path=app.path:
                          self.remove_from_folder_from_dialog(folder_name, path, dialog))
                card.bind("<ButtonPress-1>", lambda _e, c=card: self._card_press(c))
                card.bind("<ButtonRelease-1>", lambda _e, c=card: self._card_release(c), add="+")
                self.after(10, lambda app=app, card=card: self._load_card_icon(app, card, icon_size))
            content.update_idletasks()
            canvas.configure(scrollregion=canvas.bbox("all"))

        search_var.trace_add("write", render_folder)
        render_folder()

        def rename_folder():
            new_name = name_var.get().strip()
            if not new_name or new_name == folder_name:
                return
            if new_name in self.app_folders:
                messagebox.showerror("文件夹名称", "该文件夹名称已存在", parent=dialog)
                return
            self.app_folders[new_name] = self.app_folders.pop(folder_name)
            self.save_user_config()
            dialog.title(new_name)
            self.render_apps()

        ttk.Button(dialog, text="保存名称", style="Accent.TButton",
                   command=rename_folder).pack(pady=(0, 10))

    def remove_from_folder_from_dialog(self, folder_name, path, dialog):
        self.app_folders.get(folder_name, set()).discard(path)
        self.save_user_config()
        dialog.destroy()
        self.render_apps()

    def _placeholder_icon(self, app, size):
        if Image is None:
            return None
        # iOS 风格蓝色渐变占位图标
        image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        if ImageDraw:
            draw = ImageDraw.Draw(image)
            for y in range(size):
                t = y / max(1, size - 1)
                r = int(0 + (0 - 0) * t)
                g = int(122 + (130 - 122) * t)
                b = int(255 + (255 - 255) * t)
                draw.line([(0, y), (size, y)], fill=(r, g, b, 255))
            letter = (app.name.strip() or "A")[0].upper()
            draw.text((size // 2, size // 2), letter, fill="white", anchor="mm",
                      font=self._font_for_size(size // 2))
        return self._shape_icon_image(image)

    @staticmethod
    def _font_for_size(size):
        try:
            from PIL import ImageFont
            return ImageFont.truetype("arial.ttf", size)
        except Exception:
            return ImageFont.load_default()

    def _shape_icon_image(self, image):
        """将图标处理为 iOS 规范：正方形适配 + Squircle 圆角 + 顶部高光。"""
        if Image is None:
            return image
        img = image.convert("RGBA")
        side = max(img.width, img.height)
        if img.width != img.height:
            squared = Image.new("RGBA", (side, side), (0, 0, 0, 0))
            squared.paste(img, ((side - img.width) // 2, (side - img.height) // 2), img)
            img = squared
        if img.width != side:
            img = img.resize((side, side), Image.LANCZOS)

        # iOS Squircle 圆角遮罩
        mask = Image.new("L", (side, side), 0)
        draw = ImageDraw.Draw(mask)
        radius = max(10, int(side * 0.22))
        draw.rounded_rectangle((0, 0, side - 1, side - 1), radius=radius, fill=255)
        img.putalpha(mask)

        # 顶部高光（iOS 图标质感）
        try:
            sheen = Image.new("RGBA", (side, side), (0, 0, 0, 0))
            sheen_draw = ImageDraw.Draw(sheen)
            for y in range(side // 2):
                alpha = int(36 * (1 - y / (side // 2)))
                sheen_draw.line([(0, y), (side, y)], fill=(255, 255, 255, alpha))
            sheen.putalpha(mask)
            img = Image.alpha_composite(img, sheen)
        except Exception:
            pass
        return img

    def _load_card_icon(self, app, card, size=96):
        if not card.winfo_exists():
            return
        image = get_icon_image(app, size)
        if image is None:
            return
        if ImageTk:
            shaped = self._shape_icon_image(image)
            card._icon_photo = ImageTk.PhotoImage(shaped)
            card.itemconfigure(card.icon_id, image=card._icon_photo)

    def select_card(self, iid, launch=False):
        app = self.app_by_iid.get(iid)
        if app is None:
            return
        self.selected_app_path = app.path
        if launch:
            self.launch_selected()

    def _resize_card_frame(self, event):
        self.card_canvas.itemconfigure(self.card_window, width=max(event.width, 850))
        if (hasattr(self, "_android_items")
                and abs(event.width - self.android_last_width) > 20):
            self.android_last_width = event.width
            self.after_idle(self._render_android_page)

    @staticmethod
    def _matches_search(app, query):
        return not query or query in app.name.lower() or query in app.path.lower()

    def _is_hidden(self, path):
        normalized = os.path.normcase(os.path.abspath(path))
        return any(normalized == os.path.normcase(os.path.abspath(item))
                   for item in self.hidden_paths)

    def open_settings(self):
        dialog = tk.Toplevel(self)
        dialog.title("应用资源库设置")
        dialog.geometry("720x700")
        dialog.transient(self)
        dialog.grab_set()
        dialog.configure(bg=self.COLORS["bg"])

        directories = list(self.folders)
        folder_map = {name: set(paths) for name, paths in self.app_folders.items()}

        listbox_opts = dict(bg=self.COLORS["card"], fg=self.COLORS["label"],
                            selectbackground=self.COLORS["accent"],
                            selectforeground="#FFFFFF", highlightthickness=0,
                            borderwidth=0, activestyle="none",
                            font=(self.font_family, 10))

        ttk.Label(dialog, text="扫描目录").pack(anchor=tk.W, padx=14, pady=(12, 2))
        directory_list = tk.Listbox(dialog, height=5, **listbox_opts)
        for path in directories:
            directory_list.insert(tk.END, path)
        directory_list.pack(fill=tk.X, padx=14)

        directory_buttons = ttk.Frame(dialog)
        directory_buttons.pack(fill=tk.X, padx=14, pady=4)

        def add_directory():
            path = filedialog.askdirectory(parent=dialog, title="选择扫描目录")
            if path and path not in directories:
                directories.append(path)
                directory_list.insert(tk.END, path)

        def remove_directory():
            for index in reversed(directory_list.curselection()):
                del directories[index]
            directory_list.delete(0, tk.END)
            for path in directories:
                directory_list.insert(tk.END, path)

        ttk.Button(directory_buttons, text="添加目录", command=add_directory).pack(side=tk.LEFT)
        ttk.Button(directory_buttons, text="删除选中", command=remove_directory).pack(side=tk.LEFT, padx=6)

        ttk.Label(dialog, text="应用文件夹（右键应用卡片可移动到文件夹）").pack(anchor=tk.W, padx=14, pady=(8, 2))
        folder_list = tk.Listbox(dialog, height=5, **listbox_opts)
        folder_list.pack(fill=tk.X, padx=14)

        def refresh_folders():
            folder_list.delete(0, tk.END)
            for name in sorted(folder_map, key=str.casefold):
                folder_list.insert(tk.END, name)

        refresh_folders()
        folder_row = ttk.Frame(dialog)
        folder_row.pack(fill=tk.X, padx=14, pady=4)
        folder_name = tk.StringVar()
        ttk.Entry(folder_row, textvariable=folder_name, width=25).pack(side=tk.LEFT)

        def add_app_folder():
            name = folder_name.get().strip()
            if name and name not in folder_map:
                folder_map[name] = set()
                folder_name.set("")
                refresh_folders()

        def remove_app_folder():
            for index in reversed(folder_list.curselection()):
                name = folder_list.get(index)
                folder_map.pop(name, None)
            refresh_folders()

        ttk.Button(folder_row, text="新建文件夹", command=add_app_folder).pack(side=tk.LEFT, padx=6)
        ttk.Button(folder_row, text="删除文件夹", command=remove_app_folder).pack(side=tk.LEFT)

        filter_row = ttk.Frame(dialog)
        filter_row.pack(fill=tk.X, padx=14, pady=(8, 2))
        ttk.Label(filter_row, text="最小 EXE 体积 KB：").pack(side=tk.LEFT)
        min_size = tk.StringVar(value=str(self.filters["min_size"] // 1024))
        ttk.Entry(filter_row, textvariable=min_size, width=8).pack(side=tk.LEFT, padx=6)
        ttk.Label(dialog, text="额外黑名单（每行一个关键词）").pack(anchor=tk.W, padx=14, pady=(4, 2))
        denylist = tk.Text(dialog, height=4, bg=self.COLORS["card"], fg=self.COLORS["label"],
                           insertbackground=self.COLORS["label"], highlightthickness=0,
                           borderwidth=0, font=(self.font_family, 10), wrap="word")
        denylist.pack(fill=tk.X, padx=14)
        denylist.insert("1.0", "\n".join(self.filters["denylist"]))

        desktop_row = ttk.Frame(dialog)
        desktop_row.pack(fill=tk.X, padx=14, pady=8)
        ttk.Label(desktop_row, text="桌面网格：").pack(side=tk.LEFT)
        columns_var = tk.StringVar(value=str(self.desktop_columns))
        rows_var = tk.StringVar(value=str(self.desktop_rows))
        ttk.Combobox(desktop_row, textvariable=columns_var, state="readonly",
                     values=tuple(str(value) for value in range(4, 9)), width=4).pack(side=tk.LEFT, padx=4)
        ttk.Label(desktop_row, text="列 ×").pack(side=tk.LEFT)
        ttk.Combobox(desktop_row, textvariable=rows_var, state="readonly",
                     values=tuple(str(value) for value in range(3, 9)), width=4).pack(side=tk.LEFT, padx=4)
        ttk.Label(desktop_row, text="行").pack(side=tk.LEFT)
        ttk.Label(desktop_row, text="布局：").pack(side=tk.LEFT, padx=(18, 0))
        layout_var = tk.StringVar(value=self.layout_mode)
        ttk.Combobox(desktop_row, textvariable=layout_var, state="readonly",
                     values=("grid", "free"), width=8).pack(side=tk.LEFT, padx=4)
        ttk.Label(desktop_row, text="图标：").pack(side=tk.LEFT, padx=(12, 0))
        shape_var = tk.StringVar(value=self.icon_shape)
        ttk.Combobox(desktop_row, textvariable=shape_var, state="readonly",
                     values=("square", "rounded", "circle"), width=9).pack(side=tk.LEFT, padx=4)

        def save_settings(scan=False):
            try:
                size_kb = max(0, int(min_size.get().strip() or "0"))
            except ValueError:
                messagebox.showerror("设置错误", "最小体积必须是整数 KB", parent=dialog)
                return
            self.folders = directories
            self.filters["min_size"] = size_kb * 1024
            self.filters["denylist"] = [line.strip() for line in denylist.get("1.0", tk.END).splitlines() if line.strip()]
            self.app_folders = folder_map
            self.desktop_columns = self._read_grid_value(columns_var.get(), 4, 8, 5)
            self.desktop_rows = self._read_grid_value(rows_var.get(), 3, 8, 4)
            self.layout_mode = layout_var.get() if layout_var.get() in ("grid", "free") else "grid"
            self.icon_shape = shape_var.get() if shape_var.get() in ("square", "rounded", "circle") else "square"
            self.save_user_config()
            self.refresh_folder_list()
            dialog.destroy()
            self.render_apps()
            if scan:
                self.start_scan()

        actions = ttk.Frame(dialog)
        actions.pack(fill=tk.X, padx=14, pady=10)
        ttk.Button(actions, text="取消", command=dialog.destroy).pack(side=tk.RIGHT)
        ttk.Button(actions, text="保存", style="Accent.TButton",
                   command=lambda: save_settings(False)).pack(side=tk.RIGHT, padx=6)
        ttk.Button(actions, text="保存并扫描",
                   command=lambda: save_settings(True)).pack(side=tk.RIGHT)

    def selected_app(self):
        if not self.selected_app_path:
            messagebox.showinfo("提示", "请先选择一个应用")
            return None
        return next((app for app in self.filtered_apps if app.path == self.selected_app_path), None)

    def launch_selected(self):
        app = self.selected_app()
        if app:
            self.launch_path(app.path)

    def launch_path(self, path, as_admin=False):
        try:
            if as_admin and os.name == "nt":
                result = ctypes.windll.shell32.ShellExecuteW(
                    None, "runas", path, None, os.path.dirname(path), 1)
                if result <= 32:
                    raise OSError(f"管理员启动失败，错误码：{result}")
                return
            startupinfo = None
            if os.name == "nt":
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = 0
            subprocess.Popen([path], startupinfo=startupinfo,
                             creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
                             close_fds=os.name != "nt")
        except (OSError, AttributeError, subprocess.SubprocessError) as error:
            messagebox.showerror("启动失败", str(error))

    def open_location(self):
        app = self.selected_app()
        if app:
            self.open_path_location(app.path)

    @staticmethod
    def open_path_location(path):
        subprocess.Popen(["explorer", "/select,", path])


if __name__ == "__main__":
    ApplicationManager().mainloop()
