"""Telefonmus - styr PC'ens mus fra telefonen over WiFi.

Kør:    python telefonmus.py   (eller den byggede Telefonmus.exe)
Byg:    build.bat
Scan QR-koden i vinduet med telefonen (samme WiFi som PC'en).
"""
import base64
import ctypes
import hashlib
import io
import json
import os
import secrets
import socket
import struct
import subprocess
import sys
import threading
import time
import tkinter as tk
from ctypes import wintypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tkinter import messagebox
from urllib.parse import parse_qs, urlparse

import qrcode
from PIL import Image, ImageDraw

PORT = 8765
VIEW_WIDTH = 1280      # max bredde på billedet der sendes (skaleres kun ned når man zoomer helt ud)
VIEW_FPS = 60          # max billeder pr. sekund mens noget sker
VIEW_IDLE_FPS = 10     # hvor tit der tjekkes for ændringer når alt står stille
VIEW_INFLIGHT = 3     # max billeder på vej til telefonen ad gangen
RULE = "Telefonmus"
BASE = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
PAGE = (BASE / "pad.html").read_text(encoding="utf-8")
CONNECTED = 0  # antal forbundne telefoner
HELD = set()   # museknapper telefonen holder nede lige nu
APPDIR = Path(os.environ.get("APPDATA", Path.home())) / "Telefonmus"
APPDIR.mkdir(parents=True, exist_ok=True)
LOGFILE = APPDIR / "log.txt"
_log_lock = threading.Lock()


def log(text):
    import datetime
    line = f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S}  {text}\n".replace(TOKEN, "***")
    with _log_lock:
        try:
            with open(LOGFILE, "a", encoding="utf-8") as f:
                f.write(line)
        except OSError:
            pass


if sys.stderr is None:  # --windowed exe: send fejl/tracebacks til logfilen
    sys.stderr = open(LOGFILE, "a", encoding="utf-8", buffering=1)


def load_token():
    """Fast kode gemt i %APPDATA%, så et bogmærke på telefonen bliver ved at virke."""
    f = APPDIR / "kode.txt"
    try:
        t = f.read_text().strip()
        if len(t) >= 8:
            return t
    except OSError:
        pass
    t = secrets.token_urlsafe(6)
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(t)
    except OSError:
        pass
    return t


TOKEN = load_token()

# ---------------------------------------------------------------- Windows input
user32 = ctypes.WinDLL("user32", use_last_error=True)
ULONG_PTR = ctypes.c_size_t

class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]

class _U(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]

class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _U)]

MOVE, WHEEL, HWHEEL = 0x0001, 0x0800, 0x1000
BTN = {"left": (0x0002, 0x0004), "right": (0x0008, 0x0010), "middle": (0x0020, 0x0040)}
KEY_UP, KEY_UNICODE = 0x0002, 0x0004
VK = {"enter": 0x0D, "backspace": 0x08, "tab": 0x09, "esc": 0x1B, "left": 0x25,
      "up": 0x26, "right": 0x27, "down": 0x28, "space": 0x20, "delete": 0x2E}


def _send(*inputs):
    arr = (INPUT * len(inputs))(*inputs)
    user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))


def mouse(flags, dx=0, dy=0, data=0):
    _send(INPUT(type=0, mi=MOUSEINPUT(dx, dy, data & 0xFFFFFFFF, flags, 0, 0)))


def key_vk(vk):
    _send(INPUT(type=1, ki=KEYBDINPUT(vk, 0, 0, 0, 0)),
          INPUT(type=1, ki=KEYBDINPUT(vk, 0, KEY_UP, 0, 0)))


def type_text(text):
    for ch in text:
        if ch == "\n":
            key_vk(VK["enter"])
            continue
        units = ch.encode("utf-16-le")
        for i in range(0, len(units), 2):
            code = int.from_bytes(units[i:i + 2], "little")
            _send(INPUT(type=1, ki=KEYBDINPUT(0, code, KEY_UNICODE, 0, 0)),
                  INPUT(type=1, ki=KEYBDINPUT(0, code, KEY_UNICODE | KEY_UP, 0, 0)))


ARROW = [(0, 0), (0, 17), (4, 13), (7, 20), (10, 19), (7, 12), (12, 12)]

