#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
stu_login.py -- 学生端真门户 (RG-SAM+ Portal 组件) 分析与登录

为什么是它: /eportal/login.jsp 是 6827B 的可读页面, title
"上网认证 | RG-SAM+ Portal组件", action = user.do?method=login_ajax,
字段 username / pwd / net_access_type。这跟管理端 (admin.do) 是两套东西。

两条命令:
  --js      只抓 /eportal/nodeDir/login_bch.js 并拆开看 (零风险, 先做这个)
  正常跑    用你自己的学号密码 POST 一次, 打印服务器原话

关键点:
  * 这个页面没有验证码字段 -> 学生端登录不校验验证码
  * net_access_type 有三个选项: 校园网 / 中国联通 / 中国移动
    这是三个出口。时段策略很可能只挂在"校园网"这一类上,
    所以在断网窗口里换 --net 中国移动 再试一次, 是成本最低的关键实验。

只登自己的账号。不做字典, 不做枚举。
"""
import sys
import os
import re
import json
import time
import argparse
import urllib.request
import urllib.parse
import http.cookiejar

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
TMO = 10


class NoRedir(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def make_opener():
    cj = http.cookiejar.CookieJar()
    return urllib.request.build_opener(
        NoRedir, urllib.request.HTTPCookieProcessor(cj)), cj


def dec(b, ctype=""):
    ct = (ctype or "").lower()
    order = ("gb18030", "utf-8", "latin-1") if "gb" in ct else ("utf-8", "gb18030", "latin-1")
    for e in order:
        try:
            return b.decode(e)
        except Exception:
            pass
    return b.decode("latin-1", "replace")


def get(op, url, referer=None):
    h = {"User-Agent": UA, "Accept": "*/*",
         "Accept-Language": "zh-CN,zh;q=0.9"}
    if referer:
        h["Referer"] = referer
    req = urllib.request.Request(url, headers=h)
    try:
        r = op.open(req, timeout=TMO)
        body = r.read()
        return r.getcode(), body, dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)
    except Exception as e:
        return 0, str(e).encode(), {}


def post_form(op, url, data, referer=None):
    body = urllib.parse.urlencode(data, encoding="gb18030").encode("ascii")
    h = {"User-Agent": UA, "Accept": "*/*",
         "Accept-Language": "zh-CN,zh;q=0.9",
         "Content-Type": "application/x-www-form-urlencoded; charset=GBK",
         "X-Requested-With": "XMLHttpRequest"}
    if referer:
        h["Referer"] = referer
    req = urllib.request.Request(url, data=body, headers=h)
    try:
        r = op.open(req, timeout=TMO)
        return r.getcode(), r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)
    except Exception as e:
        return 0, str(e).encode(), {}


# ------------------------------------------------------------------ 解析页面
FORM_PAT = re.compile(r"(?is)<form\b([^>]*)>")
INPUT_PAT = re.compile(r"(?is)<(input|select|textarea)\b([^>]*)>")
ATTR_PAT = re.compile(
    r'(?is)\b(name|id|type|value|action|method)\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|([^\s>]+))')
OPT_PAT = re.compile(r"(?is)<option\b([^>]*)>(.*?)</option>")


def attr_map(s):
    d = {}
    for m in ATTR_PAT.finditer(s):
        v = m.group(2) if m.group(2) is not None else (
            m.group(3) if m.group(3) is not None else m.group(4))
        d[m.group(1).lower()] = v or ""
    return d


def parse_page(html):
    out = {"action": "", "method": "post", "fields": {}, "order": [],
           "selects": {}, "net_types": [], "title": ""}
    m = re.search(r"(?is)<title>(.*?)</title>", html)
    if m:
        out["title"] = re.sub(r"\s+", " ", m.group(1)).strip()
    fm = FORM_PAT.search(html)
    if fm:
        a = attr_map(fm.group(1))
        out["action"] = a.get("action", "")
        out["method"] = (a.get("method") or "post").lower()
    # 逐段切 select, 先把 option 拿走, 避免 option 的 value 混进 fields
    body = html
    for sm in re.finditer(r"(?is)<select\b([^>]*)>(.*?)</select>", html):
        sa = attr_map(sm.group(1))
        nm = sa.get("name") or sa.get("id")
        opts = []
        for om in OPT_PAT.finditer(sm.group(2)):
            oa = attr_map(om.group(1))
            txt = re.sub(r"(?s)<[^>]+>", "", om.group(2)).strip()
            opts.append((oa.get("value", txt), txt))
        if nm:
            out["selects"][nm] = opts
            out["fields"][nm] = opts[0][0] if opts else ""
            out["order"].append(nm)
        body = body.replace(sm.group(0), "")
    for im in INPUT_PAT.finditer(body):
        a = attr_map(im.group(2))
        nm = a.get("name") or a.get("id")
        if not nm:
            continue
        out["fields"][nm] = a.get("value", "")
        out["order"].append(nm)
    for k, opts in out["selects"].items():
        if "access" in k or "net" in k:
            out["net_types"] = [t for _, t in opts]
    return out


# ------------------------------------------------------------------ 读 JS
JS_HINTS = ["login_ajax", "net_access_type", "中国移动", "中国联通", "校园网",
            "encrypt", "strEnc", "chksum", "md5", "base64", "callback",
            "ajax", "success", "fail", "error", "timeout", "时段"]


def dig_js(js):
    print("\n[1b] login_bch.js 摘要  (%d 字节)" % len(js))
    urls = []
    for m in re.finditer(r"""["']([^"'\s]{3,120})["']""", js):
        u = m.group(1)
        if re.search(r"\.(do|jsp|action|js|css)\b", u) or u.startswith(("/", "http")):
            if u not in urls:
                urls.append(u)
    print("    含 .do/.jsp 的串 %d 条:" % len(urls))
    for u in urls[:25]:
        print("      " + u)

    cn = []
    for m in re.finditer(r"""["']([^"']{2,60})["']""", js):
        s = m.group(1)
        if re.search(r"[\u4e00-\u9fa5]", s) and s not in cn:
            cn.append(s)
    print("    中文字面量 %d 条 (服务器话术就藏在这里):" % len(cn))
    for s in cn[:30]:
        print("      " + s)

    print("    敏感词命中:")
    for w in JS_HINTS:
        c = js.count(w)
        if c:
            print("      %-16s %d 次" % (w, c))


# ------------------------------------------------------------------ 判读
RULES = [
    ("SUCCESS", ["登录成功", "认证成功", "success\":true", '"result":1', "result:1",
                 "上线成功", "欢迎"]),
    ("TIME_WINDOW", ["不在认证时段", "认证时段", "非允许时段", "禁止上网", "非上网时间",
                     "上网时间", "时段外"]),
    ("BAD_CRED", ["密码错误", "用户不存在", "用户名或密码", "账号或密码", "口令错误",
                  "用户或密码"]),
    ("OPERATOR_UNBOUND", ["运营商账号未绑定", "未绑定运营商", "未开通", "请到自助服务办理",
                          "运营商"]),
    ("BIND_LIMIT", ["绑定", "已达上限", "超过", "设备数", "同时在线", "MAC"]),
    ("ARREARS", ["欠费", "余额不足", "停机", "已过期", "套餐"]),
    ("CAPTCHA", ["验证码"]),
    ("WEBAUTH_NO_DEVICE", ["Web认证接入设备不存在", "接入设备不存在", "没有权限访问该页面"]),
    ("OFFLINE_ACC", ["已注销", "未在线", "离线"]),
]


def classify(text):
    low = text.lower()
    for tag, kws in RULES:
        for k in kws:
            if k.lower() in low:
                return tag, k
    return "UNKNOWN", ""


NETS = ["校园网", "中国移动", "中国联通"]


def fix_enc(s):
    """修 Windows 下中文参数被按 cp936/latin-1 转坏的情况"""
    if not s:
        return s
    try:
        return s.encode("latin-1").decode("utf-8")
    except Exception:
        return s


def norm_net(s):
    s = fix_enc((s or "").strip())
    if s in NETS:
        return s, ""
    if "移动" in s:
        return "中国移动", "按关键字归一"
    if "联通" in s:
        return "中国联通", "按关键字归一"
    if "校园" in s:
        return "校园网", "按关键字归一"
    return s, "不在已知三项里, 原样发送"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default="172.172.255.20:80")
    ap.add_argument("--base", default="")
    ap.add_argument("--js", action="store_true", help="只抓 login_bch.js 分析, 不登录")
    ap.add_argument("--user", default="")
    ap.add_argument("--pass", dest="pwd", default="")
    ap.add_argument("--net", default="校园网", help="校园网 / 中国移动 / 中国联通")
    ap.add_argument("--redir", default="",
                    help="AC 的重定向地址, 例如 http://10.100.0.235/ 。"
                         "不带它就会报 Web认证接入设备不存在")
    ap.add_argument("--params", default="",
                    help="直接给 wlanuserip=...&nasip=...&wlanacname=...&mac=... 这串")
    ap.add_argument("--dry", action="store_true", help="只构造请求不发送")
    ap.add_argument("--loop", type=int, default=1, help="重复尝试次数")
    ap.add_argument("--interval", type=float, default=5.0)
    ap.add_argument("--out", default="stu-login.txt")
    a = ap.parse_args()

    base = a.base or ("http://%s/eportal/" % a.target)
    page_url = base + "login.jsp"
    js_url = base + "nodeDir/login_bch.js"

    lines = []

    def say(s=""):
        print(s)
        lines.append(s)

    say("=" * 74)
    say("  学生端门户 (RG-SAM+ Portal 组件)   %s" % page_url)
    say("=" * 74)

    op, cj = make_opener()

    # ---- [0] NAS 上下文。上一版就死在这里: 裸登 login.jsp 没有 wlanuserip/nasip,
    #      ePortal 不知道该找哪台 AC, 于是回 "Web认证接入设备不存在,请联系管理员!"
    qs = ""
    if a.params:
        qs = a.params.lstrip("?")
        say("[0] 用 --params 提供的上下文, %d 个参数" % len(urllib.parse.parse_qsl(qs)))
    elif a.redir:
        st0, b0, h0 = get(op, a.redir)
        t0 = dec(b0, h0.get("Content-Type", ""))
        m = (re.search(r"""location\.href\s*=\s*['"]([^'"]+)['"]""", t0)
             or re.search(r"""(https?://[^\s'"<>]+)""", t0))
        if m:
            full = m.group(1).replace("&amp;", "&")
            qs = urllib.parse.urlsplit(full).query
            pairs = urllib.parse.parse_qsl(qs)
            say("[0] 从 %s 拿到 NAS 上下文: %d 个参数" % (a.redir, len(pairs)))
            for k, v in pairs:
                say("      %-14s %s" % (k, v[:48]))
        else:
            say("[0] 重定向页里没找到 eportal URL (%s, %dB), 原文:" % (st0, len(b0)))
            say("    " + re.sub(r"\s+", " ", t0)[:300])
        if qs:
            entry = base + "index.jsp?" + qs
            st1, b1, h1 = get(op, entry, referer=a.redir)
            say("    入口 %s" % entry[:110])
            say("    -> %s  %dB  %s" % (st1, len(b1), h1.get("Location", "")))
            page_url = base + "login.jsp?" + qs
    if not qs:
        say("[0] 没有 NAS 上下文 —— 直接 POST 会被回「Web认证接入设备不存在」。")
        say("    加 --redir http://10.100.0.235/ 或 --params \"wlanuserip=...&nasip=...\"")

    # ---- [1] 抓页面
    st, body, hd = get(op, page_url)
    if st == 0:
        say("[!] 页面取不到: %s" % body[:80])
        say("    现在是不是在断网窗口里? 那个窗口连 172.172.255.20 也可能不通。")
        return 1
    html = dec(body, hd.get("Content-Type", ""))
    info = parse_page(html)
    say("\n[1] %s  %s  %dB  title=%s" % (page_url, st, len(body), info["title"]))
    say("    action = %s   method = %s" % (info["action"], info["method"].upper()))
    say("    字段 %d 个:" % len(info["fields"]))
    for k in info["order"]:
        v = info["fields"].get(k, "")
        if k in info["selects"]:
            opts = " | ".join("%s=%s" % (ov, ot) for ov, ot in info["selects"][k])
            say("      %-24s select   %s" % (k, opts))
        else:
            say("      %-24s %s" % (k, ("= " + v[:40]) if v else ""))
    if info["net_types"]:
        say("\n    >>> net_access_type 的选项: %s" % " / ".join(info["net_types"]))
        say("        三个出口。时段策略很可能只挂在「校园网」这一类上,")
        say("        断网窗口里换 --net 中国移动 再试一次, 是成本最低的关键实验。")
    else:
        found = [w for w in ("校园网", "中国联通", "中国移动") if w in html]
        if len(found) >= 2:
            say("\n    >>> 页面上出现出口字样: %s" % " / ".join(found))
            say("        net_access_type 是 hidden input, 三个出口靠 JS 切换。")
            say("        断网窗口里改 --net 中国移动 再试一次 —— 若话术变成「运营商账号")
            say("        未绑定」之类, 说明时段只挂校园网出口; 若还是「不在认证时段内」,")
            say("        说明封的是账号, 不是出口。")
    say("    验证码字段: %s" % ("有" if any("valid" in k.lower() or "captcha" in k.lower()
                                       for k in info["fields"]) else
                              "没有 —— 学生端登录不校验验证码, 所以能脚本化"))

    # ---- [1b] JS
    jst, jsb, jsh = get(op, js_url, referer=page_url)
    if jst == 200 and jsb:
        js = dec(jsb, jsh.get("Content-Type", ""))
        with open("login_bch.js", "w", encoding="utf-8", errors="replace") as f:
            f.write(js)
        say("\n[1b] %s  %s  %dB  已存 login_bch.js" % (js_url, jst, len(jsb)))
        dig_js(js)
    else:
        say("\n[1b] login_bch.js 取不到 (%s, %dB)" % (jst, len(jsb)))

    if a.js:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        say("\n[2] --js 模式, 不登录。已存 %s" % os.path.abspath(a.out))
        return 0

    # ---- [2] 登录
    user = a.user or input("\n学号 (直接回车放弃): ").strip()
    if not user:
        say("\n[2] 放弃登录。")
        with open(a.out, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        return 0
    pwd = a.pwd or input("密码 (不回显请用 --pass 传): ").strip()
    if not pwd:
        say("\n[2] 没密码, 放弃。")
        return 0

    data = dict(info["fields"])
    data["username"] = user
    data["usernameHidden"] = user
    data["pwd"] = pwd
    net, netnote = norm_net(a.net)
    data["net_access_type"] = net
    data["is_auto_land"] = data.get("is_auto_land", "0") or "0"

    act = info["action"] or "user.do?method=login_ajax"
    action_url = urllib.parse.urljoin(page_url, act)
    if qs:
        action_url += ("&" + qs)

    say("\n[2] POST %s" % action_url)
    say("    net_access_type = %s   username = %s%s" % (
        net, user, ("  (" + netnote + ")") if netnote else ""))
    if a.dry:
        say("    --dry: 只构造不发送。字段如下:")
        for k, v in data.items():
            say("      %-26s %s" % (k, (v[:40] if v != pwd else "********")))
        return 0

    last = None
    for i in range(max(1, a.loop)):
        if i:
            time.sleep(a.interval)
        st2, rb, rh = post_form(op, action_url, data, referer=page_url)
        text = dec(rb, rh.get("Content-Type", ""))
        tag, kw = classify(text)
        last = (st2, text, tag, kw)
        say("\n    #%d  %s  %dB  -> %s%s" % (
            i + 1, st2, len(rb), tag, ("  (命中: %s)" % kw) if kw else ""))
        if rh.get("Location"):
            say("        Location: %s" % rh["Location"])
        say("        原话: %s" % re.sub(r"\s+", " ", text)[:400])

    with open(a.out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    say("\n    已存 %s" % os.path.abspath(a.out))

    # ---- [3] 判读
    say("\n" + "=" * 74)
    if last:
        tag = last[2]
        table = {
            "SUCCESS": "登录成功。记住这个返回值 —— 断网窗口里再打一次, 拿它作对照。",
            "TIME_WINDOW": "时段封禁, 跟机制 B 一致。换 --net 中国移动/中国联通 再打一次:\n"
                           "  如果话术变成「运营商账号未绑定」之类, 说明时段只挂在校园网出口上,\n"
                           "  那就是突破口; 如果还是这句, 说明封的是账号不是出口。",
            "BAD_CRED": "账号或密码不对。三次就停, 共享 WAN 一锁全楼。",
            "OPERATOR_UNBOUND": "这个出口要运营商账号, 你的学号在这条路上没绑定。\n"
                                "  这是好消息: 三个出口是分开计费/分开策略的,\n"
                                "  时段封禁很可能只挂在「校园网」那一条上。",
            "BIND_LIMIT": "设备数/绑定限制。先注销旧会话再试。",
            "ARREARS": "套餐/欠费问题 —— 这类话术说明账号能查, 有时自助页能改套餐。",
            "CAPTCHA": "居然要验证码, 那就回到人工读码。",
            "WEBAUTH_NO_DEVICE": "服务器找不到该由哪台 AC 处理这个请求 —— 你没带 NAS 上下文。\n"
                                 "  这不是密码问题, 也不是时段问题。加 --redir http://10.100.0.235/\n"
                                 "  (或 --params \"wlanuserip=...&nasip=...&wlanacname=...\") 再打一次。",
            "UNKNOWN": "没命中已知话术。把「原话」那行发我, 它是新信息。",
        }
        say("  " + table.get(tag, table["UNKNOWN"]))
    say("=" * 74)
    return 0


if __name__ == "__main__":
    sys.exit(main())
