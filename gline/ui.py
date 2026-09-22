"""OS 標準のダイアログで対話する。追加ライブラリは使わない。

macOS では osascript、それ以外では Python 同梱の tkinter を使う。
ターミナルを開けない人でも初回設定を終えられるようにするのが目的。
"""

import subprocess
import sys

TITLE = "Fukidashi"
IS_MAC = sys.platform == "darwin"


class Cancelled(Exception):
    """利用者がキャンセルした。"""


# ---------------------------------------------------------------- macOS

def _esc(text):
    return str(text).replace("\\", "\\\\").replace('"', '\\"')


def _osa(script):
    out = subprocess.run(["osascript", "-e", script],
                         capture_output=True, text=True)
    if out.returncode != 0:
        err = out.stderr.strip()
        if "User canceled" in err or "-128" in err:
            raise Cancelled()
        raise RuntimeError(err or "osascript が失敗しました")
    return out.stdout.strip()


def _mac_alert(message, title, ok, icon="note"):
    _osa('display dialog "%s" with title "%s" buttons {"%s"} '
         'default button "%s" with icon %s'
         % (_esc(message), _esc(title), _esc(ok), _esc(ok), icon))


def _mac_confirm(message, yes, no, title):
    try:
        res = _osa('display dialog "%s" with title "%s" buttons {"%s","%s"} '
                   'default button "%s"'
                   % (_esc(message), _esc(title), _esc(no), _esc(yes), _esc(yes)))
    except Cancelled:
        return False
    return yes in res


def _mac_ask(prompt, default, hidden, title):
    extra = " with hidden answer" if hidden else ""
    return _osa('display dialog "%s" with title "%s" default answer "%s" '
                'buttons {"キャンセル","次へ"} default button "次へ"%s\n'
                'text returned of result'
                % (_esc(prompt), _esc(title), _esc(default), extra))


# ---------------------------------------------------------------- その他

def _tk():
    """隠した親ウィンドウを用意する。tkinter は Python に同梱されている。"""
    import tkinter as tk
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    return tk, root


def _tk_alert(message, title, ok, kind="info"):
    tk, root = _tk()
    from tkinter import messagebox
    try:
        getattr(messagebox, "show" + kind)(title, message, parent=root)
    finally:
        root.destroy()


def _tk_confirm(message, yes, no, title):
    tk, root = _tk()
    from tkinter import messagebox
    try:
        return bool(messagebox.askyesno(
            title, "%s\n\n［はい］= %s　／　［いいえ］= %s" % (message, yes, no),
            parent=root))
    finally:
        root.destroy()


def _tk_ask(prompt, default, hidden, title):
    tk, root = _tk()
    from tkinter import simpledialog
    try:
        val = simpledialog.askstring(title, prompt, initialvalue=default,
                                     show="*" if hidden else None, parent=root)
    finally:
        root.destroy()
    if val is None:
        raise Cancelled()
    return val


# ---------------------------------------------------------------- 公開 API

def alert(message, title=TITLE, ok="OK"):
    (_mac_alert(message, title, ok) if IS_MAC
     else _tk_alert(message, title, ok, "info"))


def error(message, title=TITLE):
    (_mac_alert(message, title, "OK", icon="stop") if IS_MAC
     else _tk_alert(message, title, "OK", "error"))


def confirm(message, yes="続ける", no="やめる", title=TITLE):
    return (_mac_confirm(message, yes, no, title) if IS_MAC
            else _tk_confirm(message, yes, no, title))


def ask(prompt, default="", hidden=False, title=TITLE):
    return (_mac_ask(prompt, default, hidden, title) if IS_MAC
            else _tk_ask(prompt, default, hidden, title))


def notify(message, title=TITLE):
    """邪魔にならない通知。出せない環境では黙って諦める。"""
    if not IS_MAC:
        return
    try:
        _osa('display notification "%s" with title "%s"'
             % (_esc(message), _esc(title)))
    except Exception:
        pass


def open_url(url):
    import webbrowser
    if IS_MAC:
        subprocess.run(["open", url], capture_output=True)
    else:
        webbrowser.open(url)
