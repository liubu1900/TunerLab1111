# 矢量网络分析仪（VNA）多厂商接入指南：TCP/IP 与 GPIB

> 适用范围：TunerLab 桌面端「矢网控制 (VNA)」页、Web 控制台同名页面，以及建库向导里勾选「真实矢网」后的实机采集。
> 驱动代码：`tuner/vna_drivers.py`。厂商 SCPI 预设由两种接口共用，切换的只是传输层。
> 更新：2026-11-11（新增 GPIB / Agilent 82357B 支持；补充首次 Agilent 真机联调暴露的 SCPI 方言问题与探测脚本）。

---

## 1. 两种接口，一套驱动

| 接口 | 传输层 | 运行时依赖 | 地址形式 | 默认值 |
|---|---|---|---|---|
| **TCP/IP** | `SocketSCPI`（原生 socket SCPI） | 仅 Python 标准库 | `IP:端口` | `192.168.2.111:5025` |
| **GPIB** | `VISASCPI`（PyVISA） | **Keysight IO Libraries Suite** + `pyvisa` | VISA 资源串 | `GPIB0::17::INSTR` |

- 两种接口发出的 SCPI 命令、厂商识别（`*IDN?`）、S 参数取数（ASCII real/imag）完全一致，只有「怎么把字节送到仪器」不同。
- GPIB 消息靠 **EOI 线**结束，不是换行符，因此 `read_termination` / `write_termination` 都留空，由 VISA 在最后一个字节置 EOI。
- `pyvisa` 已在 `TunerLab/requirements-tested.txt`（1.16.2）中，但只有选择 GPIB 时才会被导入；TCP/IP 路径仍然零额外依赖。
- 采集结果 `Measurement.metadata` 会记录 `resource`（`IP:端口` 或 `GPIB0::17::INSTR`）与 `interface`（`tcpip` / `gpib`），便于追溯数据来源。

支持的厂商（两种接口共用同一张表）：

| 下拉项 | `vendor_key` | 代表机型 | 命令差异点 |
|---|---|---|---|
| 罗德 R&S | `rsp` | ZNB / ZVA / ZND | 测量定义 `:CALC1:PAR:SDEF`，取数 `SDAT` |
| Keysight ENA | `keysight_ena` | E5071C / E5080 / E5063 | `:CALC1:PAR:DEF`，取数 `SDATA` |
| Keysight PNA-X | `keysight_pna` | N5247A / N522x / N523x | 测量定义 `:CALC1:PAR:DEF:EXT`（**不是** `DEF`）、功率 `:SOUR1:POW:LEV:IMM:AMPL` |
| 思仪 Ceyear | `ceyear` | AV3672 系列 | ENA 风格 SCPI |
| 安立 Anritsu | `anritsu` | VectorStar MS46xxx / ShockLine | ENA 风格 SCPI |
| 自动识别 | `auto` | — | 连接后读 `*IDN?` 自动判断（**推荐**） |

---

## 2. GPIB 接入（Agilent / Keysight 82357B USB-GPIB）

### 2.1 需要装的东西

1. **Keysight IO Libraries Suite**（Agilent 82357B 的 Windows 驱动、VISA 运行时 `visa32.dll`、Connection Expert）。82357B 是专有 USB-GPIB 适配器，**不能用 NI-VISA 代替**，也不被 `pyvisa-py` 直接支持。
2. Python 侧 `pyvisa`（已在 `requirements-tested.txt`，虚拟环境里一般已装好）。
3. 安装完成后**重启一次或重新打开应用**，让 VISA 服务与系统 PATH 生效。

> 本机实测（2026-11-11）：装完 IO Libraries 后 `C:\Windows\System32\visa32.dll` 已存在，但从安装前就打开的进程里 PyVISA 仍报 `VI_ERROR_LIBRARY_NFOUND`。原因是 Python 3.8+ 加载依赖 DLL 不再依赖 PATH，且旧进程 PATH 未刷新。**驱动代码已自动把 Keysight / IVI 的 `bin` 目录加入 DLL 搜索路径**（`os.add_dll_directory`），所以从旧终端、IDE、常驻桌面进程启动也能加载。

### 2.2 硬件连接与 GPIB 地址

1. 82357B 插上位机 USB，GPIB 线缆接仪器，仪器上电；在 **Keysight Connection Expert** 里确认能看到该 GPIB 接口和仪器。
2. 在仪器前面板查 **GPIB Address / IEEE-488 Address**（常见 **16** 或 **17**）；多块 GPIB 卡时板号从 **0** 开始。
3. 资源串格式：`GPIB<板号>::<主地址>[::<副地址>]::INSTR`，例如 `GPIB0::17::INSTR`、`GPIB1::16::0::INSTR`。主/副地址范围 **0–30**（31 是广播地址，不可用于仪器）。
4. 界面里**可以只填地址数字**（如 `17`），软件自动补成 `GPIB0::17::INSTR`；也可以填完整 VISA 资源串。填 IP 地址会被明确拒绝。

