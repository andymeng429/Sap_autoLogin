"""配置存储：config.json + Windows DPAPI 加密密码。

设计要点：

* 密码用 Windows DPAPI（`CryptProtectData`）加密后落盘，绑定当前 Windows 账号，
  配置文件里**不出现明文**；换机器或换登录用户后需要重新填一次密码。
* 首次运行如果只找到旧的 `.env`，会自动迁移成 `config.json`，并把 `.env`
  改名为 `.env.migrated` 留档，避免"改了 .env 却不生效"的困惑。
* 保存采用"先写临时文件再替换"，中途崩溃不会把配置写坏。

本模块不依赖 GUI，也不依赖 sap_core，方便单独测试。
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from dotenv import dotenv_values

try:
    import win32crypt
except ImportError:  # pragma: no cover - 非 Windows 环境
    win32crypt = None  # type: ignore[assignment]

try:
    import winreg
except ImportError:  # pragma: no cover - 非 Windows 环境
    winreg = None  # type: ignore[assignment]


CONFIG_FILENAME = "config.json"
LEGACY_ENV_FILENAME = ".env"
CONFIG_VERSION = 1

DEFAULT_SAPLOGON_PATH = r"C:\Program Files (x86)\SAP\FrontEnd\SAPgui\saplogon.exe"
# SAP GUI 可能装在 32 位或 64 位的 Program Files 下，配置里的路径不一定对，
# 运行时按下面这些线索自动纠正（见 resolve_saplogon_path）。
SAPLOGON_RELATIVE = r"SAPgui\saplogon.exe"
SAPLOGON_FALLBACK_DIRS = (
    r"C:\Program Files\SAP\FrontEnd",
    r"C:\Program Files (x86)\SAP\FrontEnd",
)
# 注册表里记录 SAP 安装目录的位置（SAPsysdir / SAPDestDir）
SAP_SHARED_KEYS = (
    (r"SOFTWARE\SAP\SAP Shared", "SAPsysdir"),
    (r"SOFTWARE\SAP\SAP Shared", "SAPDestDir"),
    (r"SOFTWARE\WOW6432Node\SAP\SAP Shared", "SAPsysdir"),
)
DEFAULT_STARTUP_TIMEOUT = 30
DEFAULT_CONNECT_TIMEOUT = 30
DEFAULT_POPUP_TIMEOUT = 8
DEFAULT_LOG_FILE = "OpenSAPGUI.log"

# 登录成功后主窗口怎么让位，免得挡住刚探出来的 SAP GUI。
# minimize=自动最小化 / behind=排到 SAP GUI 后面 / none=什么都不做
AFTER_LOGIN_CHOICES = ("minimize", "behind", "none")
DEFAULT_AFTER_LOGIN = "minimize"
AFTER_LOGIN_LABELS = {
    "minimize": "自动最小化",
    "behind": "排到 SAP GUI 后面",
    "none": "保持原样",
}

# 加密值的前缀标识。没有这个前缀的值按明文处理（方便手工临时改配置）。
DPAPI_PREFIX = "dpapi:"
# 附加熵：本程序专用的"盐"，别的程序即使以同一 Windows 用户身份也解不开。
DPAPI_ENTROPY = b"OpenSAPGUI/v1"
DPAPI_DESCRIPTION = "OpenSAPGUI"

LEGACY_PROFILE_PATTERN = re.compile(r"^SAP_CONN_([A-Za-z0-9_]+)_NAME$")
# .env 里的密码行：迁移留档时要把值清空，明文密码不能留在磁盘上
LEGACY_PASSWORD_LINE_PATTERN = re.compile(
    r"^(\s*SAP_CONN_[A-Za-z0-9_]+_PASSWORD\s*=).*$", re.IGNORECASE | re.MULTILINE
)


class ConfigError(RuntimeError):
    """配置读取/写入/加解密失败。"""


# --------------------------------------------------------------------------- #
# 加解密
# --------------------------------------------------------------------------- #
def encrypt_password(plain: str) -> str:
    """把明文密码加密成可落盘字符串。空串原样返回。"""
    if not plain:
        return ""
    if win32crypt is None:
        raise ConfigError("当前环境没有 pywin32，无法使用 DPAPI 加密密码。")
    blob = win32crypt.CryptProtectData(
        plain.encode("utf-8"), DPAPI_DESCRIPTION, DPAPI_ENTROPY, None, None, 0
    )
    return DPAPI_PREFIX + base64.b64encode(blob).decode("ascii")


def decrypt_password(stored: str) -> str:
    """还原密码。没有 dpapi: 前缀的值按明文处理（兼容手工编辑的配置）。"""
    if not stored:
        return ""
    if not stored.startswith(DPAPI_PREFIX):
        return stored
    if win32crypt is None:
        raise ConfigError("当前环境没有 pywin32，无法解密已保存的密码。")

    try:
        blob = base64.b64decode(stored[len(DPAPI_PREFIX):], validate=True)
    except Exception as exc:  # noqa: BLE001
        raise ConfigError("配置里的密码字段不是合法的 Base64，文件可能被改坏了。") from exc

    try:
        _description, data = win32crypt.CryptUnprotectData(blob, DPAPI_ENTROPY, None, None, 0)
    except Exception as exc:  # noqa: BLE001
        raise ConfigError(
            "无法解密保存的密码。常见原因：配置文件是从别的电脑/别的 Windows 账号"
            "拷过来的，或系统重装过。请重新填写连接密码。"
        ) from exc
    return data.decode("utf-8")


def is_encrypted(value: str) -> bool:
    return bool(value) and value.startswith(DPAPI_PREFIX)


# --------------------------------------------------------------------------- #
# 路径
# --------------------------------------------------------------------------- #
def application_dir() -> Path:
    """程序所在目录。打包成 exe 后是 exe 同级目录，而不是临时解包目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def default_config_path() -> Path:
    """config.json 的位置：环境变量 SAP_CONFIG_FILE > 程序所在目录。"""
    override = os.getenv("SAP_CONFIG_FILE", "").strip()
    if override:
        return Path(override).expanduser()
    return application_dir() / CONFIG_FILENAME


