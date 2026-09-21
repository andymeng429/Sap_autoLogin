"""PySide6 图形界面：连接列表 + 配置编辑 + 单击登录。

界面结构：

* 主窗口列出所有已配置的连接入口（连接名 + client + 用户名），**单击卡片即登录**；
* 卡片右侧有「编辑」「删除」小按钮，点按钮不会触发登录；
* 登录在后台线程执行，界面不会卡死，状态实时显示在卡片和状态栏上。

本模块只负责界面与线程编排，真正的登录流程在 `sap_core.perform_login`。
"""

from __future__ import annotations

import gc
import logging
import os
import sys
import time
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QFont, QGuiApplication
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from config_store import (
    AFTER_LOGIN_CHOICES,
    AFTER_LOGIN_LABELS,
    AppConfig,
    AppOptions,
    ConfigError,
    ConfigStore,
    ConnectionEntry,
    application_dir,
    env_rules_to_text,
    match_environment,
    normalize_after_login,
    parse_env_rules,
    sort_entries,
)
from sap_core import SAPError, Settings, LoginTarget, perform_login
from sap_landscape import (
    client_candidates,
    connection_names,
    load_services,
    tcode_candidates,
)
from win_focus import send_main_window_behind_sap

LOGGER = logging.getLogger("sap.gui")

# 环境徽章配色：开发蓝、测试琥珀、生产红，按环境名里的常见词匹配
ENV_BADGE_PALETTES: list[tuple[tuple[str, ...], tuple[str, str, str]]] = [
    (("生产", "prod", "prd"), ("#FCEBEB", "#791F1F", "#A32D2D")),
    (("测试", "质量", "uat", "qa", "test"), ("#FAEEDA", "#633806", "#854F0B")),
    (("开发", "dev"), ("#E6F1FB", "#0C447C", "#185FA5")),
]
ENV_BADGE_NEUTRAL = ("#F1EFE8", "#444441", "#888780")


def env_badge_style(env: str) -> str:
    """环境名 -> 徽章样式（背景/文字/边框）。"""
    text = env.strip().lower()
    palette = ENV_BADGE_NEUTRAL
    for words, colors in ENV_BADGE_PALETTES:
        if any(word in text for word in words):
            palette = colors
            break
    background, color, border = palette
    return (
        f"background:{background}; color:{color}; border:0.5px solid {border};"
        "border-radius:6px; padding:0px 7px; font-size:11px;"
    )

# SAP Fiori 蓝，和 SAP 自己的界面风格一致
ACCENT = "#0a6ed1"
ACCENT_DARK = "#085caf"
DANGER = "#bb0000"
WARN = "#e9730c"
TEXT = "#1d2d3e"
TEXT_WEAK = "#5b738b"

STYLE_SHEET = f"""
QWidget {{
    font-family: "Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", sans-serif;
    font-size: 13px;
    color: {TEXT};
}}
QMainWindow, QDialog {{ background: #f5f6f7; }}

QScrollArea, #listHost {{ background: #f5f6f7; border: none; }}

QFrame#card {{
    background: #ffffff;
    border: 1px solid #d9d9d9;
    border-radius: 8px;
}}
QFrame#card:hover {{ border: 1px solid {ACCENT}; background: #f7fbff; }}
QFrame#card[pinned="true"] {{ border-left: 4px solid #f2a900; }}
QFrame#card[incomplete="true"] {{ border-left: 4px solid {WARN}; }}
QFrame#card[incomplete="false"] {{ border-left: 4px solid {ACCENT}; }}
QFrame#card[busy="true"] {{ background: #eef4fb; border: 1px solid {ACCENT}; }}

QLabel#cardTitle {{ font-size: 14px; font-weight: 600; }}
QLabel#cardSub {{ color: {TEXT_WEAK}; font-size: 12px; }}
QLabel#cardWarn {{ color: {WARN}; font-size: 12px; }}

QPushButton {{
    background: #ffffff;
    border: 1px solid #b8c4ce;
    border-radius: 6px;
    padding: 6px 14px;
}}
QPushButton:hover {{ border-color: {ACCENT}; color: {ACCENT}; }}
QPushButton:pressed {{ background: #eef4fb; }}
QPushButton:disabled {{ color: #9aa8b4; border-color: #dfe4e8; background: #f7f8f9; }}

QPushButton#primary {{
    background: {ACCENT}; border: 1px solid {ACCENT}; color: #ffffff; font-weight: 600;
}}
QPushButton#primary:hover {{ background: {ACCENT_DARK}; border-color: {ACCENT_DARK}; color: #ffffff; }}
QPushButton#primary:disabled {{ background: #a9c7e4; border-color: #a9c7e4; color: #ffffff; }}

QPushButton#mini {{
    padding: 2px 9px; font-size: 12px; border-radius: 5px;
    background: transparent; border: 1px solid transparent; color: {TEXT_WEAK};
}}
QPushButton#mini:hover {{ border-color: #b8c4ce; color: {ACCENT}; }}
QPushButton#miniDanger:hover {{ border-color: {DANGER}; color: {DANGER}; }}
QPushButton#miniPin {{ color: #9aa8b4; }}
QPushButton#miniPin:hover {{ border-color: #f2a900; color: #b57d00; }}
QPushButton#miniPinOn {{ color: #b57d00; font-weight: 600; }}
QPushButton#miniPinOn:hover {{ border-color: #f2a900; color: #8a5e00; }}

QLineEdit, QSpinBox {{
    background: #ffffff; border: 1px solid #b8c4ce; border-radius: 6px;
    padding: 6px 8px; selection-background-color: {ACCENT};
}}
QLineEdit:focus, QSpinBox:focus {{ border: 1px solid {ACCENT}; }}
QLineEdit:disabled {{ background: #f0f1f2; color: #9aa8b4; }}

QLabel#emptyTitle {{ font-size: 16px; font-weight: 600; color: {TEXT_WEAK}; }}
QLabel#emptyHint {{ color: {TEXT_WEAK}; }}
QStatusBar {{ background: #ffffff; border-top: 1px solid #e2e6ea; color: {TEXT_WEAK}; }}
"""


