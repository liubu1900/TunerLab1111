# Agilent N5224A SCPI 指令集核对（真机 + 手册逐条对照）

> 仪器：Agilent Technologies N5224A，序列号 MY51441375，固件 **A.09.90.19**
> 接口：GPIB0::16::INSTR（Agilent 82357B USB-GPIB）
> 选项：\*OPT? = "080,086,217,021,025,P02"
> 数据来源：两次 SCPI 调试工具真机探测（2026-11-11 16:53、17:26）+ Keysight PNA 在线编程手册。
> 结论：**指令集本身正确**，17:26 那次"未找到可用定义命令"是**同名测量导致的 +104 被误判**，不是命令不支持。

---

## 0. 两份探测报告的差异（本次报错的真正原因）

| 命令 | 16:53 第一次 | 17:26 第二次 | 说明 |
|---|---|---|---|
| `:CALC1:PAR:DEF:EXT 'TL_probe_S11','S11'` | **OK +0** | +104 Duplicate measurement name | 命令头有效；第二次同名已存在 |
| `:CALC1:PAR:EXT 'TL_probe_S11','S11'` | +104 | +104 | 与 DEF:EXT 等价（DEFine 可省略） |
| `:CALC1:PAR:DEF 'TL_probe_S11','S11'` | -158 String data not allowed | -158 | 旧 DEF 的 `<param>` 不应加引号；PNA 用 DEF:EXT |
| `:CALC1:PAR:SDEF` | -113 Undefined header | -113 | R&S 专有，PNA 确实不支持 |

**关键点**：`-113 Undefined header` = 命令头不存在；`+104 Duplicate measurement name` = 命令头和参数都被接受，只是这个名字的测量**已经存在**。
第一次探测创建了 `TL_probe_S11` 并留在仪器上；第二次探测所有定义候选都撞到这个名字 → 全部 +104 → 旧版探测工具把 +104 当成 REJECT，误报"无可用写法"。

**连带风险（已修复）**：驱动连接时要定义 S11/S21/S12/S22 四个测量。只要仪器上残留同名测量（面板默认就有 `CH1_S11_1` 等，或上次调试遗留），DEF:EXT 就返回 +104；旧驱动把它当失败并一路回退，最后抛 `failed to define measurement S11`。这才是 0.5.12"还有报错"的来源。

---

## 1. 连接/识别阶段（每次连接都发）

| # | 指令 | 用途 | N5224A 实测 | 手册依据 | 结论 |
|---|---|---|---|---|---|
| 1 | `*CLS` | 清空错误队列 | 有 | IEEE-488.2 标准 | ✅ 保留（best-effort） |
| 2 | `*IDN?` | 读身份 | `Agilent Technologies,N5224A,MY51441375,A.09.90.19` | IEEE-488.2 标准 | ✅ 命中 `keysight_pna` profile |
| 3 | `:SYST:ERR?` | 读错误队列 | 有（返回 +0 / -113 / +104） | IEEE-488.2 标准 | ✅ 用于判定每条命令是否被接受 |

## 2. 预设阶段（\_preset，每次连接都发）

| # | 指令 | 用途 | N5224A 实测 | 手册依据（PNA 命令树） | 结论 |
|---|---|---|---|---|---|
| 4 | `:FORMat:DATA ASCii` | 设 ASCII 取数格式 | **OK +0** | `:FORMat:DATA {ASCii\|REAL32\|REAL64}`，默认 ASCii（e5052b/e5063a FORMat:DATA 页） | ✅ 正确 |
| 5 | `:SENS1:SWE:TYPE LIN` | 线性扫频 | **OK +0** | `SENSe:SWEep:TYPE`（pxivna Setup 页） | ✅ 正确 |
| 6 | `:INIT1:CONT OFF` | 关闭连续触发 | **OK +0** | `INITiate<cnum>:CONTinuous`（PNA 命令树） | ✅ 正确；**必须带通道号 1** |
| 7 | `:CALC1:PAR:DEF:EXT '<name>','<param>'` | 定义命名测量（4 次：S11/S21/S12/S22） | **OK +0**；同名时 +104 | `CALCulate<cnum>:PARameter[:DEFine]:EXTended <Mname>,<param>`，适用 All（pxivna Calculate:Parameter 页）。**`DEFine` 为可选节点** | ✅ 首选方言；+104 按"已存在可复用"处理 |
| 8 | `:CALC1:PAR:SEL '<name>'` | 选中测量 | **OK +0** | `CALCulate<cnum>:PARameter:SELect <Mname>`，适用 N522xB/N523xB/N524xB（同页）；名称大小写敏感 | ✅ 正确；**不要用 `PAR1:SEL`（实测 -109）** |

## 3. 扫频/功率/带宽设置

| # | 指令 | 用途 | N5224A 实测 | 手册依据 | 结论 |
|---|---|---|---|---|---|
| 9 | `:SENS1:FREQ:STAR <Hz>` | 起始频率 | **OK +0** | `SENSe<cnum>:FREQuency:STARt`（e5080a Sense:Frequency 页，All） | ✅ 正确 |
| 10 | `:SENS1:FREQ:STOP <Hz>` | 终止频率 | **OK +0** | `SENSe<cnum>:FREQuency:STOP`，同上 | ✅ 正确 |
| 11 | `:SENS1:SWE:POIN <n>` | 扫频点数 | **OK +0** | `SENSe:SWEep:POINts`（pxivna Setup 页） | ✅ 正确 |
| 12 | `:SOUR1:POW:LEV <dBm>` | 源功率 | **OK +0** | `SOURce<cnum>:POWer[:LEVel][:IMMediate][:AMPLitude] <num>`，适用 All（pxivna/e5055a Source 页）。`LEVel/IMMediate/AMPLitude` 均为**可选节点** | ✅ 正确；长写法 `SOUR1:POW:LEV:IMM:AMPL` 亦 **OK +0**，两者等价 |
| 13 | `:SENS1:BAND <Hz>` | 中频带宽 IFBW | **OK +0** | `SENSe<cnum>:BANDwidth\|BWIDth[:RESolution]`，适用 All（pxivna Sense:Bandwidth 页），例 `SENS:BWID 1KHZ` | ✅ 正确 |

