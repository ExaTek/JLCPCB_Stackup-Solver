"""Desktop app (Qt): cheapest orderable JLC stackup for the required impedances.

Run:  JLC Stackup Picker.lnk  |  pythonw jlcapp.pyw  |  python jlcstack.py ui
Internally everything is in mils; the unit switch only converts what is shown and typed.
"""
import concurrent.futures as cf
import faulthandler
import gc
import json
import logging
import multiprocessing
import os
import re
import sys
import threading
import time

from PySide6.QtCore import QLocale, QObject, QPointF, QRectF, QSize, Qt, QTimer, QtMsgType, Signal, qInstallMessageHandler
from PySide6.QtGui import QColor, QFont, QFontMetrics, QIcon, QPainter, QPalette, QPen, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFrame,
                               QGridLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QMainWindow, QMessageBox, QProgressBar, QPushButton, QScrollArea,
                               QSpinBox, QSplitter, QTableWidget, QTableWidgetItem, QToolButton, QVBoxLayout,
                               QWidget)

from .. import paths as P
from ..core import engine as E
from ..core import stackup as ST
from ..export import altium as A
from ..export import kicad as K
from . import i18n
from .i18n import tr

STATE_PATH = os.path.join(P.DATA, "settings.json")
LOGDIR = P.LOGS
LOG = logging.getLogger("jlcapp")
MM = 0.0254
LAYERS = [4, 6, 8, 10]
MAX_THICK = 2.0
EXPORT = "altium" if sys.platform == "win32" else "kicad"      # Altium Designer runs on Windows only
PRESETS = {
    4: [("SGPS", "классика: земля и питание внутри"), ("SPGS", "зеркальный: питание под TOP"),
        ("SGGS", "две земли, питание полигонами"), ("GSSG", "EMC: земля снаружи, сигналы внутри")],
    6: [("SGSPGS", "3 сигнальных — рекомендуемый"), ("SGSGPS", "3 сигнальных, питание у BOTTOM"),
        ("SGSSGS", "4 сигнальных, питание полигонами"), ("GSPGSG", "EMC: 2 внутренних сигнальных")],
    8: [("SGSPPSGS", "4 сигнальных, 2 слоя питания"), ("SGSPGSGS", "4 сигнальных, 3 земли"),
        ("SGSGPSGS", "4 сигнальных, питание в центре"), ("SGSGGSGS", "4 сигнальных, 4 земли"),
        ("SGSSPSGS", "5 сигнальных, L3/L4 — двойной стриплайн")],
    10: [("SGSGPPGSGS", "4 сигнальных, 2 питания в центре"), ("SGSPGGPSGS", "4 сигнальных, 2 питания"),
         ("SGSGSPGSGS", "5 сигнальных"), ("SGSSGPSSGS", "6 сигнальных, двойные стриплайны")],
}
WHERE = [("all", "все сигнальные", "требование должно выполняться на каждом сигнальном слое"),
         ("any", "хотя бы один", "достаточно одного сигнального слоя, где это получается"),
         ("inner_all", "все внутр.", "каждый внутренний сигнальный слой (стриплайн)"),
         ("inner_any", "любой внутр.", "хотя бы один внутренний сигнальный слой"),
         ("outer", "TOP и BOTTOM", "только наружные слои (микрострип под маской)")]

C = {"bg": "#1a1d23", "panel": "#22262e", "base": "#1d2128", "alt": "#2a303a", "line": "#323844",
     "text": "#e4e7ec", "mute": "#8e98a8", "acc": "#4c8dff", "ok": "#3fa66b", "bad": "#e5675d",
     "badbg": "#3a2226", "warn": "#e0b45a", "calc": "#c3cad6", "jlc": "#8fbff5", "okbg": "#1f3a29", "oktx": "#8fe0a9"}
ROLE = {"S": "#a9b8cc", "G": "#3fa66b", "P": "#d9822b"}
ROLE_TXT = {"S": "сигнал", "G": "земля", "P": "питание"}
# result groups: key = colour in C, filter button label, what it means
TIERS = [("ok", "подходят", "выполнены все импедансы"),
         ("warn", "почти", "не выполнен один импеданс"),
         ("bad", "не подходят", "не выполнено два импеданса и больше")]
REQC = ["#d9a441", "#6ca6e8", "#b48ee0", "#4fb8a8", "#e08a6b", "#9aaf5b", "#d987b8", "#8c9bb0"]


def req(name, kind, z, where="all", mode="synth", w=0.0, s=0.0):
    return dict(name=name, kind=kind, z=z, tol=5, where=where, mode=mode, w=w, s=s,
                range=False, wmin=3.5, wmax=8.0, smin=3.5, smax=8.0)


def std3():
    return [req("S50", "se", 50), req("D90", "diff", 90), req("D100", "diff", 100)]


def ddr_example():
    return [req("DDR S40", "se", 40), req("DDR D80", "diff", 80), req("S50", "se", 50), req("D85", "diff", 85),
            req("D90", "diff", 90), req("D100", "diff", 100)]


DEFAULT = {"layers": 8, "outer": 1.0, "thick": [1.6], "inner": [0.5, 1.0], "nofee": False, "plan": "SGSPPSGS",
           "names": {}, "reqs": std3(), "lim": {"wmin": 3.5, "wmax": 8.0, "smin": 3.5, "smax": 8.0},
           "finish": "ENIG", "mask": None, "mode": "compact", "lastdir": "", "unit": "mil"}

NOTES = [(r"fixed geometry", "фикс."), (r"needs ([\d.]+) < min width", r"нужно W=\1 < мин."),
         (r"needs ([\d.]+) > max width", r"нужно W=\1 > макс."), (r"needs W ([\d.]+) > max width", r"нужно W=\1 > макс."),
         (r"needs S ([\d.]+) > max gap", r"нужно S=\1 > макс."), (r"min gap gives lower Z", "даже мин. зазор мал"),
         (r"unreachable even with S=60 at W=([\d.]+)", r"недостижимо при W=\1"),
         (r"target out of range", "вне диапазона"), (r"no width fits", "нет подходящей ширины"),
         (r"outer layer references a power plane", "наружный слой опирается на питание"),
         (r"reference is not the adjacent layer", "опорный слой не соседний"),
         (r"adjacent signal layer \(broadside crosstalk\)", "соседний сигнальный слой — наводки"),
         (r"closest reference (L\d+) is a power plane", r"ближайшая опора \1 — питание"),
         (r"no plane on one side - not computed", "нет плейна с одной стороны — не считается")]


def tier(r):
    return "ok" if r["fails"] == 0 else ("warn" if r["fails"] == 1 else "bad")


def fee_text(r):
    if r.get("fee") is None:
        return "цена неизвестна"
    return "без доплаты" if r["fee"] == 0 else f"доплата ${r['fee']:.0f}"


def ru(s):
    for a, b in NOTES:
        s = re.sub(a, b, s)
    return s


def mix(c1, c2, a):
    x, y = QColor(c1), QColor(c2)
    return QColor(round(x.red() * a + y.red() * (1 - a)), round(x.green() * a + y.green() * (1 - a)),
                  round(x.blue() * a + y.blue() * (1 - a)))


def dot(color, d=12):
    pm = QPixmap(d, d)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor(color))
    p.setPen(Qt.NoPen)
    p.drawEllipse(1, 1, d - 2, d - 2)
    p.end()
    return QIcon(pm)


