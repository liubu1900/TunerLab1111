# 各轴独立运动：去掉 X 与探针的相互限制

> 日期：2026-11-07　需求：其他 Z 轴不在最高位置时 X 轴不能移动，该限制需要去掉；每个轴都没有相互限制和运动关联。

---

## 1. 结论

默认行为已改为**各轴独立**：

| 行为 | 改前 | 改后（默认） |
|---|---|---|
| X 轴移动 | 所有探针必须在 `safe_z`（且目标位置探针也在 `safe_z`），否则拒绝 | 不检查探针位置，直接下发 |
| 单轴 X 定位 / 点动 | 探针未回缩即拒绝 | 不检查探针位置 |
| 校准采集（`MotionPlanner`） | 自动分三段：回缩全部 Z → 平移 X → 插入探针 | **一次直接移动**，不再替 X 动探针 |
| X 回零 | 必须等所有探针回零 | 不等待，逐轴独立 |
| 手动「回缩全部 Z」 | 有 | 保留为手动工具，不再是自动前置步骤 |

**保留不变**（这些是单轴自身保护，不是轴间关联）：软限位、编码器反馈校验、位置回读、逐轴回零状态与故障锁存、急停锁存与复位、超时、`retract-before-X` 的旧逻辑（可开关恢复）。

---

## 2. 开关与回滚

`TunerConfig` 新增字段 `independent_axes: bool = True`。

| 取值 | 效果 |
|---|---|
| `True`（默认） | 各轴独立，无轴间限制 |
| `False` | 完整恢复旧的 retract-before-X 联锁、三段式规划、X 回零等待探针 |

```python
from tuner.models import TunerConfig
cfg = TunerConfig(independent_axes=False)          # 回滚到旧行为

from tuner.zmotion_motion import build_tuner_config
cfg = build_tuner_config(axes, independent_axes=False)
```

所有入口（运动控制台、校准工作台、Web 控制台、TunerLabApp 桌面壳、虚拟控制器）共用同一个配置类，行为一致。桌面壳的 `TunerMotionController` 继承 `ZmotionMotionController`，未覆盖 `move_to` / `move_axis_to`，因此自动生效。

> **0.3.6 修正**：0.3.5 漏掉了桌面壳自己的 OP 脉冲回零入口（`motion_adapter.py::home_via_output` 与 `pages_motion.py` 的回零包装），这两处曾硬编码保留「X 回零必须等探针回零」。0.3.6 已让它们同样受 `independent_axes` 控制，详见本文 §7。

> 该字段**不写入 HDF5 校准库元数据**（`database.create()` 主动剔除），所以新版本写的库仍能被旧版 exe 读取；反之旧库读入时取当前默认策略。避免现场新旧程序混用时 `TunerConfig(**meta['config'])` 报未知关键字。

---

## 3. 改动文件

| 文件 | 改动 |
|---|---|
| `TunerLab/tuner/models.py` | 新增 `independent_axes` 字段及说明 |
| `TunerLab/tuner/motion.py` | Mock `move_to` 联锁加开关；`MotionPlanner.move_to` 独立模式直接一次下发 |
| `TunerLab/tuner/zmotion_motion.py` | `move_to`、`move_axis_to`、`home_axis_safe` 的 X 联锁加开关；`home()` 去掉探针优先的顺序依赖；`build_tuner_config()` 增加同名参数 |
| `TunerLab/tuner/mock_hardware.py` | 虚拟控制器 `home_axis_safe` 的 X 依赖加开关 |
| `TunerLab/tuner/database.py` | 库元数据剔除 `independent_axes`，保持跨版本文件格式兼容 |
| `TunerLab/tuner/ui_hardware.py` | 安全提示文案、模块说明 |
| `TunerLab/tuner/web/static/index.html` | 按钮改为「手动回缩全部 Z」并补充提示 |
| `TunerLabApp/tunerlab_app/pages_about.py` | 「运行约束」改为独立轴与碰撞风险提示 |
| `TunerLab/README.md`、`TunerLab/docs/{SPEC,HARDWARE,USER_MANUAL,TASKS}.md` | 规格、安全规则、操作说明、清单同步 |
| `TunerLab/tests/*`、`TunerLabApp/docs/CHANGE_INDEPENDENT_AXES.md` | 旧联锁用例改为显式 `independent_axes=False` 回归；新增独立模式用例；本文档 |

> 注：`TunerLabApp/dist/` 下是**已冻结的历史构建产物**，其中的代码与文档副本不随源码改动。现场使用的 `TunerLab.exe` 必须重新打包才会生效。

---

## 4. 风险（必须读）

1. **防撞保护被移除**。原联锁的作用是防止探针在伸入状态下随 X  carriage 横移刮伤晶圆/探针。现在软件不再阻止，**物理避让完全依赖操作员确认和硬件联锁**。第一次按 X 移动前，请确认探针已抬到安全高度或 DUT 已移开。
2. 校准采集现在会把 X 与 Z 的位移**一次下发**（多轴同时运动），而不是「先回缩、平移、再插入」。路径与旧版不同，采集时间与机械负载也会不同；建议先在虚拟模式跑一遍全流程，再用低速、少量点位做实机验证。
3. 软件急停、软限位仍在，但**不替代硬件互锁**；`FS/RS_LIMIT` 出厂默认值、Z1 原点输入不翻转、Z2 不可用等既有实机问题不受本次改动影响（这是 2026-11-07 当时的状态；11-08 现场已逐项修复，见 §7）。
4. 若现场存在旧版 exe 与新版本并存：旧 exe 仍有联锁，新 exe 没有；两边操作同一台机器时行为不一致，建议统一升级。

