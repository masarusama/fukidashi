#!/usr/bin/env python3
"""初期設定をひととおり案内する。

    python3 setup.py

アカウントの登録、アプリパスワードの保存、接続確認、最初の同期まで。
入力したパスワードは OS の保管庫に入るだけで、設定ファイルにも
このプログラムのどこにも書き出されません。
"""

import getpass
import json
import sys
import webbrowser
from pathlib import Path

from gline import config, imapsync, secrets, store

APP_PASSWORD_URL = "https://myaccount.google.com/apppasswords"
ROOT = Path(__file__).resolve().parent


def ask(prompt, default=None):
    suffix = " [%s]" % default if default else ""
    while True:
        try:
            val = input("%s%s: " % (prompt, suffix)).strip()
        except (EOFError, KeyboardInterrupt):
            print("\n中断しました。")
            raise SystemExit(1)
        if val:
            return val
        if default is not None:
            return default


def yes(prompt, default=True):
    hint = "Y/n" if default else "y/N"
    while True:
        val = ask("%s (%s)" % (prompt, hint), "y" if default else "n").lower()
        if val in ("y", "yes", "はい"):
            return True
        if val in ("n", "no", "いいえ"):
            return False


def rule(title):
    print("\n" + "─" * 58)
    print(title)
    print("─" * 58)


def collect_accounts(existing):
    accounts = []
    if existing:
        print("すでに設定されているアカウント:")
        for a in existing:
            print("   ・%s (%s)" % (a.email, a.label))
        if yes("これに追加しますか？（いいえ＝最初から作り直す）"):
            accounts = [{"email": a.email, "label": a.label,
                         "aliases": sorted(a.addrs - {a.email})} for a in existing]

    while True:
        n = len(accounts) + 1
        email = ask("\n%d つめのアカウントの Gmail アドレス" % n)
        if "@" not in email:
            print("   アドレスの形式が違うようです。")
            continue
        label = ask("   画面に出す短い名前", email.split("@")[0])

        aliases = []
        print("\n   このアカウントに、別のアドレス宛のメールが転送されて届いていますか？")
        print("   （届いているのに登録しないと、会話がばらばらに表示されます）")
        while yes("   別アドレスを登録する", False):
            a = ask("      アドレス")
            if "@" in a:
                aliases.append(a)

        accounts.append({"email": email, "label": label, "aliases": aliases})
        if not yes("\nもう1つアカウントを追加しますか？", False):
            break
    return accounts


def ensure_password(email):
    try:
        if secrets.get(email):
            print("   パスワードは保存済みです。")
            if not yes("   入れ直しますか？", False):
                return True
    except secrets.SecretError as exc:
        print("   %s" % exc)

    print("\n   %s のアプリパスワードが必要です。" % email)
    print("   ・Google アカウントで2段階認証が有効になっている必要があります")
    print("   ・16桁のパスワードを、下のページで作成してください")
    print("     %s" % APP_PASSWORD_URL)
    if yes("   ブラウザで開きますか？"):
        try:
            webbrowser.open(APP_PASSWORD_URL)
        except Exception:
            pass

    for attempt in range(3):
        pw = getpass.getpass("   アプリパスワード（入力は表示されません）: ")
        if not pw.strip():
            print("   空でした。もう一度どうぞ。")
            continue
        try:
            where = secrets.put(email, pw)
        except secrets.SecretError as exc:
            print("\n   保存できませんでした:\n   %s" % exc)
            return False
        print("   %s に保存しました。" % where)
        return True
    return False


def check_login(cfg, account):
    print("   接続を確認しています…")
    try:
        pw = config.app_password(account)
        imap = imapsync.connect(cfg, account, pw)
        mbox = imapsync.find_all_mail(imap)
        imap.logout()
    except Exception as exc:
        print("   つながりませんでした: %s" % exc)
        print("   アプリパスワードが正しいか、Gmail の IMAP が有効かを確認してください。")
        return False
    print("   OK（メールボックス: %s）" % mbox)
    return True


def main():
    rule("Gmail を会話として読むための初期設定")
    print("パスワードは OS の保管庫に入るだけで、設定ファイルには書きません。")
    print("このプログラムは Gmail 以外のどこにも接続しません。")

    cfg_path = ROOT / "config.json"
    existing = []
    if cfg_path.exists():
        try:
            existing = config.load(cfg_path).accounts
        except config.ConfigError:
            pass

    rule("1. アカウント")
    accounts = collect_accounts(existing)

    days = ask("\n何日分さかのぼって取り込みますか", "90")
    try:
        days = max(1, int(days))
    except ValueError:
        days = 90

    data = {
        "accounts": accounts,
        "initial_days": days,
        "strip_patterns": [],
        "force_people": [],
        "force_notices": [],
        "mute": [],
        "port": 8765,
    }
    cfg_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
    print("\n%s を書きました。" % cfg_path.name)

    rule("2. アプリパスワード")
    cfg = config.load(cfg_path)
    ok = []
    for account in cfg.accounts:
        print("\n▸ %s" % account.email)
        if ensure_password(account.email) and check_login(cfg, account):
            ok.append(account)

    if not ok:
        print("\n使えるアカウントがありませんでした。設定を見直してもう一度実行してください。")
        return 1

    rule("3. 最初の取り込み")
    print("%d 日分を取り込みます。初回は数分かかります。" % cfg.initial_days)
    if not yes("いま実行しますか？"):
        print("\nあとで次を実行してください:\n   python3 sync.py")
        return 0

    db = store.connect(cfg.db_path, cfg.primary.email)
    for account in ok:
        print("\n── %s ──" % account.label)
        try:
            imapsync.sync(cfg, account, config.app_password(account), conn_db=db)
        except Exception as exc:
            print("   失敗しました: %s" % exc)
    store.mark_duplicates(db)
    s = store.stats(db, cfg)
    db.close()

    rule("できました")
    print("%d 通 / %d 会話 を取り込みました。" % (s["messages"], s["conversations"]))
    print("\n次を実行すると画面が開きます:\n   python3 serve.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
