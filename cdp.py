# -*- coding: utf-8 -*-
"""
cdp.py —— 极简 Chrome DevTools Protocol 客户端（只用标准库）

用途：驱动本机已安装的 Edge/Chrome 打开页面、执行 JS、读页面文本。
这样不需要 playwright / selenium / chromedriver。

用法：
    import cdp
    br = cdp.launch(port=9333, url="about:blank", headless=False)
    pg = cdp.Page(port=9333)
    pg.navigate("http://172.172.255.20/eportal/login.jsp")
    print(pg.title(), pg.eval("document.forms.length"))
    br.close()
"""

import base64
import json
import os
import socket
import struct
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request

BROWSER_PATHS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]


# ---------------------------------------------------------------- WebSocket

class WS(object):
    """够用的 WebSocket 客户端：发文本帧、收文本帧、回 pong。"""

    def __init__(self, url, timeout=15.0):
        if not url.startswith("ws://"):
            raise ValueError("只支持 ws://，收到 %r" % url)
        rest = url[5:]
        hostport, _, path = rest.partition("/")
        path = "/" + path
        host, _, port = hostport.partition(":")
        port = int(port or 80)
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        self.buf = b""
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        req = (
            "GET %s HTTP/1.1\r\n"
            "Host: %s:%d\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Key: %s\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n" % (path, host, port, key)
        )
        self.sock.sendall(req.encode("ascii"))
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise IOError("WS 握手被对端关闭")
            head += chunk
            if len(head) > 65536:
                raise IOError("WS 握手响应过大")
        header, _, tail = head.partition(b"\r\n\r\n")
        line = header.split(b"\r\n")[0].decode("latin-1")
        if "101" not in line:
            raise IOError("WS 握手失败: %s" % line)
        self.buf = tail
        self.closed = False

    # ---- 收 ----
    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise IOError("WS 连接已关闭")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _read_frame(self):
        b1, b2 = self._read(2)
        opcode = b1 & 0x0F
        masked = b2 & 0x80
        ln = b2 & 0x7F
        if ln == 126:
            ln = struct.unpack(">H", self._read(2))[0]
        elif ln == 127:
            ln = struct.unpack(">Q", self._read(8))[0]
        mask = self._read(4) if masked else None
        payload = self._read(ln) if ln else b""
        if mask:
            payload = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
        return opcode, payload

    def recv(self):
        while True:
            opcode, payload = self._read_frame()
            if opcode == 8:
                self.closed = True
                raise IOError("WS 收到关闭帧")
            if opcode == 9:                       # ping -> pong
                self._send_frame(10, payload)
                continue
            if opcode in (1, 2):
                return payload.decode("utf-8", "replace")
            # 3/4/5/6/7/11/12 忽略

    # ---- 发 ----
    def _send_frame(self, opcode, payload):
        mask = os.urandom(4)
        masked = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
        ln = len(payload)
        head = bytearray([0x80 | opcode])
        if ln < 126:
            head.append(0x80 | ln)
        elif ln < 65536:
            head.append(0x80 | 126)
            head += struct.pack(">H", ln)
        else:
            head.append(0x80 | 127)
            head += struct.pack(">Q", ln)
        head += mask
        self.sock.sendall(bytes(head) + masked)

    def send(self, text):
        self._send_frame(1, text.encode("utf-8"))

    def close(self):
        try:
            self._send_frame(8, b"")
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass
        self.closed = True


# ---------------------------------------------------------------- 浏览器进程

def find_browser():
    for p in BROWSER_PATHS:
        if os.path.exists(p):
            return p
    raise RuntimeError("没找到 Edge/Chrome，检查安装路径")


def _http_json(port, path, timeout=2.0):
    url = "http://127.0.0.1:%d%s" % (port, path)
    req = urllib.request.Request(url, headers={"Host": "127.0.0.1:%d" % port})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def launch(port=9333, url="about:blank", headless=False, profile=None, wait=20.0):
    """起一个带调试端口的 Edge，返回 Popen。已在该端口监听则直接复用。"""
    try:
        v = _http_json(port, "/json/version", timeout=1.0)
        if v.get("webSocketDebuggerUrl"):
            return None                        # 复用已有实例
    except Exception:
        pass
    exe = find_browser()
    prof = profile or os.path.join(tempfile.gettempdir(), "cdp-profile-%d" % port)
    args = [exe,
            "--remote-debugging-port=%d" % port,
            "--user-data-dir=%s" % prof,
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-networking",
            "--disable-features=Translate,MediaRouter"]
    if headless:
        args.append("--headless=new")
    args.append(url)
    p = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    t0 = time.time()
    while time.time() - t0 < wait:
        try:
            v = _http_json(port, "/json/version", timeout=1.0)
            if v.get("webSocketDebuggerUrl"):
                return p
        except Exception:
            pass
        time.sleep(0.3)
    raise RuntimeError("浏览器调试端口 %d 在 %.0f 秒内没起来" % (port, wait))


