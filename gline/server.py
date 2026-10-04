"""127.0.0.1 だけに待ち受けるローカル HTTP サーバ。"""

import hmac
import ipaddress
import json
import mimetypes
import secrets as _secrets
import socket
import sys
import threading
import time
import traceback
from html import escape
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import ai, config, imapsync, qr, smtpsend, store

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


SESSION_COOKIE = "fk_session"

# 鍵を持たない端末にも見せてよいファイル。アイコンだけで、秘密は含まない。
PUBLIC_STATIC = frozenset({"/static/icon-192.png", "/static/apple-touch-icon.png"})


def lan_ip():
    """この機械が外へ出るときに使う IP アドレス。

    UDP の connect は相手を決めるだけで、実際には何も送らない。
    """
    for target in ("10.255.255.255", "8.8.8.8"):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect((target, 1))
            return sock.getsockname()[0]
        except OSError:
            continue
        finally:
            sock.close()
    return None


def acceptable_ip(ip):
    """家庭や職場の内側のアドレスだけを許す。

    インターネットから直接届くアドレス（グローバル IP）に待ち受けを開くと、
    世界中から見えてしまう。ループバックや未指定も、スマホからは使えない。
    """
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return not (addr.is_global or addr.is_loopback
                or addr.is_unspecified or addr.is_multicast)


def check_lan_request(expected_host, token, path, host, origin, sent_token, paired):
    """LAN 側で、要求を受けてよいか判断する。断る理由を返す（問題なければ None）。

    LAN 側は同じネットワークの誰からでも届くので、手前の守りより厳しくする。

      1. Host が待ち受けのアドレスそのものであること。別の名前で来た要求
         （DNS リバインディング）は断る。
      2. Origin があれば、自分自身であること。
      3. ペアリング済みの端末（鍵の Cookie を持つ端末）だけを通す。
         鍵が無ければ画面も合言葉も渡さない。
      4. /api/ には、さらに合言葉の独自ヘッダを要る。
    """
    if host is None or host.strip().lower() != expected_host:
        return "host"
    if origin is not None and origin.strip() != "http://" + expected_host:
        return "origin"
    if path == "/pair" or path in PUBLIC_STATIC:
        return None
    if not paired:
        return "unpaired"
    if path.startswith("/api/") and sent_token != token:
        return "token"
    return None


class MobileError(Exception):
    pass


class Mobile:
    """スマホから見るための、LAN 側の待ち受けとペアリングを預かる。

    押したときだけ開き、押さなければ何も待ち受けない。アプリを閉じると
    自動で止まり、ペアリングも消える（保存しない）。
    """

    SESSION_SECONDS = 7 * 24 * 3600
    CODE_SECONDS = 300
    MAX_FAILS = 5

    def __init__(self, cfg, factory, ip_func=lan_ip, acceptable=acceptable_ip,
                 port=None, clock=time.time):
        self.cfg = cfg
        self.factory = factory
        self.ip_func = ip_func
        self.acceptable = acceptable
        self.port = port or cfg.port
        self.clock = clock
        self.lock = threading.RLock()
        self.httpd = None
        self.ip = None
        self.codes = {}
        self.sessions = {}
        self.fails = 0

    # -- 状態 -------------------------------------------------------------
    @property
    def enabled(self):
        return self.httpd is not None

    @property
    def host(self):
        return "%s:%d" % (self.ip, self.port)

    def _purge(self):
        now = self.clock()
        for table in (self.codes, self.sessions):
            for key in [k for k, exp in table.items() if exp < now]:
                del table[key]

    def status(self):
        with self.lock:
            self._purge()
            out = {"enabled": self.enabled, "devices": len(self.sessions)}
            if self.enabled:
                out["host"] = self.host
                # 回線が変わって IP が替わると、開いた先に届かなくなる
                out["stale"] = bool(self.ip_func() not in (None, self.ip))
            return out

    # -- 開く・閉じる ------------------------------------------------------
    def enable(self):
        with self.lock:
            if self.enabled:
                return
            ip = self.ip_func()
            if not ip:
                raise MobileError("ネットワークに繋がっていないようです。Wi-Fi に繋いでから、"
                                  "もう一度お試しください。")
            if not self.acceptable(ip):
                raise MobileError(
                    "この接続（%s）は、インターネットから直接見える可能性があるため、"
                    "スマホ用の待ち受けは開きません。家庭や職場の Wi-Fi に繋いでください。" % ip)
            try:
                httpd = ThreadingHTTPServer((ip, self.port), self.factory(self))
            except OSError as exc:
                raise MobileError("%s:%d を開けませんでした: %s\n"
                                  "ファイアウォールの設定を確かめてください。" % (ip, self.port, exc))
            httpd.daemon_threads = True
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            self.httpd, self.ip, self.fails = httpd, ip, 0

    def disable(self):
        with self.lock:
            httpd, self.httpd = self.httpd, None
            self.codes.clear()
            self.sessions.clear()
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()

    # -- ペアリング --------------------------------------------------------
    def new_pairing(self):
        """QR コードに入れる、1回きり・短時間の合言葉を作る。"""
        with self.lock:
            if not self.enabled:
                raise MobileError("スマホ用の待ち受けが開いていません。")
            self._purge()
            code = _secrets.token_urlsafe(9)
            self.codes = {code: self.clock() + self.CODE_SECONDS}   # 前のものは捨てる
            self.fails = 0
            return {"url": "http://%s/pair?code=%s" % (self.host, code),
                    "expires_in": self.CODE_SECONDS}

    def redeem(self, code):
        """合言葉を鍵に引き換える。使えない場合は None。

        間違いが続いたら、有効な合言葉を全部捨てる。パソコンで新しく
        出し直すまで、総当たりは続けられない。
        """
        probe = (code or "").encode("utf-8")
        with self.lock:
            self._purge()
            hit = None
            for candidate in self.codes:
                if hmac.compare_digest(candidate.encode("utf-8"), probe):
                    hit = candidate
            if hit is None:
                self.fails += 1
                if self.fails >= self.MAX_FAILS:
                    self.codes.clear()
                return None
            del self.codes[hit]                      # 1回しか使えない
            sid = _secrets.token_urlsafe(32)
            self.sessions[sid] = self.clock() + self.SESSION_SECONDS
            return sid

    def valid_session(self, sid):
        if not sid:
            return False
        with self.lock:
            self._purge()
            return sid in self.sessions


