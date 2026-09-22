"""Gmail の IMAP から取得して SQLite に流し込む。

読む専用なので BODY.PEEK[] しか使わない。同期しても未読が既読に
変わることはなく、サーバ側の状態は一切書き換えない。
"""

import email
import imaplib
import zlib
import json
import re
import ssl
import sys
import time
from datetime import datetime, timedelta

from . import mailparse, store

imaplib._MAXLINE = 10_000_000

LIST_RE = re.compile(rb'^\((?P<flags>[^)]*)\)\s+"(?P<delim>[^"]*)"\s+(?P<name>.+)$')
UID_RE = re.compile(rb"UID\s+(\d+)")
FLAGS_RE = re.compile(rb"FLAGS\s+\(([^)]*)\)")
SIZE_RE = re.compile(rb"RFC822\.SIZE\s+(\d+)")

CHUNK = 40


def log(msg):
    sys.stdout.write(msg + "\n")
    sys.stdout.flush()


def connect(cfg, account, password):
    ctx = ssl.create_default_context()
    conn = imaplib.IMAP4_SSL(cfg.imap_host, cfg.imap_port, ssl_context=ctx, timeout=120)
    conn.login(account.email, password)
    return conn


def find_all_mail(conn):
    """SPECIAL-USE の \\All を持つメールボックス名を返す（言語設定に依存しない）。"""
    typ, data = conn.list()
    if typ != "OK":
        raise RuntimeError("LIST に失敗しました: %s" % typ)
    fallback = None
    for line in data:
        if not isinstance(line, bytes):
            continue
        m = LIST_RE.match(line.strip())
        if not m:
            continue
        flags = m.group("flags").lower()
        name = m.group("name").decode("utf-8", "replace").strip()
        if b"\\all" in flags:
            return name
        if b"\\noselect" not in flags and name.strip('"').upper() == "INBOX":
            fallback = name
    if fallback:
        log("警告: 「すべてのメール」が見つからないため INBOX を使います（送信メールは入りません）。")
        return fallback
    raise RuntimeError("同期対象のメールボックスが見つかりませんでした。")


def _search_uids(conn, cfg, last_uid):
    if last_uid:
        typ, data = conn.uid("SEARCH", None, "UID", "%d:*" % (last_uid + 1))
    else:
        since = (datetime.now() - timedelta(days=cfg.initial_days)).strftime("%d-%b-%Y")
        typ, data = conn.uid("SEARCH", None, "SINCE", since)
    if typ != "OK":
        raise RuntimeError("SEARCH に失敗しました: %s" % typ)
    uids = [int(x) for x in (data[0] or b"").split()]
    return [u for u in sorted(uids) if u > last_uid]


def _parse_fetch(data):
    """imaplib の FETCH 応答を {uid: {...}} にほどく。"""
    out = {}
    for item in data:
        if not isinstance(item, tuple) or len(item) < 2:
            continue
        head, payload = item[0], item[1]
        m = UID_RE.search(head)
        if not m:
            continue
        uid = int(m.group(1))
        fm = FLAGS_RE.search(head)
        flags = (fm.group(1).decode("ascii", "replace") if fm else "")
        try:
            tt = imaplib.Internaldate2tuple(head)
            internal = int(time.mktime(tt)) if tt else 0
        except Exception:
            internal = 0
        out[uid] = {"raw": payload, "flags": flags, "internal": internal}
    return out


def _fetch_sizes(conn, uids):
    sizes = {}
    for i in range(0, len(uids), 500):
        batch = uids[i:i + 500]
        typ, data = conn.uid("FETCH", ",".join(map(str, batch)), "(UID RFC822.SIZE)")
        if typ != "OK":
            continue
        for line in data:
            blob = line[0] if isinstance(line, tuple) else line
            if not isinstance(blob, bytes):
                continue
            mu, ms = UID_RE.search(blob), SIZE_RE.search(blob)
            if mu and ms:
                sizes[int(mu.group(1))] = int(ms.group(1))
    return sizes


def build_record(cfg, account, uid, raw, flags, internal, keep_raw=True):
    msg = email.message_from_bytes(raw)

    from_list = mailparse.addresses(msg, "From")
    from_name, from_addr = from_list[0] if from_list else ("", "")
    to_list = mailparse.addresses(msg, "To")
    cc_list = mailparse.addresses(msg, "Cc")

    to_addrs = [a for _, a in to_list]
    cc_addrs = [a for _, a in cc_list]

    conv_key, is_direct = mailparse.conversation(cfg, from_addr, to_addrs, cc_addrs)

    body, full, attachments = mailparse.extract(msg)
    body = mailparse.strip_quotes(body, cfg.strip_patterns)

    ts = mailparse.message_date(msg, internal)
    is_me = cfg.is_me(from_addr)

    rec = {
        "account": account.email,
        "uid": uid,
        "message_id": mailparse.header_text(msg.get("Message-ID", "")) or None,
        "conv_key": conv_key,
        "ts": ts,
        "from_addr": from_addr,
        "from_name": from_name,
        "to_json": json.dumps([a for _, a in to_list + cc_list], ensure_ascii=False),
        "subject": mailparse.header_text(msg.get("Subject", "")),
        "is_me": 1 if is_me else 0,
        "unread": 0 if "\\Seen" in flags else 1,
        "is_direct": 1 if is_direct else 0,
        "body": body,
        "body_full": full,
        "attachments": json.dumps(attachments, ensure_ascii=False),
        "bulk": 1 if mailparse.is_bulk(msg) else 0,
        "promo": 1 if mailparse.is_promotional(full) else 0,
        "truncated": 0,
        # 生データを持っておくと、解析の手直しのたびに取り直さずに済む
        "raw": zlib.compress(raw, 6) if keep_raw else None,
    }
    names = [(a, n, ts) for n, a in from_list + to_list + cc_list if n]
    return rec, names


