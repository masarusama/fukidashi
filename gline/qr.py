"""QR コードを作る（標準ライブラリだけ）。

パソコンの画面に出した URL をスマホで読み取ってもらうためのもの。
用途が URL に限られるので、次に絞っている。

  * バイトモード（UTF-8 のまま入れる）
  * 誤り訂正レベル M（約15%の欠けまで復元できる）
  * バージョン 1〜10（21×21 〜 57×57。213 バイトまで）

手順は規格どおり。データを符号化し、ブロックに分けて Reed-Solomon の
誤り訂正符号を付け、交互に並べてから、機能パターン（位置検出・タイミング・
位置合わせ）を避けて蛇行しながら敷き詰める。最後に8通りのマスクのうち
見た目の偏りが最も少ないものを選び、形式情報を書き込む。
"""

# バージョン: (全コード語数, 1ブロックあたりの誤り訂正語数, [(ブロック数, データ語数), ...])
# レベル M。どの行も 全体 = Σ ブロック数 × (データ語数 + 誤り訂正語数) になる。
_M = {
    1: (26, 10, [(1, 16)]),
    2: (44, 16, [(1, 28)]),
    3: (70, 26, [(1, 44)]),
    4: (100, 18, [(2, 32)]),
    5: (134, 24, [(2, 43)]),
    6: (172, 16, [(4, 27)]),
    7: (196, 18, [(4, 31)]),
    8: (242, 22, [(2, 38), (2, 39)]),
    9: (292, 22, [(3, 36), (2, 37)]),
    10: (346, 26, [(4, 43), (1, 44)]),
}

# 位置合わせパターンの中心座標
_ALIGN = {
    1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30],
    6: [6, 34], 7: [6, 22, 38], 8: [6, 24, 42], 9: [6, 26, 46], 10: [6, 28, 50],
}

# 誤り訂正レベル M を表す2ビット
_FORMAT_LEVEL_M = 0b00

MAX_BYTES = 213          # バージョン 10・レベル M に入るバイト数


class QRError(ValueError):
    pass


# ---------------------------------------------------------------- GF(256)

_EXP = [0] * 512
_LOG = [0] * 256
_x = 1
for _i in range(255):
    _EXP[_i] = _x
    _LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D                      # x^8 + x^4 + x^3 + x^2 + 1
for _i in range(255, 512):
    _EXP[_i] = _EXP[_i - 255]


def _mul(a, b):
    if a == 0 or b == 0:
        return 0
    return _EXP[_LOG[a] + _LOG[b]]


def _generator(degree):
    """∏(x - α^i), i = 0..degree-1。最高次の係数が先頭。"""
    poly = [1]
    for i in range(degree):
        nxt = [0] * (len(poly) + 1)
        for j, coef in enumerate(poly):
            nxt[j] ^= coef
            nxt[j + 1] ^= _mul(coef, _EXP[i])
        poly = nxt
    return poly


def rs_remainder(data, degree):
    """データ × x^degree を生成多項式で割った余り（＝誤り訂正語）。"""
    gen = _generator(degree)
    rem = [0] * degree
    for byte in data:
        factor = byte ^ rem[0]
        rem = rem[1:] + [0]
        for i in range(degree):
            rem[i] ^= _mul(gen[i + 1], factor)
    return rem


# ---------------------------------------------------------------- 符号化

def _pick_version(n_bytes):
    for version in range(1, 11):
        _, _, groups = _M[version]
        data_cw = sum(n * d for n, d in groups)
        count_bits = 8 if version < 10 else 16
        if 4 + count_bits + 8 * n_bytes <= data_cw * 8:
            return version
    raise QRError("長すぎて入りません（%d バイト。上限 %d）" % (n_bytes, MAX_BYTES))


