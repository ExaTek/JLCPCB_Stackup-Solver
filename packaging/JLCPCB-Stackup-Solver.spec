# PyInstaller spec: a self-contained program folder
#   JLCPCB-Stackup-Solver/
#     JLCPCB-Stackup-Solver[.exe]   the program
#     lib/                          Python, Qt, numpy/scipy (do not touch)
#     data/                         stack database and everything that changes (see jlc/paths.py)
# Build from the repository root:
#   pip install -r requirements.txt pyinstaller
#   pyinstaller --noconfirm packaging/JLCPCB-Stackup-Solver.spec      ->  dist/JLCPCB-Stackup-Solver/
import glob
import os
import shutil
import sys

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
NAME = "JLCPCB-Stackup-Solver"

binaries = []
if sys.platform.startswith("linux"):
    # xcb helpers Qt needs and many desktops lack ("could not load the Qt platform plugin xcb")
    for lib in ("libxcb-cursor.so.0", "libxcb-icccm.so.4", "libxcb-image.so.0", "libxcb-keysyms.so.1",
                "libxcb-render-util.so.0", "libxkbcommon-x11.so.0"):
        binaries += [(p, ".") for p in glob.glob(f"/usr/lib/*-linux-gnu/{lib}")[:1]]

a = Analysis([os.path.join(ROOT, "jlc", "__main__.py")], pathex=[ROOT], binaries=binaries,
             datas=[(os.path.join(ROOT, "jlc", "resources"), "jlc/resources")],
             excludes=["tkinter", "matplotlib", "PIL", "IPython", "pytest", "pandas"])
# a Widgets app needs neither the software OpenGL renderer nor Qt's own translations (~27 MB)
a.binaries = [b for b in a.binaries if "opengl32sw" not in b[0]]
a.datas = [d for d in a.datas if "/translations/" not in d[0].replace("\\", "/")]
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name=NAME, console=False, upx=False,
          icon=os.path.join(ROOT, "jlc", "resources", "app.ico") if sys.platform == "win32" else None,
          contents_directory="lib")
COLLECT(exe, a.binaries, a.datas, name=NAME, upx=False)

# the data folder lives next to the program, not inside it: it is updated at run time
out = os.path.join(DISTPATH, NAME, "data")
os.makedirs(out, exist_ok=True)
for n in ("jlcpcb_stackups.zip", "jlc_calc_cache.json"):
    shutil.copy2(os.path.join(ROOT, "data", n), out)
