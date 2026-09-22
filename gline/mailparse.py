"""RFC822 のメールを「吹き出し1個ぶんの発言」に正規化する。

読みやすさはここで決まる。やっていることは3つ:
  1. HTML を素のテキストに落とす（引用ブロックは畳む）
  2. 引用・返信ヘッダ・署名を末尾から削る
  3. 誰と誰の会話なのかを決める（会話キー）
"""

import email
import email.policy
import re
from email.header import decode_header
from email.utils import getaddresses, parsedate_to_datetime
from html.parser import HTMLParser

# --------------------------------------------------------------------------
# ヘッダ
# --------------------------------------------------------------------------

def header_text(raw):
    """MIME エンコードされたヘッダを読める文字列にする。壊れていても諦めない。"""
    if not raw:
        return ""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    parts = []
    try:
        chunks = decode_header(raw)
    except Exception:
        return raw.strip()
    for text, enc in chunks:
        if isinstance(text, bytes):
            parts.append(_decode_bytes(text, enc))
        else:
            parts.append(text)
    return re.sub(r"\s+", " ", "".join(parts)).strip()


def addresses(msg, *fields):
    """指定ヘッダから (表示名, アドレス小文字) のリストを返す。

    必ず「生ヘッダを解析 → 表示名だけをデコード」の順でやる。先にデコード
    すると、表示名に含まれる <> や [] をアドレス構文と誤読して壊れる
    （例: 安心介護マガジン配信<送信専用> <magazine@example.jp>）。
    """
    raw = []
    for field in fields:
        for value in msg.get_all(field, []):
            raw.append(value if isinstance(value, str) else str(value))
    out = []
    for name, addr in getaddresses(raw):
        addr = addr.strip().lower()
        if "@" not in addr:
            continue
        out.append((header_text(name), addr))
    return out


def message_date(msg, internaldate=None):
    for value in msg.get_all("Date", []):
        try:
            dt = parsedate_to_datetime(header_text(value))
        except (TypeError, ValueError, IndexError):
            continue
        if dt is None:
            continue
        if dt.tzinfo is None:
            return int(dt.timestamp())
        return int(dt.timestamp())
    if internaldate is not None:
        return int(internaldate)
    return 0


# --------------------------------------------------------------------------
# HTML → テキスト
# --------------------------------------------------------------------------

_VOID = {"br", "img", "hr", "input", "meta", "link", "source", "col", "area", "base"}
_DROP = {"script", "style", "head", "title", "meta", "link", "noscript"}
_BLOCK = {
    "p", "div", "br", "tr", "li", "blockquote", "table", "ul", "ol", "pre",
    "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "header",
    "footer", "hr", "td", "th", "dd", "dt", "figure", "address",
}
# 引用ブロックだと名乗っているクラス / id
_QUOTE_HINTS = (
    "gmail_quote", "yahoo_quoted", "moz-cite-prefix", "ydp", "quoted",
    "outlookmessageheader", "appendonsend", "divrplyfwdmsg", "msoblockquote",
)


class _HTMLToText(HTMLParser):
    def __init__(self, drop_quotes):
        super().__init__(convert_charrefs=True)
        self.drop_quotes = drop_quotes
        self.out = []
        self.stack = []          # [(tag, opened_a_skip)]
        self.skip = 0
        self.quote_seen = False

    # -- helpers ----------------------------------------------------------
    def _is_quote(self, tag, attrs):
        if tag == "blockquote":
            return True
        flat = " ".join(v.lower() for _, v in attrs if v)
        return any(hint in flat for hint in _QUOTE_HINTS)

    def _newline(self, hard=False):
        self.out.append("\n\n" if hard else "\n")

    # -- parser hooks -----------------------------------------------------
    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        started = False
        if tag in _DROP:
            started = True
        elif self.drop_quotes and self._is_quote(tag, attrs):
            started = True
            self.quote_seen = True

        # <meta> や <link> のような空要素は中身を持たないので、読み飛ばしを
        # 開始させてはいけない。閉じタグが来ず、解除できなくなる。
        if tag not in _VOID:
            if started:
                self.skip += 1
            self.stack.append((tag, started))

        if self.skip:
            return
        if tag == "br":
            self._newline()
        elif tag == "li":
            self._newline()
            self.out.append("・")
        elif tag in _BLOCK:
            self._newline()

    def handle_startendtag(self, tag, attrs):
        tag = tag.lower()
        if self.skip:
            return
        if tag == "br":
            self._newline()
        elif tag == "img":
            alt = dict((k.lower(), v) for k, v in attrs).get("alt")
            if alt and alt.strip():
                self.out.append("[画像: %s]" % alt.strip())

    def handle_endtag(self, tag):
        tag = tag.lower()
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                for _, started in self.stack[i:]:
                    if started:
                        self.skip = max(0, self.skip - 1)
                del self.stack[i:]
                break
        if not self.skip and tag in _BLOCK:
            self._newline()

    def handle_data(self, data):
        if self.skip or not data:
            return
        self.out.append(re.sub(r"[ \t\r\f\v]+", " ", data.replace("\n", " ")))

    def text(self):
        return "".join(self.out)


