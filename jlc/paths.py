"""Where files live.

data()  read-only resources shipped with the app: data/ next to the package, or inside
        the PyInstaller bundle (sys._MEIPASS). `jlcstack.py update` refreshes them in a
        source checkout.
user()  the per-user folder for settings, caches and logs (created on demand):
        Windows %LOCALAPPDATA%\\JLCPCB-Stackup-Solver, Linux ~/.local/share/JLCPCB-Stackup-Solver
        (or $XDG_DATA_HOME); JLC_STACKUP_HOME overrides it (portable use, tests).
"""
import os
import shutil
import sys

APP = "JLCPCB-Stackup-Solver"
FROZEN = getattr(sys, "frozen", False)
ROOT = getattr(sys, "_MEIPASS", None) or os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
DATA = os.path.join(ROOT, "data")


def _user_dir():
    if os.environ.get("JLC_STACKUP_HOME"):
        return os.path.abspath(os.environ["JLC_STACKUP_HOME"])
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or os.path.expanduser("~")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, APP)


USER = _user_dir()
LOGS = os.path.join(USER, "logs")


def data(name):
    return os.path.join(DATA, name)


def user(name):
    os.makedirs(USER, exist_ok=True)
    return os.path.join(USER, name)


def migrate():
    """Settings and caches used to live in data/ of the source checkout: take them over once
    (not into an explicit JLC_STACKUP_HOME, which starts clean)."""
    if os.environ.get("JLC_STACKUP_HOME"):
        return
    for name in ("app_state.json", "engine_cache.json"):
        old, new = data(name), os.path.join(USER, name)
        if os.path.exists(old) and not os.path.exists(new):
            try:
                shutil.copy2(old, user(name))
            except OSError:
                pass
