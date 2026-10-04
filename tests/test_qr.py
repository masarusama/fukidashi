#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""QR コードの作成を確かめる。標準ライブラリだけで動く。

    python3 tests/test_qr.py

QR は「読み取れない」ときの原因が見えにくい。ここでは次の3段で固めている。

  1. 規格の公開テストベクタ（誤り訂正の計算）と、表の整合
  2. 出来上がった配列の構造（位置検出・タイミング・形式情報）
  3. 実際に読み取れると確かめた出力の指紋（ゴールデンハッシュ）

読み取りの確認そのものは macOS の検出器（CoreImage）で、バージョン 1〜10・
全8マスク・日本語を含む73枚すべてについて済ませてある。ここの指紋は、
そのとき読めた配列が変わっていないことを守るためのもの。
"""

import hashlib
import os
import sys
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gline  # noqa: E402
from gline import qr  # noqa: E402

gline.use_utf8_output()

PASS, FAIL = [], []


def check(name, got, want):
    (PASS if got == want else FAIL).append(
        name if got == want else "%s\n    期待: %r\n    実際: %r" % (name, want, got))


def check_true(name, got):
    check(name, bool(got), True)


# ---------------------------------------------------------------- 誤り訂正

# 規格の解説で広く使われている例（"HELLO WORLD"・1-M）
DATA = [32, 91, 11, 120, 209, 114, 220, 77, 67, 64, 236, 17, 236, 17, 236, 17]
WANT = [196, 35, 39, 119, 235, 215, 231, 226, 93, 23]
check("Reed-Solomon が規格の例と一致する", qr.rs_remainder(DATA, 10), WANT)

for version, (total, ec, groups) in qr._M.items():
    check("v%d: 全体 = Σ ブロック数 × (データ語 + 誤り訂正語)" % version,
          sum(n * (d + ec) for n, d in groups), total)

# 機能パターンを除いた「置けるモジュール数」が、コード語 + 剰余ビットに一致する。
# 位置検出・位置合わせ・バージョン情報の幾何がずれると、ここで必ず食い違う。
for version in range(1, 11):
    size, _, is_func = qr._function_patterns(version)
    free = sum(1 for y in range(size) for x in range(size) if not is_func[y][x])
    remainder = 0 if version == 1 or version >= 7 else 7
    check("v%d: 置けるモジュール数が理論値と一致する" % version,
          free, qr._M[version][0] * 8 + remainder)

# ---------------------------------------------------------------- バージョン選び

check("14 バイトは v1 に入る", qr._pick_version(14), 1)
check("15 バイトは v2 になる", qr._pick_version(15), 2)
check("62 バイトは v4 に入る", qr._pick_version(62), 4)
check("63 バイトは v5 になる", qr._pick_version(63), 5)
check("213 バイトは v10 に入る", qr._pick_version(213), 10)
try:
    qr.make_matrix("x" * (qr.MAX_BYTES + 1))
    check("入りきらなければ断る", "通った", "断る")
except qr.QRError:
    check("入りきらなければ断る", "断る", "断る")

# ---------------------------------------------------------------- 構造

URL = "http://192.168.0.108:8765/pair?code=Vf3k_a9ZxQ2m"
m = qr.make_matrix(URL)
n = len(m)
check("サイズは 17 + 4×バージョン", n, 17 + 4 * 4)
check("正方形", all(len(row) == n for row in m), True)


def finder(top, left):
    """位置検出パターン（7×7）が期待どおりか。"""
    for dy in range(7):
        for dx in range(7):
            ring = max(abs(dy - 3), abs(dx - 3))
            want = ring != 2
            if m[top + dy][left + dx] != want:
                return False
    return True


check("左上の位置検出パターン", finder(0, 0), True)
check("右上の位置検出パターン", finder(0, n - 7), True)
check("左下の位置検出パターン", finder(n - 7, 0), True)
check("タイミングパターン（横）",
      [m[6][x] for x in range(8, n - 8)], [x % 2 == 0 for x in range(8, n - 8)])
check("タイミングパターン（縦）",
      [m[y][6] for y in range(8, n - 8)], [y % 2 == 0 for y in range(8, n - 8)])
check("常に暗いモジュール", m[n - 8][8], True)


def read_format(first):
    """形式情報（誤り訂正レベルとマスク）を読み戻す。"""
    if first:
        cells = [(8, i) for i in range(6)] + [(8, 7), (8, 8), (7, 8)] + \
                [(14 - i, 8) for i in range(9, 15)]
    else:
        cells = [(n - 1 - i, 8) for i in range(8)] + [(8, n - 15 + i) for i in range(8, 15)]
    bits = 0
    for i, (x, y) in enumerate(cells):
        bits |= (1 if m[y][x] else 0) << i
    return bits ^ 0x5412


f1, f2 = read_format(True), read_format(False)
check("形式情報は2か所で一致する", f1, f2)
check("形式情報の誤り訂正レベルは M", (f1 >> 13) & 0b11, 0b00)

rem = f1 >> 10
for _ in range(10):
    rem = (rem << 1) ^ ((rem >> 9) * 0x537)
check("形式情報の検査ビット（BCH）が正しい", rem & 0x3FF, f1 & 0x3FF)
check_true("マスクは 0〜7", 0 <= ((f1 >> 10) & 0b111) <= 7)

# ---------------------------------------------------------------- 指紋

flat = "".join("".join("1" if v else "0" for v in row) for row in m)
check("読み取れると確かめた配列から変わっていない",
      hashlib.sha256(flat.encode()).hexdigest(),
      "6cce4e76e0decc403d045452eeb89a8f95943c5e54266ba033d2b81c0b1583fb")
check("同じ入力なら必ず同じ配列になる", qr.make_matrix(URL) == m, True)

jp = qr.make_matrix("http://192.168.0.108:8765/pair?code=あいうえお")
check_true("日本語（UTF-8）も入る", len(jp) >= 21)

# ---------------------------------------------------------------- SVG

svg = qr.to_svg(m)
root = ET.fromstring(svg)
check("SVG として読める", root.tag.endswith("svg"), True)
check("白地と黒の2色だけ", sorted({e.get("fill") for e in root.iter() if e.get("fill")}),
      ["#000", "#fff"])
check("余白（クワイエットゾーン）が4モジュール", root.get("viewBox"), "0 0 %d %d" % (n + 8, n + 8))
check_true("暗い部分が描かれている", "M" in svg and "h" in svg)

print("%d 件成功 / %d 件失敗" % (len(PASS), len(FAIL)))
if FAIL:
    print()
    for f in FAIL:
        print("  ✗ " + f)
    sys.exit(1)
print("すべて通りました。")
