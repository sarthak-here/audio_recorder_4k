#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
4K Audio Recorder Pro
High-Resolution Audio Recording | Up to 32-bit Float / 384kHz
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import numpy as np
import sounddevice as sd
import soundfile as sf
import threading
import queue
import time
import os
import json
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

# ── Color palette ─────────────────────────────────────────────────────────────
BG_DARK      = "#0d0d0f"
BG_MID       = "#16161b"
BG_PANEL     = "#111115"
ACCENT_BLUE  = "#00a8ff"
ACCENT_GREEN = "#00e676"
ACCENT_RED   = "#ff1744"
ACCENT_AMBER = "#ffa000"
ACCENT_CYAN  = "#00e5ff"
TEXT_PRIMARY = "#e0e0ee"
TEXT_DIM     = "#5a5a72"
BORDER       = "#252530"

# ── Constants ─────────────────────────────────────────────────────────────────
SAMPLE_RATES = [44100, 48000, 88200, 96000, 176400, 192000, 352800, 384000]

BIT_DEPTHS = {
    "16-bit Int":   ("int16",   "PCM_16"),
    "24-bit Int":   ("int32",   "PCM_24"),
    "32-bit Float": ("float32", "FLOAT"),
}

FORMATS = {"WAV": ".wav", "FLAC": ".flac", "W64": ".w64"}

SETTINGS_FILE   = Path.home() / ".4k_audio_recorder.json"
RECORDINGS_DIR  = Path.home() / "Desktop" / "4K_Recordings"
WAVEFORM_SECS   = 12          # seconds of waveform history shown live


class DCBlocker:
    """First-order high-pass filter that removes DC offset and sub-20Hz rumble.
    Transfer function: H(z) = (1 - z^-1) / (1 - R*z^-1)
    """
    def __init__(self, R: float = 0.9995):
        self.R = R
        self._x_prev = None
        self._y_prev = None

    def reset(self):
        self._x_prev = None
        self._y_prev = None

    def process(self, data: np.ndarray) -> np.ndarray:
        ch = data.shape[1] if data.ndim > 1 else 1
        if self._x_prev is None:
            self._x_prev = np.zeros(ch, dtype=np.float32)
            self._y_prev = np.zeros(ch, dtype=np.float32)

        flat   = data if data.ndim > 1 else data.reshape(-1, 1)
        out    = np.empty_like(flat)
        xp, yp = self._x_prev, self._y_prev
        R      = self.R

        for i in range(len(flat)):
            y       = flat[i] - xp + R * yp
            out[i]  = y
            xp, yp  = flat[i], y

        self._x_prev = xp
        self._y_prev = yp
        return out if data.ndim > 1 else out.reshape(-1)


