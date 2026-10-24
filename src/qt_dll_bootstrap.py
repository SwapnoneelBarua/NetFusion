"""Register bundled Qt and Shiboken DLL locations before importing PySide6.

PyInstaller's Windows bootloader does not make nested DLL directories
available to Python extension modules on every supported Windows setup.
Keep the AddDllDirectory handles alive for the life of the process.
"""
import os
import sys
from pathlib import Path


if sys.platform == "win32" and getattr(sys, "frozen", False):
    bundle_dir = Path(sys._MEIPASS)
    dll_dirs = (
        bundle_dir / "PySide6",
        bundle_dir / "shiboken6",
        bundle_dir,
    )
    _dll_directory_handles = []
    for dll_dir in dll_dirs:
        if dll_dir.is_dir():
            _dll_directory_handles.append(os.add_dll_directory(str(dll_dir)))

    pyside_dir = bundle_dir / "PySide6"
    os.environ["PATH"] = os.pathsep.join(
        [*(str(path) for path in dll_dirs if path.is_dir()), os.environ.get("PATH", "")]
    )

    plugin_dir = pyside_dir / "plugins"
    if plugin_dir.is_dir():
        os.environ.setdefault("QT_PLUGIN_PATH", str(plugin_dir))
        os.environ.setdefault("QT_QPA_PLATFORM_PLUGIN_PATH", str(plugin_dir / "platforms"))