# ---------------------------------------------------------------- skærme og skærmbilleder
gdi32 = ctypes.WinDLL("gdi32")
for fn, res, args in [
    (user32.GetDC, wintypes.HDC, [wintypes.HWND]),
    (user32.ReleaseDC, ctypes.c_int, [wintypes.HWND, wintypes.HDC]),
    (user32.MonitorFromPoint, wintypes.HMONITOR, [wintypes.POINT, wintypes.DWORD]),
    (gdi32.CreateCompatibleDC, wintypes.HDC, [wintypes.HDC]),
    (gdi32.CreateCompatibleBitmap, wintypes.HBITMAP, [wintypes.HDC, ctypes.c_int, ctypes.c_int]),
    (gdi32.SelectObject, wintypes.HGDIOBJ, [wintypes.HDC, wintypes.HGDIOBJ]),
    (gdi32.BitBlt, wintypes.BOOL, [wintypes.HDC] + [ctypes.c_int] * 4 + [wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD]),
    (gdi32.GetDIBits, ctypes.c_int, [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
                                     ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]),
    (gdi32.DeleteObject, wintypes.BOOL, [wintypes.HGDIOBJ]),
    (gdi32.DeleteDC, wintypes.BOOL, [wintypes.HDC]),
]:
    fn.restype, fn.argtypes = res, args


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


def cursor_pos():
    pt = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return pt


def monitor_rect(pt):
    """(left, top, right, bottom) for den skærm punktet ligger på."""
    info = MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO))
    user32.GetMonitorInfoW(user32.MonitorFromPoint(pt, 2), ctypes.byref(info))
    r = info.rcMonitor
    return r.left, r.top, r.right, r.bottom


def all_monitors():
    rects = []
    CB = ctypes.WINFUNCTYPE(ctypes.c_int, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)
    cb = CB(lambda h, d, r, l: rects.append((r.contents.left, r.contents.top, r.contents.right, r.contents.bottom)) or 1)
    user32.EnumDisplayMonitors(None, None, cb, 0)
    return sorted(rects)  # venstre mod højre


