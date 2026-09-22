#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""返信まわりのテスト。SMTP は差し替えるので、実際にメールは出ない。

    python3 tests/test_send.py

送信は取り消せない操作なので、宛先・差出人・取り消し猶予の三点を
念入りに確かめる。
"""

import email
import os
import sqlite3
import sys
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


# ---------------------------------------------------------------- 件名

check("件名に Re: を付ける", smtpsend._subject_for_reply("打ち合わせ"), "Re: 打ち合わせ")
check("Re: を重ねない", smtpsend._subject_for_reply("Re: 打ち合わせ"), "Re: 打ち合わせ")
check("大文字や入れ子も畳む", smtpsend._subject_for_reply("RE: Re: 件名"), "Re: 件名")
check("件名が無くても壊れない", smtpsend._subject_for_reply(""), "Re:")


# ---------------------------------------------------------------- 会話の用意

class _Account:
    email = "me@example.com"
    label = "テスト"


class _Cfg:
    me = {"me@example.com"}
    force_people, force_notices, mute = [], [], []
    accounts = [_Account()]
    primary = _Account()

    def is_me(self, a):
        return (a or "").lower() in self.me

    def account(self, e):
        return _Account() if e == "me@example.com" else None


cfg = _Cfg()
db = sqlite3.connect(":memory:")
db.row_factory = sqlite3.Row
db.executescript(store.SCHEMA_TABLES)

raw = zlib.compress(
    b"From: Alice <alice@example.com>\r\n"
    b"To: me@example.com\r\n"
    b"Subject: hello\r\n"
    b"Message-ID: <orig@example.com>\r\n"
    b"References: <older@example.com>\r\n\r\nhi\r\n")
db.execute(
    "INSERT INTO messages (account, uid, message_id, conv_key, ts, from_addr,"
    " to_json, subject, is_me, raw) VALUES"
    " ('me@example.com', 1, '<orig@example.com>', 'alice@example.com', 100,"
    " 'alice@example.com', '[\"me@example.com\"]', 'hello', 0, ?)", (raw,))
db.commit()

ctx = smtpsend.reply_context(db, cfg, "alice@example.com")
check("差出人は受け取ったアカウント", ctx["account"], "me@example.com")
check("宛先は会話の相手", ctx["to"], ["alice@example.com"])
check("元メールに繋げる", ctx["in_reply_to"], "<orig@example.com>")
check("以前の繋がりも引き継ぐ", ctx["references"], "<older@example.com>")
check("返事が来ている会話は追送扱いにしない", ctx["follow_up"], False)

msg = smtpsend.build(_Account(), ctx, "了解しました。")
check("From に自分が入る", "me@example.com" in msg["From"], True)
check("To に相手が入る", msg["To"], "alice@example.com")
check("In-Reply-To が入る", msg["In-Reply-To"], "<orig@example.com>")
check_true("References が積み上がる",
           "<older@example.com>" in msg["References"]
           and "<orig@example.com>" in msg["References"])
check("本文が入る", msg.get_content().strip(), "了解しました。")

# 自分へのメモには返信させない
try:
    smtpsend.reply_context(db, cfg, "self")
    check("自分へのメモは断る", "通った", "断る")
except smtpsend.SendError:
    check("自分へのメモは断る", "断る", "断る")

# 空の本文は組み立てさせない
try:
    smtpsend.build(_Account(), ctx, "   ")
    check("空の本文は断る", "通った", "断る")
except smtpsend.SendError:
    check("空の本文は断る", "断る", "断る")


# ---------------------------------------------------------------- 取り消し猶予

sent = []
smtpsend.send = lambda account, password, m: sent.append(m["To"]) or m["Message-ID"]
server.config.app_password = lambda account: "dummy"
server.UNDO_SECONDS = 1

ob = server.Outbox(cfg, db)

r = ob.queue("alice@example.com", "これは取り消す")
check("取り消せる", ob.cancel(r["id"]), True)
time.sleep(1.6)
check("取り消したら送られない", ob.state(r["id"])["state"], "cancelled")
check("実際に送信されていない", len(sent), 0)

r2 = ob.queue("alice@example.com", "これは送る")
time.sleep(1.6)
check("猶予を過ぎたら送られる", ob.state(r2["id"])["state"], "sent")
check("送信は1通だけ", len(sent), 1)
check("猶予後の取り消しは効かない", ob.cancel(r2["id"]), False)

db.close()

print("%d 件成功 / %d 件失敗" % (len(PASS), len(FAIL)))
if FAIL:
    print()
    for f in FAIL:
        print("  ✗ " + f)
    sys.exit(1)
print("すべて通りました。")
