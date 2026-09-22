# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller の設定（Windows 用の .exe を1ファイルで作る）。

    pyinstaller packaging/fukidashi.spec --noconfirm

画面の HTML/CSS/JS は gline/web に入っているので、データとして同梱する。
"""

import os

ROOT = os.path.abspath(os.getcwd())

a = Analysis(
    [os.path.join(ROOT, "app.py")],
    pathex=[ROOT],
    binaries=[],
    datas=[
        (os.path.join(ROOT, "gline", "web"), os.path.join("gline", "web")),
        (os.path.join(ROOT, "README.md"), "."),
        (os.path.join(ROOT, "LICENSE"), "."),
    ],
    hiddenimports=[
        # パスワード保管（Windows 資格情報マネージャー）
        "keyring.backends.Windows",
        "keyring.backends.null",
        # 設定ダイアログ
        "tkinter", "tkinter.messagebox", "tkinter.simpledialog",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=["numpy", "pandas", "matplotlib", "PIL", "pytest"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="Fukidashi",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,            # UPX はウイルス対策ソフトの誤検知を招きやすい
    console=False,        # コンソール窓を出さない
    disable_windowed_traceback=False,
    icon=os.path.join(ROOT, "packaging", "fukidashi.ico"),
)
