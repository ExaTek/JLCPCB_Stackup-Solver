"""Program update from GitHub releases.

check()     latest release: version, page, assets (network, call from a thread).
prepare()   download the archive of a release, verify it, unpack it into <program>/_update.
swap()      start a small script that waits for this program to exit, replaces the program
            file and lib/ and starts the new version.

data/ stays as it is: only files the new version ships and the folder lacks are added, so the
stack database refreshed from JLC, the settings, the caches and the logs are never overwritten.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.parse
import urllib.request
import zipfile

from . import __version__
from . import paths as P

REPO = "https://github.com/ExaTek/JLCPCB_Stackup-Solver"
API = "https://api.github.com/repos/ExaTek/JLCPCB_Stackup-Solver/releases/latest"
HEADERS = {"User-Agent": f"{P.APP}/{__version__}", "Accept": "application/vnd.github+json"}
STAGE = "_update"                       # inside the program folder, so the swap is a rename on one disk
LOCAL = ("127.0.0.1", "localhost")


def _source():
    """(API url, prefix every download must start with). JLC_UPDATE_API points the update at a test
    server on this computer; any other host is ignored."""
    api = os.environ.get("JLC_UPDATE_API", "")
    if urllib.parse.urlsplit(api).hostname in LOCAL:
        return api, api.rsplit("/", 1)[0] + "/"
    return API, f"{REPO}/releases/download/"


def version_key(v):
    """'v1.10.0' -> (1, 10, 0); anything else -> None."""
    m = re.fullmatch(r"v?(\d+(?:\.\d+)*)", (v or "").strip())
    if not m:
        return None
    k = [int(x) for x in m.group(1).split(".")]
    return tuple(k + [0] * (3 - len(k)))


def is_newer(latest, current=__version__):
    a, b = version_key(latest), version_key(current)
    return bool(a and b and a > b)


def asset_name(version):
    """Archive of this system for a version; None where no build is published."""
    suffix = {"win32": "windows-x64.zip", "linux": "linux-x64.tar.gz"}.get(sys.platform)
    return f"{P.APP}-v{version}-{suffix}" if suffix else None


def check(timeout=10):
    api, _ = _source()
    with urllib.request.urlopen(urllib.request.Request(api, headers=HEADERS), timeout=timeout) as r:
        rel = json.load(r)
    tag = rel.get("tag_name") or ""
    if not version_key(tag):
        raise ValueError(f"unexpected release tag {tag!r}")
    return {"version": tag.lstrip("v"), "page": rel.get("html_url") or f"{REPO}/releases/latest",
            "assets": {a["name"]: a for a in rel.get("assets", [])}}


def cannot_install(info):
    """Why this copy cannot replace itself (then the release page is opened instead); None if it can."""
    if not P.FROZEN:
        return "запуск из исходников"
    name = asset_name(info["version"])
    if not name:
        return "для этой системы нет готовой сборки"
    if name not in info["assets"]:
        return f"в релизе нет файла {name}"
    if not P._writable(os.path.join(P.APP_DIR, STAGE)):
        return "нет права записи в папку программы"
    return None


def _download(asset, path, prefix, progress):
    url = asset["browser_download_url"]
    if not url.startswith(prefix):
        raise ValueError(f"unexpected download address {url}")
    total = int(asset.get("size") or 0)
    sha, done = hashlib.sha256(), 0
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": HEADERS["User-Agent"]}),
                                timeout=60) as r, open(path, "wb") as f:
        while True:
            block = r.read(1 << 18)
            if not block:
                break
            f.write(block)
            sha.update(block)
            done += len(block)
            progress(done, total)
    if total and done != total:
        raise ValueError(f"downloaded {done} bytes of {total}")
    digest = asset.get("digest") or ""
    if digest.startswith("sha256:") and digest[7:].lower() != sha.hexdigest():
        raise ValueError("checksum of the download does not match the release")


def _unpack(path, into):
    """Unpack keeping names inside `into` (zipfile cleans them itself, tarfile by the data filter)."""
    if path.endswith(".zip"):
        with zipfile.ZipFile(path) as z:
            z.extractall(into)
    else:
        with tarfile.open(path) as t:
            t.extractall(into, filter="data")


def add_missing_data(new_data, data_dir):
    """Copy what the new version ships and data/ lacks; existing files are never touched.
    Returns the names copied."""
    added = []
    for root, _, files in os.walk(new_data):
        rel = os.path.relpath(root, new_data)
        for name in files:
            dst = os.path.normpath(os.path.join(data_dir, rel, name))
            if not os.path.exists(dst):
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(os.path.join(root, name), dst)
                added.append(os.path.normpath(os.path.join(rel, name)))
    return added


def cleanup(app_dir=None):
    """Remove what an interrupted update left behind (called at program start)."""
    app_dir = app_dir or P.APP_DIR
    shutil.rmtree(os.path.join(app_dir, STAGE), ignore_errors=True)
    shutil.rmtree(os.path.join(app_dir, "lib.old"), ignore_errors=True)
    for name in os.listdir(app_dir):
        if name.endswith(".old"):
            try:
                os.remove(os.path.join(app_dir, name))
            except OSError:
                pass


def prepare(info, progress=lambda done, total: None, app_dir=None):
    """Download and unpack the release; returns the folder holding its program file and lib/."""
    app_dir = app_dir or P.APP_DIR
    name = asset_name(info["version"])
    stage = os.path.join(app_dir, STAGE)
    shutil.rmtree(stage, ignore_errors=True)
    os.makedirs(stage)
    path = os.path.join(stage, name)
    _download(info["assets"][name], path, _source()[1], progress)
    _unpack(path, stage)
    new = os.path.join(stage, P.APP)
    exe = P.APP + (".exe" if sys.platform == "win32" else "")
    if not (os.path.isfile(os.path.join(new, exe)) and os.path.isdir(os.path.join(new, "lib"))):
        raise ValueError("the archive does not hold the program")
    os.remove(path)
    return new


WIN_SCRIPT = r"""@echo off
setlocal
>>"%JLC_LOG%" echo %DATE% %TIME% update: replacing the program in %JLC_APP%
set /a n=0
:wait_exe
move /y "%JLC_APP%\%JLC_EXE%" "%JLC_APP%\%JLC_EXE%.old" >nul 2>&1
if not errorlevel 1 goto wait_lib_start
set /a n+=1
if %n% geq 60 goto failed
ping -n 2 127.0.0.1 >nul
goto wait_exe
:wait_lib_start
set /a n=0
:wait_lib
move /y "%JLC_APP%\lib" "%JLC_APP%\lib.old" >nul 2>&1
if not errorlevel 1 goto install
set /a n+=1
if %n% geq 30 goto restore_exe
ping -n 2 127.0.0.1 >nul
goto wait_lib
:install
move /y "%JLC_NEW%\lib" "%JLC_APP%\lib" >nul 2>&1
if errorlevel 1 goto restore_lib
move /y "%JLC_NEW%\%JLC_NEW_EXE%" "%JLC_APP%\%JLC_EXE%" >nul 2>&1
if errorlevel 1 goto restore_new_lib
>>"%JLC_LOG%" echo %DATE% %TIME% update: done
goto start
:restore_new_lib
rmdir /s /q "%JLC_APP%\lib"
:restore_lib
move /y "%JLC_APP%\lib.old" "%JLC_APP%\lib" >nul 2>&1
:restore_exe
move /y "%JLC_APP%\%JLC_EXE%.old" "%JLC_APP%\%JLC_EXE%" >nul 2>&1
:failed
>>"%JLC_LOG%" echo %DATE% %TIME% update: FAILED, the old version is kept
:start
cd /d "%JLC_APP%"
start "" "%JLC_APP%\%JLC_EXE%"
rmdir /s /q "%JLC_APP%\lib.old" >nul 2>&1
del /q "%JLC_APP%\%JLC_EXE%.old" >nul 2>&1
rmdir /s /q "%JLC_APP%\_update" >nul 2>&1
(goto) 2>nul & del "%~f0"
"""

SH_SCRIPT = r"""#!/bin/sh
log() { echo "$(date) update: $1" >> "$JLC_LOG"; }
log "replacing the program in $JLC_APP"
n=0
while kill -0 "$JLC_PID" 2>/dev/null; do
    n=$((n + 1))
    if [ "$n" -ge 60 ]; then log "FAILED, the program did not exit"; exit 1; fi
    sleep 1
