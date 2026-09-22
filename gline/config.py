"""設定とアプリパスワードの読み込み。

パスワードはこのリポジトリには一切保存しません。macOS キーチェーン
（または環境変数）から、アカウントごとに読み出します。
"""

import json
import os
import re
import sys
from pathlib import Path

from . import secrets

ROOT = Path(__file__).resolve().parent.parent
DEFAULTS = {
    "imap_host": "imap.gmail.com",
    "imap_port": 993,
    "initial_days": 90,
    "strip_patterns": [],
    "force_people": [],
    "force_notices": [],
    "mute": [],
    "port": 8765,
    "db_path": "data/mail.db",
    "max_fetch_bytes": 5000000,
}


class ConfigError(Exception):
    pass


def _gmail_canonical(addr):
    """gmail.com のみ: ドットを除去し +tag を落とした正規形を返す。"""
    if "@" not in addr:
        return addr
    local, _, domain = addr.partition("@")
    if domain not in ("gmail.com", "googlemail.com"):
        return addr
    return local.split("+", 1)[0].replace(".", "") + "@gmail.com"


class Account:
    """同期するメールボックス1つ。"""

    def __init__(self, email, aliases=(), label=None):
        self.email = email.strip().lower()
        if "@" not in self.email:
            raise ConfigError("アカウントのアドレスが不正です: %r" % email)
        self.label = (label or self.email.split("@")[0]).strip()
        addrs = {self.email}
        for a in aliases:
            a = a.strip().lower()
            if a:
                addrs.add(a)
        addrs |= {_gmail_canonical(a) for a in list(addrs)}
        self.addrs = addrs

    def __repr__(self):
        return "<Account %s>" % self.email


class Config:
    def __init__(self, data):
        self.accounts = [
            Account(a["email"], a.get("aliases", []), a.get("label"))
            for a in data["accounts"]
        ]
        if not self.accounts:
            raise ConfigError("config.json に accounts が1つもありません。")
        seen = set()
        for acc in self.accounts:
            if acc.email in seen:
                raise ConfigError("同じアドレスが2回書かれています: %s" % acc.email)
            seen.add(acc.email)

        # 自分のアドレス全部。どのアカウントのものでも「自分＝右側」にする。
        self.me = set()
        for acc in self.accounts:
            self.me |= acc.addrs

        self.imap_host = data["imap_host"]
        self.imap_port = int(data["imap_port"])
        self.initial_days = int(data["initial_days"])
        self.port = int(data["port"])
        self.db_path = ROOT / data["db_path"]
        self.max_fetch_bytes = int(data["max_fetch_bytes"])
        self.mute = [m.strip().lower() for m in data["mute"] if m.strip()]
        self.force_people = [m.strip().lower() for m in data["force_people"] if m.strip()]
        self.force_notices = [m.strip().lower() for m in data["force_notices"] if m.strip()]

        self.strip_patterns = []
        for p in data["strip_patterns"]:
            try:
                self.strip_patterns.append(re.compile(p, re.M | re.S))
            except re.error as exc:
                raise ConfigError("strip_patterns の正規表現が不正です: %r (%s)" % (p, exc))

    @property
    def primary(self):
        return self.accounts[0]

    def account(self, email):
        for acc in self.accounts:
            if acc.email == email:
                return acc
        return None

    def is_me(self, addr):
        if not addr:
            return False
        addr = addr.strip().lower()
        return addr in self.me or _gmail_canonical(addr) in self.me


def _strip_comment_keys(data):
    return {k: v for k, v in data.items() if not k.startswith("//")}


def app_config_path():
    """Fukidashi.app が使う設定ファイルの場所。"""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Fukidashi" / "config.json"
    if os.name == "nt":
        base = os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming")
        return Path(base) / "Fukidashi" / "config.json"
    return Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")) / "fukidashi" / "config.json"


def load(path=None):
    if path is None:
        path = os.environ.get("GMAIL_LINE_CONFIG")
    if path is None:
        # 手元に config.json が無ければ、アプリ側の設定を使う。
        # アプリで読み、たまにコマンドで手入れする、という使い方のため。
        local = ROOT / "config.json"
        path = local if local.exists() else app_config_path()
    path = Path(path)
    if not path.exists():
        raise ConfigError(
            "設定ファイルが見つかりません: %s\n\n"
            "  python3 setup.py\n\n"
            "を実行するか、config.example.json を config.json に複製して\n"
            "accounts を書き換えてください。" % path
        )
    with path.open(encoding="utf-8") as fh:
        raw = _strip_comment_keys(json.load(fh))

    # 単一アカウント形式（email / aliases）もそのまま読めるようにする
    if "accounts" not in raw:
        if not raw.get("email"):
            raise ConfigError("config.json に accounts も email もありません。")
        raw["accounts"] = [{
            "email": raw["email"],
            "aliases": raw.get("aliases", []),
            "label": raw.get("label"),
        }]

    merged = dict(DEFAULTS)
    merged.update(raw)
    return Config(merged)


def app_password(account):
    """アカウントのアプリパスワードを取得する。無ければ作り方を案内する。"""
    try:
        value = secrets.get(account.email)
    except secrets.SecretError as exc:
        raise ConfigError(str(exc))
    if value:
        return value

    raise ConfigError(
        "%s のアプリパスワードが保存されていません。\n\n"
        "  python3 setup.py\n\n"
        "を実行すると、作り方から保存まで順に案内します。\n"
        "自分で保存する場合は次のコマンドです:\n\n"
        "%s\n" % (account.email, secrets.manual_instructions(account.email))
    )


def die(msg):
    sys.stderr.write("\n%s\n\n" % msg)
    raise SystemExit(1)
