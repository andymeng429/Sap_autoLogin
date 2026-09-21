# -*- coding: utf-8 -*-
"""Mock 测试：配置存储、DPAPI 加密、.env 迁移、登录目标解析。

全部在临时目录里跑，不碰真实的 config.json / .env。
直接运行：python tests/test_config_store.py
"""
import base64
import json
import os
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from config_store import (
    AFTER_LOGIN_CHOICES,
    AFTER_LOGIN_LABELS,
    DEFAULT_AFTER_LOGIN,
    AppConfig,
    AppOptions,
    ConfigError,
    ConfigStore,
    ConnectionEntry,
    decrypt_password,
    encrypt_password,
    env_rules_to_text,
    is_encrypted,
    match_environment,
    normalize_after_login,
    parse_client_list,
    parse_env_rules,
    resolve_saplogon_path,
    saplogon_candidates,
    sort_entries,
)
from sap_core import Settings, SettingsError, resolve_target

FAKE_PASSWORD = "p@ss word 测试 123"


def make_temp_dir():
    return tempfile.TemporaryDirectory(prefix="opensapgui-test-")


# --------------------------------------------------------------------------- #
# 加密
# --------------------------------------------------------------------------- #
def test_password_roundtrip():
    encrypted = encrypt_password(FAKE_PASSWORD)
    assert is_encrypted(encrypted), "加密结果应带 dpapi: 前缀"
    assert FAKE_PASSWORD not in encrypted, "密文里不该出现明文"
    assert decrypt_password(encrypted) == FAKE_PASSWORD


def test_password_empty_string():
    assert encrypt_password("") == ""
    assert decrypt_password("") == ""


def test_plaintext_value_is_tolerated():
    """手工把明文写进配置文件时按明文处理，不做无谓报错。"""
    assert is_encrypted("hello") is False
    assert decrypt_password("hello") == "hello"


def test_encrypted_value_survives_a_broken_config():
    """伪装成密文的垃圾值必须报出人话，而不是抛底层异常。"""
    try:
        decrypt_password("dpapi:这不是base64!!")
    except ConfigError as exc:
        assert "Base64" in str(exc) or "解不开" in str(exc), str(exc)
        return
    raise AssertionError("坏掉的密文应当抛 ConfigError")


# --------------------------------------------------------------------------- #
# 条目
# --------------------------------------------------------------------------- #
def test_entry_completeness():
    entry = ConnectionEntry(connection="BH-1D", client="120", user="BH_002", password="x")
    assert entry.is_complete is True
    assert entry.missing_fields() == []

    entry = ConnectionEntry(connection="BH-1D")
    assert entry.is_complete is False
    assert entry.missing_fields() == ["client", "用户名", "密码"]


def test_entry_display_name():
    assert ConnectionEntry(connection="BH-1D", client="120").display_name == "BH-1D / 120"
    assert ConnectionEntry(connection="BH-1D", label="生产").display_name == "生产"


def test_entry_repr_hides_password():
    entry = ConnectionEntry(connection="BH-1D", client="120", user="u", password="topsecret")
    assert "topsecret" not in repr(entry)


# --------------------------------------------------------------------------- #
# 读写
# --------------------------------------------------------------------------- #
def test_save_and_load_roundtrip():
    with make_temp_dir() as folder:
        store = ConfigStore(pathlib.Path(folder) / "config.json")
        config = AppConfig(
            options=AppOptions(saplogon_path=r"D:\SAP\saplogon.exe", popup_timeout=5),
            entries=[
                ConnectionEntry(
                    connection="BH-1D", client="120", user="BH_002",
                    password=FAKE_PASSWORD, label="生产 120",
                )
            ],
        )
        store.save(config)

        raw_text = store.path.read_text(encoding="utf-8")
        assert FAKE_PASSWORD not in raw_text, "config.json 里绝不能出现明文密码"

        loaded = store.load()
        assert loaded.options.saplogon_path == r"D:\SAP\saplogon.exe"
        assert loaded.options.popup_timeout == 5
        assert len(loaded.entries) == 1

        entry = loaded.entries[0]
        assert entry.connection == "BH-1D"
        assert entry.client == "120"
        assert entry.label == "生产 120"
        assert entry.password == FAKE_PASSWORD, "读回来应该还原成明文"
        assert entry.entry_id == config.entries[0].entry_id, "entry_id 必须保持不变"


