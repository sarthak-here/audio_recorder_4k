# 4K Audio Recorder Pro

A professional high-resolution audio recording application built with Python and Tkinter.

## Features

- **Studio-grade quality** — up to 32-bit Float / 384 kHz sample rate
- **Real-time waveform** — scrolling canvas with color-coded level visualization
- **Stereo VU meters** — gradient level bars, peak-hold lines, and clip indicators
- **Full transport controls** — Record, Pause, Resume, Stop
- **Input gain control** — ±20 dB slider
- **Noise gate** — threshold-based background noise suppression
- **Auto-normalize** — normalize to −0.5 dBFS on save
- **Format support** — WAV, FLAC, W64
- **Bit depth** — 16-bit Int, 24-bit Int, 32-bit Float
- **Device selector** — pick any input device with live refresh
- **Recordings manager** — waveform preview, playback, export, delete
- **Settings persistence** — remembers all settings between sessions
- **One-click launch** — no console window, opens instantly

## Keyboard Shortcuts

| Key | Action |
|-----|--------|
| `Space` | Record / Pause / Resume |
| `Esc` | Stop recording |
| `P` | Play / Stop selected recording |
| `Delete` | Delete selected recording |

## Requirements

- Python 3.10+
- sounddevice
- soundfile
- numpy

## Quick Start

```bash
# 1. Install dependencies (first time only)
setup.bat

# 2. Launch the app
launch.bat
```

Or double-click the **4K Audio Recorder** shortcut on your Desktop.

## Recordings

All recordings are saved automatically to `~/Desktop/4K_Recordings/`.

## Tech Stack

| Component | Library |
|-----------|---------|
| GUI | tkinter (built-in) |
| Audio I/O | sounddevice |
| File I/O | soundfile |
| Processing | numpy |
