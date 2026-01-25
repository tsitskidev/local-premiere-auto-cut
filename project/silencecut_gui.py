import contextlib
import datetime
import importlib.util
import io
import json
import os
import platform
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

DEFAULT_ZOOM_PX_PER_SEC = 20.0
MIN_ZOOM_PX_PER_SEC = 4.0
MAX_ZOOM_PX_PER_SEC = 220.0


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


def _add_vlc_dll_dir():
    paths = []

    vlc_dir_env = os.environ.get("VLC_DIR")
    if vlc_dir_env:
        paths.append(vlc_dir_env)

    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pfx86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")

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

        if os.path.isdir(plugins):
            os.environ["VLC_PLUGIN_PATH"] = plugins

        try:
            if hasattr(os, "add_dll_directory"):
                os.add_dll_directory(p)
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
    for p in _settings_path_candidates():
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

        self.title("SilenceCut")
        self.geometry("1280x780")
        self.minsize(1040, 640)

        self._log_q = queue.Queue()
        self._running = False

        self._restoring_session = True
        self._plan = None
        self._playhead_sec = 0.0

        self._px_per_sec = DEFAULT_ZOOM_PX_PER_SEC
        self._timeline_total_w = 2400
        self._overview_w = 900
        self._overview_h = 34
        self._overview_dragging = False
        self._overview_drag_off = 0.0

        self._settings_dirty = False
        self._settings_save_after_id = None
        self._auto_load_after_id = None
        self._last_auto_loaded_path = None

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
        self._vlc_loaded_path = None
        self._vlc_duration_sec = 0.0
        self._vlc_poll_after_id = None
        self._safe_seek_inflight = False

        self._load_settings_into_vars()
        self._build_ui()
        self._install_var_traces()
        self._tick_logs()

        self.after(250, self._restore_session_ui)
        self.bind_all("<space>", self._on_spacebar, add="+")

        self.after(150, self._init_vlc_if_available)
        self.after(200, self._schedule_auto_load)

    def _vlc_parse_duration_async(self, media):
        def worker():
            try:
                # Ask VLC to parse metadata so duration becomes available without playing.
                parse = getattr(media, "parse_with_options", None)
                if callable(parse):
                    # local = 1 in some builds; flags enum varies across VLC versions
                    flag = getattr(self._vlc, "MediaParseFlag", None)
                    local_flag = getattr(flag, "local", 1) if flag else 1
                    parse(local_flag, timeout=1500)
                else:
                    # Older VLC binding
                    media.parse()

                dur_ms = media.get_duration()
                if dur_ms and dur_ms > 0:
                    self._vlc_duration_sec = float(dur_ms) / 1000.0
                    self.after(0, self._draw_all_timelines)
            except:
                pass

        threading.Thread(target=worker, daemon=True).start()

    # ---------------- Settings ----------------
    def _collect_settings(self) -> dict:
        sess = {
            "px_per_sec": float(self._px_per_sec),
            "playhead_sec": float(self._playhead_sec),
            "xview": float(self.timeline_canvas.xview()[0]) if hasattr(self, "timeline_canvas") else 0.0,
        }

        path = self.input_path.get().strip()
        if path and os.path.isfile(path):
            try:
                st = os.stat(path)
                sess["file_size"] = int(st.st_size)
                sess["file_mtime"] = float(st.st_mtime)
            except:
                pass

        # Store analysis plan so reopening shows it instantly
        if self._plan:
            # Keep it JSON-safe and small
            sess["plan"] = {
                "duration": float(self._plan.get("duration", 0.0)),
                "kept_total": float(self._plan.get("kept_total", 0.0)),
                "removed_total": float(self._plan.get("removed_total", 0.0)),
                "keeps_count": int(self._plan.get("keeps_count", 0)),
                "keeps": [(float(a), float(b)) for a, b in self._plan.get("keeps", [])],
                "removes": [(float(a), float(b)) for a, b in self._plan.get("removes", [])],
            }

        return {
            "input_path": self.input_path.get(),
            "threshold": float(self.threshold.get()),
            "min_silence": float(self.min_silence.get()),
            "pad": float(self.pad.get()),
            "min_keep": float(self.min_keep.get()),
            "audio_stream": self.audio_stream.get(),
            "regen_mono": bool(self.regen_mono.get()),
            "volume": int(self.volume.get()),
            "session": sess,
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
        self._apply_settings(data if data else dict(DEFAULTS))

    def _mark_settings_dirty(self, *_):
        self._settings_dirty = True
        if self._settings_save_after_id is not None:
            self.after_cancel(self._settings_save_after_id)
        self._settings_save_after_id = self.after(300, self._flush_settings_save)

    def _flush_settings_save(self):
        self._settings_save_after_id = None
        if not self._settings_dirty:
            return
        self._settings_dirty = False
        _save_settings(self._collect_settings())

    def reset_defaults(self):
        self._apply_settings(dict(DEFAULTS))
        self._mark_settings_dirty()
        self._append_log("\n[UI] Reset settings to defaults.\n")
        self._schedule_auto_load()

    def _install_var_traces(self):
        for v in [self.input_path, self.threshold, self.min_silence, self.pad, self.min_keep, self.audio_stream,
                  self.regen_mono, self.volume]:
            try:
                v.trace_add("write", self._mark_settings_dirty)
            except:
                pass
        try:
            self.input_path.trace_add("write", lambda *_: self._schedule_auto_load())
        except:
            pass

    def _restore_session_ui(self):
        self._restoring_session = True
        data = _load_settings()
        sess = data.get("session", {}) if isinstance(data, dict) else {}

        # Restore zoom
        try:
            pps = float(sess.get("px_per_sec", DEFAULT_ZOOM_PX_PER_SEC))
            self._px_per_sec = max(MIN_ZOOM_PX_PER_SEC, min(MAX_ZOOM_PX_PER_SEC, pps))
        except:
            pass

        # Restore playhead
        try:
            self._playhead_sec = float(sess.get("playhead_sec", 0.0))
        except:
            self._playhead_sec = 0.0

        # Restore analysis plan if it matches the same media file (size+mtime check)
        try:
            path = self.input_path.get().strip()
            ok = True
            if path and os.path.isfile(path):
                self._vlc_load_media(path)
                st = os.stat(path)
                fs = sess.get("file_size", None)
                fm = sess.get("file_mtime", None)
                if fs is not None and int(fs) != int(st.st_size):
                    ok = False
                if fm is not None and abs(float(fm) - float(st.st_mtime)) > 0.5:
                    ok = False
            else:
                ok = False

            plan = sess.get("plan", None)
            if ok and isinstance(plan, dict):
                self._plan = {
                    "duration": float(plan.get("duration", 0.0)),
                    "kept_total": float(plan.get("kept_total", 0.0)),
                    "removed_total": float(plan.get("removed_total", 0.0)),
                    "keeps_count": int(plan.get("keeps_count", 0)),
                    "keeps": [(float(a), float(b)) for a, b in plan.get("keeps", [])],
                    "removes": [(float(a), float(b)) for a, b in plan.get("removes", [])],
                }

                dur = self._plan["duration"]
                self.preview_status.set(
                    f"Duration {_sec_to_hhmmss(dur)} · Keep {_sec_to_hhmmss(self._plan['kept_total'])} · "
                    f"Cut {_sec_to_hhmmss(self._plan['removed_total'])} · Segments {self._plan['keeps_count']}"
                )
            else:
                self._plan = None
        except:
            self._plan = None

        # Draw and restore scroll/viewport
        self._draw_all_timelines()
        try:
            xv = float(sess.get("xview", 0.0))
            xv = max(0.0, min(1.0, xv))
            self.timeline_canvas.xview_moveto(xv)
        except:
            pass

        # Ensure the video is loaded (you already auto-load via input_path changes)
        self._schedule_auto_load()
        self._restoring_session = False

    def _touch_session(self):
        self._mark_settings_dirty()

    def _on_spacebar(self, ev=None):
        self.player_toggle_play()
        self._touch_session()
        return "break"

    # ---------------- UI ----------------
    def _build_ui(self):
        root = ttk.Frame(self, padding=10)
        root.pack(fill="both", expand=True)

        layout = ttk.Panedwindow(root, orient="horizontal")
        layout.pack(fill="both", expand=True)

        left = ttk.Frame(layout, padding=10)
        layout.add(left, weight=0)
        left.configure(width=340)
        left.pack_propagate(False)

        right = ttk.Frame(layout, padding=10)
        layout.add(right, weight=1)

        file_box = ttk.LabelFrame(left, text="Media", padding=10)
        file_box.pack(fill="x")
        ttk.Entry(file_box, textvariable=self.input_path).pack(fill="x")
        btns = ttk.Frame(file_box)
        btns.pack(fill="x", pady=(8, 0))
        ttk.Button(btns, text="Browse…", command=self.pick_file).pack(side="left", fill="x", expand=True)
        ttk.Button(btns, text="Reset", command=self.reset_defaults).pack(side="left", padx=(8, 0), fill="x",
                                                                         expand=True)

        settings_box = ttk.LabelFrame(left, text="Cut Settings", padding=10)
        settings_box.pack(fill="x", pady=(10, 0))

        def row(label, var):
            rr = ttk.Frame(settings_box)
            rr.pack(fill="x", pady=4)
            ttk.Label(rr, text=label).pack(side="left")
            ttk.Entry(rr, textvariable=var, width=10).pack(side="right", fill="x", expand=True)

        row("Threshold (dB)", self.threshold)
        row("Min silence (s)", self.min_silence)
        row("Pad (s)", self.pad)
        row("Min keep (s)", self.min_keep)

        rr = ttk.Frame(settings_box)
        rr.pack(fill="x", pady=4)
        ttk.Label(rr, text="Audio stream").pack(side="left")
        ttk.Entry(rr, textvariable=self.audio_stream).pack(side="right", fill="x", expand=True)

        ttk.Checkbutton(settings_box, text="Re-create mono proxy", variable=self.regen_mono).pack(anchor="w",
                                                                                                  pady=(6, 0))

        actions = ttk.LabelFrame(left, text="Actions", padding=10)
        actions.pack(fill="x", pady=(10, 0))
        act_row = ttk.Frame(actions)
        act_row.pack(fill="x")
        ttk.Button(act_row, text="Analyze", command=self.analyze_preview).pack(side="left", fill="x", expand=True)
        ttk.Button(act_row, text="Generate XML", command=self.run).pack(side="left", padx=(8, 0), fill="x", expand=True)

        self.preview_status = tk.StringVar(value="(No analysis yet)")
        ttk.Label(actions, textvariable=self.preview_status, wraplength=300).pack(fill="x", pady=(8, 0))

        self.progress = ttk.Progressbar(left, mode="indeterminate")
        self.progress.pack(fill="x", pady=(10, 0))

        player_box = ttk.LabelFrame(right, text="Preview", padding=10)
        player_box.pack(fill="both", expand=True)

        self.player_surface = tk.Frame(player_box, bg="black", height=420)
        self.player_surface.pack(fill="x", expand=False)
        self.player_surface.pack_propagate(False)

        transport = ttk.Frame(player_box)
        transport.pack(fill="x", pady=(8, 0))

        ttk.Button(transport, text="⏯", width=4, command=self.player_toggle_play).pack(side="left")
        ttk.Button(transport, text="⏹", width=4, command=self.player_stop).pack(side="left", padx=(6, 0))

        ttk.Button(transport, text="Prev Cut", command=self.jump_prev_cut).pack(side="left", padx=(10, 0))
        ttk.Button(transport, text="Next Cut", command=self.jump_next_cut).pack(side="left", padx=(6, 0))

        ttk.Button(transport, text="⏮ 1s", width=6, command=lambda: self.player_nudge(-1.0)).pack(side="left",
                                                                                                  padx=(10, 0))
        ttk.Button(transport, text="1s ⏭", width=6, command=lambda: self.player_nudge(1.0)).pack(side="left",
                                                                                                 padx=(6, 0))

        ttk.Label(transport, text="Vol").pack(side="left", padx=(14, 6))
        self.vol_slider = ttk.Scale(transport, from_=0, to=100, orient="horizontal", command=self.on_volume_slider)
        self.vol_slider.pack(side="left", fill="x", expand=True)
        self.vol_slider.set(float(self.volume.get()))

        self.time_label = tk.StringVar(value="0:00.000 / 0:00.000")
        ttk.Label(transport, textvariable=self.time_label).pack(side="right")

        timeline_box = ttk.Frame(player_box)
        timeline_box.pack(fill="x", pady=(10, 0))

        self.timeline_canvas = tk.Canvas(timeline_box, height=66, highlightthickness=1)
        self.timeline_canvas.pack(side="top", fill="x", expand=True)
        self.timeline_canvas.bind("<Button-1>", self.on_timeline_click)
        self.timeline_canvas.bind("<Configure>", lambda e: self._draw_all_timelines())
        self.timeline_canvas.bind("<MouseWheel>", self.on_timeline_mousewheel)

        self.timeline_scroll = ttk.Scrollbar(timeline_box, orient="horizontal", command=self.timeline_canvas.xview)
        self.timeline_scroll.pack(side="bottom", fill="x")
        self.timeline_canvas.configure(xscrollcommand=self.timeline_scroll.set)

        # Premiere-style overview / dragger
        overview_box = ttk.Frame(player_box)
        overview_box.pack(fill="x", pady=(6, 0))

        self.overview_canvas = tk.Canvas(overview_box, height=self._overview_h, highlightthickness=1)
        self.overview_canvas.pack(fill="x")
        self.overview_canvas.bind("<Configure>", lambda e: self._draw_all_timelines())
        self.overview_canvas.bind("<Button-1>", self.on_overview_down)
        self.overview_canvas.bind("<B1-Motion>", self.on_overview_drag)
        self.overview_canvas.bind("<ButtonRelease-1>", self.on_overview_up)

        log_box = ttk.LabelFrame(right, text="Log", padding=10)
        log_box.pack(fill="both", expand=False, pady=(10, 0))
        self.log = tk.Text(log_box, height=10, wrap="word")
        self.log.pack(fill="both", expand=True)

        self._append_log(
            "Tips:\n"
            "- Pick a file: auto-loads into the player.\n"
            "- Ctrl + Mousewheel on the timeline to zoom.\n"
            "- Drag the small overview viewport like Premiere to pan.\n"
            "- Click the main timeline to seek.\n\n"
        )

        self._draw_all_timelines()

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
        if running:
            self.progress.start(10)
        else:
            self.progress.stop()

    # ---------------- File picker + auto-load ----------------
    def pick_file(self):
        path = filedialog.askopenfilename(
            title="Select video/audio file",
            filetypes=[("Media files", "*.mp4 *.mov *.mkv *.wav *.mp3 *.m4a *.aac *.flac *.avi"), ("All files", "*.*")],
        )
        if path:
            self.input_path.set(path)

    def _schedule_auto_load(self):
        if self._auto_load_after_id is not None:
            self.after_cancel(self._auto_load_after_id)
        self._auto_load_after_id = self.after(250, self._auto_load_if_needed)

    def _auto_load_if_needed(self):
        self._auto_load_after_id = None
        path = self.input_path.get().strip()
        if not path or not os.path.isfile(path):
            return
        if path == self._last_auto_loaded_path:
            return
        self._last_auto_loaded_path = path
        self._vlc_load_media(path)

    # ---------------- Cutter module ----------------
    def _load_cutter(self):
        sp = _script_path()
        if not os.path.isfile(sp):
            raise RuntimeError(f"Couldn't find {SCRIPT_NAME}.\nExpected at:\n{sp}")
        return _load_module_from_path("cut_silence_to_fcpxml", sp)

    def _build_argv(self, inp: str):
        argv = [inp, "--threshold", str(self.threshold.get()), "--min_silence", str(self.min_silence.get()), "--pad",
                str(self.pad.get()), "--min_keep", str(self.min_keep.get())]
        aud = self.audio_stream.get().strip()
        if aud:
            argv += ["--audio_stream", aud]
        if self.regen_mono.get():
            argv += ["--regen_mono"]
        return argv

    # ---------------- Timeline helpers ----------------
    def _timeline_duration(self) -> float:
        if self._plan and float(self._plan.get("duration", 0.0)) > 0:
            return float(self._plan["duration"])
        if self._vlc_duration_sec > 0:
            return float(self._vlc_duration_sec)
        return 0.0

    def _compute_timeline_width(self) -> int:
        dur = self._timeline_duration()
        if dur <= 0:
            return max(1200, int(self.timeline_canvas.winfo_width() or 1200))
        w = int(dur * float(self._px_per_sec))
        return max(1200, w)

    def _sec_to_x(self, t: float) -> float:
        dur = self._timeline_duration()
        if dur <= 0:
            return 0.0
        return (t / dur) * float(self._timeline_total_w)

    def _x_to_sec(self, x: float) -> float:
        dur = self._timeline_duration()
        if dur <= 0:
            return 0.0
        return (x / float(self._timeline_total_w)) * dur

    def _visible_range_sec(self) -> tuple[float, float]:
        dur = self._timeline_duration()
        if dur <= 0:
            return 0.0, 0.0
        x0 = self.timeline_canvas.canvasx(0)
        x1 = self.timeline_canvas.canvasx(self.timeline_canvas.winfo_width())
        return self._x_to_sec(x0), self._x_to_sec(x1)

    def _set_view_center_time(self, t_center: float):
        dur = self._timeline_duration()
        if dur <= 0:
            return
        w = float(self._timeline_total_w)
        canvas_w = max(1, float(self.timeline_canvas.winfo_width()))
        x_center = self._sec_to_x(t_center)
        x0 = x_center - canvas_w * 0.5
        x0 = max(0.0, min(w - canvas_w, x0))
        self.timeline_canvas.xview_moveto(x0 / w)

    # ---------------- Draw timelines ----------------
    def _draw_all_timelines(self):
        self._timeline_total_w = self._compute_timeline_width()
        self._draw_main_timeline()
        self._draw_overview()

    def _draw_main_timeline(self):
        c = self.timeline_canvas
        c.delete("all")
        h = 66
        pad_y = 10
        y0, y1 = pad_y, h - pad_y
        w = self._timeline_total_w

        c.config(scrollregion=(0, 0, w, h))
        c.create_rectangle(0, y0, w, y1, outline="", fill="#222222")

        dur = self._timeline_duration()
        if dur <= 0:
            c.create_text(12, h // 2, anchor="w", fill="#bbbbbb", text="(load a file to enable timeline)")
            return

        if self._plan:
            removes = self._plan["removes"]
            keeps = self._plan["keeps"]
            for a, b in removes:
                x0, x1 = self._sec_to_x(a), self._sec_to_x(b)
                if x1 > x0:
                    c.create_rectangle(x0, y0, x1, y1, outline="", fill="#8a2d2d")
            for a, b in keeps:
                x0, x1 = self._sec_to_x(a), self._sec_to_x(b)
                if x1 > x0:
                    c.create_rectangle(x0, y0, x1, y1, outline="", fill="#2d8a45")

        step = 10 if dur <= 120 else 30 if dur <= 600 else 60
        t = 0.0
        while t <= dur:
            x = self._sec_to_x(t)
            c.create_line(x, y1, x, y1 + 6, fill="#aaaaaa")
            t += step

        xph = self._sec_to_x(self._playhead_sec)
        c.create_line(xph, 0, xph, h, fill="#ffffff", width=2, tags=("playhead",))

    def _draw_overview(self):
        o = self.overview_canvas
        o.delete("all")

        ow = max(1, int(o.winfo_width() or self._overview_w))
        oh = self._overview_h
        self._overview_w = ow

        o.config(scrollregion=(0, 0, ow, oh))
        o.create_rectangle(0, 0, ow, oh, outline="", fill="#1b1b1b")

        dur = self._timeline_duration()
        if dur <= 0:
            o.create_text(10, oh // 2, anchor="w", fill="#bbbbbb", text="(overview)")
            return

        def ox(t: float) -> float:
            return (t / dur) * ow

        if self._plan:
            for a, b in self._plan["removes"]:
                x0, x1 = ox(a), ox(b)
                if x1 > x0:
                    o.create_rectangle(x0, 6, x1, oh - 6, outline="", fill="#6c2a2a")
            for a, b in self._plan["keeps"]:
                x0, x1 = ox(a), ox(b)
                if x1 > x0:
                    o.create_rectangle(x0, 6, x1, oh - 6, outline="", fill="#2a6c3f")

        # Viewport rectangle
        v0, v1 = self._visible_range_sec()
        vx0, vx1 = ox(v0), ox(v1)
        vx1 = max(vx1, vx0 + 12)

        o.create_rectangle(vx0, 3, vx1, oh - 3, outline="#dddddd", width=2, fill="")
        o.create_rectangle(vx0, 3, vx1, oh - 3, outline="", fill="#ffffff", stipple="gray12")

        # Playhead
        px = ox(self._playhead_sec)
        o.create_line(px, 0, px, oh, fill="#ffffff", width=1)

    # ---------------- Interactions ----------------
    def on_timeline_click(self, ev):
        dur = self._timeline_duration()
        if dur <= 0:
            return
        x = self.timeline_canvas.canvasx(ev.x)
        x = max(0.0, min(float(self._timeline_total_w), float(x)))
        t = self._x_to_sec(x)
        self._playhead_sec = float(t)
        self._safe_seek_player(self._playhead_sec)
        self._draw_all_timelines()

    def on_timeline_mousewheel(self, ev):
        # Ctrl+wheel zoom (premiere-ish)
        if not (ev.state & 0x0004):  # Control key
            return

        dur = self._timeline_duration()
        if dur <= 0:
            return

        x = self.timeline_canvas.canvasx(ev.x)
        t_anchor = self._x_to_sec(x)

        delta = ev.delta
        zoom_mul = 1.12 if delta > 0 else (1.0 / 1.12)

        new_pps = float(self._px_per_sec) * zoom_mul
        new_pps = max(MIN_ZOOM_PX_PER_SEC, min(MAX_ZOOM_PX_PER_SEC, new_pps))
        if abs(new_pps - self._px_per_sec) < 0.001:
            return

        self._px_per_sec = new_pps
        self._timeline_total_w = self._compute_timeline_width()
        self._draw_main_timeline()
        self._set_view_center_time(t_anchor)
        self._draw_overview()

    def on_overview_down(self, ev):
        dur = self._timeline_duration()
        if dur <= 0:
            return
        self._overview_dragging = True

        # drag so click position becomes center of viewport
        x = float(ev.x)
        v0, v1 = self._visible_range_sec()
        vw = max(0.01, v1 - v0)
        center = (v0 + v1) * 0.5
        cx = (center / dur) * float(self._overview_w)
        self._overview_drag_off = x - cx
        self._overview_jump_to_x(x)

    def on_overview_drag(self, ev):
        if not self._overview_dragging:
            return
        self._overview_jump_to_x(float(ev.x))

    def on_overview_up(self, ev):
        self._overview_dragging = False

    def _overview_jump_to_x(self, x: float):
        dur = self._timeline_duration()
        if dur <= 0:
            return
        x = x - float(self._overview_drag_off)
        x = max(0.0, min(float(self._overview_w), x))
        t_center = (x / float(self._overview_w)) * dur
        self._set_view_center_time(t_center)
        self._draw_overview()

    # ---------------- Analyze ----------------
    def analyze_preview(self):
        if self._running:
            return

        inp = self.input_path.get().strip()
        if not inp or not os.path.isfile(inp):
            messagebox.showerror("Missing input", "Please choose a valid media file.")
            return

        self.preview_status.set("Analyzing…")
        self._plan = None
        self._draw_all_timelines()

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
                    raise RuntimeError("cut_silence_to_fcpxml.py is missing compute_plan(...).")

                with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                    plan = mod.compute_plan(inp, threshold, min_silence, pad, min_keep, audio_stream)

                out = buf.getvalue()
                if out:
                    self._log_q.put(out)

                self._plan = plan
                dur = float(plan["duration"])
                kept_total = float(plan["kept_total"])
                removed_total = float(plan["removed_total"])
                segs = plan["keeps_count"]

                self.after(0, lambda: self.preview_status.set(
                    f"Duration {_sec_to_hhmmss(dur)} · Keep {_sec_to_hhmmss(kept_total)} · Cut {_sec_to_hhmmss(removed_total)} · Segments {segs}"
                ))
                self.after(0, self._draw_all_timelines)

            except Exception as e:
                out = buf.getvalue()
                if out:
                    self._log_q.put(out)
                self.after(0, lambda: self.preview_status.set("(Analysis failed)"))
                self.after(0, lambda: messagebox.showerror("Error", str(e)))

        threading.Thread(target=worker, daemon=True).start()

    # ---------------- Jump buttons ----------------
    def _cuts(self) -> list[tuple[float, float]]:
        if not self._plan:
            return []
        cuts = [(float(a), float(b)) for a, b in self._plan.get("removes", []) if float(b) > float(a)]
        cuts.sort(key=lambda x: x[0])
        return cuts

    def jump_next_cut(self):
        cuts = self._cuts()
        if not cuts:
            return

        eps = 0.001  # 1ms - prevents re-selecting the same boundary
        t = float(self._playhead_sec)

        for a, b in cuts:
            # If we're inside a cut, jump to its end (plus eps)
            if a <= t < b:
                nt = min(self._timeline_duration(), b + eps)
                self._playhead_sec = nt
                self._safe_seek_player(nt)
                self._set_view_center_time(nt)
                self._draw_all_timelines()
                return

            # Otherwise jump to the next cut start (plus eps)
            if a > t + eps:
                nt = min(self._timeline_duration(), a + eps)
                self._playhead_sec = nt
                self._safe_seek_player(nt)
                self._set_view_center_time(nt)
                self._draw_all_timelines()
                return

        # If we're past the last cut, go to end
        dur = self._timeline_duration()
        if dur > 0:
            nt = max(0.0, dur - eps)
            self._playhead_sec = nt
            self._safe_seek_player(nt)
            self._set_view_center_time(nt)
            self._draw_all_timelines()

    def jump_prev_cut(self):
        cuts = self._cuts()
        if not cuts:
            return

        eps = 0.001
        t = float(self._playhead_sec)

        # If inside a cut, jump to its start (minus eps)
        for a, b in cuts:
            if a <= t < b:
                nt = max(0.0, a - eps)
                self._playhead_sec = nt
                self._safe_seek_player(nt)
                self._set_view_center_time(nt)
                self._draw_all_timelines()
                return

        # Otherwise jump to the previous cut start
        prev_a = None
        for a, _b in cuts:
            if a < t - eps:
                prev_a = a
            else:
                break

        if prev_a is not None:
            nt = max(0.0, prev_a - eps)
            self._playhead_sec = nt
            self._safe_seek_player(nt)
            self._set_view_center_time(nt)
            self._draw_all_timelines()
            return

        # If we're before the first cut, go to 0
        self._playhead_sec = 0.0
        self._safe_seek_player(0.0)
        self._set_view_center_time(0.0)
        self._draw_all_timelines()

    # ---------------- Generate XML ----------------
    def run(self):
        if self._running:
            return

        inp = self.input_path.get().strip()
        if not inp or not os.path.isfile(inp):
            messagebox.showerror("Missing input", "Please choose a valid media file.")
            return

        argv = self._build_argv(inp)
        self._append_log("\n---\nRunning…\n\n")
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

    # ---------------- VLC embedded playback ----------------
    def _init_vlc_if_available(self):
        if self._vlc is None:
            self._append_log(
                "[VLC] Not available.\n"
                f"      VLC dir detected: {self._vlc_dll_dir}\n"
                f"      Error: {self._vlc_err}\n\n"
            )
            return

        try:
            self._vlc_instance = self._vlc.Instance()
            self._vlc_player = self._vlc_instance.media_player_new()
            self.update_idletasks()
            self._vlc_player.set_hwnd(self.player_surface.winfo_id())
            self._apply_volume()
            self._start_vlc_poll()
        except Exception as e:
            self._append_log(f"[VLC] Failed to initialize embedded player: {e}\n\n")
            self._vlc_instance = None
            self._vlc_player = None

    def _vlc_load_media(self, path: str):
        if not self._vlc_player or not self._vlc_instance:
            return

        try:
            if self._vlc_loaded_path == path:
                # Same file: do not nuke plan; just ensure timelines are drawn
                self._draw_all_timelines()
                return

            self._vlc_player.stop()
            media = self._vlc_instance.media_new(path)
            self._vlc_player.set_media(media)

            self._vlc_loaded_path = path
            self._vlc_duration_sec = 0.0

            # IMPORTANT: don't clear plan when restoring session
            if not getattr(self, "_restoring_session", False):
                self._plan = None
                self.preview_status.set("(No analysis yet)")
                self._playhead_sec = 0.0

            # If you added the parse-duration async helper, call it here:
            # self._vlc_parse_duration_async(media)

            self._draw_all_timelines()
            self._append_log(f"[VLC] Loaded: {path}\n")

        except Exception as e:
            self._append_log(f"[VLC] Load error: {e}\n")

    def player_toggle_play(self):
        if not self._vlc_player:
            return

        try:
            if self._vlc_player.is_playing():
                self._vlc_player.pause()
                return

            if self._vlc_loaded_path and os.path.isfile(self._vlc_loaded_path):
                self._vlc_player.play()
                return

            p = self.input_path.get().strip()
            if p and os.path.isfile(p):
                self._vlc_load_media(p)
                self.after(60, lambda: self._vlc_player.play())
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
        t = max(0.0, self._current_player_time_sec() + float(delta_sec))
        self._playhead_sec = t
        self._safe_seek_player(t)
        self._set_view_center_time(t)
        self._draw_all_timelines()

    def _current_player_time_sec(self) -> float:
        if not self._vlc_player:
            return 0.0
        try:
            ms = self._vlc_player.get_time()
            if ms is None or ms < 0:
                return 0.0
            return float(ms) / 1000.0
        except:
            return 0.0

    def _safe_seek_player(self, t_sec: float):
        if not self._vlc_player or self._safe_seek_inflight:
            return

        self._safe_seek_inflight = True
        try:
            was_playing = bool(self._vlc_player.is_playing())
        except:
            was_playing = False

        try:
            if was_playing:
                self._vlc_player.pause()
            self._vlc_player.set_time(int(max(0.0, t_sec) * 1000.0))
        except:
            pass

        def resume():
            try:
                if was_playing:
                    self._vlc_player.play()
            except:
                pass
            self._safe_seek_inflight = False

        self.after(80, resume)

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

    def _start_vlc_poll(self):
        if self._vlc_poll_after_id is not None:
            self.after_cancel(self._vlc_poll_after_id)
        self._vlc_poll_after_id = self.after(100, self._vlc_poll)

    def _vlc_poll(self):
        self._vlc_poll_after_id = None
        if not self._vlc_player:
            return

        try:
            len_ms = self._vlc_player.get_length()
            if len_ms and len_ms > 0:
                self._vlc_duration_sec = float(len_ms) / 1000.0

            cur = self._current_player_time_sec()
            dur = float(self._vlc_duration_sec)

            if dur > 0:
                self.time_label.set(f"{_sec_to_hhmmss(cur)} / {_sec_to_hhmmss(dur)}")
                self._playhead_sec = cur
                self._draw_all_timelines()
            else:
                self.time_label.set(f"{_sec_to_hhmmss(cur)} / 0:00.000")

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