### 2.3 「检测 VISA」按钮 / 命令行自检

桌面「矢网控制」页与 Web 控制台都有 **检测 VISA** 按钮，等价于：

```powershell
cd D:\Tuner
.\.venv\Scripts\python -c "from tuner.vna_drivers import visa_resources; print(visa_resources())"
```

返回示例：

```text
{'backend': 'C:\\WINDOWS\\system32\\visa32.dll',
 'resources': ['GPIB0::17::INSTR', 'ASRL3::INSTR']}
```

按钮会把第一个 GPIB 资源自动填进资源框。若 `resources` 为空，说明 VISA 运行时已加载但**总线上还没有可见设备**——检查 82357B 是否插好、驱动是否安装、仪器是否上电、GPIB 线是否接好，必要时重启一次。

### 2.4 常见加载故障

| 报错 | 含义 | 处理 |
|---|---|---|
| `Could not locate a VISA implementation` | 找不到 `visa32.dll` | 安装 Keysight IO Libraries Suite，重启 |
| `VI_ERROR_LIBRARY_NFOUND (-1073807202)` | 主库找到了，依赖 DLL 没加载 | 代码已自动补 DLL 搜索路径；仍失败则确认 64 位 Python 配 64 位 VISA、重启、修复安装 |
| `VI_ERROR_RSRC_NFOUND` / 打开资源失败 | 资源名不存在 | 用「检测 VISA」读真实资源名，核对板号与主地址 |
| 连接超时、无 `*IDN?` 响应 | 地址错 / 仪器未上电 / 未处于 GPIB 远控模式 | 前面板确认 GPIB 地址与 Remote 状态；适当加大超时 |

---

## 3. TCP/IP 接入（原有方式）

### 3.1 三项网络准备

1. **同网段互通**：上位机与矢网能互相 `ping` 通。
2. **开启 SCPI / LAN 服务器**：仪器允许 LAN 远程控制（见 3.2）。
3. **防火墙放行 TCP 5025**：`telnet 192.168.2.111 5025` 或 `Test-NetConnection -Port 5025 192.168.2.111` 先验端口。

### 3.2 各品牌开启 SCPI（LAN 远程控制）步骤

> 固件版本不同菜单路径会有差异；连上后若识别厂商为空或命令报错，请把 `*IDN?` 返回字符串发出来校准命令集。

#### 罗德 R&S（ZNB / ZVA / ZND）
- **System → Remote Control**（或 I/O → Remote），确认 TCP/IP SCPI 服务器已启用，端口 5025。
- 取数助记符 `SDAT`；测量定义 `:CALC1:PAR:SDEF 'name','S11'`（带引号），软件已做 SDEF/DEF 互回退。

#### Keysight E5071C（ENA 系列）
- **System → Misc Setup → Network Setting** 配置 IP 并启用 LAN；SCPI Socket 服务器默认端口 5025。
- 可用 `:SYST:COMM:LAN:PORT?` 查询端口；取数助记符 `SDATA`，测量定义 `:CALC1:PAR:DEF`。

#### Keysight N5247A（PNA / PNA-X）
- **System → System Setup → Network Setup** 设置 IP；启用 SCPI Server（5025）。
- 测量定义必须用 `:CALC1:PAR:DEF:EXT 'S11','S11'`（或 `:CALC1:PAR:EXT`）；裸 `:CALC1:PAR:DEF` 在 PNA 命令树上不存在，会回 `-113 Undefined header`。功率命令 `:SOUR1:POW:LEV:IMM:AMPL`；软件固定定义 4 个 S 参数，需保证通道 1 可用。

#### 思仪 Ceyear（AV3672）
- **系统 → 通信设置 → LAN / 网络** 配置并启用远程；ENA 风格 SCPI，端口 5025。

#### 安立 Anritsu（VectorStar / ShockLine）
- **System → Remote / SCPI Server** 启用远程 SCPI；部分机型默认端口不是 5025（如 5030），请在面板确认后同步修改端口。

---

## 4. 软件发出的命令对照（TCP/IP、GPIB 相同）

