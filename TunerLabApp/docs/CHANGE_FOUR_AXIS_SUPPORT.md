# 同时支持 3 轴与 4 轴机型

> 日期：2026-11-21　分支：`feature/0.5.13-move-only`（本改动随 **0.5.13.6** 发布）
> 需求：确认 0.5.13.5 是否支持 4 轴（X/Z1/Z2/Z3 → 物理轴 0/1/2/3）；若不支持则调整，使其同时支持 3 轴和 4 轴。

---

## 1. 结论

改前：**不支持**。数据层虽然保留了 4 轴定义（`TUNER_MODELS[4]`），但操作界面只提供 3 轴机型——
`VISIBLE_TUNER_MODELS = (3,)`，桌面通讯页还会把旧的 4 轴配置强制归一到 3 轴，Web 页隐藏了 4 轴选项。
更关键的是，旧实现里"切换轴数"只改单台 Tuner 的 `axis_count`，会让四轴的 Z3（物理轴 3）
与双台三轴里右台的 X 重叠，无法安全共存。

改后：**3 轴与 4 轴同时支持，可随时切换**。两个机型是两套互斥的设备组合：

| 机型 | 设备组合 | 逻辑轴 → 物理轴号 | 探针数 |
|---|---|---|---|
| 3 轴 | 双台三轴 | 左 X/Z1/Z2 = 0/1/2；右 X/Z1/Z2 = 3/4/5 | 2 |
| 4 轴 | 单台四轴 | X/Z1/Z2/Z3 = 0/1/2/3 | 3 |

四轴的 Z3 占用第二台三轴的 X（都是物理轴 3），因此现场只能接其中一种；切换机型会**整组替换**
设备组合（不是只改某台 Tuner 的轴数），两套组合的接线与各轴参数分别保留，切回不用重新标定。

新装默认机型为 **3 轴**（现场的双台三轴接线）；这与改前"数据层默认 4 轴、UI 再强制归一到 3"的
矛盾状态不同，现在默认值与现场接线一致。

---

## 2. 改动清单

### 后端（`TunerLab/tuner/`）
- `axis_params.py`：`VISIBLE_TUNER_MODELS = (3, 4)`；新增 `DEFAULT_ACTIVE_AXIS_COUNT = 3`（新装默认机型，
  与 `DEFAULT_AXIS_COUNT = 4` 的"控制器全集"语义区分开）。
- `tuner_units.py`：新增 `INSTALLATIONS` / `DEFAULT_UNIT_BY_MODEL` / `INSTALLATION_LABELS` / `units_for_model()` /
  `installation_label()`，集中定义"机型 → 设备组合"的映射。
- `settings.py`：设置文件 schema **5 → 6**，新增 `units_by_model` 保存两套组合的接线；
  新增 `set_model()`（切换整机组合），`set_unit_axis_count()` 保留为兼容别名；
  `from_dict()` 兼容旧文件（无 `units_by_model` 时按机型生成默认组合）。

### 界面
- 桌面 `TunerLabApp/tunerlab_app/pages_comm.py`：型号下拉提供 3/4 两项；去掉"归一到 3 轴"逻辑；
  切换后重建当前 Tuner 下拉（3 轴两台 ↔ 4 轴一台）并给出提示。
- Web `TunerLab/tuner/web/static/index.html` / `app.js`：型号下拉提供 3/4 两项并说明物理轴映射；
  切换后同步重建 Tuner 下拉。
- `tuner/ui_hardware.py`（底层 UI）本就用 `sorted(TUNER_MODELS)` 同时提供两种，无需改动。

### 测试与文档
- 新增 `TunerLab/tests/test_axis_configuration.py`：8 个用例覆盖机型映射、轴号 0/1/2/3、
  切换往返、参数独立保留、旧文件迁移、schema 6 持久化。
- 更新 `test_library_axis_display.py`（3/4 选项、切换重建下拉）与 `test_tuner_models.py`
  （默认 3 轴）中断言旧行为的用例。
- 更新 `TunerLab/README.md`、`TunerLab/docs/operation_manual.html` 的机型说明。

---

## 3. 验收要点

- [x] 4 轴机型映射为 X=0、Z1=1、Z2=2、Z3=3（`units_for_model(4)` 与 `set_model(4)` 均验证）。
- [x] 3 轴机型仍为左 0/1/2、右 3/4/5，切换回 3 轴可恢复双台组合。
- [x] 3/4 轴切换后各 Tuner 参数独立保留（含 Z3 的 UNITS）。
- [x] settings schema 6 往返保存/加载保持两套组合；schema ≤5 旧文件可迁移并可升到 4 轴。
- [x] 桌面/Web 型号下拉同时提供两项；桌面切换后 Tuner 下拉从两台变一台（4 轴）。
- [x] 所有改动 Python 文件通过语法编译；新增回归用例 8/8 通过。

> 说明：本环境无法安装 numpy / PySide6 等依赖（pip 源不可用），完整 pytest 套件未能运行；
> 上述验证通过仅依赖标准库的打桩脚本完成，覆盖全部轴配置相关逻辑。建议在带依赖的开发机上
> 补跑 `python -m pytest TunerLab/tests TunerLabApp/tests -q` 做最终回归。