def test_saved_file_is_valid_json_with_version():
    with make_temp_dir() as folder:
        store = ConfigStore(pathlib.Path(folder) / "config.json")
        store.save(AppConfig())
        payload = json.loads(store.path.read_text(encoding="utf-8"))
        assert payload["version"] == 1
        assert payload["entries"] == []


def test_missing_config_without_env_gives_empty_config():
    with make_temp_dir() as folder:
        config = ConfigStore(pathlib.Path(folder) / "config.json").load()
        assert config.entries == []
        assert config.options.saplogon_path


def test_broken_json_reports_clearly():
    with make_temp_dir() as folder:
        path = pathlib.Path(folder) / "config.json"
        path.write_text("{ 这不是 json", encoding="utf-8")
        try:
            ConfigStore(path).load()
        except ConfigError as exc:
            assert "json" in str(exc).lower() or "JSON" in str(exc), str(exc)
            return
        raise AssertionError("坏 JSON 应当抛 ConfigError")


def test_newer_version_is_refused():
    with make_temp_dir() as folder:
        path = pathlib.Path(folder) / "config.json"
        path.write_text(json.dumps({"version": 99, "entries": []}), encoding="utf-8")
        try:
            ConfigStore(path).load()
        except ConfigError as exc:
            assert "版本" in str(exc), str(exc)
            return
        raise AssertionError("更高版本应当拒绝加载")


# --------------------------------------------------------------------------- #
# .env 迁移
# --------------------------------------------------------------------------- #
LEGACY_ENV = """\
SAPLOGON_PATH=C:\\Program Files (x86)\\SAP\\FrontEnd\\SAPgui\\saplogon.exe
SAP_DEFAULT_CLIENT=120
SAP_STARTUP_TIMEOUT=25
SAP_POPUP_TIMEOUT=6
SAP_LOG_FILE=OpenSAPGUI.log

SAP_CONN_1D_NAME=BH-1D
SAP_CONN_1D_USER=BH_002
SAP_CONN_1D_PASSWORD={password}

SAP_CONN_2Q_NAME=BH-2Q
SAP_CONN_2Q_USER=BH_002
SAP_CONN_2Q_PASSWORD={password}

SAP_CONN_3P_NAME=BH-3P
SAP_CONN_3P_USER=BH_002
SAP_CONN_3P_PASSWORD={password}

SAP_CLIENT_MAP=8:BH-3P,6:BH-2Q
SAP_CLIENT_FALLBACK=BH-1D
""".format(password=FAKE_PASSWORD)


def test_migrate_from_legacy_env():
    with make_temp_dir() as folder:
        folder_path = pathlib.Path(folder)
        (folder_path / ".env").write_text(LEGACY_ENV, encoding="utf-8")
        store = ConfigStore(folder_path / "config.json")

        config = store.load()
        assert store.migrated_last_load is True, "迁移发生时必须告知界面"

        assert [e.connection for e in config.entries] == ["BH-1D", "BH-2Q", "BH-3P"]
        assert all(e.user == "BH_002" for e in config.entries)
        assert all(e.password == FAKE_PASSWORD for e in config.entries)

        # 兜底连接能确定地用 SAP_DEFAULT_CLIENT，其余 client 留空等用户补
        by_connection = {e.connection: e for e in config.entries}
        assert by_connection["BH-1D"].client == "120"
        assert by_connection["BH-3P"].client == ""

        # 旧的映射规则要保留，老快捷方式 "OpenSAPGUI.exe 800" 才能继续用
        assert config.options.client_rules == [["8", "BH-3P"], ["6", "BH-2Q"]]
        assert config.options.startup_timeout == 25
        assert config.options.popup_timeout == 6

        # 迁移结果要落盘，且 .env 清洗后留档：明文密码不能继续留在磁盘上
        assert store.exists(), "迁移后应写出 config.json"
        assert not (folder_path / ".env").exists()
        archived = (folder_path / ".env.migrated").read_text(encoding="utf-8")
        assert FAKE_PASSWORD not in archived, "留档文件里也不能留明文密码"
        assert "SAP_CONN_1D_PASSWORD" in archived, "但键名要保留，方便用户对照"
        assert FAKE_PASSWORD not in store.path.read_text(encoding="utf-8")


