#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""サーバを実際に立てて、画面が使う経路をひととおり叩く。

    python3 tests/test_http.py

部品ごとの試験だけでは、経路の繋ぎ忘れを見逃す。実際に
POST /api/send の登録漏れをこれで取りこぼした。SMTP は差し替えるので
メールは出ない。
"""

import json
import os
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gline  # noqa: E402
from gline import server, smtpsend, store  # noqa: E402

gline.use_utf8_output()

PASS, FAIL = [], []


def check(name, got, want):
    (PASS if got == want else FAIL).append(
        name if got == want else "%s\n    期待: %r\n    実際: %r" % (name, want, got))


# ---------------------------------------------------------------- 準備

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
    " 'alice@example.com', '[\"me@example.com\"]', 'hello', 0, 'hi', 'hi', '[]', ?)",
    (raw,))
db.commit()
db.close()

sent = []
smtpsend.send = lambda a, p, m: sent.append(m["To"]) or m["Message-ID"]
server.config.app_password = lambda a: "dummy"
server.UNDO_SECONDS = 1

httpd = server.serve(cfg)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
time.sleep(0.4)

BASE = "http://127.0.0.1:%d" % cfg.port


def fetch(path, data=None, token=None, origin=None, host=None):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(data).encode() if data is not None else None,
        method="POST" if data is not None else "GET")
    if token:
        req.add_header(server.TOKEN_HEADER, token)
    if origin:
        req.add_header("Origin", origin)
    if host:
        req.add_header("Host", host)
    try:
        with urllib.request.urlopen(req, timeout=5) as res:
            body = res.read().decode("utf-8")
            return res.status, (json.loads(body) if body.startswith(("{", "[")) else body)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


# 画面から合言葉を取り出す（実際の利用と同じ経路）
status, page = fetch("/")
check("トップページが出る", status, 200)
token = page.split('name="fukidashi-token" content="')[1].split('"')[0]
check("合言葉が埋め込まれている", len(token) > 20, True)

# ---------------------------------------------------------------- 読む経路

check("状態", fetch("/api/status", token=token)[0], 200)
check("会話一覧", fetch("/api/conversations", token=token)[0], 200)
check("メッセージ", fetch("/api/messages?key=alice@example.com", token=token)[0], 200)

code, ctx = fetch("/api/reply_context?key=alice@example.com", token=token)
check("返信の下調べ", code, 200)
check("宛先が決まる", ctx.get("to"), ["alice@example.com"])

# ---------------------------------------------------------------- 守り

check("合言葉なしは断る", fetch("/api/status")[0], 403)
check("よそからの POST は断る",
      fetch("/api/sync", data={}, token=token, origin="https://evil.example.com")[0], 403)

# ---------------------------------------------------------------- 送る経路

code, res = fetch("/api/send", data={"key": "alice@example.com", "text": "了解しました"},
                  token=token)
check("送信を受け付ける", code, 200)
check("宛先を返す", res.get("to"), ["alice@example.com"])
ident = res.get("id")

code, st = fetch("/api/send/state?id=" + ident, token=token)
check("状態を見られる", st.get("state"), "waiting")

code, res2 = fetch("/api/send/cancel", data={"id": ident}, token=token)
check("取り消せる", res2.get("cancelled"), True)
time.sleep(1.6)
check("取り消したら送られない", len(sent), 0)

# 取り消さなければ送られる
code, res3 = fetch("/api/send", data={"key": "alice@example.com", "text": "送ります"},
                   token=token)
check("2通目も受け付ける", code, 200)
time.sleep(1.8)
check("猶予後に送られる", len(sent), 1)

# 異常系
check("会話の指定なしは断る", fetch("/api/send", data={"text": "x"}, token=token)[0], 400)
check("空の本文は断る",
      fetch("/api/send", data={"key": "alice@example.com", "text": " "}, token=token)[0], 400)
check("知らない会話は断る",
      fetch("/api/send", data={"key": "nobody@example.com", "text": "x"}, token=token)[0], 400)
check("知らない経路は 404", fetch("/api/nope", data={}, token=token)[0], 404)

httpd.shutdown()
server.shutdown(httpd)

print("%d 件成功 / %d 件失敗" % (len(PASS), len(FAIL)))
if FAIL:
    print()
    for f in FAIL:
        print("  ✗ " + f)
    sys.exit(1)
print("すべて通りました。")
