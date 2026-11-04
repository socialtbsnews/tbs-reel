"""Fonts for TBS Reel.

Three places fonts come from:
  * built in   - free fonts shipped with the app (fonts/ folder),
  * computer   - fonts installed on this PC or Mac (e.g. BiblioMono, FF Meta Serif Pro),
  * added      - font files picked in the app with "Add fonts…" (copied to ~/.tbs_reel_fonts).
Fonts are grouped into families; each family has a regular and a bold file.
A family with Bangla letters appears in the Bangla menu, one with English letters in the English menu.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import asdict, dataclass

USER_FONT_DIR = os.path.join(os.path.expanduser("~"), ".tbs_reel_fonts")
CACHE = os.path.join(os.path.expanduser("~"), ".tbs_reel_fontcache.json")
FONT_EXT = (".ttf", ".otf", ".ttc")
PREFERRED_BANGLA = ["BiblioMono", "Noto Sans Bengali"]
PREFERRED_ENGLISH = ["FF Meta Serif Pro", "FF Meta Serif", "Noto Serif"]
PREFERRED_TOP = ["November Condensed LCG", "November Condensed", "Roboto Condensed"]   # date and credit


@dataclass
class Face:
    path: str
    family: str
    weight: int
    italic: bool
    bangla: bool
    latin: bool


@dataclass
class Family:
    name: str
    regular: str
    bold: str
    fake_bold: bool      # no real bold file: thicken the regular one
    bangla: bool
    latin: bool
    source: str
    italic: str = ""     # italic file ("" = none: slant the regular one)
    bold_italic: str = ""


def _base() -> str:
    return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))


def system_font_dirs() -> list[str]:
    home = os.path.expanduser("~")
    if sys.platform.startswith("win"):
        return [os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"),
                os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "Windows", "Fonts")]
    if sys.platform == "darwin":
        return ["/Library/Fonts", os.path.join(home, "Library", "Fonts"), "/System/Library/Fonts/Supplemental"]
    return ["/usr/share/fonts", "/usr/local/share/fonts", os.path.join(home, ".fonts"),
            os.path.join(home, ".local", "share", "fonts")]


def read_face(path: str) -> Face | None:
    """Name, weight and script coverage of one font file."""
    try:
        from fontTools.ttLib import TTFont
        f = TTFont(path, lazy=True, fontNumber=0)
        name = f["name"]
        fam = name.getDebugName(16) or name.getDebugName(1) or os.path.splitext(os.path.basename(path))[0]
        sub = (name.getDebugName(17) or name.getDebugName(2) or "").lower()
        os2 = f["OS/2"] if "OS/2" in f else None
        weight = os2.usWeightClass if os2 is not None else (700 if "bold" in sub else 400)
        if weight < 100:              # a few old fonts store 1-9
            weight *= 100
        italic = bool(os2 and os2.fsSelection & 1) or "italic" in sub or "oblique" in sub
        cmap = f.getBestCmap() or {}
        bangla = sum(1 for k in (0x995, 0x996, 0x9BE, 0x9BF, 0x9CD, 0x985) if k in cmap) >= 5
        latin = all(ord(c) in cmap for c in "AZaz09")
        f.close()
        return Face(path, fam.strip(), int(weight), italic, bangla, latin)
    except Exception:
        return None


def _scan_dir(folder: str, cache: dict, recursive: bool = True) -> list[Face]:
    faces = []
    if not folder or not os.path.isdir(folder):
        return faces
    walker = os.walk(folder) if recursive else [(folder, [], os.listdir(folder))]
    for root, _dirs, files in walker:
        for fn in files:
            if not fn.lower().endswith(FONT_EXT):
                continue
            p = os.path.join(root, fn)
            try:
                key = f"{p}|{os.path.getmtime(p):.0f}"
            except OSError:
                continue
            if key in cache:
                d = cache[key]
                faces.append(Face(**d) if d else None)
            else:
                face = read_face(p)
                cache[key] = asdict(face) if face else None
                faces.append(face)
    return [f for f in faces if f]


def _families(faces: list[Face], source: str) -> dict[str, Family]:
    groups: dict[str, list[Face]] = {}
    for f in faces:
        if f.bangla or f.latin:
            name = f.family
            if f.italic:              # "Noto Serif Italic" belongs to "Noto Serif"
                for suffix in (" Italic", " Oblique", " It"):
                    if name.endswith(suffix):
                        name = name[: -len(suffix)]
            groups.setdefault(name, []).append(f)
    out = {}
    for name, fs in groups.items():
        upright = [f for f in fs if not f.italic] or fs
        regular = min(upright, key=lambda f: (abs(f.weight - 400), f.weight))
        heavier = [f for f in upright if f.weight >= regular.weight + 150]
        bold = min(heavier, key=lambda f: abs(f.weight - 700)) if heavier else regular
        slanted = [f for f in fs if f.italic]
        it = min(slanted, key=lambda f: abs(f.weight - regular.weight)) if slanted else None
        bit = min(slanted, key=lambda f: abs(f.weight - bold.weight)) if slanted else None
        if bit is not None and bold is not regular and bit.weight < regular.weight + 150:
            bit = None
        out[name] = Family(name, regular.path, bold.path, bold is regular,
                           any(f.bangla for f in fs), any(f.latin for f in fs), source,
                           it.path if it else "", bit.path if bit else "")
    return out


def built_in() -> dict[str, Family]:
    d = os.path.join(_base(), "fonts")
    return _families(_scan_dir(d, {}, recursive=False), "built in")


def scan_all(include_system: bool = True) -> dict[str, Family]:
    """All families. Later sources win: computer fonts over built-in, added over both."""
    try:
        with open(CACHE, encoding="utf-8") as fh:
            cache = json.load(fh)
    except Exception:
        cache = {}
    fams = built_in()
    if include_system:
        faces = []
        for d in system_font_dirs():
            faces += _scan_dir(d, cache)
        fams.update(_families(faces, "computer"))
    fams.update(_families(_scan_dir(USER_FONT_DIR, cache), "added"))
    try:
        with open(CACHE, "w", encoding="utf-8") as fh:
            json.dump(cache, fh)
    except Exception:
        pass
    return fams


def add_font_files(paths: list[str]) -> list[str]:
    """Copy font files into the app's own font folder, so they stay available."""
    os.makedirs(USER_FONT_DIR, exist_ok=True)
    added = []
    for p in paths:
        if p.lower().endswith(FONT_EXT) and read_face(p):
            dst = os.path.join(USER_FONT_DIR, os.path.basename(p))
            if os.path.abspath(p) != os.path.abspath(dst):
                shutil.copy2(p, dst)
            added.append(dst)
    return added


def pick_default(fams: dict[str, Family], bangla: bool, prefs: list[str] | None = None) -> str:
    prefs = prefs or (PREFERRED_BANGLA if bangla else PREFERRED_ENGLISH)
    for want in prefs:
        for name, f in fams.items():
            if name.lower() == want.lower() and (f.bangla if bangla else f.latin):
                return name
    for name, f in fams.items():
        if f.source == "built in" and (f.bangla if bangla else f.latin):
            return name
    return next(iter(fams), "")
