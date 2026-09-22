# -*- coding: utf-8 -*-
"""Mock 测试：登录结果校验、报错弹窗保护、会话挑选。

全部使用假控件，不连接真实 SAP，可以直接 `python tests/test_login_verify.py` 跑。
"""
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import sap_core as m


# --------------------------------------------------------------------------- #
# 假控件
# --------------------------------------------------------------------------- #
class Element:
    """万能假控件：需要什么属性就传什么。"""

    def __init__(self, **attrs):
        self.__dict__.update(attrs)


class Button(Element):
    def __init__(self, text):
        super().__init__(Text=text, pressed=False)

    def press(self):
        self.pressed = True


class FakeChildren:
    """模拟 SAP 的 GuiComponentCollection。"""

    def __init__(self, items):
        self._items = list(items)
        self.Count = len(items)

    def __call__(self, index):
        return self._items[index]

    def ElementAt(self, index):
        return self._items[index]


class FakeSession:
    """按控件 ID 查表的假会话。查不到就抛异常（和真实行为一致）。"""

    def __init__(self, elements=None, busy=False):
        self.elements = dict(elements or {})
        self.Busy = busy

    def findById(self, eid):
        if eid in self.elements:
            return self.elements[eid]
        raise Exception("not found: " + eid)


class Field:
    """假输入框：把写进去的值记在所属会话上，方便断言。"""

    def __init__(self, owner, key):
        self._owner = owner
        self._key = key

    @property
    def text(self):
        return self._owner.filled.get(self._key, "")

    @text.setter
    def text(self, value):
        self._owner.filled[self._key] = value


class LoginFlowSession(FakeSession):
    """模拟一次完整登录：发送按钮后登录页消失（或按参数保持不动）。"""

    def __init__(self, stay_on_login=False, bar=("", ""), popup=None):
        super().__init__({})
        self.stay_on_login = stay_on_login
        self.bar = Element(Text=bar[0], MessageType=bar[1])
        self.popup = popup
        self.entered = False
        self.filled = {}
        self._fields = {
            m.ID_CLIENT: Field(self, "client"),
            m.ID_USER: Field(self, "user"),
            m.ID_PASSWORD: Field(self, "password"),
        }

    def findById(self, eid):
        if eid == "wnd[0]":
            return Element(sendVKey=self._send_vkey)
        if eid == m.ID_STATUS_BAR:
            return self.bar
        if eid == "wnd[1]":
            if self.popup:
                return Element(Text=self.popup)
            raise Exception("not found: wnd[1]")
        if eid in self._fields:
            if self.entered and not self.stay_on_login:
                raise Exception("登录页已消失: " + eid)
            return self._fields[eid]
        raise Exception("not found: " + eid)

    def _send_vkey(self, key):
        self.entered = True


def login_screen_elements(**extra):
    """一套完整的“停在登录页”的控件。"""
    elements = {
        m.ID_CLIENT: Element(text="", Text=""),
        m.ID_USER: Element(text="", Text=""),
        m.ID_PASSWORD: Element(text="", Text=""),
        m.ID_STATUS_BAR: Element(Text="", MessageType=""),
        "wnd[0]": Element(sendVKey=lambda key: None),
    }
    elements.update(extra)
    return elements


# --------------------------------------------------------------------------- #
# 用例
# --------------------------------------------------------------------------- #
def test_confirmable_popup_decisions():
    """弹窗可点性判定：报错弹窗必须被拦下，其它维持旧兜底行为。"""
    assert m._is_confirmable_popup("继续此登录，但不结束其它登录 | 结束其它登录，继续此登录")
    assert m._is_confirmable_popup("Multiple logons are not permitted")
    assert m._is_confirmable_popup("Continue")
    assert m._is_confirmable_popup("")                 # 取不到文字 -> 保持旧兜底
    assert m._is_confirmable_popup("Btn0 | Btn1")      # 认不出来 -> 保持旧兜底

    assert not m._is_confirmable_popup("用户名或密码错误")
    assert not m._is_confirmable_popup("Password incorrect")
    assert not m._is_confirmable_popup("用户已被锁定，由于多次输入错误密码")


def test_error_popup_is_not_clicked():
    """报错弹窗：既不选单选框也不按按钮，直接交回上层报错。"""
    ok_btn = Button("确认")
    session = FakeSession({
        "wnd[1]": Element(Text="用户名或密码错误"),
        "wnd[1]/tbar[0]/btn[0]": ok_btn,
        "wnd[1]/tbar[0]/btn[1]": ok_btn,
    })
    handled = m.SAPSession(session, popup_timeout=1).dismiss_login_conflict()
    assert handled is False, "报错弹窗不应被当作登录冲突处理"
    assert ok_btn.pressed is False, "报错弹窗的按钮绝不能被自动按下"


