"""The tray/reader roles must not preload QtCore via PyInstaller's Qt hook."""
import sys

if not any(arg == "--panel" or arg == "--snapshot" or arg.startswith("--snapshot=")
           for arg in sys.argv[1:]):
    from _pyi_rth_utils import qt
    # The on-demand panel is another process, where the original hook runs normally.
    qt.create_embedded_qt_conf = lambda *args: None
