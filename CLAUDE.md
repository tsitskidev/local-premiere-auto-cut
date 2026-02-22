# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

SilenceCut is a Windows desktop tool that detects silence in video/audio files using ffmpeg and generates a Final Cut Pro 7 XML (xmeml v4) timeline that Premiere Pro imports as a sequence. It has an embedded VLC media player for preview and a custom Premiere-style timeline.

## Build

Run from the `project/` directory:
```
build.bat
```

Output: `project/dist/SilenceCut.exe`

The bat script auto-installs PyInstaller and python-vlc if missing. It hardcodes Python to `C:\Python313\python.exe` — update `PY=` in `build.bat` if your Python is elsewhere.

The built exe bundles `cut_silence_to_fcpxml.py` as a data file (via `--add-data`) and dynamically imports it at runtime using `importlib`. When running from source, both `.py` files must be in the same directory.

## Running from source

```
C:\Python313\python.exe project/silencecut_gui.py
```

Requirements on PATH: `ffmpeg`, `ffprobe`
Required install: VLC **x64** (not x86), `python-vlc`

## Architecture

**Two files, two layers:**

### `project/cut_silence_to_fcpxml.py` — pure logic, no GUI
- `compute_plan(input_path, threshold, min_silence, pad, min_keep, audio_stream)` → dict with `keeps`, `removes`, `duration`, etc.
  This is what the GUI calls for "Analyze".
- `main(argv)` → runs the full pipeline: creates a mono proxy (`.mov` side-car next to the input), runs silencedetect, writes `<base>__nosilence.XML` next to the input.
  This is what "Generate XML" calls.
- Key pipeline: `run_ffmpeg_silencedetect` → `parse_silences` → `merge_overlaps` → `invert_to_keeps` (applies padding and min_keep filtering) → `make_fcp7_xml`.
- FCP7 XML links to the **mono proxy** (not the original), which becomes the media source in the Premiere sequence.

### `project/silencecut_gui.py` — tkinter `App` class
- **VLC integration**: Embeds VLC by calling `set_hwnd()` on a `tk.Frame`'s HWND. DLL discovery tries `VLC_DIR` env var, then Program Files x64/x86. Falls back gracefully if VLC is absent.
- **Timeline canvas**: Scrollable/zoomable (Ctrl+wheel) canvas that draws keep segments (green) and remove segments (red). Coordinate system: `_sec_to_x` / `_x_to_sec` map time ↔ pixel using `_timeline_total_w` (computed from `_px_per_sec`).
- **Overview canvas**: Fixed-width minimap showing the viewport rectangle (drag to pan).
- **Session persistence**: Settings + last analysis plan saved to `silencecut_settings.json` (next to exe, or `%APPDATA%\SilenceCut\`) as JSON. On reopen, the plan is restored only if the file's size and mtime match (within 0.5s).
- **Threading**: Analysis and XML generation run in daemon threads. GUI updates are marshalled back via `self.after(0, ...)`. Log output is captured via `QueueWriter` into a `queue.Queue` and drained by a 50ms `_tick_logs` timer.
- `_load_cutter()` dynamically imports `cut_silence_to_fcpxml.py` via `importlib` at runtime, resolving its path via `sys._MEIPASS` (PyInstaller) or `__file__`.

## Settings defaults

| Parameter | Default | Meaning |
|---|---|---|
| threshold | -35 dB | Silence level |
| min_silence | 0.25 s | Min silence duration to cut |
| pad | 0.08 s | Padding added around each kept segment |
| min_keep | 0.10 s | Minimum kept segment length |

## Output files (written next to the input video)

- `__SILENCECUT_MONO_PROXY__<name>.mov` — mono audio proxy used as XML media source
- `<name>__nosilence.XML` — FCP7/xmeml timeline for Premiere
