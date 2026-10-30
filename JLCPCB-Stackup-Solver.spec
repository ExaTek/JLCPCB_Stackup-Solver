# PyInstaller spec: one self-contained executable per OS (Python, Qt, numpy/scipy and the JLC data inside).
#   pip install -r requirements.txt pyinstaller
#   pyinstaller --noconfirm JLCPCB-Stackup-Solver.spec      ->  dist/JLCPCB-Stackup-Solver[.exe]
import glob
import sys

datas = [(f"data/{n}", "data") for n in ("templates.json.gz", "order_options.json", "configs.json",
                                         "price_request.json", "jlc_calc_cache.json", "app.ico")]
binaries = []
if sys.platform.startswith("linux"):
    # xcb helpers Qt needs and many desktops lack ("could not load the Qt platform plugin xcb")
    for lib in ("libxcb-cursor.so.0", "libxcb-icccm.so.4", "libxcb-image.so.0", "libxcb-keysyms.so.1",
                "libxcb-render-util.so.0", "libxkbcommon-x11.so.0"):
        binaries += [(p, ".") for p in glob.glob(f"/usr/lib/*-linux-gnu/{lib}")[:1]]

a = Analysis(["jlcapp.pyw"], pathex=["."], binaries=binaries, datas=datas,
             excludes=["tkinter", "matplotlib", "PIL", "IPython", "pytest", "pandas"])
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name="JLCPCB-Stackup-Solver", console=False,
          icon="data/app.ico" if sys.platform == "win32" else None, upx=False)
