# 矢量网络分析仪（VNA）多厂商 TCP/IP 接入指南

> 适用范围：TunerLab Web 控制台（`python -m tuner.web`）的「矢网控制（VNA）」面板，
> 以及「校准工作台」中勾选"使用真实矢网"后的实机采集。
> 驱动代码：`tuner/vna_drivers.py`（仅依赖 Python 标准库 `socket`，无需 VISA 运行时）。

---

## 1. 一句话原理

所有支持的矢量网络分析仪都通过 **TCP/IP 上的 SCPI 文本协议** 直接控制，默认端口 **5025**。
软件用原生 socket 发命令、读响应，**不依赖** Keysight IO Libraries / R&S VISA / NI-VISA 等任何厂商运行时。
只要仪器开启了 SCPI（LAN）服务器、且与上位机同网段，即可连接。

支持的厂商（按 Web 面板下拉顺序）：

| 下拉项 | `vendor_key` | 代表机型 | 命令差异点 |
|---|---|---|---|
| 罗德 R&S | `rsp` | ZNB / ZVA / ZND | 测量定义 `:CALC:PAR:SDEF`，取数 `SDAT` |
| Keysight E5071C | `keysight_ena` | E5071C / E5080 / E5063（ENA 系） | `:CALC:PAR:DEF`，取数 `SDATA` |
| Keysight N5247 PNA-X | `keysight_pna` | N5247A / N522x / N523x（PNA/PNA-X） | 功率 `:SOUR:POW:LEV:IMM:AMPL` |
| 思仪 Ceyear | `ceyear` | AV3672 系列 | ENA 风格 SCPI |
| 安立 Anritsu | `anritsu` | VectorStar MS46xxx / ShockLine | ENA 风格 SCPI |
| 自动识别 | `auto` | — | 连接后读 `*IDN?` 自动判断（**推荐**） |

> 选择"自动识别"时，软件读 `*IDN?` 后按厂商关键词（ROHDE/R&S、E507/E508、N524/PNA、CEYEAR/AV367、ANRITSU/MS46…）
> 自动选定命令集。如果不确定机型，优先用"自动识别"。

---

## 2. 连接前的三项网络准备（每家都通用）

1. **同网段互通**：上位机（跑 Web 服务的 Windows 主机）与矢网必须在同一子网，能互相 `ping` 通。
   例如矢网 `192.168.0.50`、掩码 `255.255.255.0`，上位机 IP 也应在 `192.168.0.x`。
2. **开启 SCPI / LAN 服务器**：仪器需允许 LAN 远程控制（见第 3 节各品牌步骤）。
3. **防火墙放行 TCP 5025**：在上位机防火墙入/出站规则里放行 5025；若公司网络有隔离策略，请接在仪器同网段的小交换机/直连上，避免跨网段。

> 与 Zmotion 控制器不同，矢网是**标准 TCP 端口**，可用 `telnet 192.168.0.50 5025` 或
> `Test-NetConnection -Port 5025 192.168.0.50`（PowerShell）先验端口是否通，比 UDP 广播的控制器容易排查。

---

## 3. 各品牌开启 SCPI（LAN 远程控制）步骤

> 不同固件版本的菜单路径会有差异，下列为通用做法；若菜单名称不同，以仪器实际界面为准。
> 连上后若软件报"识别厂商"为空或命令报错，请把 `*IDN?` 返回字符串发给我，我再校准命令集。

### 3.1 罗德 R&S（ZNB / ZVA / ZND）
- 前面板：**System → Remote Control**（或 **I/O → Remote**），确认 **TCP/IP SCPI 服务器**已启用，端口 **5025**。
- 如远程操作被面板锁定，可在远程前于面板解除"本地锁定"，或在 SCPI 下用 `:SYST:LOCK:REQ` 申请控制权。
- 数据格式：R&S 取数助记符为 `SDAT`（软件已默认使用，无需手改）。
- 常见坑：R&S 的通道/迹线定义为 `:CALC1:PAR:SDEF 'name','S11'`（带引号），软件已按"严格带引号 + SDEF/DEF 互回退"处理。

