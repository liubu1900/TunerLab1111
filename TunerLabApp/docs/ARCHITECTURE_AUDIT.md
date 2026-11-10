# TunerLab v0.1.0 架构审计与功能核对

> 审计对象：`D:\TunerLab_v0.1.0-20261104\TunerLab_v0.1.0-20261104\TunerLab`
> 代码规模：57 个 `.py`（其中 `tuner/` 26 个、`tests/` 22 个、`vendor/` 1 个），约 18k 行；前端 3 个文件 1.8k 行；测试函数 238 个 / 22 个文件。
> 审计依据：`docs/` 全部 10 份文档 + 逐模块源码通读（非仅凭文档）。

---

## 0. 一句话结论

三条功能描述**方向是对的，但细节上有 1 处机制性偏差、1 处数据形态偏差、1 处与代码实际实现相反**；同时代码里还有大量描述未覆盖的能力（虚拟控制器、安全联锁、标度自检、Smith 图布点、二阶插值调谐、插值验证、数据库版本管理、操作日志、多厂商矢网、Web 控制台）。

更关键的是：**"校准"这个词在本项目里被用于四件完全不同的事**，混用会直接导致需求对不上：

| 口语里的"校准" | 代码里的实体 | 做什么 | 入口 |
|---|---|---|---|
| 校准位置（机械） | `home()` / `home_axis_safe()` | `DATUM` 找原点，建立机床坐标系 | 轴移动控制 → 回零 |
| 校准标度（计量） | `probe_axis_scale()` + `UNITS` 修正 | 测「编码器位移 ÷ 指令位移」，修 `UNITS` | 标度自检 → 采纳 |
| 校准 Tuner（RF） | `CalibrationManager` + `CalibrationDatabase` | 逐点移动 + 测 S 参数，建「位置 → S」库 | 建库 → 采集 |
| 反推位置（逆向） | `GammaSearchEngine` / `TunerResponseModel.tune` | 给定目标 Γ/Z，查库+插值求位置 | 阻抗调谐 |

你说的第 3 条其实横跨了第 3、4 两行，而"每次只校一个轴、其他轴抬高"这件事**代码里没有**。

---

## 1. 运行形态：三个入口，一套后端

| 入口 | 命令 | 形态 | 覆盖范围 | 是否需要硬件 |
|---|---|---|---|---|
| 运动控制台 | `python -m tuner.app` | PySide6 桌面，4 页 | 通讯 / 轴移动 / 轴参数 / 操作日志 | 需要（可选虚拟） |
| 校准工作台 | `python -m tuner.ui` | PySide6 桌面，8 页 | Mock 校准全流程 | 不需要（纯 Mock） |
| Web 控制台 | `python -m tuner.web` | `http.server` + 浏览器，12 页 | 上面两者的**并集** + 真实矢网 + Smith 布点 | 可选（真实/虚拟） |
| CLI 验收 | `python -m tuner.demo` | 命令行 | 无界面跑完 2/3 探针闭环 | 不需要 |

`run.bat` 只是一个 `choice` 菜单壳，最终仍是 `python -m ...`。**没有任何打包产物**：无 `dist/`、无 `.spec`、无安装器（`docs/TASKS.md` 最后一行明确写「Windows 独立 exe/安装器 未完成」）。

技术栈：Python ≥3.11、PySide6、NumPy/SciPy、h5py、SQLite（标准库）、原生 socket SCPI（**不是** PyVISA，`PyVISA` 只在 `vna.py` 的 legacy `PyVISAVNAController` 里用到，实际未启用）。

---

## 2. 分层与模块职责

### 2.1 契约层（纯数据/协议，无 IO）

| 模块 | 行数 | 职责 | 关键约束 |
|---|---|---|---|
| `models.py` | 51 | `TunerConfig` / `Measurement` / `SearchResult` | `probes∈{2,3}`；`axes=('X',Z1..Zn)`；`grid()` = X×Z^n 笛卡尔积；`validate_position()` 做软限位 |
| `motion.py` | 168 | `MotionController` 契约 + Mock 实现 + `MotionPlanner` | 共享 X 移动前后**所有探针必须在 safe_z**；急停锁存需复位+回零 |
| `vna.py` | 138 | `VNAController` 协议 + `MockVNAController` + legacy `PyVISAVNAController` | Mock 用 ABCD 级联：传输线段 + 并联电纳，`insertion²·f` 控反射 |
| `axis_params.py` | 237 | `AxisParams`（14 个字段）+ 型号定义 | `COORDINATE_FIELDS` 变更 ⇒ 回零失效 + 标定失效 |
| `settings.py` | 224 | JSON 持久化，schema 3 | `%APPDATA%\TunerLab\settings.json`，原子写（tmp+replace） |

