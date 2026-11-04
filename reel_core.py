"""Core reel-making logic for TBS Reel (no GUI code here).

A reel is a list of items (photos and video clips) turned into one 1080 x 1350
(4:5) MP4 at 30 fps (H.264 + AAC), using the TBS reel frame:
  * TBS logo top left, date and credit top right,
  * a dark panel at the bottom with the headline (Bangla and/or English),
    where any part of the headline can be highlighted in yellow,
  * the teal strip along the bottom edge.

How it is made:
  1. Each item becomes a short high-quality segment: photos get a gentle zoom,
     clips are trimmed; the frame is laid on top.
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

import fribidi_loader  # noqa: F401  (must come before PIL.ImageFont: Bangla shaping)
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps, features

import fontlib

W, H, FPS = 1080, 1350, 30
PHOTO_EXT = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp", ".bmp"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".mts", ".m2ts", ".3gp", ".wmv"}
AUDIO_EXT = {".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus", ".wma"}

# ---- the TBS reel frame (measured from the design, 1080 x 1350)
LOGO_BOX = (64, 69, 78)              # x, y, size of the round logo
TOP_RIGHT = 1016                     # right edge of the date and credit
DATE_BASELINE, DATE_SIZE = 102, 30      # sized so November Condensed matches the design
CREDIT_BASELINE, CREDIT_SIZE = 131, 24
HEAD_CENTER_Y = 1056                 # middle of the headline block
HEAD_SIZE = 72                       # headline font size at 100% (shrinks if the text is long)
HEAD_LINE = 1.32                     # line spacing, as a multiple of the font size
HEAD_MAX_W = 960                     # widest a headline line may be
HEAD_MAX_LINES = 3
WHITE, YELLOW = (255, 255, 255, 255), (255, 246, 54, 255)
TOP_WIDE_LIMIT = 1.15                # pictures wider than this (w/h) go "wide at the top" in Auto

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
    photo_secs: float = 3.0       # default length for each new photo
    framing: str = "auto"         # auto / fill / top / fit
    motion: str = "zoom"          # "zoom" = gentle zoom on photos (alternating in / out); "none"
    logo_on: bool = True
    logo_path: str = ""           # empty = built-in TBS logo
    date_text: str = ""           # e.g. "25 OCTOBER | 2026" (empty = today)
    credit_text: str = "Video: "  # e.g. "Video: Shariar Gaalib" or "ছবি: ..."
    headline: list = field(default_factory=list)   # [[text, highlighted, bold], ...]
    bangla_font: str = ""         # font family for Bangla letters (empty = best available)
    english_font: str = ""        # font family for English letters
    top_font: str = ""            # font family for the date and credit (English letters)
    head_size_pct: float = 100.0  # headline size
    music_path: str = ""
    music_volume_pct: float = 70.0
    clip_sound: bool = True          # keep the sound recorded with video clips
    clip_volume_pct: float = 100.0
    end_card: bool = False           # TBS logo end card
    end_secs: float = 2.5
    out_dir: str = ""


@dataclass
class Item:
    path: str
    kind: str                     # "photo" or "video"
    length: float = 3.0           # seconds on screen
    start: float = 0.0            # video only: where to start in the clip
    duration: float = 0.0         # video only: full clip length
    has_audio: bool = False       # video only
    meta: dict = field(default_factory=dict)

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
    Bangla and English letters look the same size whatever fonts are picked."""
    try:
        f = ImageFont.truetype(path, 100, layout_engine=LAYOUT)
        box = f.getbbox("\u0995" if bangla else "H")
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


def headline_font(size: int, bangla: bool = True, bold: bool = True, s: "ReelSettings | None" = None):
    s = s or ReelSettings()
    return get_font(s.bangla_font if bangla else s.english_font, bangla, bold, size)[0]


NEUTRAL = re.compile(r"[^A-Za-z\u0980-\u09FF]")


def script_runs(text: str) -> list[tuple[str, bool]]:
    """Split text into Bangla / English pieces. Spaces, digits and punctuation
    go with the letters before them (or after, at the start)."""
    out: list[list] = []
    pending = ""
    for ch in text:
        if NEUTRAL.match(ch) or ch in "\u200c\u200d":
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