# --------------------------------------------------------------------------- #
# 后台登录线程
# --------------------------------------------------------------------------- #
class LoginThread(QThread):
    """在后台线程里跑登录，避免阻塞界面。"""

    progressed = Signal(str)
    finished_with = Signal(bool, str, str)   # (是否成功, 标题, 详情)

    def __init__(self, settings: Settings, target: LoginTarget, tcode: Optional[str]) -> None:
        super().__init__()
        self._settings = settings
        self._target = target
        self._tcode = tcode

    def run(self) -> None:  # noqa: D102 - QThread 约定
        try:
            perform_login(
                self._settings,
                self._target,
                tcode=self._tcode,
                on_progress=self.progressed.emit,
            )
        except SAPError as exc:
            LOGGER.error("登录失败: %s", exc)
            self.finished_with.emit(False, f"登录 {self._target.label} 失败", str(exc))
        except Exception as exc:  # noqa: BLE001 - 兜底，别让界面线程收不到结果
            LOGGER.exception("登录时发生未预期的错误")
            self.finished_with.emit(
                False, f"登录 {self._target.label} 出错", f"{type(exc).__name__}: {exc}"
            )
        else:
            tcode_hint = f"，并已进入 {self._tcode}" if self._tcode else ""
            self.finished_with.emit(True, "登录成功", f"{self._target.label}{tcode_hint}")