def targets(port):
    return _http_json(port, "/json/list")


def find_page(port, want_url=None, new_tab_url=None):
    """找 type=page 的目标；优先 URL 命中，其次空白页，最后新建。"""
    items = [t for t in targets(port) if t.get("type") == "page"]
    if want_url:
        for t in items:
            if want_url.split("?")[0] in (t.get("url") or ""):
                return t
    for t in items:
        u = t.get("url") or ""
        if u in ("about:blank", "", "edge://newtab/", "chrome://newtab/"):
            return t
    if items:
        return items[0]
    # 新建标签页（新版 Chromium 要求 PUT）
    path = "/json/new?" + urllib.parse.quote(new_tab_url or "about:blank", safe="")
    url = "http://127.0.0.1:%d%s" % (port, path)
    req = urllib.request.Request(url, method="PUT")
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


# ---------------------------------------------------------------- 页面控制

class Page(object):
    def __init__(self, port=9333, want_url=None, timeout=20.0):
        self.port = port
        self.id = 0
        self.events = []
        self.dialogs = []
        tgt = find_page(port, want_url=want_url)
        self.target = tgt
        self.ws = WS(tgt["webSocketDebuggerUrl"], timeout=timeout)
        self.send("Page.enable")
        self.send("Runtime.enable")

    # ---- 命令 ----
    def send(self, method, params=None, timeout=20.0):
        self.id += 1
        mid = self.id
        msg = {"id": mid, "method": method, "params": params or {}}
        self.ws.send(json.dumps(msg))
        t0 = time.time()
        while time.time() - t0 < timeout:
            raw = self.ws.recv()
            try:
                obj = json.loads(raw)
            except Exception:
                continue
            if obj.get("id") == mid:
                if "error" in obj:
                    raise RuntimeError("%s 失败: %s" % (method, obj["error"]))
                return obj.get("result", {})
            if obj.get("method"):
                self.events.append(obj)
                if obj["method"] == "Page.javascriptDialogOpening":
                    self.dialogs.append((obj.get("params") or {}).get("message", ""))
        raise TimeoutError("%s 超时" % method)

    def pump(self, seconds=2.0):
        """读一段时间的事件（收 alert 文案），顺手把弹出的对话框全部确认掉。"""
        end = time.time() + seconds
        old = self.ws.sock.gettimeout()
        self.ws.sock.settimeout(0.4)
        try:
            while time.time() < end:
                try:
                    raw = self.ws.recv()
                except socket.timeout:
                    continue
                except Exception:
                    break
                try:
                    obj = json.loads(raw)
                except Exception:
                    continue
                if obj.get("method"):
                    self.events.append(obj)
                    if obj["method"] == "Page.javascriptDialogOpening":
                        self.dialogs.append((obj.get("params") or {}).get("message", ""))
        finally:
            try:
                self.ws.sock.settimeout(old)
            except Exception:
                pass
        for _ in range(len(self.dialogs) + 2):
            try:
                self.send("Page.handleJavaScriptDialog", {"accept": True}, timeout=3.0)
            except Exception:
                break
        return self.dialogs

    def navigate(self, url, wait=1.5):
        self.send("Page.navigate", {"url": url})
        time.sleep(wait)
        return self.url()

    def url(self):
        return self.eval("location.href")

    def title(self):
        return self.eval("document.title")

    def eval(self, expr, timeout=15.0):
        r = self.send("Runtime.evaluate", {
            "expression": expr,
            "returnByValue": True,
            "awaitPromise": True,
        }, timeout=timeout)
        res = r.get("result", {})
        if "value" in res:
            return res["value"]
        return res.get("description")

    def text(self, limit=4000):
        t = self.eval("(document.body&&document.body.innerText||'')")
        return (t or "")[:limit]

    def wait_for(self, expr, timeout=15.0, step=0.4):
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                if self.eval(expr):
                    return True
            except Exception:
                pass
            time.sleep(step)
        return False

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


def kill_browser(port=9333):
    """按调试端口找到并结束浏览器进程树（用 wmic/taskkill，保留其它实例）。"""
    try:
        v = _http_json(port, "/json/version", timeout=1.0)
        pid = None
        for line in subprocess.run(
                ["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True,
                errors="replace").stdout.splitlines():
            if (":%d" % port) in line and "LISTENING" in line:
                pid = int(line.split()[-1])
                break
        if pid:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True)
            return pid
    except Exception:
        pass
    return None


if __name__ == "__main__":
    import sys
    url = sys.argv[1] if len(sys.argv) > 1 else "about:blank"
    launch(port=9333, url=url, headless=False)
    pg = Page(9333)
    pg.navigate(url)
    print("url   =", pg.url())
    print("title =", pg.title())
    print("text  =", pg.text(300).replace("\n", " "))
    pg.close()