| 用途 | 命令（示例，key=auto 时按厂商切换） |
|---|---|
| 清状态 | `*CLS` |
| 识别 | `*IDN?` |
| 数据格式 | `:FORMat:DATA ASCii` |
| 扫频类型 | `:SENS1:SWE:TYPE LIN` |
| 连续触发关 | `:INIT1:CONT OFF` |
| 定义 S11 | 依次尝试 `:CALC1:PAR:DEF` / `:CALC1:PAR:SDEF` / `:CALC1:PAR:DEF:EXT` / `:CALC1:PAR:EXT`（均带 `'S11','S11'`），命中后本会话复用该写法 |
| 选迹线 | `:CALC1:PAR:SEL 'S11'` |
| 起始频率 | `:SENS1:FREQ:STAR <Hz>` |
| 终止频率 | `:SENS1:FREQ:STOP <Hz>` |
| 频点数 | `:SENS1:SWE:POIN <n>` |
| 功率 | `:SOUR1:POW:LEV <dBm>`（PNA-X 用 `:SOUR1:POW:LEV:IMM:AMPL`） |
| 中频带宽 | `:SENS1:BAND <Hz>` |
| 触发+完成 | `:INIT1:IMM;*OPC?`（返回 `1` 确认完成） |
| 取数 | `:CALC1:DATA? SDATA`（R&S 用 `SDAT`） |
| 读错误队列 | `:SYST:ERR?` |

**容错机制**
- **测量定义（4 种方言自动协商）**：按 `DEF` → `SDEF` → `DEF:EXT` → `EXT` 顺序尝试，每条命令前后都发 `*CLS` 清空错误队列，再用 `:SYST:ERR?` 判定该命令是否被接受；命中后缓存该写法，S21/S12/S22 直接复用。R&S 认 `SDEF`，Keysight PNA / PNA-X 认 `DEF:EXT` / `EXT`，ENA 与多数国产机型认 `DEF`。逐条清队列很重要：真实仪器对每条不认识的命令都会压入一个 `-113`，残留条目会让下一条本来有效的命令被误判为失败。四条全被拒时抛 `failed to define measurement ...`（异常里列出所有试过的命令），仪器面板通常显示 `-113 Undefined header`。
- **触发**：依次尝试 3 种 `:INIT…;*OPC?` 组合；若都失败（部分 GPIB 老仪器不接受「触发 + 查询」拼在同一条消息里），再退化为「单独发触发命令 → 单独查询 `*OPC?`」。
- **错误探查**：写命令后查 `:SYST:ERR?`；仪器不支持错误队列时读取失败会被忽略，不会中断连接。

---

## 5. 在软件里怎么选

### 5.1 桌面端「矢网控制 (VNA)」页
1. **接口**下拉选 `TCP/IP` 或 `GPIB (VISA)`。
2. TCP/IP：填 IP、端口；GPIB：填 VISA 资源（或点「检测 VISA」自动填入）。两组字段会自动互斥显示。
3. 选厂商（建议自动识别）、超时、扫频起止/点数、功率、中频带宽 → **连接并配置**。
4. 看「仪器状态」卡的 IDN / 识别厂商 / 最近错误 → **单次扫描（4 路）** 读取 S11~S22。

### 5.2 Web 控制台
「矢网控制 (VNA)」页的 **接口** 下拉与检测按钮行为同上；建库段的「使用真实矢网」下同样有接口与 VISA 资源字段。

### 5.3 建库向导「真实矢网」
「参考面与仪器」步骤里把 **矢网来源** 选为真实矢网，再选 **接口**（TCP/IP / GPIB），填 IP 或 VISA 资源。后端 `studio_init_hardware` / `studio_bridge` 会按该接口建立真实仪器，其余校准流程不变。

> 同一台矢网不要被「VNA 面板」和「建库向导真实矢网」两个会话同时占用，命令会交错。单台实机建议二选一。

---

## 6. 故障排查

| 现象 | 可能原因 | 处理 |
|---|---|---|
| TCP/IP 连接超时 / 被拒 | IP 错、网段不通、防火墙拦 5025、SCPI 服务器未开 | 先 `ping` + `Test-NetConnection -Port 5025`；开 SCPI；放行防火墙 |
| GPIB 报「未找到 VISA 运行时」 | 未装 IO Libraries / 装完未重启 | 装 Keysight IO Libraries Suite 并重启 |
| GPIB 报 `VI_ERROR_LIBRARY_NFOUND` | 依赖 DLL 未加载 | 代码已自动补 DLL 路径；确认 64 位 Python + 64 位 VISA、重启、修复安装 |
| 「检测 VISA」返回空列表 | 82357B 未插好 / 驱动未装 / 仪器未上电 / GPIB 线松 | 在 Connection Expert 里确认接口与仪器可见 |
| GPIB 打开资源失败 | 资源名与仪器地址不符 | 用检测按钮读真实资源名；核对板号、主地址（0–30） |
| `*IDN?` 空或乱码 | 端口/地址不对、仪器处于别的协议 | TCP/IP 核对 SCPI 端口；GPIB 核对地址与远控模式 |
| 识别厂商=空/未知 | IDN 不含已知关键词 | 手选厂商后重连；把 IDN 发出来补关键词 |
| **「failed to define measurement S11」+ 仪器 `-113 Undefined header`** | **命令头在该机型上不存在（SCPI 方言不匹配）**，不是参数错、也不是固件版本校验失败 | 跑第 8 节的探测脚本拿 `*IDN?` 与可用命令表；按结果调整 profile。已内置 PNA 的 `DEF:EXT` / `EXT` 回退，重连试试 |
| 取数为空 / 解析报错 | 助记符不符或数据非 ASCII | 检查 `:FORMat:DATA ASCii` 支持；把报错发出来 |
| 触发后无完成确认 | 触发命令形态不同 | 已内置 3 种组合 + GPIB 拆分回退；仍失败请提供 `:SYST:ERR?` 内容 |

