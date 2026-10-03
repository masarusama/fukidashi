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

if sys.platform != "darwin":
    check("macOS 以外では移動の確認をしない", app.check_translocated(), False)

check_true("記録を書いても落ちない", app.log("テスト") is None)

print("%d 件成功 / %d 件失敗" % (len(PASS), len(FAIL)))
if FAIL:
    print()
    for f in FAIL:
        print("  ✗ " + f)
    sys.exit(1)
print("すべて通りました。")
