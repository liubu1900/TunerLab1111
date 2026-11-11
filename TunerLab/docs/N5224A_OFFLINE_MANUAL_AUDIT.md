# N5224A 离线编程手册核对

日期：2026-11-11。目标仪器由用户确认：**Agilent N5224A，A.09.90.19，GPIB0::16::INSTR**；选项 `080,086,217,021,025,P02`。不将该记录推广为另一台N5244A的实测结果。

## 文档来源与证据范围

已成功解包并阅读用户上传的 `pnahelp.chm`，共19,422,821字节，SHA256：

```text
a1dd359a59ecfa82d261afdd8aaca76bc133b28d5731eb4456b08b9f86a2b6e5
```

这是PNA官方离线帮助，相关页面包含2012–2013年的修改记录；没有据此声称其发行版本恰好对应A.09.90.19。命令语义以实际阅读的页面为依据，仪器接受/拒绝的现场证据来自 `error3.txt`；修改后的完整流程仍未在实机执行。此前在线帮助域名403的限制已由该离线资料补足，不再作为本次手册核对的阻塞。

下表路径均相对于CHM内部的 `Programming/GP-IB_Command_Finder/`。

## 指令差异、影响与修复

| 项目 | 手册依据 | 发现与当前处理 |
|---|---|---|
| 触发源与立即触发 | `Initiate.htm`、`Trigger_SCPI.htm`、`Sense/Sweep_SCPI.htm`；另见 `Programming/GPIB_Example_Programs/Triggering_the_PNA_using_SCPI.htm` | **此前 `TRIG:SOUR IMM` 后配 `INIT1:IMM` 不符合手册**：INIT:IMM要求MANual源。已改为手册简单单通道方式：HOLD → IMM源 → `SENS1:SWE:MODE SING` → `*OPC?`，不在超时后重试INIT |
| 关闭连续触发 | `Initiate.htm` | `INIT:CONT OFF`切换全局触发源为Manual，不等同于通道HOLD。PNA初始化改用 `SENS1:SWE:MODE HOLD` |
| 点触发、平均 | `Sense/Sweep_SCPI.htm`、`Sense/Average_SCPI.htm` | SING接收一个触发；历史点触发可能导致扫描不完整。初始化显式 `SWE:TRIG:POIN OFF`、`AVER:STAT OFF`，执行一次普通线性扫描 |
| 同名测量目录 | `Calculate/Parameter.htm` | 普通 `PAR:CAT?` 中接收机参数 `R1,1`会引入额外逗号；改查 `PAR:CAT:EXT?`，手册规定其转换为 `R1_1`，能可靠按名称/参数对解析 |
| 定义与复用 | `Calculate/Parameter.htm` | 使用 `PAR:DEF:EXT`及PNA的 `PAR:EXT`回退。+104只说明重名，核对同名确实映射相同S参数才复用；支持 `S1_1`与 `S11`等价表示。记录中的带引号旧DEF报-158，不等于整个DEF命令头不存在 |
| Equation Editor | `Calculate/Equation.htm`、`Calculate/Data.htm` | 测量名为S11不保证SDATA就是S11：启用公式可能使其取其他参数或零值。选择后查询 `CALC1:EQU:STAT?`，只接受OFF；不自动改用户公式，报错断开以免采到错误数据 |
| 复数数据 | `Calculate/Data.htm`、`Format_SCPI.htm` | 保持ASCII及完整 `SDATA`助记符，每点两数（实、虚）；负数开头合法。SDATA是否应用误差修正取决于仪器CORR状态，软件没有执行RF校准 |
| 点数 | `Sense/Sweep_SCPI.htm` | 手册支持1点至型号上限；保留单频单点用途，不以通用“至少2点”规则拒绝 |
| IFBW | `Sense/Sense_Bandwidth.htm` | `SENS1:BAND`是合法短写。无效带宽可能无报错地向上取到下个支持值；设置后查询 `BAND?`，把实际值用于状态，避免把请求值当成实机值 |
| 两端口功率 | `source.htm` | `SOURce<cnum>:POWer<port>[:LEVel][:IMMediate][:AMPLitude]`省略port默认为1；功率未耦合时只设置端口1会遗漏端口2。标量功率现在同时设置 `SOUR1:POW:LEV`及 `SOUR1:POW2:LEV`，保持0dBm有效 |
| 完成及错误 | `Common_Commands.htm`及上述触发示例 | `*OPC?`严格接受1；必要写入和查询检查错误队列，非0、空响应或通信失败不能判成功。超时断开以防残留响应被下个查询误读 |

当前PNA采集核心序列如下。写入与查询分开发送，每条必要配置另外核对错误队列：

```text
# 初始化及配置（不复位仪器、不删除用户测量）
:SENS1:SWE:MODE HOLD
:SENS1:SWE:TRIG:POIN OFF
:SENS1:AVER:STAT OFF
:FORMat:DATA ASCii
:SENS1:SWE:TYPE LIN
:CALC1:PAR:DEF:EXT 'S11','S11'  # 同理S21、S12、S22；重名则核对目录
:SENS1:FREQ:STAR <Hz>
:SENS1:FREQ:STOP <Hz>
:SENS1:SWE:POIN <N>
:SOUR1:POW:LEV <dBm>
:SOUR1:POW2:LEV <dBm>
:SENS1:BAND <Hz>
:SENS1:BAND?

# 每次单扫：内部连续源，仅通道1接受一次触发后回HOLD
:SENS1:SWE:MODE HOLD
:TRIG:SOUR IMM
:SENS1:SWE:MODE SING
*OPC?

# 依次选择S11、S21、S12、S22并取数
:CALC1:PAR:SEL 'S11'
:CALC1:EQU:STAT?              # 必须OFF
:CALC1:DATA? SDATA
```

旧方言中保存的 `INIT1:IMM`、`INIT:IMM`及对应完整拼写在识别为PNA时会迁移到SING方式，并给出提示；未知PNA触发覆盖不静默执行。

## SCPI Recorder 链接

用户提供的 `NA520xA/Programming/SCPI_Recorder.htm` 属于另一条产品帮助路径。该在线链接在当前云环境仍返回403；本份旧PNA帮助中没有找到对应Recorder页面，因此没有据此声称A.09.90.19具备SCPI Recorder。它可以辅助记录支持该功能的仪器操作，但不能替代N5224A命令树及本机验证。

## 验证与剩余风险

`tests/test_n5224a_gpib.py` 的有状态VISA替身验证触发源约束、单扫完成、四参数顺序、同名目录、公式拒绝、IFBW取整、两端口功率及超时停止；全套测试结果见 [联动验收记录](../../TunerLabApp/docs/TUNER_CONFIGURATION_ACCEPTANCE.md)。

尚未执行Windows VISA/82357B连接、修改后的完整真机指令序列和RF准确度验收。旧仪器残留的特殊测量、源端口模式、校准/去嵌等设置，以及硬件故障仍可能导致拒绝或测量差异；不能保证连接时完全不报错。应依 [现场步骤](N5224A_GPIB_PRECHECK.md) 从11点扫描验证，并保留IDN、原始错误及实际配置。