def test_migrated_config_is_not_migrated_twice():
    with make_temp_dir() as folder:
        folder_path = pathlib.Path(folder)
        (folder_path / ".env").write_text(LEGACY_ENV, encoding="utf-8")
        store = ConfigStore(folder_path / "config.json")
        store.load()

        # 第二次加载读的是 config.json，不该再动 .env，也不该再触发迁移提示
        again = store.load()
        assert store.migrated_last_load is False
        assert len(again.entries) == 3


def test_env_without_connections_is_not_migrated():
    """.env 里只有日志路径没有连接 —— 别迁移出个空壳卡住用户。"""
    with make_temp_dir() as folder:
        folder_path = pathlib.Path(folder)
        (folder_path / ".env").write_text("SAP_LOG_FILE=x.log\n", encoding="utf-8")
        store = ConfigStore(folder_path / "config.json")
        config = store.load()
        assert config.entries == []
        assert not store.exists()
        assert (folder_path / ".env").exists(), "没迁移就不该动 .env"


# --------------------------------------------------------------------------- #
# 登录目标解析
# --------------------------------------------------------------------------- #
def build_settings(entries, client_rules=()):
    return Settings.from_config(
        AppConfig(
            options=AppOptions(client_rules=[list(rule) for rule in client_rules]),
            entries=entries,
        )
    )


def sample_entries():
    return [
        ConnectionEntry(connection="BH-1D", client="120", user="u1", password="p1", label="生产120"),
        ConnectionEntry(connection="BH-1D", client="110", user="u1", password="p1", label="生产110"),
        ConnectionEntry(connection="BH-3P", client="", user="u3", password="p3", label="测试3P"),
    ]


def test_resolve_by_exact_client():
    target = resolve_target(build_settings(sample_entries()), "110")
    assert target.client == "110"
    assert target.label == "生产110"
    assert target.connection == "BH-1D"


def test_resolve_by_connection_name():
    """client 栏是空的条目，按连接名也能选中，并且 client 沿用用户输入。"""
    settings = build_settings(sample_entries(), client_rules=(("8", "BH-3P"),))
    target = resolve_target(settings, "BH-3P")
    assert target.connection == "BH-3P"
    assert target.client == ""


def test_resolve_by_label():
    target = resolve_target(build_settings(sample_entries()), "生产120")
    assert target.client == "120"


def test_resolve_by_legacy_prefix_rule():
    """旧快捷方式 OpenSAPGUI.exe 800 -> 走 BH-3P，client 用输入的完整值。"""
    settings = build_settings(sample_entries(), client_rules=(("8", "BH-3P"),))
    target = resolve_target(settings, "800")
    assert target.connection == "BH-3P"
    assert target.client == "800"


def test_resolve_by_client_prefix():
    settings = build_settings(sample_entries())
    assert resolve_target(settings, "12").client == "120"


def test_resolve_without_keyword_uses_first():
    target = resolve_target(build_settings(sample_entries()), None)
    assert target.client == "120"


def test_resolve_reports_available_entries():
    settings = build_settings(sample_entries())
    try:
        resolve_target(settings, "999")
    except SettingsError as exc:
        message = str(exc)
        assert "999" in message
        assert "生产120" in message, "报错里应列出可用入口"
        return
    raise AssertionError("匹配不到时必须抛 SettingsError")


def test_resolve_rejects_when_nothing_enabled():
    entries = sample_entries()
    entries[0].enabled = False
    entries[1].enabled = False
    entries[2].enabled = False
    try:
        resolve_target(build_settings(entries), None)
    except SettingsError as exc:
        assert "没有可用" in str(exc)
        return
    raise AssertionError("没有启用条目时必须抛 SettingsError")


def test_entry_repr_and_from_json_tolerate_int_client():
    """JSON 里 client 写成数字（120 而不是 "120"）也要能读。"""
    entry = ConnectionEntry.from_json({
        "connection": "BH-1D", "client": 120, "user": "u", "password": "p",
    })
    assert entry.client == "120"
    assert entry.is_complete


