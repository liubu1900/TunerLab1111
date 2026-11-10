# 用户操作手册

## 两个应用

| 命令 | 用途 | 是否需要硬件 |
|---|---|---|
| `python -m tuner.app` | **运动控制台**：通讯设置、轴移动控制、轴参数设定 | 需要 Zmotion 控制器 |
| `python -m tuner.ui` | 校准工作台：Mock 校准、Smith 图、阻抗调谐 | 不需要（Mock） |

两者共享同一份设置文件（`%APPDATA%\TunerLab\settings.json`）。

---

## 运动控制台（实机）

左侧 3 个页面。

### 1. 通讯设置

| 项目 | 说明 |
|---|---|
| 控制器 IP | 默认 `192.168.0.11`（Zmotion 出厂地址），可修改 |
| 连接 / 断开 | 建立或释放以太网链路 |
| 广播搜索 | 在本网段内广播查找控制器，不占用连接 |
| 固件版本 | 连接后自动读取（实测 `2.9600`） |
| SDK 库 | 显示 `vendor/zmotion/zauxdll.dll` 是否就位 |
| 设置文件 | 通讯设置的保存位置，可直接打开所在文件夹 |

「保存通讯设置」把 IP 和「启动时自动连接」写入设置文件，下次启动自动载入。

> **连不上时**：错误码 `20008` 表示"找不到控制器"。可能是地址写错、控制器没上电，
> 或者上位机不在同一网段（本机以太网口需为 `192.168.0.x`）。驱动会自动重试 3 次；
> 持续失败才判定为故障。

### 2. 轴移动控制

**实时状态**表每 300 ms 刷新一次：

| 列 | 含义 |
|---|---|
| 指令位置 DPOS | 控制器下发的目标位置（**控制器单位**） |
| 反馈位置 MPOS | 编码器实测位置（**编码器单位**） |
| 跟随误差 | DPOS − MPOS，两个标度不同，不要直接当 mm 读 |
| 空闲 | 控制器是否已停止运动 |
| 回零 | 该轴上次回零是否成功 |
| 状态码 | 控制器原始 `AXIS_STATUS` 位图 |

> ⚠️ **当前设备存在标度缺陷，务必先看。** 本机 `UNITS` 与编码器反馈标度不一致：
> 指令 `0.005` 在 X / Z1 / Z3 上实际走约 0.2–0.48，即**实际行程约为指令数字的
> 40–96 倍**。软件会检测到并**拒绝执行**这类移动（提示 "encoder feedback does not
> match the commanded displacement"）。这不是软件故障，是控制器 `UNITS` 需要按驱动
> 脉冲分辨率重新设定。修正前不要用本工具做生产运动。详见
> [`HARDWARE.md`](HARDWARE.md)。

**运动控制**区：选择轴、设定点动步长与绝对目标值，然后：

| 按钮 | 动作 |
|---|---|
| 点动 − / 点动 + | 按步长沿负/正方向移动，步长可设 0.0001–100 mm |
| 绝对定位 | 移动到输入的绝对位置（控制器单位） |
| 回零 | 对当前轴执行 `DATUM`，回零模式取自轴参数表 |
| 停止 | `CANCEL` 停止全部轴运动 |
| **标度自检** | 让当前轴走一个极小步长（0.005）再回到原位，测量「编码器位移 ÷ 指令位移」 |
| **急停 (EMERGENCY STOP)** | `RAPIDSTOP` 立即停止全部轴，并**锁定软件状态** |
| 复位急停 | 解除锁定。**复位后必须重新回零才能运动** |

**标度自检**是排查上述标度问题的工具：比值为 1.00 表示指令与反馈一致；比值远大于
1 说明 `UNITS` 与反馈标度不匹配，轴的实际行程是指令数字的该倍数。该比值可写入轴
参数表的「反馈标度 FEEDBACK_SCALE」，之后软件会按它校验移动是否到位。

> **各轴独立运动**：X 轴不再受探针位置约束，软件不会自动回缩探针，也不会因为
> 探针未回缩而拒绝 X 指令；X 回零也不再等待探针回零。移动 X 前请自行确认探针
> 与 DUT 已避让——这层保护现在完全由操作员和硬件联锁负责。若需要恢复旧联锁，
> 在构造配置时设 `TunerConfig(independent_axes=False)`。
>
> 「安全回缩全部 Z / 手动回缩全部 Z」按钮仍保留，但它是**手动工具**，不是自动前置步骤。
>
> **未回零的轴单独锁定**：某轴未回零时，只有该轴不能动，其余轴照常工作（当前
> Z2 处于此状态）。

### 3. 轴参数设定

四行四列的表格，每行对应一个轴：

| 参数 | 控制器参数 | 单位 | 说明 |
|---|---|---|---|
| 轴类型 | `ATYPE` | — | 4 = 带编码器反馈 |
| 脉冲当量 | `UNITS` | 脉冲/mm | 1 mm 对应的脉冲数 |
| 速度 | `SPEED` | mm/s | |
| 加速度 | `ACCEL` | mm/s² | |
| 减速度 | `DECEL` | mm/s² | |
| 回零爬行 | `CREEP` | mm/s | 回零接近原点时的速度 |
| 点动速度 | `JOG` | mm/s | 连续点动时的速度 |
| 脉冲模式 | `INVERT_STEP` | — | 0 = 脉冲+方向 |
| 原点输入口 | `DATUM_IN` | 通道号 | −1 = 不用原点开关 |
| 正向限位 | `FS_LIMIT` | mm | **出厂默认 ±200000000，需按实际行程填写** |
| 负向限位 | `RS_LIMIT` | mm | 同上 |
| 回零模式 | `DATUM_MODE` | — | 1 = 反向寻找原点开关 |
| 反馈标度 | `FEEDBACK_SCALE` | — | 编码器单位 ÷ 指令单位，用「标度自检」测得后填入 |