### 2.2 实机层（唯一碰硬件的地方）

| 模块 | 行数 | 职责 |
|---|---|---|
| `zmotion.py` | 587 | ctypes 绑定 `zauxdll.dll`：`ZAux_OpenEth/Close/SearchEth/Execute/DirectCommand`、`Direct_Set/Get*`（14 组轴参数）、`Single_MoveAbs/Move/Cancel/Datum/Vmove`、`Rapidstop`、`GetAllAxisInfo`；错误码封装为 `ZmotionError`；连接 3 次重试（0.4s×n 退避） |
| `zmotion_motion.py` | 642 | 把上面适配成 `MotionController`：软限位、逐轴回零状态采纳、retract-before-X、**编码器校验**（MPOS 位移 vs DPOS 位移×feedback_scale，5%）、故障锁存、标度自检、UNITS 写入 |
| `zmotion_errors.py` | 275 | `AXISSTATUS` / `AXIS_STOPREASON` 位解码成中文，控制器错误码字典 |
| `mock_hardware.py` | 490 | `MockZmotionLink` + `MockZmotionController`：同契约虚拟控制器，可**注入 bit22 故障**、可生成初始位置 |
| `vna_drivers.py` | 416 | 裸 socket SCPI（5025），6 个厂商 profile（rsp / keysight_ena / keysight_pna / ceyear / anritsu / generic），`*IDN?` 自动识别，命令 trial 回退 |

### 2.3 采集与数据层

| 模块 | 行数 | 职责 |
|---|---|---|
| `calibration.py` | 72 | `CalibrationManager`：prepare/run/pause/resume/cancel；逐点 move→measure→write；**运动异常不重试直接中止**，采集异常重试 2 次 |
| `database.py` | 189 | HDF5 主库 + SQLite 边车；`valid` 位是提交标记；`append_positions()` 原子重写；`fork_version()` 派生版本 |
| `operation_log.py` | 209 | 线程安全环形缓冲（2000 条），9 个分类，可选 JSONL sink，`since_seq` 增量拉取 |
| `virtual_data.py` | 144 | 合成校准库（|Γ| 随探针深度、相位随 X），用于虚拟模式演示 |

### 2.4 RF / 算法层

| 模块 | 行数 | 职责 |
|---|---|---|
| `rf.py` | 48 | Z↔Γ、终端 Γ、Redheffer 级联、DUT 平面 Γ（含夹具） |
| `search.py` | 110 | 频点插值 S → Γ → KDTree 最近邻；多频加权 RMS；`optimize_local` 线性插值+SLSQP |
| `interp.py` | 558 | standard 方案：**二阶 Lagrange**、3^n 邻域、先 Z 后 X；张量网格 `TensorInterpolator` + 散点 `ScatteredInterpolator`；两段式调谐（general→gradient→细网格） |
| `pointplan.py` | 416 | Smith 图均匀布点（向日葵/同心环），95/181/361 档，覆盖度评估，缺口补点 |
| `tuner_validation.py` | 273 | 标准验证环：在标定点**之间**取点、实测、算误差矢量、分解幅度/相位误差、给出"建议重新标定" |
| `validation.py` / `tuning.py` | 49 / 49 | A→B→A 重复性报告；`TuningManager` 门面 |

### 2.5 界面层

| 模块 | 行数 | 职责 |
|---|---|---|
| `ui_hardware.py` | 1338 | `HardwareSession`（唯一持有 link + settings）+ 4 个页面；`_MotionTask` 把阻塞运动丢到工作线程，**停止/急停在 UI 线程同步下发** |
| `ui_theme.py` | 132 | 浅色主题 + 中英文字体分工 |
| `app.py` | 129 | 运动控制台外壳（左侧 210px 列表导航） |
| `ui.py` | 380 | Mock 校准台（深色 QSS，自带 8 页，**功能已被 Web 版超越**） |
| `web/state.py` | 172 | 三个独立锁：`hw_lock` / `hw_emergency_lock`（急停专用，避免被阻塞运动卡住）/ `studio_lock` |
| `web/handlers.py` | 1422 | 50 条路由的纯函数（`func(state, body) -> dict`），无 HTTP 依赖 |
| `web/server.py` | 161 | `ThreadingHTTPServer` + 手写路由 + 静态文件 + `{ok,data|error}` 信封 |

