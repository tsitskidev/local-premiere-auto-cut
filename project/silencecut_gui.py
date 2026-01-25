import contextlib
import datetime
import importlib.util
import io
import json
import os
import queue
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

SCRIPT_NAME = "cut_silence_to_fcpxml.py"

DEFAULTS = {
    "input_path": "",
    "threshold": -35.0,
    "min_silence": 0.25,
    "pad": 0.08,
    "min_keep": 0.10,
    "audio_stream": "",
    "regen_mono": False,
    "volume": 80,
}


def _install_crash_log():
    def excepthook(exc_type, exc, tb):
        try:
            p = os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), "silencecut_crash.log")
            with open(p, "a", encoding="utf-8") as f:
                f.write("\n=== CRASH " + datetime.datetime.now().isoformat() + " ===\n")
                import traceback
                f.write("".join(traceback.format_exception(exc_type, exc, tb)))
        except:
            pass

    sys.excepthook = excepthook


_install_crash_log()

import platform

def _add_vlc_dll_dir():
    paths = []

    # Prefer env override
    vlc_dir_env = os.environ.get("VLC_DIR")
    if vlc_dir_env:
        paths.append(vlc_dir_env)

    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pfx86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")

    # Prefer 64-bit VLC when running 64-bit Python/app
    is_64 = platform.architecture()[0] == "64bit"

    pf_vlc = os.path.join(pf, "VideoLAN", "VLC")
    pfx86_vlc = os.path.join(pfx86, "VideoLAN", "VLC")

    if is_64:
        paths += [pf_vlc, pfx86_vlc]
    else:
        paths += [pfx86_vlc, pf_vlc]

    for p in paths:
        lib = os.path.join(p, "libvlc.dll")
        plugins = os.path.join(p, "plugins")

        if not (p and os.path.isdir(p) and os.path.isfile(lib)):
            continue

        # In frozen apps, VLC *must* find plugins.
        if os.path.isdir(plugins):
            os.environ["VLC_PLUGIN_PATH"] = plugins

        try:
            if hasattr(os, "add_dll_directory"):
                os.add_dll_directory(p)

            # Also add to PATH for dependent DLL discovery
            os.environ["PATH"] = p + os.pathsep + os.environ.get("PATH", "")
            return p
        except:
            continue

    return None

def _resource_dir():
    return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))


def _script_path():
    return os.path.join(_resource_dir(), SCRIPT_NAME)


def _load_module_from_path(module_name: str, path: str):
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Failed to load spec for: {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _sec_to_hhmmss(t: float) -> str:
    if t < 0:
        t = 0.0
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t % 60
    if h > 0:
        return f"{h}:{m:02d}:{s:06.3f}"
    return f"{m}:{s:06.3f}"


def _settings_path_candidates() -> list[str]:
    exe_dir = os.path.dirname(os.path.abspath(sys.argv[0]))
    paths = [os.path.join(exe_dir, "silencecut_settings.json")]

    appdata = os.getenv("APPDATA")
    if appdata:
        paths.append(os.path.join(appdata, "SilenceCut", "silencecut_settings.json"))

    return paths


def _ensure_parent_dir(path: str):
    d = os.path.dirname(path)
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)


def _load_settings() -> dict:
    for p in _settings_path_candidates():
        try:
            if os.path.isfile(p):
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    return data
        except:
            pass
    return {}


