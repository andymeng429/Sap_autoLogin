# -*- coding: utf-8 -*-
"""Mock 测试：日志文件挂载与"路径失效"回退。

背景：config.json 从别的电脑拷过来时，里面的 log_file 可能还是那台机器的
绝对路径（例如 D:\\ANDY\\SAP-AUTORUN\\OpenSAPGUI.log）。此时必须退回到
程序目录，否则程序崩溃时一点日志都留不下，等于没法排查。

直接运行：python tests/test_log_attach.py
"""
import logging
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import sap_core


def make_temp_dir():
    return tempfile.TemporaryDirectory(prefix="opensapgui-log-")


def detach_file_handlers():
    """摘掉本用例挂上去的文件 handler 并关闭，否则 Windows 下临时目录删不掉。"""
    root = logging.getLogger()
    for handler in list(root.handlers):
        if isinstance(handler, logging.FileHandler):
            root.removeHandler(handler)
            handler.close()


class _FakeAppDir:
    """把 application_dir 临时指到别处，避免污染真实目录。"""

    def __init__(self, path):
        self._path = pathlib.Path(path)
        self._original = None

    def __enter__(self):
        self._original = sap_core.application_dir
        sap_core.application_dir = lambda: self._path
        return self._path

    def __exit__(self, *exc):
        sap_core.application_dir = self._original
        detach_file_handlers()
        return False


# --------------------------------------------------------------------------- #
def test_no_log_file_returns_none():
    assert sap_core.attach_log_file(None) is None, "没配就不该挂 handler"
    assert sap_core.attach_log_file("") is None, "空串同样当没配"


def test_relative_path_lands_next_to_program():
    with tempfile.TemporaryDirectory(prefix="opensapgui-log-") as tmp, _FakeAppDir(tmp):
        path = sap_core.attach_log_file("OpenSAPGUI.log")
        assert path == pathlib.Path(tmp) / "OpenSAPGUI.log", path
        assert path.exists(), "日志文件应被真正创建"
        logging.getLogger("sap").warning("写入测试内容")
        assert "写入测试内容" in path.read_text(encoding="utf-8"), "内容要落盘"


def test_unwritable_path_falls_back_to_program_dir():
    with tempfile.TemporaryDirectory(prefix="opensapgui-log-") as tmp, _FakeAppDir(tmp):
        # 含非法字符的目录名：Windows 上建目录就会失败，用来模拟"拷来的绝对路径不可用"
        bad = str(pathlib.Path(tmp) / "bad<>name" / "OpenSAPGUI.log")
        path = sap_core.attach_log_file(bad)
        expected = pathlib.Path(tmp) / sap_core.DEFAULT_LOG_NAME
        assert path == expected, f"应退回程序目录 {expected}，实际 {path}"
        assert path.exists()


def test_writable_absolute_path_is_used_as_is():
    with tempfile.TemporaryDirectory(prefix="opensapgui-log-") as tmp, _FakeAppDir(
        pathlib.Path(tmp) / "program"
    ):
        target = pathlib.Path(tmp) / "logs" / "custom.log"
        path = sap_core.attach_log_file(str(target))
        assert path == target, f"能写的绝对路径要原样使用，实际 {path}"
        assert path.exists()


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