def _notice_page(title, lines):
    """スマホに見せる、飾りの少ないお知らせの画面。"""
    body = "".join("<p>%s</p>" % escape(l) for l in lines)
    return ("<!DOCTYPE html><html lang=\"ja\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            "<title>Fukidashi</title><style>"
            ":root{color-scheme:light dark}"
            "body{font:16px/1.7 -apple-system,BlinkMacSystemFont,'Hiragino Sans',sans-serif;"
            "max-width:28em;margin:12vh auto;padding:0 20px}"
            "h1{font-size:19px}</style></head><body><h1>%s</h1>%s</body></html>"
            % (escape(title), body))


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
            passwords, missing = {}, []
            for a in self.cfg.accounts:
                try:
                    passwords[a.email] = config.app_password(a)
                except config.MissingPassword as exc:
                    missing.append((a, exc))
            if not passwords:
                # 1つも使えないなら、理由をそのまま見せる
                raise missing[0][1]
            n = imapsync.sync_all(self.cfg, passwords, progress=self._log)
            note = ""
            if missing:
                note = "（%s はパスワード未設定のため飛ばしました。アプリを開き直すと入力できます）" % \
                    "、".join(a.label for a, _ in missing)
            self._log("完了: %d 件を取り込みました。%s" % (n, note))
        except config.MissingPassword as exc:
            # 設定の途中という想定内の状態。記録を汚さない。
            self.error = str(exc)
            self._log("エラー: %s" % exc)
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


