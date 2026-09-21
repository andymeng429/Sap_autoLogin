# -*- coding: utf-8 -*-
"""Mock 测试：登录结束后卡片不留多余那一行 + 主窗口怎么让位。

离屏渲染，不弹真窗口，也不连 SAP。
直接运行：python tests/test_card_state.py
"""
import os
import pathlib
import shutil
import sys
import tempfile

# 必须在导入 PySide6 之前设好，否则会尝试开真窗口
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

try:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication
except ImportError as exc:  # pragma: no cover - 缺依赖时直接报失败
    print("缺少 PySide6，无法运行界面测试：%s" % exc)
    sys.exit(1)

import gui_app
from config_store import AppConfig, AppOptions, ConfigStore, ConnectionEntry

APP = QApplication.instance() or QApplication([])

COMPLETE = ConnectionEntry(
    connection="BH-1D", client="120", user="ANDY", password="secret", tcode="SE09"
)
INCOMPLETE = ConnectionEntry(connection="BH-3P", user="ANDY", password="secret")


def make_card(entry=COMPLETE):
    return gui_app.EntryCard(entry)


def row_visible(card):
    """状态行有没有占位：按"相对卡片可见"判断，别用 isVisible()（祖先没显示时永远是 False）。"""
    return not card.state_label.isHidden()


def make_window(after_login="minimize"):
    """临时目录里造一个空配置，避免碰到真实的 config.json。"""
    folder = tempfile.mkdtemp(prefix="opensapgui-gui-")
    window = gui_app.MainWindow(ConfigStore(pathlib.Path(folder) / "config.json"))
    window.config.options.after_login = after_login
    return window, folder


def teardown(window, folder):
    window.close()
    window.deleteLater()
    shutil.rmtree(folder, ignore_errors=True)


# --------------------------------------------------------------------------- #
# 卡片状态行
# --------------------------------------------------------------------------- #
def test_complete_card_starts_without_state_row():
    card = make_card()
    assert row_visible(card) is False
    assert card.state_label.text() == ""


def test_busy_shows_progress_row():
    card = make_card()
    card.set_busy(True)
    assert row_visible(card) is True
    assert card.state_label.text() == "正在登录…"
    assert card.state_label.objectName() == "cardSub", "登录中不是警告"


def test_clear_message_removes_the_extra_row():
    """用户点名的那个问题：登录成功后卡片多一行、行高变高。"""
    card = make_card()
    card.show()
    APP.processEvents()
    compact_height = card.sizeHint().height()

    card.set_busy(True, "正在登录…")
    APP.processEvents()
    assert card.sizeHint().height() > compact_height, "登录中多一行本来就该高一点"

    card.clear_message()
    APP.processEvents()
    assert row_visible(card) is False, "登录结束后不该留状态行"
    assert card._busy is False
    assert card.sizeHint().height() == compact_height, "卡片要缩回两行的紧凑高度"
    card.close()


def test_set_message_empty_hides_row():
    card = make_card()
    card.set_message("已登录")
    assert row_visible(card) is True
    card.set_message("")
    assert row_visible(card) is False


def test_set_busy_false_also_hides_row():
    card = make_card()
    card.set_busy(True, "等待中…")
    card.set_busy(False)
    assert row_visible(card) is False
    assert card._busy is False


def test_clear_message_restores_buttons():
    card = make_card()
    card.set_busy(True)
    assert card.edit_button.isEnabled() is False
    card.clear_message()
    assert card.edit_button.isEnabled() is True
    assert card.delete_button.isEnabled() is True
    assert card.pin_button.isEnabled() is True


def test_incomplete_card_keeps_its_hint():
    """"还缺 xxx"是常驻信息，收工后要留着——但它不是登录状态。"""
    card = make_card(INCOMPLETE)
    assert row_visible(card) is True
    assert card.state_label.objectName() == "cardWarn"

    card.set_busy(True, "正在登录…")
    assert card.state_label.objectName() == "cardSub"

    card.clear_message()
    assert row_visible(card) is True
    assert card.state_label.objectName() == "cardWarn"
    assert "还缺" in card.state_label.text()


# --------------------------------------------------------------------------- #
# 登录结束后的整体处理
# --------------------------------------------------------------------------- #
def test_finish_clears_cards_and_status_text():
    window, folder = make_window()
    card = make_card()
    card.set_busy(True, "正在登录…")
    window.cards.append(card)

    window._on_login_finished(True, "登录成功", "BH-1D / 120")
    try:
        assert row_visible(card) is False
        assert window.status_label.text() == "BH-1D / 120"
        assert "已登录" not in window.status_label.text(), "状态栏也不要再挂「已登录」"
    finally:
        teardown(window, folder)


