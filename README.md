# OpenSAPGUI · SAP GUI 自动登录

一键登录 SAP，可选直接进入指定事务码。图形界面里单击卡片即可登录，也支持命令行调用（给快捷方式 / 计划任务用）。
连接配置全部保存在本地，**密码用 Windows DPAPI 加密，仓库里没有任何明文凭据**。

- 图形界面：连接卡片列表，单击即登录；按名称排序、可置顶、带开发/测试/生产环境徽章
- 连接名 / client / 事务码下拉：自动读取本机 SAP Logon 的景观文件，不用手敲
- 稳健的登录流程：挑对会话、等界面空闲、处理重复登录弹窗，**登录结果和事务码都会校验**，失败不谎报成功
- 登录完成后主窗口可自动最小化、或让到 SAP GUI 窗口后面
- 单实例限制：已经在运行时再启动，只会把已有窗口拉到前台，不会开出第二个界面

---

## 目录

- [环境要求](#环境要求)
- [快速开始](#快速开始)
- [图形界面用法](#图形界面用法)
- [命令行用法](#命令行用法)
- [配置文件](#配置文件)
- [打包与分发](#打包与分发)
- [项目结构](#项目结构)
- [测试](#测试)
- [常见问题](#常见问题)
- [安全说明](#安全说明)

---

## 环境要求

| 项目 | 说明 |
|---|---|
| 操作系统 | Windows 10 / 11 |
| SAP | 已安装 **SAP GUI for Windows**（含 SAP Logon） |
| SAP GUI Scripting | 客户端脚本开关已打开，且账号在服务端有脚本权限（`SAP GUI Scripting` 权限对象）。没有这个权限，任何自动化工具都跑不动 |
| Python（仅源码运行 / 打包时需要） | 3.10 或更高 |

依赖（`requirements.txt`）：

```
PySide6-Essentials>=6.6   # 图形界面。只装 Essentials 就够 QtCore/QtGui/QtWidgets
pywin32>=306              # SAP COM 自动化 + DPAPI 密码加密
python-dotenv>=1.0.0      # 仅用于把旧版 .env 一次性迁移到 config.json
pyinstaller>=6.0          # 打包 exe 时使用
```

---

## 快速开始

### 方式一：直接用打包好的程序（推荐）

1. 解压整个 `OpenSAPGUI` 文件夹（**不要只把 exe 单独拷出来**，它需要同级的 `_internal\` 目录）；
2. 双击 `OpenSAPGUI.exe`；
3. 界面右上角「＋ 新建连接」，填连接名、client、用户名、密码（连接名和 client 有下拉可选）；
4. 之后**单击卡片**就能直接登录。

> 建议放在 `D:\` 或用户目录下，**不要放 `C:\Program Files`** —— 那里没有写权限，程序生成不了 `config.json`。

### 方式二：从源码运行

```bat
git clone git@github.com:andymeng429/Sap_autoLogin.git
cd Sap_autoLogin
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt

:: 打开图形界面
.venv\Scripts\python.exe OpenSAPGUI.py

:: 或者走命令行
.venv\Scripts\python.exe OpenSAPGUI.py 120 SE09
```

---

## 图形界面用法

**主界面**：标题栏下方是连接卡片列表，每张卡片固定两行（主行 = 名称 + 环境徽章，副行 = 连接名 / client / 用户 / 事务码）。卡片布局在登录全程不会变高变矮。

| 操作 | 说明 |
|---|---|
| 单击卡片 | 按该条目登录；登录进度显示在窗口底部状态栏 |
| 卡片右侧「置顶」 | 固定到列表最前（置顶组内仍按名称排序）|
| 卡片右侧「编辑」/「删除」 | 修改或移除该条目 |
| 右上「＋ 新建连接」 | 新增一条 |
| 右上「全局设置」 | SAP Logon 路径、超时、日志文件、景观文件、环境规则、登录后行为 |

**编辑连接**里的字段：

| 字段 | 说明 |
|---|---|
| 显示名称（可选）| 卡片标题。留空则显示「连接名 / client」 |
| 连接名 | 下拉取自本机 SAP Logon 景观文件，也可手输 |
| client | 下拉取自所选连接在景观文件里的 client；也支持全局设置里的兜底候选 |
| 事务码（可选）| 登录后自动进入的 T-code，如 `SE09`。下拉取自景观文件，也可手输 |
| 用户名 / 密码 | 密码存盘时自动加密 |

**环境徽章**：SAP 本身不提供「这套系统是开发还是生产」的信息，所以按你的命名约定判——在「全局设置 → 环境判定规则」里配，每行一条「关键词=环境」，先命中先用：

```
*D=开发       连接名以 D 结尾
*Q=测试       连接名以 Q 结尾
BH-3P=生产    连接名包含 BH-3P
client:800=生产   client 号等于 800
```

徽章颜色按环境名自动配色：含「生产 / prod / prd」为红，含「测试 / 质量 / uat / qa / test」为琥珀，含「开发 / dev」为蓝，其余灰色。

**登录完成后**（全局设置里选）：
- **自动最小化**（默认）：登录成功即缩到任务栏；
- **排到 SAP GUI 后面**：把主窗口插到 SAP 会话窗口之后，只调层级不抢焦点；找不到 SAP 窗口就什么都不做；
- **保持原样**。

**单实例**：程序已在运行时再次双击，不会开出第二个界面，而是把已有窗口还原并拉到前台（抢不到前台时闪一下任务栏）。如果没有正在登录的任务，关窗口前不会多问；有登录在跑时会先确认再退出。

---

## 命令行用法

不带任何参数 = 打开图形界面（旧快捷方式无需修改）。

```bat
OpenSAPGUI.exe                      :: 图形界面
OpenSAPGUI.exe --gui                :: 同上，显式指定
OpenSAPGUI.exe 120 BP               :: 登录 client=120 的条目，并进入 BP
OpenSAPGUI.exe 800 MM03             :: client 以 8 开头 -> 按映射规则走 BH-3P
OpenSAPGUI.exe --connection BH-3P   :: 直接点名连接
OpenSAPGUI.exe --list               :: 列出已配置的连接后退出
```

| 参数 | 说明 |
|---|---|
| `target` | client 编号或连接名；省略时用第一条启用的连接 |
| `tcode` | 登录后执行的事务码，省略则只登录 |
| `--gui` | 打开图形界面 |
| `--connection NAME` | 直接指定连接名 |
| `--config PATH` | 指定 `config.json` 路径（也可用环境变量 `SAP_CONFIG_FILE`）|
| `--user` / `--password` | 临时覆盖配置（命令行参数同机其它进程可见，仅建议排障时用）|
| `--timeout SECONDS` | 覆盖启动/连接超时，想快速失败就设小一点 |
| `--log-file PATH` | 同时把日志写入指定文件 |
| `--no-verify` | 跳过登录结果与事务码的结果校验（排障用，不推荐）|
| `--keep-open` | 结束后等待回车再关窗口（仅控制台版有意义）|
| `-v, --verbose` | 输出调试日志 |

**兼容旧快捷方式**：以前的脚本习惯用「client 前缀 → 连接」的映射（如 `800` 走 `BH-3P`）。这套规则保留在全局设置的 `client_rules` 里，所以老的 `OpenSAPGUI.exe 800` 快捷方式不用改也照常工作：先按 client 找已配置的条目，找不到再用前缀规则兜底。

---

## 配置文件

程序同级目录下的 `config.json`（首次运行自动生成）。**它含连接名、用户名和加密后的密码，不要提交到版本库、不要连程序一起发给别人。**

```jsonc
{
  "version": 1,
  "options": {
    "saplogon_path": "C:\\Program Files\\SAP\\FrontEnd\\SAPgui\\saplogon.exe",
    "startup_timeout": 30,          // 等 SAP Logon 启动
    "connect_timeout": 30,          // 等连接建立
    "popup_timeout": 8,             // 等登录弹窗
    "log_file": "OpenSAPGUI.log",   // 相对路径 = 程序目录；留空则不写文件
    "client_rules": [["8", "BH-3P"], ["6", "BH-2Q"]],  // 旧快捷方式兼容规则
    "landscape_file": "",           // SAP 景观文件路径，留空自动探测
    "default_clients": "100,110,120,610,800",  // client 下拉兜底候选
    "env_rules": [["*D", "开发"], ["*Q", "测试"], ["*P", "生产"]],
    "after_login": "minimize"       // minimize / behind / none
  },
  "entries": [
    {
      "id": "e1a2b3c4",
      "label": "",                  // 留空则显示「connection / client」
      "connection": "BH-1D",
      "client": "120",
      "user": "YOUR_USER",
      "password": "dpapi:AQAAAN...",  // DPAPI 密文，绑定当前 Windows 账号
      "tcode": "SE09",
      "enabled": true,
      "pinned": false
    }
  ]
}
```

**从旧的 `.env` 迁移**：首次运行如果发现旧的 `.env`，会自动生成 `config.json` 并把 `.env` 改名为 `.env.migrated` 留档。旧 `.env` 只有 client 前缀规则、没有完整 client，所以迁移时**除兜底连接外 client 一律留空**，界面上会提示你补全（这是故意不猜）。

**密码是怎么存的**：用 Windows DPAPI（`CryptProtectData`，附加熵 `OpenSAPGUI/v1`）加密，密钥绑定当前 Windows 用户。换电脑或重装系统后解不开——此时程序不会报错崩溃，而是把该条目的**其它字段照常加载、只留空密码**，界面提示你重填。这是设计取舍：密码不跨机可解，也就没有可搬运的密钥。

---

## 打包与分发

```bat
.venv\Scripts\python.exe -m PyInstaller OpenSAPGUI.spec --noconfirm --clean
```

产物是 `dist\OpenSAPGUI\`（`OpenSAPGUI.exe` + `_internal\`），用**文件夹模式（onedir）**打包：

- 单文件模式每次运行都要把运行库解压到 `%TEMP%\_MEIxxxx`，退出时若被占用（典型诱因：杀软往进程注入的 DLL 占住捆绑的 `VCRUNTIME140.dll`）就会弹
  *"Failed to remove temporary directory"* 警告框——这个框由 PyInstaller 引导器弹出，Python 层拦不住。onedir 不解压，弹窗从根上消失；
- spec 里剔除了用不到的 Qt 运行时（QML / Quick / PDF / 虚拟键盘 / pythonwin）以缩减体积。注意 `opengl32sw.dll` **是故意保留的**：它是软件 OpenGL 兜底，虚拟机 / 远程桌面 / 老显卡上靠它才能把界面画出来；
- 分发时把整个文件夹打包发出去，并**剔除 `config.json`**（里面是你的连接和加密密码）；
- `COLLECT` 每次会重建整个 `dist\OpenSAPGUI\`，所以打完包要重新把 `config.json` 拷进去。

`tests/test_exit_cleanup.py` 里有一条 `test_spec_stays_folder_mode`，断言 spec 必须保持 onedir，防止以后被改回单文件把这个弹窗带回来。

---

## 项目结构

| 文件 | 职责 |
|---|---|
| `OpenSAPGUI.py` | 入口：参数分发（GUI / CLI）、单实例互斥体、控制台隐藏、未捕获异常兜底 |
| `sap_core.py` | SAP 会话封装与登录流程，**不依赖任何 GUI 库**，界面和命令行共用 |
| `config_store.py` | `config.json` 读写、DPAPI 加解密、旧 `.env` 迁移、环境判定规则解析 |
| `gui_app.py` | PySide6 界面：卡片列表、编辑 / 全局设置对话框、后台登录线程 |
| `sap_landscape.py` | 只读解析 SAP Logon 景观文件（`SAPUILandscape.xml` / `saplogon.ini`），给下拉框供数据 |
| `win_focus.py` | Windows 窗口层级：把主窗口让到 SAP GUI 后面、把已有窗口拉到前台。缺 pywin32 时静默退化 |
| `SAPScriptingTracker.py` | 独立小工具：探测当前 SAP GUI 会话的 Scripting 是否可用、会话是否 Busy |
| `OpenSAPGUI.spec` | PyInstaller 打包配置（onedir）|
| `run_tests.bat` | 一键跑全部测试 |

### 登录流程做了什么

不是"发完按键就算成功"：

1. **挑会话**——从 SAP Logon 打开指定连接，只认真正处于登录页的那个会话，避免误用已登录的旧会话；
2. **等空闲**——用 `Busy` 标志轮询代替无脑 sleep；
3. **处理弹窗**——识别「重复登录 / 已在别处登录」冲突弹窗并自动选择"继续此登录"；**报错弹窗不会被盲点**，只会如实报错。为了不白等，观察窗口内没弹窗就立刻返回（旧版每次正常登录都要白等满 8 秒）；
4. **校验登录**——查强制改密提示、报错信息、是否仍停在登录页。密码错了 / 用户被锁 / client 不许登录都会明确报错，而不是假装成功；
5. **校验事务码**——执行 T-code 后读状态栏，`E`/`A` 类型即报错（T-code 不存在或没权限），`W` 记录警告。

---

## 测试

```bat
run_tests.bat
```

8 个测试文件、共 **151 项**，**全部是 mock**：不连真实 SAP、不动真实窗口、不需要 `config.json`。

| 测试 | 覆盖 |
|---|---|
| `test_config_store.py` | 配置读写、DPAPI 往返、client 列表解析、环境规则匹配、排序、旧 `.env` 迁移 |
| `test_sap_landscape.py` | 景观文件解析（XML / INI）、连接名与 client / tcode 候选 |
| `test_entry_dialog.py` | 编辑对话框：字段校验、下拉联动、往返 |
| `test_login_verify.py` | 登录结果校验的三种分支（成功 / 报错弹窗 / 控件缺失）|
| `test_login_conflict.py` | 重复登录冲突弹窗处理 |
| `test_win_focus.py` | 窗口层级（假 win32 模块注入）、拉回前台 |
| `test_card_state.py` | 卡片高度在登录全程不变、登录后行为分支、设置对话框 |
| `test_exit_cleanup.py` | 临时目录清扫、单实例锁、spec 保持文件夹模式 |

---

## 常见问题

**双击没反应 / 一闪而过？**
程序没有控制台，异常只会进日志。看 exe 同级的 `OpenSAPGUI.log`（若全局设置里把日志设成了绝对路径，就去那个路径找）。

**提示「已经在运行了」？**
单实例限制。已有实例的窗口会被自动拉到前台；如果它在最小化状态会先还原。

**换台电脑后提示密码要重填？**
DPAPI 密文绑定原电脑的 Windows 账号，换机解不开——这是设计而非缺陷。填一次即可，之后照常。

**连接名 / client 下拉是空的？**
下拉数据来自本机 SAP Logon 的景观文件。程序会自动探测 `%APPDATA%\SAP\Common` 和注册表里的配置位置；如果你们的景观文件在共享盘，去「全局设置」里手动指定。另外在「全局设置」里填「默认 client 候选」（如 `100,110,120,610,800`）可以保证任何机器上都有下拉可选。

**登录报错说脚本被禁用？**
需要在 SAP GUI 客户端打开脚本开关，并由管理员给账号分配 `SAP GUI Scripting` 权限。这是服务端管控，工具本身绕不过去。

**退出时还弹 "Failed to remove temporary directory"？**
那是旧单文件版本的问题。现在用的是文件夹模式，不会再产生临时解压目录；如果还看到，说明你手上是旧包，换 `dist\OpenSAPGUI\` 这份。

**能放在 `C:\Program Files` 下吗？**
不建议——那里普通用户没有写权限，`config.json` 会写不进去。

---

## 安全说明

- 仓库里**不包含**任何真实凭据：`.env`、`.env.migrated`、`config.json`、`*.log` 均已在 `.gitignore` 中排除；
- 密码仅以 DPAPI 密文形式落在本机 `config.json`，内存中的明文不写日志（`LoginTarget.__repr__` 会把密码显示为 `***`）；
- `.env.example` 只是配置模板，里面的连接名 / 账号均为占位符；
- 用 `--password` 传密码会暴露在命令行里（同机其它进程可见），仅在临时排障时使用。

---

## 免责声明

本工具通过 SAP GUI Scripting 接口驱动 SAP GUI for Windows 完成登录，**仅供在你已获授权的 SAP 系统与账号上使用**。
请遵守所在组织的 IT 与安全规范；因使用本工具产生的任何后果由使用者自行承担。

---

## License

私人项目，未指定开源许可。