### 3.2 Keysight E5071C（ENA 系列）
- 前面板：**System → Misc Setup → Network Setting**，设置 IP / 子网掩码 / 网关，并启用 LAN。
- ENA 的 **SCPI Socket 服务器默认端口 5025 且默认随 LAN 启用**；可在 `:SYST:COMM:LAN:PORT?` 查询/用 `:SYST:COMM:LAN:PORT <n>` 修改。
- 也可通过 SCPI 配置地址：`SYST:COMM:LAN:ADDR "192.168.0.50"`、`SYST:COMM:LAN:SMASK "255.255.255.0"`、`SYST:COMM:LAN:GATE "192.168.0.1"`。
- 数据格式： ENA 取数助记符为 `SDATA`，测量定义为 `:CALC1:PAR:DEF`（软件默认）。

### 3.3 Keysight N5247A（PNA / PNA-X 系列）
- 前面板：**System → System Setup → Network Setup** 设置 IP；**System → System Setup → Configure… → SICL/LAN 或 SCPI Server** 启用 SCPI 服务器（端口 **5025**，默认启用）。
- PNA-X 功率设置走 `:SOUR:POW:LEV:IMM:AMPL`（软件已默认；ENA 系用的是 `:SOUR:POW:LEV`，两者都会尝试）。
- 若仪器启用了"多端口"或"通道数"限制，软件固定定义 4 个 S 参数（S11/S21/S12/S22），需保证通道 1 可用。

### 3.4 思仪 Ceyear（AV3672 系列）
- 前面板：**系统 → 通信设置 → LAN / 网络**，配置 IP 并启用远程（LAN）控制。
- AV3672 采用 **ENA 风格 SCPI**，端口 **5025**，测量定义 `:CALC1:PAR:DEF`、取数 `SDATA`（与 ENA 一致，软件默认）。
- 如该机型使用与 ENA 不同的端口或命令前缀，请在前面板确认 SCPI 端口后，于 Web 面板把端口改一致。

### 3.5 安立 Anritsu（VectorStar / ShockLine）
- 前面板：**System → Remote / SCPI Server**（不同型号措辞不同），启用远程 SCPI，端口通常 **5025**。
- ShockLine 与 VectorStar 均为 ENA 风格 SCPI，软件按 `DEF` / `SDATA` 默认处理。
- 注意：部分 Anritsu 机型默认 SCPI 端口可能不是 5025（如 5030 或自定义），请在前面板"Remote"菜单确认实际端口，
  并在 Web 面板把"端口"改成对应值后再连接。

---

## 4. 软件发出的命令对照（便于对照仪器报错）

连接与采集过程中，软件（按厂商 profile）会依次发送以下 SCPI：

| 用途 | 命令（示例，key=auto 时按厂商切换） |
|---|---|
| 清状态 | `*CLS` |
| 识别 | `*IDN?` |
| 数据格式 | `:FORMat:DATA ASCii` |
| 扫频类型 | `:SENS1:SWE:TYPE LIN` |
| 连续触发关 | `:INIT1:CONT OFF` |
| 定义 S11 | `:CALC1:PAR:DEF 'S11','S11'`（R&S 用 `SDEF`） |
| 选迹线 | `:CALC1:PAR:SEL 'S11'` |
| 起始频率 | `:SENS1:FREQ:STAR <Hz>` |
| 终止频率 | `:SENS1:FREQ:STOP <Hz>` |
| 频点数 | `:SENS1:SWE:POIN <n>` |
| 功率 | `:SOUR1:POW:LEV <dBm>`（PNA-X 用 `:SOUR1:POW:LEV:IMM:AMPL`） |
| 中频带宽 | `:SENS1:BAND <Hz>` |
| 触发+完成 | `:INIT1:IMM;*OPC?`（返回 `1` 确认完成） |
| 取数 | `:CALC1:DATA? SDATA`（R&S 用 `SDAT`） |
| 读错误队列 | `:SYST:ERR?` |