def test_finish_hides_warning_row_on_failure():
    """失败详情在弹窗和状态栏里说了，卡片上不再挂黄字。"""
    window, folder = make_window()
    card = make_card()
    card.set_busy(True)
    window.cards.append(card)

    # 弹窗会阻塞，换成记录参数的空实现
    original = gui_app.QMessageBox
    gui_app.QMessageBox = _silent_message_box
    _silent_message_box.last = None
    try:
        window._on_login_finished(False, "登录失败", "密码错误\n第二行细节")
    finally:
        gui_app.QMessageBox = original

    try:
        assert row_visible(card) is False
        assert window.status_label.text() == "登录失败：密码错误"
        assert _silent_message_box.last is not None, "失败必须弹窗"
    finally:
        teardown(window, folder)


class _silent_message_box:
    """顶掉 QMessageBox，只记录调用，exec() 直接返回。"""

    Critical = 3
    Information = 1
    Question = 4
    Warning = 2
    Yes = 0x4000
    No = 0x10000

    last = None

    def __init__(self, *_args, **_kwargs):
        self.text = ""

    def setIcon(self, *_args): pass
    def setWindowTitle(self, *_args): pass
    def setText(self, *_args): pass
    def setInformativeText(self, *_args): pass
    def setTextInteractionFlags(self, *_args): pass

    def exec(self):
        _silent_message_box.last = self
        return 0


# --------------------------------------------------------------------------- #
# 登录成功后的窗口行为
# --------------------------------------------------------------------------- #
def test_after_login_minimize():
    window, folder = make_window("minimize")
    window.show()
    try:
        window._after_login_action()
        assert window.isMinimized() or (window.windowState() & Qt.WindowMinimized)
    finally:
        window.showNormal()
        teardown(window, folder)


def test_after_login_behind_calls_the_helper():
    window, folder = make_window("behind")
    calls = []
    original = gui_app.send_main_window_behind_sap
    gui_app.send_main_window_behind_sap = lambda hwnd: calls.append(hwnd) or True
    try:
        window.status_label.setText("BH-1D / 120")
        expected_hwnd = int(window.winId())
        window._after_login_action()

        assert len(calls) == 1, "要真的按句柄去调窗口层级"
        assert calls[0] == expected_hwnd
        assert "已让到" in window.status_label.text()
    finally:
        gui_app.send_main_window_behind_sap = original
        teardown(window, folder)


def test_after_login_behind_says_nothing_when_not_found():
    window, folder = make_window("behind")
    original = gui_app.send_main_window_behind_sap
    gui_app.send_main_window_behind_sap = lambda hwnd: False
    try:
        window.status_label.setText("BH-1D / 120")
        window._after_login_action()
        assert window.status_label.text() == "BH-1D / 120", "没做成就不吹牛"
    finally:
        gui_app.send_main_window_behind_sap = original
        teardown(window, folder)


def test_after_login_none_does_nothing():
    window, folder = make_window("none")
    calls = []
    original = gui_app.send_main_window_behind_sap
    gui_app.send_main_window_behind_sap = lambda hwnd: calls.append(hwnd) or True
    try:
        window.show()
        window._after_login_action()
        assert window.isMinimized() is False
        assert calls == []
    finally:
        gui_app.send_main_window_behind_sap = original
        teardown(window, folder)


def test_after_login_unknown_value_falls_back_to_minimize():
    window, folder = make_window("随便填的")
    window.show()
    try:
        window._after_login_action()
        assert window.isMinimized() or (window.windowState() & Qt.WindowMinimized)
    finally:
        window.showNormal()
        teardown(window, folder)


def test_options_dialog_exposes_after_login_choice():
    dialog = gui_app.OptionsDialog(AppOptions(after_login="behind"))
    try:
        assert dialog.after_login_combo.currentData() == "behind"
        keys = [dialog.after_login_combo.itemData(i)
                for i in range(dialog.after_login_combo.count())]
        assert keys == list(gui_app.AFTER_LOGIN_CHOICES)
        assert dialog.result_options().after_login == "behind"
    finally:
        dialog.close()
        dialog.deleteLater()


def test_options_dialog_result_keeps_other_fields():
    options = AppOptions(after_login="none", popup_timeout=5, default_clients="100,110")
    dialog = gui_app.OptionsDialog(options)
    try:
        result = dialog.result_options()
        assert result.after_login == "none"
        assert result.popup_timeout == 5
        assert result.default_clients == "100,110"
    finally:
        dialog.close()
        dialog.deleteLater()


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