# --------------------------------------------------------------------------- #
# 换电脑 / 换 Windows 账号：密文解不开时的降级行为
# --------------------------------------------------------------------------- #
def foreign_password() -> str:
    """伪造一段"合法 base64 但本机 DPAPI 解不开"的密文，模拟配置来自别的电脑。"""
    return "dpapi:" + base64.b64encode(os.urandom(96)).decode("ascii")


def write_foreign_config(path: pathlib.Path, entries: list[dict]) -> None:
    path.write_text(
        json.dumps({"version": 1, "entries": entries}, ensure_ascii=False),
        encoding="utf-8",
    )


def test_foreign_password_keeps_other_fields():
    """解不开密码不能拖垮整份配置：其余字段要完整保留，只提示缺密码。"""
    with make_temp_dir() as folder:
        path = pathlib.Path(folder) / "config.json"
        write_foreign_config(path, [{
            "id": "aaa", "connection": "BH-1D", "client": "120", "user": "BH_002",
            "label": "生产120", "enabled": True, "password": foreign_password(),
        }])

        config = ConfigStore(path).load()
        assert len(config.entries) == 1, "条目本身要能读出来"
        entry = config.entries[0]
        assert (entry.connection, entry.client, entry.user) == ("BH-1D", "120", "BH_002")
        assert entry.label == "生产120"
        assert entry.enabled is True
        assert entry.password == "", "解不开的密码要留空"
        assert entry.password_locked is True
        assert entry.is_complete is False
        assert entry.missing_fields() == ["密码"], "只该提示缺密码"


def test_foreign_password_counts_and_keeps_usable_entries():
    """一份配置里混着本机条目和外来条目时，两边都要正确处理。"""
    with make_temp_dir() as folder:
        path = pathlib.Path(folder) / "config.json"
        write_foreign_config(path, [
            {"id": "aaa", "connection": "BH-1D", "client": "120", "user": "u1",
             "password": foreign_password()},
            {"id": "bbb", "connection": "BH-3P", "client": "800", "user": "u2",
             "password": encrypt_password(FAKE_PASSWORD)},
        ])

        store = ConfigStore(path)
        config = store.load()
        assert store.locked_password_last_load == 1, "只该统计解不开的那条"

        good = config.find_entry_by_connection("BH-3P")
        assert good.password == FAKE_PASSWORD
        assert good.password_locked is False
        assert good.is_complete is True


def test_locked_counter_resets_between_loads():
    """换回正常配置后计数必须归零，否则界面会一直提示要重填密码。"""
    with make_temp_dir() as folder:
        path = pathlib.Path(folder) / "config.json"
        write_foreign_config(path, [{
            "connection": "BH-1D", "client": "120", "user": "u",
            "password": foreign_password(),
        }])
        store = ConfigStore(path)
        store.load()
        assert store.locked_password_last_load == 1

        write_foreign_config(path, [{
            "connection": "BH-1D", "client": "120", "user": "u",
            "password": encrypt_password(FAKE_PASSWORD),
        }])
        store.load()
        assert store.locked_password_last_load == 0


def test_locked_flag_never_lands_on_disk():
    """password_locked 只是内存标记，保存后不能出现在 config.json 里。"""
    with make_temp_dir() as folder:
        path = pathlib.Path(folder) / "config.json"
        write_foreign_config(path, [{
            "connection": "BH-1D", "client": "120", "user": "u",
            "password": foreign_password(),
        }])

        store = ConfigStore(path)
        config = store.load()
        store.save(config)

        text = path.read_text(encoding="utf-8")
        assert "password_locked" not in text
        assert FAKE_PASSWORD not in text
        # 留空的密码不该被加密成一坨垃圾密文
        assert json.loads(text)["entries"][0]["password"] == ""


def test_foreign_password_copy_keeps_flag():
    """编辑框取消后回填副本时，标记不能丢（否则提示会消失但密码仍未填）。"""
    entry = ConnectionEntry(connection="BH-1D", client="120", user="u",
                            password="", password_locked=True)
    assert entry.copy().password_locked is True


