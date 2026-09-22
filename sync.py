#!/usr/bin/env python3
"""Gmail から取り込む（読む専用・サーバ側は書き換えない）。

    python3 sync.py                 全アカウントを前回の続きから同期
    python3 sync.py --full          取り込み済みを捨てて initial_days 分を再取得
    python3 sync.py --account <メール>  そのアカウントだけ
"""

import sys

from gline import config, imapsync, store


def main(argv):
    try:
        cfg = config.load()
    except config.ConfigError as exc:
        config.die(str(exc))

    only = None
    if "--account" in argv:
        i = argv.index("--account")
        if i + 1 >= len(argv):
            config.die("--account の後にアドレスを指定してください。")
        only = argv[i + 1].strip().lower()
        if not cfg.account(only):
            config.die("config.json にないアカウントです: %s\n設定済み: %s"
                       % (only, ", ".join(a.email for a in cfg.accounts)))

    targets = [a for a in cfg.accounts if only is None or a.email == only]

    # 最初にパスワードを全部そろえる（途中で止まらないように）
    passwords = {}
    for acc in targets:
        try:
            passwords[acc.email] = config.app_password(acc)
        except config.ConfigError as exc:
            config.die(str(exc))

    db = store.connect(cfg.db_path, cfg.primary.email)
    if "--full" in argv:
        for acc in targets:
            print("%s の取り込み済みデータを消して、%d 日分を取り直します。"
                  % (acc.email, cfg.initial_days))
            store.reset_messages(db, acc.email)

    total = 0
    for acc in targets:
        print("── %s (%s) ──" % (acc.label, acc.email))
        try:
            total += imapsync.sync(cfg, acc, passwords[acc.email], conn_db=db)
        except Exception as exc:
            print("  同期に失敗しました: %s" % exc)

    dups = store.mark_duplicates(db)
    if dups:
        print("複数アカウントに重複していたメール: %d 通（1通として表示します）" % dups)

    s = store.stats(db, cfg)
    print("取り込み %d 件 / 合計 %d 通・%d 会話" % (total, s["messages"], s["conversations"]))
    for email, v in s["per_account"].items():
        print("   %-32s %5d 通 / 未読 %4d" % (email, v["messages"], v["unread"]))
    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
