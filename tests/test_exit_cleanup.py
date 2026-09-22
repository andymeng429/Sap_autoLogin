# -*- coding: utf-8 -*-
"""Mock 测试：启动时清扫 PyInstaller 残留临时目录（_MEIxxxx）。

直接运行：python tests/test_exit_cleanup.py
"""
import os
import pathlib
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import OpenSAPGUI

CODEBUDDY_SAFE_DELETE_OK = os.environ.get("CODEBUDDY_SAFE_DELETE_ENABLED") == "0"


def make_dir(base, name, age_seconds):
    path = pathlib.Path(base) / name
    path.mkdir()
    (path / "python313.dll").write_text("fake", encoding="utf-8")
    stamp = time.time() - age_seconds
    os.utime(path, (stamp, stamp))
    return path


def test_removes_stale_mei_dirs_only():
    if not CODEBUDDY_SAFE_DELETE_OK:
        print("SKIP 需要环境变量 CODEBUDDY_SAFE_DELETE_ENABLED=0 才能清理临时目录")
        return
    with tempfile.TemporaryDirectory(prefix="opensapgui-test-") as tmp:
        stale = make_dir(tmp, "_MEI111111", age_seconds=3600)
        fresh = make_dir(tmp, "_MEI222222", age_seconds=10)      # 太新，不碰
        innocent = make_dir(tmp, "not-mei", age_seconds=3600)    # 名字不匹配

        removed = OpenSAPGUI.cleanup_stale_mei(tmp)

        names = [pathlib.Path(p).name for p in removed]
        assert "_MEI111111" in names, "超过两分钟的残留该清掉"
        assert stale.exists() is False, "残留目录应已被删除"
        assert fresh.exists() is True, "两分钟内的新目录可能是运行中的实例，不能动"
        assert innocent.exists() is True, "只清 _MEI* 命名的，别人的东西不碰"


def test_missing_dir_or_file_is_ignored():
    if not CODEBUDDY_SAFE_DELETE_OK:
        print("SKIP 需要环境变量 CODEBUDDY_SAFE_DELETE_ENABLED=0 才能清理临时目录")
        return
    with tempfile.TemporaryDirectory(prefix="opensapgui-test-") as tmp:
        not_a_dir = pathlib.Path(tmp) / "_MEI333333"
        not_a_dir.write_text("我是文件", encoding="utf-8")
        os.utime(not_a_dir, (time.time() - 3600,) * 2)

        removed = OpenSAPGUI.cleanup_stale_mei(tmp)
        assert removed == [], "_MEI 命名的普通文件不该被当成残留目录"
        assert not_a_dir.exists() is True


def test_empty_temp_returns_nothing():
    with tempfile.TemporaryDirectory(prefix="opensapgui-test-") as tmp:
        assert OpenSAPGUI.cleanup_stale_mei(tmp) == []


def test_entry_module_imports_cleanly():
    """入口模块只定义不执行：import 不该有副作用（不开线程、不读配置）。"""
    assert callable(OpenSAPGUI.cleanup_stale_mei)
    assert callable(OpenSAPGUI.main)
    assert callable(OpenSAPGUI.acquire_single_instance_lock)
    assert callable(OpenSAPGUI.release_single_instance_lock)


def test_single_instance_lock_blocks_second_acquire():
    """锁被别的实例占着时必须拿不到；对方释放后要能重新拿到。"""
    if sys.platform != "win32":
        print("SKIP 非 Windows 平台")
        return
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    name = r"Local\OpenSAPGUI.test.%d" % os.getpid()
    OpenSAPGUI.release_single_instance_lock()      # 防止其它用例残留状态
    try:
        holder = kernel32.CreateMutexW(None, False, name)   # 模拟另一个实例
        assert OpenSAPGUI.acquire_single_instance_lock(name) is False, \
            "锁被别人占着时必须拿不到"
        kernel32.CloseHandle(holder)
        assert OpenSAPGUI.acquire_single_instance_lock(name) is True, \
            "别人释放后要能拿到"
    finally:
        OpenSAPGUI.release_single_instance_lock()


def test_run_gui_entry_exits_when_already_running():
    """已有实例时二次启动必须悄悄退出，且把已有窗口拉到前台。"""
    calls = []
    original_acquire = OpenSAPGUI.acquire_single_instance_lock
    original_bring = OpenSAPGUI.bring_window_to_front
    OpenSAPGUI.acquire_single_instance_lock = lambda name=None: False
    OpenSAPGUI.bring_window_to_front = lambda title: calls.append(title) or True

    saved = sys.modules.get("gui_app")
    sys.modules["gui_app"] = None      # 哨兵：真被 import 就会直接报错
    try:
        rc = OpenSAPGUI.run_gui_entry([])
    finally:
        OpenSAPGUI.acquire_single_instance_lock = original_acquire
        OpenSAPGUI.bring_window_to_front = original_bring
        if saved is None:
            sys.modules.pop("gui_app", None)
        else:
            sys.modules["gui_app"] = saved

    assert rc == 0, "已有一个实例在跑时，第二次启动要安静地退出"
    assert calls == [OpenSAPGUI.WINDOW_TITLE], "要把已有窗口拉到前台"
    assert saved is None or "gui_app" in sys.modules


def test_spec_stays_folder_mode():
    """打包形态必须是 onedir —— 单文件模式才会解压 _MEI 临时目录、弹那个警告框。

    改回单文件（把 a.binaries 塞回 EXE、去掉 COLLECT）就会让
    "Failed to remove temporary directory" 复现，这里守住。
    """
    spec_path = pathlib.Path(__file__).resolve().parent.parent / "OpenSAPGUI.spec"
    spec = spec_path.read_text(encoding="utf-8")

    assert "COLLECT(" in spec, "onedir 必须靠 COLLECT 把运行库落到文件夹里"
    assert "contents_directory='_internal'" in spec, "依赖目录固定为 _internal"

    exe_block = spec.split("exe = EXE(")[1].split("\n)")[0]
    assert "exclude_binaries=True" in exe_block, "EXE 不应自己内嵌运行库"
    assert "a.binaries" not in exe_block, "a.binaries 进了 EXE 就是单文件模式了"


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
