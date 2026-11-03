"""Where files live.

data/  next to the program (the folder with the .exe, or the repository root when run
       from source) holds everything that changes:
         jlcpcb_stackups.zip   stack database (Settings > Update stack database)
         rezonit_stackups.zip  Rezonit's typical builds, same format (`python -m jlc update --fab rezonit`)
         jlc_calc_cache.json   answers of JLC's Si9000 (grows with every online check)
         settings.json         the form
         cache/                computed widths, UI images
         logs/                 app.log, app_debug.log, crash.log
       If that folder cannot be written (e.g. under Program Files), the changing files go
       to the per-user folder instead; JLC_STACKUP_HOME sets another folder (tests, CI).
resources  read-only files inside the program (jlc/resources).
"""
import os
import sys

APP = "JLCPCB-Stackup-Solver"
FROZEN = getattr(sys, "frozen", False)
APP_DIR = (os.path.dirname(os.path.abspath(sys.executable)) if FROZEN
           else os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")))
SHIPPED = os.path.join(APP_DIR, "data")
RESOURCES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "resources")
DB = "jlcpcb_stackups.zip"
FABS = {"jlcpcb": DB, "rezonit": "rezonit_stackups.zip"}     # stack database of each factory


def _writable(d):
    probe = os.path.join(d, f".write_test_{os.getpid()}")
    try:
        os.makedirs(d, exist_ok=True)
        open(probe, "w").close()
        os.remove(probe)
        return True
    except OSError:
        return False


def _per_user():
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, APP)


if os.environ.get("JLC_STACKUP_HOME"):
    DATA = os.path.abspath(os.environ["JLC_STACKUP_HOME"])
else:
    DATA = SHIPPED if _writable(SHIPPED) else _per_user()
CACHE = os.path.join(DATA, "cache")
LOGS = os.path.join(DATA, "logs")


def data(name):
    """Path of a changing file in the data folder (the folder is created on demand)."""
    os.makedirs(DATA, exist_ok=True)
    return os.path.join(DATA, name)


def shipped(name):
    """A file that comes with the program and may be updated: the data-folder copy if there
    is one, else the one next to the program."""
    p = os.path.join(DATA, name)
    return p if os.path.exists(p) else os.path.join(SHIPPED, name)


def resource(name):
    return os.path.join(RESOURCES, name)
