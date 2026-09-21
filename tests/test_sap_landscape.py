# -*- coding: utf-8 -*-
"""Mock 测试：SAP Logon 连接列表的解析（SAPUILandscape.xml / saplogon.ini）。

全部用临时文件里的样本数据，不碰真实配置。
直接运行：python tests/test_sap_landscape.py
"""
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sap_landscape import (
    SapService,
    client_candidates,
    connection_names,
    landscape_files,
    load_services,
    parse_landscape_xml,
    parse_saplogon_ini,
    resolve_landscape_path,
    tcode_candidates,
)

try:
    from config_store import application_dir
except ImportError:  # pragma: no cover
    application_dir = None

LANDSCAPE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<Landscape updated="2026-06-25T08:05:32Z" version="1" generator="SAP GUI for Windows v7700">
  <Workspace uuid="w1" name="Local">
    <Item uuid="i1"/>
  </Workspace>
  <Service type="SAPGUI" uuid="s1" name="BH-1D" systemid="BHD" server="10.1.5.33:3200"/>
  <Service type="SAPGUI" uuid="s2" name="BH-2Q" systemid="BHQ" server="10.1.5.34:3200"/>
  <Service type="Reference" uuid="r1" name="BH800-PO" description="BH-3P" systemid="BHP" client="800"/>
  <Service type="Reference" uuid="r2" name="BH120-PO" description="BH-1D" systemid="BHD" client="120"/>
  <Service type="Reference" uuid="r3" name="BH110-SE09"/>