# --------------------------------------------------------------------------- #
# SAP Logon 可执行文件定位
# --------------------------------------------------------------------------- #
def _registry_sap_dirs() -> list[str]:
    """从注册表读 SAP 安装目录；读不到就返回空，不报错。"""
    if winreg is None:
        return []
    dirs: list[str] = []
    for sub_key, value_name in SAP_SHARED_KEYS:
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, sub_key) as key:
                raw, _ = winreg.QueryValueEx(key, value_name)
        except OSError:
            continue
        text = str(raw).strip().rstrip("\\")
        if text and text not in dirs:
            dirs.append(text)
    return dirs


def saplogon_candidates(configured: str = "", extra_dirs: Iterable[str] = ()) -> list[Path]:
    """列出待尝试的 saplogon.exe 路径，按可信度排序：配置值 > 注册表 > 常见安装位置。"""
    candidates: list[Path] = []
    seen: set[str] = set()

    def add(path: Path) -> None:
        key = str(path).lower()
        if key not in seen:
            seen.add(key)
            candidates.append(path)

    if configured.strip():
        add(Path(configured.strip()))

    for folder in [*extra_dirs, *_registry_sap_dirs(), *SAPLOGON_FALLBACK_DIRS]:
        text = str(folder).strip().rstrip("\\")
        if not text:
            continue
        # 注册表里给的可能是 ...\SAPgui 本身，也可能是它的上级目录
        if text.lower().endswith("sapgui"):
            add(Path(text) / "saplogon.exe")
        else:
            add(Path(text) / SAPLOGON_RELATIVE)

    return candidates


def resolve_saplogon_path(configured: str = "", exists=None) -> Optional[Path]:
    """把配置里的路径解析成真实存在的 saplogon.exe；全都不存在时返回 None。

    SAP GUI 装在 32 位还是 64 位的 Program Files 下因机器而异，
    配置里那个默认值不一定对，所以这里按候选顺序逐个核实。
    """
    check = exists if exists is not None else (lambda p: Path(p).exists())
    for candidate in saplogon_candidates(configured):
        try:
            if check(candidate):
                return candidate
        except OSError:
            continue
    return None


# --------------------------------------------------------------------------- #
# 数据模型
# --------------------------------------------------------------------------- #
def new_entry_id() -> str:
    return uuid.uuid4().hex[:12]


