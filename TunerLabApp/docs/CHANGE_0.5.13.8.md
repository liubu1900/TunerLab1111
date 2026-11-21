# 0.5.13.8 维护更新（初始化 / 建库矢网 / 校准文件夹 / 频率下发 / Smith 框选）

> 日期：2026-11-21　分支：`feature/0.5.13-move-only`
> 本版修复操作现场反馈的 6 个问题，不改变已保存校准库的数据格式。

---

## 1. 一键初始化弹两遍确认框

**现象**：点击「一键初始化全部轴」弹出两次确认框。

**根因**：按钮由基类 `ui_hardware.py` 创建并连接了一次 `clicked → on_initialize_all`；
桌面派生类 `pages_motion.py` 在把它重新布局进按钮行时又连接了一次。同一次点击调用槽函数两遍，
确认框弹两次（第二次的运动请求被 `_motion_busy` 拦下，所以机械只动一次，但弹窗重复）。

**修复**：删除派生类的重复连接，只保留基类一处。按钮和确认框文案精简为
「即将初始化全部轴，机械会移动。是否继续？」，不再在按钮 tooltip / 弹窗里解释
「回零 + MPOS 设为 0」等内部操作过程；单轴初始化按钮与 Web 端同步精简。
新增回归测试断言 clicked 信号只连接一次。

## 2. 建库真实矢网参数无需手动输入

**现象**：建库选「真实矢网」时要重新手填 IP / GPIB 地址，与「矢网控制」页已在用的设置重复。

**修复**：建库向导的矢网连接参数默认取用 `state.vna['config']`（矢网控制页当前设置）：
- 桌面 `StudioSetupPage` 构造时用其预填型号 / 接口 / IP / VISA 资源，并把端口、超时、SCPI 方言
  一并写入建库请求；字段仍可手动覆盖。
- 后端 `studio_bridge._build_vna()` 与 `handlers.studio_init_hardware()` 在请求缺少连接参数时
  回退到 `state.vna['config']`（含 dialect）。这样即便不经向导（Web / API）也不用手填。
- 提示文案改为「已自动取用矢网控制页当前设置」。

## 3. 「打开所在文件夹」打开错目录

**现象**：校准库页「打开所在文件夹」打开的是 TunerLab 默认数据目录，而不是当前库所在目录。

**根因**：`StudioDatabasePage.on_reveal()` 用的是 `paths.calibration_dir()`（新库的默认位置），
从不读取活动库的真实路径。

**修复**：改为从 `handlers.studio_database(state)['path']` 取当前库路径并打开其父目录；
仅在尚无活动库时回退到默认校准目录。回归测试覆盖"库在其它目录"与"无库回退"两种情况。

## 4. 任意频点列表不生效（仍按 5 GHz 单点测）

**现象**：建库选「任意频率列表」并填写频点，实际仍按默认单频点 5 GHz 测试。

**根因**（两处叠加）：
- `apply_frequency_plan()` 对列表模式只维护本地频率网格，**不下发 SCPI**，真实矢网停留在上一次的
  单点状态。
- 桌面真机路径 `studio_bridge._build_vna()` 完全忽略频率模式，无条件 `set_sweep(fo, fo, 1)`；
  列表模式下 `_init_body` 传的正是 `fo=5 GHz、points=1`，于是仪器被设成 5 GHz 单点。

**修复**：所有频率模式统一经 `apply_frequency_plan()` 把首/末频点与点数下发到仪器
`set_sweep(start, stop, points)`。等间隔列表精确复现（如 5/10/15 GHz），非等间隔列表为线性近似
并在预览里给出提示——关键是仪器不再停留在陈旧的 5 GHz 单点。桌面真机路径也改为按频率模式应用。

## 5. 所有 Smith 图框选放大异常（直接放到 60 倍）

**现象**：鼠标框选放大时直接放大到最大 60 倍，且放大的区域不是框选区域。

**根因**：桌面 `SmithView.zoom_to_rect()` 的缩放公式 `min(base/half_re, base/half_im)` 多乘了一个
`base`（约 160 px），使任何框选都被钳到 `max_zoom=60`，框选区域缩成一个小点。
Web 端 Smith canvas 则根本没有框选放大（左键拖拽是平移，缩放倍率固定）。

**修复**：
- 桌面公式改为 `min(1/half_re, 1/half_im) * margin`，缩放比例随框选大小变化（小框放大更多，
  大框放大更少），并把框选中心平移到视野中心。
- Web 端新增框选放大：左键拖拽画框，按框大小计算缩放、框选区域居中；Shift+拖拽平移，保留滚轮 /
  按钮缩放与复原。两个 Smith 图（矢量重复性、Γ 派生）均支持，并更新操作提示。
- 回归测试新增"倍率随框大小变化、不恒为 60、框选区域居中"断言。

## 6. 频率 6~18 GHz / 3 点却只有 18 GHz 一个点

**现象**：建库设置起始 6 GHz、终止 18 GHz、点数 3，实际库里只有 18 GHz 一个频点。

**根因**：
- 桌面真机初始化 `studio_bridge._init_real()` 从未把 `frequency_plan / frequency_mode / frequency_hz`
  写入 `state.studio`，库头频率元数据与状态不对。
- mock 分支对 `freq_sweep` 不把用户网格应用到 `MockVNAController`，其频率数组停留在
  1–20 GHz / 41 点默认，导致"设置的值"与"实际建库用的网格"不一致。

**修复**：真机路径补齐频率状态写入（与 `handlers` 对齐）；mock 分支对所有模式都应用频率方案到
合成矢网。回归测试验证 6/12/18 GHz 三点方案正确生成、下发并写入状态。

---

## 验证
- 完整 pytest 套件全绿（含本轮新增 8 个回归用例：`test_release_05138_fixes.py` 4 个 +
  `test_release_05138_ui.py` 4 个；另更新 `test_p0_pointplan.py` 与
  `verification/test_smith_mode_boxzoom.py` 各 1 处）。
- `py_compile` 与 `node --check` 全部通过。
- 冻结包构建与 `--smoke-test` 见发布记录。