done
a="$JLC_APP"
if mv "$a/$JLC_EXE" "$a/$JLC_EXE.old"; then
    if mv "$a/lib" "$a/lib.old"; then
        if mv "$JLC_NEW/lib" "$a/lib" && mv "$JLC_NEW/$JLC_NEW_EXE" "$a/$JLC_EXE"; then
            log "done"
        else
            rm -rf "$a/lib"
            mv "$a/lib.old" "$a/lib"
            mv "$a/$JLC_EXE.old" "$a/$JLC_EXE"
            log "FAILED, the old version is kept"
        fi
    else
        mv "$a/$JLC_EXE.old" "$a/$JLC_EXE"
        log "FAILED, the old version is kept"
    fi
else
    log "FAILED, the old version is kept"
fi
cd "$a" || exit 1
nohup "$a/$JLC_EXE" >/dev/null 2>&1 &
rm -rf "$a/lib.old" "$a/$JLC_EXE.old" "$a/_update"
rm -f "$0"
"""


def swap(new, app_dir=None, exe=None, pid=None):
    """Start the replacing script; the caller must exit right after. All paths reach the script through
    the environment, so a folder name in any alphabet is safe in a plain ASCII script."""
    app_dir = app_dir or P.APP_DIR
    win = sys.platform == "win32"
    fd, script = tempfile.mkstemp(prefix="jlc_update_", suffix=".cmd" if win else ".sh")
    with os.fdopen(fd, "w", encoding="ascii", newline="\r\n" if win else "\n") as f:
        f.write(WIN_SCRIPT if win else SH_SCRIPT)
    env = dict(os.environ)
    # the new program is a separate one: it must not take this process's unpacked runtime as its own
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    if "LD_LIBRARY_PATH_ORIG" in env:
        env["LD_LIBRARY_PATH"] = env["LD_LIBRARY_PATH_ORIG"]
    else:
        env.pop("LD_LIBRARY_PATH", None)
    env.update(JLC_APP=app_dir, JLC_NEW=new, JLC_EXE=exe or os.path.basename(sys.executable),
               JLC_NEW_EXE=P.APP + (".exe" if win else ""), JLC_PID=str(pid or os.getpid()),
               JLC_LOG=os.path.join(P.LOGS if os.path.isdir(P.LOGS) else tempfile.gettempdir(), "update.log"))
    cwd = tempfile.gettempdir()         # a process standing in the program folder would hold it
    if win:
        subprocess.Popen(["cmd.exe", "/d", "/c", script], env=env, cwd=cwd,
                         creationflags=subprocess.CREATE_NO_WINDOW, close_fds=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        subprocess.Popen(["/bin/sh", script], env=env, cwd=cwd, start_new_session=True, close_fds=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