def test_dismiss_returns_fast_when_no_popup():
    """没有弹窗时必须立刻收工，不能干等满 popup_timeout（旧版白等 8 秒）。"""
    started = time.time()
    handled = m.SAPSession(FakeSession({}), popup_timeout=30).dismiss_login_conflict()
    elapsed = time.time() - started
    assert handled is False
    assert elapsed < m.POPUP_GRACE + 1.5, "无弹窗时耗时 %.1f 秒，仍然等太久了" % elapsed


def test_login_happy_path():
    """完整登录：表单值写对、登录页消失 -> 判定成功，且不白等。"""
    session = LoginFlowSession()
    started = time.time()
    m.SAPSession(session, popup_timeout=30).login("120", "DEMO_USER", "secret")
    assert session.filled == {"client": "120", "user": "DEMO_USER", "password": "secret"}
    assert session.entered is True
    assert time.time() - started < 6, "正常登录耗时过长"


def test_login_failure_is_reported():
    """密码错误：login() 必须抛 LoginError，而不是默默返回。"""
    session = LoginFlowSession(
        stay_on_login=True,
        bar=("用户名或密码错误", "E"),
        popup="用户名或密码错误",
    )
    try:
        m.SAPSession(session, popup_timeout=30).login("120", "DEMO_USER", "wrong")
    except m.LoginError as exc:
        assert "登录未成功" in str(exc), str(exc)
        return
    raise AssertionError("登录失败时必须抛 LoginError")


def test_wait_idle_polls_until_ready():
    class FlakyBusy:
        def __init__(self):
            self.calls = 0

        @property
        def Busy(self):
            self.calls += 1
            return self.calls < 3

    fake = FlakyBusy()
    assert m.SAPSession(fake, popup_timeout=1).wait_idle(timeout=3) is True
    assert fake.calls >= 3


def test_is_busy_tolerates_missing_attribute():
    """控件不支持 Busy 时应视为空闲，不能因此死等。"""
    assert m.SAPSession(object(), popup_timeout=1).is_busy() is False


def test_verify_login_success():
    """已经离开登录页 -> 判定登录成功，不抛异常。"""
    session = FakeSession({
        m.ID_STATUS_BAR: Element(Text="", MessageType=""),
        m.ID_COMMAND_FIELD: Element(text=""),
        "wnd[0]": Element(sendVKey=lambda key: None),
    })
    m.SAPSession(session, popup_timeout=1).verify_login("120", "DEMO_USER")


def test_verify_login_wrong_password():
    """密码错误：弹窗报错必须暴露成 LoginError，而不是“登录完成”。"""
    session = FakeSession(login_screen_elements(**{
        m.ID_STATUS_BAR: Element(Text="用户名或密码错误", MessageType="E"),
        "wnd[1]": Element(Text="用户或密码不正确"),
    }))
    try:
        m.SAPSession(session, popup_timeout=1).verify_login("120", "DEMO_USER")
    except m.LoginError as exc:
        assert "密码" in str(exc), str(exc)
        return
    raise AssertionError("密码错误时必须抛 LoginError")


def test_verify_login_stuck_on_login_screen():
    """一直停在登录页且没有任何提示 -> 也要报错，不能默默成功。"""
    original = m.LOGIN_VERIFY_TIMEOUT
    m.LOGIN_VERIFY_TIMEOUT = 1        # 缩短等待，避免测试跑 12 秒
    try:
        session = FakeSession(login_screen_elements())
        try:
            m.SAPSession(session, popup_timeout=1).verify_login("120", "DEMO_USER")
        except m.LoginError as exc:
            assert "仍停留在登录界面" in str(exc), str(exc)
            return
        raise AssertionError("停在登录页时必须抛 LoginError")
    finally:
        m.LOGIN_VERIFY_TIMEOUT = original


