"""Windows side of the app keys: list Start-menu apps, render their icons, launch them.

Apps come from PowerShell's Get-StartApps (classic desktop apps AND Store apps).
Each app is addressed by its AppUserModelID; it is launched and its icon is read
through the shell's AppsFolder, so both kinds work the same way.
"""
import ctypes
import io
import json
import subprocess
import threading
import time
from ctypes import wintypes

from PIL import Image

_lock = threading.Lock()
_apps = []          # [{"name", "path"}]; "path" holds the AppUserModelID (the page calls it path)
_apps_at = 0.0
_icons = {}

CREATE_NO_WINDOW = 0x08000000


def list_apps():
    """Start-menu apps, sorted by name. Cached for 5 minutes (Get-StartApps takes ~1 s)."""
    global _apps, _apps_at
    with _lock:
        if _apps and time.time() - _apps_at < 300:
            return _apps
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "[Console]::OutputEncoding=[Text.Encoding]::UTF8; Get-StartApps | Select-Object Name,AppID | ConvertTo-Json -Compress"],
            capture_output=True, timeout=20, creationflags=CREATE_NO_WINDOW).stdout.decode("utf-8", "replace")
        rows = json.loads(out or "[]")
        if isinstance(rows, dict):
            rows = [rows]
    except Exception:
        rows = []
    seen, apps = set(), []
    for r in rows:
        name, aid = (r.get("Name") or "").strip(), (r.get("AppID") or "").strip()
        # Skip uninstallers, docs and web links: they clutter a 7-key picker.
        low = name.lower()
        if not name or not aid or aid.lower().startswith("http") or "uninstall" in low or "kaldır" in low:
            continue
        if name in seen:
            continue
        seen.add(name)
        apps.append({"name": name, "path": aid})
    apps.sort(key=lambda a: a["name"].lower())
    with _lock:
        if apps:
            _apps, _apps_at = apps, time.time()
        return _apps


def is_known(aid):
    return any(a["path"] == aid for a in list_apps())


def launch(aid):
    subprocess.Popen(["explorer.exe", "shell:AppsFolder\\" + aid], creationflags=CREATE_NO_WINDOW)


# ---------- icons via IShellItemImageFactory (raw COM through ctypes) ----------

class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]


def _guid(s):
    g = GUID()
    ctypes.oledll.ole32.CLSIDFromString(ctypes.c_wchar_p(s), ctypes.byref(g))
    return g


class SIZE(ctypes.Structure):
    _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long), ("biYPelsPerMeter", ctypes.c_long),
                ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


class BITMAP(ctypes.Structure):
    _fields_ = [("bmType", ctypes.c_long), ("bmWidth", ctypes.c_long), ("bmHeight", ctypes.c_long),
                ("bmWidthBytes", ctypes.c_long), ("bmPlanes", wintypes.WORD), ("bmBitsPixel", wintypes.WORD),
                ("bmBits", ctypes.c_void_p)]


IID_IShellItemImageFactory = _guid("{bcc18b79-ba16-442f-80c4-8a59c30c463b}")
SIIGBF_BIGGERSIZEOK, SIIGBF_ICONONLY = 0x1, 0x4
_shell32, _gdi32, _ole32 = ctypes.windll.shell32, ctypes.windll.gdi32, ctypes.windll.ole32
_shell32.SHCreateItemFromParsingName.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
_gdi32.GetObjectW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
_gdi32.GetDIBits.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint]
_gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
_tls = threading.local()


def _com_init():
    if not getattr(_tls, "com", False):
        _ole32.CoInitializeEx(None, 0x2)   # apartment-threaded, once per server thread
        _tls.com = True


def _hbitmap_png(hbm, size):
    bm = BITMAP()
    _gdi32.GetObjectW(hbm, ctypes.sizeof(bm), ctypes.byref(bm))
    w, h = bm.bmWidth, bm.bmHeight
    bih = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)  # negative = top-down
    buf = ctypes.create_string_buffer(w * h * 4)
    hdc = ctypes.windll.user32.GetDC(None)
    _gdi32.GetDIBits(hdc, hbm, 0, h, buf, ctypes.byref(bih), 0)
    ctypes.windll.user32.ReleaseDC(None, hdc)
    img = Image.frombuffer("RGBA", (w, h), buf.raw, "raw", "BGRA", 0, 1)
    if img.getextrema()[3] == (0, 0):          # some icons come back with no alpha channel at all
        img = img.convert("RGB").convert("RGBA")
    img = img.resize((size, size), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, "PNG")
    return out.getvalue()


def icon(aid, size=114):
    with _lock:
        if aid in _icons:
            return _icons[aid]
    _com_init()
    ptr = ctypes.c_void_p()
    hr = _shell32.SHCreateItemFromParsingName("shell:AppsFolder\\" + aid, None, ctypes.byref(IID_IShellItemImageFactory), ctypes.byref(ptr))
    if hr != 0 or not ptr:
        return None
    vtbl = ctypes.cast(ctypes.cast(ptr, ctypes.POINTER(ctypes.c_void_p))[0], ctypes.POINTER(ctypes.c_void_p))
    get_image = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, SIZE, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))(vtbl[3])
    release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtbl[2])
    hbm = ctypes.c_void_p()
    try:
        hr = get_image(ptr, SIZE(size, size), SIIGBF_BIGGERSIZEOK | SIIGBF_ICONONLY, ctypes.byref(hbm))
        if hr != 0 or not hbm:
            return None
        png = _hbitmap_png(hbm, size)
    finally:
        if hbm:
            _gdi32.DeleteObject(hbm)
        release(ptr)
    with _lock:
        _icons[aid] = png
    return png
