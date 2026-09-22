"""SQLite への保存と読み出し。

メールは (アカウント, UID) で一意。同じメールが複数のアカウントに
届いている場合は dup=1 を立てて、表示側では1通として扱う。
"""

import json
import re
import sqlite3
import threading
from pathlib import Path

from . import mailparse

COLUMNS = """
    account     TEXT NOT NULL,
    uid         INTEGER NOT NULL,
    message_id  TEXT,
    conv_key    TEXT NOT NULL,
    ts          INTEGER NOT NULL,
    from_addr   TEXT,
    from_name   TEXT,
    to_json     TEXT,
    subject     TEXT,
    is_me       INTEGER NOT NULL DEFAULT 0,
    unread      INTEGER NOT NULL DEFAULT 0,
    is_direct   INTEGER NOT NULL DEFAULT 1,
    body        TEXT,
    body_full   TEXT,
    attachments TEXT,
    truncated   INTEGER NOT NULL DEFAULT 0,
    bulk        INTEGER NOT NULL DEFAULT 0,
    promo       INTEGER NOT NULL DEFAULT 0,
    dup         INTEGER NOT NULL DEFAULT 0,
    raw         BLOB,
    PRIMARY KEY (account, uid)
"""

# テーブルを先に作り、古い形式ならここで作り替えてから索引を張る。
# 索引を先に張ると、まだ account 列が無い状態で失敗する。
SCHEMA_TABLES = """
CREATE TABLE IF NOT EXISTS messages (%s);

CREATE TABLE IF NOT EXISTS names (
    addr TEXT PRIMARY KEY,
    name TEXT,
    ts   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS state (
    key   TEXT PRIMARY KEY,
    value TEXT
);
""" % COLUMNS

