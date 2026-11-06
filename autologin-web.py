# -*- coding: utf-8 -*-
"""
autologin-web.py —— 在校园网WiFi下、没有因特网连接时，自动开浏览器把认证页填好并提交。

行为：
  1. 每 N 秒探测一次因特网（HTTP 204 探针，不看 ping）
  2. 不在线 + 在校园网 → 打开 Edge（CDP 驱动），跳到认证页，填账号密码，点登录
  3. 00:00 - 06:30 之内完全不动作（这个时段认证会被服务端拒绝，登也登不上）
  4. 失败按 1.6 倍退避，最长 5 分钟一次；成功则回到 15 秒一次的待命探测

用法：
    python autologin-web.py --diagnose     看当前判据
    python autologin-web.py --once         跑一轮就退出
    python autologin-web.py                常驻（默认）
    python autologin-web.py --force        忽略时间窗口，强制尝试一次
"""

import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CONF_PATH = os.path.join(HERE, "autologin-web.json")
LOG_PATH = os.path.join(HERE, "autologin-web.log")
LOCK_PATH = os.path.join(HERE, "autologin-web.lock")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import cdp  # 同目录


# ---------------------------------------------------------------- 配置 / 日志

DEFAULTS = {
    "user": "",
    "password": "",
    "portal": "http://172.172.255.20/",
    "ac": "10.100.0.235",
    "net": "校园网",
    "campus_gw": "192.168.1.1",
    "skip_start": "00:00",
    "skip_end": "06:30",
    "interval": 15,
    "online_interval": 3600,
    "exit_when_online": False,
    "window_login": False,
    "window_cooldown": 60,
    "cooldown": 30,
    "backoff_factor": 1.6,
    "backoff_max": 300,
    "debug_port": 9333,
    "headless": False,
    "keep_browser": True,
    "login_wait": 25,
    "probes": [
        "http://connect.rom.miui.com/generate_204",
        "http://www.gstatic.com/generate_204",
        "http://223.5.5.5/",
    ],
}


def load_conf():
    conf = dict(DEFAULTS)
    if os.path.exists(CONF_PATH):
        with open(CONF_PATH, "r", encoding="utf-8-sig") as f:
            conf.update(json.load(f))
    return conf


def log(obj):
    obj = dict(obj)
    obj.setdefault("ts", time.strftime("%Y-%m-%d %H:%M:%S"))
    line = json.dumps(obj, ensure_ascii=False)
    print(line, flush=True)
    try:
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > 4 * 1024 * 1024:
            os.replace(LOG_PATH, LOG_PATH + ".1")
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------- 网络判据

