"""アプリパスワードの保管場所を OS ごとに吸収する。

どの経路でも、パスワードがこのプログラムの外（ネットワークやファイル）に
出ることはない。IMAP のログインに使って、それで終わり。

優先順:
  1. 環境変数
  2. macOS の security コマンド（追加インストール不要）
  3. keyring パッケージ（Windows 資格情報マネージャー / freedesktop secret service）
  4. Linux の secret-tool コマンド
"""

import os
import platform
import re
import subprocess

SERVICE = "gmail_line"
IS_MAC = platform.system() == "Darwin"
IS_WINDOWS = platform.system() == "Windows"


class SecretError(Exception):
    pass


def env_key(email):
    return "GMAIL_LINE_APP_PASSWORD_" + re.sub(r"\W", "_", email).upper()


def _keyring():
    try:
        import keyring
        return keyring
    except Exception:
        return None


def _run(cmd, stdin=None):
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              input=stdin, timeout=25)
    except (OSError, subprocess.SubprocessError) as exc:
        raise SecretError("コマンドを実行できませんでした（%s）: %s" % (cmd[0], exc))


def get(email):
    """保存されているパスワードを返す。無ければ None。"""
    for key in (env_key(email), "GMAIL_LINE_APP_PASSWORD"):
        val = os.environ.get(key)
        if val:
            return val.replace(" ", "")

    if IS_MAC:
        out = _run(["security", "find-generic-password",
                    "-s", SERVICE, "-a", email, "-w"])
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip().replace(" ", "")

    kr = _keyring()
    if kr is not None:
        try:
            val = kr.get_password(SERVICE, email)
            if val:
                return val.replace(" ", "")
        except Exception:
            pass

    if not IS_MAC and not IS_WINDOWS:
        out = _run(["secret-tool", "lookup", "service", SERVICE, "account", email])
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip().replace(" ", "")

    return None


def put(email, password):
    """パスワードを OS の保管庫に入れる。成功したら保管庫の名前を返す。"""
    password = password.replace(" ", "")
    if not password:
        raise SecretError("空のパスワードは保存できません。")

    if IS_MAC:
        # パスワードはコマンド引数に置かない（同じマシンの ps から見えてしまう）。
        # security は -w を値なしで渡すと標準入力から2回読む。
        out = _run(["security", "add-generic-password", "-U",
                    "-s", SERVICE, "-a", email, "-w"],
                   stdin="%s\n%s\n" % (password, password))
        if out.returncode == 0:
            return "macOS キーチェーン"
        raise SecretError("キーチェーンに保存できませんでした: %s"
                          % (out.stderr.strip() or out.returncode))

    kr = _keyring()
    if kr is not None:
        try:
            kr.set_password(SERVICE, email, password)
            return "Windows 資格情報マネージャー" if IS_WINDOWS else "システムの資格情報ストア"
        except Exception as exc:
            raise SecretError("資格情報ストアに保存できませんでした: %s" % exc)

    if not IS_WINDOWS:
        out = _run(["secret-tool", "store", "--label=gmail_line",
                    "service", SERVICE, "account", email], stdin=password)
        if out.returncode == 0:
            return "secret-tool"

    raise SecretError(how_to_install())


def how_to_install():
    if IS_WINDOWS:
        return ("パスワードの保管に keyring パッケージが必要です。\n"
                "    pip install keyring\n"
                "を実行してからもう一度お試しください。")
    if IS_MAC:
        return "macOS の security コマンドが見つかりませんでした。"
    return ("パスワードの保管に keyring パッケージ、または secret-tool が必要です。\n"
            "    pip install keyring\n"
            "    （または）sudo apt install libsecret-tools")


def manual_instructions(email):
    """自分で保存したい人向けの手順。"""
    if IS_MAC:
        return "   security add-generic-password -U -s %s -a %s -w" % (SERVICE, email)
    if IS_WINDOWS:
        return ("   python -c \"import keyring,getpass;"
                "keyring.set_password('%s','%s',getpass.getpass())\"" % (SERVICE, email))
    return "   secret-tool store --label=gmail_line service %s account %s" % (SERVICE, email)
