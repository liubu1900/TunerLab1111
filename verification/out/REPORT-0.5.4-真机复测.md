# TunerLab 0.5.4 — 真机复测与交付报告

日期：2026-11-09　机台：Zmotion 控制器 `192.168.0.11`（固件 2.9600）+ R&S ZNA26 矢网 `192.168.2.111:5025`
校准库：`C:\Users\Eoulu\Documents\TunerLab\calibrations\cal_20261109_181953.h5`（1152 点 / 5 GHz / 0 errors）
验证脚本：`verification/verify_live_057.py --with-motion`；结果数据 `verification/out/live_057.json`

---

## 一、结论

| 项目 | 结果 |
|---|---|
| 真机功能复测（真实 Qt 事件 + 真硬件） | **31 / 31 通过，0 失败** |
| 冻结包 `--smoke` | **连续 3 次 exit 0**（16 页全部构建） |
| 冻结包正常模式连真机启动 | 14 s 无 error / traceback / crash（124 = 预期超时终止） |
| 版本号 | `--version` → `TunerLab 0.5.4`；exe 文件资源 `0.5.4.0` |
| 台架状态 | 已恢复原始 DPOS `[279.4116, 3, 5, 1]`，`faulted=False`，四轴 idle、已回零 |
| 交付包 | `TunerLabApp/dist_058/TunerLab/TunerLab.exe` |

---

## 二、本轮修复与新增（0.5.4）

| # | 问题 / 需求 | 处理 | 关键文件 |
|---|---|---|---|
| 1 | 1152/1152 却显示 99%、1151/1152 且状态「已完成」 | `progress(index,total)` 是 0-based 索引，UI 当完成数用。新增权威计数 `progress_counts()` / `studio_task.completed` / `total_points`；「已完成」仅在 `done >= total` 时显示；有失败点显示「部分完成（有失败点）」 | `tuner/database.py`、`tuner/web/handlers.py`、`tunerlab_app/pages_studio.py` |
| 2 | 采集过程需要实时 Smith 图 | 运行页底部新增「实时 Smith 图（已采 Γ）」：按频率切片读 HDF5、青色点云 + 粉色最新点、频率下拉、刷新节流 | `tuner/database.py::live_smith_snapshot`、`GET /api/studio/live_smith`、`pages_studio.py` |
| 3 | 全软件 Smith 图需放大缩小看局部阻抗点 | 新增 `SmithView` 纯 Python 视图（zoom 1–60、以光标为锚缩放、中键 / Ctrl+左键平移、双击复位、逆变换点选）；深色 `SmithChart` 与浅色 `SmithPreview` 共用；Smith 页 / 目标图案 / 向导布点 / 运行页 / 旧 MainWindow 全覆盖 | `tuner/smith_view.py`、`tuner/ui.py`、`tunerlab_app/ui.py` 等 |
| 4 | 校准采集超时需重试 3 次再放弃 | 新增 `MotionTimeout` 子类；`CalibrationManager(motion_timeout_retries=3)`：仅超时清软件锁存后重试，限位 / 编码器不符 / 未回零等真故障立即中止 | `tuner/motion.py`、`tuner/zmotion_motion.py`、`tuner/calibration.py` |
| 5 | 桌面实机建库被 5 s 超时误杀 | `studio_bridge._init_real` 漏继承控制器 60 s；补上并把 `motion_timeout` 加入 `RUNTIME_POLICY_FIELDS`，使 5 s 旧库可用 60 s 续扫 | `tunerlab_app/studio_bridge.py`、`tuner/models.py` |
| 6 | 软件报「运动故障」但 Zmotion 上位机可正常移动 | `faulted` 是软件层锁存而非硬件故障字；健康且 idle 时自动清锁，Smith 页补「复位运动故障」按钮；真故障 / 急停仍拦截、仍要求回零 | `tunerlab_app/motion_adapter.py`、`tunerlab_app/pages_tune.py` |
| 7 | 版本号未随功能更新 | 升至 **0.5.4**（发布日期 2026-11-09），同步 About / 标题栏 / Windows 文件版本资源 / 发布校验脚本 | `tunerlab_app/version.py`、`packaging/version_info.txt`、`packaging/verify_release.py` |

---

## 三、真机复测明细（31 项）

### 硬件链路与环境

| 检查 | 结果 |
|---|---|
| 控制器直连 | `192.168.0.11` connected=True |
| 测试前四轴状态 | idle=[True, True, True, True] |
| 运动规划器 / 矢网 | `MotionPlanner` / `TCPVNA`（均为真实硬件） |
| 有效运动超时 | 60.0 s |
| 起始 / 结束 DPOS | `[279.4116, 3.0, 5.0, 1.0]`（未擅自回零） |

### 进度与实时 Smith

| 检查 | 结果 |
|---|---|
| 库文件与点数 | 1152 / 1152，`state=loaded` |
| `studio_task` 权威计数 | `completed=1152 total_points=1152` |
| 运行页计数器 / 进度条 / 状态 | `1152 / 1152` / `100%` / `已完成` |
| 实时 Γ 云 | 1152 点 @ 5.0000 GHz，数值有限、在单位圆内 |
| 最新点高亮 | index=1151，Γ≈`0.1892-0.0631j` |
| 频率下拉 / 图注 | 1 项、完整点数与操作提示正确 |