def _save_settings(data: dict) -> str | None:
    candidates = _settings_path_candidates()
    for p in candidates:
        try:
            _ensure_parent_dir(p)
            with open(p, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            return p
        except:
            continue
    return None


def _try_import_vlc():
    try:
        import vlc
        return vlc, None
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


class App(tk.Tk):
    def __init__(self):
        super().__init__()

        self.title("SilenceCut → FCP7 XML")
        self.geometry("1080x760")
        self.minsize(940, 660)

        self._log_q = queue.Queue()
        self._running = False
        self._plan = None
        self._playhead_sec = 0.0
        self._timeline_total_w = 3000

        self._settings_dirty = False
        self._settings_save_after_id = None

        self.input_path = tk.StringVar()
        self.threshold = tk.DoubleVar()
        self.min_silence = tk.DoubleVar()
        self.pad = tk.DoubleVar()
        self.min_keep = tk.DoubleVar()
        self.audio_stream = tk.StringVar()
        self.regen_mono = tk.BooleanVar()
        self.volume = tk.IntVar()

        self._vlc_dll_dir = _add_vlc_dll_dir()
        self._vlc, self._vlc_err = _try_import_vlc()

        self._vlc_instance = None
        self._vlc_player = None
        self._vlc_media = None
        self._vlc_loaded_path = None
        self._vlc_is_playing = False
        self._vlc_duration_sec = 0.0
        self._vlc_poll_after_id = None

        self._load_settings_into_vars()
        self._build_ui()
        self._install_var_traces()
        self._tick_logs()

        self.after(200, self._init_vlc_if_available)

    # ------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------
    def _collect_settings(self) -> dict:
        return {
            "input_path": self.input_path.get(),
            "threshold": float(self.threshold.get()),
            "min_silence": float(self.min_silence.get()),
            "pad": float(self.pad.get()),
            "min_keep": float(self.min_keep.get()),
            "audio_stream": self.audio_stream.get(),
            "regen_mono": bool(self.regen_mono.get()),
            "volume": int(self.volume.get()),
        }

    def _apply_settings(self, data: dict):
        merged = dict(DEFAULTS)
        merged.update({k: v for k, v in data.items() if k in DEFAULTS})

        self.input_path.set(str(merged["input_path"]))
        self.threshold.set(float(merged["threshold"]))
        self.min_silence.set(float(merged["min_silence"]))
        self.pad.set(float(merged["pad"]))
        self.min_keep.set(float(merged["min_keep"]))
        self.audio_stream.set(str(merged["audio_stream"]))
        self.regen_mono.set(bool(merged["regen_mono"]))
        self.volume.set(int(merged["volume"]))

    def _load_settings_into_vars(self):
        data = _load_settings()
        if not data:
            data = dict(DEFAULTS)
        self._apply_settings(data)

    def _mark_settings_dirty(self, *_):
        self._settings_dirty = True
        if self._settings_save_after_id is not None:
            self.after_cancel(self._settings_save_after_id)
        self._settings_save_after_id = self.after(350, self._flush_settings_save)

    def _flush_settings_save(self):
        self._settings_save_after_id = None
        if not self._settings_dirty:
            return
        self._settings_dirty = False
        p = _save_settings(self._collect_settings())
        if p is None:
            return

    def reset_defaults(self):
        self._apply_settings(dict(DEFAULTS))
        self._mark_settings_dirty()
        self.preview_status.set("(Defaults restored)")
        self._append_log("\n[UI] Reset settings to defaults.\n")

    def _install_var_traces(self):
        vars_to_trace = [
            self.input_path, self.threshold, self.min_silence, self.pad, self.min_keep,
            self.audio_stream, self.regen_mono, self.volume
        ]
        for v in vars_to_trace:
            try:
                v.trace_add("write", self._mark_settings_dirty)
            except:
                pass

    # ------------------------------------------------------------
    # UI
    # ------------------------------------------------------------
    def _build_ui(self):
        root = ttk.Frame(self, padding=14)
        root.pack(fill="both", expand=True)

        top = ttk.Frame(root)
        top.pack(fill="both", expand=True)

        left = ttk.Frame(top)
        left.pack(side="left", fill="both", expand=True)

        right = ttk.Frame(top)
        right.pack(side="right", fill="y", padx=(12, 0))

        # ---------------- Input + Options ----------------
        input_box = ttk.LabelFrame(left, text="Input", padding=12)
        input_box.pack(fill="x")

        row = ttk.Frame(input_box)
        row.pack(fill="x")

        ttk.Entry(row, textvariable=self.input_path).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="Browse…", command=self.pick_file).pack(side="left", padx=(10, 0))
        ttk.Button(row, text="Load in Player", command=self.load_in_player).pack(side="left", padx=(10, 0))

        opts = ttk.LabelFrame(left, text="Cut Settings", padding=12)
        opts.pack(fill="x", pady=(12, 0))

        grid = ttk.Frame(opts)
        grid.pack(fill="x")

        grid.columnconfigure(1, weight=1)
        grid.columnconfigure(3, weight=1)

        def add_labeled(var, label, r, c):
            ttk.Label(grid, text=label).grid(row=r, column=c * 2, sticky="w", padx=(0, 10), pady=6)
            ttk.Entry(grid, textvariable=var).grid(row=r, column=c * 2 + 1, sticky="we", pady=6)

        add_labeled(self.threshold, "Threshold (dB):", 0, 0)
        add_labeled(self.min_silence, "Min silence (sec):", 0, 1)
        add_labeled(self.pad, "Pad around cuts (sec):", 1, 0)
        add_labeled(self.min_keep, "Min keep (sec):", 1, 1)

        ttk.Label(grid, text="Audio stream map (optional, e.g. 0:a:0):").grid(row=2, column=0, sticky="w", pady=6)
        ttk.Entry(grid, textvariable=self.audio_stream).grid(row=2, column=1, columnspan=3, sticky="we", pady=6)

        ttk.Checkbutton(grid, text="Re-create mono proxy even if it exists", variable=self.regen_mono).grid(
            row=3, column=0, columnspan=4, sticky="w", pady=(6, 0)
        )

        btn_row = ttk.Frame(opts)
        btn_row.pack(fill="x", pady=(10, 0))
        ttk.Button(btn_row, text="Reset Defaults", command=self.reset_defaults).pack(side="left")

        # ---------------- Preview (timeline) ----------------
        preview_box = ttk.LabelFrame(left, text="Timeline Preview", padding=12)
        preview_box.pack(fill="x", pady=(12, 0))

        pr = ttk.Frame(preview_box)
        pr.pack(fill="x")

        ttk.Button(pr, text="Analyze + Draw Timeline", command=self.analyze_preview).pack(side="left")
        ttk.Button(pr, text="Seek Player to Playhead", command=self.seek_player_to_playhead).pack(side="left", padx=(10, 0))

        self.preview_status = tk.StringVar(value="(No analysis yet)")
        ttk.Label(pr, textvariable=self.preview_status).pack(side="left", padx=(12, 0))

        self.playhead_label = tk.StringVar(value="Playhead: 0:00.000")
        ttk.Label(pr, textvariable=self.playhead_label).pack(side="right")

        timeline_frame = ttk.Frame(preview_box)
        timeline_frame.pack(fill="x", pady=(10, 0))

        self.timeline_canvas = tk.Canvas(timeline_frame, height=64, highlightthickness=1)
        self.timeline_canvas.pack(side="top", fill="x", expand=True)

        self.timeline_scroll = ttk.Scrollbar(timeline_frame, orient="horizontal", command=self.timeline_canvas.xview)
        self.timeline_scroll.pack(side="bottom", fill="x")

        self.timeline_canvas.configure(xscrollcommand=self.timeline_scroll.set)
        self.timeline_canvas.bind("<Button-1>", self.on_timeline_click)

        # ---------------- Log + Run ----------------
        run_row = ttk.Frame(left)
        run_row.pack(fill="x", pady=(12, 0))

        self.run_btn = ttk.Button(run_row, text="Run (Generate XML)", command=self.run)
        self.run_btn.pack(side="left")

        self.progress = ttk.Progressbar(run_row, mode="indeterminate")
        self.progress.pack(side="left", fill="x", expand=True, padx=(14, 0))

        log_box = ttk.LabelFrame(left, text="Log", padding=10)
        log_box.pack(fill="both", expand=True, pady=(12, 0))

        self.log = tk.Text(log_box, height=10, wrap="word")
        self.log.pack(fill="both", expand=True)

        # ---------------- Embedded Player (right panel) ----------------
        player_box = ttk.LabelFrame(right, text="Embedded Player (VLC)", padding=12)
        player_box.pack(fill="y", expand=True)

        self.player_surface = tk.Frame(player_box, width=360, height=240, bg="black")
        self.player_surface.pack(fill="both", expand=True)
        self.player_surface.pack_propagate(False)

        ctrl = ttk.Frame(player_box)
        ctrl.pack(fill="x", pady=(10, 0))

        ttk.Button(ctrl, text="Play/Pause", command=self.player_toggle_play).pack(side="left")
        ttk.Button(ctrl, text="Stop", command=self.player_stop).pack(side="left", padx=(8, 0))
        ttk.Button(ctrl, text="<< 1s", command=lambda: self.player_nudge(-1.0)).pack(side="left", padx=(8, 0))
        ttk.Button(ctrl, text="1s >>", command=lambda: self.player_nudge(1.0)).pack(side="left", padx=(8, 0))

        vol_row = ttk.Frame(player_box)
        vol_row.pack(fill="x", pady=(10, 0))

        ttk.Label(vol_row, text="Volume").pack(side="left")
        self.vol_slider = ttk.Scale(vol_row, from_=0, to=100, orient="horizontal", command=self.on_volume_slider)
        self.vol_slider.pack(side="left", fill="x", expand=True, padx=(10, 0))
        self.vol_slider.set(float(self.volume.get()))

        self.player_time_label = tk.StringVar(value="0:00.000 / 0:00.000")
        ttk.Label(player_box, textvariable=self.player_time_label).pack(fill="x", pady=(8, 0))

        self.seek_slider = ttk.Scale(player_box, from_=0, to=1, orient="horizontal", command=self.on_seek_slider)
        self.seek_slider.pack(fill="x", pady=(6, 0))
        self._seek_is_dragging = False
        self.seek_slider.bind("<ButtonPress-1>", lambda e: self._set_seek_drag(True))
        self.seek_slider.bind("<ButtonRelease-1>", lambda e: self._set_seek_drag(False))

        self._append_log(
            "Embedded playback uses VLC.\n"
            "- Install VLC + python-vlc if the player panel stays disabled.\n"
            "- Load in Player, then Analyze + Draw Timeline.\n"
            "- Click the timeline to set playhead, then Seek Player to Playhead.\n\n"
        )

    def _append_log(self, s: str):
        self.log.insert("end", s)
        self.log.see("end")

    def _tick_logs(self):
        try:
            while True:
                msg = self._log_q.get_nowait()
                self._append_log(msg)
        except queue.Empty:
            pass
        self.after(50, self._tick_logs)

    def _set_running(self, running: bool):
        self._running = running
        self.run_btn.config(state=("disabled" if running else "normal"))
        if running:
            self.progress.start(10)
        else:
            self.progress.stop()

    # ------------------------------------------------------------
    # File picker
    # ------------------------------------------------------------
    def pick_file(self):
        path = filedialog.askopenfilename(
            title="Select video/audio file",
            filetypes=[
                ("Media files", "*.mp4 *.mov *.mkv *.wav *.mp3 *.m4a *.aac *.flac *.avi"),
                ("All files", "*.*"),
            ],
        )
        if path:
            self.input_path.set(path)

    # ------------------------------------------------------------
    # Cutter module
    # ------------------------------------------------------------
    def _load_cutter(self):
        sp = _script_path()
        if not os.path.isfile(sp):
            raise RuntimeError(f"Couldn't find {SCRIPT_NAME}.\nExpected at:\n{sp}")
        return _load_module_from_path("cut_silence_to_fcpxml", sp)

    def _build_argv(self, inp: str):
        argv = [
            inp,
            "--threshold", str(self.threshold.get()),
            "--min_silence", str(self.min_silence.get()),
            "--pad", str(self.pad.get()),
            "--min_keep", str(self.min_keep.get()),
        ]
        aud = self.audio_stream.get().strip()
        if aud:
            argv += ["--audio_stream", aud]
        if self.regen_mono.get():
            argv += ["--regen_mono"]
        return argv

    # ------------------------------------------------------------
    # Timeline analysis + draw
    # ------------------------------------------------------------
    def analyze_preview(self):
        if self._running:
            return

        inp = self.input_path.get().strip()
        if not inp or not os.path.isfile(inp):
            messagebox.showerror("Missing input", "Please choose a valid media file.")
            return

        self.preview_status.set("Analyzing...")
        self.timeline_canvas.delete("all")
        self._plan = None

        threshold = float(self.threshold.get())
        min_silence = float(self.min_silence.get())
        pad = float(self.pad.get())
        min_keep = float(self.min_keep.get())
        audio_stream = self.audio_stream.get().strip() or None

        def worker():
            buf = io.StringIO()
            try:
                mod = self._load_cutter()
                if not hasattr(mod, "compute_plan"):
                    raise RuntimeError("cut_silence_to_fcpxml.py is missing compute_plan(...). Update the cutter script.")

                with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                    plan = mod.compute_plan(inp, threshold, min_silence, pad, min_keep, audio_stream)

                out = buf.getvalue()
                if out:
                    self._log_q.put(out)

                self._plan = plan
                self._playhead_sec = 0.0
                self.after(0, self.draw_timeline)
            except Exception as e:
                out = buf.getvalue()
                if out:
                    self._log_q.put(out)
                self.after(0, lambda: self.preview_status.set("(Analysis failed)"))
                self.after(0, lambda: messagebox.showerror("Error", str(e)))

        threading.Thread(target=worker, daemon=True).start()

    def draw_timeline(self):
        plan = self._plan
        if not plan:
            return

        duration = float(plan["duration"])
        keeps = plan["keeps"]
        removes = plan["removes"]
        kept_total = float(plan["kept_total"])
        removed_total = float(plan["removed_total"])

        base_w = 3000
        scale = max(1.0, duration / 300.0)
        total_w = int(base_w * scale)
        self._timeline_total_w = max(1200, total_w)

        h = 64
        pad_y = 10
        bar_y0 = pad_y
        bar_y1 = h - pad_y

        self.timeline_canvas.config(scrollregion=(0, 0, self._timeline_total_w, h))
        self.timeline_canvas.delete("all")

        self.timeline_canvas.create_rectangle(0, bar_y0, self._timeline_total_w, bar_y1, outline="", fill="#222222")

        def x_of(t: float) -> float:
            if duration <= 0.0:
                return 0.0
            return (t / duration) * self._timeline_total_w

        for a, b in removes:
            x0 = x_of(a)
            x1 = x_of(b)
            if x1 > x0:
                self.timeline_canvas.create_rectangle(x0, bar_y0, x1, bar_y1, outline="", fill="#8a2d2d")

        for a, b in keeps:
            x0 = x_of(a)
            x1 = x_of(b)
            if x1 > x0:
                self.timeline_canvas.create_rectangle(x0, bar_y0, x1, bar_y1, outline="", fill="#2d8a45")

        if duration <= 120:
            step = 10
        elif duration <= 600:
            step = 30
        else:
            step = 60

        t = 0.0
        while t <= duration:
            x = x_of(t)
            self.timeline_canvas.create_line(x, bar_y1, x, bar_y1 + 6, fill="#aaaaaa")
            t += step

        self._draw_playhead()

        self.preview_status.set(
            f"Duration: {_sec_to_hhmmss(duration)} | Keep: {_sec_to_hhmmss(kept_total)} | Cut: {_sec_to_hhmmss(removed_total)} | Segments: {plan['keeps_count']}"
        )
        self.playhead_label.set(f"Playhead: {_sec_to_hhmmss(self._playhead_sec)}")

    def _draw_playhead(self):
        plan = self._plan
        if not plan:
            return

        duration = float(plan["duration"])
        if duration <= 0:
            return

        x = (self._playhead_sec / duration) * self._timeline_total_w
        self.timeline_canvas.delete("playhead")
        self.timeline_canvas.create_line(x, 0, x, 64, fill="#ffffff", width=2, tags=("playhead",))

    def on_timeline_click(self, ev):
        plan = self._plan
        if not plan:
            return

        duration = float(plan["duration"])
        if duration <= 0:
            return

        x = self.timeline_canvas.canvasx(ev.x)
        x = max(0.0, min(float(self._timeline_total_w), float(x)))
        self._playhead_sec = (x / float(self._timeline_total_w)) * duration
        self._draw_playhead()
        self.playhead_label.set(f"Playhead: {_sec_to_hhmmss(self._playhead_sec)}")

    # ------------------------------------------------------------
    # Run cutter (XML)
    # ------------------------------------------------------------
    def run(self):
        if self._running:
            return

        inp = self.input_path.get().strip()
        if not inp or not os.path.isfile(inp):
            messagebox.showerror("Missing input", "Please choose a valid media file.")
            return

        argv = self._build_argv(inp)
        self._append_log("\n---\nRunning...\n\n")
        self._set_running(True)

        def worker():
            buf = io.StringIO()
            try:
                mod = self._load_cutter()
                if not hasattr(mod, "main"):
                    raise RuntimeError("cut_silence_to_fcpxml.py does not expose main(argv).")

                with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                    rc = mod.main(argv)

                out = buf.getvalue()
                if out:
                    self._log_q.put(out)

                if rc is None:
                    rc = 0

                if rc == 0:
                    base = os.path.splitext(os.path.basename(inp))[0]
                    out_xml = os.path.join(os.path.dirname(os.path.abspath(inp)), f"{base}__nosilence.XML")
                    self._log_q.put(f"\nDone.\nXML:\n{out_xml}\n")
                    self.after(0, lambda: messagebox.showinfo("Done", f"Finished!\n\nXML:\n{out_xml}"))
                else:
                    self._log_q.put(f"\nFailed (exit code {rc}).\n")
                    self.after(0, lambda: messagebox.showerror("Error", f"Failed (exit code {rc}). See log."))
            except Exception as e:
                out = buf.getvalue()
                if out:
                    self._log_q.put(out)
                self._log_q.put(f"\nERROR: {e}\n")
                self.after(0, lambda: messagebox.showerror("Error", str(e)))
            finally:
                self.after(0, lambda: self._set_running(False))

        threading.Thread(target=worker, daemon=True).start()

    # ------------------------------------------------------------
    # VLC embedded playback
    # ------------------------------------------------------------
    def _init_vlc_if_available(self):
        if self._vlc is None:
            self._append_log(
                "[VLC] python-vlc not available or VLC DLLs not found.\n"
                f"      VLC dir detected: {getattr(self, '_vlc_dll_dir', None)}\n"
                f"      Error: {self._vlc_err}\n"
                "      Fix: install VLC, or set VLC_DIR env var to your VLC folder.\n\n"
            )

        try:
            self._vlc_instance = self._vlc.Instance()
            self._vlc_player = self._vlc_instance.media_player_new()

            self.update_idletasks()
            hwnd = self.player_surface.winfo_id()
            self._vlc_player.set_hwnd(hwnd)

            self._apply_volume()
            self._start_vlc_poll()
        except Exception as e:
            self._append_log(f"[VLC] Failed to initialize embedded player: {e}\n\n")
            self._vlc_instance = None
            self._vlc_player = None

    def load_in_player(self):
        inp = self.input_path.get().strip()
        if not inp or not os.path.isfile(inp):
            messagebox.showerror("Missing input", "Please choose a valid media file.")
            return
        self._vlc_load_media(inp)

    def _vlc_load_media(self, path: str):
        if not self._vlc_player or not self._vlc_instance:
            messagebox.showerror("VLC not ready", "Embedded player isn't available. Install VLC + python-vlc.")
            return

        try:
            self._vlc_media = self._vlc_instance.media_new(path)
            self._vlc_player.set_media(self._vlc_media)
            self._vlc_loaded_path = path
            self._vlc_duration_sec = 0.0
            self._vlc_is_playing = False

            # Prime playback briefly to populate duration (VLC often reports 0 until played)
            self._vlc_player.play()
            self.after(120, self._vlc_player.pause)

            self._append_log(f"[VLC] Loaded: {path}\n")
        except Exception as e:
            messagebox.showerror("Player error", str(e))

    def player_toggle_play(self):
        if not self._vlc_player:
            messagebox.showerror("VLC not ready", "Embedded player isn't available. Install VLC + python-vlc.")
            return
        try:
            if self._vlc_player.is_playing():
                self._vlc_player.pause()
            else:
                if self._vlc_loaded_path is None:
                    self.load_in_player()
                else:
                    self._vlc_player.play()
        except Exception as e:
            self._append_log(f"[VLC] Play/Pause error: {e}\n")

    def player_stop(self):
        if not self._vlc_player:
            return
        try:
            self._vlc_player.stop()
        except:
            pass

    def player_nudge(self, delta_sec: float):
        if not self._vlc_player:
            return
        try:
            ms = self._vlc_player.get_time()
            if ms < 0:
                ms = 0
            self._vlc_player.set_time(int(max(0, ms + delta_sec * 1000)))
        except:
            pass

    def seek_player_to_playhead(self):
        t = float(self._playhead_sec)
        if self._vlc_player and self._vlc_loaded_path:
            self._vlc_player.set_time(int(max(0.0, t) * 1000.0))
            return
        messagebox.showinfo("Player", "Load a file in the embedded player first.")

    def _apply_volume(self):
        if not self._vlc_player:
            return
        try:
            v = int(self.volume.get())
            self._vlc_player.audio_set_volume(max(0, min(100, v)))
        except:
            pass

    def on_volume_slider(self, v):
        try:
            self.volume.set(int(float(v)))
        except:
            return
        self._apply_volume()

    def _set_seek_drag(self, dragging: bool):
        self._seek_is_dragging = dragging
        if not dragging:
            self.on_seek_slider(self.seek_slider.get())

    def on_seek_slider(self, v):
        if not self._vlc_player or not self._vlc_loaded_path:
            return
        if self._seek_is_dragging:
            return

        try:
            frac = float(v)
            frac = max(0.0, min(1.0, frac))
            if self._vlc_duration_sec > 0:
                self._vlc_player.set_time(int(frac * self._vlc_duration_sec * 1000.0))
        except:
            pass

    def _start_vlc_poll(self):
        if self._vlc_poll_after_id is not None:
            self.after_cancel(self._vlc_poll_after_id)
        self._vlc_poll_after_id = self.after(100, self._vlc_poll)

    def _vlc_poll(self):
        self._vlc_poll_after_id = None
        if not self._vlc_player:
            return

        try:
            ms = self._vlc_player.get_time()
            if ms < 0:
                ms = 0

            len_ms = self._vlc_player.get_length()
            if len_ms and len_ms > 0:
                self._vlc_duration_sec = float(len_ms) / 1000.0

            cur = float(ms) / 1000.0
            dur = float(self._vlc_duration_sec)

            if dur > 0 and not self._seek_is_dragging:
                self.seek_slider.set(cur / dur)

            self.player_time_label.set(f"{_sec_to_hhmmss(cur)} / {_sec_to_hhmmss(dur)}")
        except:
            pass

        self._start_vlc_poll()


if __name__ == "__main__":
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except:
        pass

    App().mainloop()