# --------------------------------------------------------------------------- #
# 置顶与列表排序
# --------------------------------------------------------------------------- #
def test_pinned_roundtrip():
    """pinned 是持久字段：保存再读回应保持一致。"""
    with make_temp_dir() as folder:
        path = pathlib.Path(folder) / "config.json"
        config = AppConfig(entries=[
            ConnectionEntry(connection="BH-1D", client="120", user="u",
                            password=FAKE_PASSWORD, pinned=True),
            ConnectionEntry(connection="BH-3P", client="800", user="u",
                            password=FAKE_PASSWORD, pinned=False),
        ])
        store = ConfigStore(path)
        store.save(config)

        text = path.read_text(encoding="utf-8")
        assert '"pinned": true' in text
        loaded = store.load()
        assert loaded.entries[0].pinned is True
        assert loaded.entries[1].pinned is False


def test_old_config_without_pinned_field_loads_as_false():
    """旧版 config.json 没有 pinned 字段，读取后应默认 False，不报错。"""
    entry = ConnectionEntry.from_json({
        "connection": "BH-1D", "client": "120", "user": "u", "password": "p",
    })
    assert entry.pinned is False


def test_sort_entries_pins_first_then_by_name():
    """置顶组在最前（组内也按名称），非置顶组按名称排。"""
    make = lambda label, pinned=False, client="100": ConnectionEntry(  # noqa: E731
        connection="CONN", client=client, label=label, password="x", pinned=pinned
    )
    entries = [make("Zeta"), make("Alpha", pinned=True), make("Mid"),
               make("Beta", pinned=True)]
    ordered = sort_entries(entries)

    assert [e.label for e in ordered] == ["Alpha", "Beta", "Mid", "Zeta"]
    # 不改传入列表本身（config.entries 保持添加顺序）
    assert [e.label for e in entries] == ["Zeta", "Alpha", "Mid", "Beta"]


def test_sort_entries_same_name_uses_client():
    """同名条目（不同 client）按 client 排，顺序稳定。"""
    make = lambda client: ConnectionEntry(  # noqa: E731
        connection="BH-1D", client=client, password="x"
    )
    ordered = sort_entries([make("800"), make("110"), make("610")])
    assert [e.client for e in ordered] == ["110", "610", "800"]


# --------------------------------------------------------------------------- #
# saplogon.exe 路径探测
# --------------------------------------------------------------------------- #
def test_saplogon_candidates_put_configured_path_first():
    cands = saplogon_candidates(r"D:\MySAP\saplogon.exe")
    assert cands[0] == pathlib.Path(r"D:\MySAP\saplogon.exe")
    assert pathlib.Path(
        r"C:\Program Files\SAP\FrontEnd\SAPgui\saplogon.exe"
    ) in cands, "64 位 Program Files 也该在候选里"
    assert pathlib.Path(
        r"C:\Program Files (x86)\SAP\FrontEnd\SAPgui\saplogon.exe"
    ) in cands, "32 位 Program Files 也该在候选里"


def test_saplogon_candidates_join_dir_or_sapgui_dir():
    """注册表给的可能已经是 ...\\SAPgui，也可能是它的上级目录，两种都要能拼对。"""
    cands = saplogon_candidates("", extra_dirs=[r"D:\SAP\FrontEnd", r"D:\SAP\FrontEnd\SAPgui"])
    assert pathlib.Path(r"D:\SAP\FrontEnd\SAPgui\saplogon.exe") in cands
    lowered = [str(c).lower() for c in cands]
    assert len(lowered) == len(set(lowered)), "候选之间不该重复"


def test_resolve_saplogon_falls_back_when_configured_path_is_wrong():
    """配置里写的是 32 位路径、机器上只有 64 位安装时，要能自动纠正。"""
    x64 = r"C:\Program Files\SAP\FrontEnd\SAPgui\saplogon.exe"
    found = resolve_saplogon_path(
        r"C:\Program Files (x86)\SAP\FrontEnd\SAPgui\saplogon.exe",
        # Windows 路径大小写不敏感，注册表返回的目录大小写可能不同
        exists=lambda p: str(p).lower() == x64.lower(),
    )
    assert found is not None, "应能回退到 64 位安装路径"
    assert str(found).lower() == x64.lower()
    assert "x86" not in str(found).lower(), "不该选中配置里那个不存在的路径"


