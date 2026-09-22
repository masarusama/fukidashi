#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""解析まわりの回帰テスト。

    python3 tests/test_parsing.py

ここに並んでいるのは、実際の受信箱で見つかって直した不具合ばかり。
どれも「静かに間違った結果を出す」たちの悪いものだったので、残しておく。
標準ライブラリだけで動く（pytest は要らない）。
"""

import email
import os
import sys
from email.message import EmailMessage

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gline  # noqa: E402
from gline import mailparse, server, store  # noqa: E402

gline.use_utf8_output()

PASS, FAIL = [], []


def check(name, got, want):
    if got == want:
        PASS.append(name)
    else:
        FAIL.append("%s\n    期待: %r\n    実際: %r" % (name, want, got))


def check_true(name, got):
    check(name, bool(got), True)


# ---------------------------------------------------------------- 引用剥がし

check("英語Gmailの返信引用を落とす",
      mailparse.strip_quotes(
          "了解しました。\n\n"
          "On Mon, Jan 5, 2026 at 9:02 AM Alice <a@x.com> wrote:\n"
          "> 打ち合わせの候補を送ります。\n"),
      "了解しました。")

check("日本語Gmailの返信引用を落とす",
      mailparse.strip_quotes(
          "承知しました。\n\n"
          "2026年1月5日(月) 9:02 山田太郎 <y@x.com>:\n"
          "> 資料を添付しました。\n"),
      "承知しました。")

check("Outlook形式のヘッダブロックを落とす",
      mailparse.strip_quotes(
          "ご連絡ありがとうございます。\n\n"
          "差出人: 佐藤 <s@x.com>\n送信日時: 2026年1月5日 9:02\n"
          "宛先: 自分\n件名: RE: 見積もり\n\n本文\n"),
      "ご連絡ありがとうございます。")

check("-- から下の署名を落とす",
      mailparse.strip_quotes("確認しました。\n\n--\n株式会社れい\n鈴木\n"),
      "確認しました。")

check("連絡先ブロックを落とす（本文後半に2行以上）",
      mailparse.strip_quotes(
          "林様\n\nお世話になっております。\n来週書類をお渡しします。\n\n"
          "〒174-0063 東京都板橋区\nTEL：03-0000-0000\nFAX：03-0000-0001\n"),
      "林様\n\nお世話になっております。\n来週書類をお渡しします。")

check("本文中の電話番号は消さない",
      mailparse.strip_quotes(
          "お電話ありがとうございました。\nTEL：03-1111-2222 にかけ直します。\n"
          "よろしくお願いします。"),
      "お電話ありがとうございました。\nTEL：03-1111-2222 にかけ直します。\n"
      "よろしくお願いします。")

# 罫線だらけのメルマガで全部消える事故を防ぐ
check_true("削りすぎたら元に戻す",
           "週刊ニュース" in mailparse.strip_quotes(
               "=" * 50 + "\n週刊ニュース 第12号\n" + "=" * 50 + "\n今週の話題。"))

# ---------------------------------------------------------------- HTML

# 閉じタグの無い <meta> や <link> が読み飛ばしを開始したまま解除されず、
# HTML だけのメール（受信箱の24%）が丸ごと空になっていた
_DOC = ('<html><head><meta charset="utf-8"><meta name="v" content="x">'
        '<link rel="stylesheet" href="a.css"><title>件名</title>'
        '<style>p{color:red}</style></head><body>'
        '<table><tr><td><p>今週の注目記事です。</p></td></tr></table>'
        '<blockquote class="gmail_quote">引用部分</blockquote></body></html>')

check_true("空要素タグの後も本文を読める", "今週の注目記事です。" in mailparse.html_to_text(_DOC))
check_true("head の中身は出さない", "件名" not in mailparse.html_to_text(_DOC))
check_true("引用ブロックは畳む", "引用部分" not in mailparse.html_to_text(_DOC, drop_quotes=True))
check_true("全文側には引用も残す", "引用部分" in mailparse.html_to_text(_DOC, drop_quotes=False))

check("CRLF を LF に揃える",
      mailparse.tidy("一行目\r\n二行目\r\n"), "一行目\n二行目")

# ---------------------------------------------------------------- 文字コード

# ISO-2022-JP は7bitなので utf-8 でも「成功」してしまい、
# エスケープ列がそのまま本文に残って文字化けしていた
_JIS = "こんにちは、テストです。".encode("iso-2022-jp")
check("ISO-2022-JP を正しく解く", mailparse._decode_bytes(_JIS, "iso-2022-jp"),
      "こんにちは、テストです。")
check("charset の申告が誤っていても解く", mailparse._decode_bytes(_JIS, "us-ascii"),
      "こんにちは、テストです。")
check("UTF-8 はそのまま", mailparse._decode_bytes("ふつうの本文".encode("utf-8"), "utf-8"),
      "ふつうの本文")

# ---------------------------------------------------------------- ヘッダ

# デコード後の表示名に <> や [] が入ると、アドレス解析が壊れていた
_M = email.message_from_string(
    "From: =?UTF-8?B?5a6J5b+D5LuL6K2344Oe44Ks44K444Oz6YWN5L+hPOmAgeS/oeWwgueUqD4=?="
    " <magazine@example.jp>\nTo: me@example.com\nSubject: test\n\nhi\n")
check("表示名に <> があってもアドレスを取れる",
      mailparse.addresses(_M, "From"),
      [("安心介護マガジン配信<送信専用>", "magazine@example.jp")])

# ---------------------------------------------------------------- 本文の選択

_ALT = EmailMessage()
_ALT["From"] = "a@x.com"
_ALT["To"] = "me@example.com"
_ALT["Subject"] = "テスト"
_ALT.set_content("このメールはhtmlメールです。")
_ALT.add_alternative("<html><body><p>こちらが本当の本文です。</p></body></html>",
                     subtype="html")
_body, _full, _att = mailparse.extract(_ALT)
check_true("代替テキストだけの場合は HTML を使う", "こちらが本当の本文" in _body)

# ---------------------------------------------------------------- 会話キー


class _Cfg:
    """テスト用の最小限の設定。"""
    me = {"me@example.com", "me2@example.com"}
    force_people = []
    force_notices = []
    mute = []

    def is_me(self, a):
        return (a or "").lower() in self.me


_cfg = _Cfg()

check("件名ではなく参加者で会話を決める",
      mailparse.conversation(_cfg, "a@x.com", ["me@example.com"], [])[0], "a@x.com")
check("自分の別アドレス宛でも同じ会話になる",
      mailparse.conversation(_cfg, "a@x.com", ["me2@example.com"], [])[0], "a@x.com")
check("自分同士のやりとりは self",
      mailparse.conversation(_cfg, "me@example.com", ["me2@example.com"], [])[0], "self")
check("複数人は連結したキーになる",
      mailparse.conversation(_cfg, "a@x.com", ["me@example.com", "b@x.com"], [])[0],
      "a@x.com|b@x.com")

# ---------------------------------------------------------------- 人/お知らせ

check("やりとりが成立していれば人", store.classify(_cfg, "s@x.com", 3, 9, 9, 9), "people")
# 配信停止の依頼など、自分の発信しかない相手を人に入れない
# 実際には受信が無いので senders は空になる
check("配信停止の依頼だけの相手はお知らせ",
      store.classify(_cfg, "abuse@x.com", 2, 0, 0, 2, []), "notice")
check_true("VERP アドレスを機械と見なす",
           mailparse.robot_address("1axc0ij-mr+2ename=gmail.com@bf58x.example.net"))
check("人のアドレスは巻き込まない", mailparse.robot_address("taro.yamada@example.com"), False)
check("配信ヘッダが多数ならお知らせ", store.classify(_cfg, "a@x.com", 0, 5, 0, 8), "notice")
check("配信停止の導線が多数ならお知らせ", store.classify(_cfg, "a@x.com", 0, 0, 5, 8), "notice")
check("機械アドレスだけならお知らせ", store.classify(_cfg, "noreply@x.com", 0, 0, 0, 3), "notice")
check("ふつうの相手は人", store.classify(_cfg, "alice@x.com", 0, 0, 0, 3), "people")

check_true("noreply を機械と見なす", mailparse.robot_address("noreply@x.com"))
check_true("語の途中の info も拾う", mailparse.robot_address("shopinfo@x.com"))
check_true("配信用サブドメインを拾う", mailparse.robot_address("hello@mail.x.com"))
check("人のアドレスは機械扱いしない", mailparse.robot_address("hanako@example.com"), False)

_BULK = email.message_from_string(
    "From: a@x.com\nList-Unsubscribe: <https://x.com/u>\n\nhi\n")
check_true("List-Unsubscribe を一斉配信と判定", mailparse.is_bulk(_BULK))
check("ふつうのメールは一斉配信でない",
      mailparse.is_bulk(email.message_from_string("From: a@x.com\n\nhi\n")), False)
check_true("配信停止の文言を拾う", mailparse.is_promotional("……\n配信停止はこちら\n"))

# ---------------------------------------------------------------- 受け口の防御

# 127.0.0.1 で待つだけでは、ブラウザで開いた無関係なページから
# 要求を投げられてしまう。3つの検査すべてが要る。
_P, _T = 8765, "secret-token"


def _req(path="/api/status", host="127.0.0.1:8765",
         origin="http://127.0.0.1:8765", token=_T):
    return server.check_request(_P, _T, path, host, origin, token)


check("正規の画面からは通す", _req(), None)
check("コマンドライン（Origin 無し）は通す", _req(origin=None), None)
check("localhost 表記も通す",
      _req(host="localhost:8765", origin="http://localhost:8765"), None)

check("よそのサイトからの要求は断る", _req(origin="https://evil.example.com"), "origin")
check("DNSリバインディングは Host で断る",
      _req(host="evil.example.com:8765", origin="http://evil.example.com:8765"), "host")
check("Host が無ければ断る", _req(host=None), "host")
check("合言葉が無ければ api は断る", _req(token=None), "token")
check("合言葉が違えば断る", _req(token="wrong"), "token")
check("よそのポートを名乗っても断る", _req(host="127.0.0.1:9999"), "host")
check("画面ファイルは合言葉なしで出す", _req(path="/", token=None), None)
check("静的ファイルも合言葉なしで出す", _req(path="/static/app.js", token=None), None)

# ---------------------------------------------------------------- 結果

print("%d 件成功 / %d 件失敗" % (len(PASS), len(FAIL)))
if FAIL:
    print()
    for f in FAIL:
        print("  ✗ " + f)
    sys.exit(1)
print("すべて通りました。")
