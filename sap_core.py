"""SAP GUI 自动化的核心逻辑：配置解析、会话封装、启动器。

这个模块**不依赖任何 GUI 库**，命令行模式和图形界面共用同一套登录流程，
保证两边行为一致（也方便单独写 mock 测试）。

配置来源是 `config_store.AppConfig`（config.json）。
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Optional

import win32com.client

from config_store import (
    AppConfig,
    ConfigError,
    ConnectionEntry,
    application_dir,
    resolve_saplogon_path,
    saplogon_candidates,
)

LOGGER = logging.getLogger("sap")

# --------------------------------------------------------------------------- #
# 默认值
# --------------------------------------------------------------------------- #
DEFAULT_STARTUP_TIMEOUT = 30
DEFAULT_CONNECT_TIMEOUT = 30
DEFAULT_POPUP_TIMEOUT = 8

# 等待会话空闲（Busy 落到 False）的时限，以及登录后判断结果的总时限
IDLE_TIMEOUT = 15
LOGIN_VERIFY_TIMEOUT = 12

# 探测"到底有没有重复登录弹窗"的观察窗口（秒）。
# 没有弹窗时必须在这个窗口后立刻收工，否则每次正常登录都要白等满 popup_timeout。
POPUP_GRACE = 2.0

# 登录界面控件 ID
ID_CLIENT = "wnd[0]/usr/txtRSYST-MANDT"
ID_USER = "wnd[0]/usr/txtRSYST-BNAME"
ID_PASSWORD = "wnd[0]/usr/pwdRSYST-BCODE"
ID_COMMAND_FIELD = "wnd[0]/tbar[0]/okcd"
ID_STATUS_BAR = "wnd[0]/sbar"

# "首次登录/密码到期后强制改密"界面的控件 ID（与登录界面区分开，避免误判成登录失败）
ID_NEW_PASSWORD = "wnd[0]/usr/txtRSYST-BCODE"
ID_REPEAT_PASSWORD = "wnd[0]/usr/txtRSYST-BCODE2"

# 多登录冲突弹窗中，"继续此登录，但不结束其它登录" 可能的单选框 ID
RADIO_CANDIDATE_IDS = (
    "radMULTI_LOGON_OPT2",
    "radSPOP-OPTION2",
    "rad[1]",
    "rbtnSPOP-OPTION2",
    "rbtn[1]",
    "radio[1]",
)

# 冲突弹窗中"继续"类按钮在各国语言下的常见文本
CONTINUE_LABELS = (
    "继续此登录",
    "继续",
    "Continue this logon",
    "Continue this session",
    "Continue",
    "Yes, continue",
)

# 重复登录/多重登录弹窗的特征词。只有弹窗内容命中这些词，才说明它确实是
# 我们要处理的"已在他处登录"提示。
MULTI_LOGON_HINTS = (
    "多重登录",
    "多处登录",
    "多次登录",
    "多個登入",
    "其它登录",
    "其他登录",
    "继续此登录",
    "结束其它登录",
    "multiple logon",
    "multiple logons",
    "logon elsewhere",
    "continue this logon",
    "end other logon",
)

# 明确的登录报错特征词。命中且整屏都没有"继续"类文字时，说明这是报错弹窗，
# 绝不能按位置盲点按钮 —— 否则会把"密码错误/用户被锁定"直接点掉，
# 让脚本误以为登录成功。
ERROR_HINTS = (
    "用户名或密码",
    "密码不正确",
    "密码错误",
    "口令错误",
    "密码已过期",
    "用户不存在",
    "用户被锁定",
    "已锁定",
    "由于多次",
    "登录失败",
    "user name or password",
    "password incorrect",
    "incorrect password",
    "password has expired",
    "user is locked",
    "locked due to",
    "logon failed",
    "not authorized",
    "no authorization",
    "没有授权",
    "无权限",
    "已被管理员",
    "terminated by administrator",
)

POPUP_WINDOW_INDEXES = range(1, 6)      # wnd[1] ~ wnd[5]
TOOLBAR_BUTTON_INDEXES = range(0, 6)    # tbar[0]/btn[0] ~ btn[5]

# 递归收集弹窗文字时的节点上限：弹窗都很小，给个上限防止意外遍历整个控件树。
TEXT_HARVEST_BUDGET = 200


def _contains_any(haystack: str, needles: tuple[str, ...]) -> bool:
    """大小写不敏感地判断 haystack 是否包含 needles 中任意一个词。"""
    lowered = haystack.lower()
    return any(needle.lower() in lowered for needle in needles)


def _is_confirmable_popup(snapshot: str) -> bool:
    """判断弹窗是否属于"可以放心点确认"的类型。

    判定顺序（越靠前优先级越高）：
    1. 命中多重登录特征词 -> 是（正是目标弹窗）；
    2. 命中"继续/确认"类按钮文本 -> 是；
    3. 命中明确的报错特征词 -> 否，交给上层报错，不盲目点按钮；
    4. 其余情况 -> 是，保持旧版"按位置兜底点击 btn[1]"的行为。

    第 4 条是刻意保留的：部分 SAP 版本/语言下弹窗既取不到单选框 ID，
    也取不到按钮文本，只能靠位置兜底，删掉会导致重复登录时卡住。
    """
    if _contains_any(snapshot, MULTI_LOGON_HINTS):
        return True
    if _contains_any(snapshot, CONTINUE_LABELS):
        return True
    if _contains_any(snapshot, ERROR_HINTS):
        return False
    return True


# --------------------------------------------------------------------------- #
# 异常类型
# --------------------------------------------------------------------------- #
class SAPError(RuntimeError):
    """SAP 自动化流程的基类异常。"""


class SettingsError(SAPError):
    """配置缺失或格式错误。"""


class StartupError(SAPError):
    """SAP Logon 无法启动。"""


class SessionError(SAPError):
    """无法建立连接或获取会话。"""


class LoginError(SAPError):
    """登录流程失败。"""


# --------------------------------------------------------------------------- #
# 登录目标
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class LoginTarget:
    """一次登录所需的全部信息。密码明文只存在于内存中。"""

    label: str
    connection: str
    client: str
    user: str
    password: str
    tcode: str = ""             # 登录成功后要进入的事务码，可空

    def __repr__(self) -> str:  # 避免密码出现在日志/异常里
        return (
            f"LoginTarget(label={self.label!r}, connection={self.connection!r}, "
            f"client={self.client!r}, user={self.user!r}, password='***')"
        )


@dataclass(frozen=True)
class Settings:
    """运行所需的全部配置。"""

    saplogon_path: Path
    startup_timeout: int
    connect_timeout: int
    popup_timeout: int
    log_file: Optional[str]
    entries: tuple[ConnectionEntry, ...]
    client_rules: tuple[tuple[str, str], ...] = ()
    verify: bool = True

    @classmethod
    def from_config(cls, config: AppConfig) -> "Settings":
        options = config.options
        return cls(
            saplogon_path=Path(options.saplogon_path),
            startup_timeout=options.startup_timeout,
            connect_timeout=options.connect_timeout,
            popup_timeout=options.popup_timeout,
            log_file=options.log_file.strip() or None,
            entries=tuple(config.entries),
            client_rules=tuple((rule[0], rule[1]) for rule in options.client_rules),
        )

    @property
    def usable_entries(self) -> tuple[ConnectionEntry, ...]:
        return tuple(entry for entry in self.entries if entry.enabled)

    def describe_available(self) -> str:
        """给报错信息用的"有哪些可用入口"清单。"""
        lines = []
        for entry in self.usable_entries:
            state = "" if entry.is_complete else f"  [缺: {'/'.join(entry.missing_fields())}]"
            lines.append(f"  {entry.display_name}  (连接 {entry.connection}, client {entry.client}){state}")
        return "\n".join(lines) or "  (没有任何启用的连接，请先在界面上添加)"


def resolve_target(settings: Settings, keyword: Optional[str] = None) -> LoginTarget:
    """按关键字挑一条登录入口。

    匹配顺序（越靠前优先级越高）：

    1. 条目 client 完全相等；
    2. 条目连接名完全相等（`--connection PRD-1` 或直接敲连接名）；
    3. 条目备注完全相等；
    4. `client_rules` 前缀规则（client 号以某几位开头 -> 连接名），
       此时 client 用用户输入的完整值；
    5. 条目 client 前缀匹配；
    6. 没给关键字 -> 第一条可用条目。

    都没命中就抛 SettingsError，并列出可用入口。
    """
    entries = list(settings.usable_entries)
    if not entries:
        raise SettingsError("没有可用的连接。请先在界面上添加一条连接配置。")

    key = (keyword or "").strip()
    if not key:
        return LoginTarget(
            label=entries[0].display_name,
            connection=entries[0].connection,
            client=entries[0].client,
            user=entries[0].user,
            password=entries[0].password,
            tcode=entries[0].tcode,
        )

    lowered = key.lower()

    def make(entry: ConnectionEntry, client: Optional[str] = None) -> LoginTarget:
        return LoginTarget(
            label=entry.display_name,
            connection=entry.connection,
            client=(client if client is not None else entry.client).strip(),
            user=entry.user,
            password=entry.password,
            tcode=entry.tcode,
        )

    for entry in entries:
        if entry.client.strip() == key:
            LOGGER.debug("client %s 精确命中条目 %s", key, entry.display_name)
            return make(entry)

    for entry in entries:
        if entry.connection.strip().lower() == lowered:
            LOGGER.debug("连接名 %s 命中条目 %s", key, entry.display_name)
            return make(entry)

    for entry in entries:
        if entry.display_name.lower() == lowered:
            LOGGER.debug("备注 %s 命中条目 %s", key, entry.display_name)
            return make(entry)

    for prefix, name in settings.client_rules:
        if key.startswith(prefix):
            entry = next(
                (item for item in entries if item.connection.strip().lower() == name.strip().lower()),
                None,
            )
            if entry is not None:
                LOGGER.debug("命中旧映射规则 %s* -> %s，client 取 %s", prefix, name, key)
                return make(entry, key)

    for entry in entries:
        if entry.client.strip() and entry.client.strip().startswith(key):
            LOGGER.debug("client 前缀 %s 命中条目 %s", key, entry.display_name)
            return make(entry)

    raise SettingsError(
        f"找不到匹配 {key!r} 的连接。可用入口：\n{settings.describe_available()}"
    )


# --------------------------------------------------------------------------- #
# SAP 会话封装
# --------------------------------------------------------------------------- #
class SAPSession:
    """封装一个 SAP GUI 会话的常用操作。"""

    def __init__(
        self,
        session: object,
        popup_timeout: int = DEFAULT_POPUP_TIMEOUT,
        verify: bool = True,
    ) -> None:
        self._session = session
        self._popup_timeout = popup_timeout
        self._verify = verify

    # ---------------- 会话状态 ---------------- #
    def is_busy(self) -> bool:
        """会话是否正在处理中。控件不可用时保守视为空闲，避免死等。"""
        try:
            return bool(self._session.Busy)
        except Exception:  # noqa: BLE001 - 属性可能不存在
            return False

    def wait_idle(self, timeout: float = IDLE_TIMEOUT, settle: float = 0.2) -> bool:
        """等待会话处理完毕。

        旧版到处 `sendVKey(0)` 后固定 `sleep(0.5)`，机器慢或网络差时屏幕还没
        切过来就继续下一步，是"偶发找不到控件"的主要来源。这里先给 SAP 一点
        时间进入忙状态，再轮询到它空闲为止。
        """
        time.sleep(settle)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self.is_busy():
                return True
            time.sleep(0.1)
        LOGGER.debug("等待会话空闲超过 %.1f 秒，继续按当前状态执行", timeout)
        return False

    def status_message(self) -> tuple[str, str]:
        """读取状态栏，返回 (消息类型, 文本)。

        消息类型：S=成功、W=警告、E=错误、A=终止、I=信息，取不到则为空串。
        """
        bar = self.find(ID_STATUS_BAR)
        if bar is None:
            return "", ""
        text = str(getattr(bar, "Text", "") or "").strip()
        mtype = str(getattr(bar, "MessageType", "") or "").strip().upper()
        return mtype, text

    def is_login_screen(self) -> bool:
        """当前是否停在登录界面（登录未成功的最直接证据）。"""
        return self.find(ID_PASSWORD) is not None or self.find(ID_USER) is not None

    # ---------------- 元素访问 ---------------- #
    def find(self, element_id: str) -> Optional[object]:
        """安全查找控件，找不到返回 None（SAP 脚本接口只会抛异常）。"""
        try:
            return self._session.findById(element_id)
        except Exception:  # noqa: BLE001 - COM 接口异常类型不可预期
            return None

    def press(self, element_id: str) -> bool:
        element = self.find(element_id)
        if element is None:
            return False
        try:
            element.press()
            return True
        except Exception:  # noqa: BLE001
            LOGGER.debug("按下 %s 失败", element_id)
            return False

    def send_vkey(self, key: int = 0, window: str = "wnd[0]") -> None:
        element = self.find(window)
        if element is None:
            raise SAPError(f"窗口 {window} 不存在，无法发送虚拟按键")
        element.sendVKey(key)

    # ---------------- 登录 ---------------- #
    def login(self, client: str, user: str, password: str) -> None:
        self._fill_login_form(client, user, password)
        self.send_vkey(0)
        self.wait_idle()

        if self.dismiss_login_conflict():
            LOGGER.info("检测到重复登录弹窗，已自动选择“继续此登录，但不结束其它登录”。")
            self.wait_idle()

        self.verify_login(client, user)

    def verify_login(self, client: str, user: str) -> None:
        """登录后校验，确认真的是"登录成功"。

        旧版在这里直接打印"登录完成"：密码错了、用户被锁了、client 不允许登录
        时同样会"成功"返回，调用方（快捷方式/VBS）完全看不出异常，直到下一步
        才发现屏幕不对。这里补三道检查：强制改密、报错提示、是否仍停在登录页。
        """
        if not self._verify:
            LOGGER.warning("已跳过登录结果校验，请自行确认登录是否成功。")
            return

        deadline = time.time() + LOGIN_VERIFY_TIMEOUT
        while time.time() < deadline:
            self.wait_idle()

            if self._login_blocker() is not None:
                break            # 出现报错，不必再等

            # 迟到一点的重复登录弹窗（不在 dismiss_login_conflict 的观察窗口内冒出来）
            if self._any_popup_present():
                if self.dismiss_login_conflict(timeout=POPUP_GRACE):
                    LOGGER.info("登录过程中出现重复登录弹窗，已自动选择“继续此登录”。")
                    continue
                break            # 处理不了，交给下面统一报错

            if not self.is_login_screen():
                break            # 已离开登录页，认为成功
            time.sleep(0.3)
        else:
            LOGGER.debug("等待登录结果超过 %s 秒，按当前界面判定", LOGIN_VERIFY_TIMEOUT)

        if self.find(ID_REPEAT_PASSWORD) is not None:
            raise LoginError(
                "SAP 要求首次登录或密码到期后修改密码"
                f"（当前是改密屏，新密码字段 {ID_NEW_PASSWORD} / {ID_REPEAT_PASSWORD}）。"
                "为避免写坏密码，脚本不会自动改密，"
                "请先手工登录一次并设置好新密码，再运行本程序。"
            )

        blocker = self._login_blocker()
        if blocker is not None:
            raise LoginError(
                f"登录未成功（client={client}, user={user}）：{blocker}\n"
                "常见原因：密码错误、用户被锁定、client 不允许该用户登录。"
            )

        if self.is_login_screen():
            mtype, text = self.status_message()
            detail = f"状态栏[{mtype}] {text}" if text else "界面未给出任何提示信息"
            raise LoginError(
                f"输入账号密码后仍停留在登录界面（{detail}）。"
                "请确认用户名/密码与该 client 一致。"
            )

        LOGGER.info("登录成功（client=%s, user=%s）。", client, user)

    def _login_blocker(self) -> Optional[str]:
        """返回阻止登录成功的提示信息（报错弹窗或状态栏错误），没有则返回 None。"""
        for index in POPUP_WINDOW_INDEXES:
            window_id = f"wnd[{index}]"
            if self.find(window_id) is None:
                continue
            snapshot = self._popup_snapshot(window_id)
            if snapshot and _contains_any(snapshot, ERROR_HINTS):
                return f"{window_id} 弹窗: {snapshot}"

        mtype, text = self.status_message()
        if mtype in ("E", "A") and text:
            return f"状态栏[{mtype}] {text}"
        return None

    def _fill_login_form(self, client: str, user: str, password: str) -> None:
        for element_id, value in (
            (ID_CLIENT, client),
            (ID_USER, user),
            (ID_PASSWORD, password),
        ):
            element = self.find(element_id)
            if element is None:
                raise LoginError(
                    f"登录界面缺少控件 {element_id}，请确认 SAP 登录窗口已打开"
                    "（若已登录，请先退出或改用已有会话）。"
                )
            element.text = value

    # ---------------- 多登录冲突弹窗 ---------------- #
    def dismiss_login_conflict(self, timeout: Optional[float] = None) -> bool:
        """处理重复登录/已登录冲突弹窗，成功处理返回 True。

        有三条快速返回路径，避免"没弹窗也干等满 popup_timeout"（旧版每次正常
        登录都要白等 8 秒）：

        * 弹窗出现且处理成功 -> True；
        * 弹窗出现但确认是报错、处理不了 -> False（立即，交给上层报错）；
        * 观察 POPUP_GRACE 秒后依然没有任何弹窗 -> False（正常登录）。
        """
        total = self._popup_timeout if timeout is None else timeout
        deadline = time.time() + total
        no_popup_until = time.time() + min(total, POPUP_GRACE)
        saw_popup = False

        while time.time() < deadline:
            if not self._any_popup_present():
                if saw_popup:
                    return True                # 弹窗已消失，视为处理完毕
                if time.time() >= no_popup_until:
                    return False               # 压根没出现过弹窗
            else:
                saw_popup = True
                handled, blocked = self._handle_conflict_once()
                if handled:
                    return True
                if blocked:
                    return False
            time.sleep(0.3)

        if saw_popup:
            self._dump_popup_info()
        return False

    def _any_popup_present(self) -> bool:
        return any(self.find(f"wnd[{index}]") is not None for index in POPUP_WINDOW_INDEXES)

    def _dump_popup_info(self) -> None:
        """排查用：把弹窗上的文字打到 DEBUG 日志（-v 可见），便于确认控件 ID。"""
        if not LOGGER.isEnabledFor(logging.DEBUG):
            return
        for index in POPUP_WINDOW_INDEXES:
            window_id = f"wnd[{index}]"
            if self.find(window_id) is None:
                continue
            LOGGER.debug("弹窗 %s 内容: %s", window_id, self._popup_snapshot(window_id) or "(无文字)")

    def _handle_conflict_once(self) -> tuple[bool, bool]:
        """尝试处理一次弹窗，返回 (是否已处理, 是否遇到不可处理的报错弹窗)。"""
        for index in POPUP_WINDOW_INDEXES:
            window_id = f"wnd[{index}]"
            if self.find(window_id) is None:
                continue
            if self._select_continue_option(window_id):
                return True, False
            if self._press_continue_button(window_id):
                return True, False

            snapshot = self._popup_snapshot(window_id)
            if snapshot and not _is_confirmable_popup(snapshot):
                LOGGER.error(
                    "弹窗 %s 疑似报错提示，已放弃自动点击以免掩盖真实错误。内容: %s",
                    window_id,
                    snapshot,
                )
                return False, True

            if self._press_default_option(window_id):
                return True, False
        return False, False

    # ---------------- 弹窗文字读取 ---------------- #
    def _popup_snapshot(self, window_id: str) -> str:
        """把弹窗上的可见文字拼成一行（控件树文字 + 工具栏按钮文字，已去重）。

        既用于判断弹窗性质，也用于排查控件 ID。
        """
        window = self.find(window_id)
        if window is None:
            return ""

        texts = self._harvest_texts(window, [TEXT_HARVEST_BUDGET])
        texts.extend(
            label
            for index in TOOLBAR_BUTTON_INDEXES
            if (button := self.find(f"{window_id}/tbar[0]/btn[{index}]")) is not None
            if (label := self._read_label(button))
        )

        seen: set[str] = set()
        unique: list[str] = []
        for text in texts:
            text = text.strip()
            if text and text not in seen:
                seen.add(text)
                unique.append(text)
        return " | ".join(unique)

    def _harvest_texts(self, element: object, budget: list[int]) -> list[str]:
        """尽力递归收集控件树上的文字。任何一步失败都只跳过，不影响主流程。"""
        if budget[0] <= 0:
            return []
        budget[0] -= 1

        texts: list[str] = []
        for attr in ("Text", "Tooltip"):
            try:
                value = getattr(element, attr)
            except Exception:  # noqa: BLE001
                continue
            if isinstance(value, str) and value.strip():
                texts.append(value.strip())

        for child in self._children(element):
            texts.extend(self._harvest_texts(child, budget))
        return texts

    @staticmethod
    def _children(element: object) -> list[object]:
        """取出子控件列表。SAP 各版本取集合元素的方式不一致（Children(i) / ElementAt(i)），逐个尝试。"""
        try:
            collection = element.Children
            count = int(collection.Count)
        except Exception:  # noqa: BLE001
            return []

        children: list[object] = []
        for index in range(max(0, min(count, 50))):
            for accessor in (
                lambda: collection.ElementAt(index),
                lambda: collection(index),
            ):
                try:
                    children.append(accessor())
                    break
                except Exception:  # noqa: BLE001
                    continue
        return children

    def _select_continue_option(self, window_id: str) -> bool:
        """选中“继续此登录”单选项并确认。"""
        for radio_id in RADIO_CANDIDATE_IDS:
            radio = self.find(f"{window_id}/usr/{radio_id}")
            if radio is None:
                continue

            self._select_radio(radio, radio_id)
            if not self.press(f"{window_id}/tbar[0]/btn[0]"):
                self.send_vkey(0, window=window_id)
            self.wait_idle(timeout=5)
            LOGGER.debug("已在 %s 上选择 %s 并确认", window_id, radio_id)
            return True
        return False

    @staticmethod
    def _select_radio(radio: object, radio_id: str) -> None:
        """不同 SAP 版本暴露的选中方式不同，逐个尝试。

        注意用 getattr 取值：属性本身可能不存在，直接在元组里写 radio.select
        会在进入 try 之前就抛 AttributeError。
        """
        for name in ("select", "press"):
            action = getattr(radio, name, None)
            if action is None:
                continue
            try:
                action()
                return
            except Exception:  # noqa: BLE001
                continue
        try:
            radio.Selected = True
        except Exception:  # noqa: BLE001
            LOGGER.debug("单选框 %s 的三种选中方式均不可用", radio_id)

    def _press_continue_button(self, window_id: str) -> bool:
        """按文本匹配"继续"类按钮。"""
        for index in TOOLBAR_BUTTON_INDEXES:
            button_id = f"{window_id}/tbar[0]/btn[{index}]"
            button = self.find(button_id)
            if button is None:
                continue

            label = self._read_label(button)
            if label and any(token.lower() in label.lower() for token in CONTINUE_LABELS):
                if self.press(button_id):
                    time.sleep(0.2)
                    LOGGER.debug("已在 %s 按下“继续”按钮", button_id)
                    return True
        return False

    def _press_default_option(self, window_id: str) -> bool:
        """兜底：直接按 tbar[0]/btn[1]。

        部分 SAP 版本/语言下，多登录弹窗的单选框 ID 与按钮文本都匹配不到预设值，
        此时只能按位置兜底。这是旧脚本沿用下来的行为，务必保留，
        否则重复登录时不会自动选择“继续此登录”。

        调用前由 `_handle_conflict_once` 用 `_is_confirmable_popup()` 把关：
        只有确认弹窗不是报错信息时才会走到这里。
        """
        button_id = f"{window_id}/tbar[0]/btn[1]"
        if self.find(button_id) is None:
            return False
        if not self.press(button_id):
            return False

        time.sleep(0.2)
        LOGGER.warning(
            "未匹配到“继续此登录”控件，已按兜底逻辑点击 %s。"
            "若发现点击了错误的按钮，请带上 -v 的输出反馈实际弹窗的控件 ID。",
            button_id,
        )
        return True

    @staticmethod
    def _read_label(button: object) -> Optional[str]:
        for attr in ("Text", "text", "Tooltip", "tooltip", "Caption", "caption"):
            try:
                label = getattr(button, attr)
            except Exception:  # noqa: BLE001
                continue
            if label:
                return str(label)
        return None

    # ---------------- 事务码 ---------------- #
    def enter_transaction(self, tcode: str) -> None:
        field = self.find(ID_COMMAND_FIELD)
        if field is None:
            raise SAPError("未找到命令字段 wnd[0]/tbar[0]/okcd，请确认主界面已打开。")
        field.text = tcode
        self.send_vkey(0)
        self.wait_idle()

        if self._verify:
            mtype, text = self.status_message()
            if mtype in ("E", "A") and text:
                raise SAPError(
                    f"事务码 {tcode} 执行报错：状态栏[{mtype}] {text}\n"
                    "请确认该 T-code 存在、且当前用户有权限。"
                )
            if mtype == "W" and text:
                LOGGER.warning("事务码 %s 提示: %s", tcode, text)

        LOGGER.info("已输入 T-code: %s", tcode)


# --------------------------------------------------------------------------- #
# 启动器
# --------------------------------------------------------------------------- #
class SAPLauncher:
    """负责启动 SAP Logon 并获取会话。"""

    # 让 saplogon.exe 脱离父进程控制台：否则脚本退出后 CMD 窗口会被 SAP Logon
    # 一直"挂"住不关闭（Windows 上子进程默认继承父控制台）。
    _DETACHED_FLAGS = (
        getattr(subprocess, "DETACHED_PROCESS", 0)
        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    )

    def __init__(self, saplogon_path: Path, startup_timeout: int, connect_timeout: int) -> None:
        self._saplogon_path = saplogon_path
        self._startup_timeout = startup_timeout
        self._connect_timeout = connect_timeout

    def open_session(
        self, connection_name: str, popup_timeout: int, verify: bool = True
    ) -> SAPSession:
        self._ensure_saplogon_running()
        return self._open_connection(connection_name, popup_timeout, verify=verify)

    def _ensure_saplogon_running(self) -> None:
        if self._get_sap_gui() is not None:
            LOGGER.debug("SAP GUI 已在运行")
            return

        # 配置里的路径不一定对（32 位/64 位 Program Files 因机器而异），
        # 按 配置值 > 注册表 > 常见安装位置 逐个核实，找到就用。
        target = resolve_saplogon_path(str(self._saplogon_path))
        if target is None:
            tried = "\n".join(
                f"  · {path}" for path in saplogon_candidates(str(self._saplogon_path))[:6]
            )
            raise StartupError(
                "找不到 SAP Logon 可执行文件。已尝试以下位置：\n"
                f"{tried}\n"
                "请在「全局设置」里把路径改成实际的 saplogon.exe 位置。"
            )

        if target != self._saplogon_path:
            LOGGER.info(
                "配置里的 SAP Logon 路径不存在（%s），改用自动探测到的 %s",
                self._saplogon_path, target,
            )
            self._saplogon_path = target  # 记住，后续不再重复探测

        LOGGER.info("启动 SAP Logon: %s", target)
        subprocess.Popen(
            [str(target)],
            close_fds=True,
            creationflags=self._DETACHED_FLAGS,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        if self._wait(lambda: self._get_sap_gui() is not None, self._startup_timeout):
            return
        raise StartupError(
            f"SAP GUI 在 {self._startup_timeout} 秒内未就绪，请确认已正确安装并允许脚本访问。"
        )

    def _open_connection(
        self, connection_name: str, popup_timeout: int, verify: bool = True
    ) -> SAPSession:
        LOGGER.info("连接 SAP 系统: %s", connection_name)
        holder: dict[str, object] = {}

        def _connect() -> bool:
            sap_gui = self._get_sap_gui()
            if sap_gui is None:
                return False

            try:
                engine = sap_gui.GetScriptingEngine
            except Exception as exc:  # noqa: BLE001 - COM 异常类型不可预期
                # 脚本被禁用属于配置问题，重试没有意义，立即失败而不是干等满超时。
                raise SessionError(
                    "SAP GUI 脚本接口不可用。请在 SAP Logon 中打开"
                    "「选项 → 脚本」，勾选“启用脚本”，并取消相关提示确认。"
                ) from exc

            try:
                connection = engine.OpenConnection(connection_name, True)
            except Exception:  # noqa: BLE001 - 已存在连接时回退到第一个
                try:
                    connection = engine.Children(0)
                except Exception:  # noqa: BLE001
                    return False  # 连接尚未建立，继续等待

            session = self._pick_session(connection)
            if session is None:
                return False  # 连接已有但会话未就绪，继续等待
            holder["session"] = session
            return True

        if self._wait(_connect, self._connect_timeout):
            return SAPSession(holder["session"], popup_timeout=popup_timeout, verify=verify)
        raise SessionError(
            f"{self._connect_timeout} 秒内无法获取 {connection_name} 的会话，"
            "请检查网络连接、连接名以及 SAP GUI 脚本权限。"
        )

    @classmethod
    def _pick_session(cls, connection: object) -> Optional[object]:
        """从连接里挑一个"该用来登录"的会话。

        连接已存在时 `OpenConnection` 可能只做复用，此时 Children(0) 未必是本轮
        新开出来的那个 —— 如果第一项是别的 client 上已登录的老会话，直接拿它填
        登录表单必然失败。这里优先挑"停在登录页"的会话，都没有就取最后一个
        （SAP 把新会话追加在末尾）。
        """
        sessions: list[object] = []
        try:
            count = int(connection.Children.Count)
        except Exception:  # noqa: BLE001
            return None

        for index in range(max(0, count)):
            for accessor in (
                lambda index=index: connection.Children(index),
                lambda index=index: connection.Children.ElementAt(index),
            ):
                try:
                    sessions.append(accessor())
                    break
                except Exception:  # noqa: BLE001
                    continue

        if not sessions:
            return None
        for session in reversed(sessions):
            if cls._is_on_login_screen(session):
                return session
        return sessions[-1]

    @staticmethod
    def _is_on_login_screen(session: object) -> bool:
        try:
            session.findById(ID_PASSWORD)
            return True
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _get_sap_gui() -> Optional[object]:
        try:
            return win32com.client.GetObject("SAPGUI")
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _wait(condition, timeout: int, interval: float = 0.5) -> bool:
        """轮询等待条件成立。前 2 秒用 0.2 秒的短间隔快速响应，之后退回到正常间隔。"""
        deadline = time.time() + timeout
        fast_until = time.time() + 2.0
        while time.time() < deadline:
            if condition():
                return True
            time.sleep(0.2 if time.time() < fast_until else interval)
        return False


# --------------------------------------------------------------------------- #
# 一次完整登录（GUI 和 CLI 共用）
# --------------------------------------------------------------------------- #
def perform_login(
    settings: Settings,
    target: LoginTarget,
    tcode: Optional[str] = None,
    on_progress: Optional[Callable[[str], None]] = None,
) -> None:
    """启动 SAP、登录，可选再跳事务码。失败时抛 SAPError 子类。"""
    def notify(message: str) -> None:
        LOGGER.info(message)
        if on_progress is not None:
            on_progress(message)

    missing = [name for name, value in (
        ("连接名", target.connection),
        ("client", target.client),
        ("用户名", target.user),
        ("密码", target.password),
    ) if not value]
    if missing:
        raise SettingsError(
            f"这条连接还缺 {'/'.join(missing)}，请先在界面上补全再登录。"
        )

    notify(f"client={target.client} -> 连接 {target.connection}，用户 {target.user}")

    launcher = SAPLauncher(
        saplogon_path=settings.saplogon_path,
        startup_timeout=settings.startup_timeout,
        connect_timeout=settings.connect_timeout,
    )
    session = launcher.open_session(
        target.connection,
        popup_timeout=settings.popup_timeout,
        verify=settings.verify,
    )

    session.login(target.client, target.user, target.password)

    effective_tcode = (tcode or target.tcode or "").strip()
    if effective_tcode:
        session.enter_transaction(effective_tcode)


# --------------------------------------------------------------------------- #
# 日志
# --------------------------------------------------------------------------- #
def configure_logging(verbose: bool = False, stream=None) -> None:
    """配置日志。无控制台时（GUI 模式 / windowed exe）挂空 handler，避免 sys.stdout 为空报错。"""
    handlers: list[logging.Handler] = []
    out = stream if stream is not None else sys.stdout
    if out is not None:
        handlers.append(logging.StreamHandler(out))
    if not handlers:
        handlers.append(logging.NullHandler())

    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
        force=True,
    )


def attach_log_file(log_file: Optional[str]) -> Optional[Path]:
    """额外挂一个文件 handler，支持 %TEMP% 等环境变量。

    相对路径（如 OpenSAPGUI.log）会解析到程序所在目录，而非当前工作目录，
    这样无论在哪里启动，日志都落在程序旁边。
    """
    if not log_file:
        return None
    path = Path(os.path.expandvars(log_file)).expanduser()
    if not path.is_absolute():
        path = application_dir() / path
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(path, encoding="utf-8")
        file_handler.setFormatter(
            logging.Formatter(
                "%(asctime)s [%(levelname)s] %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        logging.getLogger().addHandler(file_handler)
        LOGGER.debug("日志文件: %s", path)
        return path
    except OSError as exc:  # 路径不可写时只警告，不影响主流程
        LOGGER.warning("无法写入日志文件 %s: %s", path, exc)
        return None