QSS = f"""
QWidget {{ background:{C['bg']}; color:{C['text']}; font-family:'Segoe UI'; font-size:10pt; }}
QFrame#card {{ background:{C['panel']}; border:1px solid {C['line']}; border-radius:8px; }}
QWidget#cardbody, QWidget#clear, QLabel {{ background:transparent; }}
QLabel#h {{ color:{C['mute']}; font-size:9pt; font-weight:600; }}
QLabel#mute {{ color:{C['mute']}; }}
QLabel#title {{ font-size:15pt; font-weight:600; }}
QLabel#chip {{ border-radius:9px; padding:1px 8px; font-size:9pt; border:1px solid {C['line']}; color:{C['mute']}; }}
QLabel#chipok {{ border-radius:9px; padding:1px 8px; font-size:9pt; background:{C['okbg']}; color:{C['oktx']}; }}
QLabel#chipbad {{ border-radius:9px; padding:1px 8px; font-size:9pt; background:{C['badbg']}; color:#ff9d94; }}
QComboBox, QLineEdit, QSpinBox, QDoubleSpinBox {{ background:{C['base']}; border:1px solid {C['line']};
  border-radius:5px; padding:2px 5px; min-height:22px; }}
QComboBox:hover, QLineEdit:hover, QSpinBox:hover, QDoubleSpinBox:hover {{ border-color:{C['acc']}; }}
QAbstractSpinBox::up-button, QAbstractSpinBox::down-button {{ width:0; border:none; }}
QAbstractSpinBox:disabled {{ background:{C['bg']}; border-color:{C['bg']}; color:{C['mute']}; }}
QComboBox::drop-down {{ border:none; width:18px; background:transparent; }}
QComboBox QAbstractItemView {{ background:{C['panel']}; border:1px solid {C['line']}; selection-background-color:{C['acc']}; outline:0; }}
QPushButton {{ background:{C['alt']}; border:1px solid {C['line']}; border-radius:6px; padding:5px 12px; }}
QPushButton:hover {{ border-color:{C['acc']}; }}
QPushButton:disabled {{ color:{C['mute']}; }}
QPushButton#primary {{ background:{C['acc']}; border:none; color:white; font-weight:600; padding:9px; font-size:11pt; }}
QPushButton#done {{ background:{C['okbg']}; border:1px solid #2f5a3d; color:{C['oktx']}; padding:7px 16px; }}
QPushButton#altium {{ background:#4a3a2b; border:1px solid #6e5640; color:#f1dcc2; font-weight:600; padding:7px 16px; }}
QPushButton#altium:hover {{ background:#56432f; border-color:#a07a52; }}
QPushButton#kicad {{ background:#2b5797; border:1px solid #3d6bb0; color:white; font-weight:600; padding:7px 16px; }}
QPushButton#kicad:hover {{ background:#3464ab; border-color:#6f97d6; }}
QPushButton#jlc {{ background:#0072fc; border:1px solid #0072fc; color:white; font-weight:600; padding:7px 16px; }}
QPushButton#jlc:hover {{ background:#1a84ff; border-color:#5aa6ff; }}
QPushButton#jlc:disabled {{ background:#1d3f6e; border-color:#1d3f6e; color:#a9c4e8; }}
QPushButton#jlcerr {{ background:{C['badbg']}; border:1px solid #6b3a3a; color:#ff9d94; font-weight:600; padding:7px 16px; }}
QPushButton#seg {{ border-radius:0; padding:4px 14px; }}
QPushButton#seg:checked {{ background:{C['acc']}; color:white; border-color:{C['acc']}; }}
QPushButton#pill {{ border-radius:11px; padding:2px 11px; min-width:20px; color:{C['mute']}; }}
QPushButton#pill:checked {{ background:#2c4470; border-color:#4c74b8; color:#dbe7ff; font-weight:600; }}
QPushButton#pill[tone="ok"]:checked {{ background:{C['okbg']}; border-color:#2f5a3d; color:{C['oktx']}; }}
QPushButton#pill[tone="warn"]:checked {{ background:#3a3220; border-color:#6b5a2e; color:#f0d08a; }}
QPushButton#pill[tone="bad"]:checked {{ background:{C['badbg']}; border-color:#6b3a3a; color:#ff9d94; }}
QToolButton {{ background:{C['alt']}; border:1px solid {C['line']}; border-radius:4px; min-width:24px; min-height:22px; }}
QToolButton:hover {{ border-color:{C['acc']}; }}
QToolButton:disabled {{ background:{C['bg']}; border-color:{C['bg']}; color:#3d4452; }}
QToolButton:checked {{ background:#2c4470; border-color:#4c74b8; color:#dbe7ff; }}
QToolButton[role="S"]:checked {{ background:{ROLE['S']}; color:#11151b; font-weight:700; border-color:{ROLE['S']}; }}
QToolButton[role="G"]:checked {{ background:{ROLE['G']}; color:#0d1a12; font-weight:700; border-color:{ROLE['G']}; }}
QToolButton[role="P"]:checked {{ background:{ROLE['P']}; color:#1d1206; font-weight:700; border-color:{ROLE['P']}; }}
QToolButton#flat {{ background:transparent; border:none; color:{C['mute']}; font-weight:600; }}
QTableWidget {{ background:{C['base']}; gridline-color:{C['line']}; border:none; }}
QHeaderView::section {{ background:{C['panel']}; color:{C['mute']}; border:none;
  border-bottom:1px solid {C['line']}; border-right:1px solid {C['line']}; padding:4px 6px; }}
QListWidget {{ background:{C['panel']}; border:none; outline:0; }}
QListWidget::item {{ border-bottom:1px solid {C['line']}; }}
QListWidget::item:selected {{ background:#2b3548; }}
QScrollArea {{ border:none; }}
QProgressBar {{ background:{C['base']}; border:1px solid {C['line']}; border-radius:4px; color:{C['text']};
  text-align:center; font-size:9pt; min-height:18px; }}
QProgressBar::chunk {{ background:{C['acc']}; border-radius:3px; }}
QSplitter::handle {{ background:{C['line']}; }}
QCheckBox {{ spacing:7px; color:{C['mute']}; }}
QCheckBox:hover {{ color:{C['text']}; }}
QCheckBox::indicator {{ width:15px; height:15px; border:1px solid #4a5363; border-radius:3px; background:{C['base']}; }}
QCheckBox::indicator:hover {{ border-color:{C['acc']}; }}
QCheckBox::indicator:checked {{ background:{C['acc']}; border-color:{C['acc']}; }}
QToolTip {{ background:{C['panel']}; color:{C['text']}; border:1px solid {C['line']}; padding:4px; }}
"""


# ---------------------------------------------------------------- widgets
class Card(QFrame):
    def __init__(self, title=None, collapsible=False, collapsed=False, tip=""):
        super().__init__()
        self.setObjectName("card")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 12)
        lay.setSpacing(8)
        self.head = QHBoxLayout()
        lay.addLayout(self.head)
        self.body = QWidget()
        self.body.setObjectName("cardbody")
        self.bl = QVBoxLayout(self.body)
        self.bl.setContentsMargins(0, 0, 0, 0)
        self.bl.setSpacing(6)
        if title:
            if collapsible:
                title = tr(title)
                b = QToolButton(text=("▸ " if collapsed else "▾ ") + title.upper())
                b.setObjectName("flat")
                b.setToolTip(tip or "свернуть / развернуть")
                b.clicked.connect(lambda: self._toggle(b, title))
                self.head.addWidget(b)
                self.body.setVisible(not collapsed)
            else:
                h = QLabel(tr(title).upper())
                h.setObjectName("h")
                self.head.addWidget(h)
            self.head.addStretch()
        lay.addWidget(self.body)

    def _toggle(self, b, title):
        v = not self.body.isVisible()
        self.body.setVisible(v)
        b.setText(("▾ " if v else "▸ ") + title.upper())


class Pill(QPushButton):
    """Checkable pill whose width follows its label in the bold (checked) font plus padding,
    so checking or translating it never clips the text."""
    PAD = 14                                    # QSS padding 11px + border + a little air

    def __init__(self, text=""):
        super().__init__(text)
        self.setObjectName("pill")
        self.setCheckable(True)

    def sizeHint(self):
        f = QFont(self.font())
        f.setWeight(QFont.DemiBold)
        icon = 0 if self.icon().isNull() else self.iconSize().width() + 6
        s = super().sizeHint()
        return QSize(max(s.width(), QFontMetrics(f).horizontalAdvance(self.text()) + icon + 2 * self.PAD), s.height())

    def minimumSizeHint(self):
        return self.sizeHint()


class Pills(QWidget):
    """Row of pill buttons. single=True: exactly one checked; else lo..hi checked."""
    changed = Signal(list)

    def __init__(self, values, fmt, selected, single=False, lo=1, hi=99, tips=None):
        super().__init__()
        self.setObjectName("clear")
        self.single, self.lo, self.hi = single, lo, hi
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(5)
        self.btn = {}
        self.order = [v for v in selected if v in values] or [values[0]]
        self.order = self.order[:1] if single else self.order[-hi:]
        for v in values:
            b = Pill(fmt(v))
            b.setChecked(v in self.order)
            b.setToolTip((tips or {}).get(v, ""))
            b.clicked.connect(lambda on, v=v: self._click(v, on))
            self.btn[v] = b
            h.addWidget(b)
        h.addStretch()

    def _click(self, v, on):
        if self.single:
            self.order = [v]
        elif on:
            self.order.append(v)
            if len(self.order) > self.hi:
                self.order.pop(0)                    # drop the oldest choice
        elif len(self.order) > self.lo:
            self.order.remove(v)
        for k, b in self.btn.items():
            b.setChecked(k in self.order)
        self.changed.emit(sorted(self.order))


class Switch(QWidget):
    """Two-state pill switch: options = ((value, label), (value, label))."""
    toggled = Signal(str)

    def __init__(self, value, options=(("mil", "mil"), ("mm", "мм")),
                 tip="единицы: mil ↔ мм. Меняет все поля ввода и результаты; точность mil — 0.1, мм — 0.01"):
        super().__init__()
        self.options, self.unit = options, value
        self.setFixedSize(112, 26)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip(tip)

    def mousePressEvent(self, e):
        self.unit = self.options[1][0] if self.unit == self.options[0][0] else self.options[0][0]
        self.update()
        self.toggled.emit(self.unit)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QColor(C["line"]))
        p.setBrush(QColor(C["base"]))
        p.drawRoundedRect(QRectF(0.5, 0.5, self.width() - 1, self.height() - 1), 13, 13)
        half = (self.width() - 4) / 2
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#2c4470"))
        p.drawRoundedRect(QRectF(2 if self.unit == self.options[0][0] else 2 + half, 2, half, self.height() - 4), 11, 11)
        p.setFont(QFont("Segoe UI", 9, QFont.DemiBold))
        for i, (u, t) in enumerate(self.options):
            p.setPen(QColor("#dbe7ff" if self.unit == u else C["mute"]))
            p.drawText(QRectF(2 + i * half, 0, half, self.height()), Qt.AlignCenter, tr(t))
        p.end()


