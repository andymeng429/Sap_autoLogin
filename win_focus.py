"""窗口层级小工具：让主窗口别挡住 SAP GUI。

登录成功后 SAP GUI 的窗口是后面才由 SAP 自己创建的，Windows 的前台窗口
锁定规则会让它"就绪了但还压在咱们后面"。用户想要的是反过来——我们的
窗口让位。这里只做一件事：把主窗口在 z 顺序里排到 SAP GUI 窗口下面。

拿不到 `win32gui`（非 Windows / 精简运行环境）时全部退化成"什么都不做"，
调用方据此决定要不要改走最小化。
"""

from __future__ import annotations

import logging

LOGGER = logging.getLogger("sap.winfocus")

# SAP GUI for Windows 的顶层窗口类名。
# 会话窗口排最前：登录完成后用户真正要看的是它，不是 SAP Logon 那个列表。
SAP_WINDOW_CLASSES: tuple[str, ...] = (
    "SAP_FRONTEND_SESSION",
    "SapGuiWorkspace",
    "saplogon",
)


def is_sap_window_class(name: str) -> bool:
    """窗口类名是不是 SAP GUI 的（严格全等匹配，避免误伤同名程序）。"""
    return (name or "").strip() in SAP_WINDOW_CLASSES


def _win32():
    """延迟拿 win32gui / win32con；拿不到就给 (None, None)。"""
    try:
        import win32con  # noqa: PLC0415 - 可选依赖，按需导入
        import win32gui  # noqa: PLC0415
    except ImportError:
        LOGGER.debug("环境里没有 win32gui，跳过窗口层级调整")
        return None, None
    return win32gui, win32con


def find_sap_window() -> int:
    """找当前最该显示的 SAP 窗口句柄；找不到返回 0。

    按 `SAP_WINDOW_CLASSES` 的顺序给优先级：命中第一类就直接返回，
    同类里有多个时取最近激活过的那个。
    """
    win32gui, _ = _win32()
    if win32gui is None:
        return 0

    found: dict[str, list[int]] = {}

    def collect(hwnd, _param) -> None:
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return
            klass = win32gui.GetClassName(hwnd)
        except Exception:  # noqa: BLE001 - 枚举途中窗口可能已经销毁
            return
        if is_sap_window_class(klass):
            found.setdefault(klass, []).append(hwnd)

    try:
        win32gui.EnumWindows(collect, None)
    except Exception:  # noqa: BLE001 - 枚举失败不该影响登录流程
        LOGGER.warning("枚举窗口失败，无法定位 SAP GUI", exc_info=True)
        return 0

    for klass in SAP_WINDOW_CLASSES:
        for hwnd in found.get(klass, []):
            return hwnd
    return 0


def send_main_window_behind_sap(main_hwnd: int) -> bool:
    """把 `main_hwnd` 排到 SAP GUI 窗口后面。做到返回 True。

    找不到 SAP 窗口（或调用失败）时返回 False，此时什么都不改——
    主窗口宁可留在原地，也不要沉到所有窗口底下去找不着。
    """
    win32gui, win32con = _win32()
    if win32gui is None or not main_hwnd:
        return False

    sap_hwnd = find_sap_window()
    if not sap_hwnd or sap_hwnd == main_hwnd:
        LOGGER.info("没找到 SAP GUI 窗口，主窗口保持原位")
        return False

    try:
        win32gui.SetWindowPos(
            main_hwnd,
            sap_hwnd,
            0, 0, 0, 0,
            win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE,
        )
    except Exception:  # noqa: BLE001
        LOGGER.warning("调整主窗口层级失败", exc_info=True)
        return False

    LOGGER.info("已把主窗口排到 SAP GUI 窗口之后")
    return True
