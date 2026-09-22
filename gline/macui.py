"""macOS 標準のダイアログだけで対話する。

追加ライブラリなしで GUI を出すために osascript を使う。
ターミナルを開けない人でも初回設定を終えられるようにするのが目的。
"""

import subprocess

TITLE = "Fukidashi"


class Cancelled(Exception):
    """利用者がキャンセルを押した。"""


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


def alert(message, title=TITLE, ok="OK"):
    _osa('display dialog "%s" with title "%s" buttons {"%s"} '
         'default button "%s" with icon note'
         % (_esc(message), _esc(title), _esc(ok), _esc(ok)))


def error(message, title=TITLE):
    _osa('display dialog "%s" with title "%s" buttons {"OK"} '
         'default button "OK" with icon stop' % (_esc(message), _esc(title)))


def confirm(message, yes="続ける", no="やめる", title=TITLE):
    try:
        res = _osa('display dialog "%s" with title "%s" buttons {"%s","%s"} '
                   'default button "%s"'
                   % (_esc(message), _esc(title), _esc(no), _esc(yes), _esc(yes)))
    except Cancelled:
        return False
    return _esc(yes) in res or yes in res


def ask(prompt, default="", hidden=False, title=TITLE):
    extra = " with hidden answer" if hidden else ""
    script = ('display dialog "%s" with title "%s" default answer "%s" '
              'buttons {"キャンセル","次へ"} default button "次へ"%s\n'
              'text returned of result'
              % (_esc(prompt), _esc(title), _esc(default), extra))
    return _osa(script)


def choose(prompt, options, title=TITLE):
    items = ", ".join('"%s"' % _esc(o) for o in options)
    script = ('choose from list {%s} with title "%s" with prompt "%s"' 
              % (items, _esc(title), _esc(prompt)))
    res = _osa(script)
    if res == "false":
        raise Cancelled()
    return res


def notify(message, title=TITLE):
    try:
        _osa('display notification "%s" with title "%s"'
             % (_esc(message), _esc(title)))
    except Exception:
        pass


def open_url(url):
    subprocess.run(["open", url], capture_output=True)
