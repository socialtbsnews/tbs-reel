"""Core reel-making logic for TBS Reel (no GUI code here).

A reel is a list of items (photos and video clips) turned into one MP4 at 30 fps
(H.264 + AAC), in one of two sizes:
  * 9:16 reel   1080 x 1920
  * 4:5 post    1080 x 1350
using the TBS frame:
  * TBS logo top left, date and credit top right,
  * a dark shade at the bottom (its size and darkness can be changed) with the
    headline, where any part can be yellow and/or bold, and which can be moved,
  * the teal strip along the bottom edge (4:5 only).

How it is made:
  1. Each item becomes a short high-quality segment: photos get a gentle zoom,
     clips are trimmed, each can be zoomed in on its subject, faded from or to
     black, muted, or have bleeps over words; the frame is laid on top.
  2. The segments (and the optional TBS end card) are joined, the music is
     mixed in, and the reel is encoded once at high quality.
The bundled ffmpeg (from the imageio-ffmpeg package) does the video work.
"""
from __future__ import annotations

import datetime
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Callable

import fribidi_loader  # noqa: F401  (must come before PIL.ImageFont: complex text shaping)
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps, features

import fontlib

FPS = 30
PHOTO_EXT = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp", ".bmp"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".mts", ".m2ts", ".3gp", ".wmv"}
AUDIO_EXT = {".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus", ".wma"}

# ---- the two TBS frames. The 4:5 one is measured from the design; the 9:16 one from
#      TBS's published reels (logo and date kept clear of the app buttons at the top).
LAYOUTS = {
    "reel": dict(label="9:16 reel  (1080 x 1920)", w=1080, h=1920,
                 logo=(88, 236, 88), right=992, date=(298, 36), credit=(334, 28),
                 head_y=0.80, head_size=62, margin=88, shade=30, dark=80, strip=28),
    "feed": dict(label="4:5 post  (1080 x 1350)", w=1080, h=1350,
                 logo=(64, 69, 78), right=1016, date=(102, 30), credit=(131, 24),
                 head_y=0.782, head_size=72, margin=60, shade=30, dark=76, strip=20),
}
W, H = 1080, 1350                    # the 4:5 size (kept for old callers)
HEAD_LINE = 1.32                     # line spacing, as a multiple of the font size
HEAD_MAX_LINES = 3
WHITE, YELLOW = (255, 255, 255, 255), (255, 246, 54, 255)
TEAL = (35, 71, 71, 255)
TOP_WIDE_LIMIT = 1.15                # pictures wider than this (w/h) go "wide at the top" in Auto
FADE = 0.5                           # seconds of each fade from / to black
BLEEP_HZ, BLEEP_VOL = 1000, 0.35

if sys.platform.startswith("win"):
    _NO_WINDOW = {"creationflags": 0x08000000}  # CREATE_NO_WINDOW: no black console boxes
else:
    _NO_WINDOW = {}

LAYOUT = ImageFont.Layout.RAQM if features.check("raqm") else ImageFont.Layout.BASIC
BENGALI = re.compile(r"[ঀ-৿]")
MONTHS = ["JANUARY", "FEBRUARY", "MARCH", "APRIL", "MAY", "JUNE", "JULY", "AUGUST", "SEPTEMBER",
          "OCTOBER", "NOVEMBER", "DECEMBER"]


def base_dir() -> str:
    return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))


def resource(*parts: str) -> str:
    return os.path.join(base_dir(), *parts)


@lru_cache(maxsize=1)
def ffmpeg_exe() -> str:
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        exe = shutil.which("ffmpeg")
        if exe:
            return exe
        raise RuntimeError("ffmpeg was not found. Reinstall the app.")


def today_text() -> str:
    d = datetime.date.today()
    return f"{d.day} {MONTHS[d.month - 1]} | {d.year}"


# ---------------------------------------------------------------- settings
@dataclass
class ReelSettings:
    layout: str = "reel"          # "reel" = 9:16 (1080 x 1920), "feed" = 4:5 (1080 x 1350)
    photo_secs: float = 3.0       # default length for each new photo
    framing: str = "auto"         # auto / fill / top / fit
    motion: str = "zoom"          # "zoom" = gentle zoom on photos (alternating in / out); "none"
    logo_on: bool = True
    logo_path: str = ""           # empty = built-in TBS logo
    date_text: str = ""           # e.g. "25 OCTOBER | 2026" (empty = today)
    credit_text: str = "Video: "  # e.g. "Video: Variety"
    headline: list = field(default_factory=list)   # [[text, highlighted, bold], ...]
    head_align: str = "center"    # left / center / right
    head_pos: dict = field(default_factory=dict)   # per layout: [x offset px, centre y as part of height]
    shade: dict = field(default_factory=dict)      # per layout: [size %, darkness %]
    bangla_font: str = ""         # (kept so old settings files still load)
    english_font: str = ""        # font family for the headline
    top_font: str = ""            # font family for the date and credit
    head_size_pct: float = 100.0  # headline size
    music_path: str = ""
    music_volume_pct: float = 70.0
    clip_sound: bool = True          # keep the sound recorded with video clips
    clip_volume_pct: float = 100.0
    fade_in: bool = False            # fade from black at the start
    fade_out: bool = False           # fade to black at the end
    fade_between: bool = False       # fade through black between clips
    disclaimer: str = ""             # "" = none, or a key of DISCLAIMERS
    disclaimer_at: str = "start"     # start / end
    disclaimer_secs: float = 5.0
    end_card: bool = False           # TBS logo end card
    end_secs: float = 2.5
    out_dir: str = ""


def lay(s: ReelSettings) -> dict:
    return LAYOUTS.get(s.layout, LAYOUTS["reel"])


def dims(s: ReelSettings) -> tuple[int, int]:
    L = lay(s)
    return L["w"], L["h"]


def head_pos(s: ReelSettings) -> tuple[float, float]:
    """(x offset in pixels, centre of the headline block as a part of the height)."""
    p = (s.head_pos or {}).get(s.layout)
    if p and len(p) == 2:
        return float(p[0]), float(p[1])
    return 0.0, lay(s)["head_y"]


def shade_of(s: ReelSettings) -> tuple[float, float]:
    """(size: % of the height that is fully dark, darkness %)."""
    p = (s.shade or {}).get(s.layout)
    L = lay(s)
    if p and len(p) == 2:
        return float(p[0]), float(p[1])
    return float(L["shade"]), float(L["dark"])


@dataclass
class Item:
    path: str
    kind: str                     # "photo" or "video"
    length: float = 3.0           # seconds on screen
    start: float = 0.0            # video only: where to start in the clip
    duration: float = 0.0         # video only: full clip length
    has_audio: bool = False       # video only
    meta: dict = field(default_factory=dict)
    # meta also holds the edits: "zoom", "cx", "cy" (picked subject), "mute",
    # "bleeps" ([[from, to], ...] in seconds of the original file),
    # "blurs" ([[x0, y0, x1, y1], ...] boxes as parts of the frame's width and height)

    @property
    def name(self) -> str:
        return os.path.basename(self.path)


def kind_of(path: str) -> str | None:
    ext = os.path.splitext(path)[1].lower()
    if ext in PHOTO_EXT:
        return "photo"
    if ext in VIDEO_EXT:
        return "video"
    return None


