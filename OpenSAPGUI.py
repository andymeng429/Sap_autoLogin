"""SAP GUI 自动登录：图形界面 + 命令行两种用法。

用法::

    OpenSAPGUI.exe                      # 不带参数 -> 打开图形界面，单击卡片即登录
    OpenSAPGUI.exe --gui                # 同上，显式指定
    OpenSAPGUI.exe 120 BP               # 命令行：登录 client 120 的条目并进入 BP
    OpenSAPGUI.exe 800 MM03             # 命令行：client 以 8 开头 -> 按映射规则走 BH-3P
    OpenSAPGUI.exe --connection BH-3P   # 命令行：直接点名连接
    OpenSAPGUI.exe --list               # 列出现有连接配置

配置存放在程序同级的 ``config.json``，密码用 Windows DPAPI 加密，**不含明文**。
首次运行若检测到旧的 ``.env``，会自动迁移成 config.json 并把 .env 改名留档。

界面模式下自带的控制台窗口会自动隐藏 —— 仅当该控制台属于本进程，
所以从 CMD 里手动运行不会把你的终端窗口一起藏掉。

本文件只做入口和参数分发，真正的实现在：

* ``config_store.py``  配置读写 + DPAPI 加密
* ``sap_core.py``      SAP 会话封装、登录流程（无 GUI 依赖）
* ``gui_app.py``       PySide6 界面
"""

from __future__ import annotations

import argparse
import ctypes
import logging
import os
import shutil
import sys
import tempfile
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Optional

from config_store import ConfigError, ConfigStore
from sap_core import (
    SAPError,
    Settings,
    SettingsError,
    attach_log_file,
    configure_logging,
    perform_login,
    resolve_target,
)
from win_focus import bring_window_to_front

LOGGER = logging.getLogger("sap")

# 界面主窗口标题（gui_app.MainWindow 也用它），单实例拉回前台时按这个找窗口
WINDOW_TITLE = "SAP 自动登录"

# 单实例互斥体：`Local\` 限定当前登录会话，多用户同时登录互不影响
SINGLE_INSTANCE_MUTEX_NAME = r"Local\OpenSAPGUI.SingleInstance"
ERROR_ALREADY_EXISTS = 183

_instance_lock_handle = None


def acquire_single_instance_lock(name: str = SINGLE_INSTANCE_MUTEX_NAME) -> bool:
    """占住"只开一个界面"的互斥体；已经有实例在跑就返回 False。

    Windows 命名互斥体：句柄由模块级变量持有，进程退出时由内核自动
    回收、互斥体随之消失，不需要显式释放（release 只给测试用）。
    创建失败（极罕见的权限问题）按允许启动处理——别让环境异常把程序
    变成打不开。
    """
    global _instance_lock_handle
    if sys.platform != "win32":
        return True                      # 非 Windows（开发/测试）不限制
    if _instance_lock_handle is not None:
        return True                      # 本进程已经持有
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.CreateMutexW(None, False, name)
    except Exception:  # noqa: BLE001
        LOGGER.warning("创建单实例互斥体失败，按允许启动处理", exc_info=True)
        return True
    if not handle:
        LOGGER.warning(
            "创建单实例互斥体失败（错误码 %s），按允许启动处理",
            ctypes.get_last_error(),
        )
        return True
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)     # 已经有实例在跑了
        return False
    _instance_lock_handle = handle
    return True


def release_single_instance_lock() -> None:
    """释放互斥体。进程退出时内核本来就会回收，这个主要给测试用。"""
    global _instance_lock_handle
    if _instance_lock_handle and sys.platform == "win32":
        try:
            ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(
                _instance_lock_handle
            )
        except Exception:  # noqa: BLE001
            pass
    _instance_lock_handle = None


def _warn_already_running() -> None:
    """兜底提示：检测到已有实例，但没找到它的窗口（极端时序）才走到这里。"""
    if sys.platform != "win32":
        return
    try:
        # windowed exe 没有控制台，用原生 MessageBox（此时 Qt 未加载）
        ctypes.windll.user32.MessageBoxW(
            None,
            "SAP 自动登录已经在运行了。",
            WINDOW_TITLE,
            0x40,        # MB_ICONINFORMATION
        )
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- #
# 运行环境准备
# --------------------------------------------------------------------------- #
def _ensure_std_streams() -> None:
    """兜底补上标准流。

    打包成 windowed exe（console=False）后 sys.stdout / sys.stderr 是 None，
    任何 print、日志或 traceback 都会直接抛异常。这里给个空流顶上，
    保证程序至少能跑起来、出错能记进日志文件。
    """
    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
    if getattr(sys, "stdin", None) is None:
        sys.stdin = open(os.devnull, "r", encoding="utf-8")