## 4. 触发与取数

| # | 指令 | 用途 | N5224A 实测 | 手册依据 | 结论 |
|---|---|---|---|---|---|
| 14 | `:INIT1:IMM;*OPC?` | 单次触发并等待完成 | `:INIT1:IMM` **OK +0**；`*OPC?` 标准 | `INITiate<cnum>:IMMediate`；`*OPC?` IEEE-488.2 | ✅ 正确；**必须带通道号 1**（`:INIT:IMM` 无通道号实测超时） |
| 15 | `:CALC1:DATA? SDATA` | 取复数 S 参数（实,实,虚,虚…） | **OK +0**（返回 `+5.15E-002,+5.87E-001,…`） | `CALCulate<cnum>:DATA? <char>`，适用 **N522xB/N523xB/N524xB**，`<char>=SDATA` 为"已加误差修正的复数测量数据"，每点返回 2 个数（pxivna Calculate:Data 页） | ✅ 正确；SDATA 是 PNA 正确助记符 |
| 15b | `:CALC1:DATA? SDAT` | （R&S 助记符，仅对照） | -224 Illegal parameter value + 超时 | PNA 手册只列 SDATA；SDAT 为 R&S 写法 | ❌ 不用于 PNA；仅 R&S profile 使用 |
| 15c | `:CALC1:TRAC1:DATA? SDATA` / `:CALC1:MEAS1:DATA:SDATA?` | （其他家族取数，仅对照） | -113 + 超时 | 非 PNA 命令树 | ❌ 不用于 PNA |

## 5. 探测工具专用（清理/回读，非测量流程）

| # | 指令 | 用途 | N5224A 实测 | 手册依据 | 结论 |
|---|---|---|---|---|---|
| 16 | `:CALC1:PAR:CAT?` | 回读测量目录 | 有（测量目录回读=有） | `CALCulate<cnum>:PARameter:CATalog?`，适用 All，返回 `"name,param,…"`（默认 `"CH1_S11_1,S11"`）（pxivna Calculate:Parameter 页） | ✅ 正确 |
| 17 | `:CALC1:PAR:DEL 'TL_probe_S11'` | **本次新增**：探测前删除上次同名测量 | 支持（PNA 命令树含 DELete） | `CALCulate<cnum>:PARameter:DELete[:NAME] <Mname>`，适用 All；`:DELete:ALL` 删全部（同页） | ✅ 修复重复探测的关键 |

---

## 6. 已确认的 N5224A 方言（keysight_pna profile）

```text
define_cmd      = :CALC1:PAR:DEF:EXT     # 唯一被 +0 接受的定义命令
fetch_mnemonic  = SDATA                  # :CALC1:DATA? SDATA
power_cmd       = :SOUR1:POW:LEV         # 与长写法 IMM:AMPL 均 +0，取短写法
ifbw_cmd        = :SENS1:BAND
trigger_cmd     = :INIT1:IMM             # 必须带通道号
```

## 7. 本次代码修复（0.5.13）

1. **驱动 `vna_drivers.py`**：`_define_measurement()` 现在把 `+104 Duplicate measurement name` 视为"命令头有效、测量已存在可复用"，不再一路回退到抛异常 → 重连幂等。
2. **探测工具 `vna_probe.py`**：
   - 探测前 best-effort 发送 `:CALC1:PAR:DEL 'TL_probe_S11'`，清掉上次探测遗留的同名测量；
   - `+104` 判定为 OK（命令头有效），报告明确解释，不再误报"未找到可用写法"。
3. **回归测试**：用 error1.txt 的"第二次探测全部 +104"场景做重放测试，确保方言仍被正确识别、重连不再抛错。

## 8. 手册参考链接

- CALCulate:PARameter（DEF:EXT / SELect / CATalog / DELete）：https://helpfiles.keysight.com/csg/pxivna/Programming/GP-IB_Command_Finder/Calculate/Parameter.htm
- CALCulate:DATA（SDATA 助记符、适用机型）：https://helpfiles.keysight.com/csg/pxivna/Programming/GP-IB_Command_Finder/Calculate/Data.htm
- SENSe:FREQuency（STARt/STOP）：https://helpfiles.keysight.com/csg/e5080a/programming/gp-ib_command_finder/sense/frequency.htm
- SENSe:BANDwidth：https://helpfiles.keysight.com/csg/pxivna/Programming/GP-IB_Command_Finder/Sense/Sense_Bandwidth.htm
- SOURce:POWer（LEVel/IMMediate/AMPLitude 可选节点）：https://helpfiles.keysight.com/csg/pxivna/Programming/GP-IB_Command_Finder/source.htm
- FORMat:DATA（ASCii/REAL32/REAL64）：https://helpfiles.keysight.com/csg/e5052b/programming/new_command_reference/format/scpi_format_data.htm
- 迹线/测量/通道引用规则（SELect 名称大小写敏感、MNUMber）：https://helpfiles.keysight.com/csg/pxivna/Programming/Learning_about_GPIB/Referring_to_Traces_Measurements_Channels_Windows_Using_SCPI.htm
