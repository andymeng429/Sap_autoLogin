# -*- coding: utf-8 -*-
"""Mock 测试：把主窗口让到 SAP GUI 后面的窗口层级逻辑。

不依赖真实 SAP、不真的动窗口——win32 层用假的模块注入。
直接运行：python tests/test_win_focus.py
"""
import logging
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import win_focus
from win_focus import (
    SAP_WINDOW_CLASSES,
    bring_window_to_front,
    find_sap_window,
    is_sap_window_class,
    send_main_window_behind_sap,
)

# 有意走到失败分支时别把 traceback 打得到处都是
logging.getLogger("sap.winfocus").addHandler(logging.NullHandler())
logging.getLogger("sap.winfocus").propagate = False


class FakeGui:
    """假的 win32gui：记下 SetWindowPos 等调用，用注册表模拟窗口清单。"""

    def __init__(self, explode=False, windows=None, foreground=0,
                 minimized=None, lock_foreground=False):
        self.calls = []
        self.explode = explode
        self.windows = dict(windows or {})   # hwnd -> title
        self.minimized = set(minimized or ())
        self.foreground = foreground
        self.lock_foreground = lock_foreground   # 模拟 Windows 前台锁定

    def EnumWindows(self, callback, _param):
        if self.explode:
            raise OSError("枚举失败")
        for hwnd, title in list(self.windows.items()):
            callback(hwnd, None)

    def IsWindowVisible(self, hwnd):
        return hwnd in self.windows

    def GetWindowText(self, hwnd):
        return self.windows.get(hwnd, "")

    def IsIconic(self, hwnd):
        return hwnd in self.minimized

    def ShowWindow(self, hwnd, cmd):
        self.calls.append(("ShowWindow", hwnd, cmd))
        self.minimized.discard(hwnd)

    def SetForegroundWindow(self, hwnd):
        self.calls.append(("SetForegroundWindow", hwnd))
        if not self.lock_foreground:
            self.foreground = hwnd

    def GetForegroundWindow(self):
        return self.foreground

    def BringWindowToTop(self, hwnd):
        self.calls.append(("BringWindowToTop", hwnd))

    def FlashWindow(self, hwnd, flash):
        self.calls.append(("FlashWindow", hwnd, flash))

    def SetWindowPos(self, *args):
        if self.explode:
            raise OSError("句柄已失效")
        self.calls.append(("SetWindowPos",) + args)
        return True


class FakeCon:
    SWP_NOMOVE = 0x0002
    SWP_NOSIZE = 0x0001
    SWP_NOACTIVATE = 0x0010
    SW_RESTORE = 9
    SW_SHOW = 5


def patch_win32(gui, con=None):
    """替换 _win32()，返回还原函数。"""
    original = win_focus._win32
    win_focus._win32 = lambda: (gui, con or FakeCon())
    return lambda: setattr(win_focus, "_win32", original)


def patch_find(hwnd):
    original = win_focus.find_sap_window
    win_focus.find_sap_window = lambda: hwnd
    return lambda: setattr(win_focus, "find_sap_window", original)


# --------------------------------------------------------------------------- #
# 类名匹配
# --------------------------------------------------------------------------- #
def test_matches_known_sap_classes():
    for name in SAP_WINDOW_CLASSES:
        assert is_sap_window_class(name), name


def test_rejects_other_windows():
    assert is_sap_window_class("Notepad") is False
    assert is_sap_window_class("") is False
    assert is_sap_window_class(None) is False
    assert is_sap_window_class("sap_frontend_session") is False, "类名精确匹配，别误伤"


def test_first_class_is_the_session_window():
    """会话窗口要排在前面，登录完用户看的是它而不是 SAP Logon 列表。"""
    assert SAP_WINDOW_CLASSES[0] == "SAP_FRONTEND_SESSION"


# --------------------------------------------------------------------------- #
# 查找与让位
# --------------------------------------------------------------------------- #
def test_find_sap_window_returns_int():
    """本机有没有 SAP 都不该抛异常，返回值必须是句柄或 0。"""
    result = find_sap_window()
    assert isinstance(result, int) and result >= 0


def test_behind_skipped_without_main_hwnd():
    restore = patch_win32(FakeGui())
    try:
        assert send_main_window_behind_sap(0) is False
    finally:
        restore()


def test_behind_skipped_without_sap_window():
    gui = FakeGui()
    restore_win32 = patch_win32(gui)
    restore_find = patch_find(0)
    try:
        assert send_main_window_behind_sap(111) is False
        assert gui.calls == [], "找不到 SAP 窗口时不要乱动窗口"
    finally:
        restore_find()
        restore_win32()