# ═══════════════════════════════════════════════════════════════════════════════
#  VU Meter
# ═══════════════════════════════════════════════════════════════════════════════
class VUMeter(tk.Canvas):
    PEAK_HOLD_TICKS = 55  # ~2.5s at 22fps decay

    def __init__(self, parent, **kw):
        super().__init__(parent, bg=BG_DARK, highlightthickness=0, **kw)
        self.level_l = self.level_r = 0.0
        self.hold_l  = self.hold_r  = 0.0
        self.hold_timer_l = self.hold_timer_r = 0
        self.clip_l  = self.clip_r  = False
        self.bind("<Configure>", lambda e: self._draw())

    def update_levels(self, l, r=None):
        r = l if r is None else r
        self.level_l, self.level_r = l, r
        for side, level, attr_h, attr_t, attr_c in (
            ("l", l, "hold_l", "hold_timer_l", "clip_l"),
            ("r", r, "hold_r", "hold_timer_r", "clip_r"),
        ):
            hold = getattr(self, attr_h)
            timer = getattr(self, attr_t)
            if level >= hold:
                setattr(self, attr_h, level)
                setattr(self, attr_t, self.PEAK_HOLD_TICKS)
            else:
                if timer > 0:
                    setattr(self, attr_t, timer - 1)
                else:
                    setattr(self, attr_h, max(0.0, hold - 0.008))
            if level > 0.99:
                setattr(self, attr_c, True)
        self._draw()

    def decay(self):
        self.level_l = max(0.0, self.level_l - 0.04)
        self.level_r = max(0.0, self.level_r - 0.04)
        self._draw()

    def reset_clip(self):
        self.clip_l = self.clip_r = False
        self.hold_l = self.hold_r = 0.0
        self._draw()

    @staticmethod
    def _to_db(v):
        return max(-72.0, 20.0 * np.log10(max(v, 1e-10)))

    @staticmethod
    def _db_color(db):
        if db > -3:  return ACCENT_RED
        if db > -9:  return ACCENT_AMBER
        if db > -20: return "#e6e600"
        return ACCENT_GREEN

    def _draw(self):
        self.delete("all")
        W, H = self.winfo_width(), self.winfo_height()
        if W < 10 or H < 10:
            return

        GAP = 4
        bar_w = (W - 3 * GAP) // 2

        for i, (level, hold, clipped, lbl) in enumerate([
            (self.level_l, self.hold_l, self.clip_l, "L"),
            (self.level_r, self.hold_r, self.clip_r, "R"),
        ]):
            x = GAP + i * (bar_w + GAP)

            # trough
            self.create_rectangle(x, 0, x + bar_w, H,
                                  fill=BG_MID, outline="")

            # filled portion (bottom-up)
            if level > 0.0001:
                bar_h = int(level * (H - 4))
                y0 = H - bar_h
                # draw in 3 segments for colour gradient
                segs = 30
                seg_h = max(1, bar_h // segs)
                for s in range(segs):
                    sy = H - (s + 1) * seg_h
                    if sy < y0:
                        break
                    frac = s / segs              # 0 = bottom (quiet), 1 = top (loud)
                    db = self._to_db(frac * level + (1 - frac) * 0)
                    db_mapped = frac * 0 + (1 - frac) * (-72)  # rough
                    seg_db = -72 + frac * 72
                    color = self._db_color(seg_db)
                    self.create_rectangle(x + 1, sy, x + bar_w - 1,
                                          sy + seg_h - 1,
                                          fill=color, outline="")

            # peak-hold tick
            if hold > 0.0001:
                py = H - int(hold * (H - 4)) - 2
                pk_db = self._to_db(hold)
                self.create_rectangle(x + 1, max(2, py), x + bar_w - 1,
                                      max(2, py) + 2,
                                      fill=self._db_color(pk_db), outline="")

            # clip flash
            clip_col = ACCENT_RED if clipped else "#2a1a1a"
            self.create_rectangle(x, 0, x + bar_w, 3,
                                  fill=clip_col, outline="")

            # label
            self.create_text(x + bar_w // 2, H - 2, text=lbl,
                             fill=TEXT_DIM, font=("Consolas", 8, "bold"),
                             anchor="s")

        # dB rulers between the two bars
        ruler_x = GAP + bar_w
        for mark_db in (0, -6, -12, -18, -24, -36, -48):
            level_v = 10 ** (mark_db / 20.0)
            y = H - int(level_v * (H - 4)) - 1
            if 2 < y < H - 10:
                self.create_line(ruler_x, y, ruler_x + GAP, y,
                                 fill=BORDER)
                self.create_text(ruler_x + GAP // 2, y,
                                 text=str(mark_db),
                                 fill=TEXT_DIM,
                                 font=("Consolas", 6),
                                 anchor="e")


# ═══════════════════════════════════════════════════════════════════════════════
#  Waveform Canvas
# ═══════════════════════════════════════════════════════════════════════════════
class WaveformCanvas(tk.Canvas):
    def __init__(self, parent, **kw):
        super().__init__(parent, bg=BG_DARK, highlightthickness=0, **kw)
        self._samples   = np.array([], dtype=np.float32)
        self._is_playing = False
        self._play_pos   = 0.0
        self._max_samples = 0
        self.bind("<Configure>", lambda e: self._draw())

    def push_samples(self, chunk: np.ndarray, sr: int):
        mono = chunk.mean(axis=1) if chunk.ndim > 1 else chunk
        self._max_samples = sr * WAVEFORM_SECS
        self._samples = np.concatenate([self._samples, mono])
        if len(self._samples) > self._max_samples:
            self._samples = self._samples[-self._max_samples:]
        self._draw()

    def load_preview(self, samples: np.ndarray):
        self._samples = samples.astype(np.float32)
        self._is_playing = False
        self._play_pos = 0.0
        self._draw()

    def clear(self):
        self._samples = np.array([], dtype=np.float32)
        self._draw()

    def set_play_pos(self, pos: float):
        self._play_pos = pos
        self._is_playing = True
        self._draw()

    def stop_play(self):
        self._is_playing = False
        self._play_pos = 0.0
        self._draw()

    def _draw(self):
        self.delete("all")
        W, H = self.winfo_width(), self.winfo_height()
        if W < 4 or H < 4:
            return
        mid = H // 2

        # Grid
        for i in range(1, 4):
            y = H * i // 4
            self.create_line(0, y, W, y, fill=BORDER, dash=(4, 10))
        self.create_line(0, mid, W, mid, fill="#1f1f2a")

        if len(self._samples) < 2:
            self.create_text(W // 2, mid,
                             text="◆  READY — Press SPACE to begin recording",
                             fill=TEXT_DIM,
                             font=("Consolas", 11))
            return

        n = len(self._samples)
        amp = mid * 0.92

        for x in range(W):
            i0 = int(x * n / W)
            i1 = int((x + 1) * n / W)
            if i0 >= n:
                break
            chunk = self._samples[i0:i1] if i1 > i0 else self._samples[i0:i0+1]
            peak = float(np.max(np.abs(chunk)))

            if peak > 0.9:
                color = ACCENT_RED
            elif peak > 0.65:
                color = ACCENT_AMBER
            elif peak > 0.0001:
                color = ACCENT_BLUE
            else:
                color = BORDER

            y_top = int(mid - peak * amp)
            y_bot = int(mid + peak * amp)
            self.create_line(x, y_top, x, y_bot, fill=color)

        # Playback cursor
        if self._is_playing:
            cx = int(self._play_pos * W)
            self.create_line(cx, 0, cx, H, fill="white", width=2)

        # Scrolling indicator (right edge glow)
        if not self._is_playing:
            self.create_rectangle(W - 3, 0, W, H,
                                  fill=ACCENT_BLUE, outline="", stipple="gray50")


# ═══════════════════════════════════════════════════════════════════════════════
#  Recording metadata
# ═══════════════════════════════════════════════════════════════════════════════
class Recording:
    def __init__(self, path, duration, sr, bit_depth, channels):
        self.path      = Path(path)
        self.duration  = duration
        self.sr        = sr
        self.bit_depth = bit_depth
        self.channels  = channels

    @property
    def size_mb(self):
        try:
            return self.path.stat().st_size / 1_048_576
        except:
            return 0.0

    @property
    def dur_str(self):
        m, s = divmod(self.duration, 60)
        return f"{int(m):02d}:{s:05.2f}"


# ═══════════════════════════════════════════════════════════════════════════════
#  Main Application
# ═══════════════════════════════════════════════════════════════════════════════
class AudioRecorder4K(tk.Tk):

    def __init__(self):
        super().__init__()
        self.title("4K Audio Recorder Pro")
        self.geometry("1140x780")
        self.minsize(860, 620)
        self.configure(bg=BG_DARK)

        # State
        self._recording    = False
        self._paused       = False
        self._playing      = False
        self._stream       = None
        self._audio_chunks = []
        self._q            = queue.Queue()
        self._rec_start    = 0.0
        self._pause_start  = 0.0
        self._total_paused = 0.0
        self._cur_path     = None
        self._recordings   = []
        self._play_thread  = None
        self._device_map   = {}

        # Settings vars
        cfg = self._load_cfg()
        self.v_sr       = tk.IntVar   (value=cfg.get("sr",       48000))
        self.v_depth    = tk.StringVar(value=cfg.get("depth",    "32-bit Float"))
        self.v_ch       = tk.IntVar   (value=cfg.get("ch",       2))
        self.v_fmt      = tk.StringVar(value=cfg.get("fmt",      "WAV"))
        self.v_dev      = tk.StringVar(value=cfg.get("dev",      "Default"))
        self.v_gain_db  = tk.DoubleVar(value=cfg.get("gain_db",  0.0))
        self.v_gate     = tk.DoubleVar(value=cfg.get("gate",     0.0))
        self.v_normalize= tk.BooleanVar(value=cfg.get("normalize",False))
        self.v_mono_mix = tk.BooleanVar(value=cfg.get("mono_mix", False))
        self.v_dc_block = tk.BooleanVar(value=cfg.get("dc_block", True))
        self.v_denoise  = tk.BooleanVar(value=cfg.get("denoise",  False))
        self._dc_blocker = DCBlocker()
        self.v_status   = tk.StringVar(value="Ready · SPACE = Record")

        RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)

        self._setup_theme()
        self._build_ui()
        self._refresh_devices()
        self._scan_existing()
        self._tick_timer()
        self._tick_decay()

        self.bind("<space>",   self._kbd_space)
        self.bind("<Escape>",  lambda e: self._stop())
        self.bind("<p>",       lambda e: self._toggle_play())
        self.bind("<Delete>",  lambda e: self._delete_sel())
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── Theme ──────────────────────────────────────────────────────────────────
    def _setup_theme(self):
        s = ttk.Style(self)
        s.theme_use("clam")
        s.configure(".", background=BG_DARK, foreground=TEXT_PRIMARY,
                    fieldbackground=BG_MID, bordercolor=BORDER)
        s.configure("TCombobox", fieldbackground=BG_MID, background=BG_MID,
                    foreground=TEXT_PRIMARY, arrowcolor=ACCENT_BLUE,
                    bordercolor=BORDER, selectbackground=ACCENT_BLUE,
                    selectforeground="white", padding=4)
        s.map("TCombobox",
              fieldbackground=[("readonly", BG_MID)],
              foreground=[("readonly", TEXT_PRIMARY)],
              selectbackground=[("readonly", ACCENT_BLUE)])
        s.configure("TScale", background=BG_DARK, troughcolor=BG_MID,
                    sliderthickness=14)
        s.configure("TScrollbar", background=BG_MID, troughcolor=BG_DARK,
                    arrowcolor=TEXT_DIM, bordercolor=BORDER)

    # ── UI builder ─────────────────────────────────────────────────────────────
    def _build_ui(self):
        # ── Header ────────────────────────────────────────────────────────────
        hdr = tk.Frame(self, bg=BG_MID, height=48)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)
        tk.Label(hdr, text="◈  4K AUDIO RECORDER PRO",
                 bg=BG_MID, fg=ACCENT_BLUE,
                 font=("Consolas", 15, "bold")).pack(side="left", padx=16, pady=10)
        tk.Label(hdr, text="32-bit Float  ·  up to 384 kHz  ·  Studio Grade",
                 bg=BG_MID, fg=TEXT_DIM,
                 font=("Consolas", 9)).pack(side="left")
        tk.Label(hdr, text="SPACE=Rec  ESC=Stop  P=Play  Del=Delete",
                 bg=BG_MID, fg=TEXT_DIM,
                 font=("Consolas", 9)).pack(side="right", padx=16)

        # ── Settings strip ────────────────────────────────────────────────────
        strip = tk.Frame(self, bg=BG_PANEL, pady=6)
        strip.pack(fill="x")

        def sep():
            tk.Frame(strip, bg=BORDER, width=1).pack(side="left", fill="y",
                                                      padx=6, pady=2)
        def grp(label, builder):
            f = tk.Frame(strip, bg=BG_PANEL)
            f.pack(side="left", padx=10)
            tk.Label(f, text=label, bg=BG_PANEL, fg=TEXT_DIM,
                     font=("Consolas", 7, "bold")).pack(anchor="w")
            builder(f)

        grp("INPUT DEVICE",  self._w_device)
        sep()
        grp("SAMPLE RATE",   self._w_sr)
        sep()
        grp("BIT DEPTH",     self._w_depth)
        sep()
        grp("CHANNELS",      self._w_channels)
        sep()
        grp("FORMAT",        self._w_format)
        sep()
        grp("OPTIONS",       self._w_options)

        # ── Body ──────────────────────────────────────────────────────────────
        body = tk.Frame(self, bg=BG_DARK)
        body.pack(fill="both", expand=True, padx=6, pady=4)

        left = tk.Frame(body, bg=BG_DARK)
        left.pack(side="left", fill="both", expand=True)

        right = tk.Frame(body, bg=BG_DARK, width=224)
        right.pack(side="right", fill="y", padx=(6, 0))
        right.pack_propagate(False)

        self._build_left(left)
        self._build_right(right)

        # ── Status bar ────────────────────────────────────────────────────────
        sb = tk.Frame(self, bg=BG_MID, height=22)
        sb.pack(fill="x", side="bottom")
        sb.pack_propagate(False)
        tk.Label(sb, textvariable=self.v_status, bg=BG_MID, fg=TEXT_DIM,
                 font=("Consolas", 9)).pack(side="left", padx=12)
        self._info_lbl = tk.Label(sb, text="", bg=BG_MID, fg=TEXT_DIM,
                                   font=("Consolas", 9))
        self._info_lbl.pack(side="right", padx=12)

    # ── Settings widgets ───────────────────────────────────────────────────────
    def _w_device(self, p):
        f = tk.Frame(p, bg=BG_PANEL)
        f.pack()
        self._dev_cb = ttk.Combobox(f, textvariable=self.v_dev,
                                     width=24, state="readonly")
        self._dev_cb.pack(side="left")
        tk.Button(f, text="↺", bg=BG_MID, fg=ACCENT_BLUE, relief="flat",
                  font=("Consolas", 11), cursor="hand2",
                  activebackground=BG_PANEL, activeforeground=ACCENT_BLUE,
                  command=self._refresh_devices).pack(side="left", padx=3)

    def _w_sr(self, p):
        vals = [f"{r:,} Hz" for r in SAMPLE_RATES]
        self._sr_cb = ttk.Combobox(p, width=13, state="readonly", values=vals)
        sr = self.v_sr.get()
        self._sr_cb.set(f"{sr:,} Hz" if sr in SAMPLE_RATES else "48,000 Hz")
        self._sr_cb.bind("<<ComboboxSelected>>", self._on_sr)
        self._sr_cb.pack()

    def _w_depth(self, p):
        ttk.Combobox(p, textvariable=self.v_depth, width=13, state="readonly",
                     values=list(BIT_DEPTHS)).pack()

    def _w_channels(self, p):
        f = tk.Frame(p, bg=BG_PANEL)
        f.pack()
        for txt, val in (("Mono", 1), ("Stereo", 2)):
            tk.Radiobutton(f, text=txt, variable=self.v_ch, value=val,
                           bg=BG_PANEL, fg=TEXT_PRIMARY, selectcolor=BG_MID,
                           activebackground=BG_PANEL, activeforeground=ACCENT_BLUE,
                           font=("Consolas", 9), cursor="hand2").pack(side="left", padx=4)

    def _w_format(self, p):
        ttk.Combobox(p, textvariable=self.v_fmt, width=7, state="readonly",
                     values=list(FORMATS)).pack()

    def _w_options(self, p):
        for txt, var in (
            ("DC Block",       self.v_dc_block),
            ("Auto-Normalize", self.v_normalize),
            ("Mono Mix-down",  self.v_mono_mix),
            ("Denoise (post)", self.v_denoise),
        ):
            tk.Checkbutton(p, text=txt, variable=var,
                           bg=BG_PANEL, fg=TEXT_DIM, selectcolor=BG_MID,
                           activebackground=BG_PANEL, activeforeground=TEXT_PRIMARY,
                           font=("Consolas", 8), cursor="hand2").pack(anchor="w")

    # ── Left panel ─────────────────────────────────────────────────────────────
    def _build_left(self, p):
        # Timer row
        tr = tk.Frame(p, bg=BG_DARK)
        tr.pack(fill="x", pady=(0, 4))

        self._timer_lbl = tk.Label(tr, text="00:00:00.0",
                                    bg=BG_DARK, fg=ACCENT_GREEN,
                                    font=("Consolas", 30, "bold"))
        self._timer_lbl.pack(side="left")

        self._dot_lbl = tk.Label(tr, text=" ●", bg=BG_DARK, fg=BG_DARK,
                                  font=("Consolas", 20))
        self._dot_lbl.pack(side="left")

        self._state_lbl = tk.Label(tr, text="STANDBY", bg=BG_DARK, fg=TEXT_DIM,
                                    font=("Consolas", 12, "bold"))
        self._state_lbl.pack(side="left", padx=6)

        self._file_lbl = tk.Label(tr, text="", bg=BG_DARK, fg=TEXT_DIM,
                                   font=("Consolas", 9))
        self._file_lbl.pack(side="right", padx=8)

        # Waveform
        wf_border = tk.Frame(p, bg=BORDER, bd=1)
        wf_border.pack(fill="both", expand=True)
        self._wf = WaveformCanvas(wf_border)
        self._wf.pack(fill="both", expand=True, padx=1, pady=1)

        # Playback progress
        self._pb_var = tk.DoubleVar(value=0)
        self._pb_bar = ttk.Scale(p, from_=0, to=100, orient="horizontal",
                                  variable=self._pb_var)
        self._pb_bar.pack(fill="x", pady=(3, 0))

        # Controls row
        cr = tk.Frame(p, bg=BG_DARK)
        cr.pack(fill="x", pady=6)

        self._btn_rec  = self._btn(cr, "⏺  RECORD",  ACCENT_RED,   self._kbd_space, 12)
        self._btn_rec.pack(side="left", padx=4)
        self._btn_pause= self._btn(cr, "⏸  PAUSE",   ACCENT_AMBER, self._toggle_pause, 10)
        self._btn_pause.pack(side="left", padx=2)
        self._btn_stop = self._btn(cr, "⏹  STOP",    "#5a6a7a",    self._stop, 10)
        self._btn_stop.pack(side="left", padx=2)
        self._btn_play = self._btn(cr, "▶  PLAY",    ACCENT_GREEN, self._toggle_play, 10)
        self._btn_play.pack(side="left", padx=10)

        self._btn_pause.configure(state="disabled")
        self._btn_stop.configure(state="disabled")

        # Sliders row
        sl = tk.Frame(p, bg=BG_DARK)
        sl.pack(fill="x", pady=2)

        self._gain_lbl, self._gate_lbl = self._build_sliders(sl)

    def _build_sliders(self, p):
        def slider_group(parent, title, from_, to_, var, fmt_fn):
            f = tk.Frame(parent, bg=BG_DARK)
            f.pack(side="left", padx=12)
            tk.Label(f, text=title, bg=BG_DARK, fg=TEXT_DIM,
                     font=("Consolas", 8, "bold")).pack()
            lbl = tk.Label(f, text="0.0 dB", bg=BG_DARK, fg=TEXT_PRIMARY,
                           font=("Consolas", 9))
            lbl.pack()

            def on_change(val):
                lbl.configure(text=fmt_fn(float(val)))

            sc = ttk.Scale(f, from_=from_, to=to_, orient="horizontal",
                           length=130, variable=var, command=on_change)
            sc.pack()
            return lbl

        gain_lbl = slider_group(p, "INPUT GAIN",  -20, 20,   self.v_gain_db,
                                lambda v: f"{v:+.1f} dB")
        gate_lbl = slider_group(p, "NOISE GATE",    0, 0.12, self.v_gate,
                                lambda v: "OFF" if v < 0.002 else
                                          f"{20*np.log10(max(v,1e-10)):.0f} dB")
        return gain_lbl, gate_lbl

    # ── Right panel ────────────────────────────────────────────────────────────
    def _build_right(self, p):
        tk.Label(p, text="LEVEL METERS", bg=BG_DARK, fg=TEXT_DIM,
                 font=("Consolas", 8, "bold")).pack(pady=(0, 2))

        vu_f = tk.Frame(p, bg=BORDER, bd=1)
        vu_f.pack(fill="x")
        self._vu = VUMeter(vu_f, width=210, height=240)
        self._vu.pack(fill="both", expand=True, padx=1, pady=1)

        row = tk.Frame(p, bg=BG_DARK)
        row.pack(fill="x", pady=4)
        self._btn(row, "RESET CLIP", "#3a1520", self._vu.reset_clip, 10).pack(side="left", padx=2)
        self._peak_lbl = tk.Label(row, text="-∞ dBFS", bg=BG_DARK,
                                   fg=TEXT_DIM, font=("Consolas", 9))
        self._peak_lbl.pack(side="left", padx=6)

        tk.Label(p, text="RECORDINGS", bg=BG_DARK, fg=TEXT_DIM,
                 font=("Consolas", 8, "bold")).pack(anchor="w", pady=(8, 2))

        list_f = tk.Frame(p, bg=BORDER, bd=1)
        list_f.pack(fill="both", expand=True)

        sb = ttk.Scrollbar(list_f)
        sb.pack(side="right", fill="y")
        self._lst = tk.Listbox(list_f, yscrollcommand=sb.set,
                                bg=BG_MID, fg=TEXT_PRIMARY,
                                selectbackground=ACCENT_BLUE,
                                selectforeground="white",
                                font=("Consolas", 8), relief="flat", bd=0,
                                activestyle="none")
        self._lst.pack(fill="both", expand=True)
        sb.config(command=self._lst.yview)
        self._lst.bind("<Double-Button-1>", lambda e: self._toggle_play())
        self._lst.bind("<<ListboxSelect>>",  self._on_sel_change)

        br = tk.Frame(p, bg=BG_DARK)
        br.pack(fill="x", pady=3)
        for txt, fg, cmd in (
            ("▶",  ACCENT_GREEN, self._toggle_play),
            ("🗑", ACCENT_RED,   self._delete_sel),
            ("📁", TEXT_DIM,     self._open_folder),
            ("💾", ACCENT_AMBER, self._export_sel),
        ):
            tk.Button(br, text=txt, command=cmd,
                      bg=BG_MID, fg=fg, relief="flat", width=3,
                      font=("Consolas", 11), cursor="hand2",
                      activebackground=BG_PANEL).pack(side="left", padx=2)

    def _btn(self, parent, text, color, cmd, w=10):
        return tk.Button(parent, text=text, command=cmd,
                         bg=color, fg="white", relief="flat",
                         font=("Consolas", 10, "bold"),
                         width=w, pady=6,
                         activebackground=color, activeforeground="white",
                         cursor="hand2", bd=0)

    # ── Device management ──────────────────────────────────────────────────────
    def _refresh_devices(self):
        try:
            devs     = sd.query_devices()
            hostapis = sd.query_hostapis()

            # Prefer WASAPI on Windows — it only lists currently connected hardware
            preferred_api = None
            for i, api in enumerate(hostapis):
                if "WASAPI" in api["name"]:
                    preferred_api = i
                    break
            # Fall back to MME / DirectSound if WASAPI not found
            if preferred_api is None:
                for i, api in enumerate(hostapis):
                    if api["default_input_device"] >= 0:
                        preferred_api = i
                        break

            names = ["Default"]
            self._device_map = {"Default": None}

            for i, d in enumerate(devs):
                if d["max_input_channels"] <= 0:
                    continue
                # Skip devices not on the preferred host API
                if preferred_api is not None and d.get("hostapi") != preferred_api:
                    continue
                n = d["name"][:36]
                if n not in self._device_map:
                    names.append(n)
                    self._device_map[n] = i

            self._dev_cb["values"] = names
            if self.v_dev.get() not in names:
                self.v_dev.set("Default")
            self.v_status.set(f"Found {len(names)-1} input device(s)  ·  SPACE = Record")
        except Exception as e:
            self.v_status.set(f"Device error: {e}")

    def _on_sr(self, _=None):
        raw = self._sr_cb.get().replace(",", "").replace(" Hz", "")
        self.v_sr.set(int(raw))

    # ── Recording ──────────────────────────────────────────────────────────────
    def _kbd_space(self, _=None):
        if self._playing:
            return
        if not self._recording:
            self._start_rec()
        elif self._paused:
            self._resume_rec()
        else:
            self._pause_rec()

    def _start_rec(self):
        sr       = self.v_sr.get()
        channels = self.v_ch.get()
        dtype    = BIT_DEPTHS[self.v_depth.get()][0]
        device   = self._device_map.get(self.v_dev.get())
        sd_dtype = "float32" if "float" in dtype else dtype

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        ext = FORMATS[self.v_fmt.get()]
        self._cur_path = RECORDINGS_DIR / f"REC_{ts}{ext}"

        self._audio_chunks = []
        self._dc_blocker.reset()
        self._rec_start    = time.time()
        self._total_paused = 0.0
        self._paused       = False
        self._wf.clear()
        self._vu.reset_clip()

        try:
            self._stream = sd.InputStream(
                samplerate=sr, channels=channels, dtype="float32",
                device=device, callback=self._audio_cb,
                blocksize=4096, latency="high",
            )
            self._stream.start()
        except Exception as e:
            messagebox.showerror("Recording Error",
                                 f"Cannot open audio stream:\n{e}\n\n"
                                 "Try a different device or lower sample rate.")
            return

        self._recording = True
        self._btn_rec.configure(text="⏸  PAUSE", bg=ACCENT_AMBER)
        self._btn_pause.configure(state="normal")
        self._btn_stop.configure(state="normal")
        self._dot_lbl.configure(fg=ACCENT_RED)
        self._state_lbl.configure(text="● REC", fg=ACCENT_RED)
        self.v_status.set(f"Recording → {self._cur_path.name}")
        self._drain_queue()

    def _audio_cb(self, indata, frames, t, status):
        if self._paused:
            return
        data = indata.copy()

        # DC offset removal + sub-20Hz rumble filter
        if self.v_dc_block.get():
            data = self._dc_blocker.process(data)

        gain = 10 ** (self.v_gain_db.get() / 20.0)
        data *= gain

        gate = self.v_gate.get()
        if gate > 0:
            rms_per_frame = np.max(np.abs(data), axis=1 if data.ndim > 1 else 0)
            mask = rms_per_frame < gate
            if data.ndim > 1:
                data[mask] = 0
            else:
                data[mask] = 0

        data = np.clip(data, -1.0, 1.0)
        self._q.put(data)

    def _drain_queue(self):
        if not self._recording:
            return
        processed = 0
        while not self._q.empty() and processed < 8:
            chunk = self._q.get()
            self._audio_chunks.append(chunk)
            sr = self.v_sr.get()
            self._wf.push_samples(chunk, sr)

            if chunk.ndim > 1 and chunk.shape[1] >= 2:
                l = float(np.max(np.abs(chunk[:, 0])))
                r = float(np.max(np.abs(chunk[:, 1])))
            else:
                v = float(np.max(np.abs(chunk)))
                l = r = v
            self._vu.update_levels(l, r)
            db = 20 * np.log10(max(max(l, r), 1e-10))
            self._peak_lbl.configure(text=f"{db:.1f} dBFS")
            processed += 1

        self.after(25, self._drain_queue)

    def _pause_rec(self):
        self._paused = True
        self._pause_start = time.time()
        self._btn_rec.configure(text="▶  RESUME", bg=ACCENT_GREEN)
        self._state_lbl.configure(text="⏸ PAUSED", fg=ACCENT_AMBER)
        self._dot_lbl.configure(fg=ACCENT_AMBER)

    def _resume_rec(self):
        self._total_paused += time.time() - self._pause_start
        self._paused = False
        self._btn_rec.configure(text="⏸  PAUSE", bg=ACCENT_AMBER)
        self._state_lbl.configure(text="● REC", fg=ACCENT_RED)
        self._dot_lbl.configure(fg=ACCENT_RED)

    def _toggle_pause(self):
        if self._paused:
            self._resume_rec()
        else:
            self._pause_rec()

    def _stop(self, _=None):
        if not self._recording:
            return
        self._recording = False
        self._paused    = False

        # Read tkinter vars NOW on the main thread before spawning background work
        save_opts = {
            "normalize": self.v_normalize.get(),
            "mono_mix":  self.v_mono_mix.get(),
            "denoise":   self.v_denoise.get(),
            "depth":     self.v_depth.get(),
            "sr":        self.v_sr.get(),
        }

        # Respond to the user immediately — no freeze
        self._btn_rec.configure(text="⏺  RECORD", bg=ACCENT_RED)
        self._btn_pause.configure(state="disabled")
        self._btn_stop.configure(state="disabled")
        self._dot_lbl.configure(fg=BG_DARK)
        self._state_lbl.configure(text="⏳ SAVING", fg=ACCENT_AMBER)
        self.v_status.set("Finalizing recording…")

        # Hand off stream teardown + disk write to a background thread
        stream = self._stream
        chunks = list(self._audio_chunks)
        path   = self._cur_path
        self._stream       = None
        self._audio_chunks = []

        threading.Thread(
            target=self._bg_stop,
            args=(stream, chunks, path, save_opts),
            daemon=True,
        ).start()

    def _bg_stop(self, stream, chunks, path, opts):
        """Background thread: close audio stream, process, and write file."""
        try:
            if stream:
                stream.stop()
                stream.close()
        except Exception:
            pass

        if not chunks or not path:
            self.after(0, self._finish_stop)
            return

        try:
            audio = np.concatenate(chunks, axis=0)

            if opts["normalize"]:
                peak = np.max(np.abs(audio))
                if peak > 0:
                    audio = audio * (0.95 / peak)

            if opts["mono_mix"] and audio.ndim > 1:
                audio = audio.mean(axis=1)

            if opts["denoise"]:
                audio = self._spectral_denoise(audio)

            subtype = BIT_DEPTHS[opts["depth"]][1]
            if subtype == "PCM_16":
                audio = (audio * 32767).astype(np.int16)
            elif subtype == "PCM_24":
                audio = (audio * 8388607).astype(np.int32)

            sr = opts["sr"]
            sf.write(str(path), audio, sr, subtype=subtype)

            info = sf.info(str(path))
            rec  = Recording(path, info.duration, sr, opts["depth"], info.channels)
            self.after(0, lambda r=rec, i=info, s=sr, d=opts["depth"]:
                       self._finish_save(r, i, s, d))

        except Exception as e:
            self.after(0, lambda err=str(e):
                       messagebox.showerror("Save Error", err))
            self.after(0, self._finish_stop)

    def _finish_save(self, rec, info, sr, depth):
        """Back on the main thread after a successful save."""
        self._recordings.insert(0, rec)
        self._refresh_list()
        m, s = divmod(info.duration, 60)
        self.v_status.set(
            f"Saved: {rec.path.name}  ({int(m):02d}:{s:05.2f} · {rec.size_mb:.1f} MB)"
        )
        self._file_lbl.configure(
            text=f"{sr:,} Hz · {depth} · {rec.size_mb:.1f} MB"
        )
        self._finish_stop()

    def _finish_stop(self):
        """Final UI reset after recording is fully done."""
        self._vu.update_levels(0, 0)
        self._state_lbl.configure(text="STANDBY", fg=TEXT_DIM)

    # ── Playback ───────────────────────────────────────────────────────────────
    def _toggle_play(self, _=None):
        if self._recording:
            return
        if self._playing:
            self._stop_play()
        else:
            self._play_sel()

    def _play_sel(self):
        sel = self._lst.curselection()
        if not sel:
            return
        idx = sel[0]
        if idx >= len(self._recordings):
            return
        rec = self._recordings[idx]
        if not rec.path.exists():
            messagebox.showerror("Error", "File not found")
            return

        self._playing = True
        self._btn_play.configure(text="⏹  STOP", bg=ACCENT_RED)
        self._wf.is_playing = True

        self._play_thread = threading.Thread(
            target=self._play_thread_fn, args=(rec,), daemon=True
        )
        self._play_thread.start()

    def _play_thread_fn(self, rec: Recording):
        try:
            data, sr = sf.read(str(rec.path), dtype="float32")
            total = len(data)
            channels = data.shape[1] if data.ndim > 1 else 1
            play_data = data if data.ndim > 1 else data.reshape(-1, 1)
            if play_data.shape[1] == 1:
                play_data = np.repeat(play_data, 2, axis=1)

            event = threading.Event()
            frame_pos = [0]

            def cb(outdata, frames, t, status):
                if not self._playing:
                    raise sd.CallbackStop()
                end = frame_pos[0] + frames
                chunk = play_data[frame_pos[0]:end]
                if len(chunk) == 0:
                    raise sd.CallbackStop()
                if len(chunk) < frames:
                    outdata[:len(chunk)] = chunk
                    outdata[len(chunk):] = 0
                else:
                    outdata[:] = chunk
                frame_pos[0] += frames
                pos = frame_pos[0] / total
                self.after(0, lambda p=pos: self._wf.set_play_pos(p))
                self.after(0, lambda p=pos: self._pb_var.set(p * 100))

            with sd.OutputStream(samplerate=sr, channels=2, dtype="float32",
                                  callback=cb, finished_callback=event.set):
                event.wait()

        except Exception as e:
            self.after(0, lambda: messagebox.showerror("Playback Error", str(e)))
        finally:
            self.after(0, self._stop_play)

    def _stop_play(self):
        self._playing = False
        sd.stop()
        self._btn_play.configure(text="▶  PLAY", bg=ACCENT_GREEN)
        self._wf.stop_play()
        self._pb_var.set(0)

    # ── Recordings list ────────────────────────────────────────────────────────
    def _scan_existing(self):
        files = sorted(
            list(RECORDINGS_DIR.glob("*.wav")) +
            list(RECORDINGS_DIR.glob("*.flac")) +
            list(RECORDINGS_DIR.glob("*.w64")),
            key=lambda f: f.stat().st_mtime, reverse=True
        )[:30]
        for f in files:
            try:
                info = sf.info(str(f))
                self._recordings.append(
                    Recording(f, info.duration, info.samplerate,
                              "Unknown", info.channels)
                )
            except:
                pass
        self._refresh_list()

    def _refresh_list(self):
        self._lst.delete(0, tk.END)
        for r in self._recordings:
            m, s = divmod(r.duration, 60)
            self._lst.insert(tk.END,
                              f"{r.path.stem[-18:]}  [{int(m):02d}:{int(s):02d}]")

    def _on_sel_change(self, _=None):
        sel = self._lst.curselection()
        if not sel:
            return
        rec = self._recordings[sel[0]]
        self._file_lbl.configure(
            text=f"{rec.sr:,} Hz · {rec.bit_depth} · "
                 f"{rec.dur_str} · {rec.size_mb:.1f} MB"
        )
        # Load waveform preview (downsampled)
        try:
            info = sf.info(str(rec.path))
            limit = min(info.frames, info.samplerate * 60)
            data, _ = sf.read(str(rec.path), frames=limit, dtype="float32")
            mono = data.mean(axis=1) if data.ndim > 1 else data
            # Downsample to ~44100 for display
            step = max(1, len(mono) // 44100)
            self._wf.load_preview(mono[::step])
        except:
            pass

    def _delete_sel(self, _=None):
        sel = self._lst.curselection()
        if not sel:
            return
        rec = self._recordings[sel[0]]
        if not messagebox.askyesno("Delete", f"Delete {rec.path.name}?"):
            return
        try:
            rec.path.unlink(missing_ok=True)
            self._recordings.pop(sel[0])
            self._refresh_list()
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def _open_folder(self):
        subprocess.Popen(f'explorer "{RECORDINGS_DIR}"')

    def _export_sel(self):
        sel = self._lst.curselection()
        if not sel:
            messagebox.showinfo("Export", "Select a recording first")
            return
        rec = self._recordings[sel[0]]
        target = filedialog.asksaveasfilename(
            defaultextension=rec.path.suffix,
            filetypes=[("Audio", "*.wav *.flac *.w64")],
            initialfile=rec.path.name,
        )
        if target:
            try:
                shutil.copy2(rec.path, target)
                self.v_status.set(f"Exported → {target}")
            except Exception as e:
                messagebox.showerror("Export Error", str(e))

    # ── Timer + decay ticks ────────────────────────────────────────────────────
    def _tick_timer(self):
        if self._recording and not self._paused:
            elapsed = time.time() - self._rec_start - self._total_paused
            h = int(elapsed // 3600)
            m = int((elapsed % 3600) // 60)
            s = elapsed % 60
            self._timer_lbl.configure(text=f"{h:02d}:{m:02d}:{s:05.2f}")
            cur = self._dot_lbl.cget("fg")
            self._dot_lbl.configure(fg=BG_DARK if cur == ACCENT_RED else ACCENT_RED)
        self.after(100, self._tick_timer)

    def _tick_decay(self):
        if not self._recording:
            self._vu.decay()
        self.after(45, self._tick_decay)

    # ── Settings persistence ───────────────────────────────────────────────────
    def _load_cfg(self):
        try:
            if SETTINGS_FILE.exists():
                return json.loads(SETTINGS_FILE.read_text())
        except:
            pass
        return {}

    @staticmethod
    def _spectral_denoise(audio: np.ndarray,
                          fft_size: int = 2048,
                          threshold_db: float = -38.0) -> np.ndarray:
        """Overlap-add spectral gating. Estimates noise floor from first 0.3s,
        then suppresses frequency bins below that floor * threshold."""
        is_stereo = audio.ndim > 1
        channels  = audio.shape[1] if is_stereo else 1
        flat      = audio if is_stereo else audio.reshape(-1, 1)
        out       = np.zeros_like(flat)

        hop  = fft_size // 2
        win  = np.hanning(fft_size).astype(np.float32)
        norm = 1e-9

        for ch in range(channels):
            sig = flat[:, ch]

            # Estimate noise floor from first 0.3 s (≈ first few frames)
            noise_frames = max(1, int(0.3 * 44100 / hop))
            noise_est = np.zeros(fft_size // 2 + 1, dtype=np.float32)
            count = 0
            for i in range(0, min(noise_frames * hop, len(sig) - fft_size), hop):
                frame = sig[i:i + fft_size] * win
                noise_est += np.abs(np.fft.rfft(frame))
                count += 1
            if count > 0:
                noise_est /= count

            threshold = noise_est * (10 ** (threshold_db / 20.0))

            # Overlap-add processing
            result = np.zeros(len(sig) + fft_size, dtype=np.float32)
            for i in range(0, len(sig) - fft_size, hop):
                frame   = sig[i:i + fft_size] * win
                spec    = np.fft.rfft(frame)
                mag     = np.abs(spec)
                phase   = np.angle(spec)
                # Soft-knee gate: attenuate rather than hard-zero
                gain_mask = np.maximum(0.0, 1.0 - (threshold / (mag + norm)))
                mag_clean = mag * gain_mask
                spec_clean = mag_clean * np.exp(1j * phase)
                result[i:i + fft_size] += np.fft.irfft(spec_clean) * win

            out[:, ch] = result[:len(sig)]

        return out if is_stereo else out.reshape(-1)

    def _save_cfg(self):
        try:
            SETTINGS_FILE.write_text(json.dumps({
                "sr":        self.v_sr.get(),
                "depth":     self.v_depth.get(),
                "ch":        self.v_ch.get(),
                "fmt":       self.v_fmt.get(),
                "dev":       self.v_dev.get(),
                "gain_db":   self.v_gain_db.get(),
                "gate":      self.v_gate.get(),
                "normalize": self.v_normalize.get(),
                "mono_mix":  self.v_mono_mix.get(),
                "dc_block":  self.v_dc_block.get(),
                "denoise":   self.v_denoise.get(),
            }))
        except:
            pass

    def _on_close(self):
        if self._recording:
            if not messagebox.askyesno("Quit", "Recording in progress. Stop and quit?"):
                return
            self._stop()
            # _stop is now non-blocking; give the background thread a moment to close the stream
            self.after(400, self._on_close)
            return
        if self._state_lbl.cget("text") == "⏳ SAVING":
            # Save in progress — check again shortly
            self.after(200, self._on_close)
            return
        if self._playing:
            self._stop_play()
        self._save_cfg()
        self.destroy()


# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    try:
        app = AudioRecorder4K()
        app.mainloop()
    except Exception as e:
        import traceback
        try:
            from tkinter import messagebox
            messagebox.showerror("Fatal Error",
                                 f"{e}\n\n{traceback.format_exc()}")
        except:
            print(traceback.format_exc())