def test_resolve_saplogon_prefers_configured_path_when_valid():
    custom = r"D:\Tools\SAP\saplogon.exe"
    found = resolve_saplogon_path(custom, exists=lambda p: True)
    assert str(found) == custom


def test_resolve_saplogon_returns_none_when_nothing_exists():
    """一个都找不到时返回 None（由调用方给出带候选清单的报错），不抛异常。"""
    assert resolve_saplogon_path(r"X:\nope\saplogon.exe", exists=lambda p: False) is None


def test_resolve_saplogon_skips_problematic_candidates():
    """存在性检查抛异常（权限/网络盘）不能让整个探测失败。"""
    def broken(path):
        if "Program Files (x86)" in str(path):
            raise OSError("网络盘不可达")
        return False

    assert resolve_saplogon_path(r"C:\Program Files (x86)\SAP\FrontEnd\SAPgui\saplogon.exe",
                                 exists=broken) is None


# --------------------------------------------------------------------------- #
# 下拉框的数据来源设置
# --------------------------------------------------------------------------- #
def test_parse_client_list_accepts_messy_input():
    assert parse_client_list("100,110,120") == ["100", "110", "120"]
    assert parse_client_list("100, 110  120") == ["100", "110", "120"]
    assert parse_client_list("100，110；120、610") == ["100", "110", "120", "610"]
    assert parse_client_list("100,100,110") == ["100", "110"], "重复的要去掉"
    assert parse_client_list("") == []
    assert parse_client_list(None) == []


def test_options_roundtrip_keeps_dropdown_sources():
    options = AppOptions(
        landscape_file=r"\\share\sap\SAPUILandscape.xml",
        default_clients="100,110,120,610,800",
    )
    again = AppOptions.from_json(json.loads(json.dumps(options.to_json())))
    assert again.landscape_file == options.landscape_file
    assert again.default_clients == options.default_clients
    assert again.client_candidate_fallback() == ["100", "110", "120", "610", "800"]


def test_options_default_to_auto_detect():
    """老配置文件里没有这两个键时要落在"自动探测、无兜底"上。"""
    options = AppOptions.from_json({"saplogon_path": r"C:\x\saplogon.exe"})
    assert options.landscape_file == ""
    assert options.default_clients == ""
    assert options.client_candidate_fallback() == []

    assert AppOptions.from_json({}).default_clients == ""
    assert AppOptions.from_json(None).landscape_file == ""


def test_options_with_null_values_are_tolerated():
    """手工把值写成 null 时按空处理，别把界面搞崩。"""
    options = AppOptions.from_json({"landscape_file": None, "default_clients": None})
    assert options.landscape_file == ""
    assert options.client_candidate_fallback() == []


def test_config_file_stores_dropdown_sources():
    with make_temp_dir() as folder:
        store = ConfigStore(pathlib.Path(folder) / "config.json")
        config = AppConfig(
            options=AppOptions(landscape_file=r"D:\sap\SAPUILandscape.xml",
                               default_clients="110, 120"),
            entries=[ConnectionEntry(connection="BH-1D", client="120",
                                     user="u", password=FAKE_PASSWORD)],
        )
        store.save(config)

        again = store.load()
        assert again.options.landscape_file == r"D:\sap\SAPUILandscape.xml"
        assert again.options.client_candidate_fallback() == ["110", "120"]


# --------------------------------------------------------------------------- #
# 事务码字段
# --------------------------------------------------------------------------- #
def test_entry_tcode_field_persists():
    with make_temp_dir() as folder:
        store = ConfigStore(pathlib.Path(folder) / "config.json")
        config = AppConfig(entries=[ConnectionEntry(
            connection="BH-1D", client="110", user="u",
            password=FAKE_PASSWORD, tcode="SE80",
        )])
        store.save(config)

        again = store.load()
        assert again.entries[0].tcode == "SE80"


def test_entry_tcode_defaults_empty():
    """老配置文件没有 tcode 键时按空处理，不影响加载。"""
    entry = ConnectionEntry.from_json({"connection": "BH-1D", "client": "120"})
    assert entry.tcode == ""