def test_verify_login_forced_password_change():
    """强制改密界面：明确提示，而不是当成登录成功或密码错误。"""
    session = FakeSession({
        m.ID_NEW_PASSWORD: Element(text="", Text=""),
        m.ID_REPEAT_PASSWORD: Element(text="", Text=""),
        m.ID_STATUS_BAR: Element(Text="", MessageType=""),
        "wnd[0]": Element(sendVKey=lambda key: None),
    })
    try:
        m.SAPSession(session, popup_timeout=1).verify_login("120", "DEMO_USER")
    except m.LoginError as exc:
        assert "修改密码" in str(exc), str(exc)
        return
    raise AssertionError("改密界面必须抛 LoginError")


def test_no_verify_skips_checks():
    """--no-verify：跳过校验，不下任何结论。"""
    session = FakeSession(login_screen_elements())
    m.SAPSession(session, popup_timeout=1, verify=False).verify_login("120", "DEMO_USER")


def test_enter_transaction_reports_error():
    session = FakeSession({
        m.ID_COMMAND_FIELD: Element(text=""),
        m.ID_STATUS_BAR: Element(Text="事务代码 SE09 不存在", MessageType="E"),
        "wnd[0]": Element(sendVKey=lambda key: None),
    })
    try:
        m.SAPSession(session, popup_timeout=1).enter_transaction("SE09")
    except m.SAPError as exc:
        assert "SE09" in str(exc), str(exc)
        return
    raise AssertionError("事务码报错时必须抛 SAPError")


def test_enter_transaction_ok_when_no_message():
    session = FakeSession({
        m.ID_COMMAND_FIELD: Element(text=""),
        m.ID_STATUS_BAR: Element(Text="", MessageType=""),
        "wnd[0]": Element(sendVKey=lambda key: None),
    })
    m.SAPSession(session, popup_timeout=1).enter_transaction("SE09")


def test_pick_session_prefers_login_screen():
    """连接里既有已登录的老会话、也有新开的登录页会话时，必须挑后者。"""
    logged_in = FakeSession({})
    on_login = FakeSession({m.ID_PASSWORD: Element(text="")})
    connection = Element(Children=FakeChildren([logged_in, on_login]))
    assert m.SAPLauncher._pick_session(connection) is on_login


def test_pick_session_falls_back_to_last():
    first, second = FakeSession({}), FakeSession({})
    connection = Element(Children=FakeChildren([first, second]))
    assert m.SAPLauncher._pick_session(connection) is second


def test_pick_session_none_when_empty():
    connection = Element(Children=FakeChildren([]))
    assert m.SAPLauncher._pick_session(connection) is None


# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# perform_login 的事务码选择
# --------------------------------------------------------------------------- #
def test_perform_login_tcode_precedence():
    """事务码：显式传参 > 条目里配的；都没有就不进事务码。"""
    class FakeOpenedSession:
        def __init__(self):
            self.calls = []

        def login(self, client, user, password):
            self.calls.append(("login", client, user))

        def enter_transaction(self, tcode):
            self.calls.append(("tcode", tcode))

    created = []

    class FakeLauncher:
        def __init__(self, **kwargs):
            self.session = FakeOpenedSession()
            created.append(self.session)

        def open_session(self, connection, popup_timeout, verify):
            return self.session

    def make_settings(tcode=""):
        entry = m.ConnectionEntry(
            connection="DEV-1", client="120", user="U",
            password="P", tcode=tcode,
        )
        return m.Settings.from_config(m.AppConfig(entries=[entry]))

    def make_target(tcode=""):
        return m.LoginTarget(label="L", connection="DEV-1", client="120",
                             user="U", password="P", tcode=tcode)

    original = m.SAPLauncher
    m.SAPLauncher = FakeLauncher
    try:
        # 条目里配的事务码由 resolve_target 带进 target；调用方没传就用它
        m.perform_login(make_settings(), make_target("SE80"), tcode=None)
        assert ("tcode", "SE80") in created[-1].calls

        # 显式传参优先于 target 里带的
        m.perform_login(make_settings(), make_target("SE80"), tcode="MM03")
        assert ("tcode", "MM03") in created[-1].calls

        # 两边都没有 -> 只登录，不进事务码
        m.perform_login(make_settings(), make_target(), tcode=None)
        assert not any(call[0] == "tcode" for call in created[-1].calls)
    finally:
        m.SAPLauncher = original


def main() -> int:
    tests = [(name, obj) for name, obj in sorted(globals().items())
             if name.startswith("test_") and callable(obj)]
    failed = 0
    for name, func in tests:
        try:
            func()
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print("FAIL %-45s %s: %s" % (name, type(exc).__name__, exc))
        else:
            print("PASS %s" % name)
    print("\n共 %d 项，失败 %d 项" % (len(tests), failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