---

## 3. 关键调用链

```
① 连接
CommunicationPage.on_connect
  └─ HardwareSession.connect(ip, mode)
       ├─ real    : ZmotionController.open() → ZAux_OpenEth(UDP 广播, 重试3)
       │            → read_firmware('?VERSION') → ZmotionMotionController.initialize()
       │            → refresh_home_state()（采纳控制器 HOME_STATUS，不自动回零）
       └─ virtual : MockZmotionController.initialize() → home()

② 手动运动（含全部安全闸门）
AxisMotionPage.on_jog → _MotionTask(工作线程)
  └─ ZmotionMotionController.move_axis_to(axis, target)
       ├─ _require_ready(axes=[axis])   # 初始化/急停锁存/故障锁存/该轴已回零
       ├─ 软限位检查（AxisParams.fs/rs_limit）
       ├─ axis=='X' ⇒ 所有探针必须在 safe_z
       ├─ MOVEABS → wait_idle(timeout)
       ├─ |DPOS-target| ≤ 0.002                # 位置到位
       └─ |ΔMPOS − ΔDPOS×feedback_scale| ≤ max(0.002, 5%)
             └─ 失败 ⇒ stop_all + 故障锁存 + 清空回零状态

③ 校准采集（Web 实机路径，最完整）
studio_init_hardware(motion_source='real')
  ├─ 拒绝 feedback_scale≠1 的轴（标度未修正不允许扫）
  ├─ build_tuner_config(x_limits, z_limits, safe_z)  # 操作员申报行程，不用出厂 ±2e8
  └─ MotionPlanner(config, controller)
studio_setup → CalibrationDatabase.create()  # 'x' 独占创建，绝不覆盖
studio_run  → 线程: CalibrationManager.run()
              └─ 每点: planner.move_to() → vna.measure() → db.write_point()
                       └─ write_point: valid=False → 写 actual/s → valid=True（提交标记）

④ 反推位置（调谐）
studio_tune(R, X, f, method)
  ├─ target_Γ = z_to_gamma(Z, z0)
  ├─ search(): 频点插值 S → Γ → KDTree 取 k=5 近邻
  └─ method≠nearest: tune(two-stage)
        ├─ general: 最近标定点
        ├─ gradient: 二阶 Lagrange 插值面上做局部搜索
        └─ fine grid: 细网格再搜（standard 专利做法）
     ⇒ 返回 predicted=True，**必须实测才算数**
```

---

## 4. 三条描述的逐条核对

### 第 1 条：控制 Tuner 各轴移动

> 原描述：「调用 RTSys 软件指令实现」

| 项 | 实际实现 | 判定 |
|---|---|---|
| 通信方式 | `ctypes` 直接绑定官方 `zauxdll.dll`（64 位）的 C API，**不经过 RTSys 软件** | ❌ 偏差 |
| 指令语义 | `MOVEABS` / `MOVE` / `DATUM` / `VMOVE` / `CANCEL` / `RAPIDSTOP`，与 RTSys 里写的 Basic 指令**同一套运动内核** | ✅ 同源 |
| Basic 通道 | 仅用于 `?VERSION`、`?ERRCODE/?ERRMSG` 两条查询 | 部分 |
| 轴数 | 4 根物理轴固定（X=0/Z1=1/Z2=2/Z3=3），3 轴型只命令前 3 根 | ✅ |
| 安全 | 软限位、retract-before-X、编码器校验、逐轴回零、急停锁存 | ✅ 超出描述 |

**纠正**：准确说法是「通过厂商 SDK（`zauxdll.dll`）直接下发运动指令，指令集与 RTSys 中的 Basic 指令同源；不依赖 RTSys 进程，也不需要 RTSys 处于运行状态」。

### 第 2 条：连接矢网、设参数、自动采集

