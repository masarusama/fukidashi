"""127.0.0.1 だけに待ち受けるローカル HTTP サーバ。"""

import json
import mimetypes
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import config, imapsync, store

def _web_dir():
    """画面ファイルの場所。PyInstaller で固めた .exe の中も探す。"""
    bundled = getattr(sys, "_MEIPASS", None)
    if bundled:
        packed = Path(bundled) / "gline" / "web"
        if packed.is_dir():
            return packed
    return Path(__file__).resolve().parent / "web"


WEB = _web_dir()


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


def make_handler(cfg, db, runner, control):

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
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj, ensure_ascii=False), )

        def _static(self, name):
            path = (WEB / name).resolve()
            if WEB not in path.parents or not path.is_file():
                return self._send(404, "not found", "text/plain; charset=utf-8")
            ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype.endswith("javascript"):
                ctype += "; charset=utf-8"
            self._send(200, path.read_bytes(), ctype)

        # -- ルーティング -------------------------------------------------
        def do_GET(self):
            try:
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
    httpd = ThreadingHTTPServer(("127.0.0.1", cfg.port),
                                make_handler(cfg, db, runner, control))
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
