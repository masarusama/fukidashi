"""127.0.0.1 だけに待ち受けるローカル HTTP サーバ。"""

import json
import mimetypes
import secrets as _secrets
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import config, imapsync, smtpsend, store

def _web_dir():
    """画面ファイルの場所。PyInstaller で固めた .exe の中も探す。"""
    bundled = getattr(sys, "_MEIPASS", None)
    if bundled:
        packed = Path(bundled) / "gline" / "web"
        if packed.is_dir():
            return packed
    return Path(__file__).resolve().parent / "web"


WEB = _web_dir()

TOKEN_HEADER = "X-Fukidashi-Token"
TOKEN_META = "__FUKIDASHI_TOKEN__"


def new_token():
    return _secrets.token_urlsafe(32)


def allowed_origins(port):
    return {"http://127.0.0.1:%d" % port,
            "http://localhost:%d" % port,
            "http://[::1]:%d" % port}


def allowed_hosts(port):
    return {"127.0.0.1:%d" % port, "localhost:%d" % port, "[::1]:%d" % port}


def check_request(port, token, path, host, origin, sent_token):
    """要求を受けてよいか判断する。断る理由を返す（問題なければ None）。

    127.0.0.1 で待ち受けているだけでは守れない。ブラウザで開いた
    まったく無関係なページが、ここへ要求を投げられてしまうため。

      1. Host の検証 — DNS リバインディング対策。攻撃者が自分のドメインを
         127.0.0.1 に向けると、ブラウザからは同一オリジンに見えてしまい、
         Origin の検証をすり抜けてメールの中身まで読まれる。待ち受け名が
         127.0.0.1/localhost でなければ断る。
      2. Origin の検証 — 通常の CSRF 対策。よそのページからの要求を断る。
      3. 合言葉 — /api/ には独自ヘッダを必須にする。独自ヘッダが付くと
         ブラウザは事前確認（preflight）を必ず行い、こちらは許可を返さない
         ので、よそのページからの要求はそもそも届かない。
    """
    if host is None or host.strip().lower() not in allowed_hosts(port):
        return "host"
    if origin is not None and origin.strip() not in allowed_origins(port):
        return "origin"
    if path.startswith("/api/") and sent_token != token:
        return "token"
    return None


class SyncRunner:
    """同期を1本だけ走らせ、進捗を持っておく。"""

    def __init__(self, cfg):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.thread = None
        self.lines = []
        self.error = None
        self.finished_at = 0

    @property
    def running(self):
        return self.thread is not None and self.thread.is_alive()

    def start(self):
        with self.lock:
            if self.running:
                return False
            self.lines = []
            self.error = None
            self.thread = threading.Thread(target=self._run, daemon=True)
            self.thread.start()
            return True

    def _log(self, msg):
        with self.lock:
            self.lines.append(msg)
            del self.lines[:-40]

    def _run(self):
        try:
            passwords = {a.email: config.app_password(a) for a in self.cfg.accounts}
            n = imapsync.sync_all(self.cfg, passwords, progress=self._log)
            self._log("完了: %d 件を取り込みました。" % n)
        except Exception as exc:
            self.error = str(exc)
            self._log("エラー: %s" % exc)
            traceback.print_exc()
        finally:
            self.finished_at = int(time.time())

    def state(self):
        with self.lock:
            return {
                "running": self.running,
                "lines": list(self.lines),
                "error": self.error,
                "finished_at": self.finished_at,
            }


UNDO_SECONDS = 8


class Outbox:
    """送信を少し待ってから実行する。

    送った瞬間に取り返しがつかなくなるのを避けるための猶予。
    画面を閉じても予定どおり送られる（取り消しは明示的な操作だけ）。
    """

    def __init__(self, cfg, db, on_sent=None):
        self.cfg = cfg
        self.db = db
        self.on_sent = on_sent
        self.lock = threading.Lock()
        self.items = {}

    def queue(self, conv_key, text):
        ctx = smtpsend.reply_context(self.db, self.cfg, conv_key)
        account = self.cfg.account(ctx["account"])
        msg = smtpsend.build(account, ctx, text)

        ident = _secrets.token_urlsafe(12)
        timer = threading.Timer(UNDO_SECONDS, self._fire, args=(ident,))
        with self.lock:
            self.items[ident] = {
                "id": ident, "state": "waiting", "ctx": ctx, "msg": msg,
                "account": account, "error": None,
                "at": time.time() + UNDO_SECONDS,
            }
        timer.daemon = True
        timer.start()
        return {"id": ident, "undo_seconds": UNDO_SECONDS,
                "to": ctx["to_names"], "from": ctx["account"],
                "subject": ctx["subject"]}

    def cancel(self, ident):
        with self.lock:
            item = self.items.get(ident)
            if item is None:
                return False
            if item["state"] != "waiting":
                return False
            item["state"] = "cancelled"
            return True

    def _fire(self, ident):
        with self.lock:
            item = self.items.get(ident)
            if item is None or item["state"] != "waiting":
                return
            item["state"] = "sending"
        try:
            password = config.app_password(item["account"])
            smtpsend.send(item["account"], password, item["msg"])
            item["state"] = "sent"
            if self.on_sent:
                try:
                    self.on_sent(item["account"])
                except Exception:
                    traceback.print_exc()
        except Exception as exc:
            item["state"] = "failed"
            item["error"] = str(exc)
            traceback.print_exc()

    def state(self, ident):
        with self.lock:
            item = self.items.get(ident)
            if item is None:
                return None
            return {"id": ident, "state": item["state"], "error": item["error"]}


