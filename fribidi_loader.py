"""Make Bangla text shaping work in the packaged app.

Pillow shapes complex scripts (Bangla) with Raqm, and Raqm needs the FriBiDi
library, which Pillow's Windows and Mac wheels do not include. The build puts
FriBiDi in the app's "libs" folder; this module loads it before Pillow's font
module starts, so Pillow finds it. Import this before PIL.ImageFont.
"""
import ctypes
import os
import sys

LOADED = None


def _libdir() -> str:
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "libs")


def load():
    global LOADED
    d = _libdir()
    if LOADED or not os.path.isdir(d):
        return LOADED
    try:
        if sys.platform.startswith("win"):
            for name in ("fribidi-0.dll", "libfribidi-0.dll"):
                p = os.path.join(d, name)
                if os.path.exists(p):
                    os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
                    try:
                        os.add_dll_directory(d)
                    except Exception:
                        pass
                    ctypes.WinDLL(p)       # Pillow's LoadLibrary("fribidi-0") then finds this one
                    LOADED = p
                    break
        elif sys.platform == "darwin":
            p = os.path.join(d, "libfribidi.dylib")
            if os.path.exists(p):
                ctypes.CDLL(p, mode=ctypes.RTLD_GLOBAL)
                old = os.getcwd()
                try:   # Pillow asks for "libfribidi.dylib" by name; the current folder is searched
                    os.chdir(d)
                    import PIL._imagingft  # noqa: F401
                finally:
                    os.chdir(old)
                LOADED = p
    except Exception:
        pass
    return LOADED


load()