def _data_codewords(data, version):
    _, _, groups = _M[version]
    capacity = sum(n * d for n, d in groups) * 8
    bits = []

    def put(value, length):
        for i in range(length - 1, -1, -1):
            bits.append((value >> i) & 1)

    put(0b0100, 4)                                   # バイトモード
    put(len(data), 8 if version < 10 else 16)        # 文字数
    for b in data:
        put(b, 8)
    put(0, min(4, capacity - len(bits)))             # 終端
    while len(bits) % 8:
        bits.append(0)
    pad = (0xEC, 0x11)
    i = 0
    while len(bits) < capacity:
        put(pad[i % 2], 8)
        i += 1

    return [int("".join(map(str, bits[k:k + 8])), 2) for k in range(0, len(bits), 8)]


def _interleave(codewords, version):
    _, ec_len, groups = _M[version]
    blocks, pos = [], 0
    for count, size in groups:
        for _ in range(count):
            chunk = codewords[pos:pos + size]
            pos += size
            blocks.append((chunk, rs_remainder(chunk, ec_len)))

    out = []
    for i in range(max(len(c) for c, _ in blocks)):
        for chunk, _ in blocks:
            if i < len(chunk):
                out.append(chunk[i])
    for i in range(ec_len):
        for _, ec in blocks:
            out.append(ec[i])
    return out


# ---------------------------------------------------------------- 配置

def _bit(value, i):
    return (value >> i) & 1


def _draw_format(modules, is_func, size, mask):
    data = (_FORMAT_LEVEL_M << 3) | mask
    rem = data
    for _ in range(10):
        rem = (rem << 1) ^ ((rem >> 9) * 0x537)
    bits = ((data << 10) | rem) ^ 0x5412

    def put(x, y, v):
        modules[y][x] = bool(v)
        is_func[y][x] = True

    for i in range(6):
        put(8, i, _bit(bits, i))
    put(8, 7, _bit(bits, 6))
    put(8, 8, _bit(bits, 7))
    put(7, 8, _bit(bits, 8))
    for i in range(9, 15):
        put(14 - i, 8, _bit(bits, i))
    for i in range(8):
        put(size - 1 - i, 8, _bit(bits, i))
    for i in range(8, 15):
        put(8, size - 15 + i, _bit(bits, i))
    put(8, size - 8, 1)                              # 常に暗いモジュール


def _function_patterns(version):
    size = 17 + 4 * version
    modules = [[False] * size for _ in range(size)]
    is_func = [[False] * size for _ in range(size)]

    def put(x, y, v):
        if 0 <= x < size and 0 <= y < size:
            modules[y][x] = bool(v)
            is_func[y][x] = True

    for i in range(size):                            # タイミング
        put(6, i, i % 2 == 0)
        put(i, 6, i % 2 == 0)

    for cx, cy in ((3, 3), (size - 4, 3), (3, size - 4)):    # 位置検出（余白つき）
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                put(cx + dx, cy + dy, max(abs(dx), abs(dy)) not in (2, 4))

    pos = _ALIGN[version]
    for i, cy in enumerate(pos):                     # 位置合わせ
        for j, cx in enumerate(pos):
            if (i == 0 and j == 0) or (i == 0 and j == len(pos) - 1) \
                    or (i == len(pos) - 1 and j == 0):
                continue                             # 位置検出と重なる
            for dy in range(-2, 3):
                for dx in range(-2, 3):
                    put(cx + dx, cy + dy, max(abs(dx), abs(dy)) != 1)

    if version >= 7:                                 # バージョン情報
        rem = version
        for _ in range(12):
            rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
        bits = (version << 12) | rem
        for i in range(18):
            a, b = size - 11 + i % 3, i // 3
            put(a, b, _bit(bits, i))
            put(b, a, _bit(bits, i))

    _draw_format(modules, is_func, size, 0)          # 場所を確保（値は後で上書き）
    return size, modules, is_func


