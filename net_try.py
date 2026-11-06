# -*- coding: utf-8 -*-
"""net_try -- three-exit comparison, browser-faithful chain.

The old version built the POST by hand with 5 fields and reused the first token it saw.
ePortal rejected that with "您采用了收藏的历史页面" -- which means the page token must come
from the *end* of the redirect chain, and every hidden field on the login page must go back.

This version walks the chain exactly like a browser:
    AC redirector  ->  index.jsp?tok  ->  login.jsp?tok2  (200)  ->  POST action from the page

    python net_try.py --user <学号> --pass ******
    python net_try.py --user ... --pass ... --only cnmobile
"""
import argparse
import os
import re
import subprocess
import sys
import time
import urllib.parse

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import stu_login as S

ALIAS = {
    "campus": "校园网", "school": "校园网", "0": "校园网",
    "cnmobile": "中国移动", "mobile": "中国移动", "cmcc": "中国移动", "1": "中国移动",
    "cnunicom": "中国联通", "unicom": "中国联通", "cu": "中国联通", "2": "中国联通",
}
SAFE = {"校园网": "campus", "中国移动": "cnmobile", "中国联通": "cnunicom"}


def alias(name):
    n = (name or "").strip()
    return ALIAS.get(n.lower(), n)


def tag(name):
    return SAFE.get(name, re.sub(r"[^0-9A-Za-z]", "_", name) or "net")