SCHEMA_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_conv_ts ON messages(conv_key, ts);
CREATE INDEX IF NOT EXISTS idx_ts      ON messages(ts);
CREATE INDEX IF NOT EXISTS idx_msgid   ON messages(message_id);
CREATE INDEX IF NOT EXISTS idx_account ON messages(account);
"""

FIELDS = ("account", "uid", "message_id", "conv_key", "ts", "from_addr",
          "from_name", "to_json", "subject", "is_me", "unread", "is_direct",
          "body", "body_full", "attachments", "truncated", "bulk", "promo",
          "raw")


class Database:
    """スレッドごとに接続を持つ SQLite。

    Python の sqlite3 は threadsafety=1、つまり**接続をスレッド間で
    共有してはいけない**。check_same_thread=False は Python 側の確認を
    外すだけで、SQLite 自体が安全になるわけではない。複数のスレッドが
    1つの接続を同時に触ると内部構造が壊れ、SQLITE_CORRUPT や
    segfault（sqlite3BtreeIndexMoveto での EXC_BAD_ACCESS）になる。

    ここを通す限り、どのスレッドも自分専用の接続しか触らない。
    ファイルは WAL なので、接続が複数あっても同時に読み書きできる。
    """

    def __init__(self, path, default_account=""):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._all = []
        self._lock = threading.Lock()

        conn = self._open()
        conn.executescript(SCHEMA_TABLES)
        _migrate(conn, default_account)
        conn.executescript(SCHEMA_INDEXES)
        conn.commit()

    def _open(self):
        conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        # 接続が増えるぶん、書き込みの順番待ちが起きる
        conn.execute("PRAGMA busy_timeout=15000")
        self._local.conn = conn
        with self._lock:
            self._all.append(conn)
        return conn

    @property
    def conn(self):
        conn = getattr(self._local, "conn", None)
        return conn if conn is not None else self._open()

    def execute(self, *args):
        return self.conn.execute(*args)

    def executescript(self, script):
        return self.conn.executescript(script)

    def commit(self):
        return self.conn.commit()

    def close(self):
        with self._lock:
            conns, self._all = self._all, []
        for conn in conns:
            try:
                conn.close()
            except Exception:
                pass
        self._local.conn = None


def connect(path, default_account=""):
    return Database(path, default_account)


def _migrate(conn, default_account):
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(messages)")}
    if "account" not in cols:
        _rebuild_with_accounts(conn, cols, default_account)
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(messages)")}
    for col, decl in (("truncated", "INTEGER NOT NULL DEFAULT 0"),
                      ("bulk", "INTEGER NOT NULL DEFAULT 0"),
                      ("promo", "INTEGER NOT NULL DEFAULT 0"),
                      ("dup", "INTEGER NOT NULL DEFAULT 0"),
                      ("raw", "BLOB")):
        if col not in cols:
            conn.execute("ALTER TABLE messages ADD COLUMN %s %s" % (col, decl))
    conn.commit()


def _rebuild_with_accounts(conn, old_cols, default_account):
    """単一アカウント時代のテーブルを (account, uid) 主キーに作り替える。"""
    carry = [c for c in old_cols if c in
             set(FIELDS) - {"account"} | {"dup"}]
    conn.executescript("CREATE TABLE messages_v2 (%s);" % COLUMNS)
    conn.execute(
        "INSERT INTO messages_v2 (account, %s) SELECT ?, %s FROM messages"
        % (", ".join(carry), ", ".join(carry)),
        (default_account,))
    conn.execute("DROP TABLE messages")
    conn.execute("ALTER TABLE messages_v2 RENAME TO messages")
    # 旧形式の同期状態はアカウント付きの鍵に移す
    for key in ("uidvalidity", "last_uid", "last_sync"):
        row = conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        if row:
            conn.execute("INSERT OR REPLACE INTO state(key,value) VALUES(?,?)",
                         ("%s:%s" % (key, default_account), row["value"]))
            conn.execute("DELETE FROM state WHERE key=?", (key,))
    conn.commit()


# -- state -----------------------------------------------------------------

def get_state(conn, key, account=None, default=None):
    if account:
        key = "%s:%s" % (key, account)
    row = conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_state(conn, key, value, account=None):
    if account:
        key = "%s:%s" % (key, account)
    conn.execute(
        "INSERT INTO state(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


def reset_messages(conn, account=None):
    if account:
        conn.execute("DELETE FROM messages WHERE account=?", (account,))
        conn.execute("DELETE FROM state WHERE key IN (?,?)",
                     ("uidvalidity:%s" % account, "last_uid:%s" % account))
    else:
        conn.execute("DELETE FROM messages")
        conn.execute("DELETE FROM state WHERE key LIKE 'uidvalidity%' "
                     "OR key LIKE 'last_uid%'")
    conn.commit()


# -- write -----------------------------------------------------------------

_INSERT = (
    "INSERT INTO messages (%s) VALUES (%s) "
    "ON CONFLICT(account, uid) DO UPDATE SET "
    "  unread=excluded.unread, conv_key=excluded.conv_key,"
    "  body=excluded.body, body_full=excluded.body_full,"
    "  bulk=excluded.bulk, promo=excluded.promo,"
    "  raw=COALESCE(excluded.raw, messages.raw)"
    % (", ".join(FIELDS), ", ".join(":" + f for f in FIELDS))
)


def upsert_message(conn, rec):
    conn.execute(_INSERT, rec)


def rewrite_body(conn, account, uid, rec):
    """保存済みの生データから作り直した内容で上書きする（通信しない）。"""
    params = {k: rec[k] for k in ("conv_key", "subject", "from_addr", "from_name",
                                  "to_json", "is_me", "is_direct", "body",
                                  "body_full", "attachments", "bulk", "promo")}
    params.update(account=account, uid=uid)
    conn.execute(
        """UPDATE messages SET conv_key=:conv_key, subject=:subject,
               from_addr=:from_addr, from_name=:from_name, to_json=:to_json,
               is_me=:is_me, is_direct=:is_direct, body=:body,
               body_full=:body_full, attachments=:attachments,
               bulk=:bulk, promo=:promo
           WHERE account=:account AND uid=:uid""", params)


def remember_name(conn, addr, name, ts):
    if not addr or not name:
        return
    conn.execute(
        "INSERT INTO names(addr,name,ts) VALUES(?,?,?) "
        "ON CONFLICT(addr) DO UPDATE SET name=excluded.name, ts=excluded.ts "
        "WHERE excluded.ts > names.ts",
        (addr, name, ts),
    )


def mark_duplicates(conn):
    """同じメールが複数アカウントに届いている場合、1通だけを表示対象にする。"""
    conn.execute("UPDATE messages SET dup=0 WHERE dup<>0")
    conn.execute("""
        UPDATE messages SET dup=1 WHERE rowid NOT IN (
            SELECT MIN(rowid) FROM messages
            GROUP BY COALESCE(NULLIF(message_id,''), account || ':' || uid)
        )""")
    conn.commit()
    return conn.execute("SELECT COUNT(*) c FROM messages WHERE dup=1").fetchone()["c"]


def raw_rows(conn):
    return conn.execute(
        "SELECT account, uid, raw, truncated FROM messages "
        "WHERE raw IS NOT NULL ORDER BY account, uid").fetchall()


# -- read ------------------------------------------------------------------

def _name_map(conn):
    return {r["addr"]: r["name"] for r in conn.execute("SELECT addr,name FROM names")}


def label_for(conv_key, names):
    if conv_key == "self":
        return "自分へのメモ"
    addrs = conv_key.split("|")
    shown = [names.get(a) or a.split("@")[0] for a in addrs]
    if len(shown) <= 2:
        return "、".join(shown)
    return "%s ほか%d人" % (shown[0], len(shown) - 1)


_DECOR = re.compile(r"[=_\-*~─-╿]{4,}")


def _snippet(text):
    """一覧用の1行要約。罫線だらけのメルマガでも中身が見えるようにする。"""
    text = _DECOR.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()[:120]


def classify(cfg, conv_key, mine, bulk_n, promo_n, total, senders=()):
    """会話を「人」か「お知らせ」に振り分ける。"""
    for pat in cfg.force_people:
        if pat in conv_key:
            return "people"
    for pat in cfg.force_notices:
        if pat in conv_key:
            return "notice"
    if not total:
        return "people"
    if 0 < mine < total:
        # やりとりが成立している相手は、アドレスが何であれ「人」。
        # 自分の発信しかない会話（配信停止の依頼など）は対象外にする。
        return "people"
    if bulk_n * 2 >= total:          # 一斉配信のヘッダを持つ
        return "notice"
    if promo_n * 2 >= total:         # 本文に配信停止の導線がある
        return "notice"
    who = senders or ([] if conv_key == "self" else conv_key.split("|"))
    if who and all(mailparse.robot_address(a) for a in who):
        return "notice"
    return "people"


def conversations(conn, cfg, query=None, kind=None, account=None):
    where = ["dup=0"]
    params = []
    if account:
        where.append("account=?")
        params.append(account)
    clause = " WHERE " + " AND ".join(where)

    rows = conn.execute("""
        SELECT conv_key,
               MAX(ts)  AS last_ts,
               COUNT(*) AS total,
               SUM(CASE WHEN unread=1 AND is_me=0 THEN 1 ELSE 0 END) AS unread,
               SUM(is_me) AS mine,
               SUM(bulk)  AS bulk_n,
               SUM(promo) AS promo_n,
               MAX(is_direct) AS is_direct,
               GROUP_CONCAT(DISTINCT account) AS accounts
        FROM messages""" + clause + """
        GROUP BY conv_key ORDER BY last_ts DESC""", params).fetchall()

    names = _name_map(conn)
    senders = {}
    for r in conn.execute("SELECT DISTINCT conv_key, from_addr FROM messages "
                          "WHERE dup=0 AND is_me=0 AND from_addr <> ''"):
        senders.setdefault(r["conv_key"], []).append(r["from_addr"])

    out = []
    q = (query or "").strip().lower()
    for r in rows:
        key = r["conv_key"]
        if any(m in key for m in cfg.mute):
            continue
        group = classify(cfg, key, r["mine"] or 0, r["bulk_n"] or 0,
                         r["promo_n"] or 0, r["total"], senders.get(key, []))
        if kind and group != kind:
            continue
        label = label_for(key, names)
        last = conn.execute(
            "SELECT body, is_me, subject FROM messages WHERE conv_key=? AND dup=0"
            + (" AND account=?" if account else "") +
            " ORDER BY ts DESC LIMIT 1",
            ([key, account] if account else [key])).fetchone()
        snippet = _snippet(last["body"] or last["subject"] or "")
        if q:
            hit = q in label.lower() or q in key.lower()
            if not hit:
                hit = conn.execute(
                    "SELECT 1 FROM messages WHERE conv_key=? AND dup=0 AND "
                    "(body LIKE ? OR subject LIKE ?) LIMIT 1",
                    (key, "%" + q + "%", "%" + q + "%")).fetchone() is not None
            if not hit:
                continue
        out.append({
            "key": key,
            "label": label,
            "addrs": [] if key == "self" else key.split("|"),
            "last_ts": r["last_ts"],
            "total": r["total"],
            "unread": r["unread"] or 0,
            "is_direct": bool(r["is_direct"]),
            "kind": group,
            "accounts": sorted((r["accounts"] or "").split(",")),
            "snippet": ("自分: " if last["is_me"] else "") + snippet,
        })
    return out


def messages(conn, conv_key, limit=400, before=None, account=None):
    params = [conv_key]
    clause = ""
    if account:
        clause += " AND account=?"
        params.append(account)
    if before:
        clause += " AND ts < ?"
        params.append(int(before))
    params.append(int(limit))
    rows = conn.execute(
        "SELECT * FROM messages WHERE conv_key=? AND dup=0" + clause +
        " ORDER BY ts DESC LIMIT ?", params).fetchall()
    names = _name_map(conn)
    out = []
    for r in reversed(rows):
        body = r["body"] or ""
        full = r["body_full"] or ""
        out.append({
            "uid": r["uid"],
            "account": r["account"],
            "ts": r["ts"],
            "is_me": bool(r["is_me"]),
            "unread": bool(r["unread"]),
            "from_addr": r["from_addr"],
            "from_name": r["from_name"] or names.get(r["from_addr"] or "") or "",
            "subject": r["subject"] or "",
            "body": body,
            "has_more": full.strip() != body.strip() and bool(full.strip()),
            "attachments": json.loads(r["attachments"] or "[]"),
        })
    return out


def message_full(conn, account, uid):
    row = conn.execute(
        "SELECT body_full, to_json, subject, from_addr FROM messages "
        "WHERE account=? AND uid=?", (account, uid)).fetchone()
    if not row:
        return None
    return {
        "body_full": row["body_full"] or "",
        "to": json.loads(row["to_json"] or "[]"),
        "subject": row["subject"] or "",
        "from_addr": row["from_addr"] or "",
    }


def stats(conn, cfg=None):
    row = conn.execute(
        "SELECT COUNT(*) n, MIN(ts) a, MAX(ts) b FROM messages WHERE dup=0").fetchone()
    per = {}
    for r in conn.execute("SELECT account, COUNT(*) c, "
                          "SUM(CASE WHEN unread=1 AND is_me=0 THEN 1 ELSE 0 END) u "
                          "FROM messages WHERE dup=0 GROUP BY account"):
        per[r["account"]] = {"messages": r["c"], "unread": r["u"] or 0}
    out = {
        "messages": row["n"] or 0,
        "oldest": row["a"] or 0,
        "newest": row["b"] or 0,
        "duplicates": conn.execute(
            "SELECT COUNT(*) c FROM messages WHERE dup=1").fetchone()["c"],
        "conversations": conn.execute(
            "SELECT COUNT(DISTINCT conv_key) c FROM messages WHERE dup=0").fetchone()["c"],
        "per_account": per,
    }
    if cfg:
        out["last_sync"] = max(
            [int(get_state(conn, "last_sync", a.email, 0) or 0) for a in cfg.accounts]
            or [0])
    return out