# --------------------------------------------------------------------------- #
# 连接卡片
# --------------------------------------------------------------------------- #
class EntryCard(QFrame):
    """一条连接入口。单击卡片主体 = 登录；右侧小按钮 = 编辑/删除。"""

    activated = Signal(object)
    edit_requested = Signal(object)
    delete_requested = Signal(object)
    pin_requested = Signal(object)

    def __init__(self, entry: ConnectionEntry, env: str = "",
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.entry = entry
        self._busy = False

        self.setObjectName("card")
        self.setProperty("incomplete", "false" if entry.is_complete else "true")
        self.setProperty("busy", "false")
        self.setProperty("pinned", "true" if entry.pinned else "false")
        self.setCursor(Qt.PointingHandCursor)
        # Minimum（而不是 Fixed）：警告文字显示/隐藏时卡片高度要能跟着变
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)

        layout = QHBoxLayout(self)
        # 紧凑布局：上下留白压到 6px，一屏能多放几张卡
        layout.setContentsMargins(12, 6, 8, 6)
        layout.setSpacing(10)

        text_box = QVBoxLayout()
        text_box.setSpacing(1)

        self.title_label = QLabel(entry.display_name)
        self.title_label.setObjectName("cardTitle")
        title_row = QHBoxLayout()
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.setSpacing(6)
        title_row.addWidget(self.title_label)
        if env:
            self.env_badge = QLabel(env)
            self.env_badge.setObjectName("envBadge")
            self.env_badge.setStyleSheet(env_badge_style(env))
            title_row.addWidget(self.env_badge)
        title_row.addStretch(1)
        text_box.addLayout(title_row)

        subtitle = " · ".join(
            part for part in (
                f"连接 {entry.connection}" if entry.connection else "未填连接名",
                f"client {entry.client}" if entry.client else None,
                f"事务码 {entry.tcode}" if entry.tcode else None,
                f"用户 {entry.user}" if entry.user else None,
            ) if part
        )
        self.sub_label = QLabel(subtitle)
        self.sub_label.setObjectName("cardSub")
        text_box.addWidget(self.sub_label)

        self.state_label = QLabel()
        self.state_label.setObjectName("cardWarn")
        self.state_label.setVisible(False)
        text_box.addWidget(self.state_label)

        if not entry.is_complete:
            self._show_state(
                "还缺：" + "、".join(entry.missing_fields()) + "，请点「编辑」补全",
                warning=True,
            )

        layout.addLayout(text_box, 1)

        self.pin_button = self._mini_button(
            "已置顶" if entry.pinned else "置顶",
            self._on_pin,
            pin=True,
            pinned_style=entry.pinned,
        )
        self.edit_button = self._mini_button("编辑", self._on_edit)
        self.delete_button = self._mini_button("删除", self._on_delete, danger=True)
        layout.addWidget(self.pin_button, 0, Qt.AlignVCenter)
        layout.addWidget(self.edit_button, 0, Qt.AlignVCenter)
        layout.addWidget(self.delete_button, 0, Qt.AlignVCenter)

    def _mini_button(self, text: str, slot, danger: bool = False,
                     pin: bool = False, pinned_style: bool = False) -> QPushButton:
        button = QPushButton(text)
        if pin:
            button.setObjectName("miniPinOn" if pinned_style else "miniPin")
            button.setToolTip("置顶后这条连接固定排在列表最前面")
        else:
            button.setObjectName("miniDanger" if danger else "mini")
        button.setCursor(Qt.PointingHandCursor)
        button.setFocusPolicy(Qt.NoFocus)
        button.clicked.connect(slot)
        return button

    # ---------------- 状态 ---------------- #
    def _show_state(self, message: str, warning: bool = False) -> None:
        """统一的状态行入口：message 为空就把这一行收起来，卡片回到两行高度。"""
        self.state_label.setObjectName("cardWarn" if warning else "cardSub")
        self.state_label.setText(message)
        self.state_label.setVisible(bool(message))
        self.style().unpolish(self.state_label)
        self.style().polish(self.state_label)
        # 隐藏后要主动让布局重算，否则卡片会留着多出来那一行的高度
        self.state_label.updateGeometry()
        self.updateGeometry()

    def set_busy(self, busy: bool, message: str = "") -> None:
        self._busy = busy
        self.setProperty("busy", "true" if busy else "false")
        self.style().unpolish(self)
        self.style().polish(self)
        self.setCursor(Qt.BusyCursor if busy else Qt.PointingHandCursor)

        self.pin_button.setEnabled(not busy)
        self.edit_button.setEnabled(not busy)
        self.delete_button.setEnabled(not busy)

        if busy:
            self._show_state(message or "正在登录…")
        else:
            self._show_state("")

    def set_message(self, message: str, warning: bool = False) -> None:
        self.set_busy(False)
        self._show_state(message, warning=warning)

    def clear_message(self) -> None:
        """收工：登录结束后不留任何临时提示（成败都一样）。

        成功不再写「已登录」——多一行会把卡片撑高；失败的详情已经在弹窗和
        状态栏说了，卡片上再挂一行黄字纯属噪音。只有"字段没填全"属于常驻
        信息，继续留着。
        """
        self.set_busy(False)
        if not self.entry.is_complete:
            self._show_state(
                "还缺：" + "、".join(self.entry.missing_fields()) + "，请点「编辑」补全",
                warning=True,
            )

    # ---------------- 事件 ---------------- #
    def _on_pin(self) -> None:
        self.pin_requested.emit(self.entry)

    def _on_edit(self) -> None:
        self.edit_requested.emit(self.entry)

    def _on_delete(self) -> None:
        self.delete_requested.emit(self.entry)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt 命名约定
        # 点在"编辑/删除"按钮上时事件被子控件吃掉，不会走到这里
        if event.button() == Qt.LeftButton and not self._busy:
            self.activated.emit(self.entry)
        super().mouseReleaseEvent(event)


