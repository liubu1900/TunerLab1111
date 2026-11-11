# N5224A GPIB 连接专项复核

目标使用日期：2026-11-12。用户已确认实际仪器为 **N5224A**，不是最初描述的N5244A。
上传的 `error3.txt`：固件 **A.09.90.19**，接口 **GPIB0::16::INSTR**，选项 `080,086,217,021,025,P02`。这不是另一台N5244A的固件或验收证据。

已读取用户补充的 `pnahelp.chm`，修订此前的触发方案。逐页依据、文件校验值和差异分析见 [离线手册核对](N5224A_OFFLINE_MANUAL_AUDIT.md)。修改后的完整流程仍需现场验证。

## 指令对照和异常

| 功能 | 当前使用 | 核对结论及处理 |
|---|---|---|
| 身份 | `*IDN?`，探测另读 `*OPT?` | 与上传记录核对型号、固件及选项 |
| 清状态 | `*CLS`、`:SYST:ERR?` | 记录可用；严格解析完整错误码，不能把空响应当成功 |
| 通道保持 | `:SENS1:SWE:MODE HOLD` | 替代PNA初始化中的 `INIT1:CONT OFF`；后者实际改变触发源，不是通道HOLD |
| 扫描设置 | `:SENS1:SWE:TRIG:POIN OFF`、`:SENS1:AVER:STAT OFF`、`:FORMat:DATA ASCii`、`:SENS1:SWE:TYPE LIN` | 按手册建立一次普通线性扫描，避免遗留点触发/平均改变触发需求 |
| 定义测量 | `:CALC1:PAR:DEF:EXT 'S11','S11'`，其余三个S参数同理 | +104表示重名；查 `PAR:CAT:EXT?` 核对参数一致才复用。只保留PNA的 `PAR:EXT`回退，不自动删除用户测量 |
| 错误定义写法 | 带引号旧DEF、SDEF、MEAS:DEF、逐迹线DEF | 记录出现 -158/-113/-113/-109；已从已识别PNA的探测路径移除。-158不表示整个DEF命令头不存在 |
| 选择测量 | `:CALC1:PAR:SEL 'S11'`及 `:CALC1:EQU:STAT?` | 选择失败停止取数；公式不是OFF则拒绝，避免测量名称正确而数据受公式改变 |
| 频率/点数 | `:SENS1:FREQ:STAR`、`STOP`、`:SENS1:SWE:POIN` | 上传记录1–2GHz/11点接受；每条配置检查错误；手册支持单频1点 |
| 功率 | `:SOUR1:POW:LEV`、`:SOUR1:POW2:LEV` | 短写合法；分别设置两源端口，避免未耦合时遗漏端口2。保留0dBm合法输入 |
| IFBW | `:SENS1:BAND`及 `:SENS1:BAND?` | 设置合法；查询仪器实际值，处理手册规定的自动向上取整 |
| 单次触发 | `HOLD` → `:TRIG:SOUR IMM` → `:SENS1:SWE:MODE SING` → `*OPC?` | 手册简单单通道方式；当前不再混用IMM源与要求Manual源的INIT:IMM。完成必须精确返回1 |
| 无通道号触发 | `:INIT:IMM` | 上传记录超时；PNA分支不再尝试，不在OPC超时后重触发或查询重试 |
| 数据 | `:CALC1:DATA? SDATA` | 上传记录正常，每点实部/虚部两数；负数开头合法。是否进行误差修正取决于仪器CORR状态 |
| 不兼容取数 | `SDAT`、`TRAC1:DATA? SDATA`、`MEAS1:DATA:SDATA?` | 上传记录 -224/-113/-113并超时，已从已识别PNA的探测移除 |
| GPIB消息结束 | VISA `send_end=True`、终止字符串为空 | 显式使用EOI；Windows仍需实际VISA/82357B驱动 |

## 配置与通信失败处理

PNA必需配置、测量选择和数据查询检查错误队列，失败返回具体指令及原始错误；VISA write未抛异常不足以判为成功。

部分配置成功后后续失败，频率网格和仪器可能不一致，此时关闭会话并要求重连。OPC超时同样断开，避免将残留响应当成下一查询结果。界面状态读取实际transport状态。

触发、选择四个S参数、读取数据共用采集锁，避免本进程同一会话的扫描交错。Connection Expert Interactive IO、其他应用实例、VNA面板和真实VNA建库仍不能同时占用同一仪器。

## 11月12日现场步骤

1. Windows x64安装Keysight IO Libraries Suite和82357B驱动，重启应用。在Connection Expert确认 **GPIB0::16::INSTR**，验证IDN为N5224A；完成后关闭Interactive IO。
2. 应用“矢网控制”选择GPIB、地址 `16` 或完整资源串、厂商自动识别/PNA。默认地址可能仍为17，每次启动核对。清除其他型号的方言；旧PNA INIT触发覆盖会迁移到SING并提示。
3. 先用 **1–2GHz、11点、-10dBm、IFBW1000Hz、超时10000ms** 连接并配置，单扫一次。确认无 -113/-158/-224/超时，四条S参数各11个复数点，频率、两端口功率和实际IFBW与面板一致。
4. 若提示Equation Editor未关闭，先在仪器核查该测量，确认用途后手动关闭公式或使用不带公式的测量；应用不会自动改已有公式。
5. 断开再重连，验证同名同参数复用；错误映射应明确拒绝。再验证0dBm设置。确认校准、去嵌及源端口配置符合测量需求，软件未执行仪器自身RF校准。
6. 低IFBW/大点数扫描过慢时，提高超时后再重连；失败后不要重复点击扫频。排障导出探测报告和日志，保留完整IDN、具体指令、原始错误及界面参数。
7. 真实VNA建库前先断开VNA面板；两个入口仍采用独立会话，此既有限制没有在本次改动中重构。

## 验证范围和运行程序

`tests/test_n5224a_gpib.py`使用有状态VISA/GPIB替身，覆盖仪器身份、同名目录、手册触发语义、OPC错误/超时、四参数顺序、Equation Editor、IFBW取整及两端口功率。它不连接实机，不能证明RF准确度或Windows运行时已验收。

离线手册复核阶段的本地完整验证：**689 passed、9 skipped、0 failures、0 errors**；桌面smoke构建17页并退出0。9项跳过为Windows厂商DLL检查及8项默认关闭的实机测试。记录位于 `/workspace/layout-review/delivery-tests.xml`、`delivery-tests.log`、`delivery-smoke.log`；这些是Linux辅助结果。

修复随 **0.5.14** 发版；GitHub Actions仅在Windows构建、SDK加载及安装启动检查成功后发布新EXE/ZIP，见 [版本说明](../../TunerLabApp/docs/RELEASE_0.5.14.md)。**已有0.5.13安装包不会自动包含这些修复**；现场需使用0.5.14产物或包含本次修改的源码。
