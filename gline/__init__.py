"""Fukidashi — Gmail を LINE のような会話ビューで読むローカルツール（読む専用）。"""

import sys

__version__ = "0.1.0"


def use_utf8_output():
    """標準出力を UTF-8 にする。

    Windows では、出力先が端末でなくパイプやファイルだと cp1252 などに
    なり、日本語を出そうとした時点で UnicodeEncodeError で落ちる。
    ログに残す使い方で確実に踏むので、入口で必ず呼ぶ。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass
