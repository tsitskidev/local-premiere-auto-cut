"""
SilenceCut launcher.
Finds system Python and runs silencecut_gui.py from the same directory.
"""
import os
import sys
import subprocess
import ctypes


def _msg(text):
    ctypes.windll.user32.MessageBoxW(0, text, "SilenceCut", 0x10)


def _find_python():
    import shutil
    for name in ("pythonw", "python"):
        exe = shutil.which(name)
        if exe:
            if name == "python":
                pw = os.path.join(os.path.dirname(exe), "pythonw.exe")
                if os.path.isfile(pw):
                    return pw
            return exe
    return None


here = os.path.dirname(os.path.abspath(sys.argv[0]))
script = os.path.join(here, "silencecut_gui.py")

if not os.path.isfile(script):
    _msg(f"silencecut_gui.py not found next to the launcher.\nExpected:\n{script}")
    sys.exit(1)

py = _find_python()
if not py:
    _msg("Python not found on PATH.\nInstall Python 3 and make sure it is on PATH.")
    sys.exit(1)

try:
    subprocess.Popen([py, script], cwd=here)
except Exception as e:
    _msg(f"Failed to launch Python:\n{py}\n\n{e}")
