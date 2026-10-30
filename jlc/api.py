"""Online access to JLCPCB's impedance tool (verified 2026-10-30).

Endpoints (all POST JSON, no auth, plain curl/urllib works):
  /api/jlcTools/impedance/selectPageImpedanceDefaultTemplate  stackup templates
      body {"pageNum":N,"pageSize":100} (+ optional plateLayerNumber,
      plateThickness, innerCopperThickness filters); body.total / body.list.
      The page parameter is pageNum: currentPage is silently ignored.
  /api/jlcTools/impedance/selectPageImpedancePicture           solver models
  /api/jlcTools/impedance/impedance-config/copper-trace-width/list  T1 / etch
  /api/jlcTools/impedance/impedance-config/coverlay/list       solder mask C1..C3
  /api/jlcTools/impedance/calc                                 Si9000 solve
      body {accessId, impedance_calc_mark, paramMd5, impedance_calc_arg, uuid}
      returns immediately; the result arrives on the websocket
      wss://tools.jlc.com/jlcTools/webSocket/<uuid> as JSON with the same accessId.

Only forward solves work (W -> Z). The "W2_<model>" back-solve marks fail
without extra parameters, so widths are found by bisection on top of calc().
"""
import base64
import concurrent.futures as cf
import json
import os
import socket
import ssl
import struct
import threading
import time
import urllib.request
import uuid

BASE = "https://jlcpcb.com/api/jlcTools/impedance"
WS_HOST = "tools.jlc.com"
HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://jlcpcb.com/pcb-impedance-calculator",
    "Content-Type": "application/json",
}


def post(path, body, timeout=60):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(), headers=HEADERS, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


# ---------------------------------------------------------------- templates

_TPL_KEYS = ("appointName", "receptionDisplayName", "plateLayerNumber", "plateThickness",
             "innerCopperThickness", "cuprumThickness", "urgentState", "defaultFlag",
             "receptionDefaultFlag", "chargeFlag", "laminationType", "boardType", "copperFoilType",
             "impedanceDefaultTemplateAccessId", "createTime", "auditStatus", "deskSort")
_LAYER_KEYS = ("layerName", "material", "materialName", "materialType", "dielectricConstant",
               "dielectricThick", "topConductorThick", "botConductorThick", "specificationsModel",
               "glueContent", "copperThickness", "residualCopperRate", "residualCopperRateDown",
               "brandType", "supplierType")


def _trim(t):
    out = {k: t.get(k) for k in _TPL_KEYS}
    out["layers"] = [{k: y.get(k) for k in _LAYER_KEYS} for y in t.get("basicDataList") or []]
    out["layersBeforePress"] = [{k: y.get(k) for k in _LAYER_KEYS} for y in t.get("beforeCountPpDataList") or []]
    vo = t.get("impedanceComponentStructResVO") or {}
    if any(vo.get(k) for k in ("viaHoleInfoList", "massLaminationList", "componentList")):
        out["structure"] = vo
    return out


def fetch_templates(page_size=200, log=print, **filters):
    """All stackup templates (every layer count, HDI and custom ones included)."""
    res, page, total = {}, 1, None
    while True:
        body = dict(filters, pageNum=page, pageSize=page_size)
        d = post("/selectPageImpedanceDefaultTemplate", body)["body"]
        total = d["total"]
        for t in d["list"]:
            res[t["impedanceDefaultTemplateAccessId"]] = _trim(t)
        log(f"  templates page {page}: {len(res)}/{total}")
        if not d["list"] or page * page_size >= total:
            return list(res.values())
        page += 1


ORDER_URL = "https://cart.jlcpcb.com/api/overseas-core-platform/shoppingCart/getImpedanceTemplateSettings"


def fetch_order_options(layers, thickness, inner_oz, outer_oz=1):
    """Stackups the quote form actually offers for these specs, with surcharge
    (fixedFee / coefficient), expedited flag and enable flag."""
    body = {"stencilLayer": layers, "stencilPly": thickness, "cuprumThickness": outer_oz,
            "insideCuprumThickness": inner_oz}
    req = urllib.request.Request(ORDER_URL, data=json.dumps(body).encode(), method="POST",
                                 headers=dict(HEADERS, Referer="https://cart.jlcpcb.com/quote"))
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.loads(r.read().decode("utf-8")).get("data") or []
    keep = ("templateName", "showName", "stencilLayer", "stencilPly", "cuprumThickness",
            "insideCuprumThickness", "defaultFlag", "expeditedFlag", "enableFlag", "sort",
            "fixedFee", "coefficient", "compressionThickness", "laminationType", "impedanceTemplateCode")
    return [{k: x.get(k) for k in keep} for x in data]


PRICE_URL = "https://cart.jlcpcb.com/api/overseas-core-platform/shoppingCart/calculationGoodsCostsNew"


