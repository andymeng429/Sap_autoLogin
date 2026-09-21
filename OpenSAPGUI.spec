# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置。

打包命令::

    .venv\\Scripts\\python.exe -m PyInstaller OpenSAPGUI.spec --noconfirm --clean

注意：

* 不打包 config.json —— 它含 DPAPI 加密的密码（绑定打包机器/账号），
  放在 exe 同级目录由程序自己生成，分发时让接收方自己填。
* console=False：这是带图形界面的程序，不需要黑窗口。入口里的
  `_ensure_std_streams()` 会在 sys.stdout 为 None 时补上空流，
  避免 windowed 模式下 logging / traceback 直接崩。
"""


a = Analysis(
    ['OpenSAPGUI.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[
        # pywin32：COM 调用 + DPAPI 加密 + 窗口层级
        'win32com',
        'win32com.client',
        'win32gui',
        'pythoncom',
        'pywintypes',
        'win32crypt',
        # 配置解析
        'dotenv',
        # 本项目的模块（gui_app 是在函数内延迟导入的，显式列出更保险）
        'sap_core',
        'config_store',
        'gui_app',
        'sap_landscape',
        'win_focus',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 界面只用到 QtCore / QtGui / QtWidgets，去掉 QML、WebEngine 这些用不上的大块
    excludes=[
        'tkinter',
        'PySide6.QtQml',
        'PySide6.QtQuick',
        'PySide6.QtQuickWidgets',
        'PySide6.QtQuickControls2',
        'PySide6.QtWebEngineCore',
        'PySide6.QtWebEngineWidgets',
        'PySide6.QtSql',
        'PySide6.QtTest',
        'PySide6.QtDBus',
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='OpenSAPGUI',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=True,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