| 项 | 实际实现 | 判定 |
|---|---|---|
| 连接 | TCP/IP SCPI（默认 5025），**5 厂商 + 自动识别**，无 VISA 运行时 | ✅ 超出描述 |
| 可设参数 | 起始/终止频率、点数、功率(dBm)、中频带宽(Hz) | ✅ |
| 「步进」 | **没有独立步进参数**，`step=(stop−start)/(points−1)` 由点数推导 | ⚠️ 表述偏差 |
| 未覆盖参数 | 平均次数、扫描时间、通道/端口选择、二进制数据块、仪器端 SOLT/TRL 校准控制 | ❌ 缺失 |
| 自动采集 | 按布点逐点 `move → trigger → *OPC? → SDATA?` | ✅ |
| 数据落盘 | HDF5 里的 `s[N,F,2,2] complex` 复矩阵 + SQLite 任务表 | ✅ 但不是 S2P 文件 |
| `.s2p` / Touchstone | **全代码库无任何导出实现**（`grep -i s2p/touchstone` 只命中注释和一处错误字符串） | ❌ 缺失 |

**纠正**：采集的是**完整二端口 4 个 S 参数**，以 HDF5 张量 + SQLite 边车存储，可用于任意频点插值与调谐；但**不能导出成 `.s2p` 给外部工具**。如果工艺上要求交付 `.s2p`，这是必须补的功能（约 100 行：`skrf.Network` 或手写 Touchstone 1.0/2.0）。

### 第 3 条：校准 Tuner 位置、按库反推、逐轴校准

| 项 | 实际实现 | 判定 |
|---|---|---|
| 「根据已有库反推位置」 | `GammaSearchEngine.search()`（KDTree）+ `TunerResponseModel.tune()`（二阶插值） | ✅ 但这是**调谐**，不是校准 |
| 「按库校准」 | 校准是**正向**建库：已知位置 → 测 S → 存表；没有反向拟合 | ⚠️ 因果颠倒 |
| 「每次仅校准一个轴，其他轴抬高」 | **完全没有**。`TunerConfig.grid()` = X × Z1 × … × Zn 全组合；`CalibrationManager` 只按这个笛卡尔积逐点跑 | ❌ 缺失 |
| 其他轴「抬高」 | 只有 `safe_z` 退针（`retract()` / retract-before-X），语义是**安全避让**，不是校准隔离 | 部分 |
| 逐轴回零 | 有（`home_axis_safe`，且 X 必须在所有探针回零后才允许） | ✅ |

**纠正**：现在的校准是「全组合网格」，点数为 `Nx × Nz^探针数`（例：9×4³=576）。你说的「一次只动一个轴、其余轴退到参考位」是另一种标定策略——**单轴扫描标定**，用于分离探针间的相互耦合，代码里没有实现，需要新增：

- 布点方式：`X 扫描 × (Z1=ref, Z2=ref, …)` → 再 `Z1 扫描 × (X=ref, Z2=ref…)` → 依此类推；
- 每个子扫描单独记录「基准位」与「被扫轴」，否则后续插值无法判断哪些点是同一条曲线；
- 需要新增 `plan='per_axis'` 的布点器 + 数据库 metadata 标记 + 插值模型对"非张量网格"的退化处理（现有 `ScatteredInterpolator` 可承接，但精度需验证）。

---

## 5. 描述里没提、但代码里确实有的功能

| 类别 | 能力 | 位置 | 价值 |
|---|---|---|---|
| **双模式** | 真实 / 虚拟控制器切换，虚拟模式自动回零、可注入 bit22 故障、可生成合成校准库 | `settings.mode`、`mock_hardware.py`、`virtual_data.py` | 无硬件也能演练全流程、培训、演示 |
| **运动安全** | 共享 X 退针联锁、编码器实测校验、故障锁存、逐轴回零、软限位、急停需复位+回零 | `zmotion_motion.py` | 这是本项目最值钱的部分，防止撞针/刮片 |
| **标度自检** | 走 0.005 再回原位，测「编码器/指令」比值，反算正确 `UNITS` 并写回控制器 | `probe_axis_scale()`、`apply_scale_correction()` | 直接对应 HARDWARE.md 里那个"实际行程是 40–96 倍"的阻断缺陷 |
| **布点策略** | Smith 图均匀布点（95/181/361 三档，向日葵/同心环）、覆盖度评估、缺口补点追加到同一库 | `pointplan.py` | 比笛卡尔网格覆盖率高一档，点数还少 |
| **插值调谐** | 二阶 Lagrange（3^n 邻域）+ 两段式 + 细网格，报告每阶段误差 | `interp.py` | 行业通用方法，精度显著优于最近邻 |
| **插值验证** | 在标定点**之间**取点实测，算误差矢量并分解幅度/相位，超差建议重标 | `tuner_validation.py` | 唯一能证明"插值可信"的手段 |
| **重复性验证** | A→B→A 三次往返，全频段 S11/S21 复数统计 | `validation.py` | 机械重复定位精度证据 |
| **数据库治理** | 独占创建、提交标记、失败点重试、断点续扫、追加点原子重写、版本分叉（保留 lineage） | `database.py` | 长任务中断不用重跑 |
| **来源隔离** | `simulated` 标志贯穿 Measurement→DB→报告，Mock 与实测**永不混写** | 多处校验 | 防止把仿真当实测交付 |
| **操作日志** | 9 分类（通讯/运动/回零/参数/急停/标度/矢网/校准/系统）、2000 条环形、可导出 JSON、`since_seq` 增量 | `operation_log.py` | 出问题能回答"到底下发了什么" |
| **Web 远程** | 12 页、50 路由、300ms 轮询、局域网可访问、急停独立锁 | `web/` | 平板/另一台机器看状态 |
| **诊断** | AXISSTATUS/STOP_REASON 位解码、控制器错误码查询 | `zmotion_errors.py` | Z2 的 bit22 就是这么定位的 |