def _hide_own_console() -> None:
    """界面模式下隐藏自带的控制台窗口。

    只在该控制台**由本进程独占**时才隐藏：不判断的话，从 CMD / PowerShell
    里手动运行会把用户自己的终端窗口一起藏掉。
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        user32 = ctypes.windll.user32
        hwnd = kernel32.GetConsoleWindow()
        if not hwnd:
            return

        buffer = (ctypes.c_uint * 16)()
        attached = kernel32.GetConsoleProcessList(buffer, 16)
        if attached > 1:      # 还有别的进程挂在同一个控制台上，别动
            return

        user32.ShowWindow(hwnd, 0)   # SW_HIDE = 0
    except Exception:  # noqa: BLE001 - 隐藏失败无所谓，最多多个黑窗口
        pass


def _install_excepthook() -> None:
    """界面模式下兜住未捕获异常，别让窗口出现"点了没反应"。"""
    def handler(exc_type, exc_value, exc_traceback) -> None:
        LOGGER.critical("未捕获的异常", exc_info=(exc_type, exc_value, exc_traceback))
        try:
            from PySide6.QtWidgets import QApplication, QMessageBox

            if QApplication.instance() is not None:
                QMessageBox.critical(
                    None,
                    "程序出错",
                    f"{exc_type.__name__}: {exc_value}\n\n详细信息已写入日志文件。",
                )
        except Exception:  # noqa: BLE001
            pass

    sys.excepthook = handler


# --------------------------------------------------------------------------- #
# 命令行
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="OpenSAPGUI",
        description="SAP GUI 自动登录。不带参数运行会打开图形界面。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例:\n"
               "  OpenSAPGUI.exe                 打开图形界面\n"
               "  OpenSAPGUI.exe 120 BP          登录 client 120 并进入 BP\n"
               "  OpenSAPGUI.exe 800 MM03        client 以 8 开头 -> BH-3P\n"
               "  OpenSAPGUI.exe --list          列出已配置的连接\n",
    )
    parser.add_argument("target", nargs="?",
                        help="client 编号或连接名；省略时用第一条启用的连接")
    parser.add_argument("tcode", nargs="?", help="登录后执行的事务码，省略则只登录")
    parser.add_argument("--gui", action="store_true", help="打开图形界面")
    parser.add_argument("--config", metavar="PATH",
                        help="指定 config.json 路径，默认在程序同级目录")
    parser.add_argument("--connection", metavar="NAME",
                        help="直接指定连接名（等同于把连接名写在第一个参数）")
    parser.add_argument("--user", help="覆盖配置中的用户名")
    parser.add_argument("--password",
                        help="覆盖配置中的密码（注意：命令行参数会被同机其它进程看到，"
                             "仅建议临时排障时使用）")
    parser.add_argument("--timeout", type=int, metavar="SECONDS",
                        help="覆盖启动/连接超时（秒），想快速失败可设小一点")
    parser.add_argument("--log-file", metavar="PATH",
                        help="同时把日志写入指定文件（无窗口运行时用来排查问题）")
    parser.add_argument("--no-verify", action="store_true",
                        help="跳过登录结果与事务码的结果校验（老版本行为，仅排障时用）")
    parser.add_argument("--keep-open", action="store_true",
                        help="结束后等待回车再关闭窗口（仅控制台版有意义）")
    parser.add_argument("--list", action="store_true", help="列出已配置的连接后退出")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")
    return parser


def _has_visible_console() -> bool:
    """当前进程有没有可见的控制台窗口（windowed exe 里没有）。"""
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        return bool(ctypes.windll.kernel32.GetConsoleWindow())
    except Exception:  # noqa: BLE001
        return True


def notify_user(title: str, message: str, error: bool = True) -> None:
    """无控制台（windowed exe）时用系统弹窗把结果告诉用户。

    有控制台时不弹 —— 输出已经打在终端上了，重复弹窗很烦。
    """
    if _has_visible_console():
        return
    try:
        import ctypes
        flags = 0x10 if error else 0x40      # MB_ICONERROR / MB_ICONINFORMATION
        ctypes.windll.user32.MessageBoxW(None, message, title, flags)
    except Exception:  # noqa: BLE001
        pass


def print_connections(settings: Settings) -> str:
    """渲染连接清单，返回文本（命令行打印；无控制台时用于弹窗展示）。"""
    lines = [
        f"SAP Logon : {settings.saplogon_path}",
        f"超时      : 启动 {settings.startup_timeout}s / 连接 {settings.connect_timeout}s"
        f" / 弹窗 {settings.popup_timeout}s",
        f"日志文件  : {settings.log_file or '(未启用)'}",
        "",
        "已配置的连接:",
    ]
    for entry in settings.entries:
        state = "启用" if entry.enabled else "停用"
        missing = "" if entry.is_complete else f"  缺: {'/'.join(entry.missing_fields())}"
        lines.append(
            f"  [{state}] {entry.display_name}   连接={entry.connection}"
            f"  client={entry.client or '(未填)'}  用户={entry.user}{missing}"
        )
    rules = "  ".join(f"{prefix}* -> {name}" for prefix, name in settings.client_rules)
    lines.append("")
    lines.append(f"兼容前缀规则: {rules or '(无)'}")
    return "\n".join(lines)


def run_cli(args: argparse.Namespace, settings: Settings) -> int:
    if args.timeout is not None:
        if args.timeout <= 0:
            raise SettingsError(f"--timeout 必须为正整数，当前为 {args.timeout}")
        settings = replace(settings, startup_timeout=args.timeout, connect_timeout=args.timeout)

    if args.list:
        report = print_connections(settings)
        if _has_visible_console() and sys.stdout is not None:
            print(report)
        else:
            notify_user("连接配置", report, error=False)
        return 0

    target = resolve_target(settings, args.connection or args.target)

    if args.user:
        target = replace(target, user=args.user)
    if args.password:
        target = replace(target, password=args.password)

    perform_login(settings, target, tcode=args.tcode)
    return 0


def _wait_for_enter() -> None:
    """--keep-open：结束后停住窗口，方便看清输出。无控制台时直接跳过。"""
    if sys.stdin is None or sys.stdout is None:
        return
    try:
        input("\n按回车键关闭窗口...")
    except (EOFError, KeyboardInterrupt):
        pass


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def cleanup_stale_mei(temp_dir: Optional[str] = None) -> list[str]:
    """清扫 %TEMP% 里残留的 PyInstaller 解压目录（_MEIxxxx）。

    现在打包走的是**文件夹模式（onedir）**，程序自己不再产生这种临时目录，
    这个函数降级为兜底：清掉本机以前用单文件版时留下的垃圾，以及其它
    单文件 exe 没删干净的同名目录。规则：

    * 只动 `_MEI*` 命名模式，删不掉的（别的程序正用着）跳过；
    * 两分钟内的新目录不碰，避免和刚启动的其它实例抢。

    返回实际删除的目录列表（仅用于测试和日志）。
    """
    if sys.platform != "win32":
        return []
    temp = Path(temp_dir) if temp_dir else Path(tempfile.gettempdir())
    removed: list[str] = []
    now = time.time()
    for path in sorted(temp.glob("_MEI*")):
        try:
            if not path.is_dir() or now - path.stat().st_mtime < 120:
                continue
            shutil.rmtree(path, ignore_errors=False)
        except OSError:
            continue        # 还被占用或权限不够：不是我们的菜，留着
        removed.append(str(path))
    return removed


def _cleanup_stale_mei_async() -> None:
    """后台清扫，不拖慢启动。"""
    threading.Thread(
        target=cleanup_stale_mei, daemon=True, name="mei-sweep"
    ).start()


def _gui_parser() -> argparse.ArgumentParser:
    """界面模式只认这几个参数，其余一律忽略，避免误报"参数错误"。"""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--gui", action="store_true")
    parser.add_argument("--config")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def run_gui_entry(raw_args: list[str]) -> int:
    known, _unknown = _gui_parser().parse_known_args(raw_args)
    configure_logging(known.verbose)
    _hide_own_console()
    _install_excepthook()

    # 单实例：已经在跑就不再开第二个界面，把已有的那个带回来
    if not acquire_single_instance_lock():
        if not bring_window_to_front(WINDOW_TITLE):
            _warn_already_running()
        return 0

    if known.config:
        os.environ["SAP_CONFIG_FILE"] = known.config

    store = ConfigStore()
    try:
        log_file: Optional[str] = store.load().options.log_file
    except ConfigError as exc:
        LOGGER.error("配置读取失败: %s", exc)
        log_file = None
    attach_log_file(log_file)

    import gui_app  # 延迟导入：命令行模式不必加载 Qt

    return gui_app.run_gui(store)


def main(argv: Optional[list[str]] = None) -> int:
    _ensure_std_streams()
    _cleanup_stale_mei_async()
    raw_args = list(sys.argv[1:] if argv is None else argv)

    if not raw_args or "--gui" in raw_args:
        return run_gui_entry(raw_args)

    args = build_parser().parse_args(raw_args)
    configure_logging(args.verbose)

    if args.config:
        os.environ["SAP_CONFIG_FILE"] = args.config

    try:
        settings = Settings.from_config(ConfigStore().load())
        attach_log_file(args.log_file or settings.log_file)
        if args.no_verify:
            settings = replace(settings, verify=False)
        return run_cli(args, settings)
    except ConfigError as exc:
        LOGGER.error("配置错误: %s", exc)
        notify_user("配置错误", str(exc))
    except SettingsError as exc:
        LOGGER.error("参数错误: %s", exc)
        notify_user("参数错误", str(exc))
    except SAPError as exc:
        LOGGER.error("SAP 操作失败: %s", exc)
        notify_user("SAP 操作失败", str(exc))
    except KeyboardInterrupt:
        LOGGER.warning("已取消。")
    finally:
        if args.keep_open:
            _wait_for_enter()
    return 1


if __name__ == "__main__":
    _return_code = main()
    logging.shutdown()          # 先把日志刷干净，再交给引导器收尾
    sys.exit(_return_code)
