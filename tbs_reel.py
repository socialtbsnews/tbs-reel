"""TBS Reel - desktop app.

1. Drop photos and video clips onto the window (or the MEDIA box), or click + Add.
2. Edit on the timeline: click to move the playhead, drag a clip to move it,
   drag its edge to trim it, right-click for more. Split, Delete, Mute and Bleep
   work at the playhead.
3. On the preview: drag the picture to pick the subject (wheel to zoom), drag the
   headline to move it, and draw blur boxes over anything sensitive.
4. Type the date, credit and headline. Select words and click Bold or Yellow.
5. Click Make reel. You get a 9:16 (1080 x 1920) or 4:5 (1080 x 1350) MP4.
"""
from __future__ import annotations

import copy
import json
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from dataclasses import asdict
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageTk

import fontlib
import reel_core as rc
from reel_core import (AUDIO_EXT, PHOTO_EXT, VIDEO_EXT, Cancelled, Item, ReelSettings, load_photo, make_item,
                       make_reel, overlay, resource, today_text, total_length, video_frame)

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    HAS_DND = True
except Exception:  # still works with the "Add" button
    HAS_DND = False

CONFIG = os.path.join(os.path.expanduser("~"), ".tbs_reel.json")
MEDIA_LIST = os.path.join(os.path.expanduser("~"), ".tbs_reel_media.json")
BG, PANEL, PANEL2, INK, MUTED, ACCENT = "#1B2128", "#232B33", "#2C3640", "#E6EAEE", "#9AA5B1", "#F97316"
YELLOW_UI, RED_UI, LINE = "#FFF636", "#E5484D", "#3A4651"
FONT = ("Segoe UI", 10)
SMALL = ("Segoe UI", 9)
BOLD = ("Segoe UI", 10, "bold")
if sys.platform.startswith("win"):
    TEXT_FONT = "Segoe UI"
elif sys.platform == "darwin":
    TEXT_FONT = "Helvetica Neue"
else:
    TEXT_FONT = "DejaVu Sans"
MAC = sys.platform == "darwin"
MEDIA_TYPES = [("Photos and videos", " ".join(f"*{e}" for e in sorted(PHOTO_EXT | VIDEO_EXT))),
               ("Videos", " ".join(f"*{e}" for e in sorted(VIDEO_EXT))),
               ("Photos", " ".join(f"*{e}" for e in sorted(PHOTO_EXT)))]
MUSIC_TYPES = [("Music", " ".join(f"*{e}" for e in sorted(AUDIO_EXT)))]
FONT_TYPES = [("Fonts", "*.ttf *.otf *.ttc *.TTF *.OTF")]
SIZES = {k: v["label"] for k, v in rc.LAYOUTS.items()}
FRAMING = {"auto": "Auto (best for each clip)", "fill": "Fill the frame (crops the sides)",
           "top": "Full width at the top", "fit": "Whole picture in the middle"}
MOTION = {"zoom": "Gentle zoom", "none": "Still"}
DISCLAIMER = {"": "None", **{k: v["label"] for k, v in rc.DISCLAIMERS.items()}}
DISC_AT = {"start": "At the start", "end": "At the end"}
PLAY_FPS = 15
# timeline drawing
TL_H, RULER, BY0, BY1, TX0 = 116, 20, 24, 108, 10
TILE_W, TILE_H, BIN_W = 100, 86, 330

_REGISTERED: set = set()


def use_font_file(path: str) -> bool:
    """Let this app's text boxes use a font file that is not installed (only for this app)."""
    if not path or path in _REGISTERED or not os.path.exists(path):
        return path in _REGISTERED
    ok = False
    try:
        import ctypes
        if sys.platform.startswith("win"):
            ok = ctypes.windll.gdi32.AddFontResourceExW(path, 0x10, 0) > 0     # FR_PRIVATE
        elif sys.platform == "darwin":
            cf = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
            ct = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreText.framework/CoreText")
            cf.CFURLCreateFromFileSystemRepresentation.restype = ctypes.c_void_p
            cf.CFURLCreateFromFileSystemRepresentation.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                                                   ctypes.c_long, ctypes.c_bool]
            ct.CTFontManagerRegisterFontsForURL.restype = ctypes.c_bool
            ct.CTFontManagerRegisterFontsForURL.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p]
            raw = path.encode("utf-8")
            url = cf.CFURLCreateFromFileSystemRepresentation(None, raw, len(raw), False)
            ok = bool(ct.CTFontManagerRegisterFontsForURL(url, 1, None))        # this app only
    except Exception:
        ok = False
    if ok:
        _REGISTERED.add(path)
    return ok


def load_settings() -> ReelSettings:
    s = ReelSettings()
    try:
        with open(CONFIG, encoding="utf-8") as f:
            for k, v in json.load(f).items():
                if hasattr(s, k):
                    setattr(s, k, v)
    except Exception:
        pass
    if s.layout not in rc.LAYOUTS:
        s.layout = "reel"
    return s


def save_settings(s: ReelSettings) -> None:
    try:
        with open(CONFIG, "w", encoding="utf-8") as f:
            json.dump(asdict(s), f, indent=2, ensure_ascii=False)
    except Exception:
        pass


def open_path(path: str, reveal: bool = False) -> None:
    try:
        if sys.platform.startswith("win"):
            if reveal:
                subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
            else:
                os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", path] if reveal else ["open", path])
        else:
            subprocess.Popen(["xdg-open", os.path.dirname(path) if reveal else path])
    except Exception:
        pass


def fmt_secs(t: float) -> str:
    t = max(0.0, round(t, 1))
    m, s = divmod(t, 60)
    return f"{int(m)}:{s:04.1f}" if m else f"{s:.1f}s"


