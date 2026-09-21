# -*- coding: utf-8 -*-
"""Mock 测试：验证重复登录弹窗的三段处理逻辑（不连接真实 SAP）。

直接运行：python tests/test_login_conflict.py
全部通过返回 0，任一场景不符合预期返回 1，方便批处理或 CI 判断。
"""
import logging
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import sap_core as m


class FakeWin:
    def sendVKey(self, k):
        print("  [sendVKey %s on wnd[1]]" % k)


class FakeBtn:
    def __init__(self, text):
        self.Text = text
        self.pressed = False

    def press(self):
        self.pressed = True
        print("  [pressed] %s" % self.Text)


class FakeRadio:
    def select(self):
        print("  [radio.select] radMULTI_LOGON_OPT2")


class FakeSession:
    """模拟：单选框不存在，按钮文本都不含“继续”，只能靠 btn[1] 兜底。"""

    def __init__(self, with_radio=False):
        self.with_radio = with_radio
        self.btns = {f"wnd[1]/tbar[0]/btn[{i}]": FakeBtn(f"Btn{i}") for i in range(6)}

    def findById(self, eid):
        if eid == "wnd[1]":
            return FakeWin()
        if self.with_radio and eid == "wnd[1]/usr/radMULTI_LOGON_OPT2":
            return FakeRadio()
        if eid in self.btns:
            return self.btns[eid]
        raise Exception("not found: " + eid)


class NoButtonSession(FakeSession):
    """弹窗存在，但没有任何可用按钮。"""

    def findById(self, eid):
        if eid == "wnd[1]":
            return FakeWin()
        raise Exception("not found: " + eid)


class NoFallbackSession(FakeSession):
    """有 btn[0]/btn[2]，但没有 btn[1]，兜底也会失败。"""

    def __init__(self):
        super().__init__()
        for i in (1, 3, 4, 5):
            self.btns.pop(f"wnd[1]/tbar[0]/btn[{i}]")


def run_case(name, session):
    """跑一个场景，返回 (是否处理成功, 被按下的按钮索引列表)。"""
    print("\n=== %s ===" % name)
    handled = m.SAPSession(session, popup_timeout=2).dismiss_login_conflict()
    pressed = [
        index
        for index in range(6)
        if (button := session.btns.get(f"wnd[1]/tbar[0]/btn[{index}]")) is not None
        and button.pressed
    ]
    print("处理结果:", handled)
    if pressed:
        print("被按下的按钮: " + ", ".join("btn[%d]" % index for index in pressed))
    else:
        print("被按下的按钮: (无)")
    return handled, pressed


def main() -> int:
    # 单独给这个脚本打开调试日志，观察弹窗文字快照
    logging.getLogger("sap").setLevel(logging.DEBUG)
    logging.getLogger().addHandler(logging.StreamHandler(sys.stdout))
    logging.getLogger().setLevel(logging.DEBUG)

    failures: list[str] = []

    handled, pressed = run_case("场景1: 单选框 ID 能匹配 -> 应选 OPT2 并确认", FakeSession(with_radio=True))
    if not handled or pressed != [0]:
        failures.append("场景1: 期望选中单选框并按下 btn[0]，实际 handled=%s pressed=%s" % (handled, pressed))

    handled, pressed = run_case("场景2: 都匹配不到 -> 应走 btn[1] 兜底", FakeSession(with_radio=False))
    if not handled or pressed != [1]:
        failures.append("场景2: 期望走 btn[1] 兜底，实际 handled=%s pressed=%s" % (handled, pressed))

    handled, pressed = run_case("场景3: 弹窗无任何按钮 -> 应返回 False", NoButtonSession())
    if handled or pressed:
        failures.append("场景3: 期望返回 False 且不按任何按钮，实际 handled=%s pressed=%s" % (handled, pressed))

    handled, pressed = run_case("场景4: 没有 btn[1] 可兜底 -> 应返回 False 并输出调试信息", NoFallbackSession())
    if handled or pressed:
        failures.append("场景4: 期望返回 False 且不按任何按钮，实际 handled=%s pressed=%s" % (handled, pressed))

    print()
    if failures:
        for item in failures:
            print("FAIL " + item)
        print("共 4 个场景，失败 %d 个" % len(failures))
        return 1

    print("共 4 个场景，全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
