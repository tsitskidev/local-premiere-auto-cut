# silencecut_gui.py
# Tkinter UI that runs cut_silence_to_fcpxml.py in-process (no subprocess),
# plus a scrollable timeline preview of KEEP/CUT and ffplay preview from a playhead.

import contextlib
import importlib.util
import io
import os
import queue
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

SCRIPT_NAME = "cut_silence_to_fcpxml.py"


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


class App(tk.Tk):
    def __init__(self):
        super().__init__()

        self.title("SilenceCut → FCP7 XML")
        self.geometry("980x700")
        self.minsize(860, 600)

        self._log_q = queue.Queue()
        self._running = False

        self.input_path = tk.StringVar()

        self.threshold = tk.DoubleVar(value=-35.0)
        self.min_silence = tk.DoubleVar(value=0.25)
        self.pad = tk.DoubleVar(value=0.08)
        self.min_keep = tk.DoubleVar(value=0.10)
        self.audio_stream = tk.StringVar(value="")
        self.regen_mono = tk.BooleanVar(value=False)

        self._plan = None
        self._playhead_sec = 0.0
        self._timeline_total_w = 3000

        self._build_ui()
        self._tick_logs()

    def _build_ui(self):
        root = ttk.Frame(self, padding=14)
        root.pack(fill="both", expand=True)

        input_box = ttk.LabelFrame(root, text="Input", padding=12)
        input_box.pack(fill="x")

        row = ttk.Frame(input_box)
        row.pack(fill="x")

        ttk.Entry(row, textvariable=self.input_path).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="Browse…", command=self.pick_file).pack(side="left", padx=(10, 0))
        ttk.Button(row, text="Open Folder", command=self.open_folder).pack(side="left", padx=(10, 0))

        opts = ttk.LabelFrame(root, text="Options", padding=12)
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

        preview_box = ttk.LabelFrame(root, text="Preview", padding=12)
        preview_box.pack(fill="x", pady=(12, 0))

        btn_row = ttk.Frame(preview_box)
        btn_row.pack(fill="x")

        ttk.Button(btn_row, text="Analyze + Draw Timeline", command=self.analyze_preview).pack(side="left")
        ttk.Button(btn_row, text="Play From Playhead (ffplay)", command=self.play_from_playhead).pack(side="left", padx=(10, 0))

        self.preview_status = tk.StringVar(value="(No analysis yet)")
        ttk.Label(btn_row, textvariable=self.preview_status).pack(side="left", padx=(12, 0))

        self.playhead_label = tk.StringVar(value="Playhead: 0:00.000")
        ttk.Label(btn_row, textvariable=self.playhead_label).pack(side="right")

        # Scrollable timeline canvas
        timeline_frame = ttk.Frame(preview_box)
        timeline_frame.pack(fill="x", pady=(10, 0))

        self.timeline_canvas = tk.Canvas(timeline_frame, height=64, highlightthickness=1)
        self.timeline_canvas.pack(side="top", fill="x", expand=True)

        self.timeline_scroll = ttk.Scrollbar(timeline_frame, orient="horizontal", command=self.timeline_canvas.xview)
        self.timeline_scroll.pack(side="bottom", fill="x")

        self.timeline_canvas.configure(xscrollcommand=self.timeline_scroll.set)
        self.timeline_canvas.bind("<Button-1>", self.on_timeline_click)

        run_row = ttk.Frame(root)
        run_row.pack(fill="x", pady=(12, 0))

        self.run_btn = ttk.Button(run_row, text="Run (Generate XML)", command=self.run)
        self.run_btn.pack(side="left")

        self.progress = ttk.Progressbar(run_row, mode="indeterminate")
        self.progress.pack(side="left", fill="x", expand=True, padx=(14, 0))

        log_box = ttk.LabelFrame(root, text="Log", padding=10)
        log_box.pack(fill="both", expand=True, pady=(12, 0))

        self.log = tk.Text(log_box, height=12, wrap="word")
        self.log.pack(fill="both", expand=True)

        self._append_log(
            "Pick a video/audio file.\n"
            "- Use Analyze + Draw Timeline to preview what will be CUT (red) vs KEPT (green).\n"
            "- Click the timeline to set a playhead, then Play From Playhead.\n"
            "- Run generates the Premiere-importable XML next to the input file.\n\n"
        )

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

    def open_folder(self):
        p = self.input_path.get().strip()
        if not p:
            return
        folder = os.path.dirname(os.path.abspath(p))
        if not os.path.isdir(folder):
            return
        try:
            if sys.platform.startswith("win"):
                os.startfile(folder)
            elif sys.platform == "darwin":
                import subprocess
                subprocess.run(["open", folder], check=False)
            else:
                import subprocess
                subprocess.run(["xdg-open", folder], check=False)
        except Exception:
            pass

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

    def _build_argv(self, inp: str):
        argv = [
            inp,
            "--threshold",
            str(self.threshold.get()),
            "--min_silence",
            str(self.min_silence.get()),
            "--pad",
            str(self.pad.get()),
            "--min_keep",
            str(self.min_keep.get()),
        ]
        aud = self.audio_stream.get().strip()
        if aud:
            argv += ["--audio_stream", aud]
        if self.regen_mono.get():
            argv += ["--regen_mono"]
        return argv

    def _pretty_command(self, inp: str):
        parts = [
            "python",
            "cut_silence_to_fcpxml.py",
            f"\"{os.path.basename(inp)}\"",
            "--threshold",
            str(self.threshold.get()),
            "--min_silence",
            str(self.min_silence.get()),
            "--pad",
            str(self.pad.get()),
        ]
        if self.min_keep.get() != 0.10:
            parts += ["--min_keep", str(self.min_keep.get())]
        if self.audio_stream.get().strip():
            parts += ["--audio_stream", self.audio_stream.get().strip()]
        if self.regen_mono.get():
            parts += ["--regen_mono"]
        return " ".join(parts)

    def _load_cutter(self):
        sp = _script_path()
        if not os.path.isfile(sp):
            raise RuntimeError(f"Couldn't find {SCRIPT_NAME}.\nExpected at:\n{sp}")
        return _load_module_from_path("cut_silence_to_fcpxml", sp)

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

        argv_like = self._build_argv(inp)
        # Extract params from argv_like safely (since our cutter exposes compute_plan)
        threshold = float(argv_like[argv_like.index("--threshold") + 1])
        min_silence = float(argv_like[argv_like.index("--min_silence") + 1])
        pad = float(argv_like[argv_like.index("--pad") + 1])
        min_keep = float(argv_like[argv_like.index("--min_keep") + 1])

        audio_stream = None
        if "--audio_stream" in argv_like:
            audio_stream = argv_like[argv_like.index("--audio_stream") + 1]

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

        # Timeline sizing: long clips get longer scroll area
        base_w = 3000
        scale = max(1.0, duration / 300.0)  # every 5 minutes adds another base width
        total_w = int(base_w * scale)
        self._timeline_total_w = max(1200, total_w)

        h = 64
        pad_y = 10
        bar_y0 = pad_y
        bar_y1 = h - pad_y

        self.timeline_canvas.config(scrollregion=(0, 0, self._timeline_total_w, h))
        self.timeline_canvas.delete("all")

        # Background bar (neutral)
        self.timeline_canvas.create_rectangle(0, bar_y0, self._timeline_total_w, bar_y1, outline="", fill="#222222")

        def x_of(t: float) -> float:
            if duration <= 0.0:
                return 0.0
            return (t / duration) * self._timeline_total_w

        # Draw CUTS (red) first
        for a, b in removes:
            x0 = x_of(a)
            x1 = x_of(b)
            if x1 <= x0:
                continue
            self.timeline_canvas.create_rectangle(x0, bar_y0, x1, bar_y1, outline="", fill="#8a2d2d")

        # Draw KEEPS (green) on top
        for a, b in keeps:
            x0 = x_of(a)
            x1 = x_of(b)
            if x1 <= x0:
                continue
            self.timeline_canvas.create_rectangle(x0, bar_y0, x1, bar_y1, outline="", fill="#2d8a45")

        # Tick marks every 10s/30s/60s depending on duration
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

        # Playhead
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

    def play_from_playhead(self):
        inp = self.input_path.get().strip()
        if not inp or not os.path.isfile(inp):
            messagebox.showerror("Missing input", "Please choose a valid media file.")
            return

        # We rely on ffplay for preview (part of ffmpeg)
        # If ffplay isn't available, fall back to opening the file.
        t = max(0.0, float(self._playhead_sec))
        try:
            import subprocess
            p = subprocess.run(["ffplay", "-version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if p.returncode != 0:
                raise RuntimeError("ffplay not available")
            subprocess.Popen(["ffplay", "-hide_banner", "-autoexit", "-ss", f"{t:.3f}", "-i", inp])
        except Exception:
            try:
                if sys.platform.startswith("win"):
                    os.startfile(inp)
                elif sys.platform == "darwin":
                    import subprocess
                    subprocess.Popen(["open", inp])
                else:
                    import subprocess
                    subprocess.Popen(["xdg-open", inp])
            except Exception as e:
                messagebox.showerror("Error", f"Couldn't launch preview player.\n\n{e}")

    def run(self):
        if self._running:
            return

        inp = self.input_path.get().strip()
        if not inp or not os.path.isfile(inp):
            messagebox.showerror("Missing input", "Please choose a valid media file.")
            return

        self._append_log("\n---\nCommand:\n" + self._pretty_command(inp) + "\n\n")
        argv = self._build_argv(inp)

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
                    self._log_q.put(f"\nDone.\nXML should be here:\n{out_xml}\n")
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


if __name__ == "__main__":
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass

    App().mainloop()