def html_to_text(html, drop_quotes=True):
    parser = _HTMLToText(drop_quotes)
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        pass  # 壊れた HTML でもそこまでの結果を使う
    return tidy(parser.text())


def tidy(text):
    text = text.replace(" ", " ").replace("​", "")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# --------------------------------------------------------------------------
# 引用・署名の切り落とし
# --------------------------------------------------------------------------

_CUTS = [
    # 英語 Gmail: "On Mon, Jan 1, 2026 at 9:00 AM Alice <a@x> wrote:"（改行を挟むことがある）
    re.compile(r"^On\s.{0,300}?\bwrote:\s*$", re.M | re.S),
    # 日本語 Gmail: "2026年1月1日(月) 9:00 Alice <a@x>:"
    re.compile(r"^\d{4}年\d{1,2}月\d{1,2}日.{0,200}?:\s*$", re.M | re.S),
    re.compile(r"^\d{4}/\d{1,2}/\d{1,2}\s.{0,200}?:\s*$", re.M | re.S),
    # Apple Mail / 一般
    re.compile(r"^.{0,120}?\bwrote:\s*$", re.M),
    re.compile(r"^-{2,}\s*(Original Message|Forwarded message|元のメッセージ|転送されたメッセージ)\s*-{2,}", re.M | re.I),
    re.compile(r"^={10,}\s*$", re.M),
    re.compile(r"^_{10,}\s*$", re.M),
    re.compile(r"^(Sent from my |iPhone から送信|Android から送信)", re.M),
]

# Outlook 形式のヘッダブロック: From:/Sent:/To:/Subject: が数行内に固まって現れる
_OL_FIRST = re.compile(r"^\s*(From|差出人|送信元|送信者)\s*[:：]\s*\S", re.M)
_OL_NEXT = re.compile(r"^\s*(Sent|Date|To|Cc|Subject|送信日時|日付|宛先|件名)\s*[:：]", re.M)

_SIG = re.compile(r"^--\s*$", re.M)

# 日本語の署名ブロックによくある連絡先の行
_CONTACT = re.compile(
    r"^\s*(〒\s*\d|TEL|FAX|PHONE|E-?MAIL|MAIL|URL|HP|携帯|電話|住所)\s*[：:.]|"
    r"^\s*〒\d{3}", re.I)
# 末尾の飾り罫線
_RULE = re.compile(r"^[\s]*[━─═＝=\-─‐–—\*\+\u2500-\u257f_]{10,}[\s]*$")


def _tail_cut(text, matcher, need=1):
    """本文の後ろ半分にある定型ブロックの開始位置を返す。

    切りすぎないよう、後半にあること・本文の半分以上を削らないことを条件にする。
    """
    lines = text.split("\n")
    if len(lines) < 3:
        return None
    offsets, pos = [], 0
    for line in lines:
        offsets.append(pos)
        pos += len(line) + 1
    half = len(text) * 0.5
    for i, line in enumerate(lines):
        if offsets[i] < half:
            continue
        if not matcher.match(line):
            continue
        if need > 1 and sum(1 for l in lines[i:] if matcher.match(l)) < need:
            continue
        if len(text) - offsets[i] > half:
            continue
        return offsets[i]
    return None


