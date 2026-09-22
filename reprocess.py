#!/usr/bin/env python3
"""保存してある生データから本文を作り直す（Gmail には接続しない）。

strip_patterns を足したときや、解析を直したときはこれを実行する。
再ダウンロードが要らないので数秒で終わる。
"""

import sys
import zlib

import gline
from gline import config, imapsync, store


def main():
    gline.use_utf8_output()
    try:
        cfg = config.load()
    except config.ConfigError as exc:
        config.die(str(exc))

    db = store.connect(cfg.db_path, cfg.primary.email)
    rows = store.raw_rows(db)
    if not rows:
        config.die("生データが保存されていません。`python3 sync.py --full` で取り直してください。")

    print("%d 通を作り直します…" % len(rows))
    changed = failed = skipped = 0
    for i, row in enumerate(rows, 1):
        if row["truncated"]:
            continue
        account = cfg.account(row["account"])
        if account is None:
            skipped += 1
            continue
        try:
            raw = zlib.decompress(row["raw"])
            rec, names = imapsync.build_record(
                cfg, account, row["uid"], raw, "", 0, keep_raw=False)
            store.rewrite_body(db, row["account"], row["uid"], rec)
            for addr, name, ts in names:
                store.remember_name(db, addr, name, ts)
            changed += 1
        except Exception as exc:
            failed += 1
            print("  %s/%s を作り直せませんでした: %s" % (row["account"], row["uid"], exc))
        if i % 500 == 0:
            db.commit()
            print("  %d / %d" % (i, len(rows)))
    db.commit()
    store.mark_duplicates(db)

    if skipped:
        print("config.json にないアカウントのメールは触りませんでした: %d 通" % skipped)
    s = store.stats(db, cfg)
    print("完了: %d 通を更新（失敗 %d）/ 合計 %d 通・%d 会話"
          % (changed, failed, s["messages"], s["conversations"]))
    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