def fetch_price(layers, thickness, inner_oz, outer_oz=1, template_code=None, width=100, length=100, qty=5):
    """Real quote-form price in USD. NB: fixedFee/coefficient of the template list do NOT
    show the stackup surcharge; this does (orderCountTolls.stackupMoney).
    Returns {stackup, inner_cu, regular, special} (special = promo price shown, if any)."""
    from .paths import data
    body = json.load(open(data("price_request.json"), encoding="utf-8"))
    g = body["pcbGoodsRequest"]
    g.update(stencilLayer=layers, stencilPly=f"{thickness:g}", insideCuprumThickness=inner_oz,
             cuprumThickness=outer_oz, stencilWidth=width, stencilLength=length, stencilWidthInch=width,
             stencilLengthInch=str(length), stencilCounts=qty,
             impedanceFlag="yes" if template_code else "no", impedanceTemplateCode=template_code)
    req = urllib.request.Request(PRICE_URL, data=json.dumps(body).encode(), method="POST",
                                 headers=dict(HEADERS, Referer="https://cart.jlcpcb.com/quote"))
    with urllib.request.urlopen(req, timeout=60) as r:
        d = json.loads(r.read().decode("utf-8"))["data"]
    t = d.get("orderCountTolls") or {}
    return {"stackup": t.get("stackupMoney") or 0.0, "inner_cu": t.get("insideCuprumThicknessMoney") or 0.0,
            "regular": t.get("dummyMoney"), "special": d.get("specialOfferShowAmount")}


def fetch_configs():
    return {
        "copper_trace_width": post("/impedance-config/copper-trace-width/list", {})["data"],
        "coverlay": post("/impedance-config/coverlay/list", {})["data"],
        "pictures": post("/selectPageImpedancePicture", {"currentPage": 1, "pageSize": 100})["body"]["list"],
    }


# ---------------------------------------------------------------- websocket

class _WS:
    """Minimal RFC 6455 client: text frames in, pong/close out."""

    def __init__(self, host, path, tries=3):
        for attempt in range(tries):        # tools.jlc.com sometimes drops the first connects
            try:
                raw = socket.create_connection((host, 443), timeout=10)
                break
            except OSError:
                if attempt == tries - 1:
                    raise
                time.sleep(1 + attempt)
        raw.settimeout(30)
        self.s = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
        key = base64.b64encode(os.urandom(16)).decode()
        self.s.sendall((f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\n"
                        f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                        f"Sec-WebSocket-Version: 13\r\nOrigin: https://jlcpcb.com\r\n"
                        f"User-Agent: Mozilla/5.0\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.s.recv(4096)
            if not chunk:
                raise ConnectionError("websocket handshake closed")
            head += chunk
        status, _, self.buf = head.partition(b"\r\n\r\n")
        if b" 101 " not in status.split(b"\r\n")[0]:
            raise ConnectionError(status.split(b"\r\n")[0].decode(errors="replace"))

    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.s.recv(65536)
            if not chunk:
                raise ConnectionError("websocket closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _send(self, opcode, payload=b""):
        mask = os.urandom(4)
        n = len(payload)
        hdr = bytes([0x80 | opcode])
        hdr += bytes([0x80 | n]) if n < 126 else bytes([0x80 | 126]) + struct.pack(">H", n)
        self.s.sendall(hdr + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def recv(self):
        msg = b""
        while True:
            b0, b1 = self._read(2)
            n = b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            mask = self._read(4) if b1 & 0x80 else None
            data = self._read(n)
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            op = b0 & 0x0F
            if op == 9:
                self._send(10, data)
                continue
            if op == 8:
                raise ConnectionError("websocket closed by server")
            msg += data
            if b0 & 0x80 and op in (0, 1, 2):
                return msg.decode("utf-8", errors="replace")

    def close(self):
        try:
            self._send(8)
            self.s.close()
        except OSError:
            pass


class Solver:
    """JLC's Si9000 backend. calc_many() fires requests in parallel and
    collects the websocket replies; results are None when JLC reports an error."""

    def __init__(self, workers=16, timeout=60):
        self.uid = str(uuid.uuid4())
        self.ws = _WS(WS_HOST, "/jlcTools/webSocket/" + self.uid)
        self.ws.s.settimeout(None)
        self.results, self.cv = {}, threading.Condition()
        self.workers, self.timeout = workers, timeout
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        while True:
            try:
                d = json.loads(self.ws.recv())
            except (ConnectionError, OSError, ValueError):
                return
            with self.cv:
                self.results[d.get("accessId")] = d
                self.cv.notify_all()

    def _post(self, aid, mark, arg):
        body = {"accessId": aid, "impedance_calc_mark": mark, "paramMd5": uuid.uuid4().hex,
                "impedance_calc_arg": arg, "uuid": self.uid}
        for attempt in range(3):
            try:
                return post("/calc", body, timeout=30)
            except OSError:
                time.sleep(1 + attempt)

    def calc_many(self, jobs):
        """jobs: [(model, arg_dict)] -> [impedance or None]"""
        ids = [str(uuid.uuid4()) for _ in jobs]
        with cf.ThreadPoolExecutor(self.workers) as ex:
            list(ex.map(lambda p: self._post(p[0], *p[1]), zip(ids, jobs)))
        end = time.time() + self.timeout
        with self.cv:
            while time.time() < end and any(i not in self.results for i in ids):
                self.cv.wait(0.5)
        out = []
        for i in ids:
            r = self.results.pop(i, None)
            ok = r and r.get("impedance_calc_status") == 0 and r.get("impedance_calc_result")
            out.append(round(r["impedance_calc_result"]["dImpedance"], 3) if ok else None)
        return out

    def close(self):
        self.ws.close()