# ---------------------------------------------------------------- ffmpeg helpers
def probe(path: str) -> dict:
    """Duration, size and whether there is sound, read from ffmpeg's own report."""
    r = subprocess.run([ffmpeg_exe(), "-hide_banner", "-i", path], capture_output=True,
                       text=True, errors="replace", **_NO_WINDOW)
    txt = r.stderr
    info = {"duration": 0.0, "has_audio": False, "has_video": False, "width": 0, "height": 0}
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", txt)
    if m:
        info["duration"] = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])
    info["has_audio"] = bool(re.search(r"Stream #.*: Audio:", txt))
    vm = re.search(r"Stream #.*: Video:.*?(\d{2,5})x(\d{2,5})", txt)
    if vm:
        info["has_video"] = True
        info["width"], info["height"] = int(vm[1]), int(vm[2])
        rot = re.search(r"rotation of (-?\d+(?:\.\d+)?) degrees", txt) or re.search(r"rotate\s*:\s*(-?\d+)", txt)
        if rot and round(abs(float(rot[1]))) % 180 == 90:   # phone clips filmed upright
            info["width"], info["height"] = info["height"], info["width"]
    return info


def make_item(path: str, photo_secs: float) -> Item | None:
    k = kind_of(path)
    if k == "photo":
        try:
            with Image.open(path) as im:
                w, h = ImageOps.exif_transpose(im).size if im.getexif().get(0x0112, 1) in (5, 6, 7, 8) else im.size
        except Exception:
            return None
        return Item(path, "photo", length=photo_secs, meta={"width": w, "height": h})
    if k == "video":
        info = probe(path)
        if not info["has_video"] or info["duration"] <= 0:
            return None
        return Item(path, "video", length=round(info["duration"], 2), duration=info["duration"],
                    has_audio=info["has_audio"], meta=info)
    return None


def video_frame(path: str, t: float = 0.0) -> Image.Image | None:
    """One frame of a clip (upright), for the preview."""
    try:
        r = subprocess.run([ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-ss", f"{max(0, t):.3f}",
                            "-i", path, "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"],
                           capture_output=True, timeout=30, **_NO_WINDOW)
        if r.stdout:
            from io import BytesIO
            return Image.open(BytesIO(r.stdout)).convert("RGB")
    except Exception:
        pass
    return None


def load_photo(path: str, max_side: int = 0) -> Image.Image:
    with Image.open(path) as im:
        if max_side:
            im.draft("RGB", (max_side, max_side))
        im = ImageOps.exif_transpose(im)
        if max_side:
            im.thumbnail((max_side, max_side), Image.LANCZOS)
        return im.convert("RGB")


# ---------------------------------------------------------------- picture framing
def resolve_framing(framing: str, pic_w: int, pic_h: int) -> str:
    """Auto: wide pictures sit full-width at the top; the rest fill the frame."""
    if framing == "auto":
        return "top" if pic_w and pic_h and pic_w / pic_h > TOP_WIDE_LIMIT else "fill"
    return framing


def has_subject(item: Item | None) -> bool:
    return item is not None and item.meta.get("zoom") is not None


def subject_box(src_w: float, src_h: float, out_w: float, out_h: float,
                zoom: float, cx: float, cy: float) -> tuple[float, float, float, float]:
    """The part of the source picture (x, y, w, h) that fills the frame when the user
    has zoomed in on a subject: zoom 1 = as big as fits, cx / cy = its centre (0..1)."""
    a = out_w / out_h
    if src_w / src_h > a:
        ch, cw = src_h, src_h * a
    else:
        cw, ch = src_w, src_w / a
    zoom = max(1.0, zoom)
    cw, ch = cw / zoom, ch / zoom
    x = min(max(cx * src_w - cw / 2, 0), src_w - cw)
    y = min(max(cy * src_h - ch / 2, 0), src_h - ch)
    return x, y, cw, ch


def _cover(img: Image.Image, w: int, h: int) -> Image.Image:
    return ImageOps.fit(img, (w, h), Image.LANCZOS, centering=(0.5, 0.5))