</Landscape>
"""

SAPLOGON_INI = """[Description]
Item1=BH-1D
Item2=BH800-PO
[System]
Item1=BH-1D : : /H/10.1.5.33/S/3200
Item2=BH-1D : :
[Client]
Item1=
Item2=800
[User]
Item1=user1
Item2=user2
"""


def make_temp_dir():
    return tempfile.TemporaryDirectory(prefix="saplandscape-test-")


def write(path: pathlib.Path, content: str, encoding: str = "utf-8") -> pathlib.Path:
    path.write_text(content, encoding=encoding)
    return path


def test_parse_landscape_xml_reads_both_kinds():
    with make_temp_dir() as folder:
        path = write(pathlib.Path(folder) / "SAPUILandscape.xml", LANDSCAPE_XML)
        services = parse_landscape_xml(path)

        systems = [s for s in services if s.kind == "system"]
        shortcuts = [s for s in services if s.kind == "shortcut"]
        assert [s.name for s in systems] == ["BH-1D", "BH-2Q"]
        assert len(shortcuts) == 3
        bh800 = next(s for s in shortcuts if s.name == "BH800-PO")
        assert bh800.client == "800"
        assert bh800.description == "BH-3P"
        # 没有 client 的快捷方式也要读出来，只是 client 为空
        no_client = next(s for s in shortcuts if s.name == "BH110-SE09")
        assert no_client.client == ""


def test_parse_landscape_xml_tolerates_garbage():
    with make_temp_dir() as folder:
        broken = write(pathlib.Path(folder) / "broken.xml", "{ 这不是 XML")
        assert parse_landscape_xml(broken) == []
        assert parse_landscape_xml(pathlib.Path(folder) / "missing.xml") == []


def test_parse_saplogon_ini():
    with make_temp_dir() as folder:
        path = write(pathlib.Path(folder) / "saplogon.ini", SAPLOGON_INI)
        services = parse_saplogon_ini(path)

        assert len(services) == 2
        direct = services[0]
        assert direct.name == "BH-1D"
        assert direct.client == ""
        via_shortcut = services[1]
        assert via_shortcut.name == "BH-1D"
        assert via_shortcut.client == "800"


def test_parse_saplogon_ini_utf16():
    """老版 SAP Logon 写的 ini 常是 UTF-16，要能读。"""
    with make_temp_dir() as folder:
        path = write(pathlib.Path(folder) / "saplogon.ini", SAPLOGON_INI, encoding="utf-16")
        services = parse_saplogon_ini(path)
        assert len(services) == 2


def test_connection_names_and_client_candidates():
    services = [
        SapService(name="BH-1D"),
        SapService(name="BH-2Q"),
        SapService(name="BH120-PO", client="120", kind="shortcut", description="BH-1D"),
        SapService(name="BH100-SIMG", client="100", kind="shortcut", description="BH-1D"),
        SapService(name="BH610-PO", client="610", kind="shortcut", description="BH-2Q"),
    ]
    assert connection_names(services) == ["BH-1D", "BH-2Q"]
    assert client_candidates(services, "BH-1D") == ["100", "120"]
    assert client_candidates(services, "BH-2Q") == ["610"]
    assert client_candidates(services, "BH-3P") == [], "没配过的连接没有候选"


def test_client_candidates_case_insensitive_and_empty():
    services = [
        SapService(name="BH120-PO", client="120", kind="shortcut", description="BH-1D"),
    ]
    assert client_candidates(services, " bh-1d ") == ["120"], "大小写和首尾空格要能对上"
    assert client_candidates(services, "") == [], "空连接名直接给空候选"


def test_connection_names_falls_back_to_shortcut_targets():
    """SAP Logon 里只配了快捷方式时，用指向的连接名兜底。"""
    services = [
        SapService(name="BH120-PO", client="120", kind="shortcut", description="BH-1D"),
    ]
    assert connection_names(services) == ["BH-1D"]


# --------------------------------------------------------------------------- #
# 全局设置里手工指定的景观文件
# --------------------------------------------------------------------------- #
def test_resolve_landscape_path():
    assert resolve_landscape_path("") is None
    assert resolve_landscape_path("   ") is None

    absolute = pathlib.Path(r"C:\share\SAPUILandscape.xml")
    assert resolve_landscape_path(str(absolute)) == absolute

    # 相对路径按程序所在目录算，和 saplogon 路径、日志文件的规则一致
    relative = resolve_landscape_path("SAPUILandscape.xml")
    if application_dir is not None:
        assert relative == application_dir() / "SAPUILandscape.xml"


def test_extra_landscape_file_is_read():
    """设置里指定的景观文件要能读到（企业共享盘场景）。"""
    with make_temp_dir() as folder:
        path = write(pathlib.Path(folder) / "SAPUILandscape.xml", LANDSCAPE_XML)

        files = landscape_files([str(path)])
        assert files and pathlib.Path(files[0]) == path, "指定的文件排在最前面"

        names = connection_names(load_services([str(path)]))
        assert "BH-1D" in names and "BH-2Q" in names


def test_extra_landscape_file_missing_is_ignored():
    """指定的文件不存在（比如共享盘没挂上）时安静跳过，不影响其它来源。"""
    services = load_services([str(pathlib.Path("X:/nope/SAPUILandscape.xml"))])
    assert isinstance(services, list)


# --------------------------------------------------------------------------- #
# client 候选的兜底
# --------------------------------------------------------------------------- #
def test_client_fallback_used_when_no_shortcuts():
    """目标电脑上没建快捷方式 -> 用全局设置里的默认候选兜底。"""
    only_systems = [SapService(name="BH-1D"), SapService(name="BH-2Q")]
    fallback = ["100", "110", "120"]

    assert client_candidates(only_systems, "BH-1D", fallback=fallback) == fallback
    assert client_candidates(only_systems, "BH-1D") == [], "没给兜底就还是空"


def test_client_fallback_ignored_when_landscape_has_candidates():
    """本机 SAP Logon 里有该连接的 client 时，以它为准，不掺杂兜底候选。"""
    services = [
        SapService(name="BH-1D"),
        SapService(name="BH120-PO", client="120", kind="shortcut", description="BH-1D"),
    ]
    assert client_candidates(services, "BH-1D", fallback=["999"]) == ["120"]


def test_client_fallback_not_used_for_empty_connection():
    """还没填连接名时不该冒出兜底候选，避免误导。"""
    assert client_candidates([], "", fallback=["100", "110"]) == []


# --------------------------------------------------------------------------- #
# 快捷方式里的启动事务码（cmd）
# --------------------------------------------------------------------------- #
LANDSCAPE_XML_CMD = """<?xml version="1.0" encoding="UTF-8"?>
<Landscape version="1">
  <Service type="SAPGUI" uuid="s1" name="BH-1D"/>
  <Service type="Reference" uuid="r1" name="BH110-SE80" description="BH-1D" client="110" cmd="SE80"/>
  <Service type="Reference" uuid="r2" name="BH120-PO" description="BH-1D" client="120" cmd="PO"/>
  <Service type="Reference" uuid="r3" name="BH100-SIMG" description="BH-1D" client="100" cmd="SIMG"/>
</Landscape>
"""


def test_parse_landscape_xml_reads_cmd():
    with make_temp_dir() as folder:
        path = write(pathlib.Path(folder) / "SAPUILandscape.xml", LANDSCAPE_XML_CMD)
        services = parse_landscape_xml(path)
        se80 = next(s for s in services if s.name == "BH110-SE80")
        assert se80.cmd == "SE80"
        assert se80.client == "110"


def test_tcode_candidates_filter_by_client():
    with make_temp_dir() as folder:
        path = write(pathlib.Path(folder) / "SAPUILandscape.xml", LANDSCAPE_XML_CMD)
        services = parse_landscape_xml(path)
        assert tcode_candidates(services, "BH-1D", "110") == ["SE80"]
        assert tcode_candidates(services, "BH-1D", "120") == ["PO"]
        assert tcode_candidates(services, "BH-1D") == ["SE80", "PO", "SIMG"]
        assert tcode_candidates(services, "BH-2Q") == [], "没配过的连接没有事务码候选"
        assert tcode_candidates(services, "") == []


def test_tcode_candidates_without_client_limit():
    """快捷方式没写 client 时，任何 client 都能带出它的事务码。"""
    services = [
        SapService(name="BH-1D"),
        SapService(name="BH-PO", kind="shortcut", description="BH-1D", cmd="PO"),
    ]
    assert tcode_candidates(services, "BH-1D", "999") == ["PO"]


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
