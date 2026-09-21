# -*- coding: utf-8 -*-
"""Mock 测试：编辑对话框里「连接名 / Client」下拉的行为。

用离屏模式渲染，不会真的弹窗，也不读本机 SAP Logon（数据全部注入）。
直接运行：python tests/test_entry_dialog.py
"""
import os
import pathlib
import sys

# 必须在导入 PySide6 之前设好，否则会尝试开真窗口
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

try:
    from PySide6.QtWidgets import QApplication
except ImportError as exc:  # pragma: no cover - 缺依赖时直接报失败
    print("缺少 PySide6，无法运行界面测试：%s" % exc)
    sys.exit(1)

import gui_app
from config_store import AppOptions, ConnectionEntry
from sap_landscape import SapService

APP = QApplication.instance() or QApplication([])

SYSTEMS_ONLY = [
    SapService(name="BH-1D", kind="system"),
    SapService(name="BH-2Q", kind="system"),
    SapService(name="BH-3P", kind="system"),
]

WITH_SHORTCUTS = SYSTEMS_ONLY + [
    SapService(name="BH120-PO", client="120", kind="shortcut", description="BH-1D", cmd="PO"),
    SapService(name="BH110-SE09", client="110", kind="shortcut", description="BH-1D", cmd="SE09"),
    SapService(name="BH800-PO", client="800", kind="shortcut", description="BH-3P"),
]


def combo_items(combo):
    return [combo.itemText(index) for index in range(combo.count())]


def use_services(services):
    """把景观数据注入对话框，替代真实读取。"""
    gui_app.load_services = lambda extra_files=(): services


def make_dialog(entry=None, options=None, services=None):
    use_services(services if services is not None else WITH_SHORTCUTS)
    return gui_app.EntryDialog(entry, options=options or AppOptions())


# --------------------------------------------------------------------------- #
# 下拉候选
# --------------------------------------------------------------------------- #
def test_connection_dropdown_lists_connections():
    dialog = make_dialog(services=SYSTEMS_ONLY)
    assert combo_items(dialog.connection_edit) == ["BH-1D", "BH-2Q", "BH-3P"]


def test_client_dropdown_uses_landscape_shortcuts():
    dialog = make_dialog(ConnectionEntry(connection="BH-1D", client="120"))
    assert combo_items(dialog.client_edit) == ["110", "120"]
    assert dialog.client_edit.currentText() == "120", "编辑已有条目要保留原值"


def test_client_dropdown_falls_back_to_default_clients():
    """目标电脑上没建快捷方式 -> 用全局设置里的默认候选。"""
    options = AppOptions(default_clients="100,110,120,610,800")
    dialog = make_dialog(ConnectionEntry(connection="BH-1D", client="610"),
                         options=options, services=SYSTEMS_ONLY)
    assert combo_items(dialog.client_edit) == ["100", "110", "120", "610", "800"]
    assert dialog.client_edit.currentText() == "610"


def test_landscape_candidates_win_over_fallback():
    options = AppOptions(default_clients="999")
    dialog = make_dialog(ConnectionEntry(connection="BH-1D"), options=options)
    assert combo_items(dialog.client_edit) == ["110", "120"], "本机有候选时不掺兜底值"


def test_no_fallback_without_connection():
    options = AppOptions(default_clients="100,110")
    dialog = make_dialog(options=options, services=[])
    assert combo_items(dialog.connection_edit) == []
    assert combo_items(dialog.client_edit) == [], "还没填连接名时不该冒出候选"


def test_landscape_file_setting_is_passed_through():
    captured = {}

    def fake_loader(extra_files=()):
        captured["extra"] = list(extra_files)
        return []

    gui_app.load_services = fake_loader
    gui_app.EntryDialog(None, options=AppOptions(landscape_file=r"D:\share\x.xml"))
    assert captured["extra"] == [r"D:\share\x.xml"]


# --------------------------------------------------------------------------- #
# 切换连接名时的联动
# --------------------------------------------------------------------------- #
def test_changing_connection_clears_client():
    """BH-1D/120 改成 BH-3P 后，120 必须清掉，否则会被误存。"""
    dialog = make_dialog(ConnectionEntry(connection="BH-1D", client="120"))
    dialog.connection_edit.setCurrentText("BH-3P")

    assert combo_items(dialog.client_edit) == ["800"]
    assert dialog.client_edit.currentText() == ""


def test_switching_back_refills_candidates():
    dialog = make_dialog(ConnectionEntry(connection="BH-1D", client="120"))
    dialog.connection_edit.setCurrentText("BH-3P")
    dialog.connection_edit.setCurrentText("BH-1D")
    assert combo_items(dialog.client_edit) == ["110", "120"]