def _place(modules, is_func, size, codewords):
    i = 0
    total_bits = len(codewords) * 8
    right = size - 1
    while right >= 1:
        if right == 6:
            right = 5
        for vert in range(size):
            for j in range(2):
                x = right - j
                upward = ((right + 1) & 2) == 0
                y = size - 1 - vert if upward else vert
                if not is_func[y][x] and i < total_bits:
                    modules[y][x] = bool(_bit(codewords[i >> 3], 7 - (i & 7)))
                    i += 1
        right -= 2


_MASKS = (
    lambda r, c: (r + c) % 2 == 0,
    lambda r, c: r % 2 == 0,
    lambda r, c: c % 3 == 0,
    lambda r, c: (r + c) % 3 == 0,
    lambda r, c: (r // 2 + c // 3) % 2 == 0,
    lambda r, c: (r * c) % 2 + (r * c) % 3 == 0,
    lambda r, c: ((r * c) % 2 + (r * c) % 3) % 2 == 0,
    lambda r, c: ((r + c) % 2 + (r * c) % 3) % 2 == 0,
)


def _apply_mask(modules, is_func, size, mask):
    fn = _MASKS[mask]
    for y in range(size):
        for x in range(size):
            if not is_func[y][x] and fn(y, x):
                modules[y][x] = not modules[y][x]


def _penalty(m):
    """規格の4つの減点規則。小さいほど読み取りやすい。"""
    size = len(m)
    score = 0
    cols = [list(c) for c in zip(*m)]

    for lines in (m, cols):
        for line in lines:
            run = 1
            for i in range(1, size):
                if line[i] == line[i - 1]:
                    run += 1
                else:
                    if run >= 5:
                        score += run - 2             # 3 + (run - 5)
                    run = 1
            if run >= 5:
                score += run - 2

    for y in range(size - 1):
        for x in range(size - 1):
            if m[y][x] == m[y][x + 1] == m[y + 1][x] == m[y + 1][x + 1]:
                score += 3

    a = [1, 0, 1, 1, 1, 0, 1, 0, 0, 0, 0]
    b = a[::-1]
    for lines in (m, cols):
        for line in lines:
            ints = [1 if v else 0 for v in line]
            for i in range(size - 10):
                seg = ints[i:i + 11]
                if seg == a or seg == b:
                    score += 40

    dark = sum(1 for row in m for v in row if v)
    total = size * size
    score += 10 * (abs(100 * dark - 50 * total) // (5 * total))
    return score


def make_matrix(text, mask=None):
    """文字列から QR のモジュール（暗い = True）の二次元配列を作る。

    mask を省略すると最も読み取りやすいものを選ぶ。指定は検査用。
    """
    data = text.encode("utf-8") if isinstance(text, str) else bytes(text)
    version = _pick_version(len(data))
    codewords = _interleave(_data_codewords(data, version), version)

    size, base, is_func = _function_patterns(version)
    _place(base, is_func, size, codewords)

    best = None
    for candidate in (range(8) if mask is None else (mask,)):
        modules = [row[:] for row in base]
        funcs = [row[:] for row in is_func]
        _apply_mask(modules, funcs, size, candidate)
        _draw_format(modules, funcs, size, candidate)
        score = _penalty(modules) if mask is None else 0
        if best is None or score < best[0]:
            best = (score, modules)
    return best[1]


def to_svg(matrix, border=4):
    """白地に黒の SVG。画面の配色に関係なく読めるよう、色は固定する。"""
    n = len(matrix)
    dim = n + 2 * border
    parts = []
    for y, row in enumerate(matrix):
        x = 0
        while x < n:
            if row[x]:
                start = x
                while x < n and row[x]:
                    x += 1
                parts.append("M%d %dh%dv1h-%dz" % (start + border, y + border,
                                                  x - start, x - start))
            else:
                x += 1
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" '
            'shape-rendering="crispEdges" role="img" aria-label="QR コード">'
            '<rect width="%d" height="%d" fill="#fff"/>'
            '<path d="%s" fill="#000"/></svg>' % (dim, dim, dim, dim, "".join(parts)))
