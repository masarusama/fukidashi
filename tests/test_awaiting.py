#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「未返信」の目印のテスト。

    python3 tests/test_awaiting.py

未返信 = 「人」の会話で、いちばん新しい1通が相手からのもの。
「対応不要」にすると外れ、そのあとに新しい着信があれば自動で戻る。
受信箱は書き換えない（手元の記録だけ）。
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gline import store  # noqa: E402

PASS, FAIL = [], []


def check(name, got, want):
    if got == want:
        PASS.append(name)
    else:
        FAIL.append("%s\n    期待: %r\n    実際: %r" % (name, want, got))


class Cfg:
    me = {"me@example.com"}
    force_people, force_notices, mute = [], [], []


db = store.connect(os.path.join(tempfile.mkdtemp(), "t.db"), "me@example.com")
_uid = [0]


def add(conv, ts, is_me, bulk=0):
    _uid[0] += 1
    db.execute(
        "INSERT INTO messages (account, uid, conv_key, ts, from_addr, is_me, bulk,"
        " body, attachments) VALUES ('me@example.com', ?, ?, ?, ?, ?, ?, 'x', '[]')",
        (_uid[0], conv, ts, "me@example.com" if is_me else conv, 1 if is_me else 0, bulk))
    db.commit()


def conv(key):
    for c in store.conversations(db, Cfg()):
        if c["key"] == key:
            return c


# 相手から来て、まだ返していない
add("a@example.com", 100, False)
check("相手が最後なら未返信", conv("a@example.com")["awaiting"], True)

# 自分が返したら外れる
add("a@example.com", 200, True)
check("自分が最後なら未返信ではない", conv("a@example.com")["awaiting"], False)

# 相手がまた書いたら戻る
add("a@example.com", 300, False)
check("また相手が最後なら戻る", conv("a@example.com")["awaiting"], True)

# 対応不要にする
check("対応不要にできる", store.set_no_reply_needed(db, "a@example.com", True), True)
c = conv("a@example.com")
check("対応不要にすると外れる", (c["awaiting"], c["dismissed"]), (False, True))

# そのあとの新着で、自動で戻る
add("a@example.com", 400, False)
c = conv("a@example.com")
check("新着が来たら自動で戻る", (c["awaiting"], c["dismissed"]), (True, False))

# 戻す操作
store.set_no_reply_needed(db, "a@example.com", True)
store.set_no_reply_needed(db, "a@example.com", False)
check("未返信に戻せる", conv("a@example.com")["awaiting"], True)

# 自分が返せば、対応不要の記録があっても未返信ではない
store.set_no_reply_needed(db, "a@example.com", True)
add("a@example.com", 500, True)
c = conv("a@example.com")
check("自分が最後なら対応不要の印も出ない", (c["awaiting"], c["dismissed"]), (False, False))

# お知らせは対象外
add("news@mail.example.com", 100, False, bulk=1)
c = conv("news@mail.example.com")
check("お知らせは未返信にしない", (c["kind"], c["awaiting"]), ("notice", False))

# 自分へのメモは対象外
add("self", 100, False)
check("自分へのメモは未返信にしない", conv("self")["awaiting"], False)

# 存在しない会話・不正
check("知らない会話は対応不要にできない",
      store.set_no_reply_needed(db, "nobody@example.com", True), False)
check("戻す操作は、記録が無くても失敗しない",
      store.set_no_reply_needed(db, "nobody@example.com", False), True)

# 受信箱は書き換えない（未読のまま）
db.execute("INSERT INTO messages (account, uid, conv_key, ts, from_addr, is_me, unread,"
           " attachments) VALUES ('me@example.com', 999, 'u@example.com', 10,"
           " 'u@example.com', 0, 1, '[]')")
db.commit()
store.set_no_reply_needed(db, "u@example.com", True)
check("対応不要にしても未読は変わらない",
      db.execute("SELECT unread FROM messages WHERE uid=999").fetchone()["unread"], 1)

print("%d 件成功 / %d 件失敗" % (len(PASS), len(FAIL)))
if FAIL:
    print()
    for f in FAIL:
        print("  ✗ " + f)
    sys.exit(1)
print("すべて通りました。")