def _placeholder_record(cfg, account, uid, header_raw, flags, internal, size):
    rec, names = build_record(cfg, account, uid, header_raw, flags, internal)
    rec["body"] = "（このメールは %.1f MB あるため本文を取得していません。Gmail で開いてください）" % (size / 1_000_000)
    rec["body_full"] = rec["body"]
    rec["truncated"] = 1
    return rec, names


def sync(cfg, account, password, conn_db=None, progress=log):
    db = conn_db or store.connect(cfg.db_path, cfg.primary.email)
    imap = connect(cfg, account, password)
    added = 0
    try:
        mailbox = find_all_mail(imap)
        progress("メールボックス: %s" % mailbox)
        typ, _ = imap.select(mailbox, readonly=True)
        if typ != "OK":
            raise RuntimeError("SELECT に失敗しました: %s" % mailbox)

        uidvalidity = (imap.response("UIDVALIDITY")[1] or [b"0"])[0]
        uidvalidity = int(uidvalidity or 0)
        known = store.get_state(db, "uidvalidity", account.email)
        if known is not None and int(known) != uidvalidity:
            progress("UIDVALIDITY が変わったので取り直します。")
            store.reset_messages(db, account.email)
        store.set_state(db, "uidvalidity", uidvalidity, account.email)

        last_uid = int(store.get_state(db, "last_uid", account.email, 0) or 0)
        uids = _search_uids(imap, cfg, last_uid)
        if not uids:
            progress("新しいメールはありません。")
            store.set_state(db, "last_sync", int(time.time()), account.email)
            db.commit()
            return 0

        progress("%d 件を取得します…" % len(uids))
        sizes = _fetch_sizes(imap, uids)

        for i in range(0, len(uids), CHUNK):
            batch = uids[i:i + CHUNK]
            bigset = {u for u in batch if sizes.get(u, 0) > cfg.max_fetch_bytes}
            big = sorted(bigset)
            small = [u for u in batch if u not in bigset]

            for group, spec in ((small, "(UID FLAGS INTERNALDATE BODY.PEEK[])"),
                                (big, "(UID FLAGS INTERNALDATE BODY.PEEK[HEADER])")):
                if not group:
                    continue
                typ, data = imap.uid("FETCH", ",".join(map(str, group)), spec)
                if typ != "OK":
                    progress("  取得に失敗（スキップ）: %s" % group)
                    continue
                for uid, item in _parse_fetch(data).items():
                    try:
                        if uid in bigset:
                            rec, names = _placeholder_record(
                                cfg, account, uid, item["raw"], item["flags"],
                                item["internal"], sizes.get(uid, 0))
                        else:
                            rec, names = build_record(
                                cfg, account, uid, item["raw"], item["flags"],
                                item["internal"])
                        store.upsert_message(db, rec)
                        for addr, name, ts in names:
                            store.remember_name(db, addr, name, ts)
                        added += 1
                    except Exception as exc:
                        progress("  UID %s の解析に失敗（スキップ）: %s" % (uid, exc))

            store.set_state(db, "last_uid", max(batch), account.email)
            db.commit()
            progress("  %d / %d" % (min(i + CHUNK, len(uids)), len(uids)))

        store.set_state(db, "last_sync", int(time.time()), account.email)
        db.commit()
        return added
    finally:
        try:
            imap.logout()
        except Exception:
            pass
        if conn_db is None:
            db.close()


def sync_all(cfg, passwords, conn_db=None, progress=log):
    """全アカウントを順に同期し、取り込んだ通数を返す。"""
    db = conn_db or store.connect(cfg.db_path, cfg.primary.email)
    total = 0
    try:
        for account in cfg.accounts:
            progress("── %s ──" % account.label)
            total += sync(cfg, account, passwords[account.email],
                          conn_db=db, progress=progress)
        dups = store.mark_duplicates(db)
        if dups:
            progress("複数アカウントに重複していたメール: %d 通（1通として表示します）" % dups)
    finally:
        if conn_db is None:
            db.close()
    return total