class StackView(QWidget):
    """Cross-section with layer roles and the impedances satisfied on each signal layer."""

    def __init__(self):
        super().__init__()
        self.setObjectName("clear")
        self.res = None

    def set(self, res, plan, names, detail, unit, mask):
        self.res, self.plan, self.names, self.detail, self.unit, self.mask = res, plan, names, detail, unit, mask
        self.rows = self._rows()
        self.setMinimumHeight(int(sum(h for _, h, _ in self.rows)) + 8)
        self.setMinimumWidth(460 if detail else 380)
        self.update()

    def _h(self, mil):
        return f"{mil * MM:.3f} мм" if self.unit == "mm" else f"{mil:.2f} mil"

    def _rows(self):
        rows, buf, cu = [], [], 0
        if self.detail:
            rows.append(("mask", 12, None))

        def flush():
            if not buf:
                return
            if self.detail:
                rows.extend(("diel", max(14, min(40, 8 + it["h"] * 1.3)), [it]) for it in buf)
            else:
                rows.append(("diel", max(12, min(44, 6 + sum(x["h"] for x in buf) * 1.25)), buf[:]))
            buf.clear()

        for it in self.res["items"]:
            if it["k"] == "Cu":
                flush()
                rows.append(("cu", 20, cu))
                cu += 1
            else:
                buf.append(it)
        flush()
        if self.detail:
            rows.append(("mask", 12, None))
        return rows

    def paintEvent(self, ev):
        if not self.res:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        y, x0, bw = 4, 38, 120
        sat = {}
        for qi, q in enumerate(self.res["reqs"]):
            for L, v in q["layers"].items():
                if v["ok"]:
                    sat.setdefault(L, []).append(qi)
        small, norm = QFont("Segoe UI", 8), QFont("Segoe UI", 9)
        for kind, h, data in self.rows:
            if kind == "mask":
                p.setPen(Qt.NoPen)
                p.setBrush(QColor("#2f6b45"))
                p.drawRoundedRect(QRectF(x0, y + 4, bw, 4), 2, 2)
                p.setPen(QColor(C["mute"]))
                p.setFont(small)
                p.drawText(QRectF(x0 + bw + 8, y - 2, 300, h + 4), Qt.AlignVCenter,
                           tr(f"маска {self._h(self.mask[0])} · Dk {self.mask[2]:g}"))
            elif kind == "cu":
                role = self.plan[data]
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(ROLE[role]))
                p.drawRoundedRect(QRectF(x0, y + 7, bw, 6), 2, 2)
                p.setFont(norm)
                p.setPen(QColor(C["mute"]))
                p.drawText(QRectF(0, y, x0 - 6, h), Qt.AlignVCenter | Qt.AlignRight, f"L{data + 1}")
                p.setPen(QColor(C["text"]))
                p.drawText(QRectF(x0 + bw + 8, y, 90, h), Qt.AlignVCenter, self.names[data])
                dx = x0 + bw + 8 + 92
                for qi in sat.get(f"L{data + 1}", []):
                    p.setPen(Qt.NoPen)
                    p.setBrush(QColor(REQC[qi % len(REQC)]))
                    p.drawEllipse(QPointF(dx, y + h / 2), 4.5, 4.5)
                    dx += 12
            else:
                core = all(x["k"] == "Core" for x in data)
                p.setPen(Qt.NoPen)
                p.setBrush(QColor("#3a3428" if core else "#2c3038"))
                p.drawRect(QRectF(x0, y, bw, h))
                p.setFont(small)
                p.setPen(QColor(C["mute"]))
                if self.detail:
                    it = data[0]
                    txt = f"{'ядро' if core else it['mat']} · {self._h(it['h'])} · Dk {it['er']:g}"
                    p.drawText(QRectF(x0 + bw + 8, y, 300, h), Qt.AlignVCenter, tr(txt))
                else:
                    txt = f"{'ядро' if core else 'препрег'} {self._h(sum(x['h'] for x in data))}"
                    p.drawText(QRectF(x0 + 6, y, bw - 8, h), Qt.AlignVCenter, tr(txt))
            y += h
        p.end()


class Bridge(QObject):
    progress = Signal(int, int)
    done = Signal(object, object)
    failed = Signal(str)
    checked = Signal(object, object)
    dbprog = Signal(str, int, int)
    dbdone = Signal(object)


