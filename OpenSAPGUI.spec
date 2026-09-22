# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（**文件夹模式 / onedir**）。

打包命令::

    .venv\\Scripts\\python.exe -m PyInstaller OpenSAPGUI.spec --noconfirm --clean

产出::

    dist\\OpenSAPGUI\\OpenSAPGUI.exe      <- 双击这个
    dist\\OpenSAPGUI\\_internal\\...       <- 运行库，别动

为什么用 onedir 而不是单文件：

    单文件模式每次启动都会把运行库解压到 %TEMP%\\_MEIxxxx，退出时再删。
    一旦有杀软往进程里注入 DLL（360/火绒这类）占住里面的 VCRUNTIME140.dll，
    引导器就删不掉临时目录，退出时弹 "Failed to remove temporary directory"
    的警告框——这个框来自 PyInstaller 的引导器，Python 层拦不住（官方
    issue #8701）。onedir 不解压、无临时目录，弹窗从根上消失，启动也更快。

分发：**整个 OpenSAPGUI 文件夹一起拷**（别只拷 exe）。config.json 由程序
    首次运行时在 exe 同级目录自己生成；要给多个人用的话，别把自己那份
    config.json 一起发出去。

其它注意：

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

# --------------------------------------------------------------------------- #
# 剔除用不到的 Qt 运行时 DLL
#
# 上面的 excludes 只管 Python 模块，PySide6 的 hook 会把整个包目录下的
# *.dll 全收进来，所以 QML / Quick / PDF / 虚拟键盘这些纯 DLL 得自己筛掉。
# 界面只用到 QtCore + QtGui + QtWidgets。
#
# 特别说明：**opengl32sw.dll（约 20MB）故意保留** —— 它是软件 OpenGL 兜底
# 渲染器，虚拟机 / 远程桌面 / 老显卡驱动下 Qt 会退到它，删了可能界面起不来。
# --------------------------------------------------------------------------- #
_DROP_BINARY_PREFIXES = (
    "PySide6\\Qt6Qml",              # QML 运行时（QmlModels / QmlWorkerScript 等）
    "PySide6\\Qt6Quick",            # Qt Quick
    "PySide6\\Qt6Pdf",              # PDF 模块
    "PySide6\\Qt6VirtualKeyboard",  # 虚拟键盘
    "pythonwin",                    # pywin32 的 IDE 扩展，运行时用不到
)


def _keep_bundled(entry) -> bool:
    name = str(entry[0]).replace("/", "\\")
    return not any(name.startswith(prefix) for prefix in _DROP_BINARY_PREFIXES)


a.binaries = TOC(item for item in a.binaries if _keep_bundled(item))
a.datas = TOC(item for item in a.datas if _keep_bundled(item))

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,      # onedir：二进制交给下面的 COLLECT 落到文件夹里
    name='OpenSAPGUI',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=True,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='OpenSAPGUI',
    # 运行库收进 _internal 子目录，exe 同级只留一个入口 + config.json，看着清爽
    contents_directory='_internal',
)