四个按钮的分工：

| 按钮 | 效果 |
|---|---|
| 从控制器读取 | 把控制器当前值填入表格（不写回） |
| 写入控制器 | 表格值下发到设备，**并**保存到设置文件 |
| 保存到文件 | 只更新本机设置文件，不下发设备 |
| 导出 / 导入 | 与其它机器交换参数（JSON） |

> **顺序很重要**：改完参数先「写入控制器」才真正生效；只点「保存到文件」的话，
> 设备仍在用旧参数，下次连接也不会自动下发。

---

## 校准工作台（Mock）

左侧 8 个页面，状态栏始终标明 Mock。无需连接任何仪器。

1. Hardware Control：选择双/三探针，设置固定探针间距，Initialize virtual hardware and home。示例坐标是 Mock mm，不是设备规格。Jog、Safe retract、Emergency stop 可单独验证。急停后 Reset and re-home。
2. Calibration Setup：设置 X 点数、每个 Z 点数和新的 HDF5 文件名。组合数 Nx×Nz^探针数。Create new calibration task。
3. Calibration Run：Run / resume 开始扫描。Pause 在当前点完成后暂停；再次 Run / resume 继续。Cancel 保留已采集点；需要继续时重新打开数据库任务。耗时操作在后台线程运行。
4. Calibration Database：打开已有 HDF5，查看元数据、模拟标记及有效点。要断点续扫，先初始化与数据库相同配置的 Mock 硬件。HDF5 和同名 SQLite 应一同保存。
5. Impedance Tuning：输入 GHz、R/X 和 |ΔΓ| 容差，搜索 5 个候选；选择表格行并移动。显示的是数据库预测；机械到位不是 RF 实测成功。独立验证动作会显示模拟测量误差。
6. Smith Chart Viewer：显示当前频率的有效数据库负载 Γ、目标与候选。GUI 默认匹配终端、无夹具；非匹配终端和夹具通过 Python API 传入 `GammaSearchEngine`。
7. Validation Report：执行 A/B 往返测量，输出全频段 S11/S21 复数重复性报告，可保存 JSON。确定性 Mock 的误差可能为零，这不是实机性能指标。

## 命令行验收

```bash
python -m tuner.demo --probes 2 --output my_run_2
python -m tuner.demo --probes 3 --output my_run_3
```
每次指定新的目录；已存在的数据库不会覆盖。输出 HDF5、SQLite 和 JSON 报告。示例分别有 45/135 个位置，19 个频率点。CLI 从数据库中选择一个已采集阻抗作闭环目标，因此精确命中验证的是系统链路，不代表任意阻抗都可达。

## Python API 示例

```python
from tuner.database import CalibrationDatabase
from tuner.search import GammaSearchEngine
from tuner.rf import RFNetworkEngine

data = CalibrationDatabase('examples/mock_2probe.h5').load()
search = GammaSearchEngine(data, z0=50, termination=0j)
target = RFNetworkEngine.z_to_gamma(25+25j, 50)
for candidate in search.search(5.5e9, target, k=5, tolerance=0.05):
    print(candidate.position, candidate.impedance, candidate.error, candidate.reachable)
proposal = search.optimize_local(5.5e9, target)  # 插值预测，需重新测量
```

`search_multi` 支持多频加权 RMS 目标。版本分叉用 `db.fork_version('new_version.h5','reason')`，原版本保持不变。新版本不等于新硬件测量，必须保留来源标记。

### 实机运动 API

```python
from tuner.axis_params import default_axis_params
from tuner.zmotion import ZmotionController
from tuner.zmotion_motion import ZmotionMotionController, build_tuner_config

params = default_axis_params()
link = ZmotionController()
link.open('192.168.0.11')
motion = ZmotionMotionController(build_tuner_config(params), link, params)
motion.initialize()                 # 连接并读取位置；不会自动回零
motion.move_axis_to('Z1', 0.10)      # 移动单个轴到绝对位置 (mm)
motion.retract()                     # 三个探针轴回缩到安全位
link.close()
```

## 排错与边界
- Qt 平台插件错误：确认桌面显示环境及系统 Qt/xcb 依赖；无头测试使用 `QT_QPA_PLATFORM=offscreen`，不可作为桌面显示方式。
- 坐标/配置不匹配：使用与数据库一致的轴数、间距、限位、Z0 和模拟模式。
- 失败点：保留错误和已完成点，排除原因后重新打开任务续扫。
- 实机通信：`docs/HARDWARE.md` 记录了实测参数与验证结论。当前 VNA 仍为 Mock，没有经实机认证的 VNA 型号预设。
- 急停后：必须「复位急停」并重新回零，软件才允许再次运动。软件急停不能替代硬件联锁。