---

## 5. 验证（2026-11-07，Windows / Python 3.13.14，`TunerLabApp/.venv`）

| 项 | 结果 |
|---|---|
| 全套 pytest（排除真机集成 + skrf 交叉验证） | **258 passed, 1 deselected, 0 failed** |
| 独立模式 | 探针伸入时 X 可移动；规划器只下发一次；`move_axis_to('X')` 不检查探针；X 可单独回零 |
| 旧联锁回归（`independent_axes=False`） | X 移动被拒绝；规划器仍分三段；X 回零仍要求探针先回零 |
| 单轴保护 | 软限位、急停锁存、编码器校验全部保留并通过 |
| 数据兼容 | 库元数据不含策略字段；`CalibrationManager` 能正常读取并使用自己刚建的库；`TuningManager` / UI 的配置比对同步归一化 |
| 桌面壳 | `TunerMotionController` 继承更新后的适配器，冒烟通过 |

**未通过/跳过的用例说明（均与本次改动无关）：**

- `tests/test_rf.py::test_skrf_cascade_crosscheck`：venv 未安装 `scikit-rf`（打包 spec 明确 `excludes` 它），环境依赖缺失。
- `tests/test_zmotion_hardware.py` 的 2 个既有用例：本机控制器可达，但现场 **Z1 未回零 / IN17 不翻转**，抛出 `home the axes before motion; not homed: Z1`，与联锁或编码器逻辑无关。这是 `docs/BUGFIX_AXIS_DIALOG.md` 记录的既有实机问题。
- 原真机联锁用例已**从该文件撤下**：它假设探针处于伸入状态，而现场探针可能已在 `safe_z`，coupled 检查会放行一次真实的 X 移动；联锁回归改由 `tests/test_zmotion.py` 的 FakeLibrary 单测确定性覆盖，不拿现场机器冒险。

> 首次全套运行（含真机文件）为 264 passed / 4 failed，其中 3 个来自上述真机状态、1 个来自 skrf 缺失；撤下危险用例并排除环境项后为 258 passed / 0 failed。

## 6. 交付形态

- 源码：上述文件（所有入口共用一套后端，行为一致）。
- 打包：版本升至 **0.3.5**（`tunerlab_app/version.py`、`packaging/version_info.txt`、`packaging/installer.iss`），产物 `dist/TunerLab-0.3.5/`；**不覆盖** `dist/TunerLab`、`TunerLab-0.3.3`、`TunerLab-0.3.4` 等现场旧产物，方便随时回退。
- 现场切换：用新目录里的 `TunerLab.exe`（或整个文件夹拷走）。旧 exe 仍带联锁，新旧不要混用同一台机器。
- 自检已通过：`TunerLab.exe --version` → `TunerLab 0.3.5`；`TunerLab.exe --smoke-test` → 13 页构建完成、`SafeAxisMotionPage` 就位、exit 0。新目录 `_internal/docs/app/CHANGE_INDEPENDENT_AXES.md` 已随包分发。

---

## 7. 0.3.6 后续修正（2026-11-08）：桌面壳残留的 X 回零联锁

**发现经过**：11-08 现场用 0.3.5 连真机，操作日志 09:54:29 出现
`MotionError: 先把探针轴回零，再回零 X：未回零 Z1`。这不是硬件问题，而是 0.3.5
改动时的漏网之鱼：核心库（`zmotion_motion.py` / `mock_hardware.py`）的该检查已加
`independent_axes` 开关，但桌面壳有两处**自己实现**的回零入口没跟上：

| 文件 | 位置 | 问题 |
|---|---|---|
| `TunerLabApp/tunerlab_app/motion_adapter.py` | `TunerMotionController.home_via_output()` | 硬编码 `if axis == 'X':` 检查探针是否回零 |
| `TunerLabApp/tunerlab_app/pages_motion.py` | 单轴回零包装 | 同样的前置检查，UI 层再拦一次 |

**修复**：两处统一改为 `if not <...>.config.independent_axes and axis == 'X':`。
即默认 `True` 时 X 可单独回零；把 `independent_axes=False` 时旧联锁完整恢复。

**验证（2026-11-08）**：

| 项 | 结果 |
|---|---|
| `compileall` | 通过 |
| 全套 pytest | **258 passed / 0 failed**（排除真机集成用例与未安装的 scikit-rf 交叉验证） |
| 行为验证（MagicMock，三探针均未回零） | 独立模式越过联锁、进入下游原点判据；`independent_axes=False` 仍被旧联锁拦住 |
| 实机状态交叉核实 | 四轴 `AXISSTATUS=0`、状态「正常」、全部已回零；IN16–19 在回零完成时均读到 ON |

**交付**：版本 **0.3.6**，产物 `dist/TunerLab-0.3.6/`。0.3.5 及更早的冻结目录保持原样不动。
> 0.3.5 的 exe 仍带这条联锁，现场请以 0.3.6 为准，不要新旧混用同一台机器。

### 附：11-08 实机核实结论（供验收对照）

同日核实了上一版「未完成清单」的硬件侧五条：Z1 原点 IN17 翻转、Z2 的 bit22/bit11、
机械行程与 UNITS 标度、命令-反馈比值与跟随误差 —— 前四项均有日志与实时状态表证据，
已解决；**硬件互锁 / 限位开关 / 急停接线属于物理验证，软件无法核实，仍需现场实测**。
`FS/RS_LIMIT` 已标定为 X 0–403 mm、Z1/Z2/Z3 0–17 mm；四轴 `feedback_scale=100`
（MPOS 为 10 µm 计数），与 `studio_bridge.VALID_FEEDBACK_SCALES` 门禁一致。
