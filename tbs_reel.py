"""TBS Reel - desktop app.

1. Add photos and video clips (drag them onto the window or click Add).
2. Edit on the timeline: click a clip, move the slider, then Split, Cut start,
   Cut end or Delete. Undo puts it back.
3. Type the date, credit and headline. Select words and click Bold or Yellow.
   Bangla and English both work, each with its own font.
4. Click Make reel. You get a 1080 x 1350 (4:5) MP4 in the TBS reel frame.
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
from reel_core import (AUDIO_EXT, PHOTO_EXT, VIDEO_EXT, Cancelled, Item, ReelSettings, frame_picture, load_photo,
                       make_item, make_reel, overlay, resource, today_text, total_length, video_frame)

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    HAS_DND = True
except Exception:  # still works with the "Add" button
    HAS_DND = False

CONFIG = os.path.join(os.path.expanduser("~"), ".tbs_reel.json")
BG, PANEL, PANEL2, INK, MUTED, ACCENT = "#1B2128", "#232B33", "#2C3640", "#E6EAEE", "#9AA5B1", "#F97316"
YELLOW_UI = "#FFF636"
FONT = ("Segoe UI", 10)
BOLD = ("Segoe UI", 10, "bold")
if sys.platform.startswith("win"):
    TEXT_FONT = "Nirmala UI"          # shows Bangla and English
elif sys.platform == "darwin":
    TEXT_FONT = "Kohinoor Bangla"
else:
    TEXT_FONT = "Noto Sans Bengali"
MEDIA_TYPES = [("Photos and videos", " ".join(f"*{e}" for e in sorted(PHOTO_EXT | VIDEO_EXT))),
               ("Videos", " ".join(f"*{e}" for e in sorted(VIDEO_EXT))),
               ("Photos", " ".join(f"*{e}" for e in sorted(PHOTO_EXT)))]
MUSIC_TYPES = [("Music", " ".join(f"*{e}" for e in sorted(AUDIO_EXT)))]
FONT_TYPES = [("Fonts", "*.ttf *.otf *.ttc *.TTF *.OTF")]
FRAMING = {"auto": "Auto (wide videos at the top)", "fill": "Fill the frame (crops the sides)",
           "top": "Full width at the top", "fit": "Whole picture in the middle"}
MOTION = {"zoom": "Gentle zoom", "none": "Still"}
PLAY_FPS = 15


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


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.s = load_settings()
        self.s.date_text = ""              # the date always starts as today
        self.items: list[Item] = []
        self.cur = -1
        self.undo_stack: list[tuple[list[Item], int]] = []
        self.pos = 0.0                     # slider position inside the selected clip (seconds)
        self.pic_cache: dict[tuple, Image.Image] = {}
        self.thumbs: dict[tuple, ImageTk.PhotoImage] = {}
        self.thumb_jobs: set = set()
        self.preview_img = None
        self.redraw_pending = False
        self.busy = False
        self.cancel_flag = False
        self.last_out = ""
        self.playing = None                # (process, queue, ...) while a clip plays in the preview
        self.drag_from = None
        self.families = rc.families()

        root.title("TBS Reel")
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        root.geometry(f"{min(1340, sw - 40)}x{min(840, sh - 80)}")
        root.minsize(1100, 680)
        root.configure(bg=BG)
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TScale", background=BG)
        style.configure("Horizontal.TProgressbar", troughcolor=PANEL, background=ACCENT, bordercolor=BG,
                        lightcolor=ACCENT, darkcolor=ACCENT)

        self._build_header()
        self._build_right()
        self._build_left()
        self._build_centre()

        if HAS_DND:
            for w in (self.canvas, self.timeline, root):
                w.drop_target_register(DND_FILES)
                w.dnd_bind("<<Drop>>", self.on_drop)
        root.bind_all("<Control-z>", lambda e: self.undo())
        root.bind_all("<Command-z>", lambda e: self.undo())
        for key, fn in (("<Delete>", self.delete), ("<BackSpace>", self.delete), ("<space>", self.toggle_play),
                        ("s", self.split), ("<Left>", lambda: self.nudge(-1)), ("<Right>", lambda: self.nudge(1))):
            root.bind(key, lambda e, f=fn: None if self._typing() else (f(), "break")[1])

        self.refresh_music_label()
        self.update_all()
        self.set_status("Start by dropping photos or video clips onto this window, or click + Add."
                        if HAS_DND else "Start by clicking + Add photos / videos.")
        threading.Thread(target=self._scan_fonts, daemon=True).start()

    # ================================================================ layout
    def _build_header(self):
        head = tk.Frame(self.root, bg=BG)
        head.pack(side="top", fill="x", padx=16, pady=(10, 4))
        self._head_imgs = []
        try:
            for name, h in (("logo.png", 32), ("brand.png", 17)):
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
        tk.Label(head, text="1  Add clips      2  Edit on the timeline      3  Type the text      4  Make reel",
                 fg=MUTED, bg=BG, font=FONT).pack(side="right")

    def _build_left(self):
        left = tk.Frame(self.root, bg=BG, width=330)
        left.pack(side="left", fill="y", padx=(16, 8), pady=(0, 12))
        left.pack_propagate(False)

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

        self._section(left, "HEADLINE  (Bangla, English or both)")
        self.head = tk.Text(left, height=4, wrap="word", bg=PANEL, fg=INK, insertbackground=INK, relief="flat",
                            font=(TEXT_FONT, 13), undo=True, padx=6, pady=5, selectbackground="#3B5A78")
        self.head.tag_configure("hl", foreground=YELLOW_UI)
        self.ui_fonts = {"en": TEXT_FONT, "bn": TEXT_FONT}
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
        tk.Label(left, text="Select words, then click Bold or Yellow (click again to undo it). "
                            "Enter starts a new line.", bg=BG, fg=MUTED, font=("Segoe UI", 9),
                 justify="left", anchor="w", wraplength=310).pack(fill="x", pady=(3, 0))
        self.load_headline()

        self._section(left, "FONTS")
        self.bn_menu = self._combo_row(left, "Bangla", self.font_changed)
        self.en_menu = self._combo_row(left, "English", self.font_changed)
        self.hsize_var = tk.DoubleVar(self.root, value=self.s.head_size_pct)
        self._slider(left, "Size", self.hsize_var, 60, 140)
        r = tk.Frame(left, bg=BG); r.pack(fill="x", pady=(4, 0))
        self._btn(r, "Add fonts…", self.add_fonts).pack(side="left")
        self.font_note = tk.Label(r, text="Looking for fonts on this computer…", bg=BG, fg=MUTED,
                                  font=("Segoe UI", 9), anchor="w", justify="left", wraplength=200)
        self.font_note.pack(side="left", padx=8, fill="x")
        self.fill_font_menus()
        self.apply_ui_fonts()

    def _build_right(self):
        right = tk.Frame(self.root, bg=BG, width=320)
        right.pack(side="right", fill="y", padx=(8, 16), pady=(0, 12))
        right.pack_propagate(False)

        foot = tk.Frame(right, bg=BG)
        foot.pack(side="bottom", fill="x")
        self.total_lbl = tk.Label(foot, bg=BG, fg=INK, font=BOLD, anchor="w")
        self.total_lbl.pack(fill="x")
        self.status = tk.Label(foot, text="", bg=BG, fg=MUTED, font=FONT, anchor="w", justify="left",
                               wraplength=300)
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

        body = tk.Frame(right, bg=BG)
        body.pack(side="top", fill="both", expand=True)
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
        self.music_lbl = tk.Label(body, bg=BG, fg=MUTED, font=FONT, anchor="w", justify="left", wraplength=300)
        self.music_lbl.pack(fill="x")
        self.mvol_var = tk.DoubleVar(self.root, value=self.s.music_volume_pct)
        self._slider(body, "Music", self.mvol_var, 0, 100)
        self.clipsound_var = tk.BooleanVar(self.root, value=self.s.clip_sound)
        self._check(body, "Keep the sound of video clips", self.clipsound_var, self.look_changed).pack(anchor="w")

        self._section(body, "LOGO AND ENDING")
        self.logo_var = tk.BooleanVar(self.root, value=self.s.logo_on)
        self._check(body, "Show TBS logo", self.logo_var, self.look_changed).pack(anchor="w")
        self.end_var = tk.BooleanVar(self.root, value=self.s.end_card)
        self._check(body, "TBS end card (2.5 seconds)", self.end_var, self.look_changed).pack(anchor="w")

    def _build_centre(self):
        mid = tk.Frame(self.root, bg=BG)
        mid.pack(side="left", fill="both", expand=True, padx=8, pady=(0, 12))

        # timeline at the bottom (packed first so it always shows)
        tl = tk.Frame(mid, bg=BG)
        tl.pack(side="bottom", fill="x")
        r = tk.Frame(tl, bg=BG); r.pack(fill="x", pady=(6, 4))
        add = self._btn(r, "+ Add photos / videos", self.choose_media)
        add.config(bg=PANEL2, font=BOLD)
        add.pack(side="left")
        self._btn(r, "◀ Move", lambda: self.move(-1)).pack(side="left", padx=(8, 0))
        self._btn(r, "Move ▶", lambda: self.move(1)).pack(side="left", padx=4)
        self._btn(r, "Clear all", self.clear).pack(side="right")
        self.undo_btn = self._btn(r, "↶ Undo", self.undo)
        self.undo_btn.pack(side="right", padx=6)
        self.timeline = tk.Canvas(tl, bg=PANEL, height=96, highlightthickness=0)
        self.timeline.pack(fill="x")
        self.tl_scroll = ttk.Scrollbar(tl, orient="horizontal", command=self.timeline.xview)
        self.timeline.config(xscrollcommand=self.tl_scroll.set)
        self.tl_scroll.pack(fill="x")
        self.timeline.bind("<ButtonPress-1>", self.tl_press)
        self.timeline.bind("<B1-Motion>", self.tl_motion)
        self.timeline.bind("<ButtonRelease-1>", self.tl_release)
        self.timeline.bind("<Configure>", lambda e: self.draw_timeline())

        # editing bar for the selected clip
        ed = tk.Frame(mid, bg=BG)
        ed.pack(side="bottom", fill="x", pady=(6, 0))
        self.item_lbl = tk.Label(ed, bg=BG, fg=INK, font=BOLD, anchor="w")
        self.item_lbl.pack(fill="x")
        r = tk.Frame(ed, bg=BG); r.pack(fill="x", pady=(2, 0))
        self.play_btn = self._btn(r, "▶ Play", self.toggle_play)
        self.play_btn.config(width=7)
        self.play_btn.pack(side="left")
        self.pos_var = tk.DoubleVar(self.root, value=0)
        self.scrub = ttk.Scale(r, from_=0, to=1, variable=self.pos_var, command=self.on_scrub)
        self.scrub.pack(side="left", fill="x", expand=True, padx=8)
        self.pos_lbl = tk.Label(r, bg=BG, fg=MUTED, font=FONT, width=13, anchor="e")
        self.pos_lbl.pack(side="left")
        r = tk.Frame(ed, bg=BG); r.pack(fill="x", pady=(4, 0))
        self.edit_btns = []
        for text, fn in (("✂ Split here", self.split), ("Cut start here", self.cut_start),
                         ("Cut end here", self.cut_end)):
            b = self._btn(r, text, fn)
            b.pack(side="left", padx=(0, 6))
            self.edit_btns.append(b)
        self.del_btn = self._btn(r, "✕ Delete clip", self.delete)
        self.del_btn.pack(side="left", padx=(0, 6))
        self.secs_frame = tk.Frame(r, bg=BG)
        self.secs_frame.pack(side="left")
        tk.Label(self.secs_frame, text="On screen", bg=BG, fg=INK, font=FONT).pack(side="left")
        self.len_var = tk.StringVar(self.root, value="")
        self.len_spin = self._spin(self.secs_frame, self.len_var, 0.5, 60, self.photo_len_changed)
        self.len_spin.pack(side="left", padx=4)
        tk.Label(self.secs_frame, text="sec", bg=BG, fg=MUTED, font=FONT).pack(side="left")

        self.canvas = tk.Canvas(mid, bg=BG, highlightthickness=0)
        self.canvas.pack(side="top", fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda e: self.refresh())
        self.canvas.bind("<Button-1>", lambda e: None if self.items else self.choose_media())

    # ================================================================ widget helpers
    def _btn(self, parent, text, cmd):
        return tk.Button(parent, text=text, command=cmd, bg=PANEL, fg=INK, activebackground=ACCENT,
                         activeforeground="#1A0F06", relief="flat", font=FONT, padx=10, pady=4, cursor="hand2")

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
        tk.Label(r, text=label, width=7, anchor="w", bg=BG, fg=INK, font=FONT).pack(side="left")
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
        r = tk.Frame(parent, bg=BG); r.pack(fill="x", pady=1)
        tk.Label(r, text=label, width=7, anchor="w", bg=BG, fg=INK, font=FONT).pack(side="left")
        val = tk.Label(r, width=5, anchor="e", bg=BG, fg=MUTED, font=FONT)

        def changed(_v=None):
            val.config(text=f"{var.get():.0f}%")
            self.look_changed()

        ttk.Scale(r, from_=lo, to=hi, variable=var, command=changed).pack(side="left", fill="x", expand=True)
        val.pack(side="left")
        val.config(text=f"{var.get():.0f}%")

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
        s.framing = self._key(self.framing_menu)
        s.motion = self._key(self.motion_menu)
        s.logo_on = bool(self.logo_var.get())
        s.music_volume_pct = round(self.mvol_var.get())
        s.clip_sound = bool(self.clipsound_var.get())
        s.end_card = bool(self.end_var.get())
        if hasattr(self, "hsize_var"):
            s.head_size_pct = round(self.hsize_var.get())
            s.date_text = self.date_var.get()
            s.credit_text = self.credit_var.get()
        if getattr(self, "_last_framing", None) != s.framing:
            self.pic_cache.clear()
            self._last_framing = s.framing
        self.save()
        self.update_total()
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
        found = [n for n in ("BiblioMono", "FF Meta Serif Pro") if n in fams]
        self.font_note.config(text=("Found " + " and ".join(found)) if found else
                              (f"{len(mine)} fonts from this computer" if mine else "Using the built-in fonts"))
        self._ov_key = None
        self.refresh()

    def fill_font_menus(self):
        fams = self.families
        bn = sorted([n for n, f in fams.items() if f.bangla], key=str.lower)
        en = sorted([n for n, f in fams.items() if f.latin], key=str.lower)
        self.bn_menu.config(values=bn)
        self.en_menu.config(values=en)
        for menu, names, attr, is_bn in ((self.bn_menu, bn, "bangla_font", True),
                                         (self.en_menu, en, "english_font", False)):
            want = getattr(self.s, attr)
            if want not in names:
                want = fontlib.pick_default(fams, is_bn)
            menu.set(want)

    def font_changed(self):
        self.s.bangla_font = self.bn_menu.get()
        self.s.english_font = self.en_menu.get()
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
                        "Pick them in the Bangla / English menus.")

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

    # ---- show the chosen fonts in the typing boxes too
    def apply_ui_fonts(self):
        fams = self.families
        for key, menu in (("en", self.en_menu), ("bn", self.bn_menu)):
            f = fams.get(menu.get())
            name = TEXT_FONT
            if f is not None:
                if f.source == "computer":
                    name = f.name                           # installed: the system already knows it
                elif use_font_file(f.regular):
                    use_font_file(f.bold)
                    name = f.name
            self.ui_fonts[key] = name
        en, bn = self.ui_fonts["en"], self.ui_fonts["bn"]
        self.head.config(font=(en, 14))
        for tag, fam, weight in (("f_en", en, "normal"), ("f_bn", bn, "normal"),
                                 ("f_en_b", en, "bold"), ("f_bn_b", bn, "bold")):
            self.head.tag_configure(tag, font=(fam, 14, weight))
        self.head.tag_raise("hl")
        for e in getattr(self, "entries", []):
            e.config(font=(en, 12))
        self.retag_fonts()

    def retag_fonts(self):
        """Give each piece of the headline the Bangla or English font, bold or not."""
        h = self.head
        for t in ("f_en", "f_bn", "f_en_b", "f_bn_b"):
            h.tag_remove(t, "1.0", "end")
        text = h.get("1.0", "end-1c")
        start, cur = 0, None
        for i, ch in enumerate(text + "\0"):
            if i < len(text):
                bn = bool(rc.BENGALI.match(ch)) if not rc.NEUTRAL.match(ch) else (cur[0] if cur else False)
                key = (bn, "b" in h.tag_names(f"1.0+{i}c"))
            else:
                key = None
            if key != cur:
                if cur is not None and i > start:
                    h.tag_add("f_%s%s" % ("bn" if cur[0] else "en", "_b" if cur[1] else ""),
                              f"1.0+{start}c", f"1.0+{i}c")
                start, cur = i, key

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

    # ================================================================ items and editing
    def snapshot(self):
        self.undo_stack.append((copy.deepcopy(self.items), self.cur))
        del self.undo_stack[:-50]

    def undo(self):
        if self.root.focus_get() is self.head:
            return  # the headline box has its own undo
        if not self.undo_stack:
            self.set_status("Nothing to undo.")
            return
        self.stop_play()
        self.items, self.cur = self.undo_stack.pop()
        self.pos = 0.0
        self.update_all()
        self.set_status("Undone.")

    def add_files(self, files):
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

        def work():
            new, bad = [], []
            for f in media:
                it = make_item(f, self.s.photo_secs)
                (new if it else bad).append(it or f)
            self.root.after(0, self._added, new, bad)
        threading.Thread(target=work, daemon=True).start()

    def _added(self, new, bad):
        if new:
            self.snapshot()
            self.items += new
            self.cur = len(self.items) - len(new)
            self.pos = 0.0
        self.update_all()
        msg = (f"{len(new)} added. Click a clip on the timeline to edit it." if new else "")
        if bad:
            msg += f" Could not read: {', '.join(os.path.basename(b) for b in bad)}"
        self.set_status(msg.strip())

    def current(self) -> Item | None:
        return self.items[self.cur] if 0 <= self.cur < len(self.items) else None

    def select(self, i):
        self.stop_play()
        if 0 <= i < len(self.items) and i != self.cur:
            self.cur = i
            self.pos = 0.0
            self.update_all()

    def move(self, d):
        i = self.cur
        if 0 <= i < len(self.items) and 0 <= i + d < len(self.items):
            self.snapshot()
            self.items[i], self.items[i + d] = self.items[i + d], self.items[i]
            self.cur = i + d
            self.update_all()

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

    def clear(self):
        if self.items and messagebox.askyesno("TBS Reel", "Remove all clips from the timeline?"):
            self.stop_play()
            self.snapshot()
            self.items, self.cur = [], -1
            self.update_all()

    def _edit_ok(self, it) -> bool:
        if it is None:
            return False
        if it.kind != "video":
            self.set_status("Split and cut work on video clips. For a photo, set how many seconds it stays.")
            return False
        if not (0.2 <= self.pos <= it.length - 0.2):
            self.set_status("Move the slider into the clip first (not right at its start or end).")
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
        second.start, second.length = round(it.start + p, 2), round(it.length - p, 2)
        it.length = p
        self.items.insert(self.cur + 1, second)
        self.cur += 1
        self.pos = 0.0
        self.update_all()
        self.set_status("Split into two clips. Select a part and click Delete clip to drop it.")

    def cut_start(self):
        it = self.current()
        if not self._edit_ok(it):
            return
        self.stop_play()
        self.snapshot()
        p = round(self.pos, 2)
        it.start, it.length = round(it.start + p, 2), round(it.length - p, 2)
        self.pos = 0.0
        self.update_all()
        self.set_status("Cut the start of the clip.")

    def cut_end(self):
        it = self.current()
        if not self._edit_ok(it):
            return
        self.stop_play()
        self.snapshot()
        it.length = round(self.pos, 2)
        self.update_all()
        self.set_status("Cut the end of the clip.")

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
            self.update_total()
            self.draw_timeline()

    def on_scrub(self, _v=None):
        it = self.current()
        if not it or self.playing is not None:
            return
        self.pos = max(0.0, min(it.length, self.pos_var.get()))
        self.update_pos_label()
        self.draw_timeline()
        if getattr(self, "_scrub_job", None):
            self.root.after_cancel(self._scrub_job)
        self._scrub_job = self.root.after(120, self.refresh)

    def nudge(self, d):
        it = self.current()
        if it and it.kind == "video":
            self.pos = max(0.0, min(it.length, self.pos + d / rc.FPS))
            self.pos_var.set(self.pos)
            self.update_pos_label()
            self.draw_timeline()
            self.refresh()

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

    def update_pos_label(self):
        it = self.current()
        self.pos_lbl.config(text=f"{fmt_secs(self.pos)} / {fmt_secs(it.length)}" if it else "")

    def update_item_bar(self):
        it = self.current()
        if it is None:
            self.item_lbl.config(text="Click a clip on the timeline to edit it" if self.items else "")
            for b in self.edit_btns + [self.del_btn, self.play_btn]:
                b.config(state="disabled")
            self.scrub.state(["disabled"])
            self.secs_frame.pack_forget()
            self.pos_lbl.config(text="")
            return
        is_v = it.kind == "video"
        self.item_lbl.config(text=f"Clip {self.cur + 1}: {it.name}  ·  "
                                  + (f"video, {fmt_secs(it.length)} (from {fmt_secs(it.start)} in the file)"
                                     if is_v else "photo"))
        for b in self.edit_btns + [self.play_btn]:
            b.config(state="normal" if is_v else "disabled")
        self.del_btn.config(state="normal")
        if is_v:
            self.scrub.state(["!disabled"])
            self.scrub.config(to=max(0.1, it.length))
            self.pos_var.set(self.pos)
            self.secs_frame.pack_forget()
        else:
            self.scrub.state(["disabled"])
            self.pos_var.set(0)
            self.len_var.set(f"{it.length:g}")
            self.secs_frame.pack(side="left")
        self.update_pos_label()

    # ---- timeline
    def _thumb(self, it: Item, w: int, h: int):
        key = (it.path, round(it.start, 2), w, h)
        img = self.thumbs.get(key)
        if img is None and key not in self.thumb_jobs:
            self.thumb_jobs.add(key)

            def work():
                try:
                    pic = load_photo(it.path, 300) if it.kind == "photo" else video_frame(it.path, it.start + 0.05)
                    pic = rc._cover(pic, w, h) if pic is not None else None
                except Exception:
                    pic = None
                self.root.after(0, self._thumb_done, key, pic)
            threading.Thread(target=work, daemon=True).start()
        return img

    def _thumb_done(self, key, pic):
        if pic is not None:
            self.thumbs[key] = ImageTk.PhotoImage(pic, master=self.root)
            self.draw_timeline()

    def _block_geometry(self):
        """x positions of the timeline blocks: width follows the length of each clip."""
        boxes, x = [], 8
        for it in self.items:
            w = max(120, min(360, round(it.length * 30)))
            boxes.append((x, x + w))
            x += w + 6
        return boxes, x

    def draw_timeline(self):
        c = self.timeline
        c.delete("all")
        if not self.items:
            c.create_text(14, 48, anchor="w", fill=MUTED, font=FONT,
                          text="Your timeline is empty. Drop photos or videos here, or click + Add.")
            c.config(scrollregion=(0, 0, c.winfo_width(), 96))
            return
        boxes, total_w = self._block_geometry()
        for i, (it, (x0, x1)) in enumerate(zip(self.items, boxes)):
            sel = i == self.cur
            c.create_rectangle(x0, 6, x1, 90, fill=PANEL2, outline=ACCENT if sel else "#3A4651",
                               width=3 if sel else 1)
            th = self._thumb(it, 52, 65)
            if th is not None:
                c.create_image(x0 + 4, 14, image=th, anchor="nw")
            tx = x0 + 60
            if tx < x1 - 12:
                c.create_text(tx, 16, anchor="nw", fill=INK, font=BOLD, text=f"{i + 1}")
                c.create_text(tx, 36, anchor="nw", fill=MUTED, font=("Segoe UI", 9),
                              text=("Video " if it.kind == "video" else "Photo ") + fmt_secs(it.length))
                room = max(4, (x1 - tx - 6) // 6)
                name = it.name if len(it.name) <= room else it.name[:max(1, room - 1)] + "…"
                c.create_text(tx, 56, anchor="nw", fill=MUTED, font=("Segoe UI", 8), text=name)
            if sel and it.kind == "video" and it.length > 0:
                px = x0 + (x1 - x0) * self.pos / it.length
                c.create_line(px, 4, px, 92, fill=YELLOW_UI, width=2)
        c.config(scrollregion=(0, 0, max(total_w + 8, c.winfo_width()), 96))

    def _block_at(self, x):
        x = self.timeline.canvasx(x)
        boxes, _ = self._block_geometry()
        for i, (x0, x1) in enumerate(boxes):
            if x0 - 3 <= x <= x1 + 3:
                return i
        return len(self.items) - 1 if boxes and x > boxes[-1][1] else None

    def tl_press(self, e):
        if not self.items:
            self.choose_media()
            return
        i = self._block_at(e.x)
        self.drag_from = i
        if i is None:
            return
        if i == self.cur and self.current().kind == "video":     # click inside the selected clip: jump there
            self.stop_play()
            boxes, _ = self._block_geometry()
            x0, x1 = boxes[i]
            frac = (self.timeline.canvasx(e.x) - x0) / max(1, x1 - x0)
            self.pos = max(0.0, min(self.current().length, frac * self.current().length))
            self.update_item_bar()
            self.draw_timeline()
            self.refresh()
        else:
            self.select(i)

    def tl_motion(self, e):
        if self.drag_from is not None:
            self.timeline.config(cursor="sb_h_double_arrow")

    def tl_release(self, e):
        self.timeline.config(cursor="")
        a, self.drag_from = self.drag_from, None
        b = self._block_at(e.x)
        if a is not None and b is not None and a != b:     # dragged a clip to a new place
            self.snapshot()
            it = self.items.pop(a)
            self.items.insert(b, it)
            self.cur = b
            self.pos = 0.0
            self.update_all()
            self.set_status(f"Moved {it.name} to place {b + 1}.")

    # ---- preview
    def refresh(self):
        if not self.redraw_pending:
            self.redraw_pending = True
            self.root.after(40, self._draw)

    def _preview_size(self):
        c = self.canvas
        cw, ch = max(60, c.winfo_width()), max(60, c.winfo_height())
        h = ch - 6
        w = round(h * 4 / 5)
        if w > cw - 6:
            w = cw - 6
            h = round(w * 5 / 4)
        return cw, ch, max(16, w), max(20, h)

    def _picture(self, it: Item, t: float, w: int, h: int) -> Image.Image | None:
        key = (it.path, round(t, 2) if it.kind == "video" else 0, self.s.framing, w, h)
        pic = self.pic_cache.get(key)
        if pic is None:
            try:
                src = load_photo(it.path, 1600) if it.kind == "photo" else video_frame(it.path, t)
            except Exception:
                src = None
            if src is None:
                return None
            pic = frame_picture(src, self.s.framing, w, h)
            if len(self.pic_cache) > 30:
                self.pic_cache.clear()
            self.pic_cache[key] = pic
        return pic

    def _overlay(self, w, h):
        key = (json.dumps(asdict(self.s), sort_keys=True, ensure_ascii=False), w, h, id(rc.families()))
        if getattr(self, "_ov_key", None) != key:
            self._ov_key, self._ov = key, overlay(self.s, w, h)
        return self._ov

    def _show(self, img: Image.Image):
        cw, ch, w, h = self._preview_size()
        self.preview_img = ImageTk.PhotoImage(img, master=self.root)
        self.canvas.delete("all")
        self.canvas.create_image((cw - img.width) // 2, (ch - img.height) // 2, image=self.preview_img,
                                 anchor="nw")

    def _draw(self):
        self.redraw_pending = False
        if self.playing is not None:
            return
        cw, ch, w, h = self._preview_size()
        it = self.current()
        if it is None:
            img = Image.alpha_composite(Image.new("RGBA", (w, h), (70, 78, 86, 255)), self._overlay(w, h))
            self._show(img.convert("RGB"))
            self.canvas.create_text(cw // 2, (ch - h) // 2 + h * 0.3, fill=INK, font=("Segoe UI", 13),
                                    justify="center", text="Drop photos and\nvideo clips here\n\nor click + Add")
            return
        pic = self._picture(it, it.start + min(self.pos, max(0, it.length - 0.04)) + 0.02, w, h)
        base = pic.convert("RGBA") if pic is not None else Image.new("RGBA", (w, h), (35, 43, 51, 255))
        self._show(Image.alpha_composite(base, self._overlay(w, h)).convert("RGB"))
        if pic is None:
            self.canvas.create_text(cw // 2, ch // 2, fill=MUTED, font=FONT, text=f"Could not show {it.name}")

    # ---- playing a clip in the preview (picture only)
    def toggle_play(self):
        if self.playing is not None:
            self.stop_play()
            return
        it = self.current()
        if not it or it.kind != "video":
            return
        if self.pos >= it.length - 0.1:
            self.pos = 0.0
        cw, ch, w, h = self._preview_size()
        w -= w % 2
        h -= h % 2
        mode = rc.resolve_framing(self.s.framing, it.meta.get("width", 0), it.meta.get("height", 0))
        if mode == "fill":
            vf = f"fps={PLAY_FPS},scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}"
        else:   # same look as the reel: the picture over a blurred, darker copy of itself
            y = "0" if mode == "top" else "(H-h)/2"
            sw, sh = max(8, w // 8), max(8, h // 8)
            vf = (f"fps={PLAY_FPS},split[a][b];[a]scale={sw}:{sh}:force_original_aspect_ratio=increase,"
                  f"crop={sw}:{sh},gblur=sigma=6,scale={w}:{h},eq=brightness=-0.09[bg];"
                  f"[b]scale={w}:{h}:force_original_aspect_ratio=decrease:force_divisible_by=2[fg];"
                  f"[bg][fg]overlay=(W-w)/2:{y}")
        cmd = [rc.ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-ss", f"{it.start + self.pos:.3f}",
               "-t", f"{it.length - self.pos:.3f}", "-i", it.path, "-filter_complex", vf, "-an",
               "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, **rc._NO_WINDOW)
        except Exception as e:
            self.set_status(f"Could not play: {e}")
            return
        q: queue.Queue = queue.Queue(maxsize=30)
        frame_bytes = w * h * 3

        def reader():
            while True:
                data = proc.stdout.read(frame_bytes)
                if len(data) < frame_bytes:
                    q.put(None)
                    return
                q.put(data)
        threading.Thread(target=reader, daemon=True).start()
        self._play_frames = 0
        self.playing = (proc, q, w, h, it, time.time(), self.pos)
        self.play_btn.config(text="❚❚ Pause")
        self._play_tick()

    def _play_tick(self):
        if self.playing is None:
            return
        proc, q, w, h, it, t0, p0 = self.playing
        # play in real time: skip frames if this computer can't keep up
        due = int((time.time() - t0) * PLAY_FPS)
        latest, ended = None, False
        try:
            while self._play_frames <= due:
                data = q.get_nowait()
                if data is None:
                    ended = True
                    break
                latest = data
                self._play_frames += 1
        except queue.Empty:
            pass
        if latest is not None:
            frame = Image.frombuffer("RGB", (w, h), latest, "raw", "RGB", 0, 1).convert("RGBA")
            self._show(Image.alpha_composite(frame, self._overlay(w, h)).convert("RGB"))
            self.pos = min(it.length, p0 + self._play_frames / PLAY_FPS)
            self.pos_var.set(self.pos)
            self.update_pos_label()
            self.draw_timeline()
        if ended:
            self.stop_play(at_end=True)
            return
        self.root.after(15, self._play_tick)

    def stop_play(self, at_end=False):
        if self.playing is None:
            return
        proc = self.playing[0]
        self.playing = None
        try:
            proc.kill()
        except Exception:
            pass
        self.play_btn.config(text="▶ Play")
        if at_end:
            it = self.current()
            if it:
                self.pos = it.length
        self.update_item_bar()
        self.draw_timeline()
        self.refresh()

    # ================================================================ files
    def choose_media(self):
        files = filedialog.askopenfilenames(title="Choose photos and video clips", filetypes=MEDIA_TYPES)
        if files:
            self.add_files(list(files))

    def on_drop(self, event):
        files = []
        for p in self.root.tk.splitlist(event.data):
            if os.path.isdir(p):
                files += [os.path.join(p, f) for f in sorted(os.listdir(p))]
            else:
                files.append(p)
        self.add_files(files)

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
    """Used by the build: prove the packaged app can shape Bangla and draw the frame."""
    from PIL import features
    lines, ok = [], True
    try:
        lines.append(f"raqm={features.check('raqm')} fribidi={features.version('fribidi')} "
                     f"loaded={rc.fribidi_loader.LOADED}")
        ok = bool(features.check("raqm"))
        f = rc.headline_font(60)
        shaped = f.getlength("\u0995\u09cd\u09b7") < f.getlength("\u0995") + f.getlength("\u09b7")
        lines.append(f"bangla_shaped={shaped}")
        ok = ok and shaped
        ov = overlay(ReelSettings(headline=[["\u0985\u09ac\u09c8\u09a7 ", False, False], ["Test", True, True]]))
        lines.append(f"overlay={ov.size} ffmpeg={os.path.exists(rc.ffmpeg_exe())}")
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