def _outlook_cut(text):
    for m in _OL_FIRST.finditer(text):
        window = text[m.end(): m.end() + 400]
        if _OL_NEXT.search(window):
            return m.start()
    return None


def _quoted_run_cut(text):
    """'>' で始まる行が 2 行以上続く最初の位置。"""
    lines = text.split("\n")
    offset = 0
    run_start = None
    run = 0
    for line in lines:
        if line.lstrip().startswith(">"):
            if run == 0:
                run_start = offset
            run += 1
            if run >= 2:
                return run_start
        else:
            run = 0
        offset += len(line) + 1
    return None


def strip_quotes(text, extra_patterns=()):
    """返信の引用・定型フッタを落として、その回の発言だけを残す。"""
    if not text:
        return ""
    cut = len(text)
    for rx in _CUTS:
        m = rx.search(text)
        if m and m.start() < cut:
            cut = m.start()
    for finder in (_outlook_cut, _quoted_run_cut):
        pos = finder(text)
        if pos is not None and pos < cut:
            cut = pos

    # 署名ブロック（連絡先が2行以上）と、末尾の飾り罫線
    for matcher, need in ((_CONTACT, 2), (_RULE, 1)):
        pos = _tail_cut(text[:cut], matcher, need)
        if pos is not None and pos < cut:
            cut = pos

    body = text[:cut]

    m = _SIG.search(body)
    if m:
        body = body[:m.start()]

    for rx in extra_patterns:
        body = rx.sub("", body)

    body = tidy(body)
    # 全部消えてしまったら削りすぎ。罫線だけのメルマガ等で起きるので元に戻す。
    if not body and text.strip():
        return tidy(text)
    return body


# --------------------------------------------------------------------------
# 本文の取り出し
# --------------------------------------------------------------------------

# ISO-2022-JP 系。厳密な iso-2022-jp は実在のメールで落ちることがあるので、
# 受けの広い -ext から順に試す。
_JP_2022 = ("iso-2022-jp-ext", "iso-2022-jp-2", "iso-2022-jp")


def _decode_bytes(payload, charset=None):
    """バイト列を文字列にする。日本語のメールで壊れない順序で試す。"""
    if not payload:
        return ""
    # ISO-2022-JP は 7bit なので utf-8 や ascii でも「成功」してしまい、
    # エスケープ列がそのまま本文に残る。先に日本語コーデックで解く。
    if b"\x1b$" in payload:
        for enc in _JP_2022:
            try:
                return payload.decode(enc)
            except (LookupError, UnicodeDecodeError):
                continue
        return payload.decode("iso-2022-jp-ext", "replace")

    for candidate in (charset, "utf-8", "cp932", "euc-jp"):
        if not candidate:
            continue
        try:
            return payload.decode(candidate)
        except (LookupError, UnicodeDecodeError):
            continue
    # latin-1 は何でも通ってしまい文字化けを隠すので、置換で明示的に壊す
    try:
        return payload.decode(charset or "utf-8", "replace")
    except LookupError:
        return payload.decode("utf-8", "replace")


def _decode_part(part):
    return _decode_bytes(part.get_payload(decode=True),
                         part.get_content_charset())


def _is_attachment(part):
    disp = (part.get("Content-Disposition") or "").lower()
    if "attachment" in disp:
        return True
    return bool(part.get_filename())


def extract(msg):
    """(本文テキスト, 引用込み本文, 添付ファイル名リスト) を返す。"""
    plain, html, attachments = [], [], []

    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_maintype() == "multipart":
                continue
            ctype = part.get_content_type()
            if _is_attachment(part):
                name = header_text(part.get_filename() or "") or "(名前なし)"
                attachments.append(name)
                continue
            if ctype == "text/plain":
                plain.append(_decode_part(part))
            elif ctype == "text/html":
                html.append(_decode_part(part))
    else:
        if msg.get_content_type() == "text/html":
            html.append(_decode_part(msg))
        else:
            plain.append(_decode_part(msg))

    plain_text = tidy("\n".join(plain)) if plain else ""
    # text/plain 側が「このメールはHTMLメールです」程度の代替文しか持たない
    # ことがある。その場合は HTML 側を本文として扱う。
    if plain_text and not (html and len(plain_text) < 120):
        full = plain_text
        stripped_source = full
    elif html:
        joined = "\n".join(html)
        full = html_to_text(joined, drop_quotes=False)
        stripped_source = html_to_text(joined, drop_quotes=True)
    else:
        full = stripped_source = ""

    return stripped_source, full, attachments