def make_handler(cfg, db, runner, control, token, outbox, mobile=None, lan=None):
    # lan が None なら、このパソコンの中だけの待ち受け。
    # lan（= Mobile）が渡されたら、同じネットワーク向けで、守りが厳しくなる。

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

        def _session_id(self):
            raw = self.headers.get("Cookie")
            if not raw:
                return None
            try:
                jar = SimpleCookie()
                jar.load(raw)
                morsel = jar.get(SESSION_COOKIE)
                return morsel.value if morsel else None
            except Exception:
                return None

        def _unpaired(self, path):
            if path.startswith("/api/"):
                return self._json({"error": "unpaired"}, 401)
            self._send(401, _notice_page(
                "このスマホは、まだ接続されていません",
                ["パソコンの Fukidashi で「スマホ」ボタンを押し、表示された QR コードを、"
                 "このスマホのカメラで読み取ってください。"]),
                "text/html; charset=utf-8")

        def _pair(self, code):
            sid = lan.redeem(code)
            if sid is None:
                return self._send(403, _notice_page(
                    "この QR コードは使えません",
                    ["期限（5分）が切れたか、すでに使われています。",
                     "パソコンの Fukidashi で、新しい QR コードを出してください。"]),
                    "text/html; charset=utf-8")
            self.send_response(302)
            self.send_header("Location", "/")
            self.send_header("Set-Cookie",
                             "%s=%s; HttpOnly; SameSite=Strict; Path=/; Max-Age=%d"
                             % (SESSION_COOKIE, sid, Mobile.SESSION_SECONDS))
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()

        def _guard(self):
            """通してよい要求か確かめる。駄目なら理由を返して True。"""
            path = urlparse(self.path).path
            host, origin = self.headers.get("Host"), self.headers.get("Origin")
            sent = self.headers.get(TOKEN_HEADER)
            if lan is None:
                reason = check_request(cfg.port, token, path, host, origin, sent)
            else:
                reason = check_lan_request(
                    lan.host, token, path, host, origin, sent,
                    lan.valid_session(self._session_id()))
            if reason is None:
                return False
            if reason == "unpaired":
                self._unpaired(path)
                return True
            self._send(403, json.dumps({"error": "refused:" + reason},
                                       ensure_ascii=False))
            return True

        def _ai_post(self, path, payload):
            """AI で返信案。許可・案の作成は LAN 側（スマホ）からも使える。
            API キーの保存・削除は、パソコンの中からだけ（LAN 側には経路が無い）。"""
            key = (payload.get("key") or "").strip()
            try:
                if path == "/api/ai/consent":
                    return self._json(ai.set_consent(db, cfg, key, payload.get("value")))
                if path == "/api/ai/suggest":
                    return self._json(ai.suggest(
                        db, cfg, key, payload.get("instruction"), payload.get("previous")))
                if lan is None and path == "/api/ai/key":
                    ai.save_key(payload.get("value"))
                    return self._json(ai.settings(cfg))
                if lan is None and path == "/api/ai/key/delete":
                    ai.delete_key()
                    return self._json(ai.settings(cfg))
            except ai.ConsentRequired as exc:
                # 403 は守りの拒否（合言葉・Host・Origin）にだけ使う。画面はそれを
                # 「アプリが再起動した」と受け取って読み込み直すので、許可の話は別の番号にする。
                return self._json({"error": str(exc), "consent": False}, 409)
            except ai.AIError as exc:
                return self._json({"error": str(exc)}, 400)
            self._json({"error": "not found"}, 404)

        def _mobile_api(self, path):
            """スマホ接続の操作。パソコンの中からだけ（LAN 側には経路が無い）。"""
            try:
                if path == "/api/mobile/enable":
                    mobile.enable()
                    return self._json(mobile.status())
                if path == "/api/mobile/disable":
                    mobile.disable()
                    return self._json(mobile.status())
                if path == "/api/mobile/pair":
                    info = mobile.new_pairing()
                    info["svg"] = qr.to_svg(qr.make_matrix(info["url"]))
                    return self._json(info)
            except (MobileError, qr.QRError) as exc:
                return self._json({"error": str(exc)}, 400)
            self._json({"error": "not found"}, 404)

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
            raw = self.rfile.read(length) if length else b""
            if self._guard():
                return
            try:
                payload = json.loads(raw.decode("utf-8")) if raw else {}
            except (ValueError, UnicodeDecodeError):
                return self._json({"error": "本文を読めませんでした"}, 400)

            path = urlparse(self.path).path
            if mobile is not None and lan is None and path.startswith("/api/mobile/"):
                return self._mobile_api(path)
            if path.startswith("/api/ai/"):
                return self._ai_post(path, payload)
            if path == "/api/send":
                key = (payload.get("key") or "").strip()
                if not key:
                    return self._json({"error": "会話が指定されていません"}, 400)
                try:
                    return self._json(outbox.queue(key, payload.get("text") or ""))
                except smtpsend.SendError as exc:
                    return self._json({"error": str(exc)}, 400)
            if path == "/api/send/cancel":
                return self._json(
                    {"cancelled": outbox.cancel((payload.get("id") or "").strip())})
            if path == "/api/sync":
                started = runner.start()
                return self._json({"started": started, **runner.state()})
            if path == "/api/quit":
                if lan is not None:
                    # スマホからこのパソコンのアプリを止めさせない
                    return self._json({"error": "not found"}, 404)
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

            if lan is not None and u.path == "/pair":
                return self._pair(one("code"))
            if mobile is not None and lan is None and u.path == "/api/mobile":
                return self._json(mobile.status())
            if u.path == "/api/ai":
                return self._json(ai.status(db, cfg, one("key") or ""))
            if lan is None and u.path == "/api/ai/settings":
                return self._json(ai.settings(cfg))

            if u.path in ("/", "/index.html"):
                return self._static("index.html")
            if u.path.startswith("/static/"):
                return self._static(u.path[len("/static/"):])

            if u.path == "/api/status":
                return self._json({
                    "local": lan is None,
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
    mobile = Mobile(
        cfg,
        lambda gate: make_handler(cfg, db, runner, control, token, outbox, lan=gate),
        port=getattr(cfg, "mobile_port", None))
    httpd = ThreadingHTTPServer(
        ("127.0.0.1", cfg.port),
        make_handler(cfg, db, runner, control, token, outbox, mobile=mobile))
    httpd.daemon_threads = True
    control["httpd"] = httpd
    httpd.gline_db = db
    httpd.gline_mobile = mobile
    return httpd


def shutdown(httpd):
    """待ち受けを止め、DB を安全に閉じる。

    書き込み途中で落とされると SQLite が中途半端な状態で残り、次の起動で
    「database disk image is malformed」になることがある。終了経路は必ずここを通す。
    """
    mobile = getattr(httpd, "gline_mobile", None)
    if mobile is not None:
        try:
            mobile.disable()          # スマホ用の待ち受けも必ず閉じる
        except Exception:
            pass
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