def ping(host, ms=1200):
    if os.name == "nt":
        cmd = "ping -n 1 -w %d %s" % (ms, host)
    else:
        cmd = "ping -c 1 -W %d %s" % (max(1, ms // 1000), host)
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, timeout=8).returncode == 0
    except Exception:
        return False


def squash(text, limit=260):
    t = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", text)
    t = re.sub(r"(?s)<[^>]+>", " ", t)
    t = re.sub(r"&nbsp;?", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t[:limit]


def alerts(text):
    out = []
    for pat in (r"""alert\(\s*"([^"]*)\"""", r"""alert\(\s*'([^']*)'""",
                r"""alert\(\s*`([^`]*)`"""):
        out += re.findall(pat, text)
    return [a.strip() for a in out if a.strip()]


def follow(op, url, max_hops=6):
    """手写跳转跟随（opener 带 NoRedir，urllib 不会自己跟）"""
    hops = []
    cur = url
    st, body, hdr = 0, b"", {}
    for _ in range(max_hops):
        st, body, hdr = S.get(op, cur)
        loc = hdr.get("Location") or hdr.get("location") or ""
        hops.append((cur, st, loc))
        if st in (301, 302, 303, 307) and loc:
            cur = urllib.parse.urljoin(cur, loc)
            continue
        break
    return cur, st, body, hdr, hops


def classify(text):
    if "历史页面" in text or "该页面已无效" in text:
        return "STALE_PAGE", "page token rejected"
    if "不在认证时段" in text:
        return "TIME_WINDOW", "不在认证时段"
    return S.classify(text)


def one_login(user, pwd, net, target, ac):
    base = "http://%s" % target if not target.startswith("http") else target
    base = base.rstrip("/")
    op, cj = S.make_opener()
    ac_url = "http://%s/" % ac

    final_url, st, body, hdr, hops = follow(op, ac_url)
    text = S.dec(body, hdr.get("Content-Type", ""))

    if not hops or hops[-1][1] == 0:
        return {"net": net, "verdict": "NO_NAS_CTX", "note": "AC unreachable",
                "status": 0, "size": 0, "raw": "", "body": b"", "alerts": [], "hops": hops}

    page = S.parse_page(text)
    if not page.get("action"):
        # 没拿到登录页：可能是重定向器只回了 location.href 的脚本
        m = re.search(r"""location\.href\s*=\s*['"]([^'"]+)['"]""", text)
        if m:
            nxt = m.group(1).replace("&amp;", "&")
            final_url, st, body, hdr, hops2 = follow(op, urllib.parse.urljoin(final_url, nxt))
            hops += hops2
            text = S.dec(body, hdr.get("Content-Type", ""))
            page = S.parse_page(text)

    if not page.get("action"):
        return {"net": net, "verdict": "NO_LOGIN_PAGE", "note": "chain ended without a form",
                "status": st, "size": len(body), "raw": text, "body": body,
                "alerts": alerts(text), "hops": hops}

    action = urllib.parse.urljoin(final_url, page["action"])
    fields = dict(page.get("fields") or {})

    # 三出口下拉里挑中文名对应的 value
    for k, opts in (page.get("selects") or {}).items():
        for val, txt in opts:
            if txt == net:
                fields[k] = val
                break
    fields["username"] = user
    fields["pwd"] = pwd
    if "net_access_type" in fields:
        fields["net_access_type"] = net
    fields.pop("validcode", None)

    st2, body2, hdr2 = S.post_form(op, action, fields, referer=final_url)
    text2 = S.dec(body2, hdr2.get("Content-Type", ""))
    verdict, why = classify(text2)

    # token 是一次性的：被拒就换一条新链再来一次
    if verdict == "STALE_PAGE":
        op2, _ = S.make_opener()
        final_url, st, body, hdr, hops2 = follow(op2, ac_url)
        hops += hops2
        text = S.dec(body, hdr.get("Content-Type", ""))
        page = S.parse_page(text)
        if page.get("action"):
            action = urllib.parse.urljoin(final_url, page["action"])
            f2 = dict(page.get("fields") or {})
            for k, opts in (page.get("selects") or {}).items():
                for val, txt in opts:
                    if txt == net:
                        f2[k] = val
                        break
            f2["username"] = user
            f2["pwd"] = pwd
            if "net_access_type" in f2:
                f2["net_access_type"] = net
            f2.pop("validcode", None)
            st2, body2, hdr2 = S.post_form(op2, action, f2, referer=final_url)
            text2 = S.dec(body2, hdr2.get("Content-Type", ""))
            verdict, why = classify(text2)
            fields = f2

    return {"net": net, "verdict": verdict, "note": why, "status": st2,
            "size": len(body2), "raw": text2, "body": body2,
            "alerts": alerts(text2), "hops": hops,
            "action": action, "fields": sorted(fields.keys())}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", required=True)
    ap.add_argument("--pass", dest="pwd", required=True)
    ap.add_argument("--target", default="172.172.255.20:80")
    ap.add_argument("--ac", default="10.100.0.235")
    ap.add_argument("--nets", default="校园网,中国移动,中国联通")
    ap.add_argument("--only", default="")
    ap.add_argument("--gap", type=float, default=6.0)
    ap.add_argument("--show-fields", action="store_true")
    a = ap.parse_args()

    nets = [a.only] if a.only else [x.strip() for x in re.split(r"[,+]", a.nets) if x.strip()]
    nets = [alias(x) for x in nets]
    lines = []

    def out(s):
        print(s)
        lines.append(s)

    out("=" * 74)
    out("  net_try  %s  %s" % (a.target, time.strftime("%Y-%m-%d %H:%M:%S")))
    out("  internet(223.5.5.5) = %s" % ("UP" if ping("223.5.5.5") else "DOWN"))
    out("=" * 74)

    results = []
    for i, net in enumerate(nets):
        r = one_login(a.user, a.pwd, net, a.target, a.ac)
        results.append(r)
        out("[%d] %-8s -> %-16s HTTP %s %dB  %s"
            % (i + 1, net, r["verdict"], r.get("status", 0), r.get("size", 0), r.get("note", "")))
        for u, s, loc in r.get("hops") or []:
            if loc:
                out("      hop> %s" % u[:96])
                out("           %s -> %s" % (s, loc[:110]))
            else:
                out("      hop> %s [%s]" % (u[:96], s))
        if r.get("action"):
            out("      post> %s" % r["action"][:140])
        if a.show_fields and r.get("fields"):
            out("      field> %s" % ", ".join(r["fields"]))
        for al in r.get("alerts") or []:
            out("      alert> %s" % al[:170])
        txt = squash(r.get("raw", "") or "")
        if txt:
            out("      text > %s" % txt)
        if r.get("body"):
            fn = "net-%s.html" % tag(net)
            try:
                open(fn, "wb").write(r["body"])
                out("      saved> %s" % fn)
            except Exception as e:
                out("      saved> failed: %s" % e)
        if i < len(nets) - 1:
            time.sleep(a.gap)

    out("-" * 74)
    vs = {r["net"]: r["verdict"] for r in results}
    campus = [v for k, v in vs.items() if "校园" in k]
    other = [v for k, v in vs.items() if "校园" not in k]
    allv = list(vs.values())

    if other and campus and any(v == "SUCCESS" for v in other) and all(v != "SUCCESS" for v in campus):
        out("结论：运营商出口能过、校园网出口被拒 —— 时段策略只挂在校园网服务上。")
        out("      换运营商出口就是合法路径；把路由器上网方式改掉即可。")
    elif allv and all(v == "TIME_WINDOW" for v in allv):
        out("结论：三个出口全部回「不在认证时段内」—— 封的是账号，不是出口。")
        out("      技术路线到此为止：唯一解是让上游给这个账号开时段例外。")
    elif "STALE_PAGE" in allv:
        out("结论：还是「历史页面」—— 说明 token 链还没走对。")
        out("      下一步：看 hop 行。浏览器里那条链的第一跳和这里的 AC 跳是否一致。")
    elif any(v == "SUCCESS" for v in allv):
        out("结论：至少一个出口登录成功 —— 这是你的基线。")
        out("      明晚 00:03 再跑同一条命令，对比即可定性。")
    elif any(v in ("BAD_CRED", "OPERATOR_UNBOUND") for v in allv):
        out("结论：被拒原因是账号/绑定，不是时段。")
    elif all(v == "NO_NAS_CTX" for v in allv):
        out("结论：AC 没给上下文。先在路由器后台点开一次门户页。")
    elif all(v == "UNKNOWN" for v in allv):
        out("结论：话术没见过，全部正文已存 net-*.html，抄 alert 那行就能定性。")
    else:
        out("结论：见上面逐条。")

    out("=" * 74)
    open("net-try.txt", "w", encoding="utf-8-sig").write("\n".join(lines))
    print("saved: net-try.txt")
    return 0


if __name__ == "__main__":
    sys.exit(main())