### 缩放、平移与点选（真实 Qt 事件）

| 检查 | 结果 |
|---|---|
| 滚轮缩放 | zoom `1.000 → 1.521`，徽标 `×1.5` |
| 光标锚点不漂移 | 缩放前后 Γ 误差 `1.11e-16` |
| 左键点选 Γ | 与期望值完全一致 |
| 中键拖拽平移 / 双击复位 | pan 生效；复位后 zoom=1.0、pan=(0,0) |
| 放大 / 缩小 / 复位按钮 | `1.300 → 1.000 → 1.000` |
| `SmithPreview` 滚轮 + 复位 | `1.322 → 1.000` |

### 受控运动与恢复

| 检查 | 结果 |
|---|---|
| 规划器与会话共用同一控制器 | True |
| 行程为短距离微调 | +2.000 mm（≤5 mm） |
| X 轴到位读回 | target=281.412 / readback=281.412 |
| 移动后真实矢网取数 | `measure()` 正常返回 |
| 回到起始位 / 无故障锁存 | delta=0.0000 mm，`faulted=False` |

---

## 四、冻结包验证（dist_058）

| 检查 | 结果 |
|---|---|
| PyInstaller 构建 | exit 0，onedir 布局，exe 9,703,391 B |
| `--version` | `TunerLab 0.5.4` |
| Windows 文件资源 | FileVersion / ProductVersion = `0.5.4.0` |
| `--smoke` ×3 | exit 0 / 0 / 0，均「16 pages built」 |
| 正常模式（连真机） | 启动日志仅 `TunerLab 0.5.4 started`，error 扫描 0 条 |
| 内嵌 PYZ 模块（947 个） | `tuner.smith_view`、`tuner.web.handlers`、`tuner.database`、`tuner.calibration`、`tuner.motion`、`tuner.zmotion_motion`、`tuner.ui`、`tunerlab_app.pages_studio`、`tunerlab_app.studio_bridge`、`tunerlab_app.motion_adapter`、`tunerlab_app.version` 全部入包 |

> `dist_057` 与 `dist_058` 的功能代码完全一致，`dist_058` 仅补齐版本号与文件版本资源；31 项真机复测在版本升级后再次跑通（0 失败），因此两包功能等价，现场统一用 **dist_058**。

---

## 五、已知遗留与现场注意

| 项 | 说明 / 建议 |
|---|---|
| `skrf` 未安装 | 核心套件 443 passed / 8 skipped / 1 failed，唯一失败是 `test_skrf_cascade_crosscheck` 缺依赖，与本次改动无关；需要时 `pip install scikit-rf` |
| Qt 字体警告 | 启动日志有 `QFontDatabase: Cannot find font directory .../PySide6/lib/fonts`。Qt6 不再自带字体，Windows 会回退系统字体，界面显示正常，非功能故障 |
| 控制器 502 端口 | 另有 Zmotion 上位机进程 `futureC`(PID 35200) 与 `RTSys`(PID 36300) 保持连接；属用户已打开的上位机软件，不影响 TunerLab，勿强杀 |
| 运动超时 | 桌面实机路径已继承 60 s；若现场改大行程 / 降速，请确认控制器 `motion_timeout` 足够覆盖最长单程（380 mm / 10 mm/s ≈ 38 s） |
| 实时 Smith 图 | 按频率切片显示；多频库需在下拉框切换频率。轮询有节流，不会反复全量读 HDF5 |
| 故障锁存 | 健康且 idle 时会自动清锁；若确属硬件故障 / 急停，仍需排除现场原因、回零后再运动 |

---

## 六、交付物

| 类型 | 路径 |
|---|---|
| 最终程序 | `TunerLabApp/dist_058/TunerLab/TunerLab.exe` |
| 真机结果数据 | `verification/out/live_057.json`（0.5.4 复测）、`verification/out/live_057_dist057.json`（dist_057 首测备份） |
| 运行页截图 | `verification/out/ui/10-采集运行-实时Smith图.png` |
| 验证脚本 | `verification/verify_live_057.py`、`verification/test_progress_and_smith_zoom.py`（8/8）、`verification/test_run_page_progress.py`（4/4）、`verification/test_timeout_retry.py`（4/4）、`verification/test_timeout_fix.py`、`verification/test_fault_latch.py`（3/3） |
| 回归 | 核心 443 passed / 8 skipped / 1 failed（环境）；桌面层 24 passed |

**建议现场操作**：关闭当前运行的旧版 `TunerLab.exe` → 用 `dist_058/TunerLab/TunerLab.exe` 启动 → 「系统 → 关于」确认为 0.5.4 → 初始化实机 → 加载 `cal_20261109_181953.h5` → 打开采集运行页核对 `1152 / 1152`、`100%`、`已完成` 与底部实时 Smith 图 → 用滚轮 / 中键 / 双击验证缩放平移。台架当前停在原 DPOS，可直接继续。