def test_typed_value_not_in_candidates_survives():
    """景观文件里没有这个 client 时，手输的值要能留住（可输可选）。"""
    dialog = make_dialog(ConnectionEntry(connection="BH-1D", client="999"))
    assert dialog.client_edit.currentText() == "999"
    assert dialog.client_edit.isEditable()


# --------------------------------------------------------------------------- #
# 事务码
# --------------------------------------------------------------------------- #
def test_tcode_dropdown_prefilled_from_landscape():
    """client 120 对应的快捷方式 cmd=PO，自动进下拉。"""
    dialog = make_dialog(ConnectionEntry(connection="BH-1D", client="120"))
    assert combo_items(dialog.tcode_edit) == ["PO"]


def test_tcode_candidates_follow_client_change():
    dialog = make_dialog(ConnectionEntry(connection="BH-1D", client="120"))
    dialog.client_edit.setCurrentText("110")
    assert combo_items(dialog.tcode_edit) == ["SE09"]


def test_tcode_typed_value_survives_client_change():
    """事务码跨 client 通用，换 client 不清空已填的值。"""
    dialog = make_dialog(ConnectionEntry(
        connection="BH-1D", client="120", tcode="ZREPORT"))
    assert dialog.tcode_edit.currentText() == "ZREPORT"
    dialog.client_edit.setCurrentText("110")
    assert dialog.tcode_edit.currentText() == "ZREPORT"


def test_result_entry_carries_tcode():
    dialog = make_dialog(ConnectionEntry(connection="BH-1D", client="120", tcode="PO"))
    dialog.tcode_edit.setCurrentText(" ZREPORT ")
    entry = dialog.result_entry()
    assert entry.tcode == "ZREPORT", "取值时要strip"


# --------------------------------------------------------------------------- #
# 环境徽章
# --------------------------------------------------------------------------- #
def test_card_shows_env_badge():
    from PySide6.QtWidgets import QLabel
    card = gui_app.EntryCard(
        ConnectionEntry(connection="BH-3P", client="800", user="u", password="p"),
        env="生产",
    )
    badges = [w for w in card.findChildren(QLabel) if w.objectName() == "envBadge"]
    assert len(badges) == 1 and badges[0].text() == "生产"


def test_card_without_env_has_no_badge():
    from PySide6.QtWidgets import QLabel
    card = gui_app.EntryCard(
        ConnectionEntry(connection="BH-1D", client="120", user="u", password="p"))
    assert not [w for w in card.findChildren(QLabel) if w.objectName() == "envBadge"]


def test_env_badge_style_by_name():
    assert "#FCEBEB" in gui_app.env_badge_style("生产")
    assert "#FAEEDA" in gui_app.env_badge_style("测试")
    assert "#E6F1FB" in gui_app.env_badge_style("开发")
    assert "#F1EFE8" in gui_app.env_badge_style("自定义环境"), "陌生名字用中性灰"


# --------------------------------------------------------------------------- #
# 全局设置对话框
# --------------------------------------------------------------------------- #
def test_options_dialog_roundtrip():
    options = AppOptions(landscape_file=r"D:\share\SAPUILandscape.xml",
                         default_clients="110,120")
    dialog = gui_app.OptionsDialog(options)
    assert dialog.landscape_edit.text() == options.landscape_file
    assert dialog.clients_edit.text() == options.default_clients

    dialog.landscape_edit.setText("")
    dialog.clients_edit.setText("100, 110")
    saved = dialog.result_options()
    assert saved.landscape_file == ""
    assert saved.client_candidate_fallback() == ["100", "110"]


def test_options_dialog_keeps_legacy_client_rules():
    """旧的 SAP_CLIENT_MAP 兼容规则不能在保存全局设置时丢掉。"""
    options = AppOptions(client_rules=[["8", "BH-3P"]])
    saved = gui_app.OptionsDialog(options).result_options()
    assert saved.client_rules == [["8", "BH-3P"]]


def test_options_dialog_env_rules_roundtrip():
    options = AppOptions(env_rules=[["*D", "开发"], ["*P", "生产"]])
    dialog = gui_app.OptionsDialog(options)
    assert dialog.env_edit.toPlainText() == "*D=开发\n*P=生产"

    dialog.env_edit.setPlainText("*D=开发\nclient:800=生产")
    saved = dialog.result_options()
    assert saved.env_rules == [["*D", "开发"], ["client:800", "生产"]]


def test_confirm_path_skips_empty_and_missing():
    dialog = gui_app.OptionsDialog(AppOptions())
    assert dialog._confirm_path("景观文件", "", "note") is True, "留空直接放行"


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
