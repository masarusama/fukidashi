"""返信を組み立てて送る。

読む側と違い、ここでの間違いは取り消せない。宛先と差出人を決める
根拠を1か所にまとめ、画面にそのまま出せる形で返す。
"""

import email
import smtplib
import ssl
import zlib
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

from . import mailparse

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587


class SendError(Exception):
    pass


def _subject_for_reply(subject):
    base = (subject or "").strip()
    stripped = base
    for _ in range(6):
        low = stripped.lower()
        if low.startswith("re:"):
            stripped = stripped[3:].strip()
        else:
            break
    if not stripped:
        return "Re:"
    return "Re: " + stripped


def reply_context(conn, cfg, conv_key):
    """この会話に返信するとき、誰から誰へ何を送るのかを決める。

    画面にそのまま出して確認してもらうための情報を返す。
    """
    if conv_key == "self":
        raise SendError("自分へのメモには返信できません。")

    def _last(only_incoming):
        return conn.execute(
            "SELECT account, message_id, subject, raw, to_json, from_addr, is_me "
            "FROM messages WHERE conv_key=? AND dup=0" +
            (" AND is_me=0" if only_incoming else "") +
            " ORDER BY ts DESC LIMIT 1", (conv_key,)).fetchone()

    # 受信したメールがあればそれに返信する。まだ返事が来ていない相手には、
    # 自分が最後に送ったメールに続ける形で送れるようにする。
    row = _last(True) or _last(False)
    if row is None:
        raise SendError("この会話にはメールがありません。")
    follow_up = bool(row["is_me"])

    account = cfg.account(row["account"])
    if account is None:
        raise SendError("このメールを受け取ったアカウントが設定にありません: %s"
                        % row["account"])

    to_addrs = [a for a in conv_key.split("|") if a and not cfg.is_me(a)]
    if not to_addrs:
        raise SendError("宛先を決められませんでした。")

    references = None
    if row["raw"]:
        try:
            orig = email.message_from_bytes(zlib.decompress(row["raw"]))
            references = mailparse.header_text(orig.get("References", "")) or None
        except Exception:
            references = None

    # 別アドレス宛に届いたメールへの返信は、Gmail が差出人を
    # アカウントのアドレスに書き換える。黙って変わると事故のもとなので伝える。
    received_at = None
    try:
        import json
        for a in json.loads(row["to_json"] or "[]"):
            if cfg.is_me(a) and a.lower() != account.email:
                received_at = a
                break
    except Exception:
        pass

    names = {r["addr"]: r["name"] for r in
             conn.execute("SELECT addr,name FROM names")}

    return {
        "conv_key": conv_key,
        "account": account.email,
        "account_label": account.label,
        "to": to_addrs,
        "to_names": [names.get(a) or a for a in to_addrs],
        "subject": _subject_for_reply(row["subject"]),
        "in_reply_to": row["message_id"] or None,
        "references": references,
        "received_at": received_at,
        "follow_up": follow_up,
    }


def build(account, ctx, text):
    if not (text or "").strip():
        raise SendError("本文が空です。")

    msg = EmailMessage()
    msg["From"] = formataddr((account.label, account.email))
    msg["To"] = ", ".join(ctx["to"])
    msg["Subject"] = ctx["subject"]
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=account.email.split("@")[1])

    # 相手の受信箱で返信として繋がるようにする
    if ctx.get("in_reply_to"):
        msg["In-Reply-To"] = ctx["in_reply_to"]
        refs = " ".join(x for x in (ctx.get("references"), ctx["in_reply_to"]) if x)
        msg["References"] = refs

    msg.set_content(text.rstrip() + "\n")
    return msg


def send(account, password, msg):
    try:
        ctx = ssl.create_default_context()
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=60) as smtp:
            smtp.ehlo()
            smtp.starttls(context=ctx)
            smtp.ehlo()
            smtp.login(account.email, password)
            smtp.send_message(msg)
    except smtplib.SMTPAuthenticationError:
        raise SendError("Gmail にログインできませんでした。"
                        "アプリパスワードを確認してください。")
    except smtplib.SMTPRecipientsRefused as exc:
        raise SendError("宛先が受け付けられませんでした: %s" % exc.recipients)
    except (smtplib.SMTPException, OSError) as exc:
        raise SendError("送信に失敗しました: %s" % exc)
    return msg["Message-ID"]