def _blur_bg(img: Image.Image, w: int, h: int) -> Image.Image:
    small = _cover(img, max(8, w // 8), max(8, h // 8)).filter(ImageFilter.GaussianBlur(6))
    return small.resize((w, h), Image.BICUBIC).point(lambda v: int(v * 0.72))


def frame_picture(img: Image.Image, framing: str, w: int = W, h: int = H) -> Image.Image:
    """Put a picture of any shape onto the w x h frame.
    fill = crop to fill; top = full width at the top over a blurred copy;
    fit = whole picture in the middle over a blurred copy."""
    framing = resolve_framing(framing, img.width, img.height)
    if framing == "fill":
        return _cover(img, w, h)
    bg = _blur_bg(img, w, h)
    s = min(w / img.width, h / img.height)
    fw, fh = max(1, round(img.width * s)), max(1, round(img.height * s))
    y = 0 if framing == "top" else (h - fh) // 2
    bg.paste(img.resize((fw, fh), Image.LANCZOS), ((w - fw) // 2, y))
    return bg


def place_picture(img: Image.Image, item: Item | None, framing: str, w: int, h: int) -> Image.Image:
    """Frame a picture for this item: its picked subject if it has one, else the framing."""
    if has_subject(item):
        x, y, cw, ch = subject_box(img.width, img.height, w, h, item.meta["zoom"],
                                   item.meta.get("cx", 0.5), item.meta.get("cy", 0.5))
        return img.resize((w, h), Image.LANCZOS if cw > w else Image.BICUBIC, box=(x, y, x + cw, y + ch))
    return frame_picture(img, framing, w, h)


def picture_filter(item: Item, framing: str, w: int, h: int, fps: int) -> str:
    """ffmpeg filter (input [0:v] -> output [pic]) that frames a clip the same way."""
    if has_subject(item):
        sw, sh = item.meta.get("width") or 16, item.meta.get("height") or 9
        x, y, cw, ch = subject_box(sw, sh, w, h, item.meta["zoom"], item.meta.get("cx", 0.5),
                                   item.meta.get("cy", 0.5))
        return (f"[0:v]fps={fps},crop=trunc(iw*{cw / sw:.5f}/2)*2:trunc(ih*{ch / sh:.5f}/2)*2:"
                f"iw*{x / sw:.5f}:ih*{y / sh:.5f},scale={w}:{h},setsar=1[pic]")
    mode = resolve_framing(framing, item.meta.get("width", 0), item.meta.get("height", 0))
    if mode == "fill":
        return f"[0:v]fps={fps},scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1[pic]"
    y = "0" if mode == "top" else "(H-h)/2"
    sw, sh = max(8, w // 8), max(8, h // 8)
    return (f"[0:v]fps={fps},split[a][b];"
            f"[a]scale={sw}:{sh}:force_original_aspect_ratio=increase,crop={sw}:{sh},"
            f"gblur=sigma=6,scale={w}:{h},eq=brightness=-0.09[bg];"
            f"[b]scale={w}:{h}:force_original_aspect_ratio=decrease:force_divisible_by=2[fg];"
            f"[bg][fg]overlay=(W-w)/2:{y},setsar=1[pic]")


# ---------------------------------------------------------------- the frame overlay
# ---- fonts: families come from fontlib (built in, installed on the computer, or added)
_FAMILIES: dict = {}


def set_families(fams: dict) -> None:
    global _FAMILIES
    _FAMILIES = dict(fams)
    _font_file.cache_clear()
    _balance.cache_clear()


def families() -> dict:
    if not _FAMILIES:
        set_families(fontlib.built_in())
    return _FAMILIES


def _family(name: str, bangla: bool, prefs=None):
    fams = families()
    f = fams.get(name)
    if f is None or not (f.bangla if bangla else f.latin):
        f = fams.get(fontlib.pick_default(fams, bangla, prefs))
    return f


@lru_cache(maxsize=64)
def _font_file(path: str, size: int):
    try:
        return ImageFont.truetype(path, max(6, size), layout_engine=LAYOUT)
    except OSError:
        return ImageFont.load_default(max(6, size))


@lru_cache(maxsize=64)
def _balance(path: str, bangla: bool) -> float:
    """Fonts differ in how big their letters are for the same point size. Scale each so
    letters look the same size whatever fonts are picked."""
    try:
        f = ImageFont.truetype(path, 100, layout_engine=LAYOUT)
        box = f.getbbox("ক" if bangla else "H")
        h = box[3] - box[1]
        return max(0.8, min(1.35, (62 if bangla else 71) / h)) if h > 0 else 1.0
    except Exception:
        return 1.0


def get_font(family: str, bangla: bool, bold: bool, size: int, prefs=None):
    """(font, extra stroke width for a fake bold)."""
    f = _family(family, bangla, prefs)
    if f is None:
        return ImageFont.load_default(max(6, size)), 0
    path = f.bold if bold else f.regular
    fake = bold and f.fake_bold
    size = round(size * _balance(path, bangla))
    return _font_file(path, size), (max(1, round(size / 60)) if fake else 0)


def get_font_style(family: str, bold: bool, italic: bool, size: int, prefs=None):
    """(font, fake-bold stroke, needs a fake slant) for English text in any style."""
    f = _family(family, False, prefs)
    if f is None:
        return ImageFont.load_default(max(6, size)), 0, italic
    if not italic:
        fnt, stroke = get_font(family, False, bold, size, prefs)
        return fnt, stroke, False
    path = (f.bold_italic or f.italic) if bold else f.italic
    fake = bool(bold and not f.bold_italic)
    if not path:
        fnt, stroke = get_font(family, False, bold, size, prefs)
        return fnt, stroke, True
    size = round(size * _balance(path, False))
    return _font_file(path, size), (max(1, round(size / 60)) if fake else 0), False


def headline_font(size: int, bangla: bool = True, bold: bool = True, s: "ReelSettings | None" = None):
    s = s or ReelSettings()
    return get_font(s.bangla_font if bangla else s.english_font, bangla, bold, size)[0]


NEUTRAL = re.compile(r"[^A-Za-zঀ-৿]")


def script_runs(text: str) -> list[tuple[str, bool]]:
    """Split text into Bangla / English pieces. Spaces, digits and punctuation
    go with the letters before them (or after, at the start)."""
    out: list[list] = []
    pending = ""
    for ch in text:
        if NEUTRAL.match(ch) or ch in "‌‍":
            if out:
                out[-1][0] += ch
            else:
                pending += ch
            continue
        bn = bool(BENGALI.match(ch))
        if out and out[-1][1] == bn:
            out[-1][0] += ch
        else:
            out.append([pending + ch, bn])
            pending = ""
    if pending:
        out.append([pending, False])
    return [(t, b) for t, b in out]


@dataclass
class Span:
    text: str
    color: tuple
    font: object
    stroke: int


def _spans(pieces, s: "ReelSettings", size: int, default_bold: bool = False, top: bool = False) -> list[Span]:
    """pieces: [(text, highlighted, bold)] -> drawable spans with the right font for each script."""
    spans: list[Span] = []
    for text, hl, bold in pieces:
        for t, bn in script_runs(text):
            if top and not bn:
                fnt, stroke = get_font(s.top_font, False, bool(bold or default_bold), size, fontlib.PREFERRED_TOP)
            else:
                fnt, stroke = get_font(s.bangla_font if bn else s.english_font, bn, bool(bold or default_bold), size)
            color = YELLOW if hl else WHITE
            if spans and spans[-1].font is fnt and spans[-1].color == color and spans[-1].stroke == stroke:
                spans[-1].text += t
            else:
                spans.append(Span(t, color, fnt, stroke))
    return spans


def _spans_width(spans: list[Span]) -> float:
    return sum(sp.font.getlength(sp.text) + 2 * sp.stroke for sp in spans)


def _draw_spans(layer: Image.Image, x: float, base: float, spans: list[Span], shadow: bool = False):
    if shadow:
        sh = Image.new("RGBA", layer.size, (0, 0, 0, 0))
        d = ImageDraw.Draw(sh)
        xx = x
        for sp in spans:
            d.text((xx + 1, base + 2), sp.text, font=sp.font, fill=(0, 0, 0, 150), anchor="ls",
                   stroke_width=sp.stroke, stroke_fill=(0, 0, 0, 150))
            xx += sp.font.getlength(sp.text) + 2 * sp.stroke
        layer.alpha_composite(sh.filter(ImageFilter.GaussianBlur(3)))
    d = ImageDraw.Draw(layer)
    for sp in spans:
        d.text((x + sp.stroke, base), sp.text, font=sp.font, fill=sp.color, anchor="ls",
               stroke_width=sp.stroke, stroke_fill=sp.color)
        x += sp.font.getlength(sp.text) + 2 * sp.stroke


@lru_cache(maxsize=8)
def _logo(path: str) -> Image.Image | None:
    p = path if path and os.path.exists(path) else resource("assets", "logo.png")
    try:
        return Image.open(p).convert("RGBA")
    except Exception:
        return None


@lru_cache(maxsize=16)
def shade_layer(w: int, h: int, size_pct: float, dark_pct: float, strip: int) -> Image.Image:
    """The dark shade at the bottom: fully dark for size_pct of the height, with a soft
    fade above it, and the teal strip along the bottom edge (4:5)."""
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    solid = h * max(0.0, min(90.0, size_pct)) / 100
    if solid > 0 and dark_pct > 0:
        top = h - solid
        ramp = max(80.0, solid * 0.8)
        amax = 255 * max(0.0, min(100.0, dark_pct)) / 100
        col = Image.new("L", (1, h), 0)
        px = col.load()
        for y in range(h):
            if y >= top:
                a = 1.0
            elif y > top - ramp:
                p = (y - (top - ramp)) / ramp
                a = p * p * (3 - 2 * p)
            else:
                a = 0.0
            px[0, y] = round(amax * a)
        layer.putalpha(col.resize((w, h)))
    if strip:
        layer.paste(TEAL, (0, h - strip, w, h))
    return layer


def _norm_runs(runs) -> list[tuple[str, bool, bool]]:
    out = []
    for r in runs or []:
        r = list(r) + [False, False]
        out.append((str(r[0]), bool(r[1]), bool(r[2])))
    return out


def _tokens(runs) -> list[list[tuple[str, bool, bool]]]:
    """Headline runs -> lines (split at Enter) -> pieces (words and spaces) with their style."""
    lines: list[list] = [[]]
    for text, hl, bold in _norm_runs(runs):
        for i, part in enumerate(text.split("\n")):
            if i:
                lines.append([])
            for piece in re.findall(r"\s+|\S+", part):
                lines[-1].append((piece, hl, bold))
    return lines


def _strip(line):
    line = list(line)
    while line and line[0][0].isspace():
        line.pop(0)
    while line and line[-1][0].isspace():
        line.pop()
    return line


def _wrap(lines, width_of, max_w):
    """Wrap each line at spaces so no line is wider than max_w."""
    out = []
    for line in lines:
        words: list[list] = []
        for piece in line:
            if piece[0].isspace():
                words.append([(" ", piece[1], piece[2])])
            elif words and not words[-1][-1][0].isspace():
                words[-1].append(piece)       # same word, different style
            else:
                words.append([piece])
        cur: list = []
        for w in words:
            trial = cur + w
            if cur and not w[0][0].isspace() and width_of(_strip(trial)) > max_w:
                out.append(_strip(cur))
                cur = list(w)
            else:
                cur = trial
        out.append(_strip(cur))
    return [ln for ln in out if ln]


def layout_headline(s: "ReelSettings"):
    """Font size and wrapped lines (as spans); shrinks until it fits in HEAD_MAX_LINES lines."""
    L = lay(s)
    max_w = L["w"] - 2 * L["margin"]
    lines = _tokens(s.headline)
    if not any(not p[0].isspace() for ln in lines for p in ln):
        return L["head_size"], []
    size = round(L["head_size"] * max(40.0, min(160.0, s.head_size_pct)) / 100)
    while True:
        width_of = lambda ln, sz=size: _spans_width(_spans(ln, s, sz))  # noqa: E731
        wrapped = _wrap(lines, width_of, max_w)
        if (len(wrapped) <= HEAD_MAX_LINES and all(width_of(ln) <= max_w for ln in wrapped)) or size <= 30:
            return size, [_spans(ln, s, size) for ln in wrapped]
        size -= 3


def headline_placement(s: ReelSettings):
    """[(x, baseline, spans)] for each headline line, and the block's box (x0, y0, x1, y1)."""
    L = lay(s)
    w, h = L["w"], L["h"]
    size, lines = layout_headline(s)
    if not lines:
        return [], None
    dx, cy = head_pos(s)
    pitch = round(size * HEAD_LINE)
    asc = max(sp.font.getmetrics()[0] for ln in lines for sp in ln)
    desc = max(sp.font.getmetrics()[1] for ln in lines for sp in ln)
    top = cy * h - (pitch * (len(lines) - 1) + asc) / 2   # centre the block on cy
    out, x0s, x1s = [], [], []
    for i, spans in enumerate(lines):
        lw = _spans_width(spans)
        if s.head_align == "left":
            x = L["margin"]
        elif s.head_align == "right":
            x = w - L["margin"] - lw
        else:
            x = (w - lw) / 2
        x += dx
        out.append((x, top + asc + i * pitch, spans))
        x0s.append(x)
        x1s.append(x + lw)
    box = (min(x0s), top, max(x1s), top + asc + (len(lines) - 1) * pitch + desc)
    return out, box


def overlay(s: ReelSettings, w: int = 0, h: int = 0) -> Image.Image:
    """The TBS frame as a transparent layer: shade, teal strip, logo,
    date + credit, and the headline with its yellow / bold parts."""
    L = lay(s)
    fw, fh = L["w"], L["h"]
    size_pct, dark_pct = shade_of(s)
    layer = shade_layer(fw, fh, round(size_pct, 1), round(dark_pct, 1), L["strip"]).copy()
    if s.logo_on:
        logo = _logo(s.logo_path)
        if logo is not None:
            x, y, size = L["logo"]
            m = logo.resize((size, max(1, round(logo.height * size / logo.width))), Image.LANCZOS)
            sh = Image.new("RGBA", layer.size, (0, 0, 0, 0))
            shadow = Image.new("RGBA", m.size, (0, 0, 0, 0))
            shadow.putalpha(m.getchannel("A").point(lambda a: a * 70 // 255))
            sh.alpha_composite(shadow, (x + 1, y + 2))
            layer.alpha_composite(sh.filter(ImageFilter.GaussianBlur(3)))
            layer.alpha_composite(m, (x, y))
    for text, (base, size) in (((s.date_text or today_text()).strip(), L["date"]),
                               (s.credit_text.strip(), L["credit"])):
        if text and not text.endswith(":"):
            spans = _spans([(text, False, False)], s, size, top=True)
            _draw_spans(layer, L["right"] - _spans_width(spans), base, spans, shadow=True)
    lines, _box = headline_placement(s)
    for x, base, spans in lines:
        _draw_spans(layer, x, base, spans, shadow=True)
    if (w or fw, h or fh) != (fw, fh):
        layer = layer.resize((w, h), Image.LANCZOS)
    return layer


def end_card_frame(t: float, total: float, logo_path: str = "", w: int = W, h: int = H) -> Image.Image:
    """TBS end card: the logo and wordmark on dark teal-navy, easing in."""
    img = Image.new("RGB", (w, h), (14, 20, 30))
    p = min(1.0, t / 0.6)
    ease = 1 - (1 - p) ** 3
    logo = _logo(logo_path)
    if logo is not None:
        lw = round(w * (0.34 + 0.04 * ease))
        m = logo.resize((lw, round(logo.height * lw / logo.width)), Image.LANCZOS)
        m.putalpha(m.getchannel("A").point(lambda a: round(a * ease)))
        img.paste(m, ((w - lw) // 2, round(h * 0.42 - m.height / 2)), m)
        try:
            bm = Image.open(resource("assets", "brand.png")).convert("RGBA")
            bw = round(w * 0.70)
            b = bm.resize((bw, round(bm.height * bw / bm.width)), Image.LANCZOS)
            b.putalpha(b.getchannel("A").point(lambda a: round(a * ease)))
            img.paste(b, ((w - bw) // 2, round(h * 0.42 + m.height / 2 + 60)), b)
        except Exception:
            pass
    fade_out = max(0.0, min(1.0, (total - t) / 0.4))  # gentle fade to black at the very end
    if fade_out < 1:
        img = img.point(lambda v: int(v * fade_out))
    return img


# ---------------------------------------------------------------- fades and bleeps
def fades_for(items: list[Item], s: ReelSettings, i: int) -> tuple[float, float]:
    """(fade-in seconds, fade-out seconds) from / to black for item i."""
    n = len(items)
    L = items[i].length
    d = min(FADE, L / 3)
    fin = d if (i == 0 and s.fade_in) or (i > 0 and s.fade_between) else 0.0
    last = i == n - 1
    fout = d if (last and ((s.fade_out and not s.end_card) or (s.fade_between and s.end_card))) or \
        (not last and s.fade_between) else 0.0
    return fin, fout


def fade_level(pos: float, length: float, fin: float, fout: float) -> float:
    """How much of the picture shows at pos (1 = all, 0 = black)."""
    a = 1.0
    if fin > 0 and pos < fin:
        a = min(a, pos / fin)
    if fout > 0 and pos > length - fout:
        a = min(a, (length - pos) / fout)
    return max(0.0, min(1.0, a))


def bleeps_in(item: Item) -> list[tuple[float, float]]:
    """Bleeps of this item as (from, to) seconds from the start of the item."""
    out = []
    for a, b in item.meta.get("bleeps") or []:
        a, b = a - item.start, b - item.start
        if b > 0 and a < item.length:
            out.append((max(0.0, a), min(item.length, b)))
    return sorted(out)


def uses_clip_sound(item: Item, s: ReelSettings) -> bool:
    return item.kind == "video" and item.has_audio and s.clip_sound and not item.meta.get("mute")


# ---------------------------------------------------------------- blur boxes
# Each blur box is {"w", "h": its size as parts of the frame,
#                   "keys": [[time in the file, centre x, centre y], ...]  (one key = it stays still;
#                           more keys = it moves smoothly between them, to follow a moving subject),
#                   "from", "to": times in the file it starts and stops (None = the whole clip)}.
def blur_list(item: Item | None) -> list[dict]:
    out = []
    for b in (item.meta.get("blurs") or []) if item else []:
        if isinstance(b, dict):
            d = {"w": float(b.get("w", 0.2)), "h": float(b.get("h", 0.1)),
                 "keys": sorted([list(map(float, k)) for k in b.get("keys") or [[item.start, 0.5, 0.5]]]),
                 "from": b.get("from"), "to": b.get("to"), "follow": bool(b.get("follow"))}
        else:                                   # old style [x0, y0, x1, y1]
            x0, y0, x1, y1 = b
            d = {"w": abs(x1 - x0), "h": abs(y1 - y0), "keys": [[item.start, (x0 + x1) / 2, (y0 + y1) / 2]],
                 "from": None, "to": None, "follow": False}
        out.append(d)
    return out


def blur_centre(d: dict, ft: float) -> tuple[float, float]:
    keys = d["keys"]
    if ft <= keys[0][0]:
        return keys[0][1], keys[0][2]
    for (t0, x0, y0), (t1, x1, y1) in zip(keys, keys[1:]):
        if ft < t1:
            p = (ft - t0) / max(1e-6, t1 - t0)
            return x0 + (x1 - x0) * p, y0 + (y1 - y0) * p
    return keys[-1][1], keys[-1][2]


def blur_active(d: dict, ft: float) -> bool:
    return (d["from"] is None or ft >= d["from"] - 1e-3) and (d["to"] is None or ft <= d["to"] + 1e-3)


def _blur_px(d: dict, w: int, h: int) -> tuple[int, int]:
    bw = min(w, max(16, int(d["w"] * w) // 2 * 2))
    bh = min(h, max(16, int(d["h"] * h) // 2 * 2))
    return bw, bh


def _blur_xy(d: dict, cx: float, cy: float, w: int, h: int) -> tuple[float, float]:
    bw, bh = _blur_px(d, w, h)
    return max(0.0, min(w - bw, cx * w - bw / 2)), max(0.0, min(h - bh, cy * h - bh / 2))


def blur_boxes(item: Item | None, w: int, h: int, pos: float = 0.0) -> list[tuple[int, int, int, int]]:
    """Blur boxes showing at pos (seconds into the item), in pixels (x, y, width, height)."""
    out = []
    if item is None:
        return out
    ft = item.start + pos
    for d in blur_list(item):
        if blur_active(d, ft):
            bw, bh = _blur_px(d, w, h)
            x, y = _blur_xy(d, *blur_centre(d, ft), w, h)
            out.append((int(x), int(y), bw, bh))
    return out


def _blur_small(bw: int, bh: int) -> tuple[int, int]:
    k = max(bw, bh) / 14            # about 14 blocks across: faces and text can't be read
    return max(2, round(bw / k)), max(2, round(bh / k))


def blur_picture(img: Image.Image, item: Item | None, pos: float = 0.0) -> Image.Image:
    """Blur the item's boxes on a framed picture (same look as the made reel)."""
    boxes = blur_boxes(item, img.width, img.height, pos)
    if not boxes:
        return img
    img = img.copy()
    for X, Y, bw, bh in boxes:
        sw, sh = _blur_small(bw, bh)
        part = img.crop((X, Y, X + bw, Y + bh)).resize((sw, sh), Image.BOX)
        part = part.filter(ImageFilter.GaussianBlur(0.6)).resize((bw, bh), Image.BICUBIC)
        img.paste(part, (X, Y))
    return img


def _piecewise(points: list[tuple[float, float]]) -> str:
    """ffmpeg expression in t that goes straight from point to point (and stays put outside)."""
    if len(points) == 1:
        return f"{points[0][1]:.1f}"
    expr = f"{points[-1][1]:.1f}"
    for (t0, v0), (t1, v1) in reversed(list(zip(points, points[1:]))):
        seg = f"{v0:.1f}+({v1 - v0:.1f})*(t-({t0:.3f}))/{max(1e-3, t1 - t0):.3f}"
        expr = f"if(lt(t\\,{t1:.3f})\\,{seg}\\,{expr})"
    return f"if(lt(t\\,{points[0][0]:.3f})\\,{points[0][1]:.1f}\\,{expr})"


def blur_filter(item: Item, w: int, h: int, src: str, dst: str) -> str:
    """ffmpeg filter [src] -> [dst] that blurs the item's boxes (moving and timed as set)."""
    boxes = [d for d in blur_list(item)
             if (d["to"] is None or d["to"] > item.start) and (d["from"] is None or d["from"] < item.start + item.length)]
    if not boxes:
        return f"[{src}]null[{dst}]"
    n = len(boxes)
    parts = [f"[{src}]split={n + 1}[bl0]" + "".join(f"[br{k}]" for k in range(n))]
    cur = "bl0"
    for k, d in enumerate(boxes):
        bw, bh = _blur_px(d, w, h)
        pts = [(t - item.start, *_blur_xy(d, cx, cy, w, h)) for t, cx, cy in d["keys"]]
        ex = _piecewise([(t, x) for t, x, _y in pts])
        ey = _piecewise([(t, y) for t, _x, y in pts])
        sw, sh = _blur_small(bw, bh)
        parts.append(f"[br{k}]crop={bw}:{bh}:{ex}:{ey},scale={sw}:{sh}:flags=area,gblur=sigma=0.6,"
                     f"scale={bw}:{bh}:flags=bicubic[bq{k}]")
        a = -1.0 if d["from"] is None else d["from"] - item.start
        b = item.length + 1 if d["to"] is None else d["to"] - item.start
        parts.append(f"[{cur}][bq{k}]overlay={ex}:{ey}:enable=between(t\\,{a:.3f}\\,{b:.3f})[bo{k}]")
        cur = f"bo{k}"
    parts.append(f"[{cur}]null[{dst}]")
    return ";".join(parts)


# ---------------------------------------------------------------- disclaimer cards
RED = (196, 0, 46, 255)
DISCLAIMERS = {
    "discretion": dict(
        label="Viewer discretion 1",
        title="Viewer Discretion Advised", sub="Content Disclaimer", italic=True, bold=False,
        body=["This content may include sensitive or distressing topics. It is shared strictly for "
              "journalistic, educational, or informational purposes based on verified sources. We do not "
              "endorse, promote, or glorify violence, hatred, discrimination, graphic content, or any form "
              "of harmful behavior. This material complies with applicable platform Community Guidelines."]),
    "meta": dict(
        label="Viewer discretion 2 (Meta)",
        title="Viewer Discretion Advised", sub="Content Disclaimer", italic=True, bold=False,
        body=["This content is shared in accordance with Meta Community Standards. It is intended solely for "
              "journalistic, educational, or informative purposes. All information is based on verified "
              "sources. We do not endorse, promote, or glorify violence, hatred, discrimination, or any form "
              "of harmful behavior."]),
    "copyright": dict(
        label="Copyright & fair use",
        title="Copyright & Fair Use Disclaimer", sub="", italic=False, bold=True,
        body=["This video is shared strictly for news reporting, historical documentation, education, and "
              "public awareness purposes.",
              "We do not claim ownership of the video, audio, images, or any third-party materials used herein. "
              "All copyrights remain with their respective owners.",
              "The content is used in accordance with fair use principles under applicable copyright laws and "
              "Meta’s Community Standards, for the purpose of providing context, analysis, and factual "
              "reporting in the public interest.",
              "If you are the copyright owner and believe this material has been used incorrectly, please "
              "contact us directly so we can take appropriate action."]),
}


def _wrap_text(text: str, font, max_w: float) -> list[str]:
    lines, cur = [], ""
    for word in text.split():
        trial = f"{cur} {word}".strip()
        if cur and font.getlength(trial) > max_w:
            lines.append(cur)
            cur = word
        else:
            cur = trial
    if cur:
        lines.append(cur)
    return lines


def _warning_sign(size: int) -> Image.Image:
    """Red warning triangle with an exclamation mark (drawn 4x larger, then smoothed)."""
    k = 4
    S = size * k
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    t = S * 0.085
    pts = [(S / 2, t), (S - t, S * 0.87), (t, S * 0.87)]
    d.line(pts + [pts[0]], fill=RED, width=round(t * 1.25), joint="curve")
    for p in pts:
        r = t * 0.62
        d.ellipse((p[0] - r, p[1] - r, p[0] + r, p[1] + r), fill=RED)
    bw = S * 0.075
    d.rounded_rectangle((S / 2 - bw / 2, S * 0.36, S / 2 + bw / 2, S * 0.64), radius=bw / 2, fill=RED)
    r = bw * 0.62
    d.ellipse((S / 2 - r, S * 0.72 - r, S / 2 + r, S * 0.72 + r), fill=RED)
    return im.resize((size, size), Image.LANCZOS)


def disclaimer_card(key: str, s: ReelSettings, w: int, h: int) -> Image.Image:
    """The disclaimer as a full picture (w x h)."""
    D = DISCLAIMERS[key]
    k = w / 1080
    img = Image.new("RGBA", (w, h), (13, 13, 13, 255))
    title_f = get_font(s.english_font, False, True, round(54 * k))[0]
    sub_f = get_font(s.english_font, False, False, round(38 * k))[0]
    body_f, body_stroke, fake_slant = get_font_style(s.english_font, D["bold"], D["italic"], round(34 * k))
    max_w = w - round(150 * k)
    pitch = round(34 * k * 1.62)
    body_lines: list[str | None] = []
    for para in D["body"]:
        body_lines += _wrap_text(para, body_f, max_w)
    sign = _warning_sign(round(230 * k))
    title_lines = _wrap_text(D["title"], title_f, max_w)
    tp = round(54 * k * 1.3)
    gap1, gap2, gap3 = round(56 * k), round(46 * k), round(40 * k)
    block = sign.height + gap1 + tp * len(title_lines) + (gap2 + round(38 * k) if D["sub"] else 0) + gap3 + \
        pitch * len(body_lines)
    y = (h - block) / 2
    img.alpha_composite(sign, ((w - sign.width) // 2, round(y)))
    y += sign.height + gap1
    d = ImageDraw.Draw(img)
    for ln in title_lines:
        d.text((w / 2, y + tp * 0.8), ln, font=title_f, fill=RED, anchor="ms")
        y += tp
    if D["sub"]:
        y += gap2
        d.text((w / 2, y + round(38 * k) * 0.8), D["sub"], font=sub_f, fill=WHITE, anchor="ms")
        y += round(38 * k)
    y += gap3
    sl = 0.2 if fake_slant else 0.0       # no italic file: slant the upright letters
    for ln in body_lines:
        band = Image.new("RGBA", (w, pitch), (0, 0, 0, 0))
        ImageDraw.Draw(band).text((w / 2, pitch * 0.72), ln, font=body_f, fill=WHITE, anchor="ms",
                                  stroke_width=body_stroke, stroke_fill=WHITE)
        if sl:
            band = band.transform(band.size, Image.AFFINE, (1, sl, -sl * pitch * 0.6, 0, 1, 0), Image.BICUBIC)
        img.alpha_composite(band, (0, round(y)))
        y += pitch
    return img.convert("RGB")


def disclaimer_secs(s: ReelSettings) -> float:
    return max(1.0, float(s.disclaimer_secs)) if s.disclaimer in DISCLAIMERS else 0.0


def render_still(img: Image.Image, secs: float, out: str, log: str, cancel=None) -> None:
    """A still picture as a segment, with a short fade from and to black."""
    w, h = img.size
    png = out + ".png"
    img.save(png)
    f = min(0.3, secs / 4)
    cmd = [ffmpeg_exe(), "-y", "-hide_banner", "-loop", "1", "-framerate", str(FPS), "-i", png,
           "-f", "lavfi", "-t", f"{secs:.3f}", "-i", "anullsrc=r=48000:cl=stereo",
           "-filter_complex", f"[0:v]format=yuv420p,fade=t=in:st=0:d={f:.3f},"
                              f"fade=t=out:st={secs - f:.3f}:d={f:.3f}[v]",
           "-map", "[v]", "-map", "1:a", *_SEG_V, *_SEG_A, "-t", f"{secs:.3f}", out]
    _run(cmd, log, total_secs=secs, cancel=cancel)


# ---------------------------------------------------------------- sound for the preview
SR = 48000


def decode_audio(path: str, start: float = 0.0, secs: float = 0.0, sr: int = SR):
    """Sound of a file as float32 stereo samples (numpy array, shape (n, 2))."""
    import numpy as np
    cmd = [ffmpeg_exe(), "-v", "error", "-ss", f"{max(0.0, start):.3f}"]
    if secs > 0:
        cmd += ["-t", f"{secs:.3f}"]
    cmd += ["-i", path, "-vn", "-ac", "2", "-ar", str(sr), "-f", "f32le", "-"]
    r = subprocess.run(cmd, capture_output=True, **_NO_WINDOW)
    a = np.frombuffer(r.stdout[: len(r.stdout) // 8 * 8], np.float32).reshape(-1, 2)
    return a


def _ramp(a, fin: float, fout: float, sr: int):
    import numpy as np
    n = len(a)
    if fin > 0:
        k = min(n, int(fin * sr))
        a[:k] *= np.linspace(0, 1, k, dtype=np.float32)[:, None]
    if fout > 0:
        k = min(n, int(fout * sr))
        if k:
            a[n - k:] *= np.linspace(1, 0, k, dtype=np.float32)[:, None]


def preview_audio(items: list[Item], s: ReelSettings, sr: int = SR, cache: dict | None = None):
    """The reel's sound (clips with mute, bleeps and fades, plus music), like the made reel."""
    import numpy as np
    cache = {} if cache is None else cache
    total = sum(it.length for it in items)
    n = int(round(total * sr))
    out = np.zeros((n, 2), np.float32)
    t0 = 0.0
    for i, it in enumerate(items):
        a0, L = int(round(t0 * sr)), int(round(it.length * sr))
        t0 += it.length
        if not uses_clip_sound(it, s):
            continue
        key = (it.path, round(it.start, 3), round(it.length, 3))
        seg = cache.get(key)
        if seg is None:
            seg = decode_audio(it.path, it.start, it.length, sr)
            cache[key] = seg
        seg = seg[:L] * np.float32(s.clip_volume_pct / 100)
        for a, b in bleeps_in(it):
            ia, ib = int(a * sr), min(len(seg), int(b * sr))
            if ib > ia:
                tt = np.arange(ib - ia, dtype=np.float32) / sr
                seg[ia:ib] = (BLEEP_VOL * np.sin(2 * np.pi * BLEEP_HZ * tt))[:, None]
        _ramp(seg, *fades_for(items, s, i), sr)
        m = min(len(seg), n - a0)
        out[a0:a0 + m] += seg[:m]
    music = s.music_path if s.music_path and os.path.exists(s.music_path) else ""
    if music and n:
        key = ("music", music)
        mus = cache.get(key)
        if mus is None:
            mus = decode_audio(music, 0, 900, sr)
            cache[key] = mus
        if len(mus):
            reps = -(-n // len(mus))
            m = np.tile(mus, (reps, 1))[:n] * np.float32(s.music_volume_pct / 100)
            _ramp(m, FADE if s.fade_in else 0.3, min(2.0, total / 4), sr)
            out += m
    np.clip(out, -0.95, 0.95, out=out)
    return out


# ---------------------------------------------------------------- rendering
class Cancelled(Exception):
    pass


def _ease(x: float) -> float:
    return x * x * (3 - 2 * x)


def photo_frames(item: Item, s: ReelSettings, index: int):
    """Yield the frames of a photo with an optional gentle zoom (alternating in and out)."""
    w, h = dims(s)
    n = max(1, round(item.length * FPS))
    zoom = 1.08 if s.motion == "zoom" else 1.0
    bw, bh = round(w * zoom), round(h * zoom)
    big = place_picture(load_photo(item.path), item, s.framing, bw, bh)
    if zoom == 1.0:
        data = big.tobytes()
        for _ in range(n):
            yield data
        return
    zoom_in = index % 2 == 0
    for i in range(n):
        p = _ease(i / max(1, n - 1))
        z = 1 + (zoom - 1) * (p if zoom_in else 1 - p)   # 1 = whole picture, zoom = closest
        cw, ch = bw / z, bh / z
        x0, y0 = (bw - cw) / 2, (bh - ch) / 2
        yield big.resize((w, h), Image.BICUBIC, box=(x0, y0, x0 + cw, y0 + ch)).tobytes()


def _run(cmd: list[str], log: str, stdin_frames=None, total_secs: float = 0.0,
         progress: Callable[[float], None] | None = None, cancel: Callable[[], bool] | None = None):
    """Run ffmpeg; feed raw frames on stdin if given; report progress (0..1)."""
    with open(log, "ab") as logf:
        if stdin_frames is not None:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=logf, **_NO_WINDOW)
            try:
                n = max(1, round(total_secs * FPS))
                for i, fr in enumerate(stdin_frames):
                    if cancel and cancel():
                        proc.kill()
                        raise Cancelled()
                    proc.stdin.write(fr)
                    if progress and i % 5 == 0:
                        progress(min(1.0, i / n))
                proc.stdin.close()
            except BrokenPipeError:
                pass
            rc = proc.wait()
        else:
            proc = subprocess.Popen(cmd + ["-progress", "pipe:1", "-nostats"], stdout=subprocess.PIPE,
                                    stderr=logf, text=True, errors="replace", **_NO_WINDOW)
            for line in proc.stdout:
                if cancel and cancel():
                    proc.kill()
                    proc.wait()
                    raise Cancelled()
                if line.startswith("out_time_us=") and progress and total_secs > 0:
                    try:
                        progress(min(1.0, int(line.split("=")[1]) / 1e6 / total_secs))
                    except ValueError:
                        pass
            rc = proc.wait()
    if rc != 0:
        tail = ""
        try:
            with open(log, encoding="utf-8", errors="replace") as f:
                tail = f.read()[-600:]
        except Exception:
            pass
        raise RuntimeError(f"ffmpeg failed (code {rc}).\n{tail}")


# near-lossless intermediate segments; the final encode happens once at the end
_SEG_V = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "8", "-pix_fmt", "yuv420p", "-r", str(FPS)]
_SEG_A = ["-c:a", "aac", "-b:a", "256k", "-ar", "48000", "-ac", "2"]


def _vfade(fin: float, fout: float, length: float) -> str:
    f = []
    if fin > 0:
        f.append(f"fade=t=in:st=0:d={fin:.3f}")
    if fout > 0:
        f.append(f"fade=t=out:st={max(0.0, length - fout):.3f}:d={fout:.3f}")
    return ",".join(f) or "null"


def _afade(fin: float, fout: float, length: float) -> str:
    f = []
    if fin > 0:
        f.append(f"afade=t=in:st=0:d={fin:.3f}")
    if fout > 0:
        f.append(f"afade=t=out:st={max(0.0, length - fout):.3f}:d={fout:.3f}")
    return ("," + ",".join(f)) if f else ""


def render_segment(item: Item, s: ReelSettings, index: int, out: str, ov_png: str, log: str,
                   progress=None, cancel=None, fades: tuple[float, float] = (0.0, 0.0)) -> None:
    ff = ffmpeg_exe()
    w, h = dims(s)
    fin, fout = fades
    L = item.length
    if item.kind == "photo":
        cmd = [ff, "-y", "-hide_banner", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}",
               "-r", str(FPS), "-i", "-", "-i", ov_png,
               "-f", "lavfi", "-t", f"{L:.3f}", "-i", "anullsrc=r=48000:cl=stereo",
               "-filter_complex", f"{blur_filter(item, w, h, '0:v', 'pb')};[pb]{_vfade(fin, fout, L)}[pf];"
                                  "[pf][1:v]overlay=0:0:format=auto,format=yuv420p[v]",
               "-map", "[v]", "-map", "2:a", *_SEG_V, *_SEG_A, "-t", f"{L:.3f}", out]
        _run(cmd, log, stdin_frames=photo_frames(item, s, index), total_secs=L,
             progress=progress, cancel=cancel)
        return
    # video clip
    vf = picture_filter(item, s.framing, w, h, FPS)
    vf += (f";{blur_filter(item, w, h, 'pic', 'pb')};[pb]{_vfade(fin, fout, L)}[pf];"
           "[pf][1:v]overlay=0:0:format=auto,format=yuv420p[v]")
    if uses_clip_sound(item, s):
        vol = s.clip_volume_pct / 100
        vf += (f";[0:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
               f"volume={vol:.3f}")
        bl = bleeps_in(item)
        if bl:
            cond = "+".join(f"between(t\\,{a:.3f}\\,{b:.3f})" for a, b in bl)
            vf += (f",volume='if({cond}\\,0\\,1)':eval=frame[ca];"
                   f"sine=frequency={BLEEP_HZ}:sample_rate=48000:duration={L:.3f},"
                   f"aformat=sample_fmts=fltp:channel_layouts=stereo,"
                   f"volume='if({cond}\\,{BLEEP_VOL}\\,0)':eval=frame[bp];"
                   f"[ca][bp]amix=inputs=2:duration=longest:normalize=0")
        vf += f"{_afade(fin, fout, L)},apad[a]"
        amap = "[a]"
    else:
        amap = "2:a"
    cmd = [ff, "-y", "-hide_banner", "-ss", f"{item.start:.3f}", "-t", f"{L:.3f}", "-i", item.path,
           "-loop", "1", "-i", ov_png,
           "-f", "lavfi", "-t", f"{L:.3f}", "-i", "anullsrc=r=48000:cl=stereo",
           "-filter_complex", vf, "-map", "[v]", "-map", amap, *_SEG_V, *_SEG_A,
           "-t", f"{L:.3f}", out]
    _run(cmd, log, total_secs=L, progress=progress, cancel=cancel)


def render_end_card(s: ReelSettings, out: str, log: str, progress=None, cancel=None) -> None:
    w, h = dims(s)
    n = max(1, round(s.end_secs * FPS))
    frames = (end_card_frame(i / FPS, s.end_secs, s.logo_path, w, h).tobytes() for i in range(n))
    cmd = [ffmpeg_exe(), "-y", "-hide_banner", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}",
           "-r", str(FPS), "-i", "-", "-f", "lavfi", "-t", f"{s.end_secs:.3f}", "-i", "anullsrc=r=48000:cl=stereo",
           "-map", "0:v", "-map", "1:a", *_SEG_V, *_SEG_A, "-t", f"{s.end_secs:.3f}", out]
    _run(cmd, log, stdin_frames=frames, total_secs=s.end_secs, progress=progress, cancel=cancel)


def total_length(items: list[Item], s: ReelSettings) -> float:
    if not items:
        return 0.0
    return sum(i.length for i in items) + (s.end_secs if s.end_card else 0.0) + disclaimer_secs(s)


def make_reel(items: list[Item], s: ReelSettings, out_path: str,
              progress: Callable[[float, str], None] | None = None,
              cancel: Callable[[], bool] | None = None) -> str:
    """Render the whole reel to out_path (MP4). progress(fraction, message)."""
    if not items:
        raise ValueError("Add some photos or clips first.")
    total = total_length(items, s)
    tmp = tempfile.mkdtemp(prefix="tbs_reel_")
    log = os.path.join(tmp, "ffmpeg.log")
    # time budget: segments ~60%, final encode ~40%
    seg_share = 0.6
    done_secs = 0.0

    def rep(frac, msg):
        if progress:
            progress(max(0.0, min(1.0, frac)), msg)

    try:
        ov_png = os.path.join(tmp, "overlay.png")
        overlay(s).save(ov_png)
        segs = []
        dsecs = disclaimer_secs(s)
        disc = os.path.join(tmp, "disclaimer.mp4")
        if dsecs:
            rep(0.0, "Making the disclaimer")
            render_still(disclaimer_card(s.disclaimer, s, *dims(s)), dsecs, disc, log, cancel)
            done_secs += dsecs
            if s.disclaimer_at != "end":
                segs.append(disc)
        for i, it in enumerate(items):
            seg = os.path.join(tmp, f"seg{i:03d}.mp4")
            label = f"Preparing {i + 1} of {len(items)}: {it.name}"
            rep(seg_share * done_secs / total, label)
            base = done_secs
            render_segment(it, s, i, seg, ov_png, log,
                           progress=lambda f, b=base, L=it.length, m=label: rep(seg_share * (b + f * L) / total, m),
                           cancel=cancel, fades=fades_for(items, s, i))
            segs.append(seg)
            done_secs += it.length
        if dsecs and s.disclaimer_at == "end":
            segs.append(disc)
        if s.end_card:
            seg = os.path.join(tmp, "end.mp4")
            rep(seg_share * done_secs / total, "Making the TBS end card")
            render_end_card(s, seg, log, cancel=cancel)
            segs.append(seg)

        # join, add music, final high-quality encode
        cmd = [ffmpeg_exe(), "-y", "-hide_banner"]
        for seg in segs:
            cmd += ["-i", seg]
        n = len(segs)
        fc = "".join(f"[{i}:v][{i}:a]" for i in range(n)) + f"concat=n={n}:v=1:a=1[v][ca]"
        music = s.music_path if s.music_path and os.path.exists(s.music_path) else ""
        if music:
            cmd += ["-stream_loop", "-1", "-i", music]
            fade = min(2.0, total / 4)
            fin = FADE if s.fade_in else 0.3
            mv = s.music_volume_pct / 100
            fc += (f";[{n}:a]atrim=0:{total:.3f},aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
                   f"volume={mv:.3f},afade=t=in:d={fin:.3f},afade=t=out:st={max(0, total - fade):.3f}:d={fade:.3f}[m];"
                   f"[ca][m]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,alimiter=limit=0.95[a]")
        else:
            fc += ";[ca]anull[a]"
        cmd += ["-filter_complex", fc, "-map", "[v]", "-map", "[a]",
                "-c:v", "libx264", "-preset", "slow", "-crf", "16", "-profile:v", "high", "-level:v", "4.2",
                "-pix_fmt", "yuv420p", "-r", str(FPS), "-g", str(FPS * 2), "-bf", "2",
                "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
                "-c:a", "aac", "-b:a", "256k", "-ar", "48000", "-ac", "2",
                "-movflags", "+faststart", "-t", f"{total:.3f}", out_path]
        rep(seg_share, "Putting the reel together (high quality)")
        _run(cmd, log, total_secs=total,
             progress=lambda f: rep(seg_share + (1 - seg_share) * f, "Putting the reel together (high quality)"),
             cancel=cancel)
        rep(1.0, "Done")
        return out_path
    except Cancelled:
        try:
            os.remove(out_path)
        except OSError:
            pass
        raise
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def preview_image(item: Item | None, s: ReelSettings, w: int, h: int) -> Image.Image:
    """What a frame of this item looks like in the reel, at w x h."""
    pic = None
    if item is not None:
        try:
            pic = load_photo(item.path, 1600) if item.kind == "photo" else video_frame(item.path, item.start + 0.05)
        except Exception:
            pic = None
    img = place_picture(pic, item, s.framing, w, h) if pic is not None else Image.new("RGB", (w, h), (70, 78, 86))
    return Image.alpha_composite(img.convert("RGBA"), overlay(s, w, h)).convert("RGB")