def test_behind_puts_main_window_after_sap():
    gui = FakeGui()
    restore_win32 = patch_win32(gui)
    restore_find = patch_find(999)
    try:
        assert send_main_window_behind_sap(111) is True
    finally:
        restore_find()
        restore_win32()

    (call,) = gui.calls
    assert call[0] == "SetWindowPos"
    _, main, insert_after, x, y, width, height, flags = call
    assert main == 111
    assert insert_after == 999, "参考窗口必须是 SAP 那个"
    assert (x, y, width, height) == (0, 0, 0, 0)
    assert flags & FakeCon.SWP_NOMOVE and flags & FakeCon.SWP_NOSIZE
    assert flags & FakeCon.SWP_NOACTIVATE, "只调层级，不抢 SAP 的焦点"


def test_behind_is_safe_when_sap_handle_equals_ours():
    gui = FakeGui()
    restore_win32 = patch_win32(gui)
    restore_find = patch_find(111)
    try:
        assert send_main_window_behind_sap(111) is False
    finally:
        restore_find()
        restore_win32()


def test_behind_reports_failure_instead_of_raising():
    gui = FakeGui(explode=True)
    restore_win32 = patch_win32(gui)
    restore_find = patch_find(999)
    try:
        assert send_main_window_behind_sap(111) is False
    finally:
        restore_find()
        restore_win32()


def test_behind_is_noop_without_win32gui():
    restore = patch_win32(None, None)
    restore_find = patch_find(999)
    try:
        assert send_main_window_behind_sap(111) is False, "没有 win32gui 就安静地不做"
    finally:
        restore_find()
        restore()


# --------------------------------------------------------------------------- #
# 单实例：把已有窗口拉回前台
# --------------------------------------------------------------------------- #
def test_bring_to_front_restores_minimized_window():
    gui = FakeGui(windows={111: "SAP 自动登录"}, minimized={111}, foreground=0)
    restore = patch_win32(gui)
    try:
        assert bring_window_to_front("SAP 自动登录") is True
    finally:
        restore()

    kinds = [c[0] for c in gui.calls]
    assert "ShowWindow" in kinds and ("SetForegroundWindow", 111) in gui.calls
    restore_cmd = [c for c in gui.calls if c[0] == "ShowWindow"][0]
    assert restore_cmd[2] == FakeCon.SW_RESTORE, "最小化的窗口要先还原"


def test_bring_to_front_ignores_other_titles():
    gui = FakeGui(windows={111: "记事本"})
    restore = patch_win32(gui)
    try:
        assert bring_window_to_front("SAP 自动登录") is False
        assert gui.calls == [], "没找到目标窗口就不许乱动别的窗口"
    finally:
        restore()


def test_bring_to_front_flash_when_foreground_locked():
    """抢不到前台（Windows 前台锁定）就闪任务栏提示，不算失败。"""
    gui = FakeGui(windows={111: "SAP 自动登录"}, foreground=999,
                  lock_foreground=True)
    restore = patch_win32(gui)
    try:
        assert bring_window_to_front("SAP 自动登录") is True
    finally:
        restore()

    assert ("FlashWindow", 111, True) in gui.calls, "抢不到前台要闪任务栏"


def test_bring_to_front_survives_enum_failure():
    gui = FakeGui(explode=True)
    restore = patch_win32(gui)
    try:
        assert bring_window_to_front("SAP 自动登录") is False
    finally:
        restore()


def test_bring_to_front_is_noop_without_win32gui():
    restore = patch_win32(None, None)
    try:
        assert bring_window_to_front("SAP 自动登录") is False
    finally:
        restore()


def test_bring_to_front_requires_a_title():
    restore = patch_win32(FakeGui(windows={1: "SAP 自动登录"}))
    try:
        assert bring_window_to_front("") is False
        assert bring_window_to_front(None) is False
    finally:
        restore()


# --------------------------------------------------------------------------- #
def main() -> int:
    tests = [(name, obj) for name, obj in sorted(globals().items())
             if name.startswith("test_") and callable(obj)]
    failed = 0
    for name, func in tests:
        try:
            func()
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print("FAIL %-52s %s: %s" % (name, type(exc).__name__, exc))
        else:
            print("PASS %s" % name)
    print("\n共 %d 项，失败 %d 项" % (len(tests), failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
