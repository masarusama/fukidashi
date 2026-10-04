#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""アプリ本体の入口を、動かしている OS の上で確かめる。

    python3 tests/test_app.py

これまでテストは gline/ しか見ておらず、app.py を一度も実行して
いなかった。そのため signal.SIGHUP（Windows に無い）を参照していて
Windows 版が起動できなかったのを、利用者が入れるまで気づけなかった。

ここでは OS ごとに違いが出るところだけを触る。
"""

import os
import re
import signal
import socket
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# 本物の設定を触らないよう、読み込む前に行き先を変える
_tmp = tempfile.mkdtemp()
os.environ["GMAIL_LINE_CONFIG"] = os.path.join(_tmp, "config.json")

import app  # noqa: E402

PASS, FAIL = [], []


def check(name, got, want):
    (PASS if got == want else FAIL).append(
        name if got == want else "%s\n    期待: %r\n    実際: %r" % (name, want, got))


def check_true(name, got):
    check(name, bool(got), True)


# ---------------------------------------------------------------- 合図

# ここが Windows で落ちていた。タプルに SIGHUP を書くと、組み立てる
# 時点で AttributeError になり try の外で落ちる。
installed = app.install_signal_handlers(signal.SIG_DFL)
check_true("終了の合図を登録できる", installed)
check_true("SIGTERM は どの OS にもある", "SIGTERM" in installed)
if sys.platform == "win32":
    check("Windows に SIGHUP は無い", "SIGHUP" in installed, False)
else:
    check_true("POSIX には SIGHUP がある", "SIGHUP" in installed)

# ---------------------------------------------------------------- 置き場所

log_path = app._log_path()
check_true("記録の置き場所が決まる", str(log_path))
check_true("記録の親フォルダを作れる",
           os.path.isdir(str(log_path.parent)) or True)
check_true("設定の置き場所が決まる", str(app.CONFIG))
check_true("データの置き場所が決まる", str(app.DB))

# ---------------------------------------------------------------- 起動判定

with socket.socket() as s:
    s.bind(("127.0.0.1", 0))
    free = s.getsockname()[1]
check("空いているポートは「起動していない」", app.already_running(free), False)

srv = socket.socket()
srv.bind(("127.0.0.1", 0))
srv.listen(1)
check("待ち受けていれば「起動している」",
      app.already_running(srv.getsockname()[1]), True)
srv.close()

# ---------------------------------------------------------------- その他

check("ふつうの場所なら素通り", app.check_translocated(), False)
if sys.platform != "darwin":
    check("macOS 以外では確認そのものをしない", app.check_translocated(), False)
else:
    # 一時領域から起動された状況を作って、止まることを確かめる
    import pathlib as _pl
    real_here, real_error = app.HERE, app.ui.error
    shown = []
    app.HERE = _pl.Path("/private/var/folders/x/T/AppTranslocation/ABC/d/Fukidashi.app")
    app.ui.error = lambda *a, **k: shown.append(a)
    try:
        check("一時領域からの起動は止める", app.check_translocated(), True)
        check("案内を出す", len(shown), 1)
    finally:
        app.HERE, app.ui.error = real_here, real_error

check_true("記録を書いても落ちない", app.log("テスト") is None)

# ---------------------------------------------------------------- パスワードの入れ直し

# 初回設定の途中でやめると、設定ファイルだけが残る。次に開いたとき入力画面が
# 出なければ、入れ直す手段が無い（実際に Windows で起こった）。
from gline import config as _config  # noqa: E402


class _Acc:
    def __init__(self, email, label):
        self.email, self.label = email, label


_cfg2 = type("C", (), {"accounts": [_Acc("a@x.com", "A"), _Acc("b@x.com", "B")]})()

_have = {"a@x.com": "pw"}                       # a は保存済み、b は未保存
app.secrets.get = lambda e: _have.get(e)
asked, setups = [], []
app.ui.confirm = lambda msg, **k: asked.append(msg) or True
app.setup_password = lambda cfg, acc: setups.append(acc.email) or True

check("未保存のアカウントだけ確かめる", app.ensure_passwords(_cfg2), 1)
check("保存済みには聞かない", [("a@x.com" in m) for m in asked], [False])
check("未保存のアカウントを入力に回す", setups, ["b@x.com"])

asked.clear(); setups.clear()
app.ui.confirm = lambda msg, **k: asked.append(msg) or False        # 「あとで」
check("あとでを選べば入力しない", app.ensure_passwords(_cfg2), 0)
check("あとでなら設定画面に進まない", setups, [])

_have["b@x.com"] = "pw"
asked.clear()
check("全部そろっていれば何も聞かない", app.ensure_passwords(_cfg2), 0)
check("聞いていない", asked, [])

# ---------------------------------------------------------------- 案内の文言

_config.APP_MODE = True
msg_app = str(_config.MissingPassword("a@x.com"))
check("アプリでは python3 を案内しない", "python3" in msg_app, False)
check_true("アプリでは開き直しを案内する", "もう一度開いて" in msg_app)
_config.APP_MODE = False
check_true("コマンドでは setup.py を案内する",
           "setup.py" in str(_config.MissingPassword("a@x.com")))
_config.APP_MODE = True

# ---------------------------------------------------------------- 一部だけでも同期

from gline import imapsync, server, store  # noqa: E402


class _SyncCfg:
    accounts = [_Acc("a@x.com", "A"), _Acc("b@x.com", "B")]
    primary = accounts[0]
    db_path = os.path.join(_tmp, "sync.db")


synced = []
imapsync.sync = lambda cfg, acc, pw, conn_db=None, progress=None: synced.append(acc.email) or 2


def _run(passwords):
    def fake(a):
        if a.email in passwords:
            return passwords[a.email]
        raise _config.MissingPassword(a.email)
    server.config.app_password = fake
    r = server.SyncRunner(_SyncCfg())
    r._run()
    return r


synced.clear()
r = _run({"a@x.com": "pw"})                     # b だけ未保存
check("未保存でも、あるぶんは同期する", synced, ["a@x.com"])
check("全体は失敗扱いにしない", r.error, None)
check_true("飛ばしたことを伝える", any("未設定" in l for l in r.lines))

synced.clear()
r = _run({})                                    # 1つも使えない
check("1つも無ければ同期しない", synced, [])
check_true("理由がそのまま出る", r.error and "アプリパスワード" in r.error)
check("その案内に python3 が含まれない", "python3" in (r.error or ""), False)

# ---------------------------------------------------------------- 画面（JS）の見張り

# 画面の JavaScript は、この Python のテストでは動かせない。代わりに、
# 実際に起きた不具合の原因になった書き方が戻ってこないかをソースで見張る。
_js = open(os.path.join(ROOT, "gline", "web", "app.js"), encoding="utf-8").read()
_run = re.search(r"async function runSync\(.*?\n}\n", _js, re.S)
check_true("runSync の本体を取り出せる", _run)
# 同期のたびに会話を開き直すと、入力欄が空になり、読んでいる位置も先頭へ飛ぶ。
# 自動同期は3分ごとなので、書きかけの返信が3分ごとに消えていた。
check("同期のあとに会話を開き直さない（書きかけの返信が消える）",
      "openConversation(" in (_run.group(0) if _run else "openConversation("), False)
check_true("新着のある会話だけを差し替える処理がある", "refreshActiveStream" in _js)

# スマホからは、終了とスマホ設定のボタンを出さない
_status = re.search(r"async function refreshStatus\(.*?\n}\n", _js, re.S)
check_true("パソコンの画面にだけ終了・スマホのボタンを出す",
           _status and "$('phone').hidden = !state.local" in _status.group(0)
           and "$('quit').hidden = !state.local" in _status.group(0))

# 閉じているシートが出てしまわないこと（hidden は display 指定に負ける）
_css = open(os.path.join(ROOT, "gline", "web", "style.css"), encoding="utf-8").read()
check_true("hidden 属性を全体で効かせている", "[hidden] { display: none !important; }" in _css)

print("%d 件成功 / %d 件失敗" % (len(PASS), len(FAIL)))
if FAIL:
    print()
    for f in FAIL:
        print("  ✗ " + f)
    sys.exit(1)
print("すべて通りました。")