---

## 7. 已知限制与验证状态

- **老式 / 非 ENA 风格仪器**（如 HP 87xx 系列）使用的不是 ENA/PNA 风格 SCPI，`*IDN?` 未命中时会落到 `generic` profile。测量定义现已覆盖 4 种写法，但**逐迹线语法**（`:CALC1:PAR1:DEF S11`）、取数助记符（部分机型用 `:CALC1:TRAC1:DATA?`）、功率 / IFBW 命令仍可能不兼容，需要按具体型号新增 profile。目前 6 个 profile 覆盖的是较新的 SCPI 兼容机型。
- **仅支持 ASCII 取数**，二进制数据块（`#` 头）暂不支持。GPIB 上大点数扫频会比 LAN 慢，建议先减小点数验证链路。
- **VNA 配置只在进程内存中**，重启应用后接口/IP/GPIB 地址会回到默认值（TCP/IP 与 GPIB 都是如此，属于既有行为）。
- 各家校准（CAL）状态、端口数、通道数差异未逐一实测，统一按「单通道 4 个 S 参数」处理。
- **实机验证状态**：TCP/IP 分支已在 R&S ZNA26（`192.168.2.111:5025`）上验证过。**2026-11-11 首次在 Agilent N4225A 上做 TCP/IP 联调**，暴露出测量定义只有 `DEF`/`SDEF` 两种写法、PNA 系机型两条都回 `-113`；已补上 `DEF:EXT` / `EXT` 回退与错误队列清理，但**尚未在该机型上复测**（缺 `*IDN?` 原文与真机命令表）。GPIB 分支目前完成的是 mock 仪器全链路测试 + 本机 VISA 运行时加载验证，**82357B 接真机的联调尚未完成**，需要在 Connection Expert 能看到 `GPIB0::xx::INSTR` 后现场确认一次。

---

## 8. 真机 SCPI 方言探测（排障首选）

连接报 `failed to define measurement ...`、或仪器面板出现 `-113` 时，**先不要升级固件**，用探测脚本查清这台机器到底认什么命令：

```powershell
cd D:\Tuner
.\.venv\Scripts\python verification\probe_vna_scpi.py --host 192.168.2.111 --port 5025
.\.venv\Scripts\python verification\probe_vna_scpi.py --interface gpib --resource GPIB0::17::INSTR
# 需要机器可读结果时加 --json
```

脚本输出三部分：

1. `*IDN?` / `*OPT?` —— 真实型号与**固件版本**，用来回答「是不是版本太旧」；
2. 6 种测量定义写法哪些被接受（被拒的逐条标 `REJECT` 并显示错误队列内容）；
3. 扫频、功率、IFBW、触发、取数助记符（SDATA / SDAT / TRACe / MEASure）的可用情况。

它只发查询和无害配置命令，不发 `*RST`；但探测「测量定义」时会创建 `TL_probe_*` 测量 / 改动迹线 1 的参数，跑完按需在前面板恢复。把输出整段发回来，即可按真实命令集固化专用 profile。

**关于「-113 是不是固件太旧」**：-113 的规范定义是「命令头语法合法，但本设备未定义该命令」。升级固件**不会**让仪器凭空支持另一套命令树，正确做法是按 `*IDN?` 换语法。只有当 IDN 显示固件确实停留在很早期版本、且该机型手册确认新固件才加入对应命令时，升级才有意义——**先探测，再决定**。

---

*更新日期：2026-11-11 · 对应代码 `tuner/vna_drivers.py`、`tuner/web/handlers.py`、`tunerlab_app/pages_vna.py`、`verification/probe_vna_scpi.py` · 版本 TunerLab v0.5.10*
