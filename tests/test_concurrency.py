#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""複数スレッドから同時に触っても壊れないことを確かめる。

    python3 tests/test_concurrency.py

Python の sqlite3 は threadsafety=1 で、接続をスレッド間で共有できない。
共有すると SQLite の内部構造が壊れ、SQLITE_CORRUPT や segfault になる。
実際にこれでアプリが落ちた（sqlite3BtreeIndexMoveto での EXC_BAD_ACCESS）。

画面は ThreadingHTTPServer が配るので、同時アクセスは日常的に起きる。
"""

import os
import sqlite3
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gline  # noqa: E402
from gline import store  # noqa: E402

gline.use_utf8_output()

THREADS = 12
ROUNDS = 60

print("sqlite3.threadsafety =", sqlite3.threadsafety,
      "（1 なら接続はスレッド間で共有できない）")

tmp = tempfile.mkdtemp()
db = store.connect(os.path.join(tmp, "t.db"), "me@example.com")

# 土台のデータを入れる
for i in range(300):
    db.execute(
        "INSERT INTO messages (account, uid, message_id, conv_key, ts,"
        " from_addr, to_json, subject, is_me, body, body_full, attachments)"
        " VALUES ('me@example.com', ?, ?, ?, ?, 'a@x.com', '[]', ?, 0, ?, ?, '[]')",
        (i, "<%d@x>" % i, "a%d@x.com" % (i % 25), 1000 + i,
         "件名 %d" % i, "本文 %d" % i, "本文 %d" % i))
db.commit()

errors = []


def worker(n):
    try:
        for r in range(ROUNDS):
            db.execute("SELECT COUNT(*) FROM messages").fetchone()
            db.execute("SELECT * FROM messages WHERE conv_key=? ORDER BY ts DESC LIMIT 20",
                       ("a%d@x.com" % (r % 25),)).fetchall()
            db.execute("SELECT conv_key, COUNT(*) FROM messages GROUP BY conv_key").fetchall()
            if r % 10 == 0:
                db.execute("UPDATE messages SET unread=? WHERE uid=?", (r % 2, n))
                db.commit()
    except Exception as exc:
        errors.append("%s: %s" % (type(exc).__name__, exc))


threads = [threading.Thread(target=worker, args=(i,)) for i in range(THREADS)]
print("%d スレッド × %d 回 の読み書きを同時に実行…" % (THREADS, ROUNDS))
for t in threads:
    t.start()
for t in threads:
    t.join()

if errors:
    print("\n%d 件のエラー:" % len(errors))
    for e in errors[:5]:
        print("   ✗", e)
    sys.exit(1)

check = db.execute("PRAGMA integrity_check").fetchone()[0]
count = db.execute("SELECT COUNT(*) c FROM messages").fetchone()["c"]
db.close()

print("エラーなし / 通数 %d / 整合性 %s" % (count, check))
if check != "ok" or count != 300:
    print("✗ データが壊れました")
    sys.exit(1)
print("すべて通りました。")
