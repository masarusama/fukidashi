#!/usr/bin/env python3
"""Fukidashi.app を組み立てる（macOS 用）。

    python3 make_app.py            → dist/Fukidashi.app
    python3 make_app.py --zip      → 配布用の zip も作る

py2app などは使わない。.app は決まった形のフォルダなので、
macOS に最初から入っている python3 で動く中身を並べるだけで済む。
"""

import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"
APP = DIST / "Fukidashi.app"
NAME = "Fukidashi"
BUNDLE_ID = "com.github.masarusama.fukidashi"

# アプリの中に入れるもの。個人の設定とデータは絶対に入れない。
PAYLOAD_FILES = ["app.py", "sync.py", "serve.py", "reprocess.py",
                 "setup.py", "README.md", "LICENSE"]
PAYLOAD_DIRS = ["gline"]

LAUNCHER = """#!/bin/sh
# Fukidashi の起動スクリプト。
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
APPDIR="$HERE/../Resources/app"

# macOS の python3 は Command Line Tools の一部なので、無い場合がある。
if ! /usr/bin/python3 -c "import sys" >/dev/null 2>&1; then
  osascript -e 'display dialog "Fukidashi を動かすには Python が必要です。\\n\\n「インストール」を押すと、macOS の開発者ツールの導入画面が開きます。数分で終わります。終わったら Fukidashi をもう一度開いてください。" with title "Fukidashi" buttons {"あとで","インストール"} default button "インストール" with icon caution' \\
    | grep -q "インストール" && xcode-select --install >/dev/null 2>&1
  exit 0
fi

# 起動に失敗したときに原因が分かるよう、ログを残す。
LOG="$HOME/Library/Logs/Fukidashi.log"
mkdir -p "$(dirname "$LOG")"
echo "--- $(date) 起動 ---" >> "$LOG"
exec /usr/bin/python3 "$APPDIR/app.py" >> "$LOG" 2>&1
"""


def build_icon(resources):
    """付属アイコン。吹き出しをそのまま描く。"""
    svg = '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024">
  <rect width="1024" height="1024" rx="224" fill="#2f7d4f"/>
  <path d="M232 296h560a56 56 0 0 1 56 56v296a56 56 0 0 1-56 56H470L322 828V704h-90a56 56 0 0 1-56-56V352a56 56 0 0 1 56-56z" fill="#ffffff"/>
  <rect x="264" y="392" width="320" height="44" rx="22" fill="#2f7d4f" opacity=".85"/>
  <rect x="264" y="486" width="440" height="44" rx="22" fill="#2f7d4f" opacity=".55"/>
</svg>'''
    svg_path = resources / "icon.svg"
    svg_path.write_text(svg, encoding="utf-8")

    iconset = resources / "icon.iconset"
    iconset.mkdir(exist_ok=True)
    made = False
    for size in (16, 32, 64, 128, 256, 512, 1024):
        png = iconset / ("icon_%dx%d.png" % (size, size))
        out = subprocess.run(
            ["sips", "-s", "format", "png", "--resampleHeightWidth",
             str(size), str(size), str(svg_path), "--out", str(png)],
            capture_output=True)
        if out.returncode == 0:
            made = True
    if made:
        subprocess.run(["iconutil", "-c", "icns", str(iconset),
                        "-o", str(resources / "icon.icns")], capture_output=True)
    shutil.rmtree(iconset, ignore_errors=True)
    svg_path.unlink(missing_ok=True)
    return (resources / "icon.icns").exists()


def build():
    if sys.platform != "darwin":
        print("これは macOS 専用です。")
        return 1

    if APP.exists():
        shutil.rmtree(APP)
    contents = APP / "Contents"
    macos = contents / "MacOS"
    resources = contents / "Resources"
    payload = resources / "app"
    for d in (macos, resources, payload):
        d.mkdir(parents=True, exist_ok=True)

    for name in PAYLOAD_FILES:
        src = ROOT / name
        if src.exists():
            shutil.copy2(src, payload / name)
    for name in PAYLOAD_DIRS:
        shutil.copytree(ROOT / name, payload / name,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

    launcher = macos / NAME
    launcher.write_text(LAUNCHER, encoding="utf-8")
    launcher.chmod(0o755)

    has_icon = build_icon(resources)

    info = {
        "CFBundleName": NAME,
        "CFBundleDisplayName": NAME,
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleVersion": "0.1.0",
        "CFBundleShortVersionString": "0.1.0",
        "CFBundleExecutable": NAME,
        "CFBundlePackageType": "APPL",
        "LSMinimumSystemVersion": "11.0",
        "NSHighResolutionCapable": True,
        # 画面はブラウザ側にあるので、Dock には出さない常駐アプリにする。
        # 終了は画面右上の「終了」ボタンから。
        "LSUIElement": True,
        "LSApplicationCategoryType": "public.app-category.productivity",
        "NSHumanReadableCopyright": "MIT License",
    }
    if has_icon:
        info["CFBundleIconFile"] = "icon"
    with (contents / "Info.plist").open("wb") as fh:
        plistlib.dump(info, fh)

    size = sum(f.stat().st_size for f in APP.rglob("*") if f.is_file())
    print("できました: %s (%.1f MB%s)"
          % (APP, size / 1e6, "" if has_icon else " / アイコンなし"))

    if "--zip" in sys.argv:
        zip_path = DIST / ("%s-macOS.zip" % NAME)
        if zip_path.exists():
            zip_path.unlink()
        subprocess.run(["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent",
                        str(APP), str(zip_path)], check=True)
        print("配布用: %s (%.1f MB)" % (zip_path, zip_path.stat().st_size / 1e6))
    return 0


if __name__ == "__main__":
    sys.exit(build())
