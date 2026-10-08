#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""スマホ接続の「常時オン」（mobile_always_on）のテスト。

    python3 tests/test_always_on.py

外出先から Tailscale 経由で読むときに、毎回 QR を読み直さずに済むようにする機能。
その代わり、次の約束を守る。

  * 設定で明示したときだけ働く。mobile_ip（Tailscale のアドレス）も必須
  * 端末は、鍵（Cookie）の中身ではなく、そのハッシュだけを保存する
  * 記憶は90日で切れる。1台ずつ、またはすべて切れる（スマホをなくしたとき用）
  * 切るなどの管理は、パソコンの中からだけ（スマホ側には経路が無い）
  * 「無効にする」は一時停止になり、勝手に開き直さない
  * 開けない間（Tailscale がまだ上がっていない等）は、静かに再試行し続ける

待ち受けのアドレスは 127.0.0.1 に差し替える。メールは出ない。
"""

import hashlib
import http.client
import json
import os
import re
import socket
import sqlite3
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gline  # noqa: E402
from gline import config, server, store  # noqa: E402

gline.use_utf8_output()

PASS, FAIL = [], []


def check(name, got, want):
    (PASS if got == want else FAIL).append(
        name if got == want else "%s\n    期待: %r\n    実際: %r" % (name, want, got))


def check_true(name, got):
    check(name, bool(got), True)


def wait_for(fn, seconds=4.0):
    end = time.time() + seconds
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.05)
    return bool(fn())


# ---------------------------------------------------------------- 設定の検査

tmp = tempfile.mkdtemp()


def load(**extra):
    path = os.path.join(tmp, "c_%d.json" % abs(hash(json.dumps(extra, sort_keys=True))))
    data = {"accounts": [{"email": "me@example.com"}]}
    data.update(extra)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    return config.load(path)


check("常時オンは既定でオフ", load().mobile_always_on, False)
check("mobile_ip があれば常時オンにできる",
      load(mobile_ip="100.111.186.80", mobile_always_on=True).mobile_always_on, True)
try:
    load(mobile_always_on=True)
    check("mobile_ip なしの常時オンは断る", "通った", "断る")
except config.ConfigError as e:
    check("mobile_ip なしの常時オンは断る", "断る", "断る")
    check_true("理由に mobile_ip と Tailscale が出る", "mobile_ip" in str(e) and "Tailscale" in str(e))

check("端末の種類: iPhone", server.device_label("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0)"), "iPhone")
check("端末の種類: Android", server.device_label("Mozilla/5.0 (Linux; Android 14; SM-N985F)"), "Android")
check("端末の種類: Mac", server.device_label("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15)"), "Mac")
check("端末の種類: 分からなければ「ブラウザ」", server.device_label("curl/8.0"), "ブラウザ")
check("端末の種類: UA が無くても落ちない", server.device_label(None), "ブラウザ")

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
    ai_model = ""
    mobile_ip = "127.0.0.1"
    mobile_always_on = True
    port = free_port()
    mobile_port = free_port()

    def __init__(self, db_path):
        self.db_path = db_path

    def is_me(self, a):
        return (a or "").lower() in self.me

    def account(self, e):
        return _Account() if e == "me@example.com" else None


cfg = _Cfg(os.path.join(tmp, "t.db"))
db = store.connect(cfg.db_path, "me@example.com")
db.execute(
    "INSERT INTO messages (account, uid, message_id, conv_key, ts, from_addr, from_name,"
    " to_json, subject, is_me, body, body_full, attachments) VALUES"
    " ('me@example.com', 1, '<a@x>', 'alice@example.com', 100, 'alice@example.com', '',"
    " '[]', 'hi', 0, '秘密の本文', '秘密の本文', '[]')")
db.commit()
db.close()

server.Mobile.RETRY_SECONDS = 0.2                  # 試験のために短く
LOCAL, LAN = cfg.port, cfg.mobile_port
LAN_HOST = "127.0.0.1:%d" % LAN
clock = [1000.0]


def call(port, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    data = json.dumps(body).encode() if body is not None else None
    hdrs = dict(headers or {})
    if data is not None:
        hdrs["Content-Type"] = "application/json"
    conn.request(method, path, data, hdrs)
    res = conn.getresponse()
    text = res.read().decode("utf-8", "replace")
    conn.close()
    try:
        return res.status, json.loads(text), dict(res.getheaders())
    except ValueError:
        return res.status, text, dict(res.getheaders())


def start():
    httpd = server.serve(cfg)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    m = httpd.gline_mobile
    m.clock = lambda: clock[0]
    time.sleep(0.15)
    _, page, _ = call(LOCAL, "GET", "/", headers={"Host": "127.0.0.1:%d" % LOCAL})
    token = re.search(r'name="fukidashi-token" content="([^"]+)"', page).group(1)
    return httpd, m, token


def local(token, method, path, body=None):
    code, res, _ = call(LOCAL, method, path, body,
                        {"Host": "127.0.0.1:%d" % LOCAL, server.TOKEN_HEADER: token})
    return code, res


def pair(token, ua):
    """QR の URL を作り、スマホとして引き換えて、鍵を返す。"""
    code, p = local(token, "POST", "/api/mobile/pair", {})
    assert code == 200, p
    _, _, hdrs = call(LAN, "GET", "/pair?code=" + p["url"].split("code=")[1],
                      headers={"Host": LAN_HOST, "User-Agent": ua})
    return re.match(server.SESSION_COOKIE + r"=([^;]+)", hdrs["Set-Cookie"]).group(1), hdrs["Set-Cookie"]


def phone(sid, path="/", token=None, method="GET", body=None):
    h = {"Host": LAN_HOST, "Cookie": "%s=%s" % (server.SESSION_COOKIE, sid),
         "Origin": "http://" + LAN_HOST}
    if token:
        h[server.TOKEN_HEADER] = token
    return call(LAN, method, path, body, h)


httpd, mobile, token = start()

# ---------------------------------------------------------------- 自動で開く・開けない間は再試行

time.sleep(0.5)                                     # 既定では 127.0.0.1 は断られる
check("開けない間は、開いていない", mobile.enabled, False)
check_true("開けない理由を覚えている（画面に出す）", mobile.last_error and "インターネット" in mobile.last_error)
mobile.acceptable = lambda ip: True                 # Tailscale が上がった、に相当
check("開けるようになったら、自動で開く", wait_for(lambda: mobile.enabled), True)
code, st = local(token, "GET", "/api/mobile")
check("常時オンと分かる", (st["always_on"], st["remember"]), (True, True))
check("ブックマーク用のアドレス", st["base_url"], "http://%s/" % LAN_HOST)
check("開いたら、エラーは消える", st["last_error"], None)

# ---------------------------------------------------------------- ペアリングと記憶

sid, cookie_line = pair(token, "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)")
check("鍵の有効期間は90日", re.search(r"Max-Age=(\d+)", cookie_line).group(1),
      str(90 * 24 * 3600))
check_true("鍵は HttpOnly", "HttpOnly" in cookie_line)

code, st = local(token, "GET", "/api/mobile")
check("端末が1台", st["devices"], 1)
check("端末の種類が一覧に出る", st["devices_list"][0]["label"], "iPhone")
check("一覧に出る id は短い（鍵そのものではない）", len(st["devices_list"][0]["id"]), 10)

code, _, _ = phone(sid)
check("ペアリングした端末は、画面を開ける", code, 200)

# 保存されているのは、鍵のハッシュだけ
raw = sqlite3.connect(cfg.db_path)
rows = raw.execute("SELECT sid_hash, label FROM paired_devices").fetchall()
raw.close()
check("1件だけ保存されている", len(rows), 1)
check("保存されているのは鍵のハッシュ", rows[0][0], hashlib.sha256(sid.encode()).hexdigest())
check("鍵そのものは、どこにも保存されていない", sid in json.dumps(rows), False)

# ---------------------------------------------------------------- 一時停止

code, st = local(token, "POST", "/api/mobile/disable", {})
check("無効にすると止まる", (code, st["enabled"], st["paused"]), (200, False, True))
time.sleep(0.8)
check("一時停止中は、勝手に開き直さない", mobile.enabled, False)
try:
    socket.create_connection(("127.0.0.1", LAN), timeout=1).close()
    check("止まっている間は繋がらない", "繋がった", "繋がらない")
except OSError:
    check("止まっている間は繋がらない", "繋がらない", "繋がらない")
check("止めても、記憶した端末は残る", local(token, "GET", "/api/mobile")[1]["devices"], 1)

code, st = local(token, "POST", "/api/mobile/enable", {})
check("再開できる", (code, st["enabled"], st["paused"]), (200, True, False))
code, _, _ = phone(sid)
check("再開しても、同じ鍵のまま入れる（QR を読み直さない）", code, 200)

# ---------------------------------------------------------------- 再起動をまたぐ

httpd.shutdown()
server.shutdown(httpd)
check("終了したら、再試行のスレッドも止まる", mobile._thread is None or not mobile._thread.is_alive(), True)
check("終了したら、待ち受けも閉じる", mobile.enabled, False)

httpd, mobile, token2 = start()
mobile.acceptable = lambda ip: True
check("再起動しても、自動で開く", wait_for(lambda: mobile.enabled), True)
code, body, _ = phone(sid)
check("再起動しても、前の鍵で入れる（ブックマークがそのまま使える）", code, 200)
check("新しい合言葉が画面に入る（再起動で変わる）", token2 in body and token not in body, True)
check("記憶した端末は残っている", local(token2, "GET", "/api/mobile")[1]["devices"], 1)

# ---------------------------------------------------------------- 90日で切れる

clock[0] += 90 * 24 * 3600 + 5
code, _, _ = phone(sid)
check("90日を過ぎたら、鍵は使えない", code, 401)
check("期限切れの端末は、一覧から消える", local(token2, "GET", "/api/mobile")[1]["devices"], 0)
clock[0] -= 90 * 24 * 3600 + 5

# 記録の掃除は、60秒に1回に間引かれる。掃除が走らない間でも、期限切れの鍵は通さない
base = clock[0]
sidT, _ = pair(token2, "Mozilla/5.0 (Linux; Android 14)")
expires = base + 90 * 24 * 3600
clock[0] = expires - 10
check("期限の直前は、まだ入れる（この呼び出しで掃除が走る）", phone(sidT)[0], 200)
clock[0] = expires + 5                      # 直前の掃除から15秒 → 次の掃除は間引かれる
check("掃除が間引かれている間でも、期限切れの鍵は通さない", phone(sidT)[0], 401)
clock[0] = base
local(token2, "POST", "/api/mobile/revoke_all", {})

# ---------------------------------------------------------------- 1台ずつ切る・すべて切る

sidA, _ = pair(token2, "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0)")
sidB, _ = pair(token2, "Mozilla/5.0 (Linux; Android 14)")
st = local(token2, "GET", "/api/mobile")[1]
labels = sorted(d["label"] for d in st["devices_list"])
# 期限切れの端末は、さっき記録からも消えている（時計を戻しても復活しない）
check("2台の種類が分かる", labels, ["Android", "iPhone"])
ids = {d["label"]: d["id"] for d in st["devices_list"] if d["label"] == "Android"}

code, res = local(token2, "POST", "/api/mobile/revoke", {"id": ids["Android"]})
check("1台だけ切れる", (code, res["revoked"]), (200, True))
check("切った端末は、すぐ入れなくなる", phone(sidB)[0], 401)
check("ほかの端末は、そのまま入れる", phone(sidA)[0], 200)

code, res = local(token2, "POST", "/api/mobile/revoke", {"id": "0123456789"})
check("存在しない id は何もしない", (code, res["revoked"]), (200, False))
code, res = local(token2, "POST", "/api/mobile/revoke", {"id": "x'; DROP TABLE paired_devices;--"})
check("不正な id は断る（SQL を通さない）", (code, res["revoked"]), (200, False))
check("表は無事", len(local(token2, "GET", "/api/mobile")[1]["devices_list"]) >= 1, True)

code, _, _ = phone(sidA, "/api/mobile/revoke_all", token2, "POST", {})
check("スマホから、端末を切る操作はできない", code, 404)
code, _, _ = phone(sidA, "/api/mobile/revoke", token2, "POST", {"id": "0123456789"})
check("スマホから、1台切る操作もできない", code, 404)
code, _, _ = phone(sidA, "/api/mobile", token2)
check("スマホから、端末の一覧は見られない", code, 404)

code, res = local(token2, "POST", "/api/mobile/revoke_all", {})
check("すべて切れる", (code, res["devices"]), (200, 0))
check("すべて切ったら、鍵はどれも使えない", (phone(sidA)[0], phone(sid)[0]), (401, 401))
check("すべて切っても、待ち受けは開いたまま", mobile.enabled, True)

# ---------------------------------------------------------------- 後始末

httpd.shutdown()
server.shutdown(httpd)
check("最後に、待ち受けも閉じる", mobile.enabled, False)

print("%d 件成功 / %d 件失敗" % (len(PASS), len(FAIL)))
if FAIL:
    print()
    for f in FAIL:
        print("  ✗ " + f)
    sys.exit(1)
print("すべて通りました。")
