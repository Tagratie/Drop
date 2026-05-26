"""Windows-specific helpers: borderless window chrome, alt-tab fix,
DWM rounded corners, work-area lookup, modern file pickers."""
import sys
import ctypes
from pathlib import Path

from .theme import NO_WINDOW


def _hwnd_of(root):
    """Return the top-level HWND of a Tk root after override-redirect."""
    if sys.platform != "win32":
        return 0
    try:
        root.update_idletasks()
        # With overrideredirect, the toplevel HWND is the Tk window itself
        # (no parent frame). Without it, we need GetParent().
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        return hwnd or root.winfo_id()
    except Exception:
        return 0




def round_window_corners(root):
    """DWM-rounded corners on Win11. No-op on Win10 / non-Windows."""
    if sys.platform != "win32":
        return
    try:
        hwnd = _hwnd_of(root)
        if not hwnd: return
        DWMWA_WINDOW_CORNER_PREFERENCE = 33
        DWMWCP_ROUND = 2
        v = ctypes.c_int(DWMWCP_ROUND)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, DWMWA_WINDOW_CORNER_PREFERENCE,
            ctypes.byref(v), ctypes.sizeof(v),
        )
    except Exception:
        pass




def set_window_region_rounded(root, radius=12):
    """Force-clip the window to a rounded rectangle via SetWindowRgn.
    Use as a fallback when DWM rounding can't apply (Win10 borderless)."""
    if sys.platform != "win32":
        return
    try:
        hwnd = _hwnd_of(root)
        if not hwnd: return
        w = root.winfo_width()
        h = root.winfo_height()
        if w < 4 or h < 4: return
        # CreateRoundRectRgn(left, top, right, bottom, ellipseW, ellipseH)
        rgn = ctypes.windll.gdi32.CreateRoundRectRgn(
            0, 0, w + 1, h + 1, radius * 2, radius * 2
        )
        # SetWindowRgn(hwnd, hrgn, bRedraw=TRUE) — Windows takes ownership of rgn
        ctypes.windll.user32.SetWindowRgn(hwnd, rgn, True)
    except Exception:
        pass




def clear_window_region(root):
    """Remove any region we set, returning the window to a normal rectangle."""
    if sys.platform != "win32":
        return
    try:
        hwnd = _hwnd_of(root)
        if not hwnd: return
        ctypes.windll.user32.SetWindowRgn(hwnd, 0, True)
    except Exception:
        pass




def fix_borderless_alt_tab(root):
    """Make a borderless (overrideredirect=True) Tk window appear in Alt-Tab
    and on the taskbar. Standard incantation: set WS_EX_APPWINDOW, clear
    WS_EX_TOOLWINDOW, then re-show the window so the shell registers it."""
    if sys.platform != "win32":
        return
    try:
        hwnd = _hwnd_of(root)
        if not hwnd: return
        GWL_EXSTYLE      = -20
        WS_EX_APPWINDOW  = 0x00040000
        WS_EX_TOOLWINDOW = 0x00000080
        # Use ...PtrW for 64-bit safety
        get_long = (ctypes.windll.user32.GetWindowLongPtrW
                    if hasattr(ctypes.windll.user32, "GetWindowLongPtrW")
                    else ctypes.windll.user32.GetWindowLongW)
        set_long = (ctypes.windll.user32.SetWindowLongPtrW
                    if hasattr(ctypes.windll.user32, "SetWindowLongPtrW")
                    else ctypes.windll.user32.SetWindowLongW)
        style = get_long(hwnd, GWL_EXSTYLE)
        style |= WS_EX_APPWINDOW
        style &= ~WS_EX_TOOLWINDOW
        set_long(hwnd, GWL_EXSTYLE, style)
        # Bounce visibility so the taskbar registers the new style
        root.withdraw()
        root.after(10, root.deiconify)
    except Exception:
        pass




def set_app_user_model_id(app_id: str) -> None:
    """Give the process a stable AppUserModelID so Windows groups our taskbar
    button under our own icon instead of pythonw/explorer's. No-op off Win."""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
    except Exception:
        pass