def wheel_dir(e) -> int:
    if getattr(e, "num", 0) == 4:
        return 1
    if getattr(e, "num", 0) == 5:
        return -1
    return 1 if getattr(e, "delta", 0) > 0 else -1


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.s = load_settings()
        self.s.date_text = ""              # the date always starts as today
        self.items: list[Item] = []
        self.cur = -1
        self.pos = 0.0                     # playhead inside the selected clip (seconds)
        self.undo_stack: list[tuple[list[Item], int]] = []
        self.bin: list[Item] = []          # the MEDIA box: everything imported, ready to reuse
        self.bin_sel: set = set()
        self.src_cache: dict = {}
        self.thumbs: dict = {}
        self.thumb_pending: set = set()
        self.thumb_stack: list = []
        self.thumb_lock = threading.Lock()
        self.thumb_event = threading.Event()
        self.preview_img = None
        self.pv = (0, 0, 1, 1)             # where the preview sits on its canvas (x, y, w, h)
        self.redraw_pending = False
        self.busy = False
        self.cancel_flag = False
        self.last_out = ""
        self.playing: dict | None = None
        self.tl_drag: dict | None = None
        self.bin_drag: dict | None = None
        self.pv_drag: dict | None = None
        self.tl_insert = None              # where a dragged clip would land
        self.pps = None                    # timeline pixels per second (None = fit)
        self.bleep_mark = None             # (path, time in the file) while a bleep is being marked
        self.blur_mode = False
        self.blur_sel = -1
        self.show_card = False
        self.audio_cache: dict = {}
        self._sound = None                 # the sounddevice module, once loaded (None = not tried)
        self._sound_warned = False
        self.families = rc.families()

        root.title("TBS Reel")
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        root.geometry(f"{min(1440, sw - 40)}x{min(920, sh - 80)}")
        root.minsize(1120, 660)
        root.configure(bg=BG)
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TScale", background=BG)
        style.configure("Horizontal.TProgressbar", troughcolor=PANEL, background=ACCENT, bordercolor=BG,
                        lightcolor=ACCENT, darkcolor=ACCENT)
        root.bind_class("TCombobox", "<MouseWheel>", lambda e: "break")   # don't change a menu by scrolling

        self._build_header()
        self._build_right()
        main = tk.Frame(root, bg=BG)
        main.pack(side="left", fill="both", expand=True)
        self._build_bottom(main)
        self._build_left(main)
        self._build_centre(main)

        if HAS_DND:
            for w in (self.canvas, self.timeline, root):
                w.drop_target_register(DND_FILES)
                w.dnd_bind("<<Drop>>", self.on_drop)
            self.bin_canvas.drop_target_register(DND_FILES)
            self.bin_canvas.dnd_bind("<<Drop>>", self.on_bin_drop)
        root.bind_all("<Control-z>", lambda e: self.undo())
        root.bind_all("<Command-z>", lambda e: self.undo())
        for key, fn in (("<Delete>", self.delete_key), ("<BackSpace>", self.delete_key),
                        ("<space>", self.toggle_play), ("s", self.split), ("b", self.bleep), ("m", self.toggle_mute),
                        ("<Left>", lambda: self.nudge(-1 / rc.FPS)), ("<Right>", lambda: self.nudge(1 / rc.FPS)),
                        ("<Shift-Left>", lambda: self.nudge(-1)), ("<Shift-Right>", lambda: self.nudge(1)),
                        ("<Home>", lambda: self.seek(0)), ("<Escape>", self.escape)):
            root.bind(key, lambda e, f=fn: None if self._typing() else (f(), "break")[1])

        for _ in range(2):
            threading.Thread(target=self._thumb_worker, daemon=True).start()
        self.refresh_music_label()
        self.update_all()
        self.set_status("Start by dropping photos or video clips onto this window, or click + Add."
                        if HAS_DND else "Start by clicking + Add photos / videos.")
        threading.Thread(target=self._scan_fonts, daemon=True).start()
        threading.Thread(target=self._load_bin, daemon=True).start()

    # ================================================================ layout
    def _build_header(self):
        head = tk.Frame(self.root, bg=BG)
        head.pack(side="top", fill="x", padx=16, pady=(8, 4))
        self._head_imgs = []
        try:
            for name, h in (("logo.png", 30), ("brand.png", 16)):
                im = Image.open(resource("assets", name)).convert("RGBA")
                im = im.resize((round(im.width * h / im.height), h), Image.LANCZOS)
                self._head_imgs.append(ImageTk.PhotoImage(im, master=self.root))
            tk.Label(head, image=self._head_imgs[0], bg=BG).pack(side="left")
            tk.Label(head, image=self._head_imgs[1], bg=BG).pack(side="left", padx=(10, 0))
            tk.Label(head, text="REEL", fg=ACCENT, bg=BG, font=("Segoe UI", 11, "bold")).pack(side="left", padx=10)
        except Exception:
            tk.Label(head, text="TBS Reel", fg=INK, bg=BG, font=("Segoe UI", 18, "bold")).pack(side="left")
        try:
            self._icon = ImageTk.PhotoImage(Image.open(resource("assets", "icon.png")).resize((64, 64), Image.LANCZOS),
                                            master=self.root)
            self.root.iconphoto(True, self._icon)
        except Exception:
            pass
        tk.Label(head, text="1  Add clips     2  Edit on the timeline     3  Type the text     4  Make reel",
                 fg=MUTED, bg=BG, font=FONT).pack(side="right")

    def _scroll_column(self, parent, width, side, padx):
        """A column that scrolls when the window is too short for everything in it."""
        outer = tk.Frame(parent, bg=BG, width=width)
        outer.pack(side=side, fill="y", padx=padx, pady=(0, 8))
        outer.pack_propagate(False)
        cv = tk.Canvas(outer, bg=BG, highlightthickness=0, bd=0)
        sb = ttk.Scrollbar(outer, orient="vertical", command=cv.yview)
        inner = tk.Frame(cv, bg=BG)
        win = cv.create_window(0, 0, window=inner, anchor="nw")
        cv.configure(yscrollcommand=sb.set)
        cv.pack(side="left", fill="both", expand=True)

        def update(_e=None):
            cv.configure(scrollregion=(0, 0, inner.winfo_reqwidth(), inner.winfo_reqheight()))
            need = inner.winfo_reqheight() > cv.winfo_height() + 2
            if need and not sb.winfo_ismapped():
                sb.pack(side="right", fill="y", before=cv)
            elif not need and sb.winfo_ismapped():
                sb.pack_forget()
                cv.yview_moveto(0)
            cv.itemconfigure(win, width=cv.winfo_width())

        def wheel(e):
            if inner.winfo_reqheight() > cv.winfo_height():
                cv.yview_scroll(-wheel_dir(e) * 2, "units")

        def enter(_e):
            for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                self.root.bind_all(seq, wheel)

        def leave(_e):
            for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                self.root.unbind_all(seq)

        inner.bind("<Configure>", update)
        cv.bind("<Configure>", update)
        outer.bind("<Enter>", enter)
        outer.bind("<Leave>", leave)
        return inner

    def _build_left(self, parent):
        left = self._scroll_column(parent, 320, "left", (16, 8))

        self._section(left, "TOP OF THE FRAME")
        r = tk.Frame(left, bg=BG); r.pack(fill="x", pady=2)
        tk.Label(r, text="Date", width=6, anchor="w", bg=BG, fg=INK, font=FONT).pack(side="left")
        self.date_var = tk.StringVar(self.root, value=today_text())
        self._entry(r, self.date_var).pack(side="left", fill="x", expand=True, ipady=3)
        self._btn(r, "Today", lambda: self.date_var.set(today_text())).pack(side="left", padx=(4, 0))
        r = tk.Frame(left, bg=BG); r.pack(fill="x", pady=2)
        tk.Label(r, text="Credit", width=6, anchor="w", bg=BG, fg=INK, font=FONT).pack(side="left")
        self.credit_var = tk.StringVar(self.root, value=self.s.credit_text)
        self._entry(r, self.credit_var).pack(side="left", fill="x", expand=True, ipady=3)
        for v in (self.date_var, self.credit_var):
            v.trace_add("write", lambda *_: self.look_changed())

        self._section(left, "HEADLINE")
        self.head = tk.Text(left, height=3, wrap="word", bg=PANEL, fg=INK, insertbackground=INK, relief="flat",
                            font=(TEXT_FONT, 13), undo=True, padx=6, pady=5, selectbackground="#3B5A78")
        self.head.tag_configure("hl", foreground=YELLOW_UI)
        self.ui_fonts = {"en": TEXT_FONT, "top": TEXT_FONT}
        self.head.pack(fill="x")
        self.head.bind("<<Modified>>", self.headline_edited)
        r = tk.Frame(left, bg=BG); r.pack(fill="x", pady=(5, 0))
        b = self._btn(r, "B  Bold", lambda: self.style_sel("b"))
        b.config(font=BOLD)
        b.pack(side="left")
        y = self._btn(r, "Yellow", lambda: self.style_sel("hl"))
        y.config(fg=YELLOW_UI)
        y.pack(side="left", padx=5)
        self._btn(r, "Plain", lambda: self.style_sel(None)).pack(side="left")
        r = tk.Frame(left, bg=BG); r.pack(fill="x", pady=(5, 0))
        tk.Label(r, text="Align", width=6, anchor="w", bg=BG, fg=INK, font=FONT).pack(side="left")
        self.align_btns = {}
        for key, text in (("left", "Left"), ("center", "Centre"), ("right", "Right")):
            ab = self._btn(r, text, lambda k=key: self.set_align(k))
            ab.config(padx=8)
            ab.pack(side="left", padx=(0, 3))
            self.align_btns[key] = ab
        self._btn(r, "Reset place", self.reset_head_pos).pack(side="right")
        tk.Label(left, text="Select words, then click Bold or Yellow. Enter starts a new line. "
                            "Drag the headline on the preview to move it.", bg=BG, fg=MUTED, font=SMALL,
                 justify="left", anchor="w", wraplength=290).pack(fill="x", pady=(3, 0))
        self.load_headline()
        self.show_align()

        self._section(left, "DARK SHADE AT THE BOTTOM")
        size, dark = rc.shade_of(self.s)
        self.shade_var = tk.DoubleVar(self.root, value=size)
        self.set_shade_size = self._slider(left, "Size", self.shade_var, 0, 60)
        self.dark_var = tk.DoubleVar(self.root, value=dark)
        self.set_shade_dark = self._slider(left, "Darkness", self.dark_var, 0, 100)

        self._section(left, "FONTS")
        self.en_menu = self._combo_row(left, "Headline", self.font_changed)
        self.top_menu = self._combo_row(left, "Date/credit", self.font_changed)
        self.hsize_var = tk.DoubleVar(self.root, value=self.s.head_size_pct)
        self._slider(left, "Size", self.hsize_var, 60, 140)
        r = tk.Frame(left, bg=BG); r.pack(fill="x", pady=(4, 0))
        self._btn(r, "Add fonts…", self.add_fonts).pack(side="left")
        self.font_note = tk.Label(r, text="Looking for fonts on this computer…", bg=BG, fg=MUTED,
                                  font=SMALL, anchor="w", justify="left", wraplength=190)
        self.font_note.pack(side="left", padx=8, fill="x")
        self.fill_font_menus()
        self.apply_ui_fonts()

    def _build_right(self):
        right = tk.Frame(self.root, bg=BG, width=300)
        right.pack(side="right", fill="y", padx=(8, 16), pady=(0, 8))
        right.pack_propagate(False)

        foot = tk.Frame(right, bg=BG)
        foot.pack(side="bottom", fill="x")
        self.total_lbl = tk.Label(foot, bg=BG, fg=INK, font=BOLD, anchor="w")
        self.total_lbl.pack(fill="x")
        self.status = tk.Label(foot, text="", bg=BG, fg=MUTED, font=FONT, anchor="w", justify="left",
                               wraplength=280)
        self.status.pack(fill="x", pady=(2, 4))
        self.bar = ttk.Progressbar(foot, mode="determinate", maximum=1000, style="Horizontal.TProgressbar")
        self.bar.pack(fill="x", pady=(0, 6))
        r = tk.Frame(foot, bg=BG); r.pack(fill="x")
        self.export_btn = tk.Button(r, text="Make reel", command=self.export, state="disabled",
                                    bg=ACCENT, fg="#1A0F06", activebackground=ACCENT, relief="flat",
                                    font=("Segoe UI", 12, "bold"), padx=12, pady=8)
        self.export_btn.pack(side="left", fill="x", expand=True)
        self.open_btn = self._btn(r, "Show video", self.open_out)
        self.open_btn.config(state="disabled")
        self.open_btn.pack(side="left", padx=(6, 0))

        holder = tk.Frame(right, bg=BG)
        holder.pack(side="top", fill="both", expand=True)
        body = self._scroll_column(holder, 300, "top", 0)
        body.master.master.pack_configure(fill="both", expand=True)

        self._section(body, "SIZE")
        self.size_menu = self._menu(body, "Size", SIZES, self.s.layout, self.layout_changed)

        self._section(body, "PICTURE")
        self.framing_menu = self._menu(body, "Framing", FRAMING, self.s.framing, self.look_changed)
        self.motion_menu = self._menu(body, "Photos", MOTION, self.s.motion, self.look_changed)
        r = tk.Frame(body, bg=BG); r.pack(fill="x", pady=2)
        tk.Label(r, text="Each photo", width=9, anchor="w", bg=BG, fg=INK, font=FONT).pack(side="left")
        self.photo_secs_var = tk.StringVar(self.root, value=f"{self.s.photo_secs:g}")
        self._spin(r, self.photo_secs_var, 1, 30, self.photo_secs_changed).pack(side="left")
        tk.Label(r, text="sec", bg=BG, fg=MUTED, font=FONT).pack(side="left", padx=4)
        self._btn(r, "Apply to all", self.apply_photo_secs).pack(side="right")

        self._section(body, "SOUND")
        r = tk.Frame(body, bg=BG); r.pack(fill="x", pady=2)
        self._btn(r, "Choose music…", self.choose_music).pack(side="left")
        self._btn(r, "No music", self.clear_music).pack(side="left", padx=6)
        self.music_lbl = tk.Label(body, bg=BG, fg=MUTED, font=SMALL, anchor="w", justify="left", wraplength=270)
        self.music_lbl.pack(fill="x")
        self.mvol_var = tk.DoubleVar(self.root, value=self.s.music_volume_pct)
        self._slider(body, "Music", self.mvol_var, 0, 100)
        self.clipsound_var = tk.BooleanVar(self.root, value=self.s.clip_sound)
        self._check(body, "Use the sound of the video clips", self.clipsound_var, self.look_changed).pack(anchor="w")
        self.cvol_var = tk.DoubleVar(self.root, value=self.s.clip_volume_pct)
        self._slider(body, "Clips", self.cvol_var, 0, 100)
        tk.Label(body, text="Untick to take the sound off every clip. To take it off one clip, "
                            "select it and click Mute.", bg=BG, fg=MUTED, font=SMALL, anchor="w",
                 justify="left", wraplength=270).pack(fill="x")

        self._section(body, "FADE TO BLACK")
        self.fin_var = tk.BooleanVar(self.root, value=self.s.fade_in)
        self._check(body, "Fade in from black at the start", self.fin_var, self.look_changed).pack(anchor="w")
        self.fout_var = tk.BooleanVar(self.root, value=self.s.fade_out)
        self._check(body, "Fade out to black at the end", self.fout_var, self.look_changed).pack(anchor="w")
        self.fbet_var = tk.BooleanVar(self.root, value=self.s.fade_between)
        self._check(body, "Fade through black between clips", self.fbet_var, self.look_changed).pack(anchor="w")

        self._section(body, "DISCLAIMER")
        self.disc_menu = self._menu(body, "Card", DISCLAIMER, self.s.disclaimer, self.disclaimer_changed)
        r = tk.Frame(body, bg=BG); r.pack(fill="x", pady=2)
        self.disc_at_menu = ttk.Combobox(r, state="readonly", font=FONT, values=list(DISC_AT.values()), width=12)
        self.disc_at_menu.set(DISC_AT.get(self.s.disclaimer_at, "At the start"))
        self.disc_at_menu.options = DISC_AT
        self.disc_at_menu.bind("<<ComboboxSelected>>", lambda e: self.disclaimer_changed())
        self.disc_at_menu.pack(side="left")
        self.disc_secs_var = tk.StringVar(self.root, value=f"{self.s.disclaimer_secs:g}")
        self._spin(r, self.disc_secs_var, 2, 20, self.disclaimer_secs_changed).pack(side="left", padx=(8, 0))
        tk.Label(r, text="sec", bg=BG, fg=MUTED, font=FONT).pack(side="left", padx=4)
        self._btn(body, "Show the card", self.preview_card).pack(anchor="w", pady=(2, 0))

        self._section(body, "LOGO AND ENDING")
        self.logo_var = tk.BooleanVar(self.root, value=self.s.logo_on)
        self._check(body, "Show TBS logo", self.logo_var, self.look_changed).pack(anchor="w")
        self.end_var = tk.BooleanVar(self.root, value=self.s.end_card)
        self._check(body, "TBS end card (2.5 seconds)", self.end_var, self.look_changed).pack(anchor="w")

    def _build_bottom(self, parent):
        bottom = tk.Frame(parent, bg=BG)
        bottom.pack(side="bottom", fill="x", padx=(16, 8), pady=(0, 8))

        # ---- MEDIA box: everything imported, ready to use again
        mb = tk.Frame(bottom, bg=BG, width=BIN_W)
        mb.pack(side="left", fill="y")
        mb.pack_propagate(False)
        r = tk.Frame(mb, bg=BG); r.pack(fill="x", pady=(6, 4))
        tk.Label(r, text="MEDIA", bg=BG, fg=MUTED, font=("Segoe UI", 8, "bold")).pack(side="left")
        self._btn(r, "+ Import", self.import_media).pack(side="left", padx=(8, 0))
        self.bin_add_btn = self._btn(r, "Add to timeline", self.bin_add_selected)
        self.bin_add_btn.pack(side="left", padx=4)
        wrap = tk.Frame(mb, bg=PANEL)
        wrap.pack(fill="both", expand=True)
        self.bin_canvas = tk.Canvas(wrap, bg=PANEL, height=TL_H, highlightthickness=0)
        bsb = ttk.Scrollbar(wrap, orient="vertical", command=self.bin_canvas.yview)
        self.bin_canvas.config(yscrollcommand=bsb.set)
        bsb.pack(side="right", fill="y")
        self.bin_canvas.pack(side="left", fill="both", expand=True)
        self.bin_canvas.bind("<Configure>", lambda e: self.draw_bin())
        self.bin_canvas.bind("<ButtonPress-1>", self.bin_press)
        self.bin_canvas.bind("<B1-Motion>", self.bin_motion)
        self.bin_canvas.bind("<ButtonRelease-1>", self.bin_release)
        self.bin_canvas.bind("<Double-Button-1>", self.bin_double)
        for seq in ("<Button-3>",) + (("<Button-2>",) if MAC else ()):
            self.bin_canvas.bind(seq, self.bin_menu)
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.bin_canvas.bind(seq, lambda e: self.bin_canvas.yview_scroll(-wheel_dir(e), "units"))

        # ---- timeline
        tl = tk.Frame(bottom, bg=BG)
        tl.pack(side="left", fill="both", expand=True, padx=(12, 0))
        r = tk.Frame(tl, bg=BG); r.pack(fill="x", pady=(6, 4))
        add = self._btn(r, "+ Add photos / videos", self.choose_media)
        add.config(bg=PANEL2, font=BOLD)
        add.pack(side="left")
        tk.Label(r, text="Timeline", bg=BG, fg=MUTED, font=SMALL).pack(side="left", padx=(12, 2))
        for text, fn in (("−", lambda: self.tl_zoom(1 / 1.5)), ("Fit", lambda: self.tl_zoom(None)),
                         ("+", lambda: self.tl_zoom(1.5))):
            zb = self._btn(r, text, fn)
            zb.config(padx=8)
            zb.pack(side="left", padx=1)
        self._btn(r, "Clear all", self.clear).pack(side="right")
        self.undo_btn = self._btn(r, "↶ Undo", self.undo)
        self.undo_btn.pack(side="right", padx=6)
        self.timeline = tk.Canvas(tl, bg=PANEL, height=TL_H, highlightthickness=0)
        self.timeline.pack(fill="x")
        self.tl_scroll = ttk.Scrollbar(tl, orient="horizontal", command=self.timeline.xview)
        self.timeline.config(xscrollcommand=self.tl_scroll.set)
        self.tl_scroll.pack(fill="x")
        self.timeline.bind("<ButtonPress-1>", self.tl_press)
        self.timeline.bind("<B1-Motion>", self.tl_motion)
        self.timeline.bind("<ButtonRelease-1>", self.tl_release)
        self.timeline.bind("<Motion>", self.tl_hover)
        self.timeline.bind("<Configure>", lambda e: self.draw_timeline())
        for seq in ("<Button-3>",) + (("<Button-2>",) if MAC else ()):
            self.timeline.bind(seq, self.tl_menu)
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.timeline.bind(seq, self.tl_wheel)
        self.timeline.bind("<Control-MouseWheel>", lambda e: self.tl_zoom(1.25 if wheel_dir(e) > 0 else 0.8))

    def _build_centre(self, parent):
        mid = tk.Frame(parent, bg=BG)
        mid.pack(side="left", fill="both", expand=True, padx=8)

        ed = tk.Frame(mid, bg=BG)
        ed.pack(side="bottom", fill="x", pady=(6, 0))
        r = tk.Frame(ed, bg=BG); r.pack(fill="x")
        self.play_btn = self._btn(r, "▶ Play", self.toggle_play)
        self.play_btn.config(width=8)
        self.play_btn.pack(side="left")
        self.pos_var = tk.DoubleVar(self.root, value=0)
        self.scrub = ttk.Scale(r, from_=0, to=1, variable=self.pos_var, command=self.on_scrub)
        self.scrub.pack(side="left", fill="x", expand=True, padx=8)
        self.pos_lbl = tk.Label(r, bg=BG, fg=MUTED, font=FONT, width=15, anchor="e")
        self.pos_lbl.pack(side="left")

        r = tk.Frame(ed, bg=BG); r.pack(fill="x", pady=(4, 0))
        self.edit_btns = []
        for text, fn in (("✂ Split", self.split), ("Trim start", self.cut_start), ("Trim end", self.cut_end),
                         ("✕ Delete", self.delete)):
            b = self._btn(r, text, fn)
            b.config(padx=8)
            b.pack(side="left", padx=(0, 4))
            self.edit_btns.append(b)
        tk.Frame(r, bg=LINE, width=1, height=22).pack(side="left", padx=6)
        self.mute_btn = self._btn(r, "Mute", self.toggle_mute)
        self.mute_btn.config(padx=8, width=7)
        self.mute_btn.pack(side="left", padx=(0, 4))
        self.bleep_btn = self._btn(r, "Bleep from here", self.bleep)
        self.bleep_btn.config(padx=8, width=14)
        self.bleep_btn.pack(side="left", padx=(0, 4))
        self.secs_frame = tk.Frame(r, bg=BG)
        tk.Label(self.secs_frame, text="On screen", bg=BG, fg=INK, font=FONT).pack(side="left", padx=(6, 0))
        self.len_var = tk.StringVar(self.root, value="")
        self.len_spin = self._spin(self.secs_frame, self.len_var, 0.5, 60, self.photo_len_changed)
        self.len_spin.pack(side="left", padx=4)
        tk.Label(self.secs_frame, text="sec", bg=BG, fg=MUTED, font=FONT).pack(side="left")

        r = tk.Frame(ed, bg=BG); r.pack(fill="x", pady=(4, 0))
        tk.Label(r, text="Zoom", bg=BG, fg=INK, font=FONT).pack(side="left")
        self.zoom_var = tk.DoubleVar(self.root, value=100)
        self.zoom_scale = ttk.Scale(r, from_=100, to=400, variable=self.zoom_var, command=self.on_zoom, length=150)
        self.zoom_scale.pack(side="left", padx=4)
        self.zoom_lbl = tk.Label(r, text="", bg=BG, fg=MUTED, font=FONT, width=5, anchor="w")
        self.zoom_lbl.pack(side="left")
        self.reset_pic_btn = self._btn(r, "Reset picture", self.reset_picture)
        self.reset_pic_btn.config(padx=8)
        self.reset_pic_btn.pack(side="left", padx=(0, 4))
        self.blur_btn = self._btn(r, "▢ Blur box", self.toggle_blur_mode)
        self.blur_btn.config(padx=8)
        self.blur_btn.pack(side="left", padx=(0, 4))
        self.item_lbl = tk.Label(r, bg=BG, fg=MUTED, font=SMALL, anchor="e")
        self.item_lbl.pack(side="right", fill="x", expand=True)

        self.canvas = tk.Canvas(mid, bg=BG, highlightthickness=0)
        self.canvas.pack(side="top", fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda e: self.refresh())
        self.canvas.bind("<ButtonPress-1>", self.pv_press)
        self.canvas.bind("<B1-Motion>", self.pv_motion)
        self.canvas.bind("<ButtonRelease-1>", self.pv_release)
        self.canvas.bind("<Motion>", self.pv_hover)
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.canvas.bind(seq, self.pv_wheel)
        for seq in ("<Button-3>",) + (("<Button-2>",) if MAC else ()):
            self.canvas.bind(seq, self.pv_menu)

    # ================================================================ widget helpers
    def _btn(self, parent, text, cmd):
        return tk.Button(parent, text=text, command=cmd, bg=PANEL, fg=INK, activebackground=ACCENT,
                         activeforeground="#1A0F06", relief="flat", font=FONT, padx=10, pady=4, cursor="hand2",
                         disabledforeground="#5C6670")

    def _entry(self, parent, var):
        e = tk.Entry(parent, textvariable=var, bg=PANEL, fg=INK, insertbackground=INK, relief="flat",
                     font=(TEXT_FONT, 12))
        self.entries = getattr(self, "entries", []) + [e]
        return e

    def _check(self, parent, text, var, cmd):
        return tk.Checkbutton(parent, text=text, variable=var, command=cmd, bg=BG, fg=INK, font=FONT,
                              selectcolor=PANEL, activebackground=BG, activeforeground=INK)

    def _section(self, parent, title):
        tk.Label(parent, text=title, bg=BG, fg=MUTED, font=("Segoe UI", 8, "bold"),
                 anchor="w").pack(fill="x", pady=(10, 2))

    def _menu(self, parent, label, options: dict, current: str, cmd):
        r = tk.Frame(parent, bg=BG); r.pack(fill="x", pady=2)
        tk.Label(r, text=label, width=8, anchor="w", bg=BG, fg=INK, font=FONT).pack(side="left")
        m = ttk.Combobox(r, state="readonly", font=FONT, values=list(options.values()))
        m.set(options.get(current, next(iter(options.values()))))
        m.bind("<<ComboboxSelected>>", lambda e: cmd())
        m.pack(side="left", fill="x", expand=True)
        m.options = options
        return m

    def _combo_row(self, parent, label, cmd):
        r = tk.Frame(parent, bg=BG); r.pack(fill="x", pady=2)
        tk.Label(r, text=label, width=10, anchor="w", bg=BG, fg=INK, font=FONT).pack(side="left")
        m = ttk.Combobox(r, state="readonly", font=FONT)
        m.bind("<<ComboboxSelected>>", lambda e: cmd())
        m.pack(side="left", fill="x", expand=True)
        return m

    def _spin(self, parent, var, lo, hi, cmd):
        sp = tk.Spinbox(parent, from_=lo, to=hi, increment=0.5, textvariable=var, width=5, font=FONT,
                        bg=PANEL, fg=INK, buttonbackground=PANEL, insertbackground=INK, relief="flat",
                        command=cmd)
        sp.bind("<KeyRelease>", lambda e: cmd())
        sp.bind("<FocusOut>", lambda e: cmd())
        return sp

    def _slider(self, parent, label, var, lo, hi):
        """A labelled slider; returns a function that sets it (and its % label) from code."""
        r = tk.Frame(parent, bg=BG); r.pack(fill="x", pady=1)
        tk.Label(r, text=label, width=8, anchor="w", bg=BG, fg=INK, font=FONT).pack(side="left")
        val = tk.Label(r, width=5, anchor="e", bg=BG, fg=MUTED, font=FONT)

        def changed(_v=None):
            val.config(text=f"{var.get():.0f}%")
            self.look_changed()

        ttk.Scale(r, from_=lo, to=hi, variable=var, command=changed).pack(side="left", fill="x", expand=True)
        val.pack(side="left")
        val.config(text=f"{var.get():.0f}%")

        def setter(v):
            var.set(v)
            val.config(text=f"{v:.0f}%")
        return setter

    @staticmethod
    def _key(menu) -> str:
        return next(k for k, v in menu.options.items() if v == menu.get())

    def _typing(self) -> bool:
        w = self.root.focus_get()
        return isinstance(w, (tk.Entry, tk.Text, tk.Spinbox, ttk.Combobox))

    def save(self):
        if getattr(self, "_save_job", None):
            self.root.after_cancel(self._save_job)
        self._save_job = self.root.after(500, lambda: save_settings(self.s))

    def set_status(self, text):
        self.status.config(text=text)

    # ================================================================ settings
    def look_changed(self):
        s = self.s
        if not hasattr(self, "end_var"):
            return
        s.framing = self._key(self.framing_menu)
        s.motion = self._key(self.motion_menu)
        s.logo_on = bool(self.logo_var.get())
        s.music_volume_pct = round(self.mvol_var.get())
        s.clip_sound = bool(self.clipsound_var.get())
        s.clip_volume_pct = round(self.cvol_var.get())
        s.fade_in, s.fade_out = bool(self.fin_var.get()), bool(self.fout_var.get())
        s.fade_between = bool(self.fbet_var.get())
        s.end_card = bool(self.end_var.get())
        if hasattr(self, "hsize_var"):
            s.head_size_pct = round(self.hsize_var.get())
            s.date_text = self.date_var.get()
            s.credit_text = self.credit_var.get()
            s.shade = dict(s.shade or {})
            s.shade[s.layout] = [round(self.shade_var.get()), round(self.dark_var.get())]
        self.save()
        self.update_total()
        self.draw_timeline()
        self.refresh()

    def layout_changed(self):
        self.s.layout = self._key(self.size_menu)
        size, dark = rc.shade_of(self.s)
        self.set_shade_size(size)
        self.set_shade_dark(dark)
        self.look_changed()
        w, h = rc.dims(self.s)
        self.set_status(f"Making a {w} x {h} reel.")

    def disclaimer_changed(self):
        self.s.disclaimer = self._key(self.disc_menu)
        self.s.disclaimer_at = self._key(self.disc_at_menu)
        self.save()
        self.update_total()
        if self.s.disclaimer:
            self.preview_card()
        else:
            self.show_card = False
            self.refresh()

    def disclaimer_secs_changed(self):
        try:
            v = float(self.disc_secs_var.get())
        except ValueError:
            return
        if 1 <= v <= 30:
            self.s.disclaimer_secs = v
            self.save()
            self.update_total()

    def preview_card(self):
        if not self.s.disclaimer:
            self.set_status("Pick a disclaimer card first.")
            return
        self.stop_play()
        self.show_card = True
        self.set_status(f"This card shows for {self.s.disclaimer_secs:g} seconds "
                        f"{'at the start' if self.s.disclaimer_at == 'start' else 'at the end'}. "
                        "Click the timeline to go back to your clips.")
        self.refresh()

    def set_align(self, key):
        self.s.head_align = key
        self.show_align()
        self.save()
        self.refresh()

    def show_align(self):
        for k, b in self.align_btns.items():
            on = k == self.s.head_align
            b.config(bg=ACCENT if on else PANEL, fg="#1A0F06" if on else INK)

    def reset_head_pos(self):
        hp = dict(self.s.head_pos or {})
        hp.pop(self.s.layout, None)
        self.s.head_pos = hp
        self.save()
        self.refresh()

    # ---- fonts
    def _scan_fonts(self):
        try:
            fams = fontlib.scan_all(include_system=True)
        except Exception:
            fams = fontlib.built_in()
        self.root.after(0, self._fonts_found, fams)

    def _fonts_found(self, fams):
        self.families = fams
        rc.set_families(fams)
        self.fill_font_menus()
        self.apply_ui_fonts()
        mine = [n for n, f in fams.items() if f.source != "built in"]
        found = [n for n in ("FF Meta Serif Pro", "November Condensed LCG") if n in fams]
        self.font_note.config(text=("Found " + " and ".join(found)) if found else
                              (f"{len(mine)} fonts from this computer" if mine else "Using the built-in fonts"))
        missing = [n for n in ("FF Meta Serif Pro", "November Condensed LCG") if n not in fams]
        if missing:
            self.set_status("Not found on this computer: " + ", ".join(missing) +
                            ". Install them, or click Add fonts… and pick the font files.")
        self._ov_key = None
        self.refresh()

    def fill_font_menus(self):
        fams = self.families
        en = sorted([n for n, f in fams.items() if f.latin], key=str.lower)
        self.en_menu.config(values=en)
        self.top_menu.config(values=en)
        for menu, attr, prefs in ((self.en_menu, "english_font", None),
                                  (self.top_menu, "top_font", fontlib.PREFERRED_TOP)):
            want = getattr(self.s, attr)
            if want not in en:
                want = fontlib.pick_default(fams, False, prefs)
            menu.set(want)

    def font_changed(self):
        self.s.english_font = self.en_menu.get()
        self.s.top_font = self.top_menu.get()
        self.apply_ui_fonts()
        self.save()
        self.refresh()

    def add_fonts(self):
        files = filedialog.askopenfilenames(title="Choose font files (.ttf or .otf)", filetypes=FONT_TYPES)
        if not files:
            return
        added = fontlib.add_font_files(list(files))
        if not added:
            messagebox.showwarning("TBS Reel", "Those files could not be read as fonts.")
            return
        self.font_note.config(text="Adding fonts…")
        threading.Thread(target=self._scan_fonts, daemon=True).start()
        self.set_status(f"Added {len(added)} font file{'s' if len(added) != 1 else ''}. "
                        "Pick them in the font menus.")

    # ---- headline
    def headline_runs(self):
        """The headline as [[text, yellow, bold], ...] from the text box."""
        runs: list[list] = []
        text = self.head.get("1.0", "end-1c")
        for i, ch in enumerate(text):
            tags = self.head.tag_names(f"1.0+{i}c")
            st = ("hl" in tags, "b" in tags)
            if runs and (runs[-1][1], runs[-1][2]) == st:
                runs[-1][0] += ch
            else:
                runs.append([ch, st[0], st[1]])
        return runs

    def load_headline(self):
        self.head.delete("1.0", "end")
        for r in self.s.headline or []:
            r = list(r) + [False, False]
            tags = tuple(t for t, on in (("hl", r[1]), ("b", r[2])) if on)
            self.head.insert("end", r[0], tags)
        self.head.edit_modified(False)

    def headline_edited(self, _e=None):
        if not self.head.edit_modified():
            return
        self.head.edit_modified(False)
        if getattr(self, "_head_job", None):
            self.root.after_cancel(self._head_job)
        self._head_job = self.root.after(200, self._headline_commit)

    def apply_ui_fonts(self):
        """Show the chosen fonts in the typing boxes too."""
        fams = self.families
        for key, menu in (("en", self.en_menu), ("top", self.top_menu)):
            f = fams.get(menu.get())
            name = TEXT_FONT
            if f is not None:
                if f.source == "computer":
                    name = f.name                           # installed: the system already knows it
                elif use_font_file(f.regular):
                    use_font_file(f.bold)
                    name = f.name
            self.ui_fonts[key] = name
        en = self.ui_fonts["en"]
        self.head.config(font=(en, 14))
        self.head.tag_configure("f_b", font=(en, 14, "bold"))
        self.head.tag_raise("hl")
        for e in getattr(self, "entries", []):
            e.config(font=(self.ui_fonts["top"], 13))
        self.retag_fonts()

    def retag_fonts(self):
        """Show the bold parts of the headline in bold in the text box."""
        h = self.head
        h.tag_remove("f_b", "1.0", "end")
        for a, b in zip(*[iter(h.tag_ranges("b"))] * 2):
            h.tag_add("f_b", a, b)

    def _headline_commit(self):
        self.retag_fonts()
        self.s.headline = self.headline_runs()
        self.save()
        self.refresh()

    def style_sel(self, tag):
        try:
            a, b = self.head.index("sel.first"), self.head.index("sel.last")
        except tk.TclError:
            self.set_status("First select (highlight with the mouse) the words in the headline you want to change.")
            return
        if tag is None:
            self.head.tag_remove("hl", a, b)
            self.head.tag_remove("b", a, b)
        else:
            # if all of the selection already has the style, take it off; otherwise put it on
            n = len(self.head.get(a, b))
            on = all(tag in self.head.tag_names(f"{a}+{i}c")
                     for i in range(n) if not self.head.get(f"{a}+{i}c").isspace())
            (self.head.tag_remove if on else self.head.tag_add)(tag, a, b)
        self._headline_commit()

    # ---- photos length, music
    def photo_secs_changed(self):
        try:
            v = float(self.photo_secs_var.get())
        except ValueError:
            return
        if 0.5 <= v <= 60:
            self.s.photo_secs = v
            self.save()

    def apply_photo_secs(self):
        self.photo_secs_changed()
        self.snapshot()
        for it in self.items:
            if it.kind == "photo":
                it.length = self.s.photo_secs
        self.update_all()

    def choose_music(self):
        p = filedialog.askopenfilename(title="Choose background music", filetypes=MUSIC_TYPES)
        if p:
            self.s.music_path = p
            self.save()
            self.refresh_music_label()

    def clear_music(self):
        self.s.music_path = ""
        self.save()
        self.refresh_music_label()

    def refresh_music_label(self):
        p = self.s.music_path
        if p and os.path.exists(p):
            self.music_lbl.config(text=f"♪ {os.path.basename(p)} (repeats if shorter, fades out at the end)")
        else:
            self.music_lbl.config(text="No music")

    # ================================================================ time on the timeline
    def starts(self):
        st, t = [], 0.0
        for it in self.items:
            st.append(t)
            t += it.length
        return st, t

    def global_t(self) -> float:
        if not (0 <= self.cur < len(self.items)):
            return 0.0
        return self.starts()[0][self.cur] + self.pos

    def locate(self, T: float):
        st, total = self.starts()
        if not self.items:
            return -1, 0.0
        T = max(0.0, min(total, T))
        for i, it in enumerate(self.items):
            if T < st[i] + it.length or i == len(self.items) - 1:
                return i, max(0.0, min(it.length, T - st[i]))
        return -1, 0.0

    def seek(self, T: float, delay: int = 40):
        """Move the playhead to T seconds into the reel."""
        self.show_card = False
        i, p = self.locate(T)
        changed = i != self.cur
        self.cur, self.pos = i, p
        if changed:
            self.blur_sel = -1
            self.update_item_bar()
        else:
            self.update_pos_label()
        self.draw_timeline()
        self.refresh(delay)

    # ================================================================ items and editing
    def snapshot(self):
        self.undo_stack.append((copy.deepcopy(self.items), self.cur))
        del self.undo_stack[:-60]
        self.undo_btn.config(state="normal")

    def undo(self):
        if self.root.focus_get() is self.head:
            return  # the headline box has its own undo
        if not self.undo_stack:
            self.set_status("Nothing to undo.")
            return
        self.stop_play()
        self.items, self.cur = self.undo_stack.pop()
        self.pos = 0.0
        self.blur_sel = -1
        self.update_all()
        self.set_status("Undone.")

    def current(self) -> Item | None:
        return self.items[self.cur] if 0 <= self.cur < len(self.items) else None

    def select(self, i):
        self.stop_play()
        if 0 <= i < len(self.items):
            self.seek(self.starts()[0][i])

    def move(self, d):
        i = self.cur
        if 0 <= i < len(self.items) and 0 <= i + d < len(self.items):
            self.snapshot()
            self.items[i], self.items[i + d] = self.items[i + d], self.items[i]
            self.cur = i + d
            self.update_all()

    def delete_key(self):
        if self.blur_sel >= 0:
            self.delete_blur()
        else:
            self.delete()

    def delete(self):
        it = self.current()
        if not it:
            return
        self.stop_play()
        self.snapshot()
        self.items.pop(self.cur)
        self.cur = min(self.cur, len(self.items) - 1)
        self.pos = 0.0
        self.update_all()
        self.set_status(f"Deleted {it.name}. Click Undo to bring it back.")

    def duplicate(self):
        it = self.current()
        if it:
            self.snapshot()
            self.items.insert(self.cur + 1, copy.deepcopy(it))
            self.cur += 1
            self.pos = 0.0
            self.update_all()

    def clear(self):
        if self.items and messagebox.askyesno("TBS Reel", "Remove all clips from the timeline?\n"
                                                          "(They stay in the MEDIA box.)"):
            self.stop_play()
            self.snapshot()
            self.items, self.cur = [], -1
            self.update_all()

    def _edit_ok(self, it) -> bool:
        if it is None:
            self.set_status("Add a clip and click it on the timeline first.")
            return False
        if not (0.15 <= self.pos <= it.length - 0.15):
            self.set_status("Move the playhead into the clip first (not right at its start or end).")
            return False
        return True

    def split(self):
        it = self.current()
        if not self._edit_ok(it):
            return
        self.stop_play()
        self.snapshot()
        p = round(self.pos, 2)
        second = copy.deepcopy(it)
        if it.kind == "video":
            second.start = round(it.start + p, 2)
        second.length = round(it.length - p, 2)
        it.length = p
        self.items.insert(self.cur + 1, second)
        self.cur += 1
        self.pos = 0.0
        self.update_all()
        self.set_status("Split into two. Click a part and press Delete to drop it.")

    def cut_start(self):
        it = self.current()
        if not self._edit_ok(it):
            return
        self.stop_play()
        self.snapshot()
        p = round(self.pos, 2)
        if it.kind == "video":
            it.start = round(it.start + p, 2)
        it.length = round(it.length - p, 2)
        self.pos = 0.0
        self.update_all()
        self.set_status("Trimmed the start.")

    def cut_end(self):
        it = self.current()
        if not self._edit_ok(it):
            return
        self.stop_play()
        self.snapshot()
        it.length = round(self.pos, 2)
        self.update_all()
        self.set_status("Trimmed the end.")

    def toggle_mute(self):
        it = self.current()
        if not it or it.kind != "video" or not it.has_audio:
            self.set_status("Select a video clip that has sound.")
            return
        self.snapshot()
        it.meta["mute"] = not it.meta.get("mute")
        self.update_item_bar()
        self.draw_timeline()
        self.set_status("This clip's sound is off." if it.meta["mute"] else "This clip's sound is back on.")

    def bleep(self):
        """First press: the bleep starts at the playhead. Second press: it ends there.
        Press inside a bleep to remove it. Works while playing too."""
        it = self.current()
        if not it or it.kind != "video" or not it.has_audio:
            self.set_status("Bleeps go on video clips that have sound.")
            return
        ft = round(it.start + self.pos, 3)
        bl = it.meta.get("bleeps") or []
        hit = [b for b in bl if b[0] - 0.02 <= ft <= b[1] + 0.02]
        if self.bleep_mark is None and hit:
            self.snapshot()
            self.current().meta["bleeps"] = [b for b in bl if b not in hit]
            self.set_status("Bleep removed.")
        elif self.bleep_mark is None:
            self.bleep_mark = (it.path, ft)
            self.set_status("The bleep starts here. Move to where the word ends (or keep playing) "
                            "and click End bleep here.")
        else:
            path, a = self.bleep_mark
            self.bleep_mark = None
            if path != it.path:
                self.set_status("A bleep has to start and end in the same clip. Try again.")
            else:
                a, b = sorted((a, ft))
                if b - a < 0.12:
                    b = a + 0.5
                self.snapshot()
                bl = [list(x) for x in (self.current().meta.get("bleeps") or [])] + [[a, b]]
                bl.sort()
                merged: list[list] = []
                for x in bl:
                    if merged and x[0] <= merged[-1][1]:
                        merged[-1][1] = max(merged[-1][1], x[1])
                    else:
                        merged.append(x)
                self.current().meta["bleeps"] = merged
                self.set_status(f"Bleep added ({b - a:.1f}s). The word's sound is replaced by a beep.")
        self.update_edit_buttons()
        self.draw_timeline()

    def photo_len_changed(self):
        it = self.current()
        if not it or it.kind != "photo":
            return
        try:
            v = float(self.len_var.get())
        except ValueError:
            return
        if 0.5 <= v <= 60 and abs(v - it.length) > 1e-6:
            it.length = v
            self.pos = min(self.pos, v)
            self.update_total()
            self.draw_timeline()

    def on_scrub(self, _v=None):
        if not self.items:
            return
        self.stop_play()
        self.seek(self.pos_var.get(), delay=110)

    def nudge(self, d):
        if self.items:
            self.stop_play()
            self.seek(self.global_t() + d)

    def escape(self):
        self.bleep_mark = None
        self.blur_mode = False
        self.blur_sel = -1
        self.update_edit_buttons()
        self.draw_timeline()
        self.refresh()

    # ---- picking the subject (zoom and move) and blur boxes
    def _ensure_subject(self, it: Item):
        if not rc.has_subject(it):
            it.meta.update(zoom=1.0, cx=0.5, cy=0.5)

    def on_zoom(self, _v=None):
        it = self.current()
        if not it:
            return
        if not getattr(self, "_zoom_snap", False):
            self.snapshot()
            self._zoom_snap = True
            self.root.after(800, lambda: setattr(self, "_zoom_snap", False))
        self._ensure_subject(it)
        it.meta["zoom"] = round(self.zoom_var.get() / 100, 3)
        self.zoom_lbl.config(text=f"{self.zoom_var.get():.0f}%")
        self.refresh()

    def reset_picture(self):
        it = self.current()
        if it and (rc.has_subject(it) or it.meta.get("blurs")):
            self.snapshot()
            for k in ("zoom", "cx", "cy"):
                it.meta.pop(k, None)
            self.update_item_bar()
            self.refresh()
            self.set_status("The picture is back to the normal framing (blur boxes kept).")

    def toggle_blur_mode(self):
        if not self.current():
            self.set_status("Click a clip on the timeline first.")
            return
        self.stop_play()
        self.blur_mode = not self.blur_mode
        self.update_edit_buttons()
        self.set_status("Drag a box over the face or thing to hide. It stays blurred for the whole clip "
                        "(split the clip to blur only part of it)." if self.blur_mode else "")

    def delete_blur(self):
        it = self.current()
        if it and 0 <= self.blur_sel < len(it.meta.get("blurs") or []):
            self.snapshot()
            self._blurs().pop(self.blur_sel)
            self.blur_sel = -1
            self.refresh()
            self.set_status("Blur box removed.")

    # ================================================================ refresh
    def update_all(self):
        self.update_total()
        self.update_item_bar()
        self.draw_timeline()
        self.refresh()
        self.undo_btn.config(state="normal" if self.undo_stack else "disabled")

    def update_total(self):
        n = len(self.items)
        if n:
            self.total_lbl.config(text=f"Reel length: {fmt_secs(total_length(self.items, self.s))}  "
                                       f"({n} clip{'s' if n != 1 else ''})")
            if not self.busy:
                self.export_btn.config(state="normal")
        else:
            self.total_lbl.config(text="Reel length: 0s")
            self.export_btn.config(state="disabled")
        self.scrub.config(to=max(0.1, self.starts()[1]))

    def update_pos_label(self):
        _st, total = self.starts()
        self.pos_lbl.config(text=f"{fmt_secs(self.global_t())} / {fmt_secs(total)}" if self.items else "")
        if self.playing is None:
            self.pos_var.set(self.global_t())
        self.update_edit_buttons()

    def update_edit_buttons(self):
        it = self.current()
        has = it is not None
        sound = has and it.kind == "video" and it.has_audio
        for b in self.edit_btns + [self.reset_pic_btn, self.blur_btn]:
            b.config(state="normal" if has else "disabled")
        self.play_btn.config(state="normal" if self.items else "disabled")
        self.mute_btn.config(state="normal" if sound else "disabled",
                             text="Unmute" if sound and it.meta.get("mute") else "Mute")
        if self.bleep_mark is not None:
            text, bg = "End bleep here", RED_UI
        elif sound and any(a - 0.02 <= it.start + self.pos <= b + 0.02 for a, b in it.meta.get("bleeps") or []):
            text, bg = "Remove bleep", PANEL
        else:
            text, bg = "Bleep from here", PANEL
        self.bleep_btn.config(state="normal" if sound else "disabled", text=text, bg=bg)
        self.blur_btn.config(bg=ACCENT if self.blur_mode else PANEL, fg="#1A0F06" if self.blur_mode else INK)
        if has:
            z = it.meta.get("zoom") or 1.0
            self.zoom_var.set(z * 100)
            self.zoom_lbl.config(text=f"{z * 100:.0f}%")
            self.zoom_scale.state(["!disabled"])
        else:
            self.zoom_scale.state(["disabled"])
            self.zoom_lbl.config(text="")

    def update_item_bar(self):
        it = self.current()
        if it is None:
            self.item_lbl.config(text="Click a clip on the timeline to edit it" if self.items else "")
            self.secs_frame.pack_forget()
        else:
            extra = []
            if it.meta.get("mute"):
                extra.append("muted")
            if it.meta.get("bleeps"):
                extra.append(f"{len(it.meta['bleeps'])} bleep{'s' if len(it.meta['bleeps']) > 1 else ''}")
            if it.meta.get("blurs"):
                extra.append(f"{len(it.meta['blurs'])} blur")
            self.item_lbl.config(text=f"Clip {self.cur + 1} of {len(self.items)}: {it.name}"
                                      + (f"  ({', '.join(extra)})" if extra else ""))
            if it.kind == "photo":
                self.len_var.set(f"{it.length:g}")
                self.secs_frame.pack(side="left")
            else:
                self.secs_frame.pack_forget()
        self.update_pos_label()

    # ================================================================ thumbnails (background)
    def thumb(self, path: str, kind: str, t: float, w: int, h: int):
        key = (path, round(t * 4) / 4 if kind == "video" else 0.0, w, h)
        if key in self.thumbs:
            return self.thumbs[key] or None
        if key not in self.thumb_pending:
            self.thumb_pending.add(key)
            with self.thumb_lock:
                self.thumb_stack.append(key)
                if len(self.thumb_stack) > 120:
                    self.thumb_pending.discard(self.thumb_stack.pop(0))
            self.thumb_event.set()
        return None

    def _thumb_worker(self):
        while True:
            self.thumb_event.wait()
            with self.thumb_lock:
                if not self.thumb_stack:
                    self.thumb_event.clear()
                    continue
                key = self.thumb_stack.pop()
            path, t, w, h = key
            try:
                src = load_photo(path, 400) if rc.kind_of(path) == "photo" else video_frame(path, t)
                pic = rc._cover(src, w, h) if src is not None else None
            except Exception:
                pic = None
            try:
                self.root.after(0, self._thumb_done, key, pic)
            except Exception:
                return

    def _thumb_done(self, key, pic):
        self.thumb_pending.discard(key)
        if len(self.thumbs) > 800:
            self.thumbs.clear()
        self.thumbs[key] = ImageTk.PhotoImage(pic, master=self.root) if pic is not None else False
        if not getattr(self, "_thumb_redraw", False):
            self._thumb_redraw = True

            def go():
                self._thumb_redraw = False
                self.draw_timeline()
                self.draw_bin()
            self.root.after(60, go)

    # ================================================================ timeline
    def _pps(self) -> float:
        if self.tl_drag and self.tl_drag.get("pps"):
            return self.tl_drag["pps"]
        if self.pps:
            return self.pps
        total = self.starts()[1]
        cw = max(200, self.timeline.winfo_width())
        return max(4.0, min(160.0, (cw - 2 * TX0) / max(1.0, total)))

    def tl_zoom(self, f):
        if f is None:
            self.pps = None
        else:
            self.pps = max(2.0, min(400.0, self._pps() * f))
        self.draw_timeline()
        self._scroll_to_playhead()

    def _x(self, t: float) -> float:
        return TX0 + t * self._pps()

    def draw_timeline(self):
        c = self.timeline
        c.delete("all")
        cw = max(200, c.winfo_width())
        if not self.items:
            c.create_text(14, TL_H // 2, anchor="w", fill=MUTED, font=FONT,
                          text="Your timeline is empty. Drop photos or videos here, drag them from MEDIA, "
                               "or click + Add.")
            c.config(scrollregion=(0, 0, cw, TL_H))
            return
        pps = self._pps()
        st, total = self.starts()
        full = 2 * TX0 + total * pps
        c.config(scrollregion=(0, 0, max(full, cw), TL_H))
        vis0, vis1 = c.canvasx(0) - 60, c.canvasx(cw) + 60
        # ruler
        step = next((s for s in (0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300) if s * pps >= 56), 600)
        c.create_rectangle(0, 0, max(full, cw), RULER, fill=BG, outline="")
        k = 0
        while k * step <= total + 1e-6:
            x = self._x(k * step)
            if vis0 <= x <= vis1:
                c.create_line(x, RULER - 7, x, RULER, fill=MUTED)
                c.create_text(x + 3, 2, anchor="nw", fill=MUTED, font=("Segoe UI", 8), text=fmt_secs(k * step))
            xm = self._x((k + 0.5) * step)
            if (k + 0.5) * step <= total and vis0 <= xm <= vis1:
                c.create_line(xm, RULER - 4, xm, RULER, fill=LINE)
            k += 1
        th = BY1 - BY0 - 4
        w_, h_ = rc.dims(self.s)
        tw = max(20, round(th * w_ / h_))
        for i, it in enumerate(self.items):
            x0, x1 = self._x(st[i]), self._x(st[i] + it.length)
            if x1 < vis0 or x0 > vis1:
                continue
            sel = i == self.cur
            c.create_rectangle(x0 + 1, BY0, x1 - 1, BY1, fill=PANEL2, outline="")
            # filmstrip
            x = x0 + 2
            while x < x1 - 3:
                wpx = int(min(tw, x1 - 3 - x))
                if wpx < 6:
                    break
                if vis0 <= x + wpx and x <= vis1:
                    t = it.start + ((x - x0) + wpx / 2) / pps if it.kind == "video" else 0
                    img = self.thumb(it.path, it.kind, t, wpx, th)
                    if img is not None:
                        c.create_image(x, BY0 + 2, image=img, anchor="nw")
                x += tw
            # fades
            fin, fout = rc.fades_for(self.items, self.s, i)
            if fin:
                c.create_polygon(x0 + 1, BY0, x0 + 1 + fin * pps, BY0, x0 + 1, BY1, fill="#000000",
                                 stipple="gray50", outline="")
            if fout:
                c.create_polygon(x1 - 1, BY0, x1 - 1 - fout * pps, BY0, x1 - 1, BY1, fill="#000000",
                                 stipple="gray50", outline="")
            # bleeps
            for a, b in rc.bleeps_in(it):
                c.create_rectangle(x0 + a * pps, BY1 - 14, max(x0 + a * pps + 3, x0 + b * pps), BY1 - 2,
                                   fill=RED_UI, outline="")
            for bd in rc.blur_list(it):
                ba = max(0.0, (bd["from"] if bd["from"] is not None else it.start) - it.start)
                bb = min(it.length, (bd["to"] if bd["to"] is not None else it.start + it.length) - it.start)
                if bb > ba:
                    c.create_rectangle(x0 + ba * pps, BY1 - 24, max(x0 + ba * pps + 3, x0 + bb * pps), BY1 - 17,
                                       fill="#3B82F6", outline="")
            if self.bleep_mark and self.bleep_mark[0] == it.path and it.start <= self.bleep_mark[1] <= it.start + it.length:
                bx = x0 + (self.bleep_mark[1] - it.start) * pps
                c.create_line(bx, BY0, bx, BY1, fill=RED_UI, width=2, dash=(3, 2))
            # label
            if x1 - x0 > 34:
                bits = [f"{i + 1}", fmt_secs(it.length)]
                if it.meta.get("mute"):
                    bits.append("muted")
                if it.meta.get("blurs"):
                    bits.append("blur")
                label = "  ".join(bits)
                room = int((x1 - x0 - 10) / 6.5)
                if room < len(label):
                    label = label[:max(1, room)]
                tid = c.create_text(x0 + 6, BY0 + 4, anchor="nw", fill=INK, font=("Segoe UI", 8, "bold"), text=label)
                bb = c.bbox(tid)
                if bb:
                    c.create_rectangle(bb[0] - 3, bb[1] - 1, bb[2] + 3, bb[3] + 1, fill="#111820", outline="")
                    c.tag_raise(tid)
            c.create_rectangle(x0 + 1, BY0, x1 - 1, BY1, outline=ACCENT if sel else LINE, width=3 if sel else 1)
        if self.tl_insert is not None:
            xi = self._x(st[self.tl_insert] if self.tl_insert < len(st) else total)
            c.create_line(xi, BY0 - 4, xi, BY1 + 4, fill=YELLOW_UI, width=3)
        self._draw_playhead()

    def _draw_playhead(self):
        c = self.timeline
        c.delete("ph")
        if not self.items:
            return
        x = self._x(self.global_t())
        c.create_line(x, 0, x, TL_H, fill=YELLOW_UI, width=2, tags="ph")
        c.create_polygon(x - 6, 0, x + 6, 0, x, 9, fill=YELLOW_UI, outline="", tags="ph")

    def _scroll_to_playhead(self):
        c = self.timeline
        st, total = self.starts()
        full = 2 * TX0 + total * self._pps()
        cw = c.winfo_width()
        if full <= cw:
            return
        x = self._x(self.global_t())
        if not (c.canvasx(0) + 20 <= x <= c.canvasx(cw) - 20):
            c.xview_moveto(max(0.0, (x - cw * 0.3) / full))
            self.draw_timeline()

    def _tl_t(self, ex) -> float:
        return max(0.0, (self.timeline.canvasx(ex) - TX0) / self._pps())

    def _block_at(self, ex):
        st, total = self.starts()
        t = (self.timeline.canvasx(ex) - TX0) / self._pps()
        for i, it in enumerate(self.items):
            if st[i] <= t < st[i] + it.length:
                return i
        return None

    def _edge_at(self, ex, ey):
        """(clip index, "l" or "r") when the pointer is on the edge of a clip."""
        if not (BY0 <= ey <= BY1):
            return None
        st, _ = self.starts()
        x = self.timeline.canvasx(ex)
        best = None
        for i, it in enumerate(self.items):
            for side, t in (("l", st[i]), ("r", st[i] + it.length)):
                d = abs(self._x(t) - x)
                if d <= 6 and (best is None or d < best[0] or (d == best[0] and side == "r" and x < self._x(t))):
                    best = (d, i, side)
        if best is None:
            return None
        _d, i, side = best
        # between two clips: the left one's end wins when the pointer is left of the line
        if side == "l" and i > 0 and x < self._x(st[i]):
            return (i - 1, "r")
        return (i, side)

    def _insert_index(self, ex) -> int:
        st, _ = self.starts()
        t = (self.timeline.canvasx(ex) - TX0) / self._pps()
        n = 0
        for i, it in enumerate(self.items):
            if t > st[i] + it.length / 2:
                n = i + 1
        return n

    def tl_hover(self, e):
        if self.tl_drag:
            return
        edge = self._edge_at(e.x, e.y) if self.items else None
        self.timeline.config(cursor="sb_h_double_arrow" if edge else ("hand2" if self.items else ""))

    def tl_press(self, e):
        self.timeline.focus_set()
        if not self.items:
            self.choose_media()
            return
        self.stop_play()
        self.show_card = False
        edge = self._edge_at(e.x, e.y)
        if e.y < RULER:
            self.tl_drag = {"mode": "seek"}
            self.seek(self._tl_t(e.x), delay=90)
            return
        if edge:
            i, side = edge
            it = self.items[i]
            self.tl_drag = {"mode": "trim", "i": i, "side": side, "x": e.x, "start": it.start,
                            "len": it.length, "pps": self._pps(), "snap": False}
            return
        i = self._block_at(e.x)
        if i is None:
            self.seek(self._tl_t(e.x))
            return
        self.tl_drag = {"mode": "press", "i": i, "x": e.x}
        self.seek(self._tl_t(e.x), delay=90)

    def tl_motion(self, e):
        d = self.tl_drag
        if not d:
            return
        if d["mode"] == "seek":
            self.seek(self._tl_t(e.x), delay=90)
        elif d["mode"] == "trim":
            it = self.items[d["i"]]
            if not d["snap"]:
                self.snapshot()
                d["snap"] = True
            delta = (e.x - d["x"]) / d["pps"]
            if d["side"] == "r":
                hi = (it.duration - it.start) if it.kind == "video" else 60.0
                it.length = round(max(0.3, min(hi, d["len"] + delta)), 2)
                pos = it.length
            else:
                if it.kind == "video":
                    ns = max(0.0, min(d["start"] + d["len"] - 0.3, d["start"] + delta))
                    it.start = round(ns, 2)
                    it.length = round(d["len"] - (ns - d["start"]), 2)
                else:
                    it.length = round(max(0.5, min(60.0, d["len"] - delta)), 2)
                pos = 0.0
            self.cur, self.pos = d["i"], max(0.0, min(it.length, pos - 0.04))
            self.update_total()
            self.update_item_bar()
            self.draw_timeline()
            self.refresh(90)
        elif d["mode"] in ("press", "move"):
            if d["mode"] == "press" and abs(e.x - d["x"]) > 8:
                d["mode"] = "move"
                self.timeline.config(cursor="fleur")
            if d["mode"] == "move":
                self.tl_insert = self._insert_index(e.x)
                self.draw_timeline()

    def tl_release(self, e):
        d, self.tl_drag = self.tl_drag, None
        self.timeline.config(cursor="")
        if not d:
            return
        if d["mode"] == "move":
            a = d["i"]
            b = self._insert_index(e.x)
            self.tl_insert = None
            if b in (a, a + 1):
                self.draw_timeline()
                return
            self.snapshot()
            it = self.items.pop(a)
            if b > a:
                b -= 1
            self.items.insert(b, it)
            self.cur, self.pos = b, 0.0
            self.update_all()
            self.set_status(f"Moved {it.name} to place {b + 1}.")
        elif d["mode"] == "trim":
            self.update_all()
            it = self.items[d["i"]]
            self.set_status(f"Clip {d['i'] + 1} is now {fmt_secs(it.length)}.")

    def tl_wheel(self, e):
        if e.state & 0x0004:     # Control held: zoom
            self.tl_zoom(1.25 if wheel_dir(e) > 0 else 0.8)
            return
        self.timeline.xview_scroll(-wheel_dir(e) * 3, "units")
        self.draw_timeline()

    def tl_menu(self, e):
        if not self.items:
            return
        i = self._block_at(e.x)
        if i is None:
            return
        self.stop_play()
        self.seek(self._tl_t(e.x))
        it = self.items[i]
        m = tk.Menu(self.root, tearoff=0, bg=PANEL, fg=INK, activebackground=ACCENT, activeforeground="#1A0F06")
        m.add_command(label="Split here   (S)", command=self.split)
        m.add_command(label="Trim start to here", command=self.cut_start)
        m.add_command(label="Trim end to here", command=self.cut_end)
        m.add_separator()
        if it.kind == "video" and it.has_audio:
            m.add_command(label="Unmute" if it.meta.get("mute") else "Mute sound   (M)", command=self.toggle_mute)
            m.add_command(label=self.bleep_btn.cget("text") + "   (B)", command=self.bleep)
        m.add_command(label="Add a blur box", command=lambda: (self.blur_mode or self.toggle_blur_mode()))
        if rc.has_subject(it):
            m.add_command(label="Reset picture", command=self.reset_picture)
        m.add_separator()
        m.add_command(label="Duplicate", command=self.duplicate)
        m.add_command(label="Move left", command=lambda: self.move(-1))
        m.add_command(label="Move right", command=lambda: self.move(1))
        m.add_separator()
        m.add_command(label="Delete   (Del)", command=self.delete)
        try:
            m.tk_popup(e.x_root, e.y_root)
        finally:
            m.grab_release()

    # ================================================================ MEDIA box
    def _load_bin(self):
        try:
            with open(MEDIA_LIST, encoding="utf-8") as f:
                paths = [p for p in json.load(f) if isinstance(p, str) and os.path.exists(p)]
        except Exception:
            return
        items = [it for it in (make_item(p, self.s.photo_secs) for p in paths[-200:]) if it]
        self.root.after(0, self._bin_loaded, items)

    def _bin_loaded(self, items):
        have = {b.path for b in self.bin}
        self.bin = [it for it in items if it.path not in have] + self.bin
        self.draw_bin()

    def save_bin(self):
        try:
            with open(MEDIA_LIST, "w", encoding="utf-8") as f:
                json.dump([b.path for b in self.bin], f, ensure_ascii=False)
        except Exception:
            pass

    def _bin_cols(self):
        return max(1, (self.bin_canvas.winfo_width() - 6) // (TILE_W + 4))

    def draw_bin(self):
        c = self.bin_canvas
        c.delete("all")
        cw = max(100, c.winfo_width())
        if not self.bin:
            c.create_text(10, 10, anchor="nw", fill=MUTED, font=SMALL, width=cw - 20,
                          text="Drop photos and videos here to keep them handy. Double-click one, or drag it "
                               "onto the timeline, to use it (as many times as you like).")
            c.config(scrollregion=(0, 0, cw, 10))
            return
        cols = self._bin_cols()
        for k, it in enumerate(self.bin):
            r, col = divmod(k, cols)
            x, y = 4 + col * (TILE_W + 4), 4 + r * (TILE_H + 4)
            sel = k in self.bin_sel
            c.create_rectangle(x, y, x + TILE_W, y + TILE_H, fill=PANEL2, outline=ACCENT if sel else LINE,
                               width=2 if sel else 1)
            img = self.thumb(it.path, it.kind, it.start + min(1.0, it.length / 3) if it.kind == "video" else 0,
                             TILE_W - 8, 56)
            if img is not None:
                c.create_image(x + 4, y + 4, image=img, anchor="nw")
            badge = f"VIDEO {fmt_secs(it.duration)}" if it.kind == "video" else "PHOTO"
            tid = c.create_text(x + 7, y + 46, anchor="nw", fill=INK, font=("Segoe UI", 7, "bold"), text=badge)
            bb = c.bbox(tid)
            if bb:
                c.create_rectangle(bb[0] - 2, bb[1], bb[2] + 2, bb[3], fill="#111820", outline="")
                c.tag_raise(tid)
            name = it.name if len(it.name) <= 16 else it.name[:15] + "…"
            c.create_text(x + 5, y + 64, anchor="nw", fill=MUTED, font=("Segoe UI", 8), text=name)
        rows = (len(self.bin) + cols - 1) // cols
        c.config(scrollregion=(0, 0, cw, rows * (TILE_H + 4) + 4))

    def _bin_at(self, ex, ey):
        x, y = self.bin_canvas.canvasx(ex), self.bin_canvas.canvasy(ey)
        col, r = int((x - 4) // (TILE_W + 4)), int((y - 4) // (TILE_H + 4))
        if col < 0 or r < 0 or col >= self._bin_cols():
            return None
        k = r * self._bin_cols() + col
        return k if k < len(self.bin) else None

    def bin_press(self, e):
        k = self._bin_at(e.x, e.y)
        if k is None:
            self.bin_sel.clear()
        elif e.state & 0x0004 or e.state & 0x0008:       # Ctrl / Cmd: add or remove from the selection
            self.bin_sel ^= {k}
        elif e.state & 0x0001 and self.bin_sel:          # Shift: a range
            a = min(self.bin_sel)
            self.bin_sel = set(range(min(a, k), max(a, k) + 1))
        elif k not in self.bin_sel:
            self.bin_sel = {k}
        self.bin_drag = {"k": k, "x": e.x_root, "y": e.y_root, "moving": False} if k is not None else None
        self.draw_bin()

    def bin_motion(self, e):
        d = self.bin_drag
        if not d:
            return
        if not d["moving"] and abs(e.x_root - d["x"]) + abs(e.y_root - d["y"]) > 10:
            d["moving"] = True
            self.root.config(cursor="plus")
        if d["moving"]:
            w = self.root.winfo_containing(e.x_root, e.y_root)
            if w is self.timeline:
                self.tl_insert = self._insert_index(e.x_root - self.timeline.winfo_rootx()) if self.items else 0
            else:
                self.tl_insert = None
            self.draw_timeline()

    def bin_release(self, e):
        d, self.bin_drag = self.bin_drag, None
        self.root.config(cursor="")
        at, self.tl_insert = self.tl_insert, None
        if d and d["moving"]:
            w = self.root.winfo_containing(e.x_root, e.y_root)
            if w is self.timeline or w is self.canvas:
                self.bin_to_timeline(sorted(self.bin_sel or {d["k"]}), at if w is self.timeline else None)
                return
        self.draw_timeline()

    def bin_double(self, e):
        k = self._bin_at(e.x, e.y)
        if k is not None:
            self.bin_sel = {k}
            self.bin_to_timeline([k], None)

    def bin_add_selected(self):
        if not self.bin_sel:
            self.set_status("Click a picture in MEDIA first (Ctrl-click to pick more than one).")
            return
        self.bin_to_timeline(sorted(self.bin_sel), None)

    def bin_menu(self, e):
        k = self._bin_at(e.x, e.y)
        if k is None:
            return
        if k not in self.bin_sel:
            self.bin_sel = {k}
            self.draw_bin()
        m = tk.Menu(self.root, tearoff=0, bg=PANEL, fg=INK, activebackground=ACCENT, activeforeground="#1A0F06")
        m.add_command(label="Add to timeline", command=self.bin_add_selected)
        m.add_command(label="Remove from MEDIA", command=self.bin_remove)
        try:
            m.tk_popup(e.x_root, e.y_root)
        finally:
            m.grab_release()

    def bin_remove(self):
        self.bin = [b for k, b in enumerate(self.bin) if k not in self.bin_sel]
        self.bin_sel.clear()
        self.save_bin()
        self.draw_bin()

    def bin_to_timeline(self, ks, at):
        new = []
        for k in ks:
            if 0 <= k < len(self.bin):
                it = copy.deepcopy(self.bin[k])
                if it.kind == "photo":
                    it.length = self.s.photo_secs
                new.append(it)
        if not new:
            return
        self.stop_play()
        self.snapshot()
        if at is None:
            at = self.cur + 1 if 0 <= self.cur < len(self.items) else len(self.items)
        at = max(0, min(len(self.items), at))
        self.items[at:at] = new
        self.cur, self.pos = at, 0.0
        self.update_all()
        self._scroll_to_playhead()
        self.set_status(f"Added {len(new)} to the timeline.")

    # ================================================================ files
    def import_media(self):
        files = filedialog.askopenfilenames(title="Import photos and video clips", filetypes=MEDIA_TYPES)
        if files:
            self.add_files(list(files), to_timeline=False)

    def choose_media(self):
        files = filedialog.askopenfilenames(title="Choose photos and video clips", filetypes=MEDIA_TYPES)
        if files:
            self.add_files(list(files))

    def _dropped(self, event):
        files = []
        for p in self.root.tk.splitlist(event.data):
            if os.path.isdir(p):
                files += [os.path.join(p, f) for f in sorted(os.listdir(p))]
            else:
                files.append(p)
        return files

    def on_drop(self, event):
        self.add_files(self._dropped(event))

    def on_bin_drop(self, event):
        self.add_files(self._dropped(event), to_timeline=False)
        return event.action

    def add_files(self, files, to_timeline=True):
        music = [f for f in files if os.path.splitext(f)[1].lower() in AUDIO_EXT]
        fonts = [f for f in files if f.lower().endswith(fontlib.FONT_EXT)]
        media = [f for f in files if os.path.splitext(f)[1].lower() in PHOTO_EXT | VIDEO_EXT]
        if music:
            self.s.music_path = music[0]
            self.save()
            self.refresh_music_label()
        if fonts:
            fontlib.add_font_files(fonts)
            threading.Thread(target=self._scan_fonts, daemon=True).start()
        if not media:
            if not (music or fonts):
                self.set_status("No photos or videos found in what you dropped.")
            return
        self.set_status(f"Adding {len(media)} file{'s' if len(media) != 1 else ''}…")
        known = {b.path: b for b in self.bin}

        def work():
            new, bad = [], []
            for f in media:
                it = copy.deepcopy(known[f]) if f in known else make_item(f, self.s.photo_secs)
                (new if it else bad).append(it or f)
            self.root.after(0, self._added, new, bad, to_timeline)
        threading.Thread(target=work, daemon=True).start()

    def _added(self, new, bad, to_timeline):
        have = {b.path for b in self.bin}
        for it in new:
            if it.path not in have:
                self.bin.append(copy.deepcopy(it))
                have.add(it.path)
        self.save_bin()
        self.draw_bin()
        if new and to_timeline:
            self.stop_play()
            self.snapshot()
            for it in new:
                if it.kind == "photo":
                    it.length = self.s.photo_secs
            first = len(self.items)
            self.items += new
            self.cur, self.pos = first, 0.0
            self.update_all()
            self._scroll_to_playhead()
            msg = (f"{len(new)} added. On the timeline: drag a clip to move it, drag its edge to trim it, "
                   "right-click for more.")
        else:
            msg = f"{len(new)} added to MEDIA. Double-click or drag to the timeline to use them." if new else ""
        if bad:
            msg += f" Could not read: {', '.join(os.path.basename(b) for b in bad)}"
        self.set_status(msg.strip())

    # ================================================================ preview
    def refresh(self, delay: int = 40):
        if not self.redraw_pending:
            self.redraw_pending = True
            self.root.after(delay, self._draw)

    def _preview_size(self):
        c = self.canvas
        cw, ch = max(60, c.winfo_width()), max(60, c.winfo_height())
        fw, fh = rc.dims(self.s)
        h = ch - 6
        w = round(h * fw / fh)
        if w > cw - 6:
            w = cw - 6
            h = round(w * fh / fw)
        return cw, ch, max(16, w), max(20, h)

    def _source(self, it: Item, t: float):
        """The item's picture (photo, or the clip's frame at t), at most 1280 px."""
        key = (it.path, round(t, 2) if it.kind == "video" else 0)
        src = self.src_cache.get(key)
        if src is None:
            try:
                src = load_photo(it.path, 1600) if it.kind == "photo" else video_frame(it.path, t)
            except Exception:
                src = None
            if src is None:
                return None
            if max(src.size) > 1280:
                src = src.copy()
                src.thumbnail((1280, 1280), Image.BILINEAR)
            if len(self.src_cache) > 24:
                self.src_cache.clear()
            self.src_cache[key] = src
        return src

    def _overlay(self, w, h):
        key = (json.dumps(asdict(self.s), sort_keys=True, ensure_ascii=False), w, h, id(rc.families()))
        if getattr(self, "_ov_key", None) != key:
            self._ov_key, self._ov = key, overlay(self.s, w, h)
        return self._ov

    def _compose(self, pic: Image.Image, idx: int, pos: float, fades: bool) -> Image.Image:
        it = self.items[idx]
        img = rc.blur_picture(pic, it, pos)
        if fades:
            lvl = rc.fade_level(pos, it.length, *rc.fades_for(self.items, self.s, idx))
            if lvl < 1:
                img = img.point(lambda v, a=lvl: int(v * a))
        return Image.alpha_composite(img.convert("RGBA"), self._overlay(pic.width, pic.height)).convert("RGB")

    def _show(self, img: Image.Image):
        cw, ch, w, h = self._preview_size()
        self.preview_img = ImageTk.PhotoImage(img, master=self.root)
        self.canvas.delete("all")
        x, y = (cw - img.width) // 2, (ch - img.height) // 2
        self.pv = (x, y, img.width, img.height)
        self.canvas.create_image(x, y, image=self.preview_img, anchor="nw")

    def _draw(self):
        self.redraw_pending = False
        if self.playing is not None:
            return
        cw, ch, w, h = self._preview_size()
        if self.show_card and self.s.disclaimer:
            self._show(rc.disclaimer_card(self.s.disclaimer, self.s, w, h))
            return
        it = self.current()
        if it is None:
            img = Image.alpha_composite(Image.new("RGBA", (w, h), (70, 78, 86, 255)), self._overlay(w, h))
            self._show(img.convert("RGB"))
            self.canvas.create_text(cw // 2, (ch - h) // 2 + h * 0.3, fill=INK, font=("Segoe UI", 13),
                                    justify="center", text="Drop photos and\nvideo clips here\n\nor click + Add")
            self._draw_guides()
            return
        src = self._source(it, it.start + min(self.pos, max(0, it.length - 0.04)) + 0.02)
        if src is not None:
            pic = rc.place_picture(src, it, self.s.framing, w, h)
            self._show(self._compose(pic, self.cur, self.pos, fades=False))
        else:
            self._show(Image.alpha_composite(Image.new("RGBA", (w, h), (35, 43, 51, 255)),
                                             self._overlay(w, h)).convert("RGB"))
            self.canvas.create_text(cw // 2, ch // 2, fill=MUTED, font=FONT, text=f"Could not show {it.name}")
        self._draw_guides()

    def _blurs(self) -> list[dict]:
        """The current clip's blur boxes (in the new style, so they can be changed in place)."""
        it = self.current()
        if it is None:
            return []
        it.meta["blurs"] = rc.blur_list(it)
        return it.meta["blurs"]

    def _ft(self) -> float:
        it = self.current()
        return (it.start + self.pos) if it else 0.0

    def _move_blur(self, d: dict, cx: float, cy: float, whole_path: bool = False):
        """Put a blur box's centre at (cx, cy) now. Following: a new point on its path here.
        Not following (or whole_path): the whole box moves."""
        cx, cy = max(0.0, min(1.0, cx)), max(0.0, min(1.0, cy))
        ft = self._ft()
        if d.get("follow") and not whole_path:
            for k in d["keys"]:
                if abs(k[0] - ft) < 0.06:
                    k[1], k[2] = round(cx, 4), round(cy, 4)
                    break
            else:
                d["keys"].append([round(ft, 3), round(cx, 4), round(cy, 4)])
                d["keys"].sort()
        else:
            ox, oy = rc.blur_centre(d, ft)
            for k in d["keys"]:
                k[1] = round(max(0.0, min(1.0, k[1] + cx - ox)), 4)
                k[2] = round(max(0.0, min(1.0, k[2] + cy - oy)), 4)

    def _blur_rects(self):
        """Blur boxes of the current clip in canvas pixels, and whether each shows right now."""
        it = self.current()
        x, y, w, h = self.pv
        out = []
        if it is None:
            return out
        ft = self._ft()
        for d in rc.blur_list(it):
            cx, cy = rc.blur_centre(d, ft)
            bw, bh = d["w"] * w, d["h"] * h
            a = x + max(0.0, min(w - bw, cx * w - bw / 2))
            b = y + max(0.0, min(h - bh, cy * h - bh / 2))
            out.append((a, b, a + bw, b + bh, rc.blur_active(d, ft)))
        return out

    def _head_rect(self):
        _lines, box = rc.headline_placement(self.s)
        if not box:
            return None
        x, y, w, h = self.pv
        fw, fh = rc.dims(self.s)
        k = w / fw
        return (x + box[0] * k - 6, y + box[1] * k - 6, x + box[2] * k + 6, y + box[3] * k + 6)

    def _draw_guides(self):
        c = self.canvas
        c.delete("guide")
        if self.playing is not None:
            return
        for k, (a, b, cc, d, on) in enumerate(self._blur_rects()):
            sel = k == self.blur_sel
            col = ACCENT if sel else ("#FFFFFF" if on else "#8A949E")
            c.create_rectangle(a, b, cc, d, outline=col, dash=(4, 3) if on else (1, 4), width=2 if sel else 1,
                               tags="guide")
            if not on:
                c.create_text((a + cc) / 2, (b + d) / 2, text="blur off here", fill="#C8D0D8", font=SMALL,
                              tags="guide")
            c.create_rectangle(cc - 7, d - 7, cc, d, fill=ACCENT if sel else "#FFFFFF", outline="", tags="guide")
            c.create_oval(cc - 9, b - 9, cc + 9, b + 9, fill="#C4002E", outline="", tags="guide")
            c.create_text(cc, b, text="✕", fill="#FFFFFF", font=("Segoe UI", 8, "bold"), tags="guide")
        if getattr(self, "_head_hover", False) or (self.pv_drag and self.pv_drag["mode"] == "head"):
            r = self._head_rect()
            if r:
                c.create_rectangle(*r, outline=YELLOW_UI, dash=(4, 3), tags="guide")

    def _pv_hit(self, ex, ey):
        """What is under the pointer on the preview."""
        for k, (a, b, cc, d, _on) in reversed(list(enumerate(self._blur_rects()))):
            if (ex - cc) ** 2 + (ey - b) ** 2 <= 100:
                return ("blur_x", k)
            if cc - 9 <= ex <= cc + 3 and d - 9 <= ey <= d + 3:
                return ("blur_size", k)
            if a <= ex <= cc and b <= ey <= d:
                return ("blur_move", k)
        r = self._head_rect()
        if r and r[0] <= ex <= r[2] and r[1] <= ey <= r[3]:
            return ("head", None)
        x, y, w, h = self.pv
        if x <= ex <= x + w and y <= ey <= y + h:
            return ("pic", None)
        return (None, None)

    def pv_hover(self, e):
        if self.pv_drag or self.playing is not None or self.show_card:
            return
        what, _k = self._pv_hit(e.x, e.y)
        if self.blur_mode and what in ("pic", "head"):
            cur = "crosshair"
        else:
            cur = {"blur_x": "hand2", "blur_size": "bottom_right_corner", "blur_move": "fleur", "head": "fleur",
                   "pic": "fleur" if self.current() else "hand2"}.get(what, "")
        self.canvas.config(cursor=cur)
        hover = what == "head"
        if hover != getattr(self, "_head_hover", False):
            self._head_hover = hover
            self._draw_guides()

    def pv_press(self, e):
        self.canvas.focus_set()
        if self.show_card:
            self.show_card = False
            self.refresh()
            return
        if not self.items:
            self.choose_media()
            return
        self.stop_play()
        it = self.current()
        x, y, w, h = self.pv
        what, k = self._pv_hit(e.x, e.y)
        fx, fy = (e.x - x) / w, (e.y - y) / h
        if self.blur_mode and what in ("pic", "head"):
            self.snapshot()
            bl = self._blurs()
            bl.append({"w": 0.0, "h": 0.0, "keys": [[round(self._ft(), 3), fx, fy]], "from": None, "to": None,
                       "follow": False})
            self.blur_sel = len(bl) - 1
            self.pv_drag = {"mode": "blur_new", "k": self.blur_sel, "fx": fx, "fy": fy}
            return
        if what == "blur_x":
            self.blur_sel = k
            self.delete_blur()
            return
        if what in ("blur_move", "blur_size"):
            self.snapshot()
            self.blur_sel = k
            bd = self._blurs()[k]
            cx, cy = rc.blur_centre(bd, self._ft())
            self.pv_drag = {"mode": what, "k": k, "fx": fx, "fy": fy, "cx": cx, "cy": cy,
                            "x0": cx - bd["w"] / 2, "y0": cy - bd["h"] / 2}
            self._draw_guides()
            return
        self.blur_sel = -1
        if what == "head":
            dx, cy = rc.head_pos(self.s)
            self.pv_drag = {"mode": "head", "ex": e.x, "ey": e.y, "dx": dx, "cy": cy}
        elif what == "pic" and it is not None:
            self.pv_drag = {"mode": "pic", "ex": e.x, "ey": e.y, "snap": False}
        self._draw_guides()

    def pv_motion(self, e):
        d = self.pv_drag
        if not d:
            return
        x, y, w, h = self.pv
        fw, fh = rc.dims(self.s)
        fx, fy = max(0.0, min(1.0, (e.x - x) / w)), max(0.0, min(1.0, (e.y - y) / h))
        it = self.current()
        if d["mode"] == "head":
            dx = d["dx"] + (e.x - d["ex"]) * fw / w
            if abs(dx) < 14:
                dx = 0.0          # snap back to the alignment line
            cy = max(0.08, min(0.97, d["cy"] + (e.y - d["ey"]) / h))
            hp = dict(self.s.head_pos or {})
            hp[self.s.layout] = [round(dx, 1), round(cy, 4)]
            self.s.head_pos = hp
            self.save()
            self.refresh(15)
        elif d["mode"] == "pic" and it is not None:
            if not d["snap"]:
                if abs(e.x - d["ex"]) + abs(e.y - d["ey"]) < 3:
                    return
                self.snapshot()
                it = self.current()
                self._ensure_subject(it)
                d["snap"] = True
            sw, sh = it.meta.get("width") or 16, it.meta.get("height") or 9
            _x0, _y0, cw, ch = rc.subject_box(sw, sh, fw, fh, it.meta["zoom"], it.meta["cx"], it.meta["cy"])
            ddx, ddy = e.x - d["ex"], e.y - d["ey"]
            d["ex"], d["ey"] = e.x, e.y
            hx, hy = cw / 2 / sw, ch / 2 / sh
            it.meta["cx"] = round(max(hx, min(1 - hx, it.meta["cx"] - ddx * cw / w / sw)), 4)
            it.meta["cy"] = round(max(hy, min(1 - hy, it.meta["cy"] - ddy * ch / h / sh)), 4)
            self.refresh(15)
        elif d["mode"] == "blur_new":
            bd = self._blurs()[d["k"]]
            bd["w"], bd["h"] = abs(fx - d["fx"]), abs(fy - d["fy"])
            bd["keys"] = [[bd["keys"][0][0], (fx + d["fx"]) / 2, (fy + d["fy"]) / 2]]
            self.refresh(15)
        elif d["mode"] == "blur_move":
            self._move_blur(self._blurs()[d["k"]], d["cx"] + fx - d["fx"], d["cy"] + fy - d["fy"])
            self.refresh(15)
        elif d["mode"] == "blur_size":
            bd = self._blurs()[d["k"]]
            bd["w"], bd["h"] = round(max(0.03, fx - d["x0"]), 4), round(max(0.02, fy - d["y0"]), 4)
            self._move_blur(bd, d["x0"] + bd["w"] / 2, d["y0"] + bd["h"] / 2, whole_path=True)
            self.refresh(15)

    def pv_release(self, e):
        d, self.pv_drag = self.pv_drag, None
        if not d:
            return
        it = self.current()
        if d["mode"] == "blur_new" and it is not None:
            bd = self._blurs()[d["k"]]
            if bd["w"] < 0.03 or bd["h"] < 0.02:      # a click, not a box: make a box of a good size
                bd["w"], bd["h"] = 0.24, 0.14
            bd["w"], bd["h"] = round(bd["w"], 4), round(bd["h"], 4)
            self.blur_mode = False
            self.update_item_bar()
            self.draw_timeline()
            self.set_status("Blur box added. Drag it to move it, drag its corner to resize. Right-click it to "
                            "blur only from or to the playhead, or to follow something that moves.")
        elif d["mode"] == "pic":
            self.update_edit_buttons()
        elif d["mode"] == "head":
            self.set_status("Headline moved. Click Reset place to put it back.")
        self.refresh()

    def pv_menu(self, e):
        """Right-click on a blur box: when it shows, following a moving subject, removing it."""
        what, k = self._pv_hit(e.x, e.y)
        if what not in ("blur_move", "blur_size", "blur_x"):
            return
        self.stop_play()
        self.blur_sel = k
        self._draw_guides()
        bd = self._blurs()[k]
        here = fmt_secs(self.pos)

        def change(fn, msg):
            self.snapshot()
            fn(self._blurs()[k])
            self.draw_timeline()
            self.refresh()
            self.set_status(msg)

        def set_from(d):
            d["from"] = round(self._ft(), 3)
            if d["to"] is not None and d["to"] <= d["from"]:
                d["to"] = None

        def set_to(d):
            d["to"] = round(self._ft(), 3)
            if d["from"] is not None and d["from"] >= d["to"]:
                d["from"] = None

        def follow(d):
            d["follow"] = True

        def still(d):
            cx, cy = rc.blur_centre(d, self._ft())
            d["keys"], d["follow"] = [[round(self._ft(), 3), cx, cy]], False

        m = tk.Menu(self.root, tearoff=0, bg=PANEL, fg=INK, activebackground=ACCENT, activeforeground="#1A0F06")
        m.add_command(label=f"Blur starts here ({here})",
                      command=lambda: change(set_from, f"The blur now starts at {here} in this clip."))
        m.add_command(label=f"Blur ends here ({here})",
                      command=lambda: change(set_to, f"The blur now ends at {here} in this clip."))
        m.add_command(label="Blur the whole clip",
                      command=lambda: change(lambda d: d.update({"from": None, "to": None}),
                                             "The blur now covers the whole clip."))
        m.add_separator()
        if bd.get("follow"):
            m.add_command(label="Stop following (keep it still here)",
                          command=lambda: change(still, "The blur box stays still now."))
        else:
            m.add_command(label="Follow something that moves…",
                          command=lambda: change(follow, "Following: move the playhead (or play and pause), then "
                                                         "drag the box onto the face again. Do it every second or "
                                                         "so; the box glides between the places you set."))
        m.add_separator()
        m.add_command(label="Remove this blur", command=self.delete_blur)
        try:
            m.tk_popup(e.x_root, e.y_root)
        finally:
            m.grab_release()

    def pv_wheel(self, e):
        it = self.current()
        if not it or self.show_card:
            return
        self.stop_play()
        if not getattr(self, "_zoom_snap", False):
            self.snapshot()
            self._zoom_snap = True
            self.root.after(800, lambda: setattr(self, "_zoom_snap", False))
        it = self.current()
        self._ensure_subject(it)
        z = it.meta["zoom"] * (1.1 if wheel_dir(e) > 0 else 1 / 1.1)
        it.meta["zoom"] = round(max(1.0, min(4.0, z)), 3)
        self.update_edit_buttons()
        self.refresh(15)

    # ---- playing the reel in the preview (picture only)
    def toggle_play(self):
        if self.playing is not None:
            self.stop_play()
            return
        if not self.items:
            return
        self.show_card = False
        self.blur_mode = False
        _st, total = self.starts()
        if self.global_t() >= total - 0.05:
            self.seek(0)
        cw, ch, w, h = self._preview_size()
        self.playing = {"t0": time.time(), "T0": self.global_t(), "idx": None, "proc": None, "q": None,
                        "frames": 0, "base": 0.0, "pic": None, "w": w - w % 2, "h": h - h % 2, "ended": False}
        self.play_btn.config(text="❚❚ Pause")
        self.canvas.delete("guide")
        token = object()
        self.playing["token"] = token
        items = copy.deepcopy(self.items)
        settings = ReelSettings(**copy.deepcopy(asdict(self.s)))
        if len(self.audio_cache) > 60:
            self.audio_cache.clear()

        def work():
            try:
                track = rc.preview_audio(items, settings, cache=self.audio_cache)
            except Exception:
                track = None
            try:
                self.root.after(0, self._audio_ready, token, track)
            except Exception:
                pass
        threading.Thread(target=work, daemon=True).start()
        self._play_tick()

    def sound(self):
        """The sound output (sounddevice), or None when this computer has none."""
        if self._sound is None:
            try:
                import sounddevice
                self._sound = sounddevice
            except Exception:
                self._sound = False
        return self._sound or None

    def _audio_ready(self, token, track):
        P = self.playing
        if P is None or P.get("token") is not token or track is None or not len(track):
            return
        sd = self.sound()
        if sd is None:
            if not self._sound_warned:
                self._sound_warned = True
                self.set_status("No sound output was found, so the preview plays without sound.")
            return
        now = P["T0"] + (time.time() - P["t0"])
        i0 = int(now * rc.SR)
        if i0 >= len(track) - rc.SR // 20:
            return
        try:
            sd.play(track[i0:], rc.SR)
        except Exception as e:
            if not self._sound_warned:
                self._sound_warned = True
                self.set_status(f"The preview can't play sound on this computer ({e}).")
            return
        # start the picture clock again together with the sound, so they stay in step
        P["audio"] = True
        P["t0"], P["T0"] = time.time(), now

    def _play_open(self, i: int, pos: float):
        P = self.playing
        self._kill(P.get("proc"))
        it = self.items[i]
        w, h = P["w"], P["h"]
        P.update(idx=i, proc=None, q=None, frames=0, base=pos, pic=None, ended=False)
        if it.kind != "video":
            src = self._source(it, 0)
            P["pic"] = rc.place_picture(src, it, self.s.framing, w, h) if src is not None else \
                Image.new("RGB", (w, h), (35, 43, 51))
            return
        vf = rc.picture_filter(it, self.s.framing, w, h, PLAY_FPS)
        cmd = [rc.ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-ss", f"{it.start + pos:.3f}",
               "-t", f"{max(0.05, it.length - pos):.3f}", "-i", it.path, "-filter_complex", vf, "-map", "[pic]",
               "-an", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, **rc._NO_WINDOW)
        except Exception as e:
            self.set_status(f"Could not play: {e}")
            return
        q: queue.Queue = queue.Queue(maxsize=30)
        size = w * h * 3

        def reader():
            while True:
                data = proc.stdout.read(size)
                if len(data) < size:
                    q.put(None)
                    return
                q.put(data)
        threading.Thread(target=reader, daemon=True).start()
        P.update(proc=proc, q=q)

    @staticmethod
    def _kill(proc):
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass

    def _play_tick(self):
        P = self.playing
        if P is None:
            return
        st, total = self.starts()
        T = P["T0"] + (time.time() - P["t0"])
        if T >= total or not self.items:
            self.stop_play(at_end=True)
            return
        i, pos = self.locate(T)
        if i != P["idx"]:
            self._play_open(i, pos)
            if self.playing is None:
                return
        it = self.items[i]
        if it.kind == "video" and P["q"] is not None:
            due = int((pos - P["base"]) * PLAY_FPS)
            latest = None
            try:
                while P["frames"] <= due:
                    data = P["q"].get_nowait()
                    if data is None:
                        P["ended"] = True
                        break
                    latest = data
                    P["frames"] += 1
            except queue.Empty:
                pass
            if latest is not None:
                P["pic"] = Image.frombuffer("RGB", (P["w"], P["h"]), latest, "raw", "RGB", 0, 1)
        self.cur, self.pos = i, pos
        if P["pic"] is not None:
            self._show(self._compose(P["pic"], i, pos, fades=True))
        self.pos_var.set(T)
        self.pos_lbl.config(text=f"{fmt_secs(T)} / {fmt_secs(total)}")
        if i != getattr(self, "_play_drawn", None):
            self._play_drawn = i
            self.update_item_bar()
            self.draw_timeline()
        else:
            self._draw_playhead()
            self.update_edit_buttons()
        self._scroll_to_playhead()
        self.root.after(15, self._play_tick)

    def stop_play(self, at_end=False):
        if self.playing is None:
            return
        P, self.playing = self.playing, None
        self._play_drawn = None
        self._kill(P.get("proc"))
        if P.get("audio") and self.sound() is not None:
            try:
                self.sound().stop()
            except Exception:
                pass
        self.play_btn.config(text="▶ Play")
        if at_end and self.items:
            self.cur = len(self.items) - 1
            self.pos = self.items[-1].length
        self.update_item_bar()
        self.draw_timeline()
        self.refresh()

    # ================================================================ export
    def export(self):
        if self.busy:
            self.cancel_flag = True
            self.set_status("Stopping…")
            return
        if not self.items:
            return
        self.stop_play()
        self._headline_commit()
        start_dir = self.s.out_dir if self.s.out_dir and os.path.isdir(self.s.out_dir) else \
            os.path.dirname(self.items[0].path)
        out = filedialog.asksaveasfilename(title="Save the reel as", defaultextension=".mp4",
                                           initialdir=start_dir,
                                           initialfile=f"TBS-Reel-{time.strftime('%Y%m%d-%H%M')}.mp4",
                                           filetypes=[("MP4 video", "*.mp4")])
        if not out:
            return
        self.s.out_dir = os.path.dirname(out)
        self.save()
        items = copy.deepcopy(self.items)
        settings = ReelSettings(**copy.deepcopy(asdict(self.s)))
        self.busy, self.cancel_flag = True, False
        self.export_btn.config(text="Stop")
        self.open_btn.config(state="disabled")
        self.bar["value"] = 0
        t0 = time.time()

        def prog(frac, msg):
            self.root.after(0, self._progress, frac, msg)

        def work():
            try:
                make_reel(items, settings, out, progress=prog, cancel=lambda: self.cancel_flag)
                secs = time.time() - t0
                self.root.after(0, self._finish, out, f"Saved {os.path.basename(out)} "
                                                      f"(made in {fmt_secs(secs)}).", None)
            except Cancelled:
                self.root.after(0, self._finish, "", "Stopped. Nothing was saved.", None)
            except Exception as e:
                self.root.after(0, self._finish, "", "The reel could not be made.", str(e))
        threading.Thread(target=work, daemon=True).start()

    def _progress(self, frac, msg):
        self.bar["value"] = round(frac * 1000)
        self.set_status(f"{msg}  ({frac * 100:.0f}%)")

    def _finish(self, out, msg, err):
        self.busy = False
        self.export_btn.config(text="Make reel")
        self.update_total()
        self.set_status(msg)
        if out:
            self.last_out = out
            self.bar["value"] = 1000
            self.open_btn.config(state="normal")
        else:
            self.bar["value"] = 0
        if err:
            messagebox.showerror("TBS Reel", f"{msg}\n\n{err[-800:]}")

    def open_out(self):
        if self.last_out and os.path.exists(self.last_out):
            open_path(self.last_out, reveal=True)


def make_root() -> tk.Tk:
    global HAS_DND
    if HAS_DND:
        try:
            return TkinterDnD.Tk()
        except Exception:
            HAS_DND = False
            stray = getattr(tk, "_default_root", None)
            if stray is not None:
                try:
                    stray.destroy()
                except Exception:
                    pass
                tk._default_root = None
    return tk.Tk()


def check(out_path: str) -> int:
    """Used by the build: prove the packaged app can shape text and draw the frames."""
    from PIL import features
    lines, ok = [], True
    try:
        lines.append(f"raqm={features.check('raqm')} fribidi={features.version('fribidi')} "
                     f"loaded={rc.fribidi_loader.LOADED}")
        ok = bool(features.check("raqm"))
        f = rc.headline_font(60)
        shaped = f.getlength("ক্ষ") < f.getlength("ক") + f.getlength("ষ")
        lines.append(f"shaped={shaped}")
        ok = ok and shaped
        for lay in rc.LAYOUTS:
            s = ReelSettings(layout=lay, headline=[["Test ", False, False], ["headline", True, True]])
            ov = overlay(s)
            card = rc.disclaimer_card("copyright", s, *rc.dims(s))
            lines.append(f"{lay}: overlay={ov.size} card={card.size}")
            ok = ok and ov.size == rc.dims(s)
        import sounddevice
        lines.append(f"sound={sounddevice.get_portaudio_version()[1]}")
        it = Item("x", "video", length=2.0, start=1.0)
        it.meta["blurs"] = [{"w": 0.2, "h": 0.1, "keys": [[1.0, 0.3, 0.3], [2.0, 0.6, 0.6]], "from": 1.2, "to": 2.5}]
        lines.append(f"blur={rc.blur_boxes(it, 1080, 1920, 0.5)}")
        lines.append(f"ffmpeg={os.path.exists(rc.ffmpeg_exe())}")
        ok = ok and os.path.exists(rc.ffmpeg_exe())
    except Exception as e:
        lines.append(f"error={e!r}")
        ok = False
    lines.append("OK" if ok else "FAILED")
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return 0 if ok else 3


def main():
    if len(sys.argv) > 2 and sys.argv[1] == "--check":
        sys.exit(check(sys.argv[2]))
    root = make_root()
    app = App(root)

    def on_close():
        if app.busy:
            if not messagebox.askyesno("TBS Reel", "A reel is still being made. Stop and quit?"):
                return
            app.cancel_flag = True
        app.stop_play()
        save_settings(app.s)
        root.destroy()
    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