@lru_cache(maxsize=1)
def _frame_bg() -> Image.Image:
    return Image.open(resource("assets", "frame.png")).convert("RGBA")


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
    lines = _tokens(s.headline)
    if not any(not p[0].isspace() for ln in lines for p in ln):
        return HEAD_SIZE, []
    size = round(HEAD_SIZE * max(40.0, min(160.0, s.head_size_pct)) / 100)
    while True:
        width_of = lambda ln, sz=size: _spans_width(_spans(ln, s, sz))  # noqa: E731
        wrapped = _wrap(lines, width_of, HEAD_MAX_W)
        if (len(wrapped) <= HEAD_MAX_LINES and all(width_of(ln) <= HEAD_MAX_W for ln in wrapped)) or size <= 30:
            return size, [_spans(ln, s, size) for ln in wrapped]
        size -= 3


def overlay(s: ReelSettings, w: int = W, h: int = H) -> Image.Image:
    """The TBS frame as a transparent layer: dark panel, teal strip, logo,
    date + credit, and the headline with its yellow / bold parts."""
    layer = _frame_bg().copy()
    if s.logo_on:
        logo = _logo(s.logo_path)
        if logo is not None:
            x, y, size = LOGO_BOX
            m = logo.resize((size, max(1, round(logo.height * size / logo.width))), Image.LANCZOS)
            sh = Image.new("RGBA", layer.size, (0, 0, 0, 0))
            shadow = Image.new("RGBA", m.size, (0, 0, 0, 0))
            shadow.putalpha(m.getchannel("A").point(lambda a: a * 70 // 255))
            sh.alpha_composite(shadow, (x + 1, y + 2))
            layer.alpha_composite(sh.filter(ImageFilter.GaussianBlur(3)))
            layer.alpha_composite(m, (x, y))
    for text, base, size in (((s.date_text or today_text()).strip(), DATE_BASELINE, DATE_SIZE),
                             (s.credit_text.strip(), CREDIT_BASELINE, CREDIT_SIZE)):
        if text and not text.endswith(":"):
            spans = _spans([(text, False, False)], s, size, top=True)
            _draw_spans(layer, TOP_RIGHT - _spans_width(spans), base, spans, shadow=True)
    size, lines = layout_headline(s)
    if lines:
        pitch = round(size * HEAD_LINE)
        asc = max(sp.font.getmetrics()[0] for ln in lines for sp in ln)
        top = HEAD_CENTER_Y - (pitch * (len(lines) - 1) + asc) / 2   # centre the block
        for i, spans in enumerate(lines):
            _draw_spans(layer, (W - _spans_width(spans)) / 2, top + asc + i * pitch, spans)
    if (w, h) != (W, H):
        layer = layer.resize((w, h), Image.LANCZOS)
    return layer


def end_card_frame(t: float, total: float, logo_path: str = "") -> Image.Image:
    """TBS end card: the logo and wordmark on dark teal-navy, easing in."""
    img = Image.new("RGB", (W, H), (14, 20, 30))
    p = min(1.0, t / 0.6)
    ease = 1 - (1 - p) ** 3
    logo = _logo(logo_path)
    if logo is not None:
        lw = round(W * (0.34 + 0.04 * ease))
        m = logo.resize((lw, round(logo.height * lw / logo.width)), Image.LANCZOS)
        m.putalpha(m.getchannel("A").point(lambda a: round(a * ease)))
        img.paste(m, ((W - lw) // 2, round(H * 0.42 - m.height / 2)), m)
        try:
            bm = Image.open(resource("assets", "brand.png")).convert("RGBA")
            bw = round(W * 0.70)
            b = bm.resize((bw, round(bm.height * bw / bm.width)), Image.LANCZOS)
            b.putalpha(b.getchannel("A").point(lambda a: round(a * ease)))
            img.paste(b, ((W - bw) // 2, round(H * 0.42 + m.height / 2 + 60)), b)
        except Exception:
            pass
    fade_out = max(0.0, min(1.0, (total - t) / 0.4))  # gentle fade to black at the very end
    if fade_out < 1:
        img = img.point(lambda v: int(v * fade_out))
    return img


# ---------------------------------------------------------------- rendering
class Cancelled(Exception):
    pass


def _ease(x: float) -> float:
    return x * x * (3 - 2 * x)


def photo_frames(item: Item, s: ReelSettings, index: int):
    """Yield the frames of a photo with an optional gentle zoom (alternating in and out)."""
    n = max(1, round(item.length * FPS))
    zoom = 1.08 if s.motion == "zoom" else 1.0
    bw, bh = round(W * zoom), round(H * zoom)
    big = frame_picture(load_photo(item.path), s.framing, bw, bh)
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
        yield big.resize((W, H), Image.BICUBIC, box=(x0, y0, x0 + cw, y0 + ch)).tobytes()


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


def render_segment(item: Item, s: ReelSettings, index: int, out: str, ov_png: str, log: str,
                   progress=None, cancel=None) -> None:
    ff = ffmpeg_exe()
    if item.kind == "photo":
        cmd = [ff, "-y", "-hide_banner", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}",
               "-r", str(FPS), "-i", "-", "-i", ov_png,
               "-f", "lavfi", "-t", f"{item.length:.3f}", "-i", "anullsrc=r=48000:cl=stereo",
               "-filter_complex", "[0:v][1:v]overlay=0:0:format=auto,format=yuv420p[v]",
               "-map", "[v]", "-map", "2:a", *_SEG_V, *_SEG_A, "-t", f"{item.length:.3f}", out]
        _run(cmd, log, stdin_frames=photo_frames(item, s, index), total_secs=item.length,
             progress=progress, cancel=cancel)
        return
    # video clip
    mode = resolve_framing(s.framing, item.meta.get("width", 0), item.meta.get("height", 0))
    if mode == "fill":
        vf = f"[0:v]fps={FPS},scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1[pic]"
    else:
        y = "0" if mode == "top" else "(H-h)/2"
        vf = (f"[0:v]fps={FPS},split[a][b];"
              f"[a]scale={W // 8}:{H // 8}:force_original_aspect_ratio=increase,crop={W // 8}:{H // 8},"
              f"gblur=sigma=6,scale={W}:{H},eq=brightness=-0.09[bg];"
              f"[b]scale={W}:{H}:force_original_aspect_ratio=decrease:force_divisible_by=2[fg];"
              f"[bg][fg]overlay=(W-w)/2:{y},setsar=1[pic]")
    vf += ";[pic][1:v]overlay=0:0:format=auto,format=yuv420p[v]"
    use_clip_audio = s.clip_sound and item.has_audio
    if use_clip_audio:
        vol = s.clip_volume_pct / 100
        vf += (f";[0:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
               f"volume={vol:.3f},apad[a]")
        amap = "[a]"
    else:
        amap = "2:a"
    cmd = [ff, "-y", "-hide_banner", "-ss", f"{item.start:.3f}", "-t", f"{item.length:.3f}", "-i", item.path,
           "-loop", "1", "-i", ov_png,
           "-f", "lavfi", "-t", f"{item.length:.3f}", "-i", "anullsrc=r=48000:cl=stereo",
           "-filter_complex", vf, "-map", "[v]", "-map", amap, *_SEG_V, *_SEG_A,
           "-t", f"{item.length:.3f}", out]
    _run(cmd, log, total_secs=item.length, progress=progress, cancel=cancel)


def render_end_card(s: ReelSettings, out: str, log: str, progress=None, cancel=None) -> None:
    n = max(1, round(s.end_secs * FPS))
    frames = (end_card_frame(i / FPS, s.end_secs, s.logo_path).tobytes() for i in range(n))
    cmd = [ffmpeg_exe(), "-y", "-hide_banner", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}",
           "-r", str(FPS), "-i", "-", "-f", "lavfi", "-t", f"{s.end_secs:.3f}", "-i", "anullsrc=r=48000:cl=stereo",
           "-map", "0:v", "-map", "1:a", *_SEG_V, *_SEG_A, "-t", f"{s.end_secs:.3f}", out]
    _run(cmd, log, stdin_frames=frames, total_secs=s.end_secs, progress=progress, cancel=cancel)


def total_length(items: list[Item], s: ReelSettings) -> float:
    return sum(i.length for i in items) + (s.end_secs if s.end_card and items else 0.0)


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
        for i, it in enumerate(items):
            seg = os.path.join(tmp, f"seg{i:03d}.mp4")
            label = f"Preparing {i + 1} of {len(items)}: {it.name}"
            rep(seg_share * done_secs / total, label)
            base = done_secs
            render_segment(it, s, i, seg, ov_png, log,
                           progress=lambda f, b=base, L=it.length, m=label: rep(seg_share * (b + f * L) / total, m),
                           cancel=cancel)
            segs.append(seg)
            done_secs += it.length
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
            mv = s.music_volume_pct / 100
            fc += (f";[{n}:a]atrim=0:{total:.3f},aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
                   f"volume={mv:.3f},afade=t=in:d=0.3,afade=t=out:st={max(0, total - fade):.3f}:d={fade:.3f}[m];"
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
    img = frame_picture(pic, s.framing, w, h) if pic is not None else Image.new("RGB", (w, h), (70, 78, 86))
    return Image.alpha_composite(img.convert("RGBA"), overlay(s, w, h)).convert("RGB")