# ---------------------------------------------------------------- main window
class Main(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("JLCPCB Stackup Solver")
        scr = QApplication.primaryScreen().availableGeometry()
        self.resize(min(1840, scr.width()), min(1040, scr.height() - 40))
        self.st = self._load()
        self.meta = self._meta()
        self.pool = None
        self.results, self.all_results, self.spec_used, self.cur = [], [], None, None
        self.last_prog = None                   # (done, total) of the running search
        self.tiers = {"ok"}                     # result groups shown (TIERS keys)
        self.db_state = None                    # stack database update: ("run", stage, done, total) | ("error", msg)
        self.br = Bridge()
        self.br.progress.connect(self._on_progress)
        self.br.done.connect(self._on_done)
        self.br.failed.connect(self._on_failed)
        self.br.checked.connect(self._on_checked)
        self.br.dbprog.connect(self._on_db_progress)
        self.br.dbdone.connect(self._on_db_done)
        i18n.LANG = self.st.get("lang", "ru")
        self._build()

    def _build(self):
        """(Re)create both panels; used at start and when the language changes.
        A rebuild keeps the window and splitter sizes."""
        shown = getattr(self, "split", None) is not None and self.isVisible()
        old = (self.size(), self.split.sizes(), self.split_res.sizes()) if shown else None
        split = QSplitter(Qt.Horizontal)
        split.addWidget(self._left())
        split.addWidget(self._right())
        split.setSizes(old[1] if old else [700, 1060])
        split.setChildrenCollapsible(False)
        self.split = split
        self.setCentralWidget(split)
        if old:
            self.split_res.setSizes(old[2])
            self.resize(old[0])
        if self.spec_used:
            self._fill_list()                   # keeps the selected stack
        if getattr(self, "busy", False):
            self.btn_go.setEnabled(False)
            self.prog.show()
            if self.last_prog:
                self._on_progress(*self.last_prog)
            else:
                self.prog.setRange(0, 1)
                self.prog.setFormat(tr("подготовка…"))

    def _lang_changed(self, lang):
        LOG.debug("language -> %s", lang)
        self.st["lang"] = i18n.LANG = lang
        self._build()

    def _status(self, text):
        self.lbl_status.setText(tr(text))

    def _loc(self, root):
        """Translate every text, tooltip and placeholder under root (no-op in Russian)."""
        if i18n.LANG == "ru" or root is None:
            return
        for w in [root] + root.findChildren(QWidget):
            if w.toolTip():
                w.setToolTip(tr(w.toolTip()))
            if isinstance(w, (QLabel, QPushButton, QToolButton, QCheckBox)) and w.text():
                w.setText(tr(w.text()))
            if isinstance(w, QLineEdit) and w.placeholderText():
                w.setPlaceholderText(tr(w.placeholderText()))
            if isinstance(w, QDoubleSpinBox) and w.specialValueText():
                w.setSpecialValueText(tr(w.specialValueText()))
            if isinstance(w, QComboBox):
                for i in range(w.count()):
                    w.setItemText(i, tr(w.itemText(i)))
                    t = w.itemData(i, Qt.ToolTipRole)
                    if t:
                        w.setItemData(i, tr(t), Qt.ToolTipRole)
            if isinstance(w, QTableWidget):
                for i in range(w.rowCount()):
                    it = w.verticalHeaderItem(i)
                    if it and it.toolTip():
                        it.setToolTip(tr(it.toolTip()))

    # ------------------------------------------------ units
    @property
    def mm(self):
        return self.st["unit"] == "mm"

    def u(self, mil):                       # mil -> shown unit
        return mil * MM if self.mm else mil

    def mil(self, val):                     # shown unit -> mil
        return val / MM if self.mm else val

    def fmt(self, mil):
        return f"{mil * MM:.2f}" if self.mm else f"{mil:.1f}".rstrip("0").rstrip(".")

    def uname(self):
        return "мм" if self.mm else "mil"

    def spin(self, mil_val, cb, tip=""):
        s = QDoubleSpinBox(decimals=2 if self.mm else 1, minimum=0, maximum=40 * (MM if self.mm else 1),
                           singleStep=0.01 if self.mm else 0.1)
        s.setValue(round(self.u(mil_val), 2 if self.mm else 1))
        s.valueChanged.connect(lambda v: cb(self.mil(v)))
        s.setToolTip(tip)
        return s

    # ------------------------------------------------ state
    def _load(self):
        st = json.loads(json.dumps(DEFAULT))
        try:
            st.update(json.load(open(STATE_PATH, encoding="utf-8")))
        except (OSError, ValueError):
            pass
        st.pop("hide_bad", None)                # replaced by the result-group buttons
        st["layers"] = st["layers"] if st["layers"] in LAYERS else 8
        for r in st["reqs"]:
            for k, v in req("x", "se", 50).items():
                r.setdefault(k, v)
            if r["where"] not in [w for w, _, _ in WHERE]:
                r["where"] = "all"
        return st

    def _save(self):
        try:
            os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
            json.dump(self.st, open(STATE_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        except OSError:
            pass

    def _meta(self):
        by = {}
        for (name, L, th, inner, outer), o in ST.order_options().items():
            d = by.setdefault(L, {"thickness": set(), "inner": set(), "outer": set()})
            if th <= MAX_THICK:
                d["thickness"].add(th)
            d["inner"].add(inner)
            d["outer"].add(outer)
        return {L: {k: sorted(v) for k, v in d.items()} for L, d in by.items() if L in LAYERS}

    def closeEvent(self, ev):
        LOG.info("window closed")
        self._save()
        super().closeEvent(ev)

    # ------------------------------------------------ left panel
    def _left(self):
        self.lw = QWidget()
        v = QVBoxLayout(self.lw)
        v.setContentsMargins(12, 12, 8, 12)
        v.setSpacing(10)
        self.board = Card("Плата")
        v.addWidget(self.board)
        self.plan_card = Card("Слои")
        v.addWidget(self.plan_card)
        self.req_card = Card("Импедансы")
        v.addWidget(self.req_card)
        self.lim_card = Card("Общие ограничения W / S", collapsible=True, collapsed=True,
                             tip="мин./макс. ширина и зазор для всех импедансов без своего диапазона")
        v.addWidget(self.lim_card)
        self.mask_card = Card("Маска и покрытие", collapsible=True, collapsed=True,
                              tip="параметры паяльной маски и финишного покрытия")
        v.addWidget(self.mask_card)
        self.set_card = Card("Настройки", collapsible=True, collapsed=True, tip="база стеков JLCPCB и папка данных")
        v.addWidget(self.set_card)
        v.addStretch()
        self._render_board()
        self._render_plan()
        self._render_reqs()
        self._render_lim()
        self._render_mask()
        self._render_settings()

        sa = QScrollArea()
        sa.setWidgetResizable(True)
        sa.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        sa.setWidget(self.lw)
        outer = QWidget()
        ov = QVBoxLayout(outer)
        ov.setContentsMargins(0, 0, 0, 10)
        ov.setSpacing(6)
        ov.addWidget(sa, 1)
        foot = QVBoxLayout()
        foot.setContentsMargins(12, 0, 12, 0)
        self.btn_go = QPushButton("Подобрать стек")
        self.btn_go.setObjectName("primary")
        self.btn_go.setToolTip("перебрать все заказываемые стеки JLC с этими параметрами и найти самые дешёвые подходящие")
        self.btn_go.clicked.connect(self._search)
        foot.addWidget(self.btn_go)
        self.prog = QProgressBar()
        self.prog.setFixedHeight(20)
        self.prog.setToolTip("сколько геометрий уже посчитано; посчитанное сохраняется в кэш")
        self.prog.hide()
        foot.addWidget(self.prog)
        self.lbl_status = QLabel("")
        self.lbl_status.setObjectName("mute")
        self.lbl_status.setWordWrap(True)
        foot.addWidget(self.lbl_status)
        ov.addLayout(foot)
        outer.setMinimumWidth(self.lw.sizeHint().width() + 24)
        self._loc(outer)
        return outer

    def _clear(self, lay):
        while lay.count():
            it = lay.takeAt(0)
            if it.widget():
                it.widget().hide()
                it.widget().deleteLater()
            elif it.layout():
                self._clear(it.layout())

    def _row(self, lay, label, w, tip=""):
        h = QHBoxLayout()
        l = QLabel(label)
        l.setObjectName("mute")
        l.setFixedWidth(100)
        l.setToolTip(tip)
        h.addWidget(l)
        h.addWidget(w, 1)
        lay.addLayout(h)

    def _render_board(self):
        bl = self.board.bl
        self._clear(bl)
        L = self.st["layers"]
        m = self.meta[L]
        lay = Pills(LAYERS, str, [L], single=True, tips={x: f"{x} медных слоёв" for x in LAYERS})
        lay.changed.connect(self._layers_changed)
        self._row(bl, "Слоёв", lay)
        if self.st["outer"] not in m["outer"]:
            self.st["outer"] = m["outer"][0]
        cu = QWidget()
        cu.setObjectName("clear")
        ch = QHBoxLayout(cu)
        ch.setContentsMargins(0, 0, 0, 0)
        ch.setSpacing(6)
        o = Pills(m["outer"], lambda x: f"{x:g} oz", [self.st["outer"]], single=True,
                  tips={x: f"наружная медь {x:g} oz (одно значение)" for x in m["outer"]})
        o.changed.connect(self._outer_changed)
        lab = QLabel("  внутренняя")
        lab.setObjectName("mute")
        inn = Pills(m["inner"], lambda x: f"{x:g} oz", self.st["inner"], lo=1,
                    tips={x: f"внутренняя медь {x:g} oz — можно выбрать несколько; 0.5 oz дешевле" for x in m["inner"]})
        self.st["inner"] = sorted(inn.order)
        inn.changed.connect(lambda v: self.st.__setitem__("inner", v))
        ch.addWidget(o)
        ch.addWidget(lab)
        ch.addWidget(inn)
        ch.addStretch()
        self._row(bl, "Медь наружная", cu, "наружная медь — одно значение; внутренняя — одно или несколько")
        th = Pills(m["thickness"], lambda x: f"{x:.1f}", self.st["thick"], lo=1, hi=2,
                   tips={x: f"толщина платы {x:.1f} мм — одна или две сразу" for x in m["thickness"]})
        self.st["thick"] = sorted(th.order)
        th.changed.connect(lambda v: (LOG.debug("thickness -> %s", v), self.st.__setitem__("thick", v)))
        self._row(bl, "Толщина, мм", th, "одна или две толщины одновременно (третья заменяет самую раннюю)")
        nf = Pill("только без доплаты за стек")
        nf.setChecked(self.st["nofee"])
        nf.setToolTip("скрыть стеки, за выбор которых JLC берёт доплату (обычно без доплаты только стандартный стек);\n"
                      "сразу применяется и к уже найденным стекам справа")
        nf.toggled.connect(self._set_nofee)
        sw = Switch(self.st["unit"])
        sw.toggled.connect(self._unit_changed)
        h = QHBoxLayout()
        h.addWidget(nf)
        h.addStretch()
        l2 = QLabel("единицы")
        l2.setObjectName("mute")
        h.addWidget(l2)
        h.addWidget(sw)
        lg = Switch(self.st.get("lang", "ru"), (("ru", "RU"), ("en", "EN")), "язык интерфейса / interface language")
        lg.setFixedSize(84, 26)
        lg.toggled.connect(self._lang_changed)
        h.addSpacing(10)
        h.addWidget(lg)
        bl.addLayout(h)
        self._loc(self.board)

    def _layers_changed(self, v):
        LOG.debug("layers -> %s", v)
        L = v[0]
        self.st["layers"] = L
        self.st["plan"] = PRESETS[L][0][0]
        self.st["names"] = {}
        self.st["thick"] = [t for t in self.st["thick"] if t in self.meta[L]["thickness"]] or [1.6]
        self._render_board()
        self._render_plan()

    def _outer_changed(self, v):
        self.st["outer"] = v[0]
        self.st["mask"] = None
        self._render_mask()

    def _unit_changed(self, unit):
        LOG.debug("unit -> %s", unit)
        self.st["unit"] = unit
        self._render_reqs()
        self._render_lim()
        self._render_mask()
        self._show(self.cur)

    # plan ------------------------------------------------
    def _render_plan(self):
        bl = self.plan_card.bl
        self._clear(bl)
        L = self.st["layers"]
        top = QHBoxLayout()
        lab = QLabel("пресет")
        lab.setObjectName("mute")
        top.addWidget(lab)
        cb = QComboBox()
        cb.setToolTip("типовые назначения слоёв; любую роль можно поменять кнопками S / G / P ниже")
        for p, d in PRESETS[L]:
            cb.addItem(f"{' '.join(p)}   —  {d}", p)
        codes = [p for p, _ in PRESETS[L]]
        if self.st["plan"] in codes:
            cb.setCurrentIndex(codes.index(self.st["plan"]))
        else:
            cb.insertItem(0, "свой: " + " ".join(self.st["plan"]), self.st["plan"])
            cb.setCurrentIndex(0)
        items = [cb.itemData(i) for i in range(cb.count())]
        cb.activated.connect(lambda i, items=items: self._set_plan(items[i]))
        top.addWidget(cb, 1)
        bl.addLayout(top)
        auto = A.default_names(list(self.st["plan"]))
        grid = QGridLayout()
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(3)
        for k, role in enumerate(self.st["plan"]):
            l = QLabel(f"L{k + 1}")
            l.setObjectName("mute")
            grid.addWidget(l, k, 0)
            for j, r in enumerate("SGP"):
                b = QToolButton(text=r)
                b.setCheckable(True)
                b.setProperty("role", r)
                b.setToolTip(f"слой L{k + 1} — {ROLE_TXT[r]}")
                b.setChecked(r == role)
                b.clicked.connect(lambda _, k=k, r=r: self._set_role(k, r))
                grid.addWidget(b, k, 1 + j)
            e = QLineEdit(self.st["names"].get(str(k), ""))
            e.setPlaceholderText(auto[k])
            e.setFixedWidth(120)
            e.setToolTip("имя слоя в Altium (пусто — имя по умолчанию)")
            e.textChanged.connect(lambda t, k=k: self.st["names"].__setitem__(str(k), t.strip()))
            grid.addWidget(e, k, 4)
            d = QLabel(ROLE_TXT[role])
            d.setStyleSheet(f"color:{ROLE[role]}")
            grid.addWidget(d, k, 5)
        grid.setColumnStretch(6, 1)
        bl.addLayout(grid)
        self._loc(self.plan_card)

    def _set_plan(self, p):
        self.st["plan"] = p
        self.st["names"] = {}
        self._render_plan()

    def _set_role(self, k, r):
        a = list(self.st["plan"])
        a[k] = r
        self.st["plan"] = "".join(a)
        self._render_plan()

    def _names(self):
        auto = A.default_names(list(self.st["plan"]))
        return [self.st["names"].get(str(k)) or auto[k] for k in range(len(auto))]

    # requirements ----------------------------------------
    def _render_reqs(self):
        bl = self.req_card.bl
        self._clear(bl)
        g = QGridLayout()
        g.setHorizontalSpacing(5)
        g.setVerticalSpacing(4)
        un = self.uname()
        heads = [("", ""), ("имя", "название — попадёт в описание профиля импеданса Altium\n"
                                   "(сам профиль называется S50 / DIFF90)"),
                 ("тип", "SE — одиночная линия, Diff — дифф. пара"), ("Z, Ω", "целевой импеданс"),
                 ("±%", "допустимое отклонение импеданса"),
                 ("на слоях", "на каких сигнальных слоях этот импеданс должен получаться"),
                 ("режим", "подбор — найти минимальные W и S; фикс. — проверить заданные W и S"),
                 ("", "свой диапазон W и S"), (f"ширина W, {un}", "ширина дорожки (как в Altium)"),
                 (f"зазор S, {un}", "зазор между дорожками пары, край–край"), ("", "")]
        for j, (h, tip) in enumerate(heads):
            l = QLabel(h)
            l.setObjectName("mute")
            l.setToolTip(tip)
            g.addWidget(l, 0, j)
        for i, r in enumerate(self.st["reqs"]):
            row = i + 1
            sw = QLabel()
            sw.setFixedSize(12, 12)
            sw.setStyleSheet(f"background:{REQC[i % len(REQC)]}; border-radius:6px;")
            sw.setToolTip("цвет этого импеданса в результатах и на сечении")
            g.addWidget(sw, row, 0)
            e = QLineEdit(r["name"])
            e.setFixedWidth(74)
            e.textChanged.connect(lambda t, r=r: r.__setitem__("name", t))
            g.addWidget(e, row, 1)
            kc = QComboBox()
            kc.addItem("SE", "se")
            kc.addItem("Diff", "diff")
            kc.setCurrentIndex(0 if r["kind"] == "se" else 1)
            kc.setFixedWidth(62)
            kc.currentIndexChanged.connect(lambda ix, r=r: (r.__setitem__("kind", ("se", "diff")[ix]), self._render_reqs()))
            g.addWidget(kc, row, 2)
            z = QSpinBox(minimum=10, maximum=200)
            z.setValue(int(r["z"]))
            z.setFixedWidth(50)
            z.valueChanged.connect(lambda val, r=r: r.__setitem__("z", val))
            g.addWidget(z, row, 3)
            t = QSpinBox(minimum=1, maximum=30)
            t.setValue(int(r["tol"]))
            t.setFixedWidth(40)
            t.valueChanged.connect(lambda val, r=r: r.__setitem__("tol", val))
            g.addWidget(t, row, 4)
            wc = QComboBox()
            for k2, lab, tip in WHERE:
                wc.addItem(lab, k2)
                wc.setItemData(wc.count() - 1, tip, Qt.ToolTipRole)
            wc.setCurrentIndex([k2 for k2, _, _ in WHERE].index(r["where"]))
            wc.setFixedWidth(140)
            wc.setToolTip(dict((k2, tip) for k2, _, tip in WHERE)[r["where"]])
            wc.currentIndexChanged.connect(lambda ix, r=r: r.__setitem__("where", WHERE[ix][0]))
            g.addWidget(wc, row, 5)
            mc = QComboBox()
            mc.addItem("подбор", "synth")
            mc.addItem("фикс.", "fixed")
            mc.setCurrentIndex(0 if r["mode"] == "synth" else 1)
            mc.setFixedWidth(76)
            mc.currentIndexChanged.connect(lambda ix, r=r: (r.__setitem__("mode", ("synth", "fixed")[ix]), self._render_reqs()))
            g.addWidget(mc, row, 6)
            fixed = r["mode"] == "fixed"
            rb = QToolButton(text="↔")
            rb.setCheckable(True)
            rb.setChecked(r["range"] and not fixed)
            rb.setEnabled(not fixed)
            rb.setToolTip("задать свой диапазон W и S для этого импеданса\n(без него действуют общие ограничения)")
            rb.clicked.connect(lambda on, r=r: (r.__setitem__("range", on), self._render_reqs()))
            g.addWidget(rb, row, 7)
            diff = r["kind"] == "diff"
            for col, key, lo, hi, en in ((8, "w", "wmin", "wmax", True), (9, "s", "smin", "smax", diff)):
                cell = QWidget()
                cell.setObjectName("clear")
                ch = QHBoxLayout(cell)
                ch.setContentsMargins(0, 0, 0, 0)
                ch.setSpacing(3)
                if fixed and en:
                    s = self.spin(r[key] or 0, lambda v, r=r, key=key: r.__setitem__(key, v), "заданное значение")
                    s.setFixedWidth(64)
                    ch.addWidget(s)
                elif not fixed and r["range"] and en:
                    for kk, tip in ((lo, "от"), (hi, "до")):
                        s = self.spin(r[kk], lambda v, r=r, kk=kk: r.__setitem__(kk, v), tip)
                        s.setFixedWidth(46)
                        ch.addWidget(s)
                else:
                    a = QLabel("авто" if en else "")
                    a.setObjectName("mute")
                    a.setToolTip("подбирается автоматически в пределах общих ограничений" if en else "")
                    a.setFixedWidth(64)
                    ch.addWidget(a)
                ch.addStretch()
                g.addWidget(cell, row, col)
            d = QToolButton(text="✕")
            d.setToolTip("удалить этот импеданс")
            d.clicked.connect(lambda _, i=i: (self.st["reqs"].pop(i), self._render_reqs()))
            g.addWidget(d, row, 10)
        bl.addLayout(g)
        br = QHBoxLayout()
        for txt, fn, tip in (("+ SE", lambda: self._add("se"), "добавить одиночную линию"),
                             ("+ Diff", lambda: self._add("diff"), "добавить дифференциальную пару"),
                             ("50 / 90 / 100", lambda: self._set_reqs(std3()),
                              "типовой набор: 50 Ω, 90 Ω и 100 Ω на всех сигнальных слоях"),
                             ("Пример с DDR", lambda: self._set_reqs(ddr_example()),
                              "пример: DDR 40/80 Ω + 50/85/90/100 Ω на всех сигнальных слоях")):
            b = QPushButton(txt)
            b.setToolTip(tip)
            b.clicked.connect(fn)
            br.addWidget(b)
        br.addStretch()
        bl.addLayout(br)
        self._loc(self.req_card)

    def _set_reqs(self, reqs):
        self.st["reqs"] = reqs
        self._render_reqs()

    def _add(self, kind):
        n = len(self.st["reqs"]) + 1
        self.st["reqs"].append(req(("S" if kind == "se" else "D") + str(n), kind, 50 if kind == "se" else 100))
        self._render_reqs()

    def _render_lim(self):
        bl = self.lim_card.bl
        self._clear(bl)
        gl = QGridLayout()
        for i, (k, lab, tip) in enumerate((("wmin", "мин. ширина", "минимальная ширина дорожки"),
                                           ("wmax", "макс. ширина", "максимальная ширина дорожки"),
                                           ("smin", "мин. зазор", "минимальный зазор в паре"),
                                           ("smax", "макс. зазор", "максимальный зазор в паре"))):
            s = self.spin(self.st["lim"][k], lambda v, k=k: self.st["lim"].__setitem__(k, v), tip)
            s.setFixedWidth(70)
            gl.addWidget(QLabel(f"{lab}, {self.uname()}"), i // 2, (i % 2) * 2)
            gl.addWidget(s, i // 2, (i % 2) * 2 + 1)
        bl.addLayout(gl)
        n = QLabel("Минимум JLC для многослойных — 3.5/3.5 mil (0.09 мм); 3 mil допустимо только в фан-ауте BGA.")
        n.setObjectName("mute")
        n.setWordWrap(True)
        bl.addWidget(n)
        self._loc(self.lim_card)

    def _jlc_mask(self):
        cv = ST._pick(ST.load_configs()["coverlay"], self.st["outer"], 1)
        return [cv["coatingAboveSubstrate"], cv["coatingAboveTrace"], 3.8]

    def _render_mask(self):
        bl = self.mask_card.bl
        self._clear(bl)
        m = self.st["mask"] or self._jlc_mask()
        gm = QGridLayout()
        for i, (lab, tip) in enumerate(((f"над подложкой, {self.uname()}", "толщина маски на голом диэлектрике (C1)"),
                                        (f"над дорожкой, {self.uname()}", "толщина маски поверх дорожки (C2)"))):
            s = self.spin(m[i], lambda v, i=i: self._mask_set(i, v), tip)
            s.setFixedWidth(70)
            gm.addWidget(QLabel(lab), i, 0)
            gm.addWidget(s, i, 1)
        er = QDoubleSpinBox(decimals=2, minimum=1, maximum=10, singleStep=0.1)
        er.setValue(m[2])
        er.setFixedWidth(70)
        er.setToolTip("диэлектрическая проницаемость маски")
        er.valueChanged.connect(lambda v: self._mask_set(2, v))
        gm.addWidget(QLabel("Dk маски"), 2, 0)
        gm.addWidget(er, 2, 1)
        fin = QComboBox()
        for f in ("ENIG", "HASL", "LF-HASL", "OSP / нет"):
            fin.addItem(f, "none" if f.startswith("OSP") else f)
        fin.setCurrentIndex(max(0, fin.findData(self.st["finish"])))
        fins = [fin.itemData(i) for i in range(fin.count())]
        fin.currentIndexChanged.connect(lambda i, fins=fins: self.st.__setitem__("finish", fins[i]))
        fin.setToolTip("финишное покрытие — попадёт в стек Altium")
        gm.addWidget(QLabel("покрытие"), 3, 0)
        gm.addWidget(fin, 3, 1)
        bl.addLayout(gm)
        n = QLabel("По умолчанию — модель маски JLC. Маска учитывается в расчёте наружных слоёв и в файле Altium.")
        n.setObjectName("mute")
        n.setWordWrap(True)
        bl.addWidget(n)
        self._loc(self.mask_card)

    # settings: stack database ----------------------------
    DB_STAGES = {"options": "варианты заказа", "templates": "шаблоны стеков", "configs": "модель JLC",
                 "prices": "доплаты"}

    def _render_settings(self):
        bl = self.set_card.bl
        self._clear(bl)
        h = QHBoxLayout()
        self.btn_db = QPushButton("Обновить базу стеков")
        self.btn_db.setToolTip("скачать с JLCPCB актуальные стеки, варианты формы заказа и доплаты:\n"
                               "несколько минут, около 1500 запросов; программой можно пользоваться,\n"
                               "старая база остаётся, пока новая не скачана целиком")
        self.btn_db.clicked.connect(self._update_db)
        h.addWidget(self.btn_db)
        self.lbl_db = QLabel()
        self.lbl_db.setWordWrap(True)
        h.addWidget(self.lbl_db, 1)
        bl.addLayout(h)
        d = QLabel(f"папка данных: {P.DATA}")
        d.setObjectName("mute")
        d.setWordWrap(True)
        d.setTextInteractionFlags(Qt.TextSelectableByMouse)
        d.setToolTip("настройки, кэш расчётов, журналы и база стеков")
        bl.addWidget(d)
        self._loc(self.set_card)
        self._db_label()

    def _db_label(self):
        st = self.db_state
        self.btn_db.setEnabled(not (st and st[0] == "run"))
        if st and st[0] == "run":
            _, stage, done, total = st
            text = f"обновляю: {self.DB_STAGES.get(stage, stage)}" + (f" {done} из {total}" if total else "…")
            color = C["text"]
        else:
            m = ST.db()["meta"]
            text = f"обновлено {m['updated']} · стеков {m['templates']}"
            color = C["mute"]
            if st:
                text = f"ошибка обновления: {st[1]}\n" + text
                color = C["warn"]
        self.lbl_db.setText(tr(text))
        self.lbl_db.setStyleSheet(f"color:{color}")

    def _update_db(self):
        if self.db_state and self.db_state[0] == "run":
            return
        self.db_state = ("run", "options", 0, 0)
        self._db_label()
        LOG.info("stack database update")

        def job():
            from ..jlcpcb import update as U
            try:
                self.br.dbdone.emit(U.refresh(lambda stage, d, n: self.br.dbprog.emit(stage, d, n)))
            except OSError as e:
                LOG.exception("stack database update failed")
                self.br.dbdone.emit({"__error__": f"нет связи с JLCPCB ({e.__class__.__name__})"})
            except Exception as e:
                LOG.exception("stack database update failed")
                self.br.dbdone.emit({"__error__": f"{e.__class__.__name__}: {e}"})
        threading.Thread(target=job, daemon=True, name="db-update").start()

    def _on_db_progress(self, stage, done, total):
        self.db_state = ("run", stage, done, total)
        self._db_label()

    def _on_db_done(self, out):
        if "__error__" in out:
            self.db_state = ("error", out["__error__"])
            self._status("база стеков не обновлена: " + out["__error__"])
        else:
            LOG.info("stack database updated: %s", out)
            self.db_state = None
            self.meta = self._meta()
            self._render_board()
            self._status(f"база стеков обновлена: {out['templates']} стеков; нажмите «Подобрать стек», чтобы пересчитать")
        self._db_label()

    def _mask_set(self, i, v):
        self.st["mask"] = self.st["mask"] or self._jlc_mask()
        self.st["mask"][i] = v

    # ------------------------------------------------ right panel
    def _right(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(8, 12, 12, 12)
        v.setSpacing(8)
        bar = QHBoxLayout()
        self.lbl_count = QLabel("РЕЗУЛЬТАТЫ")
        self.lbl_count.setObjectName("h")
        bar.addWidget(self.lbl_count)
        bar.addSpacing(6)
        self.tier_btn = {}
        for key, lab, tip in TIERS:
            b = Pill(" " + lab)                 # space: gap after the colour dot
            b.setProperty("tone", key)
            b.setIcon(dot(C[key], 10))
            b.setIconSize(QSize(10, 10))
            b.setChecked(key in self.tiers)
            b.setToolTip(tip + "\nнажмите, чтобы показать или скрыть такие стеки")
            b.clicked.connect(lambda on, key=key: self._set_tier(key, on))
            self.tier_btn[key] = b
            bar.addWidget(b)
        bar.addStretch()
        self.seg = []
        for txt, mode, tip in (("Кратко", "compact", "только ширины и сечение"),
                               ("Подробно", "detail", "плюс импеданс, отклонение, материалы и сверка с JLC")):
            b = QPushButton(txt)
            b.setObjectName("seg")
            b.setCheckable(True)
            b.setChecked(self.st["mode"] == mode)
            b.setToolTip(tip)
            b.clicked.connect(lambda _, m=mode: self._set_mode(m))
            self.seg.append(b)
            bar.addWidget(b)
        v.addLayout(bar)
        sp = QSplitter(Qt.Horizontal)
        self.lst = QListWidget()
        self.lst.setMinimumWidth(270)
        self.lst.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.lst.currentRowChanged.connect(self._select)
        sp.addWidget(self.lst)
        self.detail = QScrollArea()
        self.detail.setWidgetResizable(True)
        self.detail.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._placeholder("Задайте параметры слева и нажмите «Подобрать стек».\n\n"
                          "Первый расчёт новой комбинации — до минуты, дальше из кэша.")
        sp.addWidget(self.detail)
        sp.setSizes([270, 790])
        sp.setChildrenCollapsible(False)
        self.split_res = sp
        v.addWidget(sp, 1)
        self._loc(w)
        return w

    def _placeholder(self, text):
        ph = QLabel(tr(text))
        ph.setObjectName("mute")
        ph.setAlignment(Qt.AlignCenter)
        self.detail.setWidget(ph)

    def _set_mode(self, m):
        LOG.debug("mode -> %s", m)
        self.st["mode"] = m
        for b, mm in zip(self.seg, ("compact", "detail")):
            b.setChecked(mm == m)
        self._show(self.cur)

    # ------------------------------------------------ search
    def _spec(self):
        st = self.st
        reqs = []
        for i, r in enumerate(st["reqs"]):
            q = {"name": r["name"] or f"Z{i + 1}", "kind": r["kind"], "z": float(r["z"]), "tol": r["tol"] / 100,
                 "where": r["where"], "mode": r["mode"],
                 "w": float(r["w"]) if r["mode"] == "fixed" else None,
                 "s": float(r["s"]) if r["mode"] == "fixed" and r["kind"] == "diff" else None}
            if r["mode"] == "synth" and r["range"]:
                q.update(wmin=r["wmin"], wmax=r["wmax"])
                if r["kind"] == "diff":
                    q.update(smin=r["smin"], smax=r["smax"])
            reqs.append(q)
        step = 0.01 / MM if self.mm else 0.1          # 0.01 mm or 0.1 mil grid
        return {"layers": st["layers"], "thicknesses": st["thick"], "inner": st["inner"], "outer": st["outer"],
                "plan": list(st["plan"]), "names": self._names(),       # all stacks: filters work on the list
                "top": None, "thickness_pref": 1.6, "lim": dict(st["lim"], step=round(step, 6)),
                "mask": st["mask"] or self._jlc_mask(), "finish": st["finish"], "reqs": reqs}

    def _search(self):
        if getattr(self, "busy", False):
            LOG.info("search ignored: previous run still busy")
            return
        if not self.st["reqs"]:
            QMessageBox.information(self, tr("Подбор"), tr("Добавьте хотя бы один импеданс."))
            return
        spec = self._spec()
        self._save()
        self.btn_go.setEnabled(False)
        self.prog.setRange(0, 1)
        self.prog.setValue(0)
        self.prog.setFormat(tr("подготовка…"))
        self.prog.show()
        self._status("подбираю стеки…")
        self.busy, self.last_prog = True, None
        LOG.info("search: layers=%s thick=%s inner=%s outer=%s plan=%s unit=%s reqs=%s lim=%s",
                 spec["layers"], spec["thicknesses"], spec["inner"], spec["outer"], "".join(spec["plan"]),
                 self.st["unit"], [(q["name"], q["kind"], q["z"], q["where"], q["mode"]) for q in spec["reqs"]],
                 spec["lim"])
        t0 = time.time()

        def job():
            try:
                res = E.search(spec, progress=lambda d, n: self.br.progress.emit(d, n))
                LOG.info("search done in %.1fs: %d stacks", time.time() - t0, len(res))
                self.br.done.emit(res, spec)
            except Exception as e:
                LOG.exception("search failed")
                self.br.failed.emit(f"{e.__class__.__name__}: {e}")
        threading.Thread(target=job, daemon=True, name="search").start()

    def _on_progress(self, d, n):
        if d == 0 or d == n or d % 50 == 0:
            LOG.debug("progress %d/%d", d, n)
        self.last_prog = (d, n)
        self.prog.setRange(0, max(n, 1))
        self.prog.setValue(d)
        self.prog.setFormat(tr(f"считаю геометрии: {d} из {n}  (%p%)"))
        self._status("")

    def _on_failed(self, msg):
        self.busy = False
        self.btn_go.setEnabled(True)
        self.prog.hide()
        self._status("ошибка: " + msg)

    def _on_done(self, res, spec):
        self.busy = False
        self.btn_go.setEnabled(True)
        self.prog.hide()
        self.all_results, self.spec_used = res, spec
        found = {tier(r) for r in self._pool()[0]}
        self.tiers = {next((k for k, _, _ in TIERS if k in found), "ok")}     # best group that is not empty
        self._fill_list()

    # filters: applied at once to the stacks already found
    def _set_tier(self, key, on):
        LOG.debug("tier %s -> %s", key, on)
        (self.tiers.add if on else self.tiers.discard)(key)
        if self.spec_used:
            self._fill_list()

    def _set_nofee(self, on):
        LOG.debug("nofee -> %s", on)
        self.st["nofee"] = on
        if self.spec_used:
            self._fill_list()

    def _pool(self):
        """Found stacks left by the surcharge filter, and how many it hid."""
        if not self.st["nofee"]:
            return self.all_results, 0
        free = [r for r in self.all_results if r["fee"] == 0]
        return free, len(self.all_results) - len(free)

    def _fill_list(self):
        keep = self.cur
        pool, n_fee = self._pool()
        cnt = {k: sum(1 for r in pool if tier(r) == k) for k, _, _ in TIERS}
        for k, lab, _ in TIERS:
            self.tier_btn[k].setText(tr(f" {lab} ({cnt[k]})"))
            self.tier_btn[k].setChecked(k in self.tiers)
        self.results = res = [r for r in pool if tier(r) in self.tiers]
        self._status(f"готово: стеков найдено {len(self.all_results)}, показано {len(res)}"
                     + (f", скрыто с доплатой {n_fee}" if n_fee else ""))
        self.lbl_count.setText(tr(f"РЕЗУЛЬТАТЫ · {len(res)}"))
        self.lst.clear()
        for r in res:
            n = len(r["reqs"])
            col = C[tier(r)]
            it = QListWidgetItem()
            it.setSizeHint(QSize(240, 54))
            self.lst.addItem(it)
            wdg = QWidget()
            wdg.setObjectName("clear")
            wdg.setToolTip("зелёный — выполнены все импедансы, жёлтый — кроме одного, красный — не выполнено несколько")
            h = QHBoxLayout(wdg)
            h.setContentsMargins(10, 6, 8, 6)
            ic = QLabel()
            ic.setPixmap(dot(col, 12).pixmap(12, 12))
            h.addWidget(ic)
            vv = QVBoxLayout()
            vv.setSpacing(0)
            vv.addWidget(QLabel(f"<b>{r['name']}</b>"))
            b = QLabel(f"{n - r['fails']}/{n} · {fee_text(r)} · {r['finished'] or r['thickness']} мм · Cu {r['inner_oz']:g}")
            b.setMinimumWidth(10)
            b.setObjectName("mute")
            vv.addWidget(b)
            h.addLayout(vv, 1)
            self.lst.setItemWidget(it, wdg)
        self._loc(self.lst)
        if res:
            self.lst.setCurrentRow(next((i for i, r in enumerate(res) if r is keep), 0))
        else:
            self._placeholder("Все найденные стеки скрыты: включите группы над списком\nили снимите «только без доплаты за стек»."
                              if self.all_results else "Ничего не найдено — измените параметры слева.")

    def _select(self, i):
        LOG.debug("select row %d", i)
        self.cur = self.results[i] if 0 <= i < len(self.results) else None
        self._show(self.cur)

    # ------------------------------------------------ detail
    def _show(self, r):
        if not r:
            return
        spec = self.spec_used
        detail = self.st["mode"] == "detail"
        names, plan = spec["names"], "".join(spec["plan"])
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(14, 10, 14, 14)
        v.setSpacing(10)
        head = QHBoxLayout()
        t = QLabel(r["name"])
        t.setObjectName("title")
        head.addWidget(t)
        n = len(r["reqs"])
        chips = [(f"выполнено {n - r['fails']} из {n}", "chipok" if r["fails"] == 0 else "chipbad",
                  "сколько заданных импедансов получается на этом стеке"),
                 (fee_text(r), "chipok" if r["fee"] == 0 else "chipbad",
                  "доплата JLC за выбор этого стека (Specify Stackup), по форме заказа:\n"
                  "5 плат 100×100 мм; для другого размера и количества сумма другая.\n"
                  "Без доплаты обычно только стандартный стек каждой комбинации."),
                 (f"{r['finished'] or r['thickness']} мм", "chip", "готовая толщина платы по данным JLC (±10%)"),
                 (f"внутр. Cu {r['inner_oz']:g} oz" + (f" +${r['cu_fee']:.0f}" if r.get("cu_fee") else ""), "chip",
                  "внутренняя медь; 1 oz и толще JLC считает отдельно (по форме заказа, 5 плат 100×100 мм)")]
        if r["expedited"]:
            chips.append(("ускоренно", "chip", "доступно ускоренное производство"))
        for txt, kind, tip in chips:
            c = QLabel(txt)
            c.setObjectName(kind)
            c.setToolTip(tip)
            head.addWidget(c)
        head.addStretch()
        v.addLayout(head)
        if detail:
            nl = QLabel(r["note"])
            nl.setObjectName("mute")
            v.addWidget(nl)
        done = "jlc" in r
        card2 = Card(f"Ширины W / S, {self.uname()}" + (" · импеданс" if detail else ""))
        card2.bl.addWidget(self._table(r, spec, names, detail))
        if detail:
            lg = QLabel(f"<span style='color:{C['calc']}'>■ расчёт (офлайн)</span>&nbsp;&nbsp;&nbsp;"
                        f"<span style='color:{C['jlc']}'>■ JLC Si9000</span>"
                        + ("" if done else f"&nbsp;&nbsp;&nbsp;<span style='color:{C['mute']}'>— нажмите «Сверить с JLC»</span>"))
            card2.bl.addWidget(lg)
        v.addWidget(card2)
        warns = sorted({f"{x['layer']}: {ru(wt)}" for x in r["layers"] for wt in (x.get("warn") or [])})
        if warns:
            wl = QLabel("\n".join("⚠ " + x for x in warns))
            wl.setStyleSheet(f"color:{C['warn']}")
            wl.setWordWrap(True)
            v.addWidget(wl)
        card1 = Card("Сечение")
        sv = StackView()
        sv.set(r, plan, names, detail, self.st["unit"], spec["mask"])
        card1.bl.addWidget(sv)
        legend = QLabel(" ".join(f"<span style='color:{ROLE[x]}'>■</span> {ROLE_TXT[x]}" for x in "SGP")
                        + "  ·  точки справа — импедансы, которые получаются на слое")
        legend.setObjectName("mute")
        card1.bl.addWidget(legend)
        v.addWidget(card1)
        v.addLayout(self._actions(r))
        v.addStretch()
        self._loc(w)
        self.detail.setWidget(w)
        self._fit(w)

    def _actions(self, r):
        """CAD export (Altium on Windows, KiCad elsewhere) + JLC check under the cross-section.
        The check state lives in the result (r["checking"], r["jlc"], r["jlc_error"]), so
        re-rendering keeps it."""
        btns = QHBoxLayout()
        if EXPORT == "kicad":
            b1 = QPushButton("Сохранить для KiCad…")
            b1.setToolTip("сохранить проект KiCad: плата со стеком (слои, диэлектрики, маска, покрытие),\n"
                          "классы цепей S50 / DIFF90 и правила с шириной и зазором на каждом сигнальном слое\n"
                          "KiCad 7+: открыть проект или в своей плате Board Setup → Import Settings")
        else:
            b1 = QPushButton("Сохранить для Altium…")
            b1.setToolTip("сохранить стек (слои, маска, покрытие, профили импеданса S50 / DIFF90 с ширинами\n"
                          "на всех сигнальных слоях) в файл .stackup\n"
                          "Altium: Layer Stack Manager → File → Load Stackup")
        b1.setObjectName(EXPORT)
        b1.clicked.connect(lambda: self._export(r))
        tip = ("пересчитать все найденные ширины онлайн-калькулятором самого JLC (Si9000)\n"
               "и показать его импеданс рядом с нашим расчётом (режим «Подробно»)")
        if r.get("checking"):
            b2 = QPushButton("спрашиваю JLC…")
            b2.setEnabled(False)
        elif "jlc" in r:
            b2 = QPushButton("✓ Сверено с JLC")
            b2.setObjectName("done")
        elif r.get("jlc_error"):
            b2 = QPushButton("⚠ JLC не ответил — повторить")
            b2.setObjectName("jlcerr")
            tip += "\n\nошибка: " + r["jlc_error"]
        else:
            b2 = QPushButton("Сверить с JLC")
        if not b2.objectName():
            b2.setObjectName("jlc")
        b2.setToolTip(tip)
        b2.clicked.connect(lambda: self._verify(r))
        btns.addWidget(b1)
        btns.addWidget(b2)
        btns.addStretch()
        return btns

    def _fit(self, w):
        """Grow the window (within the screen) when the card is wider than its area."""
        if not self.detail.isVisible():         # panels just rebuilt, not laid out yet: keep the size
            return
        need = w.sizeHint().width() - self.detail.viewport().width()
        scr = self.screen().availableGeometry() if self.screen() else None
        if need > 0 and scr:
            self.resize(min(self.width() + need + 8, scr.width()), self.height())

    def _cell(self, v, q, color, detail, zj):
        ok = v["ok"]
        txt = "—" if v.get("W") is None else (
            f"{self.fmt(v['W'])}/{self.fmt(v['S'])}" if v.get("S") is not None else self.fmt(v["W"]))
        lines = [f"<b>{'✗ ' if not ok else ''}{txt}</b>"]
        if detail and v.get("Z"):
            dev = (v["Z"] - q["z"]) / q["z"] * 100
            lines.append(f"<span style='color:{C['calc']}'>{v['Z']:.1f} Ω {dev:+.1f}%</span>")
            if zj is not None:
                lines.append(f"<span style='color:{C['jlc']}'>JLC {zj:.1f} Ω</span>")
            if v.get("note") and not ok:
                lines.append(f"<span style='color:#ff9d94'>{ru(v['note'])}</span>")
        lab = QLabel("<br>".join(lines))
        lab.setAlignment(Qt.AlignCenter)
        bg = mix(color, C["base"], 0.28).name() if ok else C["badbg"]
        fg = C["text"] if ok else "#ff9d94"
        lab.setStyleSheet(f"background:{bg}; color:{fg}; padding:4px 8px; font-size:{10 if not detail else 9}pt;")
        tip = f"{q['name']}: {txt} {self.uname()}"
        if v.get("Z"):
            tip += f" → {v['Z']:.2f} Ω"
        if zj is not None:
            tip += f" (JLC {zj:.2f} Ω)"
        if v.get("note"):
            tip += "\n" + ru(v["note"])
        lab.setToolTip(tip)
        return lab

    def _table(self, r, spec, names, detail):
        lays = r["layers"]
        reqs = spec["reqs"]
        tw = QTableWidget(len(reqs), len(lays))
        tw.setEditTriggers(QAbstractItemView.NoEditTriggers)
        tw.setSelectionMode(QAbstractItemView.NoSelection)
        tw.setFocusPolicy(Qt.NoFocus)
        for j, x in enumerate(lays):
            k = int(x["layer"][1:]) - 1
            tw.setHorizontalHeaderItem(j, QTableWidgetItem(f"{x['layer']}\n{names[k]}"))
        jlc = r.get("jlc") or {}
        heights, widths = [0] * len(reqs), [0] * len(lays)
        for i, (q, rr) in enumerate(zip(reqs, r["reqs"])):
            col = REQC[i % len(REQC)]
            hi = QTableWidgetItem(dot(col), f"{q['name']}  {q['z']:g} Ω" + ("" if rr["sat"] else "  ✗"))
            hi.setForeground(QColor(C["text"] if rr["sat"] else "#ff9d94"))
            hi.setToolTip("не выполнено на нужных слоях" if not rr["sat"] else "выполнено")
            tw.setVerticalHeaderItem(i, hi)
            for j, x in enumerate(lays):
                v = rr["layers"].get(x["layer"])
                if v is None:
                    lab = QLabel("·")
                    lab.setAlignment(Qt.AlignCenter)
                    lab.setStyleSheet(f"color:{C['line']}")
                    lab.setToolTip("этот импеданс не требуется на этом слое")
                else:
                    lab = self._cell(v, q, col, detail, jlc.get(q["name"], {}).get(x["layer"]))
                tw.setCellWidget(i, j, lab)
                hint = lab.sizeHint()
                heights[i] = max(heights[i], hint.height() + 4)
                widths[j] = max(widths[j], hint.width() + 6)
        for i, h in enumerate(heights):
            tw.setRowHeight(i, max(h, 32))
        for j, wd in enumerate(widths):
            tw.setColumnWidth(j, max(wd, 84))
        tw.verticalHeader().setSectionResizeMode(QHeaderView.Fixed)
        tw.horizontalHeader().setSectionResizeMode(QHeaderView.Fixed)
        tw.setFixedHeight(tw.horizontalHeader().sizeHint().height() + tw.verticalHeader().length() + 2 * tw.frameWidth() + 2)
        tw.setMinimumWidth(tw.verticalHeader().sizeHint().width() + tw.horizontalHeader().length() + 2 * tw.frameWidth() + 2)
        tw.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        tw.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        return tw

    # ------------------------------------------------ actions
    def _stack(self, r):
        return E.find_stack(self.spec_used["layers"], r["name"], r["thickness"], r["inner_oz"], self.spec_used["outer"])

    def _export(self, r):
        spec = self.spec_used
        kicad = EXPORT == "kicad"
        fmt, ext = (K, ".kicad_pcb") if kicad else (A, ".stackup")
        fname = f"{r['name']}_{''.join(spec['plan'])}{ext}"
        path, _ = QFileDialog.getSaveFileName(self, tr("Сохранить стек для KiCad" if kicad else "Сохранить стек для Altium"),
                                              os.path.join(self.st.get("lastdir") or os.path.expanduser("~"), fname),
                                              f"{'KiCad board' if kicad else 'Altium Layer Stack'} (*{ext})")
        if not path:
            return
        if not path.endswith(ext):                      # Linux dialogs do not add it
            path += ext
        LOG.info("export %s -> %s", r["name"], path)
        try:
            QApplication.setOverrideCursor(Qt.WaitCursor)   # widths for uncovered layers take a few seconds
            try:
                out = fmt.build(self._stack(r), spec["plan"], r, spec["reqs"], names=spec["names"],
                                finish=spec["finish"], mask=spec["mask"], lim=spec["lim"])
            finally:
                QApplication.restoreOverrideCursor()
            fmt.save(path, out)
            self.st["lastdir"] = os.path.dirname(path)
            self._status(f"сохранено: {path}" + (" (+ .kicad_pro, .kicad_dru)" if kicad else ""))
        except Exception as e:
            LOG.exception("export failed")
            QMessageBox.warning(self, tr("Экспорт"), f"{e.__class__.__name__}: {e}")

    def _verify(self, r):
        if r.get("checking"):
            return
        r["checking"] = True
        r.pop("jlc_error", None)
        spec, stack = self.spec_used, self._stack(r)
        self._status("спрашиваю JLC…")
        self._show(r)
        LOG.info("JLC check %s", r["name"])

        def job():
            try:
                out = E.jlc_check(stack, r, spec["reqs"], spec["mask"])
                if not any(z is not None for d in out.values() for z in d.values()):
                    raise RuntimeError("JLC не вернул ни одного значения")
                self.br.checked.emit(r, out)
            except OSError as e:                # timeout, refused, DNS, TLS: the OS text is not helpful
                LOG.exception("JLC check failed")
                self.br.checked.emit(r, {"__error__": f"нет связи с сервером JLC ({e.__class__.__name__})"})
            except Exception as e:
                LOG.exception("JLC check failed")
                self.br.checked.emit(r, {"__error__": f"{e.__class__.__name__}: {e}"})
        threading.Thread(target=job, daemon=True, name="jlc-check").start()

    def _on_checked(self, r, out):
        r.pop("checking", None)
        if "__error__" in out:
            r["jlc_error"] = out["__error__"]
            self._status("JLC недоступен: " + out["__error__"])
        else:
            r["jlc"] = out
            self._status("сверено с JLC: его значения — голубым в режиме «Подробно»")
            if r is self.cur and self.st["mode"] != "detail":
                self._set_mode("detail")
                return
        if r is self.cur:
            self._show(r)


def arrow_qss():
    """Combo-box arrow as a small generated PNG (Qt QSS has no CSS triangles)."""
    os.makedirs(P.CACHE, exist_ok=True)
    path = os.path.join(P.CACHE, "ui_arrow.png")
    if not os.path.exists(path):
        pm = QPixmap(10, 6)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        p.setBrush(QColor(C["mute"]))
        p.setPen(Qt.NoPen)
        p.drawPolygon([QPointF(0, 0), QPointF(10, 0), QPointF(5, 6)])
        p.end()
        pm.save(path)
    check = os.path.join(P.CACHE, "ui_check.png")
    if not os.path.exists(check):
        pm = QPixmap(15, 15)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        pen = QPen(QColor("#ffffff"))
        pen.setWidthF(2.0)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        p.drawPolyline([QPointF(3.5, 7.8), QPointF(6.3, 10.5), QPointF(11.5, 4.5)])
        p.end()
        pm.save(check)
    return (f"QComboBox::down-arrow {{ image:url({path.replace(os.sep, '/')}); width:10px; height:6px; margin-right:6px; }}"
            f"QCheckBox::indicator:checked {{ image:url({check.replace(os.sep, '/')}); }}")


def app_icon():
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    for i, c in enumerate((ROLE["S"], ROLE["G"], ROLE["P"], ROLE["G"], ROLE["S"])):
        p.setBrush(QColor(c))
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(QRectF(6, 6 + i * 11.5, 52, 6), 3, 3)
    p.end()
    return QIcon(pm)


def setup(app):
    app.setStyle("Fusion")
    pal = QPalette()
    for role, c in ((QPalette.Window, C["bg"]), (QPalette.Base, C["base"]), (QPalette.AlternateBase, C["panel"]),
                    (QPalette.Text, C["text"]), (QPalette.WindowText, C["text"]), (QPalette.Button, C["alt"]),
                    (QPalette.ButtonText, C["text"]), (QPalette.Highlight, C["acc"]),
                    (QPalette.HighlightedText, "#ffffff"), (QPalette.ToolTipBase, C["panel"]),
                    (QPalette.ToolTipText, C["text"]), (QPalette.PlaceholderText, C["mute"])):
        pal.setColor(role, QColor(c))
    app.setPalette(pal)
    app.setStyleSheet(QSS + arrow_qss())
    app.setWindowIcon(app_icon())


class MainThreadGC(QObject):
    """Qt objects must be destroyed on the GUI thread. Python's cyclic GC may run in
    whatever thread happens to allocate (e.g. the search thread) and then frees Qt
    wrappers there -> access violation. So automatic GC is off and collection is
    driven by a timer on the GUI thread."""

    def __init__(self, interval_ms=1000):
        super().__init__()
        gc.disable()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.check)
        self.timer.start(interval_ms)

    def check(self):
        t0, t1, t2 = gc.get_threshold()
        c0, c1, c2 = gc.get_count()
        if c2 > t2:
            gc.collect(2)
        elif c1 > t1:
            gc.collect(1)
        elif c0 > t0:
            gc.collect(0)


def init_logging(debug):
    """Errors always go to logs/app.log; --debug logs every user action to logs/app_debug.log
    and the console. Hard crashes (segfault, abort) land in logs/crash.log via faulthandler."""
    os.makedirs(LOGDIR, exist_ok=True)
    handlers = [logging.FileHandler(os.path.join(LOGDIR, "app_debug.log" if debug else "app.log"), encoding="utf-8")]
    if debug and sys.stderr:
        handlers.append(logging.StreamHandler(sys.stderr))
    logging.basicConfig(level=logging.DEBUG if debug else logging.WARNING, handlers=handlers,
                        format="%(asctime)s %(levelname)-7s [%(threadName)s] %(message)s")
    crash = open(os.path.join(LOGDIR, "crash.log"), "a", encoding="utf-8")
    crash.write(f"\n=== start {time.strftime('%Y-%m-%d %H:%M:%S')} pid {os.getpid()} debug={debug} ===\n")
    crash.flush()
    faulthandler.enable(crash, all_threads=True)
    init_logging.crash_file = crash             # keep the file open for faulthandler
    sys.excepthook = lambda t, v, tb: LOG.critical("unhandled exception", exc_info=(t, v, tb))
    threading.excepthook = lambda a: LOG.critical("unhandled exception in thread %s",
                                                  a.thread.name if a.thread else "?",
                                                  exc_info=(a.exc_type, a.exc_value, a.exc_traceback))
    levels = {QtMsgType.QtDebugMsg: logging.DEBUG, QtMsgType.QtInfoMsg: logging.INFO,
              QtMsgType.QtWarningMsg: logging.WARNING, QtMsgType.QtCriticalMsg: logging.ERROR,
              QtMsgType.QtFatalMsg: logging.CRITICAL}
    qInstallMessageHandler(lambda mode, ctx, msg: LOG.log(levels.get(mode, logging.WARNING), "Qt: %s", msg))
    LOG.warning("start: python %s, %s, debug=%s", sys.version.split()[0], sys.executable, debug)


def selftest():
    """--selftest: what a packaged build must manage without a screen: worker processes,
    a search, the result card in both languages, the Altium and the KiCad export (written
    to data/selftest/, CI opens the KiCad one with KiCad). The report goes to
    data/selftest.log (a windowed .exe has no console); exit code 0 = ok."""
    lines, t0 = [], time.time()
    try:
        w = Main()
        w.st.update(layers=8, plan="SGSPPSGS", names={}, thick=[1.6], inner=[0.5], outer=1.0, nofee=False,
                    unit="mil", mode="detail", reqs=std3(), mask=None)
        spec = w._spec()
        res = E.search(spec)
        w._on_done(res, spec)
        lines.append(f"search: {len(res)} stacks, shown {len(w.results)}, {time.time() - t0:.1f}s")
        r = w.cur
        args = (w._stack(r), spec["plan"], r, spec["reqs"])
        kw = dict(names=spec["names"], finish=spec["finish"], mask=spec["mask"], lim=spec["lim"])
        out = P.data("selftest")
        os.makedirs(out, exist_ok=True)
        text = A.build(*args, **kw)
        A.save(os.path.join(out, "selftest.stackup"), text)
        n = sum(1 for kv in text.split("|") if kv.startswith("TRACEIMPEDANCE_V8_") and "LAYERID=" in kv)
        lines.append(f"altium: {r['name']}, {len(text)} chars, {n} trace impedances")
        files = K.build(*args, **kw)
        K.save(os.path.join(out, "selftest.kicad_pcb"), files)
        nk = files[".kicad_dru"].count("(rule ")
        lines.append(f"kicad: {files['.kicad_pcb'].count('(type \"copper\")')} copper layers, "
                     f"{len(files['.kicad_pro']['net_settings']['classes']) - 1} net classes, {nk} rules")
        w._lang_changed("en")
        lines.append(f"english: {w.lbl_status.text()}")
        ok = bool(res) and r is not None and n >= len(spec["reqs"]) and nk == n
    except Exception:
        LOG.exception("selftest failed")
        import traceback
        lines.append(traceback.format_exc())
        ok = False
    lines.append(f"{'OK' if ok else 'FAILED'} (program {P.APP_DIR}, data {P.DATA}, frozen={P.FROZEN}, "
                 f"python {sys.version.split()[0]})")
    with open(P.data("selftest.log"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    if sys.stdout:
        print("\n".join(lines))
    return 0 if ok else 1


def main():
    debug = "--debug" in sys.argv
    test = "--selftest" in sys.argv
    if test:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        if sys.platform == "win32":             # the offscreen platform does not find Windows fonts itself
            os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))
    init_logging(debug)
    if sys.executable.lower().endswith("pythonw.exe"):
        # workers would otherwise start under python.exe and open console windows
        multiprocessing.set_executable(sys.executable)
    QLocale.setDefault(QLocale(QLocale.English, QLocale.UnitedStates))     # 3.5, not 3,5 — like mils in AD
    app = QApplication([a for a in sys.argv if a not in ("--debug", "--selftest")])
    app.gc_guard = MainThreadGC()
    setup(app)
    if test:
        sys.exit(selftest())
    w = Main()
    w.show()
    code = app.exec()
    LOG.warning("exit code %s", code)
    sys.exit(code)
