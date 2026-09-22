"""从 SAP Logon 的配置文件里读出连接列表，供界面的下拉框使用。

数据来源（按优先级，找到几个读几个，结果合并去重）：

* 全局设置里手工指定的景观文件（企业常把统一的那份放共享盘，指过来即可）；
* 注册表 ``Software\\SAP\\SAPLogon\\Landscape`` 里指定的配置文件
  （企业环境常把景观文件放到共享盘上统一管理）；
* ``%APPDATA%\\SAP\\Common\\SAPUILandscapeGlobal.xml``（全局，SAP GUI 7.60+）；
* ``%APPDATA%\\SAP\\Common\\SAPUILandscape.xml``（用户级，SAP GUI 7.60+，
  本机实测条目都在这个文件里）；
* ``%APPDATA%\\SAP\\Common\\saplogon.ini``（SAP GUI 7.5x 及更老的版本）。

SAP Logon 里有两类条目：

* ``type="SAPGUI"``：真实连接（只有连接名，client 登录时才填）；
* ``type="Reference"``：快捷方式，带 description（指向的连接名）和 client。
  client 候选只能从这里拿到——所以目标机器上没建快捷方式时，
  界面会退回全局设置里配置的「默认 Client 候选」。

本模块只读不写，任何文件缺失或损坏都安静地跳过，绝不影响界面打开。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional
import xml.etree.ElementTree as ET

from config_store import application_dir

try:
    import winreg
except ImportError:  # pragma: no cover - 非 Windows 环境
    winreg = None  # type: ignore[assignment]


@dataclass(frozen=True)
class SapService:
    """SAP Logon 里的一个条目。"""

    name: str
    client: str = ""
    kind: str = "system"      # system=真实连接 / shortcut=快捷方式
    description: str = ""     # 快捷方式指向的连接名（SAP 自己填的）
    cmd: str = ""             # 快捷方式里配的启动事务码（如 SE80、SIMG）


def resolve_landscape_path(value: str) -> Optional[Path]:
    """把设置里填的景观文件路径解析成绝对路径；空值返回 None。

    相对路径按"程序所在目录"算，和 SAP Logon 路径、日志文件的规则保持一致。
    """
    text = str(value or "").strip()
    if not text:
        return None
    path = Path(os.path.expandvars(text)).expanduser()
    if not path.is_absolute():
        path = application_dir() / path
    return path


def landscape_files(extra: Iterable[str] = ()) -> list[Path]:
    """实际存在的景观配置文件，按优先级排序；一个都没有就返回空列表。

    `extra` 是全局设置里手工指定的路径（可多个），排在最前面。
    """
    candidates: list[Path] = []

    for item in extra:
        path = resolve_landscape_path(item)
        if path is not None:
            candidates.append(path)

    if winreg is not None:
        for sub_key in (
            r"Software\SAP\SAPLogon\Landscape",
            r"SOFTWARE\SAP\SAPLogon\Landscape",
            r"SOFTWARE\WOW6432Node\SAP\SAPLogon\Landscape",
        ):
            for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                try:
                    with winreg.OpenKey(hive, sub_key) as key:
                        for value_name in ("ConfigFile", "ConfigFilePath"):
                            try:
                                raw, _ = winreg.QueryValueEx(key, value_name)
                            except OSError:
                                continue
                            text = str(raw).strip()
                            if text:
                                candidates.append(Path(os.path.expandvars(text)))
                except OSError:
                    continue

    appdata = os.environ.get("APPDATA", "").strip()
    if appdata:
        common = Path(appdata) / "SAP" / "Common"
        candidates.append(common / "SAPUILandscapeGlobal.xml")
        candidates.append(common / "SAPUILandscape.xml")
        candidates.append(common / "saplogon.ini")

    files: list[Path] = []
    seen: set[str] = set()
    for path in candidates:
        key = str(path).lower()
        if key not in seen and path.is_file():
            seen.add(key)
            files.append(path)
    return files


def parse_landscape_xml(path: Path) -> list[SapService]:
    """解析 SAPUILandscape*.xml；文件缺失、损坏都返回空列表。"""
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError, ValueError):
        return []

    services: list[SapService] = []
    for node in root.iter():
        if node.tag.split("}")[-1] != "Service":
            continue
        name = (node.get("name") or "").strip()
        if not name:
            continue
        kind = (node.get("type") or "").strip()
        if kind == "SAPGUI":
            services.append(SapService(name=name, kind="system"))
        elif kind == "Reference":
            services.append(
                SapService(
                    name=name,
                    client=(node.get("client") or "").strip(),
                    kind="shortcut",
                    description=(node.get("description") or "").strip(),
                    cmd=(node.get("cmd") or "").strip(),
                )
            )
    return services


def _ini_section(text: str, section: str) -> dict[int, str]:
    """取出 saplogon.ini 里某个小节的 ItemN=... 内容。"""
    match = re.search(rf"^\[{re.escape(section)}\]\s*$(.*?)^\[", text, re.M | re.S)
    body = match.group(1) if match else text
    items: dict[int, str] = {}
    for line in body.splitlines():
        m = re.match(r"^\s*Item(\d+)\s*=(.*)$", line, re.IGNORECASE)
        if m:
            items[int(m.group(1))] = m.group(2).strip()
    return items


def parse_saplogon_ini(path: Path) -> list[SapService]:
    """解析老版 saplogon.ini（每个小节的 ItemN 按编号一一对应）。尽力兼容。"""
    text = ""
    for encoding in ("utf-8", "utf-16", "mbcs"):
        try:
            text = path.read_text(encoding=encoding)
            break
        except (OSError, UnicodeError, LookupError):
            continue
    if not text:
        return []

    descriptions = _ini_section(text, "Description")
    if not descriptions:
        return []
    systems = _ini_section(text, "System")
    clients = _ini_section(text, "Client")

    services: list[SapService] = []
    for index in sorted(descriptions):
        name = descriptions[index]
        if not name:
            continue
        client = clients.get(index, "")
        # System 列形如 "DEV-1 : : /H/host/S/3200"，只取路由前第一个字段做核对
        system_name = systems.get(index, "").split(":")[0].strip()
        services.append(
            SapService(
                name=system_name or name,
                client=client,
                kind="shortcut" if system_name else "system",
            )
        )
    return services


def load_services(extra_files: Iterable[str] = ()) -> list[SapService]:
    """读出 SAP Logon 里的全部条目（合并所有来源、按连接名+client 去重）。

    `extra_files` 为全局设置里手工指定的景观文件路径。
    """
    result: list[SapService] = []
    seen: set[tuple[str, str]] = set()
    for path in landscape_files(extra_files):
        parser = parse_saplogon_ini if path.suffix.lower() == ".ini" else parse_landscape_xml
        for service in parser(path):
            key = (service.name.lower(), service.client)
            if key not in seen:
                seen.add(key)
                result.append(service)
    return result


def connection_names(services: list[SapService]) -> list[str]:
    """真实连接名清单（排序去重）。没有真实连接时用快捷方式的指向兜底。"""
    names = sorted({s.name for s in services if s.kind == "system"})
    if not names:
        names = sorted({s.description for s in services if s.description})
    return names


def client_candidates(services: list[SapService], connection: str,
                      fallback: Iterable[str] = ()) -> list[str]:
    """某个连接在 SAP Logon 里配置过的 client（排序去重）。

    真实连接条目本身不带 client（登录时才填），候选来自指向该连接的快捷方式。
    该机器上没建快捷方式时（读不到任何候选），用 `fallback` 顶上——
    也就是全局设置里的「默认 Client 候选」。
    """
    target = connection.strip().lower()
    if not target:
        return []
    clients = {
        s.client
        for s in services
        if s.client
        and (
            (s.kind == "shortcut" and s.description.strip().lower() == target)
            or (s.kind == "system" and s.name.lower() == target)
        )
    }
    candidates = sorted(clients)
    if candidates:
        return candidates
    return [item for item in fallback if item]


def tcode_candidates(services: list[SapService], connection: str,
                     client: str = "") -> list[str]:
    """某个连接（可选再限定 client）在 SAP Logon 里配过的启动事务码。

    快捷方式的 cmd 字段存着"登录后直接进哪个事务"。client 给了就优先取
    对应 client 的快捷方式；没给（或该连接没按 client 细分）就取该连接下全部。
    """
    target = connection.strip().lower()
    if not target:
        return []
    wanted_client = client.strip()
    matched = [
        s.cmd for s in services
        if s.cmd and s.kind == "shortcut"
        and s.description.strip().lower() == target
        and (not wanted_client or not s.client or s.client == wanted_client)
    ]
    seen: set[str] = set()
    ordered: list[str] = []
    for cmd in matched:
        if cmd not in seen:
            seen.add(cmd)
            ordered.append(cmd)
    return ordered