def make_handler(cfg, db, runner, control, token, outbox):

    class Handler(BaseHTTPRequestHandler):
        server_version = "gmail_line"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            pass  # アクセスログは黙らせる

        # -- 返信ヘルパ ---------------------------------------------------
        def _send(self, code, body, ctype="application/json; charset=utf-8"):
            if isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj, ensure_ascii=False), )

        def _guard(self):
            """通してよい要求か確かめる。駄目なら 403 を返して True。"""
            reason = check_request(
                cfg.port, token, urlparse(self.path).path,
                self.headers.get("Host"), self.headers.get("Origin"),
                self.headers.get(TOKEN_HEADER))
            if reason is None:
                return False
            self._send(403, json.dumps({"error": "refused:" + reason},
                                       ensure_ascii=False))
            return True

        def _static(self, name):
            path = (WEB / name).resolve()
            if WEB not in path.parents or not path.is_file():
                return self._send(404, "not found", "text/plain; charset=utf-8")
            ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype.endswith("javascript"):
                ctype += "; charset=utf-8"
            body = path.read_bytes()
            if name == "index.html":
                # 合言葉は、このサーバが返す画面にだけ埋め込む
                body = body.replace(TOKEN_META.encode(), token.encode())
            self._send(200, body, ctype)

        # -- ルーティング -------------------------------------------------
        def do_GET(self):
            try:
                if self._guard():
                    return
                self._route()
            except BrokenPipeError:
                pass
            except Exception as exc:
                traceback.print_exc()
                self._json({"error": str(exc)}, 500)

        do_HEAD = do_GET

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            if self._guard():
                return
            path = urlparse(self.path).path
            if path == "/api/sync":
                started = runner.start()
                return self._json({"started": started, **runner.state()})
            if path == "/api/quit":
                self._json({"quitting": True})
                httpd = control.get("httpd")
                if httpd is not None:
                    # 応答を返しきってから止める
                    threading.Timer(0.3, httpd.shutdown).start()
                return
            self._json({"error": "not found"}, 404)

        def _route(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            one = lambda k, d=None: (q.get(k) or [d])[0]

            if u.path in ("/", "/index.html"):
                return self._static("index.html")
            if u.path.startswith("/static/"):
                return self._static(u.path[len("/static/"):])

            if u.path == "/api/status":
                return self._json({
                    "accounts": [{"email": a.email, "label": a.label}
                                 for a in cfg.accounts],
                    "stats": store.stats(db, cfg),
                    "sync": runner.state(),
                })
            if u.path == "/api/conversations":
                return self._json({
                    "conversations": store.conversations(
                        db, cfg, one("q"), one("kind"), one("account")),
                })
            if u.path == "/api/messages":
                key = one("key")
                if not key:
                    return self._json({"error": "key が必要です"}, 400)
                before = one("before")
                return self._json({
                    "messages": store.messages(
                        db, key, int(one("limit", 400)),
                        int(before) if before else None, one("account")),
                })
            if u.path == "/api/reply_context":
                key = one("key")
                if not key:
                    return self._json({"error": "key が必要です"}, 400)
                try:
                    ctx = smtpsend.reply_context(db, cfg, key)
                except smtpsend.SendError as exc:
                    return self._json({"error": str(exc)}, 400)
                return self._json({k: v for k, v in ctx.items()
                                   if k != "references"})
            if u.path == "/api/send/state":
                st = outbox.state(one("id") or "")
                if st is None:
                    return self._json({"error": "見つかりません"}, 404)
                return self._json(st)
            if u.path == "/api/full":
                uid, acct = one("uid"), one("account")
                data = store.message_full(db, acct, int(uid)) if uid and acct else None
                if data is None:
                    return self._json({"error": "見つかりません"}, 404)
                return self._json(data)

            self._send(404, "not found", "text/plain; charset=utf-8")

    return Handler


def serve(cfg):
    db = store.connect(cfg.db_path, cfg.primary.email)
    runner = SyncRunner(cfg)
    control = {}
    token = new_token()

    def after_sent(account):
        # 送信したものは Gmail の「送信済み」に入るので、少し待って取り込む
        threading.Timer(6.0, runner.start).start()

    outbox = Outbox(cfg, db, on_sent=after_sent)
    httpd = ThreadingHTTPServer(
        ("127.0.0.1", cfg.port),
        make_handler(cfg, db, runner, control, token, outbox))
    httpd.daemon_threads = True
    control["httpd"] = httpd
    httpd.gline_db = db
    return httpd


def shutdown(httpd):
    """待ち受けを止め、DB を安全に閉じる。

    書き込み途中で落とされると SQLite が中途半端な状態で残り、次の起動で
    「database disk image is malformed」になることがある。終了経路は必ずここを通す。
    """
    try:
        httpd.server_close()
    except Exception:
        pass
    db = getattr(httpd, "gline_db", None)
    if db is None:
        return
    try:
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        db.commit()
    except Exception:
        pass
    try:
        db.close()
    except Exception:
        pass