def set_taskbar_icon(root, ico_path: str) -> None:
    """Force the taskbar/alt-tab icon to use the right-sized frames from a
    multi-resolution .ico. Tk's iconbitmap() on Windows only wires the small
    icon — the taskbar's ICON_BIG falls back to the EXE resource (or, for a
    borderless WS_EX_APPWINDOW Tk window, an upscaled small icon). We bypass
    Tk and send WM_SETICON ourselves so Windows picks the 32 frame for
    ICON_SMALL and the 256 frame for ICON_BIG."""
    if sys.platform != "win32" or not ico_path:
        return
    try:
        hwnd = _hwnd_of(root)
        if not hwnd:
            return

        user32 = ctypes.windll.user32
        user32.LoadImageW.restype  = ctypes.c_void_p
        user32.LoadImageW.argtypes = [
            ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint,
            ctypes.c_int,    ctypes.c_int,    ctypes.c_uint,
        ]
        user32.SendMessageW.restype  = ctypes.c_void_p
        user32.SendMessageW.argtypes = [
            ctypes.c_void_p, ctypes.c_uint,
            ctypes.c_void_p, ctypes.c_void_p,
        ]
        user32.GetSystemMetrics.restype  = ctypes.c_int
        user32.GetSystemMetrics.argtypes = [ctypes.c_int]

        IMAGE_ICON        = 1
        LR_LOADFROMFILE   = 0x00000010
        LR_DEFAULTCOLOR   = 0x00000000
        WM_SETICON        = 0x0080
        ICON_SMALL, ICON_BIG = 0, 1
        SM_CXICON, SM_CXSMICON = 11, 49

        # Use system-metric sizes so DPI scaling picks larger frames automatically.
        big_sz   = user32.GetSystemMetrics(SM_CXICON)    # typically 32, 48 @ high DPI
        small_sz = user32.GetSystemMetrics(SM_CXSMICON)  # typically 16, 24 @ high DPI

        hicon_big = user32.LoadImageW(
            None, ico_path, IMAGE_ICON, big_sz, big_sz,
            LR_LOADFROMFILE | LR_DEFAULTCOLOR,
        )
        hicon_sm = user32.LoadImageW(
            None, ico_path, IMAGE_ICON, small_sz, small_sz,
            LR_LOADFROMFILE | LR_DEFAULTCOLOR,
        )
        if hicon_big:
            user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG,   hicon_big)
        if hicon_sm:
            user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, hicon_sm)
    except Exception:
        pass


def minimize_window(root):
    """Minimize a borderless Tk window. tkinter's iconify() is unreliable
    when overrideredirect=True is set — on Windows it either no-ops or
    leaves the window in a weird half-state. Bypass Tk and use the Win32
    API directly. The taskbar entry (set up by fix_borderless_alt_tab via
    WS_EX_APPWINDOW) restores the window when clicked."""
    if sys.platform == "win32":
        try:
            hwnd = _hwnd_of(root)
            if hwnd:
                SW_MINIMIZE = 6
                ctypes.windll.user32.ShowWindow(hwnd, SW_MINIMIZE)
                return
        except Exception:
            pass
    # Generic fallback for Linux / macOS where overrideredirect doesn't
    # break iconify the same way.
    try:
        root.iconify()
    except Exception:
        pass




def get_work_area():
    """Return (left, top, right, bottom) of the primary monitor's work area
    (screen minus taskbar). Falls back to full screen size."""
    if sys.platform == "win32":
        try:
            class RECT(ctypes.Structure):
                _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                            ("right", ctypes.c_long), ("bottom", ctypes.c_long)]
            r = RECT()
            SPI_GETWORKAREA = 0x0030
            ctypes.windll.user32.SystemParametersInfoW(
                SPI_GETWORKAREA, 0, ctypes.byref(r), 0
            )
            return (r.left, r.top, r.right, r.bottom)
        except Exception:
            pass
    return (0, 0, 1920, 1080)




def pick_files_modern(parent=None, title="Select files",
                      filetypes=None, multi=True):
    from tkinter import filedialog
    kw = {"title": title, "parent": parent} if parent else {"title": title}
    if filetypes:
        kw["filetypes"] = filetypes
    if multi:
        res = filedialog.askopenfilenames(**kw)
        return list(res) if res else []
    res = filedialog.askopenfilename(**kw)
    return [res] if res else []




def pick_folder_modern(parent=None, title="Select folder"):
    from tkinter import filedialog
    kw = {"title": title, "parent": parent} if parent else {"title": title}
    d = filedialog.askdirectory(**kw)
    return d or None