def next_monitor(step=1):
    """Hop musen til midten af næste skærm (venstre→højre, rundt)."""
    mons = all_monitors()
    cur = monitor_rect(cursor_pos())
    i = mons.index(cur) if cur in mons else 0
    l, t, r, b = mons[(i + step) % len(mons)]
    user32.SetCursorPos((l + r) // 2, (t + b) // 2)


class Grabber:
    """Hurtige skærmbilleder af et område (virtuelle skærmkoordinater, alle skærme).
    Genbruger Windows' tegneflader mellem billeder; bruges fra én tråd."""

    def __init__(self):
        self.sdc = user32.GetDC(None)
        self.mdc = gdi32.CreateCompatibleDC(self.sdc)
        self.bmp, self.size, self.old = None, None, None

    def grab(self, x0, y0, w, h):
        """Returnerer de rå BGRX-bytes for området."""
        if self.size != (w, h):
            if self.bmp:
                gdi32.SelectObject(self.mdc, self.old)
                gdi32.DeleteObject(self.bmp)
            self.bmp = gdi32.CreateCompatibleBitmap(self.sdc, w, h)
            self.old = gdi32.SelectObject(self.mdc, self.bmp)
            self.size = (w, h)
            self.bmi = BITMAPINFOHEADER(biSize=ctypes.sizeof(BITMAPINFOHEADER), biWidth=w, biHeight=-h,
                                        biPlanes=1, biBitCount=32)
            self.buf = ctypes.create_string_buffer(w * h * 4)
        gdi32.BitBlt(self.mdc, 0, 0, w, h, self.sdc, x0, y0, 0x00CC0020)  # SRCCOPY
        gdi32.GetDIBits(self.mdc, self.bmp, 0, h, self.buf, ctypes.byref(self.bmi), 0)
        return self.buf.raw

    def close(self):
        if self.bmp:
            gdi32.SelectObject(self.mdc, self.old)
            gdi32.DeleteObject(self.bmp)
        gdi32.DeleteDC(self.mdc)
        user32.ReleaseDC(None, self.sdc)


def screenshot_jpeg(grabber, crop_w, aspect, last_key=None):
    """Udsnit omkring musen, holdt inden for den skærm musen er på.
    crop_w = bredde i skærm-pixels, aspect = bredde/højde.
    Returnerer (jpeg-bytes eller None hvis intet er ændret, (x0, y0, bredde, højde), key)."""
    pt = cursor_pos()
    ml, mt, mr, mb = monitor_rect(pt)
    sw, sh = mr - ml, mb - mt
    cw = max(64, min(int(crop_w), sw))
    ch = min(max(64, round(cw / aspect)), sh)
    cw = min(sw, round(ch * aspect)) if ch == sh else cw
    x0 = min(max(pt.x - cw // 2, ml), mr - cw)
    y0 = min(max(pt.y - ch // 2, mt), mb - ch)
    bbox = (x0, y0, cw, ch)
    raw = grabber.grab(x0, y0, cw, ch)
    key = (bbox, pt.x, pt.y, raw)  # sammenlignes direkte (memcmp) - billigere end at hashe
    if key == last_key:  # intet nyt på skærmen og musen står stille
        return None, bbox, key
    img = Image.frombuffer("RGB", (cw, ch), raw, "raw", "BGRX", 0, 1)
    factor = -(-cw // VIEW_WIDTH)  # helt udzoomet: skalér hurtigt ned (heltalsfaktor)
    if factor > 1:
        img = img.reduce(factor)
    cx, cy = (pt.x - x0) / factor, (pt.y - y0) / factor
    ImageDraw.Draw(img).polygon([(cx + x, cy + y) for x, y in ARROW], fill="white", outline="black")
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=70)
    return buf.getvalue(), bbox, key


def handle(msg):
    t = msg.get("t")
    if t == "m":
        mouse(MOVE, int(msg["x"]), int(msg["y"]))
    elif t == "s":
        if msg.get("y"):
            mouse(WHEEL, data=int(msg["y"]))
        if msg.get("x"):
            mouse(HWHEEL, data=int(msg["x"]))
    elif t in ("c", "d", "u"):
        b = msg.get("b", "left") if msg.get("b") in BTN else "left"
        down, up = BTN[b]
        if t == "c":
            mouse(down); mouse(up)
        elif t == "d":
            HELD.add(b)
            mouse(down)
        elif b in HELD:
            HELD.discard(b)
            mouse(up)
    elif t == "txt":
        type_text(str(msg.get("s", ""))[:500])
    elif t == "key" and msg.get("k") in VK:
        key_vk(VK[msg["k"]])
    elif t == "mon":
        next_monitor(1 if msg.get("d", 1) >= 0 else -1)


# ---------------------------------------------------------------- HTTP + WebSocket
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        log(f"{self.client_address[0]}  {fmt % args}")

    def do_GET(self):
        url = urlparse(self.path)
        if parse_qs(url.query).get("k", [""])[0] != TOKEN:
            self.send_error(403, "Forkert eller manglende kode")
            return
        if url.path == "/ws" and self.headers.get("Upgrade", "").lower() == "websocket":
            return self.websocket()
        body = PAGE.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def websocket(self):
        global CONNECTED
        key = self.headers["Sec-WebSocket-Key"]
        accept = base64.b64encode(hashlib.sha1(
            (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        CONNECTED += 1
        log(f"{self.client_address[0]}  WebSocket åbnet  ({self.headers.get('User-Agent', '?')})")
        r = self.rfile
        count, why = 0, "forbindelsen lukket"
        self.wlock = threading.Lock()
        self.cond = threading.Condition()
        self.alive, self.view_on, self.force, self.inflight = True, False, False, 0
        self.crop_w, self.aspect, self.bbox = 640, 1.5, None
        self.active = self.last_t = self.next_t = time.monotonic()
        threading.Thread(target=self.view_loop, daemon=True).start()
        try:
            while True:
                head = r.read(2)
                if len(head) < 2:
                    break
                op, n = head[0] & 0x0F, head[1] & 0x7F
                if n == 126:
                    n = struct.unpack(">H", r.read(2))[0]
                elif n == 127:
                    n = struct.unpack(">Q", r.read(8))[0]
                mask = r.read(4) if head[1] & 0x80 else b"\0\0\0\0"
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(r.read(n)))
                if op == 8:
                    why = "telefonen lukkede"
                    break
                if op == 9:
                    self.ws_send(0xA, data)
                    continue
                if op == 1:
                    count += 1
                    try:
                        msg = json.loads(data)
                        t = msg.get("t")
                        if t == "ack":                 # telefonen har vist et billede
                            with self.cond:
                                self.inflight = max(0, self.inflight - 1)
                                self.cond.notify()
                            continue
                        if t == "view":                # slå skærmbillede til/fra, zoom og format
                            with self.cond:
                                if msg.get("on") and not self.view_on:
                                    self.inflight = 0
                                self.view_on = bool(msg.get("on"))
                                if msg.get("w"):
                                    self.crop_w = float(msg["w"])
                                if msg.get("a"):
                                    self.aspect = min(max(float(msg["a"]), 0.3), 4.0)
                                self.force = True
                        elif t == "abs" and self.bbox:  # tryk på billedet
                            x0, y0, cw, ch = self.bbox
                            fx = min(max(float(msg["x"]), 0.0), 1.0)
                            fy = min(max(float(msg["y"]), 0.0), 1.0)
                            user32.SetCursorPos(int(x0 + fx * (cw - 1)), int(y0 + fy * (ch - 1)))
                        else:
                            handle(msg)
                        self.wake()
                    except Exception as e:
                        log(f"fejl i besked {data[:80]!r}: {e!r}")
        except Exception as e:
            why = repr(e)
        finally:
            with self.cond:
                self.alive = False
                self.cond.notify()
            CONNECTED -= 1
            log(f"{self.client_address[0]}  WebSocket lukket efter {count} beskeder: {why}")
            # slip KUN knapper telefonen faktisk holder nede ("Hold"). At slippe en knap der
            # ikke er trykket ned, opfatter Windows som et klik (højre = kontekstmenu).
            if CONNECTED == 0:
                for b in list(HELD):
                    HELD.discard(b)
                    mouse(BTN[b][1])
        self.close_connection = True

    def ws_send(self, opcode, payload):
        n = len(payload)
        if n < 126:
            head = bytes([0x80 | opcode, n])
        elif n < 65536:
            head = bytes([0x80 | opcode, 126]) + struct.pack(">H", n)
        else:
            head = bytes([0x80 | opcode, 127]) + struct.pack(">Q", n)
        with self.wlock:
            self.wfile.write(head + payload)

    def wake(self):
        """Noget skete (input fra telefonen): tag næste billede så hurtigt FPS tillader."""
        with self.cond:
            now = time.monotonic()
            self.active = now
            self.next_t = min(self.next_t, self.last_t + 1 / VIEW_FPS)
            self.cond.notify()

    def view_loop(self):
        """Tager skærmbilleder i op til VIEW_FPS. Højst VIEW_INFLIGHT billeder er på vej ad gangen,
        så der ikke hober sig forsinkelse op; uændrede billeder sendes ikke, og når intet sker
        tjekkes kun VIEW_IDLE_FPS gange i sekundet."""
        grabber, last_key = Grabber(), None
        # Windows' timer tikker normalt kun hvert 15,6 ms, hvilket låser os til ~32 fps.
        # Bed om 1 ms-opløsning mens forbindelsen er åben (slippes igen i finally).
        winmm = ctypes.WinDLL("winmm")
        winmm.timeBeginPeriod(1)
        try:
            while True:
                with self.cond:
                    while self.alive:
                        if self.view_on and self.inflight < VIEW_INFLIGHT:
                            rest = self.next_t - time.monotonic()
                            if rest <= 0:
                                break
                            self.cond.wait(rest)
                        else:
                            self.cond.wait(0.5)
                    if not self.alive:
                        break
                    force, self.force = self.force, False
                t0 = time.monotonic()
                try:
                    jpeg, self.bbox, key = screenshot_jpeg(grabber, self.crop_w, self.aspect,
                                                          None if force else last_key)
                except Exception as e:
                    log(f"skærmbillede fejlede: {e!r}")
                    jpeg = None
                if jpeg:
                    with self.cond:
                        self.inflight += 1
                        self.active = t0
                    self.ws_send(0x2, jpeg)
                    last_key = key
                with self.cond:
                    self.last_t = t0
                    fast = t0 - self.active < 1.0
                    self.next_t = t0 + 1 / (VIEW_FPS if fast else VIEW_IDLE_FPS)
        except OSError:
            pass
        finally:
            winmm.timeEndPeriod(1)
            grabber.close()


def local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


# ---------------------------------------------------------------- Firewall
NO_WINDOW = 0x08000000


def firewall_ok():
    r = subprocess.run(["netsh", "advfirewall", "firewall", "show", "rule", f"name={RULE}"],
                       capture_output=True, creationflags=NO_WINDOW)
    return r.returncode == 0


def firewall_add():
    """Beder om administrator-tilladelse (UAC) og åbner porten for alle netværkstyper."""
    args = (f'advfirewall firewall add rule name="{RULE}" dir=in action=allow '
            f"protocol=TCP localport={PORT} profile=any")
    return ctypes.windll.shell32.ShellExecuteW(None, "runas", "netsh", args, None, 0) > 32


# ---------------------------------------------------------------- Vindue
BG, CARD, TXT, DIM, ACC, OK, BAD = "#111317", "#1b1e24", "#e6e8eb", "#8a919c", "#4f8cff", "#3fbf6f", "#d9534f"


class App:
    def __init__(self, root, url):
        self.root, self.url = root, url
        root.title("Telefonmus")
        root.configure(bg=BG, padx=24, pady=20)
        root.resizable(False, False)

        tk.Label(root, text="Telefonmus", bg=BG, fg=TXT, font=("Segoe UI Semibold", 18)).pack()
        tk.Label(root, text="Scan med telefonens kamera (samme WiFi)", bg=BG, fg=DIM,
                 font=("Segoe UI", 10)).pack(pady=(2, 12))

        self.qr = tk.Canvas(root, bg="white", highlightthickness=0)
        self.qr.pack()
        self.draw_qr()

        e = tk.Entry(root, bg=CARD, fg=TXT, relief="flat", justify="center", font=("Consolas", 11),
                     readonlybackground=CARD, width=36)
        e.insert(0, url)
        e.configure(state="readonly")
        e.pack(pady=(14, 6), ipady=6)

        row = tk.Frame(root, bg=BG)
        row.pack(fill="x", pady=(4, 0))
        self.btn(row, "Kopiér adresse", self.copy).pack(side="left", expand=True, fill="x", padx=(0, 4))
        self.fw_btn = self.btn(row, "Tillad i firewall", self.fix_firewall)
        self.fw_btn.pack(side="left", expand=True, fill="x", padx=(4, 0))

        self.status = tk.Label(root, bg=BG, font=("Segoe UI", 10))
        self.status.pack(pady=(14, 0))
        self.fw = firewall_ok()
        self.tick()

    def btn(self, parent, text, cmd):
        return tk.Button(parent, text=text, command=cmd, bg=CARD, fg=TXT, activebackground=ACC,
                         activeforeground="white", relief="flat", font=("Segoe UI", 10), pady=6, cursor="hand2")

    def draw_qr(self):
        q = qrcode.QRCode(border=2, error_correction=qrcode.constants.ERROR_CORRECT_M)
        q.add_data(self.url)
        m = q.get_matrix()
        s = max(4, 240 // len(m))
        self.qr.configure(width=len(m) * s, height=len(m) * s)
        for y, row in enumerate(m):
            for x, v in enumerate(row):
                if v:
                    self.qr.create_rectangle(x * s, y * s, x * s + s, y * s + s, fill="black", width=0)

    def copy(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.url)

    def fix_firewall(self):
        if firewall_add():
            self.root.after(1500, self.recheck)

    def recheck(self):
        self.fw = firewall_ok()

    def tick(self):
        if not self.fw:
            self.status.configure(fg=BAD, text="⚠ Firewallen blokerer måske telefonen – tryk “Tillad i firewall”")
            self.fw_btn.configure(state="normal")
        elif CONNECTED:
            self.status.configure(fg=OK, text=f"● Telefon forbundet ({CONNECTED})")
            self.fw_btn.configure(state="disabled", text="Firewall OK")
        else:
            self.status.configure(fg=DIM, text="○ Venter på telefon…")
            self.fw_btn.configure(state="disabled", text="Firewall OK")
        self.root.after(500, self.tick)


def main():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        pass
    root = tk.Tk()
    try:
        server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    except OSError:
        root.withdraw()
        messagebox.showerror("Telefonmus", f"Port {PORT} er optaget – kører Telefonmus allerede?")
        return
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://{local_ip()}:{PORT}/?k={TOKEN}"
    log(f"--- Startet, adresse {url}")
    App(root, url)
    root.mainloop()


if __name__ == "__main__":
    main()