### 容错机制
- **测量定义**：先发 `SDEF`，若 `:SYST:ERR?` 报错则自动改发 `DEF`（反之亦然），所以 R&S 与其余厂家都能定义成功。
- **触发**：依次尝试 `:INIT1:IMM` / `:INIT:IMM` / `:INITiate:IMMediate` + `*OPC?`，任一返回 `1` 即视为完成。
- **错误探查**：所有写命令后都会查 `:SYST:ERR?`，非空错误会汇总在"最近错误"字段，方便你定位。

---

## 5. 在 Web 控制台怎么用

### 5.1 单独控制矢网（「矢网控制 VNA」页）
1. 左侧导航 → **仪器控制 → 矢网控制 (VNA)**。
2. 填 **厂商**（建议"自动识别"）、**IP**、**端口**（默认 5025）、**超时**（默认 10000 ms）、扫频起止/点数、功率、中频带宽。
3. 点 **连接并配置** → 软件建立 socket、读 `*IDN?`、自动识别厂商、下发扫频/功率/中频。
4. 看"连接状态"卡：**状态=已连接**、**识别厂商**、**IDN**、**最近错误**。若 IDN 正常但识别厂商为空，说明关键词未命中，请手选对应厂商后重连。
5. 点 **单次扫描并读取** → 触发一次扫描、回读 S11~S22，页面绘制幅频曲线并列出数据表（单位 dB）。

### 5.2 用真实矢网跑校准（「校准工作台 → Dashboard」）
1. 左侧导航 → **校准工作台（模拟）→ Dashboard**。
2. 勾选 **使用真实矢网 (TCP/IP)**，填厂商/IP/起始/终止/频点。
3. 点 **初始化模拟硬件并回零**（注意：运动控制器仍是 Mock 或实机 Zmotion，与矢网相互独立；真实矢网只替换"测量"这一环）。
4. 后续「校准设置 / 校准运行 / 数据库 / Smith 图 / 阻抗调谐 / 验证报告」流程不变，只是 S 参数来自实机。

> 同一台矢网若同时被"VNA 面板"和"校准工作台真实矢网"两个会话占用，命令会交错。
> **单台实机建议二选一**：要么在 VNA 面板手动采，要么进校准工作台让它托管。

---

## 6. 故障排查

| 现象 | 可能原因 | 处理 |
|---|---|---|
| 连接超时 / 连接被拒 | IP 错、网段不通、防火墙拦 5025、SCPI 服务器未开 | 先 `ping` + `Test-NetConnection -Port 5025` 验端口；开 SCPI 服务器；放行防火墙 |
| `*IDN?` 返回空或乱码 | 端口不是 SCPI 端口、仪器已停在别的协议 | 确认端口为 SCPI（默认 5025）；换 Anritsu 自定义端口 |
| 识别厂商=空/未知 | IDN 字符串不含已知关键词 | 手选对应厂商下拉项后重连；把 IDN 发我加关键词 |
| 取数为空 / 解析报错 | 取数助记符不符（SDAT vs SDATA）或数据格式非 ASCII | 软件已按厂商设定；若仍空，检查仪器是否 `:FORMat:DATA ASCii` 支持，并把报错发我 |
| 定义测量报错 | 通道/迹线不可用、端口数受限 | 确认通道 1 可用、端口数与 S 参数数量匹配 |
| 触发后无完成确认 | 触发命令形态不同 | 软件已内置 3 种触发回退；仍失败请把 `:SYST:ERR?` 内容发我 |

---

## 7. 还没覆盖的点（如实机验证后补充）
- 各家"校准（CAL）状态、端口数、通道数"差异未逐一实测，目前统一按"单通道 4 个 S 参数"处理。
- 二进制数据块（`SDAT?`/`SDATA?` 的 `<header>#` 格式）暂未支持，统一走 ASCII 文本，速度够用但不如二进制快。
- 实测后我会根据各家的真实 `*IDN?` 与报错，把命令集收敛到精确版本，并更新本文件。

---

*生成日期：2026-11-03 · 对应代码 `tuner/vna_drivers.py` · 版本 TunerLab v0.1.0*