# --------------------------------------------------------------------------- #
# 连接编辑对话框
# --------------------------------------------------------------------------- #
class EntryDialog(QDialog):
    """新建/编辑一条连接。"""

    def __init__(self, entry: Optional[ConnectionEntry] = None,
                 parent: Optional[QWidget] = None,
                 options: Optional[AppOptions] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("编辑连接" if entry else "新建连接")
        self.setMinimumWidth(430)

        self._entry = entry.copy() if entry else ConnectionEntry()
        # 连接名 / Client 的下拉候选来源，来自全局设置
        self._options = options or AppOptions()

        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        form.setSpacing(10)

        self.label_edit = QLineEdit(self._entry.label)
        self.label_edit.setPlaceholderText("给这条入口起个好认的名字，可留空")
        form.addRow("显示名称", self.label_edit)

        # 连接名和 Client 是"可输可选"的下拉：候选取自 SAP Logon 的连接列表，
        # 读不到时下拉为空，照常手输，行为和普通输入框一样。
        self._services = load_services([self._options.landscape_file])

        self.connection_edit = QComboBox()
        self.connection_edit.setEditable(True)
        self.connection_edit.setInsertPolicy(QComboBox.NoInsert)
        self.connection_edit.setPlaceholderText("SAP Logon 里的连接名，可下拉选择")
        self.connection_edit.addItems(connection_names(self._services))
        self.connection_edit.setCurrentText(self._entry.connection)
        form.addRow("连接名 *", self.connection_edit)

        self.client_edit = QComboBox()
        self.client_edit.setEditable(True)
        self.client_edit.setInsertPolicy(QComboBox.NoInsert)
        self.client_edit.setPlaceholderText("如 120，可下拉选择")
        form.addRow("Client *", self.client_edit)

        self.tcode_edit = QComboBox()
        self.tcode_edit.setEditable(True)
        self.tcode_edit.setInsertPolicy(QComboBox.NoInsert)
        self.tcode_edit.setPlaceholderText("登录后进入的事务码，可留空")
        form.addRow("事务码", self.tcode_edit)

        # 连接名变化 -> 刷新 client 候选（并清空）；client 变化 -> 刷新事务码候选
        self.connection_edit.currentTextChanged.connect(self._update_client_items)
        self.client_edit.currentTextChanged.connect(self._update_tcode_items)

        # 先按连接名填候选（这一步会清空），紧接着把配置里存的值写回，
        # 所以编辑已有条目不会丢 client / 事务码
        self._update_client_items(self.connection_edit.currentText())
        self.client_edit.setCurrentText(self._entry.client)
        self.tcode_edit.setCurrentText(self._entry.tcode)

        self.user_edit = QLineEdit(self._entry.user)
        self.user_edit.setPlaceholderText("SAP 用户名")
        form.addRow("用户名 *", self.user_edit)

        password_row = QWidget()
        password_layout = QHBoxLayout(password_row)
        password_layout.setContentsMargins(0, 0, 0, 0)
        password_layout.setSpacing(8)
        self.password_edit = QLineEdit(self._entry.password)
        self.password_edit.setEchoMode(QLineEdit.Password)
        self.password_edit.setPlaceholderText("保存时自动加密")
        show_password = QCheckBox("显示")
        show_password.toggled.connect(
            lambda checked: self.password_edit.setEchoMode(
                QLineEdit.Normal if checked else QLineEdit.Password
            )
        )
        password_layout.addWidget(self.password_edit, 1)
        password_layout.addWidget(show_password, 0)
        form.addRow("密码 *", password_row)

        self.enabled_check = QCheckBox("在主界面显示（可单击登录）")
        self.enabled_check.setChecked(self._entry.enabled)
        form.addRow("", self.enabled_check)

        layout.addLayout(form)

        hint = QLabel(
            "密码使用 Windows DPAPI 加密后保存在 config.json 中，绑定当前 Windows 账号，"
            "换电脑或换登录用户后需要重新填写。"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {TEXT_WEAK}; font-size: 12px;")
        layout.addWidget(hint)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setText("保存")
        buttons.button(QDialogButtonBox.Save).setObjectName("primary")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _client_items(self, connection: str) -> list[str]:
        """该连接的 client 候选；SAP Logon 里读不到就用全局设置里的默认候选兜底。"""
        return client_candidates(
            self._services,
            connection,
            fallback=self._options.client_candidate_fallback(),
        )

    def _update_client_items(self, connection: str) -> None:
        """连接名变化时刷新 client 候选，并清空已填的 client。

        清空是刻意的：把 BH-1D/120 改成 BH-3P 时，120 对 BH-3P 无效，
        留着会被误存。构造时紧接着会把配置里存的值写回，所以编辑已有条目不丢值。
        """
        self.client_edit.clear()
        self.client_edit.addItems(self._client_items(connection))
        self.client_edit.setCurrentText("")

    def _update_tcode_items(self, _client: str = "") -> None:
        """client 变化时刷新事务码候选（取自 SAP Logon 快捷方式的 cmd 字段）。

        已填的事务码保留——事务码跨 client 通用，换连接也没必要清。
        """
        current = self.tcode_edit.currentText()
        self.tcode_edit.clear()
        self.tcode_edit.addItems(tcode_candidates(
            self._services,
            self.connection_edit.currentText(),
            self.client_edit.currentText(),
        ))
        self.tcode_edit.setCurrentText(current)

    def _on_accept(self) -> None:
        missing = []
        if not self.connection_edit.currentText().strip():
            missing.append("连接名")
        if not self.client_edit.currentText().strip():
            missing.append("Client")
        if not self.user_edit.text().strip():
            missing.append("用户名")
        if not self.password_edit.text():
            missing.append("密码")

        if missing:
            QMessageBox.warning(self, "还差几项", "请填写：" + "、".join(missing))
            return
        self.accept()

    def result_entry(self) -> ConnectionEntry:
        self._entry.label = self.label_edit.text().strip()
        self._entry.connection = self.connection_edit.currentText().strip()
        self._entry.client = self.client_edit.currentText().strip()
        self._entry.user = self.user_edit.text().strip()
        self._entry.password = self.password_edit.text()
        self._entry.tcode = self.tcode_edit.currentText().strip()
        self._entry.enabled = self.enabled_check.isChecked()
        return self._entry


# --------------------------------------------------------------------------- #
# 全局设置对话框
# --------------------------------------------------------------------------- #
class OptionsDialog(QDialog):
    """全局设置：SAP Logon 路径、各类超时、日志文件。"""

    def __init__(self, options: AppOptions, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("全局设置")
        self.setMinimumWidth(480)
        self._options = options

        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        form.setSpacing(10)

        self.path_edit = QLineEdit(options.saplogon_path)
        form.addRow("SAP Logon 路径", self._file_row(self.path_edit, self._browse_saplogon))

        self.startup_spin = self._spin(options.startup_timeout, 5, 300)
        form.addRow("启动超时（秒）", self.startup_spin)

        self.connect_spin = self._spin(options.connect_timeout, 5, 300)
        form.addRow("连接超时（秒）", self.connect_spin)

        self.popup_spin = self._spin(options.popup_timeout, 1, 120)
        form.addRow("弹窗等待（秒）", self.popup_spin)

        self.after_login_combo = QComboBox()
        for key in AFTER_LOGIN_CHOICES:
            self.after_login_combo.addItem(AFTER_LOGIN_LABELS[key], key)
        index = self.after_login_combo.findData(normalize_after_login(options.after_login))
        self.after_login_combo.setCurrentIndex(max(index, 0))
        form.addRow("登录完成后", self.after_login_combo)

        self.log_edit = QLineEdit(options.log_file)
        self.log_edit.setPlaceholderText("留空则不写日志文件")
        form.addRow("日志文件", self._file_row(self.log_edit, self._browse_log_file))

        self.landscape_edit = QLineEdit(options.landscape_file)
        self.landscape_edit.setPlaceholderText(
            r"留空则自动探测 %APPDATA%\SAP\Common 和注册表里的位置"
        )
        form.addRow(
            "SAP Logon 景观文件",
            self._file_row(self.landscape_edit, self._browse_landscape),
        )

        self.clients_edit = QLineEdit(options.default_clients)
        self.clients_edit.setPlaceholderText("如 100,110,120,610,800（逗号或空格分隔）")
        form.addRow("默认 Client 候选", self.clients_edit)

        self.env_edit = QPlainTextEdit(env_rules_to_text(options.env_rules))
        self.env_edit.setPlaceholderText(
            "每行一条「关键词=环境」，按顺序先命中先用：\n"
            "*D=开发          连接名以 D 结尾算开发\n"
            "*Q=测试\n"
            "*P=生产\n"
            "client:800=生产   也可以只按 client 号判"
        )
        self.env_edit.setFixedHeight(96)
        form.addRow("环境判定规则", self.env_edit)

        layout.addLayout(form)

        hint = QLabel(
            "三个路径都可以点「浏览…」用文件对话框选。\n"
            "相对路径会解析到程序所在目录；也支持 %TEMP%\\xxx.log 这类写法。\n"
            "SAP Logon 路径填错也不影响使用——启动时会自动到注册表和常见安装位置里找。\n"
            "景观文件（SAP Logon 自己的连接列表）用来填「连接名 / Client」下拉，"
            "可指向共享盘上公司统一那份；\n"
            "「默认 Client 候选」在目标电脑的 SAP Logon 里没建快捷方式时兜底。\n"
            "「环境判定规则」用来在卡片上标开发/测试/生产徽章——SAP 本身不提供环境信息，"
            "按你们的命名约定配。\n"
            "「登录完成后」决定主窗口怎么让位：自动最小化，或者排到 SAP GUI 窗口后面，"
            "免得挡住刚登录进去的界面。"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {TEXT_WEAK}; font-size: 12px;")
        layout.addWidget(hint)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setText("保存")
        buttons.button(QDialogButtonBox.Save).setObjectName("primary")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    # ---------------- 路径选择 ---------------- #
    def _file_row(self, edit: QLineEdit, slot) -> QWidget:
        """输入框 + 「浏览…」按钮（调用系统的文件对话框）。"""
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(edit, 1)

        button = QPushButton("浏览…")
        button.setToolTip("打开文件对话框选择")
        button.setFocusPolicy(Qt.NoFocus)
        button.clicked.connect(slot)
        layout.addWidget(button, 0)
        return row

    def _browse_saplogon(self) -> None:
        selected, _ = QFileDialog.getOpenFileName(
            self,
            "选择 saplogon.exe",
            self._start_dir(self.path_edit.text()),
            "SAP Logon (saplogon.exe);;可执行文件 (*.exe);;所有文件 (*)",
        )
        if selected:
            self.path_edit.setText(str(Path(selected)))

    def _browse_log_file(self) -> None:
        selected, _ = QFileDialog.getSaveFileName(
            self,
            "选择日志文件",
            self._default_log_path(),
            "日志文件 (*.log);;所有文件 (*)",
        )
        if selected:
            self.log_edit.setText(str(Path(selected)))

    def _browse_landscape(self) -> None:
        start = self._start_dir(self.landscape_edit.text())
        if not self.landscape_edit.text().strip():
            common = Path(os.environ.get("APPDATA", "")) / "SAP" / "Common"
            if common.is_dir():
                start = str(common)      # 直接落在 SAP Logon 自己的配置目录
        selected, _ = QFileDialog.getOpenFileName(
            self,
            "选择 SAP Logon 景观文件",
            start,
            "SAP Logon 配置 (SAPUILandscape*.xml *.ini);;"
            "XML 文件 (*.xml);;INI 文件 (*.ini);;所有文件 (*)",
        )
        if selected:
            self.landscape_edit.setText(str(Path(selected)))

    @staticmethod
    def _resolve(value: str) -> Path:
        """把配置里的路径解析成绝对路径。

        相对路径按"程序所在目录"算，和运行期 `attach_log_file` 的行为保持一致；
        同时也展开 %TEMP% 这类环境变量。
        """
        text = (value or "").strip()
        if not text:
            return Path()
        expanded = Path(os.path.expandvars(text)).expanduser()
        if not expanded.is_absolute():
            return application_dir() / expanded
        return expanded

    @classmethod
    def _start_dir(cls, value: str) -> str:
        """把当前值换算成文件对话框的起始目录；无效时退回用户主目录。"""
        path = cls._resolve(value)
        if path.parts:
            folder = path if path.is_dir() else path.parent
            try:
                if folder.is_dir():
                    return str(folder)
            except OSError:
                pass
        return str(Path.home())

    def _default_log_path(self) -> str:
        """日志文件对话框的起始文件名：沿用当前值，否则给个默认名。"""
        resolved = self._resolve(self.log_edit.text())
        if not resolved.parts:
            return str(Path.home() / "OpenSAPGUI.log")
        return str(resolved)

    def _on_accept(self) -> None:
        """保存前提示一下明显不存在的路径，但不阻止（可能是暂不可达的网络盘）。"""
        if not self._confirm_path(
            "SAP Logon 路径",
            self.path_edit.text(),
            "启动时程序会自动到注册表和常见安装位置里找 saplogon.exe，所以仍然可以保存。",
        ):
            return
        if not self._confirm_path(
            "SAP Logon 景观文件",
            self.landscape_edit.text(),
            "这个文件只用于填充「连接名 / Client」下拉框，读不到就退回手输和默认候选。",
        ):
            return
        self.accept()

    def _confirm_path(self, label: str, raw: str, note: str) -> bool:
        """路径填了但文件不存在时问一句；留空或存在都直接放行。"""
        text = raw.strip()
        if not text:
            return True
        path = self._resolve(text)
        try:
            if path.exists():
                return True
        except OSError:
            return True          # 网络盘暂时不通时别拦着
        answer = QMessageBox.warning(
            self,
            f"{label}不存在",
            f"找不到这个文件：\n{path}\n\n{note}\n\n要按当前内容保存吗？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        return answer == QMessageBox.Yes

    @staticmethod
    def _spin(value: int, minimum: int, maximum: int) -> QSpinBox:
        spin = QSpinBox()
        spin.setRange(minimum, maximum)
        spin.setValue(value)
        return spin

    def result_options(self) -> AppOptions:
        return AppOptions(
            saplogon_path=self.path_edit.text().strip() or AppOptions().saplogon_path,
            startup_timeout=self.startup_spin.value(),
            connect_timeout=self.connect_spin.value(),
            popup_timeout=self.popup_spin.value(),
            log_file=self.log_edit.text().strip(),
            client_rules=self._options.client_rules,
            landscape_file=self.landscape_edit.text().strip(),
            default_clients=self.clients_edit.text().strip(),
            env_rules=parse_env_rules(self.env_edit.toPlainText()),
            after_login=normalize_after_login(self.after_login_combo.currentData()),
        )


# --------------------------------------------------------------------------- #
# 主窗口
# --------------------------------------------------------------------------- #
class MainWindow(QWidget):
    """主界面：列出连接，单击即登录。"""

    def __init__(self, store: Optional[ConfigStore] = None) -> None:
        super().__init__()
        self.store = store or ConfigStore()
        self.config = AppConfig()
        self.cards: list[EntryCard] = []
        self.login_thread: Optional[LoginThread] = None

        self.setWindowTitle("SAP 自动登录")
        self.resize(560, 520)
        self.setMinimumSize(460, 360)

        self._build_ui()
        self.reload()

        if self.store.migrated_last_load:
            self._notify_about_migration()
        elif self.store.locked_password_last_load:
            self._notify_about_locked_passwords()

    def _notify_about_locked_passwords(self) -> None:
        """配置文件来自别的电脑/别的 Windows 账号时，说明只有密码需要重填。"""
        count = self.store.locked_password_last_load
        QMessageBox.warning(
            self, "密码需要重新填写",
            f"有 {count} 条连接的密码无法解密——密码是用 Windows 账号密钥加密的，"
            "配置文件从别的电脑或别的 Windows 登录账号拷过来就会解不开"
            "（重装系统同理）。\n\n"
            "连接名、client、用户名、备注都已保留，"
            "点对应卡片的「编辑」把密码重填一次就能继续用。",
        )

    def _notify_about_migration(self) -> None:
        """从旧 .env 迁移完成后说明情况，特别是哪些条目还缺 client。"""
        incomplete = [entry for entry in self.config.entries if not entry.is_complete]
        if not incomplete:
            QMessageBox.information(
                self, "配置已迁移",
                "已把旧的 .env 配置迁移到 config.json，密码已加密保存。\n"
                "旧文件改名为 .env.migrated 留档，之后改动 .env 不再生效。",
            )
            return

        names = "\n".join(f"· {entry.connection}" for entry in incomplete)
        QMessageBox.information(
            self, "配置已迁移，还有几项要补",
            "已把旧的 .env 配置迁移到 config.json，密码已加密保存。\n\n"
            "旧配置里只记录了 client 的开头数字，没有完整 client，"
            "所以下面这些连接还缺 client，请点卡片上的「编辑」补全：\n"
            f"{names}\n\n"
            "补全前这些条目暂时不能单击登录。旧文件已改名为 .env.migrated 留档。",
        )

    # ---------------- 界面搭建 ---------------- #
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 10)
        root.setSpacing(10)

        header = QHBoxLayout()
        header.setSpacing(8)

        title = QLabel("选择一个连接")
        title_font = QFont()
        title_font.setPointSize(12)
        title_font.setBold(True)
        title.setFont(title_font)
        header.addWidget(title)
        header.addStretch(1)

        self.add_button = QPushButton("＋ 新建连接")
        self.add_button.setObjectName("primary")
        self.add_button.setCursor(Qt.PointingHandCursor)
        self.add_button.clicked.connect(self.on_add)
        header.addWidget(self.add_button)

        self.options_button = QPushButton("全局设置")
        self.options_button.setCursor(Qt.PointingHandCursor)
        self.options_button.clicked.connect(self.on_options)
        header.addWidget(self.options_button)

        root.addLayout(header)

        self.status_label = QLabel()
        self.status_label.setStyleSheet(f"color: {TEXT_WEAK};")
        root.addWidget(self.status_label)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.list_host = QWidget()
        self.list_host.setObjectName("listHost")
        self.list_layout = QVBoxLayout(self.list_host)
        self.list_layout.setContentsMargins(0, 0, 6, 0)
        self.list_layout.setSpacing(5)
        self.list_layout.addStretch(1)
        self.scroll.setWidget(self.list_host)
        root.addWidget(self.scroll, 1)

        self.empty_state = self._build_empty_state()
        root.addWidget(self.empty_state)

    def _build_empty_state(self) -> QWidget:
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setAlignment(Qt.AlignCenter)
        layout.setSpacing(8)

        title = QLabel("还没有任何连接")
        title.setObjectName("emptyTitle")
        title.setAlignment(Qt.AlignCenter)
        layout.addWidget(title)

        hint = QLabel("点右上角「新建连接」添加一条，之后单击卡片就能直接登录。")
        hint.setObjectName("emptyHint")
        hint.setAlignment(Qt.AlignCenter)
        hint.setWordWrap(True)
        layout.addWidget(hint)
        return box

    # ---------------- 数据 ---------------- #
    def reload(self) -> None:
        try:
            self.config = self.store.load()
        except ConfigError as exc:
            QMessageBox.critical(self, "配置读取失败", str(exc))
            self.config = AppConfig()

        for card in self.cards:
            card.setParent(None)
            card.deleteLater()
        self.cards.clear()

        entries = sort_entries(self.config.entries)
        rules = self.config.options.env_rules
        for entry in entries:
            env = match_environment(rules, entry.connection, entry.client)
            card = EntryCard(entry, env=env)
            card.activated.connect(self.on_card_activated)
            card.edit_requested.connect(self.on_edit)
            card.delete_requested.connect(self.on_delete)
            card.pin_requested.connect(self.on_pin)
            self.list_layout.insertWidget(self.list_layout.count() - 1, card)
            self.cards.append(card)

        has_entries = bool(entries)
        self.empty_state.setVisible(not has_entries)
        self.scroll.setVisible(has_entries)
        self.add_button.setEnabled(self.login_thread is None)

        enabled = len([entry for entry in entries if entry.enabled])
        pinned = len([entry for entry in entries if entry.pinned])
        if has_entries:
            parts = [f"共 {len(entries)} 条连接，{enabled} 条已启用"]
            if pinned:
                parts.append(f"{pinned} 条置顶")
            parts.append("单击卡片即可登录")
            self.status_label.setText(" · ".join(parts))
        else:
            self.status_label.setText("")

    def save_config(self) -> None:
        try:
            self.store.save(self.config)
        except ConfigError as exc:
            QMessageBox.critical(self, "配置保存失败", str(exc))

    def on_pin(self, entry: ConnectionEntry) -> None:
        entry.pinned = not entry.pinned
        self.save_config()
        self.reload()

    def _settings(self) -> Settings:
        return Settings.from_config(self.config)

    # ---------------- 交互 ---------------- #
    def on_add(self) -> None:
        dialog = EntryDialog(parent=self, options=self.config.options)
        if dialog.exec() != QDialog.Accepted:
            return
        self.config.entries.append(dialog.result_entry())
        self.save_config()
        self.reload()

    def on_edit(self, entry: ConnectionEntry) -> None:
        dialog = EntryDialog(entry, parent=self, options=self.config.options)
        if dialog.exec() != QDialog.Accepted:
            return

        updated = dialog.result_entry()
        for index, item in enumerate(self.config.entries):
            if item.entry_id == entry.entry_id:
                self.config.entries[index] = updated
                break
        self.save_config()
        self.reload()

    def on_delete(self, entry: ConnectionEntry) -> None:
        answer = QMessageBox.question(
            self,
            "删除连接",
            f"确定要删除「{entry.display_name}」吗？此操作不可撤销。",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        self.config.entries = [
            item for item in self.config.entries if item.entry_id != entry.entry_id
        ]
        self.save_config()
        self.reload()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 命名约定
        """关窗口时如果还有登录在跑，问一句再走。

        直接放行的话 QThread 会在窗口销毁时被硬掐，轻则报
        "QThread: Destroyed while thread is still running"，重则让退出
        过程半途而废，PyInstaller 的临时目录也清不干净。
        """
        thread = self.login_thread
        if thread is not None and thread.isRunning():
            answer = QMessageBox.question(
                self,
                "还在登录",
                "还有连接正在登录，现在退出要等它结束（最多约一分钟）。\n确定要退出吗？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                event.ignore()
                return
            self.setCursor(Qt.BusyCursor)
            thread.wait()          # 登录各步都有超时，最坏几十秒会自己结束
            self.login_thread = None
            self.unsetCursor()
        event.accept()

    def on_options(self) -> None:
        dialog = OptionsDialog(self.config.options, parent=self)
        if dialog.exec() != QDialog.Accepted:
            return
        self.config.options = dialog.result_options()
        self.save_config()
        self.reload()      # 环境规则可能变了，卡片徽章要跟着刷新

    def on_card_activated(self, entry: ConnectionEntry) -> None:
        if self.login_thread is not None:
            return  # 正在登录，忽略重复点击

        if not entry.is_complete:
            QMessageBox.information(
                self,
                "还差几项",
                f"「{entry.display_name}」还缺 {'、'.join(entry.missing_fields())}。\n"
                "请点卡片右侧的「编辑」补全后再登录。",
            )
            return

        if not entry.enabled:
            QMessageBox.information(self, "已停用", "这条连接已被停用，请先编辑并勾选启用。")
            return

        target = LoginTarget(
            label=entry.display_name,
            connection=entry.connection,
            client=entry.client,
            user=entry.user,
            password=entry.password,
            tcode=entry.tcode,
        )
        self._start_login(target)

    def _start_login(self, target: LoginTarget) -> None:
        self._set_login_state(True, target.label)

        thread = LoginThread(self._settings(), target, tcode=None)
        thread.progressed.connect(self._on_progress)
        thread.finished_with.connect(self._on_login_finished)
        thread.finished.connect(thread.deleteLater)
        self.login_thread = thread
        thread.start()

    def _set_login_state(self, busy: bool, label: str = "") -> None:
        self.add_button.setEnabled(not busy)
        self.options_button.setEnabled(not busy)
        for card in self.cards:
            if busy and card.entry.display_name == label:
                card.set_busy(True, "正在登录…")
            elif busy:
                card.set_busy(True, "等待中…")
            else:
                card.set_busy(False)

    def _on_progress(self, message: str) -> None:
        self.status_label.setText(message)
        for card in self.cards:
            if card._busy:
                card._show_state(message)

    def _on_login_finished(self, success: bool, title: str, detail: str) -> None:
        self.login_thread = None
        self.add_button.setEnabled(True)
        self.options_button.setEnabled(True)

        # 不管成败，卡片都收回两行的紧凑样子：结果看状态栏，细节看弹窗
        for card in self.cards:
            card.clear_message()

        if success:
            self.status_label.setText(detail)
            self._after_login_action()
            return

        self.status_label.setText(f"{title}：{detail.splitlines()[0]}")

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Critical)
        box.setWindowTitle(title)
        box.setText(title)
        box.setInformativeText(detail)
        box.setTextInteractionFlags(Qt.TextSelectableByMouse)
        box.exec()

    def _after_login_action(self) -> None:
        """登录成功后按设置给主窗口让位，别挡住刚起来的 SAP GUI。"""
        mode = normalize_after_login(self.config.options.after_login)
        if mode == "minimize":
            self.showMinimized()
            return
        if mode == "behind" and not self.isMinimized():
            if send_main_window_behind_sap(int(self.winId())):
                self.status_label.setText(self.status_label.text() + "（已让到 SAP GUI 后面）")


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def run_gui(store: Optional[ConfigStore] = None) -> int:
    """启动图形界面，返回进程退出码。"""
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("OpenSAPGUI")
    app.setStyleSheet(STYLE_SHEET)

    window = MainWindow(store)
    window.show()

    screen = QGuiApplication.primaryScreen()
    if screen is not None:
        geometry = window.frameGeometry()
        geometry.moveCenter(screen.availableGeometry().center())
        window.move(geometry.topLeft())

    # 打包成 windowed exe 后没有控制台，界面"一闪而过"只能靠日志留痕，
    # 所以这里把起止都记一条，出问题时有据可查。
    LOGGER.info("界面已显示（%d 条连接），进入事件循环", len(window.cards))
    started = time.monotonic()
    return_code = app.exec()
    elapsed = time.monotonic() - started

    # 事件循环结束后把 Qt 侧的资源放干净：窗口、样式、残余的对象引用。
    window.deleteLater()
    app.processEvents()
    del window
    gc.collect()

    LOGGER.info("事件循环结束：返回码 %s，运行 %.1f 秒", return_code, elapsed)
    if elapsed < 1.0:
        LOGGER.warning(
            "界面在 %.2f 秒内就退出了。若不是你主动关闭，"
            "请把这份日志发给维护者。",
            elapsed,
        )
    return return_code
