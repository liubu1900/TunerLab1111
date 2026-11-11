# Tuner 三页联动实现与验收记录

日期：2026-11-11。对应 [设计与 A01–A18 清单](TUNER_CONFIGURATION_DESIGN.md)。

已完成源码更新。下表的“通过”指自动化模拟链路、Session、HTTP 或 Qt 页面验证；不代表 Windows DLL、现场接线或机械运动已经验收。

## 已实现的使用方式

通讯设置统一选择并确认设备组合：

| 组合 | 当前对象 | 逻辑轴 → 控制器物理轴 |
|---|---|---|
| 单台三轴 `single_3` | Tuner 1 | X→0、Z1→1、Z2→2 |
| 双台三轴 `dual_3` | 左 Tuner | X→0、Z1→1、Z2→2 |
| 双台三轴 `dual_3` | 右 Tuner | X→3、Z1→4、Z2→5 |
| 单台四轴 `single_4` | Tuner 1 | X→0、Z1→1、Z2→2、Z3→3 |

三页共用 Session 和当前 Tuner；单台组合不能选择右侧。每种组合、每台 Tuner、每根轴独立保存参数；双三轴右 X 与单四轴 Z3 虽然都使用轴3，参数分别保留。

操作顺序：

1. 控制器断开时选择并确认现场组合，检查三页显示的逻辑轴和物理轴号。
2. 在轴参数页编辑、**保存配置**。这一步只写电脑文件。
3. 连接控制器，**读取控制器参数**并查看差异；读取不会覆盖编辑草稿或已保存配置。
4. 需要下发时，明确选择**写入当前轴**或**写入当前 Tuner**；逐轴读回校验。失败后显示成功、失败、未执行，未核实的轴不能继续普通运动。
5. 原点输入、输出口、有效电平、回零脉冲分别确认。软件回零字段也必须显式应用，不能仅因硬件字段读回一致就视为生效。

左右切换保持连接和仍可信的运行状态，不自动回零或写参数。更换设备组合要求先断开。连接握手、排队任务、运动、回零、真实建库及参数操作期间，后台同样阻止切换。停止接口绕过普通任务锁，针对本组合全部物理轴；软件停止能力仍取决于实际驱动和硬件响应。

## A01–A18 自动化核对

核心用例位于 `TunerLab/tests/test_installation_acceptance.py`；Qt 用例位于 `TunerLabApp/tests/test_installation_ui.py`。表内列出用例名称的辨识部分。

| 编号 | 结果 | 核对内容与用例 |
|---|---|---|
| A01 | 通过 | 单三轴只向0/1/2写参数及移动；`A01_A04_writes_and_moves`、`layout_selector_rebuilds_all_pages` |
| A02 | 通过 | 双三轴左侧只访问0/1/2；同一参数化指令记录用例 |
| A03 | 通过 | 双三轴右侧只访问3/4/5；同一参数化指令记录用例 |
| A04 | 通过 | 单四轴四行、Z3→3，无右侧与轴4/5入口；同上及 Qt 页面用例 |
| A05 | 通过 | 左右配置、读回及应用状态隔离；`A05_A06_profiles_and_parameter_states_remain_independent` |
| A06 | 通过 | 组合切换再返回保留独立参数；同上，覆盖物理轴3的不同用途 |
| A07 | 通过 | 三页选择同步、轴行显示物理号、新页面使用新上下文；`all_three_pages_switch_together`、`new_panel_after_connect` |
| A08 | 通过 | 未保存草稿保存/放弃/取消三种路径，不隐式写硬件；`dirty_draft_has_save_discard_cancel` |
| A09 | 通过 | 排队即锁定目标；连接握手期间禁止配置切换；持有普通运动锁时停止仍可调用；`queued_task_blocks_switch`、`connect_handshake_reserves`、`stop_api_does_not_wait` |
| A10 | 通过 | 重叠轴、不支持组合及未知目标在发指令前拒绝；`rejects_overlapping_or_unsupported_installations_without_io` |
| A11 | 通过 | 保存、读取、写入分离；读取保留草稿，复制不携带接线；`save_read_and_selected_write_are_separate`、Qt读回用例及HTTP流程 |
| A12 | 通过 | 部分失败的逐轴结果和运动门禁；`partial_failure_preserves_per_axis_outcomes_and_blocks_motion` |
| A13 | 通过 | 坐标变更只使相关轴回零及对应Tuner校准失效；速度不清除回零；核验后运行限位更新且保留超时；相关 `A13_*` 用例 |
| A14 | 通过 | 左右切换无重新开连接、自动回零、写参数；可信回零不串台；`switch_preserves_left_home_without_writes_reopen_or_automatic_home` |
| A15 | 通过 | 断开重连不恢复旧回零/参数核验状态；`connect_does_not_adopt_old_home_or_simulated_application_state` |
| A16 | 通过 | 旧配置备份、冲突及损坏配置保留、原子提交失败不替换运行状态；相关 `A16_*` 用例 |
| A17 | 通过 | 旧页面、旧API版本、上次进程上下文拒绝；`queued_old_panel_cannot_target_new_tuner`、`previous_process_context_is_not_reused` |
| A18 | 通过 | 虚拟与真实状态隔离，重连不复用虚拟回零及参数应用状态；同A15 |

## 配置迁移与恢复

配置格式已升至 **schema 6**，发版版本为0.5.14。Windows 默认文件为 `%APPDATA%\TunerLab\settings.json`。

- 旧schema加载前保存原文到 `settings.json.pre-schema6.bak`，既有备份不覆盖；迁移保留原始 `legacy_config`、轴参数及VNA设置。
- 旧版左右两条记录不能证明现场一定安装双台；安装信息不明确时必须确认组合后才能真实连接。冲突配置不静默下发。
- Session 遇到损坏配置会保留 `settings.json.invalid.bak`，界面允许检查，但不能将默认候选直接当成已确认机台。
- 回零、位置、参数读回一致和校准可信状态不从磁盘恢复成实机事实。
- 如需退回旧程序，先退出应用并另存当前schema6文件，再用迁移前备份恢复 `settings.json`；新组合的后续编辑不会自动反向迁入旧schema。恢复后仍需确认现场参数。

## 验证结果与现场剩余项

以下为联动实现阶段的本地结果；0.5.14发版记录见 [版本说明](RELEASE_0.5.14.md)。2026-11-11，Linux / Python3.12 / 固定依赖：完整后端及桌面测试 **689 passed、9 skipped，0 failures、0 errors**；桌面 smoke 构建 **17页，退出码0**。9项跳过分别为1项Windows厂商DLL检查和8项默认关闭的实机测试。JavaScript语法及 `git diff --check` 通过。

本次会话记录：`/workspace/layout-review/delivery-tests.xml`、`delivery-tests.log`、`delivery-smoke.log`；三页渲染截图在同目录 `comm.png`、`motion.png`、`params.png`。

Windows现场还需完成：厂商DLL/VISA加载、三种组合实际接线和控制器轴数核对、各轴参数读回、原点输入/输出及极性确认、受控低速动作、断线重连及停止响应；这些检查不能由模拟链路替代。上述为发版前的本地辅助验证；0.5.14的Windows构建及冻结EXE启动检查由GitHub Actions执行，只有成功后才发布EXE/ZIP。实机验证仍需现场完成。

另已依据用户提供的 `pnahelp.chm` 修订N5224A触发等指令，见 [离线手册核对](../../TunerLab/docs/N5224A_OFFLINE_MANUAL_AUDIT.md) 和 [现场预检步骤](../../TunerLab/docs/N5224A_GPIB_PRECHECK.md)。