---

## 6. 已实现 vs 未完成（缺口清单）

| 项 | 状态 | 影响 | 出处 |
|---|---|---|---|
| 单轴逐轴标定 | **未实现** | 无法分离探针耦合；与你描述的第 3 条不符 | 本次审计 |
| `.s2p` / Touchstone 导出 | **未实现** | 数据出不了本软件 | grep 全库 |
| Windows 安装包 / exe | **未实现** | 现场部署要装 Python + 6 个包 | `TASKS.md` |
| 磁盘日志 | 未接线（`OperationLog()` 无 sink） | 进程崩溃后日志全丢 | `operation_log.py:209` |
| VNA 实机资格确认 | 未完成 | 命令集按 profile 猜，未经真机逐一验证 | `TASKS.md`、`VNA.md` |
| `UNITS`/编码器标度 | **阻断缺陷** | 指令 0.005 实走 0.2–0.48，软件目前**拒绝运动** | `HARDWARE.md` |
| Z2 轴 | 不可用（bit22 + 从未回零） | 4 轴型实际只能跑 3 根 | `HARDWARE.md` |
| 机械行程/方向 | 未标定 | `FS/RS_LIMIT` 仍是出厂 ±2e8 | `HARDWARE.md` |
| 二进制数据块 | 不支持 | ASCII 取数慢，401 点 4 参数约数秒 | `VNA.md` |
| 数据库整库入内存 | 已知限制 | 576 点 × 401 频点 ≈ 15 MB，尚可；再大要分块 | `TASKS.md` |
| Web 服务鉴权 | **无** | 若用 `--host 0.0.0.0` 暴露，局域网内任何人都能点"急停"/"运动" | `server.py:139` |
| `tuner/ui.py` | 功能已被 Web 版超越 | 三套 UI 三份功能，维护分叉 | 对比可见 |

---

## 7. 为什么"命令行使用"在现场撑不住

实测本机（Windows，Python 3.13.14）：`import numpy` 直接 `ModuleNotFoundError`，`PySide6` 同样缺失。也就是说**现在双击 `run.bat` 都起不来**，必须人工 `pip install -r` 一遍，还要保证 Python 在 PATH 上。

具体痛点：

1. **三个入口**：现场要记住 `-m tuner.app` / `-m tuner.ui` / `-m tuner.web` 哪个是哪个；`run.bat` 只是菜单，不是应用。
2. **依赖手工装**：numpy/scipy/h5py/PySide6 共约 300 MB，离线机器无法安装。
3. **无图标、无快捷方式、无单实例**：双击两次就有两个进程抢同一个 `zauxdll.dll` 句柄和同一个校准库。
4. **无崩溃兜底**：Qt 事件循环里一个未捕获异常，窗口直接消失，操作员不知道发生了什么；日志还在内存里，一起没。
5. **工作目录依赖**：`studio_setup` / `generate_data` 默认写 `Path.cwd()/calibrations`。装到 `Program Files` 后 cwd 不可写，**建库会直接失败**。
6. **无版本/关于/更新**：现场无法确认装的是哪一版、DLL 是不是对的。
7. **三套 UI 功能不一致**：桌面版没有真实矢网和 Smith 布点，Web 版才有；操作员被教会两套流程。

---

## 8. 目标形态（改造方向）

### 8.1 信息架构（单窗口，分组导航）