# --------------------------------------------------------------------------
# 配信物（お知らせ）かどうか
# --------------------------------------------------------------------------

# 一斉配信であることを名乗るヘッダ。人間が1対1で書いたメールには通常つかない。
_BULK_HEADERS = (
    "List-Unsubscribe", "List-Id", "List-Post", "Feedback-ID",
    "X-Auto-Response-Suppress", "X-Campaign-Id", "X-Mailer-Id",
)
_PRECEDENCE = re.compile(r"^(bulk|list|junk|auto_reply)$", re.I)

# 機械が送ってくるアドレスの典型。shopinfo@ や paypaycard-info@ のように
# 語がくっついて出てくるので、前方一致ではなく部分一致で見る。
_ROBOT_WORDS = (
    "noreply", "no-reply", "donotreply", "do-not-reply", "notreply",
    "newsletter", "mailmag", "magazine", "mailer", "postmaster", "post_master",
    "webmaster", "notification", "notify", "notice", "bounce", "campaign",
    "promo", "marketing", "delivery", "mailmagazine", "info", "news", "alert",
    "mkt", "autoreply", "auto_reply", "auto-reply", "wordpress", "helpdesk",
    "shopinfo", "sales", "support", "yoyaku", "otodoke", "thanks",
    "gochuumon", "point-", "-point", "send-", "webmaster", "mailinfo",
)
# 部分一致だと巻き添えが出る短い語は、ローカル部が完全一致したときだけ
_ROBOT_EXACT = frozenset((
    "cs", "mail", "post", "member", "help", "service", "contact", "order",
    "system", "admin", "shop", "reply", "entry", "regist", "webmail", "mag",
))
# 一斉配信に使われるサブドメイン
_BULK_DOMAIN = re.compile(
    r"^(mail|email|e|em|news|info|magazine|emagazine|mkt|marketing|mailmag|"
    r"pointmail|noreply|post|delivery|mg|msg|cdp|crm)\.", re.I)

# 日本語の配信物はほぼ必ず配信停止の導線を持つ
_PROMO = re.compile(
    r"配信停止|配信の停止|配信を停止|購読解除|登録解除|配信解除|受信停止|"
    r"メールマガジン|メルマガ|配信を希望されない|心当たりのない|"
    r"unsubscribe|opt[-\s]?out", re.I)


def is_bulk(msg):
    """一斉配信・自動送信のメールなら True。"""
    for name in _BULK_HEADERS:
        if msg.get(name):
            return True
    if _PRECEDENCE.match((msg.get("Precedence") or "").strip()):
        return True
    auto = (msg.get("Auto-Submitted") or "").strip().lower()
    if auto and auto != "no":
        return True
    return False


def robot_address(addr):
    """noreply 系のアドレス、または一斉配信用のドメインなら True。"""
    if not addr or "@" not in addr:
        return False
    local, _, domain = addr.partition("@")
    local = local.lower()
    if local in _ROBOT_EXACT:
        return True
    if any(w in local for w in _ROBOT_WORDS):
        return True
    return bool(_BULK_DOMAIN.match(domain))


def is_promotional(text):
    """本文に配信停止の導線があれば True。"""
    return bool(text) and bool(_PROMO.search(text))


# --------------------------------------------------------------------------
# 会話キー
# --------------------------------------------------------------------------

def conversation(cfg, from_addr, to_addrs, cc_addrs):
    """この1通がどの「トーク」に属するかを決める。

    件名や References ではなく参加者で決めるのが肝。件名を変えられても
    引用を切られても、同じ相手なら1本の会話に並ぶ。
    """
    participants = [a for a in ([from_addr] + list(to_addrs)) if a]
    others = sorted({a for a in participants if not cfg.is_me(a)})
    if not others:
        others = sorted({a for a in cc_addrs if not cfg.is_me(a)})
    if not others:
        return "self", True          # 自分宛のメモ
    return "|".join(others), len(others) == 1
