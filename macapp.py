#!/usr/bin/env python3
"""Fukidashi.app の中身。ターミナルを使わずに設定と起動をこなす。

初回はダイアログで設定を作り、2回目以降はそのまま画面を開く。
設定とデータは ~/Library/Application Support/Fukidashi/ に置く
（アプリ本体の中には書かない）。
"""

import json
import os
import signal
import sys
import threading
import time
import traceback
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

SUPPORT = Path.home() / "Library" / "Application Support" / "Fukidashi"
# 設定の場所は環境変数で差し替えられる（動作確認や、既存の設定を使いたいとき）
CONFIG = Path(os.environ.get("GMAIL_LINE_CONFIG") or (SUPPORT / "config.json"))
DB = SUPPORT / "mail.db"
APP_PASSWORD_URL = "https://myaccount.google.com/apppasswords"


def _boot():
    """設定の場所を教えてから gline を読み込む。"""
    SUPPORT.mkdir(parents=True, exist_ok=True)
    os.environ["GMAIL_LINE_CONFIG"] = str(CONFIG)
    CONFIG.parent.mkdir(parents=True, exist_ok=True)


_boot()

from gline import config, imapsync, macui, secrets, server, store  # noqa: E402


# ---------------------------------------------------------------- 初回設定

def first_run():
    macui.alert(
        "Fukidashi へようこそ。\n\n"
        "Gmail を会話の形で読むための道具です。\n"
        "メールはこのパソコンの中だけで処理され、Gmail 以外のどこにも送られません。\n\n"
        "はじめに、読みたい Gmail アカウントを登録します。")

    accounts = []
    while True:
        email = macui.ask("Gmail のアドレスを入力してください", title="アカウントの登録")
        email = email.strip().lower()
        if "@" not in email:
            macui.error("アドレスの形式が違うようです。")
            continue

        label = macui.ask("画面に表示する短い名前を決めてください",
                          email.split("@")[0], title="アカウントの登録")

        aliases = []
        if macui.confirm(
                "このアカウントに、別のアドレス宛のメールが\n"
                "転送されて届いていますか？\n\n"
                "（届いているのに登録しないと、会話がばらばらに表示されます）",
                yes="登録する", no="ない"):
            while True:
                a = macui.ask("転送元のアドレス", title="別アドレスの登録").strip().lower()
                if "@" in a:
                    aliases.append(a)
                if not macui.confirm("もう1つ登録しますか？", yes="登録する", no="終わり"):
                    break

        accounts.append({"email": email, "label": label, "aliases": aliases})

        if not macui.confirm("もう1つアカウントを追加しますか？",
                             yes="追加する", no="これで完了"):
            break

    CONFIG.write_text(json.dumps({
        "accounts": accounts,
        "initial_days": 90,
        "strip_patterns": [],
        "force_people": [],
        "force_notices": [],
        "mute": [],
        "port": 8765,
        "db_path": str(DB),
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    cfg = config.load(CONFIG)
    for account in cfg.accounts:
        if not setup_password(cfg, account):
            macui.error("%s は設定できませんでした。\n"
                        "あとでもう一度アプリを起動すると、やり直せます。" % account.email)
    return cfg


def setup_password(cfg, account):
    if secrets.get(account.email):
        return True

    macui.alert(
        "%s のアプリパスワードが必要です。\n\n"
        "これは Google アカウント本体のパスワードではなく、\n"
        "アプリ専用に発行する16桁のパスワードです。\n"
        "（2段階認証が有効になっている必要があります）\n\n"
        "次に開くページで作成して、コピーしてきてください。" % account.email,
        title="アプリパスワード", ok="ページを開く")
    macui.open_url(APP_PASSWORD_URL)

    for _ in range(3):
        try:
            pw = macui.ask("作成した16桁のアプリパスワードを貼り付けてください\n"
                           "（%s）" % account.email,
                           hidden=True, title="アプリパスワード")
        except macui.Cancelled:
            return False
        if not pw.strip():
            continue
        try:
            secrets.put(account.email, pw)
        except secrets.SecretError as exc:
            macui.error("保存できませんでした。\n%s" % exc)
            return False

        try:
            imap = imapsync.connect(cfg, account, config.app_password(account))
            imapsync.find_all_mail(imap)
            imap.logout()
            return True
        except Exception as exc:
            if not macui.confirm(
                    "Gmail にログインできませんでした。\n\n%s\n\n"
                    "パスワードを入力し直しますか？" % exc,
                    yes="やり直す", no="あとで"):
                return False
    return False


# ---------------------------------------------------------------- 取り込み

def initial_sync(cfg):
    macui.notify("メールの取り込みを始めます（数分かかります）")
    db = store.connect(cfg.db_path, cfg.primary.email)
    got = 0
    for account in cfg.accounts:
        try:
            got += imapsync.sync(cfg, account, config.app_password(account),
                                 conn_db=db, progress=lambda m: None)
        except Exception:
            traceback.print_exc()
    store.mark_duplicates(db)
    s = store.stats(db, cfg)
    db.close()
    macui.notify("%d 通 / %d 会話 を取り込みました" % (s["messages"], s["conversations"]))
    return got


# ---------------------------------------------------------------- 起動

def already_running(port):
    """すでに Fukidashi が動いていれば True。"""
    import json as _json
    import urllib.request
    try:
        with urllib.request.urlopen(
                "http://127.0.0.1:%d/api/status" % port, timeout=2) as res:
            _json.loads(res.read().decode("utf-8"))
        return True
    except Exception:
        return False


def main():
    try:
        if CONFIG.exists():
            cfg = config.load(CONFIG)
        else:
            cfg = first_run()
            threading.Thread(target=initial_sync, args=(cfg,), daemon=True).start()
    except macui.Cancelled:
        return 0
    except Exception as exc:
        traceback.print_exc()
        try:
            macui.error("設定中に問題が起きました。\n\n%s" % exc)
        except Exception:
            pass
        return 1

    url = "http://127.0.0.1:%d/" % cfg.port
    if already_running(cfg.port):
        # 2回目のダブルクリック。立ち上げ直さず、画面を出すだけ。
        webbrowser.open(url)
        return 0

    try:
        httpd = server.serve(cfg)
    except Exception as exc:
        # よくある2つの失敗を、原因が分かる言葉で出す
        if "authorization denied" in str(exc) or isinstance(exc, PermissionError):
            macui.error(
                "データの置き場所を開けませんでした。\n\n%s\n\n"
                "macOS のプライバシー保護により、アプリからこの場所を\n"
                "読めません。iCloud や Google ドライブの中を指している\n"
                "場合によく起きます。\n\n"
                "設定ファイルの db_path を、ふつうの場所（初期値の\n"
                "~/Library/Application Support/Fukidashi/mail.db など）に\n"
                "変えてください。\n\n設定: %s" % (exc, CONFIG))
        elif isinstance(exc, OSError):
            macui.error("ポート %d を使えませんでした。\n\n%s\n\n"
                        "Fukidashi がすでに起動しているかもしれません。"
                        % (cfg.port, exc))
        else:
            macui.error("起動できませんでした。\n\n%s\n\n"
                        "詳しい記録: ~/Library/Logs/Fukidashi.log" % exc)
        return 1

    if not os.environ.get("FUKIDASHI_NO_BROWSER"):
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    # ログアウトや再起動、強制終了でも DB を壊さずに終える
    def _stop(signum, frame):
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        try:
            signal.signal(sig, _stop)
        except (OSError, ValueError):
            pass

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown(httpd)
    return 0


if __name__ == "__main__":
    sys.exit(main())
