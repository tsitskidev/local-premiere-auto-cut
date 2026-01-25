# silencecut_gui.py
# A single-file Tkinter UI that runs cut_silence_to_fcpxml.py *in-process* (no subprocess),
# so the packaged EXE won't spawn a second GUI window.

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
    # When frozen with PyInstaller, files added via --add-data live in sys._MEIPASS.
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


class App(tk.Tk):
    def __init__(self):
        super().__init__()

        self.title("SilenceCut → FCP7 XML")
        self.geometry("820x540")
        self.minsize(760, 480)

        self._log_q = queue.Queue()
        self._running = False

        self.input_path = tk.StringVar()

        # Defaults (tweak as you like)
        self.threshold = tk.DoubleVar(value=-35.0)
        self.min_silence = tk.DoubleVar(value=0.25)
        self.pad = tk.DoubleVar(value=0.08)
        self.min_keep = tk.DoubleVar(value=0.10)
        self.audio_stream = tk.StringVar(value="")
        self.regen_mono = tk.BooleanVar(value=False)

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

        run_row = ttk.Frame(root)
        run_row.pack(fill="x", pady=(12, 0))

        self.run_btn = ttk.Button(run_row, text="Run", command=self.run)
        self.run_btn.pack(side="left")

        self.progress = ttk.Progressbar(run_row, mode="indeterminate")
        self.progress.pack(side="left", fill="x", expand=True, padx=(14, 0))

        log_box = ttk.LabelFrame(root, text="Log", padding=10)
        log_box.pack(fill="both", expand=True, pady=(12, 0))

        self.log = tk.Text(log_box, height=12, wrap="word")
        self.log.pack(fill="both", expand=True)

        self._append_log(
            "Pick a video/audio file, then click Run.\n"
            "The XML will be created next to the input file.\n\n"
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
        # Show the command exactly how you described.
        parts = [
            "python",
            "cut_silence_to_fcpxml.py",
            f"\"{os.path.basename(inp)}\"" if " " in os.path.basename(inp) else f"\"{os.path.basename(inp)}\"",
            "--threshold",
            str(self.threshold.get()),
            "--min_silence",
            str(self.min_silence.get()),
            "--pad",
            str(self.pad.get()),
        ]
        # (min_keep + optional args are still passed; this is just display)
        if self.min_keep.get() != 0.10:
            parts += ["--min_keep", str(self.min_keep.get())]
        if self.audio_stream.get().strip():
            parts += ["--audio_stream", self.audio_stream.get().strip()]
        if self.regen_mono.get():
            parts += ["--regen_mono"]
        return " ".join(parts)

    def run(self):
        if self._running:
            return

        inp = self.input_path.get().strip()
        if not inp or not os.path.isfile(inp):
            messagebox.showerror("Missing input", "Please choose a valid media file.")
            return

        sp = _script_path()
        if not os.path.isfile(sp):
            messagebox.showerror("Script not found", f"Couldn't find {SCRIPT_NAME}.\n\nExpected at:\n{sp}")
            return

        self._append_log("\n---\nCommand:\n" + self._pretty_command(inp) + "\n\n")
        argv = self._build_argv(inp)

        self._set_running(True)

        def worker():
            buf = io.StringIO()
            try:
                mod = _load_module_from_path("cut_silence_to_fcpxml", sp)

                if not hasattr(mod, "main"):
                    # In an EXE, running via subprocess will re-launch this GUI exe (bad),
                    # so we require a callable main(argv) in the script.
                    raise RuntimeError(
                        "cut_silence_to_fcpxml.py does not expose main(argv).\n\n"
                        "Add a main(argv=None) function to the script and call it from __main__."
                    )

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
    # DPI awareness (Windows) so the UI doesn't look blurry.
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass

    App().mainloop()
