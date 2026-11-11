# TunerLab 0.5.14

发布日期：2026-11-11。适用平台：Windows 10/11 x64。

## 更新内容

- 通讯设置、轴移动控制、轴参数设定共用设备组合与当前Tuner。支持单三轴0/1/2、双三轴左0/1/2及右3/4/5、单四轴0/1/2/3，各组合和每轴参数独立保存。
- 分离本地保存、控制器读回、指定轴/Tuner写入；增加逐轴核验、部分失败状态、任务期间切换限制及旧页面/旧请求拒绝。
- 配置升级至schema6，迁移前保留原文备份；不把虚拟或磁盘中的回零、参数一致状态当成实机事实。
- 按用户提供的PNA离线手册修复N5224A触发源配合，使用HOLD/IMM源/SING/OPC完成确认；完善同名测量目录、Equation Editor检查、IFBW实际值读回、两源端口功率及超时断开。
- 修复GitHub Actions环境变量表达式导致工作流不能启动的问题。Windows包检查SDK所需的VC++2010 x64运行时，不再依赖上一版发布目录。
- 修复Windows低精度系统时钟下连续保存目标模式时“最新优先”排序不稳定的问题，保留原有文件格式。

## Windows下载与运行

- `TunerLab-0.5.14-win64.zip`：便携版，完整解压后运行其中的 `TunerLab.exe`，保留 `_internal` 目录。
- `TunerLab-0.5.14-SHA256SUMS.txt`：便携ZIP的SHA256校验值。

请在配置备份后升级；旧版安装包不会自动取得源码修复。GPIB/82357B仍需安装Keysight IO Libraries Suite，N5224A地址为 `GPIB0::16::INSTR`，不可沿用默认17。

## 验证范围

0.5.14本地完整回归694项通过、9项跳过，桌面17页启动检查通过；包含新增Windows运行时打包核对用例。Windows发版沿用已有PyInstaller便携ZIP方式；仅在Windows测试、冻结EXE启动、SDK加载和压缩包校验成功后上传产物。

没有在本次会话执行真实运动控制器、N5224A或RF准确度验收。现场仍需按 [N5224A预检](../../TunerLab/docs/N5224A_GPIB_PRECHECK.md) 和 [Tuner验收记录](TUNER_CONFIGURATION_ACCEPTANCE.md) 核对接线及实际读回。