def test_resolve_target_carries_tcode():
    config = AppConfig(entries=[ConnectionEntry(
        connection="BH-1D", client="110", user="u", password=FAKE_PASSWORD, tcode="SE80",
    )])
    target = resolve_target(Settings.from_config(config), "110")
    assert target.tcode == "SE80"


# --------------------------------------------------------------------------- #
# 环境判定规则
# --------------------------------------------------------------------------- #
def test_parse_env_rules_variants():
    text = "*D=开发\n*Q=测试；*P=生产\n  client:800=生产 \n垃圾行\n\n"
    assert parse_env_rules(text) == [
        ["*D", "开发"], ["*Q", "测试"], ["*P", "生产"], ["client:800", "生产"],
    ]
    assert parse_env_rules("") == []
    assert parse_env_rules("没有等号") == []
    assert parse_env_rules("*D＝开发") == [["*D", "开发"]], "全角等号也认"
    assert parse_env_rules("*D=开发\n*D=开发") == [["*D", "开发"]], "重复规则去重"


def test_match_environment_by_suffix_and_keyword():
    rules = [["*D", "开发"], ["*Q", "测试"], ["*P", "生产"]]
    assert match_environment(rules, "BH-1D") == "开发"
    assert match_environment(rules, "BH-2Q") == "测试"
    assert match_environment(rules, "BH-3P") == "生产"
    assert match_environment(rules, "bh-2q") == "测试", "大小写不敏感"
    assert match_environment(rules, "ZZ-9X") == "", "不命中返回空"
    assert match_environment([], "BH-1D") == ""


def test_match_environment_by_client_and_priority():
    rules = [["client:800", "生产"], ["*P", "生产"], ["120", "特殊120"]]
    assert match_environment(rules, "BH-1D", "800") == "生产"
    assert match_environment(rules, "BH-1D", "120") == "特殊120", "包含匹配也能命中"
    assert match_environment(rules, "BH-3P", "610") == "生产"
    ordered = [["*P", "生产"], ["client:610", "测试"]]
    assert match_environment(ordered, "BH-2P", "610") == "生产", "*P 先命中就用它"
    assert match_environment(ordered, "BH-2Q", "610") == "测试"


def test_options_env_rules_roundtrip():
    options = AppOptions(env_rules=[["*D", "开发"], ["client:800", "生产"]])
    again = AppOptions.from_json(json.loads(json.dumps(options.to_json())))
    assert again.env_rules == [["*D", "开发"], ["client:800", "生产"]]
    assert AppOptions.from_json({}).env_rules == []
    assert AppOptions.from_json(None).env_rules == []


def test_env_rules_text_roundtrip():
    text = "*D=开发\n*P=生产"
    assert env_rules_to_text(parse_env_rules(text)) == text


# --------------------------------------------------------------------------- #
# 登录完成后的窗口行为
# --------------------------------------------------------------------------- #
def test_after_login_roundtrip():
    options = AppOptions(after_login="behind")
    again = AppOptions.from_json(json.loads(json.dumps(options.to_json())))
    assert again.after_login == "behind"
    assert options.to_json()["after_login"] == "behind"


def test_after_login_defaults_to_minimize():
    """老配置里没有这个字段，读出来要退化成默认的自动最小化。"""
    assert AppOptions().after_login == "minimize"
    assert AppOptions.from_json({}).after_login == DEFAULT_AFTER_LOGIN
    assert AppOptions.from_json(None).after_login == DEFAULT_AFTER_LOGIN


def test_after_login_unknown_value_falls_back():
    assert normalize_after_login("随便填的") == DEFAULT_AFTER_LOGIN
    assert normalize_after_login(None) == DEFAULT_AFTER_LOGIN
    assert normalize_after_login(" BEHIND ") == "behind", "大小写和空格要能容忍"
    assert AppOptions.from_json({"after_login": "boom"}).after_login == DEFAULT_AFTER_LOGIN


def test_after_login_choices_and_labels_agree():
    assert set(AFTER_LOGIN_CHOICES) == {"minimize", "behind", "none"}
    assert DEFAULT_AFTER_LOGIN in AFTER_LOGIN_CHOICES
    assert set(AFTER_LOGIN_LABELS) == set(AFTER_LOGIN_CHOICES), "每个选项都要有中文标签"


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
