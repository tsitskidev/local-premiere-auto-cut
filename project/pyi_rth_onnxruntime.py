# PyInstaller runtime hook: register onnxruntime DLL directories.
# Runs before any user code so the DLLs are findable when onnxruntime is imported.
import os
import sys

if hasattr(sys, '_MEIPASS'):
    _base = sys._MEIPASS
    for _d in [_base,
               os.path.join(_base, 'onnxruntime'),
               os.path.join(_base, 'onnxruntime', 'capi')]:
        if not os.path.isdir(_d):
            continue
        if hasattr(os, 'add_dll_directory'):
            try:
                os.add_dll_directory(_d)
            except Exception:
                pass
        os.environ['PATH'] = _d + os.pathsep + os.environ.get('PATH', '')