def http_probe(url, timeout=4.0):
    req = urllib.request.Request(url, headers={"User-Agent": "net-watch/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            code = r.getcode()
            return code in (200, 204), code
    except urllib.error.HTTPError as e:
        return False, e.code
    except Exception:
        return False, 0


def online(conf):
    codes = []
    for u in conf["probes"]:
        ok, code = http_probe(u)
        codes.append(code)
        if ok:
            return True, codes
    return False, codes


def default_gw():
    try:
        if os.name == "nt":
            out = subprocess.run(["route", "print", "-4"], capture_output=True,
                                 text=True, errors="replace").stdout
            for line in out.splitlines():
                p = line.split()
                if len(p) >= 3 and p[0] == "0.0.0.0" and p[1] == "0.0.0.0":
                    if re.match(r"^\d{1,3}(\.\d{1,3}){3}$", p[2]):
                        return p[2]
        else:
            out = subprocess.run(["ip", "route"], capture_output=True, text=True,
                                 errors="replace").stdout
            m = re.search(r"^default via (\d{1,3}(?:\.\d{1,3}){3})", out, re.M)
            if m:
                return m.group(1)
    except Exception:
        pass
    return ""


def on_campus(conf):
    gw = default_gw()
    if gw and gw == conf.get("campus_gw"):
        return True, "网关=%s" % gw
    try:
        r = urllib.request.urlopen("http://%s/" % conf["ac"], timeout=3)
        r.read(64)
        return True, "认证接入设备 %s 可达" % conf["ac"]
    except Exception:
        pass
    return False, "网关=%s，认证接入设备不可达" % (gw or "无")


# ---------------------------------------------------------------- 时间窗口

def hm(text):
    m = re.match(r"^(\d{1,2}):(\d{2})$", str(text).strip())
    if not m:
        return 0
    return int(m.group(1)) * 60 + int(m.group(2))


def in_skip(conf, now=None):
    now = now or time.localtime()
    cur = now.tm_hour * 60 + now.tm_min
    a, b = hm(conf["skip_start"]), hm(conf["skip_end"])
    if a == b:
        return False
    if a < b:
        return a <= cur < b
    return cur >= a or cur < b            # 跨零点


def secs_until_skip_end(conf, now=None):
    """还要多少秒才走出限制窗口；不在窗口内返回 0。用于窗口内整段睡眠，中途不探测。"""
    now = now or time.localtime()
    if not in_skip(conf, now):
        return 0
    cur = time.mktime(now)
    b = hm(conf["skip_end"])
    end = time.mktime((now.tm_year, now.tm_mon, now.tm_mday, b // 60, b % 60, 0, 0, 0, -1))
    if end <= cur:
        end += 86400
    return int(end - cur)


# ---------------------------------------------------------------- 认证页入口

def ac_entry(conf):
    """从 AC 的重定向页拿到带密文参数的认证页地址（这一步只能在浏览器外做）。"""
    url = "http://%s/" % conf["ac"]

    class NoRedir(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None

    op = urllib.request.build_opener(NoRedir)
    try:
        with op.open(url, timeout=5) as r:
            body = r.read().decode("gb18030", "replace")
            loc = r.headers.get("Location") or ""
    except urllib.error.HTTPError as e:
        body = e.read().decode("gb18030", "replace")
        loc = e.headers.get("Location") or ""
    except Exception:
        return conf["portal"], "AC 不可达，退回门户根地址"

    cand = loc if loc.startswith("http") else ""
    if not cand:
        m = re.search(r"location\.href\s*=\s*['\"]([^'\"]+)['\"]", body)
        if m:
            cand = m.group(1)
    if not cand:
        m = re.search(r"(https?://[^\s'\"<>]+)", body)
        if m:
            cand = m.group(1)
    cand = (cand or "").replace("&amp;", "&")
    if cand:
        return cand, "取自 AC 重定向"
    return conf["portal"], "AC 未给参数，退回门户根地址"


# ---------------------------------------------------------------- 浏览器登录

FILL_JS = """
(function(){
  var U = %s, P = %s, NET = %s;
  function byName(n){ return document.getElementsByName(n)[0]; }
  var u = byName('username'), p = byName('pwd');
  if (!u || !p) return 'noform';
  u.value = U;
  p.value = P;
  try { if (window.selectService) selectService(NET, NET, (%s) + ''); } catch(e) {}
  var sel = document.getElementById('bch_service_0');
  return 'filled|' + u.value + '|' + (p.value ? 'pwd-ok' : 'pwd-empty');
})()
"""


def net_index(conf):
    return {"校园网": 0, "中国联通": 1, "中国移动": 2}.get(conf["net"], 0)


def candidates(conf):
    """按可靠性排的落地页候选。最可靠的是让 AC 用 302 把浏览器带到认证页。"""
    out = []
    entry, src = ac_entry(conf)
    if entry and entry.rstrip("/") != conf["portal"].rstrip("/"):
        out.append((entry, src))
    out.append(("http://connect.rom.miui.com/generate_204", "让 AC 302 把浏览器带到认证页"))
    out.append((conf["portal"], "门户根地址"))
    out.append((conf["portal"].rstrip("/") + "/eportal/login.jsp", "登录页直连"))
    return out


def browser_login(conf):
    """返回 (ok, note)。真判据始终是 HTTP 探针，不是页面话术。"""
    port = int(conf["debug_port"])
    cdp.launch(port=port, url="about:blank", headless=bool(conf["headless"]))
    pg = cdp.Page(port)
    tried = []
    try:
        for entry, src in candidates(conf):
            try:
                cur = pg.navigate(entry, wait=2.5)
            except Exception as e:
                tried.append("%s(%s) 打开失败: %s" % (entry[:40], src, e))
                continue
            if not pg.wait_for("!!document.getElementsByName('pwd')[0]", timeout=8):
                tried.append("%s(%s) 无登录表单" % (entry[:40], src))
                if online(conf)[0]:
                    return True, "页面无登录表单，但网络已通（可能已在线）"
                continue

            js = FILL_JS % (json.dumps(conf["user"]), json.dumps(conf["password"]),
                            json.dumps(conf["net"]), net_index(conf))
            filled = pg.eval(js)
            if str(filled).startswith("noform"):
                tried.append("%s 表单里找不到 username/pwd" % entry[:40])
                continue

            pg.eval("try{doauthen()}catch(e){document.forms[0].submit()}")
            dialogs = pg.pump(3.0)

            deadline = time.time() + int(conf["login_wait"])
            codes = []
            while time.time() < deadline:
                ok, codes = online(conf)
                if ok:
                    note = "登录成功（落地 %s，来源 %s）" % (cur[:50], src)
                    if dialogs:
                        note += "（提示：%s）" % "；".join(d for d in dialogs if d)[:80]
                    return True, note
                dialogs += [d for d in pg.pump(1.0) if d not in dialogs]
                time.sleep(1.5)

            page = (pg.text(300) or "").replace("\n", " ")
            tried.append("%s 提交后未连通 | %s | %s" % (
                entry[:40], "；".join(d for d in dialogs if d)[:80] or "无话术", page[:80]))
        return False, "；".join(tried)[:400]
    finally:
        try:
            pg.close()
        except Exception:
            pass
        if not conf.get("keep_browser"):
            cdp.kill_browser(port)


def http_login(conf):
    """浏览器路线失败时的兜底：直接走 HTTP 链路（已验证可用）。"""
    script = os.path.join(HERE, "net_try.py")
    if not os.path.exists(script):
        return False, "缺少 net_try.py"
    cmd = [sys.executable, script, "--user", conf["user"], "--pass", conf["password"],
           "--only", conf["net"], "--ac", conf["ac"], "--target", conf["portal"].split("//")[-1].rstrip("/")]
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        p = subprocess.run(cmd, cwd=HERE, capture_output=True, env=env, timeout=120)
        out = (p.stdout or b"").decode("utf-8", "replace")
    except Exception as e:
        return False, "HTTP 登录异常: %s" % e
    ok, codes = online(conf)
    note = "HTTP 登录"
    m = re.search(r"alert> (.+)", out)
    if m:
        note += " | " + m.group(1).strip()[:60]
    return ok, note + (" | 已连通" if ok else " | 仍未连通 codes=%s" % codes)


def open_portal(conf, url=None):
    """让用户看见页面：用默认浏览器打开认证页。"""
    try:
        os.startfile(url or conf["portal"])
    except Exception:
        pass


# ---------------------------------------------------------------- 单实例

def pid_alive(pid):
    """只有「PID 存在且进程是 python」才算活着，避免 PID 复用把锁锁死。"""
    if not pid:
        return False
    try:
        out = subprocess.run(["tasklist", "/FI", "PID eq %d" % pid, "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, errors="replace").stdout.strip()
    except Exception:
        return False
    if not out or out.lower().startswith("info:") or out.lower().startswith("错误"):
        return False
    first = out.splitlines()[0].split(",")[0].strip('"').strip().lower()
    return first in ("python.exe", "pythonw.exe")


def acquire_lock():
    if os.path.exists(LOCK_PATH):
        try:
            pid = int(open(LOCK_PATH).read().strip() or 0)
        except Exception:
            pid = 0
        if pid and pid != os.getpid() and pid_alive(pid):
            return False
    try:
        with open(LOCK_PATH, "w") as f:
            f.write(str(os.getpid()))
    except Exception:
        pass
    return True


def release_lock():
    try:
        os.remove(LOCK_PATH)
    except Exception:
        pass


# ---------------------------------------------------------------- 主流程

def attempt(conf, why=""):
    t0 = time.time()
    try:
        ok, note = browser_login(conf)
    except Exception as e:
        ok, note = False, "浏览器路线异常: %s" % e
    if not ok:
        ok2, note2 = http_login(conf)
        if ok2:
            ok, note = True, "浏览器失败(%s) → HTTP 兜底成功(%s)" % (note, note2)
            open_portal(conf)
        else:
            note = "%s ；兜底：%s" % (note, note2)
    log({"event": "login", "ok": ok, "note": note, "why": why,
         "sec": round(time.time() - t0, 1)})
    return ok


def main(argv):
    conf = load_conf()
    mode = "--loop"
    for a in argv[1:]:
        if a in ("--once", "--loop", "--diagnose", "--force"):
            mode = a

    if mode == "--diagnose":
        ok, codes = online(conf)
        camp, why = on_campus(conf)
        print("时间        %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
        print("账号        %s（密码%s）" % (conf["user"] or "(空)", "已填" if conf["password"] else "空"))
        print("默认网关    %s" % (default_gw() or "无"))
        print("在线        %s  (探针状态码 %s)" % ("是" if ok else "否", codes))
        print("在校园网    %s  (%s)" % ("是" if camp else "否", why))
        print("限制窗口    %s - %s  当前%s窗口内（窗口内登录=%s）" % (
            conf["skip_start"], conf["skip_end"],
            "在" if in_skip(conf) else "不在",
            "开" if conf.get("window_login", True) else "关"))
        try:
            print("浏览器      %s" % cdp.find_browser())
        except Exception as e:
            print("浏览器      未找到 (%s)" % e)
        if ok:
            act = "否（已在线，进入 %ss 长睡）" % conf.get("online_interval", 300)
        elif not camp:
            act = "否（不在校园网）"
        else:
            act = "登录（窗口内也试）" if in_skip(conf) else "登录"
        print("应动作      %s" % act)
        return 0

    if not conf["user"] or not conf["password"]:
        log({"event": "abort", "note": "autologin-web.json 里没有账号或密码"})
        return 2

    if mode == "--once":
        ok, codes = online(conf)
        if ok:
            log({"event": "once", "note": "已经在线，无需登录", "codes": codes})
            return 0
        camp, why = on_campus(conf)
        if not camp:
            log({"event": "once", "note": "不在校园网：%s" % why})
            return 1
        if in_skip(conf) and not conf.get("window_login", True):
            log({"event": "once", "note": "在限制窗口内，按配置不登录"})
            return 1
        return 0 if attempt(conf, "once") else 1

    if not acquire_lock():
        log({"event": "abort", "note": "已有实例在跑（autologin-web.lock）"})
        return 3

    force = "--force" in argv
    cooldown = float(conf["cooldown"])
    win_cooldown = float(conf.get("window_cooldown", 60))
    log({"event": "start", "note": "守护启动 interval=%ss 在线后停探=%ss 窗口=%s-%s 窗口内登录=%s" % (
        conf["interval"], conf.get("online_interval", 300), conf["skip_start"],
        conf["skip_end"], conf.get("window_login", True))})
    try:
        while True:
            ok, codes = online(conf)
            if ok:
                # 已能上网：不再做任何探测/登录，进入长睡；配置 exit_when_online 则直接退出
                if conf.get("exit_when_online"):
                    log({"event": "done", "note": "已在线，按配置退出（exit_when_online=true）"})
                    break
                time.sleep(float(conf.get("online_interval", 300)))
                continue

            camp, why = on_campus(conf)
            if not camp:
                log({"event": "wait", "note": "不在校园网：%s" % why})
                time.sleep(float(conf["interval"]))
                continue

            inw = in_skip(conf) and not force

            if inw and not conf.get("window_login", True):
                nap = secs_until_skip_end(conf) or 300
                log({"event": "skip", "note": "限制窗口内，按配置不做任何操作，%d 分 %d 秒后（%s 之后）再看" % (
                    nap // 60, nap % 60, conf["skip_end"])})
                time.sleep(nap)
                continue

            if inw:
                log({"event": "window", "note": "处于 %s-%s 限制窗口内，仍尝试登录（服务端可能拒绝）" % (
                    conf["skip_start"], conf["skip_end"])})

            if attempt(conf, why + ("，窗口内" if inw else "")):
                cooldown = float(conf["cooldown"])
                force = False
                time.sleep(float(conf["interval"]))
                continue

            wait = win_cooldown if inw else cooldown
            log({"event": "backoff", "note": "下次 %.0f 秒后重试" % wait})
            time.sleep(wait)
            if not inw:
                cooldown = min(cooldown * float(conf["backoff_factor"]), float(conf["backoff_max"]))
    except KeyboardInterrupt:
        log({"event": "stop", "note": "手动中断"})
    finally:
        release_lock()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