@dataclass
class ConnectionEntry:
    """一条"可单击进入"的登录入口：连接名 + client + 凭据。"""

    connection: str = ""
    client: str = ""
    user: str = ""
    password: str = ""          # 内存中始终是明文，落盘时自动加密
    label: str = ""
    # 登录成功后要进入的事务码，可留空。SAP Logon 快捷方式里的 cmd 能自动带出。
    tcode: str = ""
    enabled: bool = True
    entry_id: str = field(default_factory=new_entry_id)
    # 置顶：列表排序时固定在最前（置顶组内仍按名称排序）。
    pinned: bool = False
    # 只存在于内存：配置文件里的密码是密文但本机解不开（换了电脑或重装了系统）。
    # 此时保留其它字段、把密码留空，等用户在界面上补填，不落盘。
    password_locked: bool = False

    @property
    def display_name(self) -> str:
        """界面上显示的标题。"""
        if self.label.strip():
            return self.label.strip()
        return f"{self.connection} / {self.client}" if self.client else self.connection

    @property
    def is_complete(self) -> bool:
        """是否已填全登录必需的四项。缺 client 时不允许直接登录。"""
        return bool(self.connection and self.client and self.user and self.password)

    def missing_fields(self) -> list[str]:
        missing = []
        if not self.connection:
            missing.append("连接名")
        if not self.client:
            missing.append("client")
        if not self.user:
            missing.append("用户名")
        if not self.password:
            missing.append("密码")
        return missing

    def copy(self) -> "ConnectionEntry":
        return ConnectionEntry(
            connection=self.connection,
            client=self.client,
            user=self.user,
            password=self.password,
            label=self.label,
            tcode=self.tcode,
            enabled=self.enabled,
            entry_id=self.entry_id,
            pinned=self.pinned,
            password_locked=self.password_locked,
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.entry_id,
            "label": self.label,
            "connection": self.connection,
            "client": self.client,
            "user": self.user,
            "password": encrypt_password(self.password),
            "tcode": self.tcode,
            "enabled": self.enabled,
            "pinned": self.pinned,
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> "ConnectionEntry":
        if not isinstance(raw, dict):
            raise ConfigError(f"连接条目格式错误，应为对象，实际是 {type(raw).__name__}")

        # 密码解密失败不能拖垮整份配置：把该条密码留空并打标记，
        # 连接名/client/用户名照常加载，用户只需补填密码。
        stored_password = _as_text(raw.get("password"))
        try:
            password = decrypt_password(stored_password)
        except ConfigError:
            password = ""
            locked = bool(stored_password)
        else:
            locked = False

        return cls(
            connection=_as_text(raw.get("connection")),
            client=_as_text(raw.get("client")),
            user=_as_text(raw.get("user")),
            password=password,
            label=_as_text(raw.get("label")),
            tcode=_as_text(raw.get("tcode")),
            enabled=bool(raw.get("enabled", True)),
            entry_id=_as_text(raw.get("id")) or new_entry_id(),
            pinned=bool(raw.get("pinned", False)),
            password_locked=locked,
        )

    def __repr__(self) -> str:  # 避免密码出现在日志里
        return (
            f"ConnectionEntry(label={self.label!r}, connection={self.connection!r}, "
            f"client={self.client!r}, user={self.user!r}, password='***')"
        )


@dataclass
class AppOptions:
    """与具体连接无关的全局设置。"""

    saplogon_path: str = DEFAULT_SAPLOGON_PATH
    startup_timeout: int = DEFAULT_STARTUP_TIMEOUT
    connect_timeout: int = DEFAULT_CONNECT_TIMEOUT
    popup_timeout: int = DEFAULT_POPUP_TIMEOUT
    log_file: str = DEFAULT_LOG_FILE
    # 兼容旧版 .env 的 SAP_CLIENT_MAP：client 前缀 -> 连接名。
    # 命令行传 client 时如果匹配不到条目，就靠它兜底，保证旧快捷方式不改也能用。
    client_rules: list[list[str]] = field(default_factory=list)
    # 下拉框的补充数据源：SAP Logon 景观文件（企业常统一放在共享盘，可在此指定）。
    # 留空则自动探测 %APPDATA%\SAP\Common 和注册表里配置的位置。
    landscape_file: str = ""
    # client 下拉的兜底候选（如 "100,110,120,610,800"）。
    # 目标电脑的 SAP Logon 里没建带 client 的快捷方式时用它，保证分发后仍有下拉可选。
    default_clients: str = ""
    # 环境判定规则，如 [["*D", "开发"], ["*Q", "测试"], ["*P", "生产"], ["client:800", "生产"]]。
    # SAP 景观文件里没有环境信息，只能按用户的命名约定判，规则在全局设置里配。
    env_rules: list[list[str]] = field(default_factory=list)
    # 登录成功后主窗口怎么让位：minimize / behind / none，见 AFTER_LOGIN_CHOICES。
    after_login: str = DEFAULT_AFTER_LOGIN

    def client_candidate_fallback(self) -> list[str]:
        """兜底的 client 候选列表。"""
        return parse_client_list(self.default_clients)

    def to_json(self) -> dict[str, Any]:
        return {
            "saplogon_path": self.saplogon_path,
            "startup_timeout": self.startup_timeout,
            "connect_timeout": self.connect_timeout,
            "popup_timeout": self.popup_timeout,
            "log_file": self.log_file,
            "client_rules": [list(rule) for rule in self.client_rules],
            "landscape_file": self.landscape_file,
            "default_clients": self.default_clients,
            "env_rules": [list(rule) for rule in self.env_rules],
            "after_login": normalize_after_login(self.after_login),
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> "AppOptions":
        if not isinstance(raw, dict):
            raw = {}
        default = cls()
        return cls(
            saplogon_path=_as_text(raw.get("saplogon_path")) or default.saplogon_path,
            startup_timeout=_as_positive_int(raw.get("startup_timeout"), default.startup_timeout),
            connect_timeout=_as_positive_int(raw.get("connect_timeout"), default.connect_timeout),
            popup_timeout=_as_positive_int(raw.get("popup_timeout"), default.popup_timeout),
            log_file=_as_text(raw.get("log_file")),
            client_rules=_as_rules(raw.get("client_rules")),
            landscape_file=_as_text(raw.get("landscape_file")),
            default_clients=_as_text(raw.get("default_clients")),
            env_rules=_as_rules(raw.get("env_rules")),
            after_login=normalize_after_login(raw.get("after_login")),
        )


@dataclass
class AppConfig:
    options: AppOptions = field(default_factory=AppOptions)
    entries: list[ConnectionEntry] = field(default_factory=list)

    def enabled_entries(self) -> list[ConnectionEntry]:
        return [entry for entry in self.entries if entry.enabled]

    def find_entry_by_id(self, entry_id: str) -> Optional[ConnectionEntry]:
        for entry in self.entries:
            if entry.entry_id == entry_id:
                return entry
        return None

    def find_entry_by_connection(self, connection: str) -> Optional[ConnectionEntry]:
        """按 SAP 连接名挑条目，优先取填全了的。"""
        target = connection.strip().lower()
        matches = [e for e in self.entries if e.connection.strip().lower() == target]
        for entry in matches:
            if entry.enabled and entry.is_complete:
                return entry
        for entry in matches:
            if entry.enabled:
                return entry
        return matches[0] if matches else None


def sort_entries(entries: list["ConnectionEntry"]) -> list["ConnectionEntry"]:
    """主界面的显示顺序：置顶的固定在最前，组内按显示名称排序。

    返回新列表，不改传入列表本身的顺序（config.entries 保持用户添加顺序）。
    """
    return sorted(
        entries,
        key=lambda e: (not e.pinned, e.display_name.casefold(), e.client),
    )


# --------------------------------------------------------------------------- #
# 存储
# --------------------------------------------------------------------------- #
class ConfigStore:
    """config.json 的读写，以及从旧 .env 的一次性迁移。"""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path else default_config_path()
        # 最近一次 load() 是否刚完成 .env -> config.json 的迁移。
        # 界面用它决定要不要弹"请补全 client"的提示，只提示这一次。
        self.migrated_last_load = False
        # 最近一次 load() 里有几条连接的密码本机解不开（配置来自别的电脑）。
        self.locked_password_last_load = 0

    # ---------------- 读 ---------------- #
    def exists(self) -> bool:
        return self.path.is_file()

    def legacy_env_path(self) -> Path:
        return self.path.parent / LEGACY_ENV_FILENAME

    def load(self) -> AppConfig:
        """读取配置；文件不存在时尝试从 .env 迁移，再不行就返回默认配置。"""
        self.migrated_last_load = False
        self.locked_password_last_load = 0
        if self.exists():
            return self._load_file()

        migrated = self._migrate_from_env()
        if migrated is not None:
            self.migrated_last_load = True
            self.save(migrated)
            return migrated
        return AppConfig()

    def _load_file(self) -> AppConfig:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ConfigError(
                f"配置文件 {self.path} 不是合法 JSON（第 {exc.lineno} 行）：{exc.msg}"
            ) from exc
        except OSError as exc:
            raise ConfigError(f"无法读取配置文件 {self.path}: {exc}") from exc

        if not isinstance(raw, dict):
            raise ConfigError(f"配置文件 {self.path} 的顶层应该是一个对象。")

        version = raw.get("version", CONFIG_VERSION)
        if isinstance(version, int) and version > CONFIG_VERSION:
            raise ConfigError(
                f"配置文件的版本（{version}）比当前程序支持的版本（{CONFIG_VERSION}）新，"
                "请升级程序后再打开。"
            )

        raw_entries = raw.get("entries", [])
        if not isinstance(raw_entries, list):
            raise ConfigError("配置文件的 entries 字段应该是数组。")

        entries = [ConnectionEntry.from_json(item) for item in raw_entries]
        self.locked_password_last_load = sum(1 for entry in entries if entry.password_locked)

        return AppConfig(
            options=AppOptions.from_json(raw.get("options", {})),
            entries=entries,
        )

    # ---------------- 写 ---------------- #
    def save(self, config: AppConfig) -> None:
        payload = {
            "version": CONFIG_VERSION,
            "options": config.options.to_json(),
            "entries": [entry.to_json() for entry in config.entries],
        }
        text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"

        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # 先写临时文件再原子替换：写到一半崩溃也不会留下半截配置
            temp_path = self.path.with_name(self.path.name + ".tmp")
            temp_path.write_text(text, encoding="utf-8")
            os.replace(temp_path, self.path)
        except OSError as exc:
            raise ConfigError(f"无法写入配置文件 {self.path}: {exc}") from exc

    # ---------------- 从旧 .env 迁移 ---------------- #
    def _migrate_from_env(self) -> Optional[AppConfig]:
        env_path = self.legacy_env_path()
        if not env_path.is_file():
            return None

        values = {
            str(key).strip().upper(): str(value)
            for key, value in dotenv_values(env_path).items()
            if value is not None
        }
        if not values:
            return None

        options = AppOptions(
            saplogon_path=_as_text(values.get("SAPLOGON_PATH")) or DEFAULT_SAPLOGON_PATH,
            startup_timeout=_as_positive_int(
                values.get("SAP_STARTUP_TIMEOUT"), DEFAULT_STARTUP_TIMEOUT
            ),
            connect_timeout=_as_positive_int(
                values.get("SAP_CONNECT_TIMEOUT"), DEFAULT_CONNECT_TIMEOUT
            ),
            popup_timeout=_as_positive_int(values.get("SAP_POPUP_TIMEOUT"), DEFAULT_POPUP_TIMEOUT),
            log_file=_as_text(values.get("SAP_LOG_FILE")),
            client_rules=_parse_rule_text(_as_text(values.get("SAP_CLIENT_MAP"))),
        )

        default_client = _as_text(values.get("SAP_DEFAULT_CLIENT")).strip()
        fallback_name = _as_text(values.get("SAP_CLIENT_FALLBACK")).strip()
        rule_targets = {name.lower() for _prefix, name in options.client_rules}

        entries: list[ConnectionEntry] = []
        for profile_id, name in _iter_legacy_profiles(values):
            # 旧 .env 只有"client 前缀 -> 连接名"的规则，没有完整 client。
            # 只有兜底连接能确定地用 SAP_DEFAULT_CLIENT，其余留空由用户在界面上补，
            # 这里不去猜一个假的 client 出来。
            client = ""
            if name.lower() == fallback_name.lower() or name.lower() not in rule_targets:
                client = default_client

            entries.append(
                ConnectionEntry(
                    connection=name,
                    client=client,
                    user=_as_text(values.get(f"SAP_CONN_{profile_id}_USER")).strip(),
                    password=_as_text(values.get(f"SAP_CONN_{profile_id}_PASSWORD")),
                    label=name,
                )
            )

        if not entries:
            # .env 存在但没有连接配置，别迁移出个空壳，交给用户从零开始配
            return None

        self._archive_legacy_env(env_path)
        return AppConfig(options=options, entries=entries)

    @staticmethod
    def _archive_legacy_env(env_path: Path) -> None:
        """把已迁移的 .env 改名留档，**并抹掉里面的明文密码**。

        留档是为了让用户还能对照原来配了哪些连接；但明文密码不能继续留在磁盘上，
        否则"密码已加密保存"就只是自欺欺人。所以这里是"清洗后另存 + 删除原文件"，
        而不是简单改名。
        """
        target = env_path.with_name(env_path.name + ".migrated")
        try:
            text = env_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return

        # 把密码值清空，只留下键名
        cleaned = LEGACY_PASSWORD_LINE_PATTERN.sub(lambda match: match.group(1), text)
        cleaned += (
            "\n"
            "# ------------------------------------------------------------\n"
            "# 这是旧版 .env 的留档，程序已不再读取它。\n"
            "# 密码已迁移到 config.json 并用 Windows DPAPI 加密，故此处已清空。\n"
            "# 需要修改配置请直接打开程序界面（双击 OpenSAPGUI.exe）。\n"
        )

        try:
            if target.exists():
                target.unlink()
            target.write_text(cleaned, encoding="utf-8")
        except OSError:
            return  # 留档写不成功就别删原文件，宁可让用户自己处理

        try:
            env_path.unlink()
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
CLIENT_SEPARATOR_PATTERN = re.compile(r"[,;，；、\s]+")


def parse_client_list(text: str) -> list[str]:
    """把 "100, 110 120" 这类手填文本拆成 client 列表（去重且保持顺序）。"""
    items: list[str] = []
    for part in CLIENT_SEPARATOR_PATTERN.split(_as_text(text)):
        if part and part not in items:
            items.append(part)
    return items


def normalize_after_login(value: Any) -> str:
    """把配置里读到的值收敛成合法选项；不认识的一律退回默认值。"""
    text = _as_text(value).strip().lower()
    return text if text in AFTER_LOGIN_CHOICES else DEFAULT_AFTER_LOGIN


# --------------------------------------------------------------------------- #
# 环境判定（开发 / 测试 / 生产）
# --------------------------------------------------------------------------- #
ENV_RULE_SEPARATOR = re.compile(r"[\n;；]+")


def parse_env_rules(text: str) -> list[list[str]]:
    """把设置框里的文本拆成规则表，每行（或分号分隔）一条「关键词=环境」。

    关键词三种写法：
      *D          连接名以 D 结尾（大小写不敏感）
      BH-3P       连接名包含该词
      client:800  client 号等于 800
    """
    rules: list[list[str]] = []
    for chunk in ENV_RULE_SEPARATOR.split(_as_text(text)):
        chunk = chunk.strip().replace("＝", "=")
        if not chunk or "=" not in chunk:
            continue
        keyword, env = (part.strip() for part in chunk.split("=", 1))
        if keyword and env and [keyword, env] not in rules:
            rules.append([keyword, env])
    return rules


def env_rules_to_text(rules: list[list[str]]) -> str:
    """把规则表还原成设置框里的文本（每行一条）。"""
    return "\n".join(f"{keyword}={env}" for keyword, env in rules if keyword and env)


def match_environment(rules: list[list[str]], connection: str, client: str = "") -> str:
    """按规则表（配置顺序，先命中先用）判定连接的环境；都不命中返回空串。"""
    conn = _as_text(connection).lower()
    cli = _as_text(client)
    for keyword, env in rules:
        key = _as_text(keyword)
        name = _as_text(env)
        if not key or not name:
            continue
        lowered = key.lower()
        if lowered.startswith("client:"):
            if cli and cli == key.split(":", 1)[1].strip():
                return name
        elif key.startswith("*"):
            suffix = key[1:].strip().lower()
            if suffix and conn.endswith(suffix):
                return name
        elif lowered in conn or (cli and cli.lower() == lowered):
            # 普通关键词：连接名包含即命中；和 client 号全等也算命中
            return name
    return ""

def _as_text(value: Any) -> str:
    """把 JSON 里可能出现的 int/None 统一成字符串。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value).strip()


def _as_positive_int(value: Any, default: int) -> int:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def _parse_rule_text(raw: str) -> list[list[str]]:
    """解析 '8:BH-3P,6:BH-2Q'，容错全角冒号/逗号。"""
    normalized = raw.replace("：", ":").replace("，", ",")
    rules: list[list[str]] = []
    for chunk in normalized.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        prefix, sep, name = chunk.partition(":")
        if sep and prefix.strip() and name.strip():
            rules.append([prefix.strip(), name.strip()])
    return rules


def _as_rules(raw: Any) -> list[list[str]]:
    if isinstance(raw, str):
        return _parse_rule_text(raw)
    if not isinstance(raw, list):
        return []
    rules: list[list[str]] = []
    for item in raw:
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            prefix, name = _as_text(item[0]), _as_text(item[1])
            if prefix and name:
                rules.append([prefix, name])
    return rules


def _iter_legacy_profiles(values: dict[str, str]) -> Iterable[tuple[str, str]]:
    """按 .env 里 SAP_CONN_<ID>_NAME 的出现顺序产出 (ID, 连接名)。"""
    for key, value in values.items():
        match = LEGACY_PROFILE_PATTERN.match(key)
        if match and value.strip():
            yield match.group(1).upper(), value.strip()
