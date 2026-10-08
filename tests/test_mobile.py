#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""スマホ接続（LAN 側の待ち受け）を、実際に立てて攻撃も混ぜて確かめる。

    python3 tests/test_mobile.py

LAN 側は同じネットワークの誰からでも届く。手前（127.0.0.1）の守りでは足りず、
別の守りが要る。ここで確かめるのは、次の約束。

  * 鍵（QR で渡す）を持たない端末には、画面も合言葉も一切渡さない
  * 合言葉の QR は1回しか使えず、5分で切れ、総当たりもできない
  * 別の名前（DNS リバインディング）や別のサイトからの要求は断る
  * 終了・スマホ設定のような管理の操作は、LAN 側からは存在しないも同然
  * 閉じたら待ち受けも、渡した鍵もすべて消える

待ち受けのアドレスは 127.0.0.1 に差し替える（家の IP に依存しないため）。
メールは出ない。
"""

import http.client
import json
import os
import re
import socket
import sys
import tempfile
import threading
import time
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gline  # noqa: E402
from gline import server, smtpsend, store  # noqa: E402

gline.use_utf8_output()

PASS, FAIL = [], []


def check(name, got, want):
    (PASS if got == want else FAIL).append(
        name if got == want else "%s\n    期待: %r\n    実際: %r" % (name, want, got))


def check_true(name, got):
    check(name, bool(got), True)


# ---------------------------------------------------------------- 純粋な判定

check("プライベート IP は許す", server.acceptable_ip("192.168.0.108"), True)
check("10.x も許す", server.acceptable_ip("10.0.0.5"), True)
check("172.16.x も許す", server.acceptable_ip("172.20.1.1"), True)
check("Tailscale（100.64/10）も許す", server.acceptable_ip("100.101.102.103"), True)
check("インターネットの IP は断る", server.acceptable_ip("8.8.8.8"), False)
check("ループバックは断る", server.acceptable_ip("127.0.0.1"), False)
check("0.0.0.0 は断る", server.acceptable_ip("0.0.0.0"), False)
check("IP でない値は断る", server.acceptable_ip("example.com"), False)

H, T = "192.168.0.108:8765", "tok"


def lan(path="/api/status", host=H, origin=None, token=T, paired=True):
    return server.check_lan_request(H, T, path, host, origin, token, paired)


check("鍵があり合言葉も合えば通す", lan(), None)
check("鍵が無ければ断る", lan(paired=False), "unpaired")
check("鍵が無くても /pair は入口として通す", lan(path="/pair", paired=False, token=None), None)
check("鍵が無くてもアイコンは通す",
      lan(path="/static/icon-192.png", paired=False, token=None), None)
check("鍵が無ければ画面は渡さない", lan(path="/", paired=False, token=None), "unpaired")
check("鍵が無ければ JS も渡さない", lan(path="/static/app.js", paired=False, token=None), "unpaired")
check("別の名前で来たら断る（リバインディング）", lan(host="evil.example.com:8765"), "host")
check("Host が無ければ断る", lan(host=None), "host")
check("ポートが違えば断る", lan(host="192.168.0.108:9999"), "host")
check("127.0.0.1 の名前で来ても断る", lan(host="127.0.0.1:8765"), "host")
check("別のサイトからは断る", lan(origin="https://evil.example.com"), "origin")
check("自分自身の Origin は通す", lan(origin="http://" + H), None)
check("API は合言葉が無ければ断る", lan(token=None), "token")
check("API は合言葉が違えば断る", lan(token="wrong"), "token")
check("画面は合言葉なしでも（鍵があれば）出す", lan(path="/", token=None), None)

# ---------------------------------------------------------------- 実際に立てる


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Account:
    email = "me@example.com"
    label = "テスト"


class _Cfg:
    me = {"me@example.com"}
    force_people, force_notices, mute = [], [], []
    accounts = [_Account()]
    primary = _Account()
    port = free_port()
    mobile_port = free_port()

    def __init__(self, db_path):
        self.db_path = db_path

    def is_me(self, a):
        return (a or "").lower() in self.me

    def account(self, e):
        return _Account() if e == "me@example.com" else None


tmp = tempfile.mkdtemp()
cfg = _Cfg(os.path.join(tmp, "t.db"))
db = store.connect(cfg.db_path, "me@example.com")
raw = zlib.compress(
    b"From: Alice <alice@example.com>\r\nTo: me@example.com\r\n"
    b"Subject: hello\r\nMessage-ID: <orig@example.com>\r\n\r\nhi\r\n")
db.execute(
    "INSERT INTO messages (account, uid, message_id, conv_key, ts, from_addr,"
    " to_json, subject, is_me, body, body_full, attachments, raw) VALUES"
    " ('me@example.com', 1, '<orig@example.com>', 'alice@example.com', 100,"
    " 'alice@example.com', '[\"me@example.com\"]', 'hello', 0, '秘密の本文', '秘密の本文', '[]', ?)",
    (raw,))
db.commit()
db.close()

sent = []
smtpsend.send = lambda a, p, m: sent.append(m["To"]) or m["Message-ID"]
server.config.app_password = lambda a: "dummy"
server.UNDO_SECONDS = 1

httpd = server.serve(cfg)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
mobile = httpd.gline_mobile
mobile.ip_func = lambda: "127.0.0.1"              # 家の IP ではなくループバックで試す
mobile.acceptable = lambda ip: True
clock = [1000.0]
mobile.clock = lambda: clock[0]
time.sleep(0.3)

LOCAL_PORT, LAN_PORT = cfg.port, cfg.mobile_port
LAN_HOST = "127.0.0.1:%d" % LAN_PORT


def call(port, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    hdrs = dict(headers or {})
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        hdrs.setdefault("Content-Type", "application/json")
    conn.request(method, path, data, hdrs)
    res = conn.getresponse()
    payload = res.read().decode("utf-8", "replace")
    out = (res.status, payload, dict(res.getheaders()))
    conn.close()
    return out


# 手前（パソコンの中）の合言葉
_, page, _ = call(LOCAL_PORT, "GET", "/", headers={"Host": "127.0.0.1:%d" % LOCAL_PORT})
token = re.search(r'name="fukidashi-token" content="([^"]+)"', page).group(1)
LOCAL = {"Host": "127.0.0.1:%d" % LOCAL_PORT, server.TOKEN_HEADER: token}


def local(method, path, body=None):
    code, text, hdrs = call(LOCAL_PORT, method, path, body, LOCAL)
    try:
        return code, json.loads(text)
    except ValueError:
        return code, text


# ---------------------------------------------------------------- 初期状態

check("起動直後は LAN 側を開いていない", mobile.enabled, False)
try:
    socket.create_connection(("127.0.0.1", LAN_PORT), timeout=1).close()
    check("起動直後は LAN 側に繋がらない", "繋がった", "繋がらない")
except OSError:
    check("起動直後は LAN 側に繋がらない", "繋がらない", "繋がらない")

code, st = local("GET", "/api/status")
check("手前の画面は local と名乗る", st.get("local"), True)
check("手前から状態を見られる", local("GET", "/api/mobile")[1].get("enabled"), False)

code, res = local("POST", "/api/mobile/pair", {})
check("有効にする前は QR を作れない", code, 400)

# ---------------------------------------------------------------- 有効にする

code, res = local("POST", "/api/mobile/enable", {})
check("有効にできる", (code, res.get("enabled")), (200, True))
check("待ち受けのアドレスを返す", res.get("host"), LAN_HOST)

code, pair = local("POST", "/api/mobile/pair", {})
check("QR 用の URL を作る", code, 200)
url = pair["url"]
check_true("URL に待ち受けのアドレスが入る", url.startswith("http://%s/pair?code=" % LAN_HOST))
check("期限は5分", pair["expires_in"], 300)
check_true("QR の SVG が付く", pair["svg"].startswith("<svg"))
code1 = url.split("code=")[1]

LANH = {"Host": LAN_HOST}


def lan_call(method, path, body=None, headers=None, cookie=None):
    h = dict(LANH)
    h.update(headers or {})
    if cookie:
        h["Cookie"] = "%s=%s" % (server.SESSION_COOKIE, cookie)
    return call(LAN_PORT, method, path, body, h)


# ---------------------------------------------------------------- 鍵が無い端末

code, body, _ = lan_call("GET", "/")
check("鍵が無ければ画面を出さない", code, 401)
check("鍵が無ければ合言葉も渡さない", token in body, False)
check("鍵が無ければメールの中身も渡さない", "秘密の本文" in body, False)
check_true("やり方を案内する", "QR" in body)

code, body, _ = lan_call("GET", "/api/conversations")
check("鍵が無ければ API は 401", code, 401)
check("メールの一覧を渡さない", "alice" in body, False)

code, body, _ = lan_call("GET", "/static/app.js")
check("鍵が無ければ JS も渡さない", code, 401)

code, body, _ = lan_call("GET", "/static/icon-192.png")
check("アイコンは鍵が無くても出せる（無ければ 404 でよい）", code in (200, 404), True)

code, body, _ = lan_call("POST", "/api/send", {"key": "alice@example.com", "text": "x"},
                         headers={"Origin": "http://" + LAN_HOST})
check("鍵が無ければ送信も断る", code, 401)
check("実際に送られていない", len(sent), 0)

# ---------------------------------------------------------------- 引き換え

code, body, hdrs = lan_call("GET", "/pair?code=wrong")
check("間違った合言葉は断る", code, 403)
check("鍵（Cookie）は渡さない", "Set-Cookie" in hdrs, False)

code, body, hdrs = lan_call("GET", "/pair?code=" + code1)
check("正しい合言葉は受け付ける（転送）", code, 302)
check("画面へ転送する", hdrs.get("Location"), "/")
cookie_line = hdrs.get("Set-Cookie", "")
check_true("鍵を渡す", cookie_line.startswith(server.SESSION_COOKIE + "="))
check_true("鍵は JavaScript から読めない（HttpOnly）", "HttpOnly" in cookie_line)
# Strict にすると、カメラやメッセージのリンクから開いたとき、ペアリング直後の転送先に
# 鍵が付かず「まだ接続されていません」になる（実機で起きた）。Lax なら付く。
check_true("鍵は SameSite=Lax（Strict にしない）", "SameSite=Lax" in cookie_line)
check("Strict は使わない（リンクから開いた最初の1回に鍵が付かなくなる）",
      "SameSite=Strict" in cookie_line, False)
sid = re.match(server.SESSION_COOKIE + r"=([^;]+)", cookie_line).group(1)
check_true("鍵は十分に長い", len(sid) >= 40)

code, body, _ = lan_call("GET", "/pair?code=" + code1)
check("同じ合言葉は2回使えない", code, 403)

# ---------------------------------------------------------------- 鍵を持つ端末

code, body, _ = lan_call("GET", "/", cookie=sid)
check("鍵があれば画面を出す", code, 200)
check_true("画面に合言葉が入る", token in body)

code, body, _ = lan_call("GET", "/api/status", cookie=sid)
check("鍵だけで合言葉が無ければ API は断る", code, 403)

LANT = {server.TOKEN_HEADER: token}
code, body, _ = lan_call("GET", "/api/status", headers=LANT, cookie=sid)
check("鍵と合言葉があれば API を使える", code, 200)
st = json.loads(body)
check("LAN 側の画面は local ではない", st.get("local"), False)

code, body, _ = lan_call("GET", "/api/conversations", headers=LANT, cookie=sid)
check("会話の一覧を見られる", code, 200)

code, body, _ = lan_call("GET", "/static/app.js", cookie=sid)
check("鍵があれば JS も出す", code, 200)

# スマホからの返信
code, body, _ = lan_call("POST", "/api/send", {"key": "alice@example.com", "text": "スマホから返信"},
                         headers=dict(LANT, Origin="http://" + LAN_HOST), cookie=sid)
check("スマホから返信を受け付ける", code, 200)
time.sleep(1.8)
check("猶予のあと送られる", len(sent), 1)

# ---------------------------------------------------------------- 攻撃

code, body, _ = lan_call("GET", "/api/status", headers=dict(LANT, Host="evil.example.com:%d" % LAN_PORT),
                         cookie=sid)
# lan_call は Host を LANH で上書きするので、直接組み立てる
code, body, _ = call(LAN_PORT, "GET", "/api/status",
                     headers={"Host": "evil.example.com:%d" % LAN_PORT, "Cookie": "%s=%s" % (server.SESSION_COOKIE, sid), **LANT})
check("別の名前で来たら、鍵があっても断る（DNS リバインディング）", code, 403)

code, body, _ = call(LAN_PORT, "GET", "/api/status",
                     headers={"Host": "127.0.0.1:%d" % LOCAL_PORT, "Cookie": "%s=%s" % (server.SESSION_COOKIE, sid), **LANT})
check("手前のアドレスを名乗っても断る", code, 403)

code, body, _ = lan_call("POST", "/api/sync", {}, headers=dict(LANT, Origin="https://evil.example.com"),
                         cookie=sid)
check("別のサイトからの POST は断る", code, 403)

# 管理の操作は、LAN 側には経路が無い
code, body, _ = lan_call("POST", "/api/quit", {}, headers=dict(LANT, Origin="http://" + LAN_HOST), cookie=sid)
check("スマホから終了させられない", code, 404)
check("手前のサーバは生きている", local("GET", "/api/status")[0], 200)

for path in ("/api/mobile/enable", "/api/mobile/disable", "/api/mobile/pair"):
    code, body, _ = lan_call("POST", path, {}, headers=dict(LANT, Origin="http://" + LAN_HOST), cookie=sid)
    check("スマホから %s は使えない" % path, code, 404)
code, body, _ = lan_call("GET", "/api/mobile", headers=LANT, cookie=sid)
check("スマホからスマホ設定を見られない", code, 404)
check("操作は効いていない（まだ有効）", mobile.enabled, True)

# ---------------------------------------------------------------- 総当たり

code, pair2 = local("POST", "/api/mobile/pair", {})
good = pair2["url"].split("code=")[1]
for i in range(server.Mobile.MAX_FAILS):
    lan_call("GET", "/pair?code=guess%d" % i)
code, body, _ = lan_call("GET", "/pair?code=" + good)
check("間違いが続いたら、正しい合言葉も使えなくなる", code, 403)

code, pair3 = local("POST", "/api/mobile/pair", {})
good3 = pair3["url"].split("code=")[1]
code, body, hdrs = lan_call("GET", "/pair?code=" + good3)
check("パソコンで出し直せば、また使える", code, 302)
check("出し直すと前の合言葉は無効", lan_call("GET", "/pair?code=" + good)[0], 403)

# ---------------------------------------------------------------- 期限

code, pair4 = local("POST", "/api/mobile/pair", {})
exp_code = pair4["url"].split("code=")[1]
clock[0] += server.Mobile.CODE_SECONDS + 1
check("合言葉は5分で切れる", lan_call("GET", "/pair?code=" + exp_code)[0], 403)

code, pair5 = local("POST", "/api/mobile/pair", {})
code, body, hdrs = lan_call("GET", "/pair?code=" + pair5["url"].split("code=")[1])
sid2 = re.match(server.SESSION_COOKIE + r"=([^;]+)", hdrs["Set-Cookie"]).group(1)
check("新しい鍵は使える", lan_call("GET", "/", cookie=sid2)[0], 200)
clock[0] += server.Mobile.SESSION_SECONDS + 1
check("鍵にも期限がある", lan_call("GET", "/", cookie=sid2)[0], 401)

# ---------------------------------------------------------------- 接続数

code, pair6 = local("POST", "/api/mobile/pair", {})
code, body, hdrs = lan_call("GET", "/pair?code=" + pair6["url"].split("code=")[1])
sid3 = re.match(server.SESSION_COOKIE + r"=([^;]+)", hdrs["Set-Cookie"]).group(1)
check("接続中の端末数が数えられる", local("GET", "/api/mobile")[1].get("devices"), 1)

# ---------------------------------------------------------------- 閉じる

code, res = local("POST", "/api/mobile/disable", {})
check("無効にできる", (code, res.get("enabled")), (200, False))
try:
    socket.create_connection(("127.0.0.1", LAN_PORT), timeout=1).close()
    check("閉じたら LAN 側に繋がらない", "繋がった", "繋がらない")
except OSError:
    check("閉じたら LAN 側に繋がらない", "繋がらない", "繋がらない")
check("手前のサーバは影響を受けない", local("GET", "/api/status")[0], 200)

local("POST", "/api/mobile/enable", {})
check("開き直しても、前の鍵は使えない", lan_call("GET", "/", cookie=sid3)[0], 401)

# 家庭の外から見えるアドレスでは開かない
local("POST", "/api/mobile/disable", {})
mobile.acceptable = server.acceptable_ip
mobile.ip_func = lambda: "8.8.8.8"
code, res = local("POST", "/api/mobile/enable", {})
check("インターネットから見えるアドレスでは開かない", code, 400)
check_true("理由を伝える", "インターネット" in res.get("error", ""))
check("開いていない", mobile.enabled, False)

mobile.ip_func = lambda: None
code, res = local("POST", "/api/mobile/enable", {})
check("ネットワークが無ければ開かない", code, 400)

# 終了の経路で LAN 側も必ず閉じる
mobile.acceptable = lambda ip: True
mobile.ip_func = lambda: "127.0.0.1"
local("POST", "/api/mobile/enable", {})
check("もう一度開いた", mobile.enabled, True)
httpd.shutdown()
server.shutdown(httpd)
check("アプリの終了で LAN 側も閉じる", mobile.enabled, False)

# ---------------------------------------------------------------- 決め打ちのアドレス（Tailscale など）

# 外出先から Tailscale 経由で見るときは、設定の mobile_ip に Tailscale のアドレス
# （100.x.x.x）を書く。空なら、いまの Wi-Fi のアドレスを自動で選ぶ。
from gline import config as _config  # noqa: E402


def _load(mobile_ip):
    path = os.path.join(tmp, "cfg_%s.json" % abs(hash(mobile_ip)))
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"accounts": [{"email": "me@example.com"}], "mobile_ip": mobile_ip}, fh)
    return _config.load(path)


check("mobile_ip は既定で空（自動）", _load("").mobile_ip, "")
check("Tailscale のアドレスを書ける", _load("100.101.102.103").mobile_ip, "100.101.102.103")
check("前後の空白は除く", _load("  192.168.0.5 ").mobile_ip, "192.168.0.5")
for bad, why in (("abc", "文字列"), ("999.1.1.1", "範囲外"), ("::1", "IPv6"), ("1.2.3", "不完全")):
    try:
        _load(bad)
        check("mobile_ip の不正な値を断る（%s）" % why, "通った", "断る")
    except _config.ConfigError:
        check("mobile_ip の不正な値を断る（%s）" % why, "断る", "断る")


def jcall(port, method, path, body=None, headers=None):
    """call の結果の本文を JSON として読む（読めなければそのまま）。"""
    code, text, _ = call(port, method, path, body, headers)
    try:
        return code, json.loads(text)
    except ValueError:
        return code, text


class _Cfg2(_Cfg):
    port = free_port()
    mobile_port = free_port()
    mobile_ip = "127.0.0.1"


cfg2 = _Cfg2(cfg.db_path)
httpd2 = server.serve(cfg2)
threading.Thread(target=httpd2.serve_forever, daemon=True).start()
mobile2 = httpd2.gline_mobile
mobile2.acceptable = lambda ip: True          # 試験ではループバックで受ける
time.sleep(0.3)

check("設定したアドレスを、そのまま使う", mobile2.ip_func(), "127.0.0.1")
check("決め打ちであることを覚えている", mobile2.fixed_ip, "127.0.0.1")
_, page2, _ = call(cfg2.port, "GET", "/", headers={"Host": "127.0.0.1:%d" % cfg2.port})
token2 = re.search(r'name="fukidashi-token" content="([^"]+)"', page2).group(1)
L2 = {"Host": "127.0.0.1:%d" % cfg2.port, server.TOKEN_HEADER: token2}

code, st = jcall(cfg2.port, "GET", "/api/mobile", headers=L2)
check("開く前から、決め打ちのアドレスが分かる（画面の文言を切り替えるため）",
      (code, st.get("configured_ip"), st.get("enabled")), (200, "127.0.0.1", False))
code, st = jcall(cfg2.port, "POST", "/api/mobile/enable", {}, L2)
check("設定したアドレスで開ける", (code, st.get("host")), (200, "127.0.0.1:%d" % cfg2.mobile_port))
check("決め打ちなら、アドレスが変わったとは見なさない", st.get("stale"), False)
code, st = jcall(cfg2.port, "GET", "/api/mobile", headers=L2)
check("開いたあとも、決め打ちのアドレスが分かる", st.get("configured_ip"), "127.0.0.1")
call(cfg2.port, "POST", "/api/mobile/disable", {}, L2)

# このパソコンに付いていないアドレスを書いたとき（Tailscale が止まっているなど）
mobile2.ip_func = lambda: "10.20.30.40"
mobile2.fixed_ip = "10.20.30.40"
mobile2.acceptable = server.acceptable_ip
code, st = jcall(cfg2.port, "POST", "/api/mobile/enable", {}, L2)
check("付いていないアドレスでは開かない", code, 400)
check_true("mobile_ip と Tailscale を案内する",
           "mobile_ip" in st.get("error", "") and "Tailscale" in st.get("error", ""))
check("開いていない", mobile2.enabled, False)

# 自動のとき（決め打ちでない）は、案内が混ざらない
mobile2.fixed_ip = ""
code, st = jcall(cfg2.port, "POST", "/api/mobile/enable", {}, L2)
check("自動のときは、mobile_ip の案内を出さない（設定していないのに紛らわしい）",
      "mobile_ip" in st.get("error", ""), False)

httpd2.shutdown()
server.shutdown(httpd2)

print("%d 件成功 / %d 件失敗" % (len(PASS), len(FAIL)))
if FAIL:
    print()
    for f in FAIL:
        print("  ✗ " + f)
    sys.exit(1)
print("すべて通りました。")