```
设备
  通讯设置        控制器 IP / 真实·虚拟 / tuner 型号 / 连接诊断
  轴移动控制      实时状态表 + 每轴独立面板 + 急停 / 标度自检
  轴参数设定      14 项参数表，读/写/存/导入导出
  矢网控制(VNA)   厂商/IP/扫频/功率/中频 → 单次扫描 + 四参数曲线
校准
  概览            连接态、库状态、有效点数、最近任务
  布点与设置      运动源(真实/模拟) / 矢网源 / 布点方式 / 点数预估 / 建库
  采集运行        进度、暂停、取消、逐点日志、失败点
  校准库          打开/元数据/有效点/续扫/版本分叉
  Smith 图        实测点云 + 目标 + 候选 + 布点预览
  阻抗调谐        R/X/f/容差 → 候选表 → 移动 → 实测验证
  验证报告        插值验证 + 重复性，误差矢量分解
记录
  操作日志        分类过滤 / 导出 / 清空
  设置与关于      版本、DLL 路径、数据目录、日志目录、日志级别
```

### 8.2 正常操作流（5 步，写在首次运行向导里）

```
① 连接控制器（真实/虚拟）→ 读固件、读回零状态
② 回零（探针先、X 后）→ 标度自检；比值≠1 则先修 UNITS
③ 申报行程（X/Z 上下限、safe_z）→ 建库（选布点方式，预估点数/耗时）
④ 采集（可暂停/取消/断点续扫）→ 自动加载库
⑤ 调谐 → 移动到候选 → 实测验证 → 导出报告
```

### 8.3 打包链路

| 阶段 | 工具 | 产物 |
|---|---|---|
| 冻结 | PyInstaller（onedir，windowed） | `dist\TunerLab\TunerLab.exe` + `_internal\` |
| 资源 | spec `datas` | `vendor/zmotion/*.dll`、`web/static/*`、图标 |
| 安装器 | Inno Setup 6 | `TunerLab-0.1.0-Setup.exe`（按用户安装、开始菜单、卸载器） |
| 数据目录 | 代码改造 | 配置/日志 → `%LOCALAPPDATA%\TunerLab`；校准库 → `文档\TunerLab\calibrations` |

> onedir 而非 onefile：`zauxdll.dll` 由 ctypes 在运行时加载，onefile 解包到临时目录后进程退出即删，DLL 句柄容易出问题；onedir 启动也快得多。

---

## 9. 审计结论

1. 你的三条描述抓住了主干（运动、矢网采集、位置↔阻抗），但把**正向建库**说成了"反推"，把**逐轴隔离标定**当成了已有功能——这两点会直接影响验收标准，必须先对齐。
2. 代码的真实成熟度**高于**描述：安全联锁、标度自检、插值调谐、插值验证、数据库治理都是描述里没有的资产。
3. 阻碍"当常规软件用"的不是功能，而是**交付形态**：三个命令行入口、手工装依赖、无安装包、无崩溃兜底、默认路径依赖 cwd。
4. 现场最大的技术风险仍是 `UNITS` 标度缺陷（软件已会拒绝运动，但没有物理测量就无法修）与 Z2 的 bit22；这两项不解决，4 轴 tuner 只能按 3 轴跑。

---

## 10. 实施说明（本次改造的落地位置）

原项目 `../TunerLab` **未改动**（仅两处试改已还原，见下）。所有新增代码在 `../TunerLabApp`：

| 内容 | 位置 |
|---|---|
| 单窗口桌面壳 | `tunerlab_app/shell.py`（13 页、三组导航、菜单/状态栏/单实例/崩溃兜底） |
| 设备页复用 | 直接 import `tuner.ui_hardware` 的 4 个页面，未重写 UI |
| 矢网/校准页 | `tunerlab_app/pages_vna.py`、`pages_studio.py`、`pages_tune.py`，薄客户端调用 `tuner.web.handlers` |
| 会话共享 | `SharedState(AppState)` 子类，覆盖 `.hw`，避免改 `web/state.py` |
| 打包 | `packaging/`：PyInstaller spec、Inno Setup 脚本、图标生成、`build.bat` |
| 说明 | `docs/DESKTOP_APP.md` |

> 唯一接触过原代码的动作：为验证可行性临时给 `web/state.py` 加过 `hw` 参数、给 `web/handlers.py` 换过默认路径；两处均已按原文恢复，现内容与原版一致（仅 mtime 变化）。
