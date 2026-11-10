# TunerLab 桌面应用（TunerLabApp）

> 本目录是**新增**的桌面应用壳，原有项目 `../TunerLab` 保持**只读、零改动**。
> 所有页面都复用原项目的后端（`tuner.*`），没有复制任何一份运动控制、校准或 RF 代码。

```
<workspace>/
├─ TunerLab/                 原项目（本次未修改任何一行）
│   ├─ tuner/                后端：Zmotion、VNA、校准、数据库、RF、Web
│   ├─ vendor/zmotion/       厂商 SDK（zauxdll.dll）
│   └─ docs/                 原文档
└─ TunerLabApp/              ← 本次新增
    ├─ tunerlab_app/         单窗口桌面应用
    ├─ packaging/            PyInstaller + Inno Setup + 图标
    ├─ docs/                 架构审计、本文件
    ├─ requirements.txt
    └─ .venv/
```

---

## 1. 为什么单独开目录

| 原因 | 说明 |
|---|---|
| 回滚成本 | 现场机器上原项目还能用 `python -m tuner.app` 跑，新壳出问题直接不用即可 |
| 升级隔离 | 原项目继续修 bug／加单轴标定，不影响壳的打包产物 |
| 审计清晰 | `git status` 里原目录干净，改动范围一眼可见 |

实现方式：新包用**绝对导入**引用原项目（`from tuner.ui_hardware import ...`）。
唯一需要“绕”的是 `AppState`——原 `web/state.py` 的构造函数会自建一个
`HardwareSession`，而桌面壳必须让自己的页面和外层共用同一个会话。处理办法是
**子类覆盖**，不改原文件：

```python
# tunerlab_app/shell.py
class SharedState(AppState):
    def __init__(self, hw):
        super().__init__()
        self.hw = hw          # 换成桌面壳已持有的会话
```

> 副作用：`AppState.__init__` 里那个临时 `HardwareSession` 会被创建又丢弃。它只读设置文件、不连硬件，代价可忽略。

---

## 2. 应用结构

单窗口、左侧三组导航、右侧页面栈，13 个页面：

```
设备                          校准                          记录
  通讯设置                      概览                          操作日志
  轴移动控制                    布点与设置                     设置与关于
  轴参数设定                    采集运行
  矢网控制 (VNA)                校准库
                                Smith 图
                                阻抗调谐
                                验证报告
```

页面实现分两类：

| 类型 | 来源 | 说明 |
|---|---|---|
| 设备 3 页 + 操作日志 | **直接复用** `tuner.ui_hardware` 的 `CommunicationPage` / `AxisParameterPage` / `OperationLogPage` | 安全联锁、300 ms 轮询、后台运动线程全部原样 |
| 轴移动页 | `tunerlab_app/pages_motion.py` 的 `SafeAxisMotionPage`（继承原 `AxisMotionPage`） | 只改回零超时、行程校验、故障复位与弹窗文案；布局与运动流程不动，详见 `docs/BUGFIX_AXIS_DIALOG.md` |
| 矢网 + 校准 7 页 | **薄客户端**，调用 `tuner.web.handlers` 的纯函数 | handler 签名是 `(state, body) -> dict`，与 HTTP 无关，可直接在 Qt 里调用，避免了第二套校准实现 |

阻塞调用（连控制器、VNA 扫描、移动到位）统一走 `bg.run_in_background`，结果用
Qt 队列信号回到 GUI 线程；**停止／急停仍是同步下发**，保证任何情况下都能抢到。

---

## 3. 运行

```bash
cd TunerLabApp
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m tunerlab_app
```

也可以用环境变量指定 Python 后双击 `packaging\build.bat`。

首次启动：

1. 左侧「通讯设置」选真实/虚拟控制器，填 IP，点连接；
2. 回零 → 标度自检（比值不为 1 先修 `UNITS`）；
3. 「布点与设置」申报行程、选布点方式、建库；
4. 「采集运行」开始，可暂停/取消；
5. 「Smith 图 / 阻抗调谐 / 验证报告」。

---

## 4. 打包与安装

```bat
packaging\build.bat
```

三步：

| 步骤 | 产物 |
|---|---|
| 生成图标 | `packaging\tunerlab.ico`（纯 Python 画，无 Pillow 依赖） |
| PyInstaller onedir | `dist\TunerLab\TunerLab.exe` + `_internal\`（约 211 MB） |
| Inno Setup（可选） | `dist\installer\TunerLab-0.2.0-Setup.exe` |

### 4.1 三种安装方式

| 方式 | 做法 | 适用 |
|---|---|---|
| **免安装** | 直接运行 `dist\TunerLab\TunerLab.exe`（整个文件夹一起拷走即可） | 临时机器、U 盘 |
| **install.cmd**（已可用） | 把 `dist\TunerLab` 整个目录拷到目标机，双击 `install.cmd` | 无 Inno Setup、无管理员权限 |
| **Setup.exe**（需 Inno） | 安装 Inno Setup 6，执行 `ISCC packaging\installer.iss` | 正式发布 |

`install.cmd` 的行为：robocopy 到 `%LOCALAPPDATA%\Programs\TunerLab` → 建开始菜单/桌面快捷方式 →
在「应用和功能」里注册卸载项 → 启动程序。目标机**不需要管理员权限**，也不需要 Python。

> 本机没有安装 Inno Setup，所以 `TunerLab-0.2.0-Setup.exe` 尚未生成；`.iss` 已写好，
> 装好 ISCC 后 `packaging\build.bat` 会自动编译出安装包。

> 为什么不用 onefile：`zauxdll.dll` 由 `ctypes` 按路径加载，onefile 解包目录随进程
> 退出被删除，DLL 路径不稳定且启动慢。onedir 是这类仪器控制程序的常规选择。

### 4.2 安装自检

```bat
TunerLab.exe --version      :: 打印版本后退出
TunerLab.exe --smoke-test   :: 建完 13 个页面、跑 1.2 s 事件循环后退出，返回 0
```

`--smoke-test` 是给现场用的：装完机不用人点，跑一条命令就能确认「能启动、页面能建、没崩」。

---

## 5. 数据放在哪

| 内容 | 位置 |
|---|---|
| 设置（IP、型号、轴参数） | `%APPDATA%\TunerLab\settings.json`（沿用原逻辑） |
| 运行日志 | `%LOCALAPPDATA%\TunerLab\logs\tunerlab.log`（5 MB × 5 轮转） |
| 操作审计 | `%LOCALAPPDATA%\TunerLab\logs\operations.jsonl` |
| 校准库 | `文档\TunerLab\calibrations\*.h5` + 同名 `.sqlite` |
| 报告 | `文档\TunerLab\reports\*.json` |

原代码默认把库写到 `Path.cwd()/calibrations`，装成应用后 cwd 可能是只读的
`Program Files`，建库会直接失败。新壳的所有入口都显式传绝对路径，**不改原代码**。

---

## 6. 与旧入口的关系

| 入口 | 状态 | 用途 |
|---|---|---|
| `python -m tunerlab_app` | **新**，推荐 | 现场操作 |
| `TunerLab.exe`（安装包） | **新** | 现场双击 |
| `python -m tuner.app` | 保留 | 只做运动控制台，脚本化调试 |
| `python -m tuner.web` | 保留 | 平板／另一台机器远程看状态 |
| `python -m tuner.ui` | 保留但过时 | Mock 演示，功能已被 Web/新壳覆盖 |

---

## 7. 已验证 / 未验证

已验证（2026-11-04，本机 Windows + Python 3.13.14 + PySide6 6.9.3）：

> v0.2.4 修复快速连点闪退（工作线程改控件）并把回零判据改为原点输入；v0.2.3 修正 MPOS/DPOS 单位口径（10 µm vs mm）与 OP 输出口回零；v0.2.2 修了回零方向、回零清零、状态表格与面板挤压；v0.2.1 修了弹窗缺陷（回零超时误报、软限位缺失、故障无法复位），
> 复现证据与修复细节见 `docs/BUGFIX_AXIS_DIALOG.md`。

| 项 | 方法 | 结果 |
|---|---|---|
| 全部页面可构建 | `MainWindow()` 离屏 | 13 页全部创建 |
| 端点流程 | 模拟运动 + 模拟矢网，3×2³=24 点建库 | 24/24 有效 |
| 调谐链路 | 10 GHz，25+25j，两段式 | general 0.335 → gradient 1e-6 |
| Smith 点云 | 加载后刷新 | 24 点 |
| 后台任务/信号 | 建库→采集→调谐全走 UI 按钮 | 无阻塞、无异常 |
| 单实例 | 启动第二个进程 | 立即退出，未开窗 |
| 冻结版启动 | `TunerLab.exe --smoke-test` | exit 0，13 页，运动页为 `SafeAxisMotionPage` |
| 真机回零/限位 | 点 Z1 回零 | 复现「5 s 误报超时、轴仍走」；已修复并改造 |
| 安装脚本 | `install.cmd` 装到临时目录再跑 | exit 0 |

未验证（需要真机）：

| 项 | 原因 |
|---|---|
| 真实 Zmotion 控制器连接/回零/运动 | 本机无控制器（`192.168.0.11` 不可达） |
| 真实 VNA 连接与取数 | 本机无矢网 |
| 打包版加载 `zauxdll.dll` | 已随包分发，但需实机确认 `TunerLab.exe` 能打开链路 |
| Win10 最低版本 | 仅在当前 Win11/10 环境验证 |

---

## 8. 还没做的（按优先级）

1. **逐轴标定**：现有校准是 `X × Z1 × … × Zn` 全组合；「一次只扫一个轴、其余退到参考位」
   需要新增布点器 + 数据库标记 + 插值退化处理（详见 `docs/ARCHITECTURE_AUDIT.md` §4）。
2. **`.s2p` 导出**：HDF5 里是完整 `N×F×2×2` 复矩阵，但没有 Touchstone 导出。
3. **正式安装器**：装 Inno Setup 6 后 `build.bat` 可直接产出 `Setup.exe`；当前用 `install.cmd` 顶上。
4. **崩溃转储**：现在未捕获异常会记日志并弹窗；若要 `minidump` 需接 `faulthandler` + WER。
5. **代码签名**：安装器与 exe 均未签名，域环境可能触发 SmartScreen。

---

## 10. 设备页改版（0.2.6，方案 5「仪表盘」）

采用方案 5 的视觉语言，但导航保持原结构：**设备 / 校准 / 记录 为一级**，其下四个设备页为二级，
统一在左侧导航栏内展开（白色侧栏 + 蓝色选中态 + 左侧 3 px 强调条）。

| 项 | 实现 |
|---|---|
| 主题 | `tunerlab_app/theme.py` 重写：浅色仪表盘，卡片圆角 12 px、KPI 瓷砖、8 px 圆角输入框与按钮 |
| 字体 | Noto Sans SC 单一字族，300/400/500/600/700 五档字重（`tunerlab_app/fonts.py` 在 `__init__` 中改写 `tuner.ui_theme`） |
| 组件 | 新增 `tunerlab_app/ui.py`：`KpiTile` / `KpiRow` / `Card` |
| 通讯设置 | 顶部 4 个 KPI（控制器状态 / 固件 / 地址 / 启动自动连接），勾选即存盘 |
| 轴移动 | KPI（已回零轴数 / 运动状态 / 申报行程 / 控制模式）+ 状态表 + 四轴面板 + 全局操作 |
| 轴参数 | KPI（轴数 / 参数项 / 回零模式 / 反馈标度）+ 工具条 + 12 列参数表（按内容定宽，可横向滚动） |
| 矢网 | KPI（仪器状态 / 厂商 / 点数 / 步进）+ 可滚动控制栏 + 2×2 四条独立迹线 + 数据表 |

### 10.1 布局修整记录

| 现象 | 原因 | 处理 |
|---|---|---|
| 状态表最后一行被“原点输入”压住 | 主题给单元格加了 padding，行高从 28 变成 40 | 行高固定 28，表高按实际行高重算 |
| 状态表下方出现残影文字 | 基类的 9 个标签已从布局移除但仍是子控件，继续绘制 | 建表后清理状态卡内非本页控件 |
| 轴面板按钮被裁 | 面板高度按旧主题定值 | 面板最小高 356，进度条 92→72 |
| 矢网左栏输入框被裁 | QSplitter 把 372 px 压到 265 px | 改为固定 376 px 控制栏 + 纵向滚动区 |
| 参数表表头被截断（“脉冲当量 UNI…”） | 13 列强制 Stretch | 改为按内容定宽 + 末列拉伸 |

四页截图见 `design/shots/app-*.png`。

---

## 11. 0.3.2 设备页四项修复

| 现象 | 根因 | 修复 |
|---|---|---|
| 「启动时自动连接」开关只剩一段圆弧、点击像没反应 | 开关用 QSS `image:` 引用 2× PNG，Qt 不读图片的 device pixel ratio，在缩放桌面下把 68×38 绘制进 34×19 的指示器框 | 新增自绘 `ToggleSwitch`（34×19、轨道 #dbe4f0、on 蓝紫渐变、白钮 200 ms 缓动）；整行（开关 + 文案）可点，勾选即写盘 |
| 控制器已连接，顶栏仍显示「未连接」 | `_sync_top_badge()` 只在窗口构造时调用一次，`connection_changed` 只更新了隐藏的 pill | `_on_connection` 内同步刷新；顶栏拆成「控制器 / 矢网」两枚徽标，连接时显示各自 IP，tooltip 给出固件或 IDN |
| 鼠标悬停只在一个 Trace 上出竖线 | 每个 `TracePlot` 各自保存光标位置，互不通信 | `TracePlot.cursor_moved(Hz)` → VNA 页广播到四图；各图按自身曲线取最近点显示读数，60 ms 防抖避免跨图闪烁 |
| 底部操作栏带「全局操作」组标题，急停位置/宽度不符 | 基类 `QGroupBox('全局操作')` 保留标题带；按钮顺序为停止在前 | 标题清空、`#OpsBar` 去掉上方 18 px 标题带；顺序改为急停 → 停止 → 复位急停 → 复位运动故障，急停 196 px 并标注 `EMERGENCY STOP` |
| 点「标度自检」弹 `Internal C++ object (QLabel) already deleted` | `_rebuild_page_layout` 把基类创建的 `scale_label` / `apply_scale_button` 当多余控件 `deleteLater()`，而 `_apply_scale_probe` 仍持有引用 | 新增 `#NoticeBar` 结果卡，`setParent` 重新挂载这两个控件并加入重建白名单；结果出现时显示卡片并 `ensureWidgetVisible` 滚入视野（0.3.3） |
| 布点与设置点「初始化并建立校准库」报「X, Z1, Z2, Z3 的 UNITS 与反馈标度不一致」，但标度自检比值正常 | `handlers.studio_init_hardware` 的实机门禁硬编码 `abs(feedback_scale - 1.0) > 0.05`；本机 MPOS 为 10 µm 计数、DPOS 为 mm，正确值就是 100，门禁把正确状态判成错误 | 新增 `tunerlab_app/studio_bridge.py`，只替换 real 分支：门禁改为接受 `1.0` 或 `100`（两种 MPOS 单位），其余逻辑（申报行程、已回零轴、单一控制器句柄、VNA 选择）与原 handler 一致；mock 分支仍直接转发（0.3.4） |

截图：`design/shots/app-comm.png`、`app-comm-connected.png`、`app-motion.png`、`app-vna-cursor.png`；
逐页对比：`design/ui-compare.html`。生成脚本：`diagnostics/capture_pages.py`。

---

## 12. 0.3.5 各轴独立运动

需求：其他 Z 轴不在最高位置时 X 轴不能移动，该限制去掉；每个轴都没有相互限制和运动关联。

| 项 | 实现 |
|---|---|
| 运动策略 | `TunerConfig.independent_axes` 默认 `True`：X 不再检查探针位置，`MotionPlanner` 一次直接下发，X 回零不等待探针 |
| 回滚 | `independent_axes=False` 完整恢复 retract-before-X 联锁与三段式规划 |
| 保留 | 软限位、编码器校验、逐轴回零状态、故障/急停锁存、超时；手动回缩 Z 按钮 |
| 兼容 | 策略字段不写入 HDF5 元数据，库比对统一走 `tuner.models.comparable_config_dict`，旧版程序仍可读新库 |
| 文案 | 轴移动页、关于页「运行约束」、Web 页面、README 与 4 份 docs 同步；明确写出碰撞避让责任转移 |
| 验证 | 258 passed / 0 failed（排除真机与 skrf 环境项），详见 `docs/CHANGE_INDEPENDENT_AXES.md` |

> 风险：软件不再阻止探针伸入时的 X 横移。物理避让现在由操作员和硬件联锁负责，
> 首次移动 X 前必须确认探针已抬离 DUT。旧 exe 仍带联锁，不要与新版混用。

改动的完整清单、开关用法与跨版本注意事项见 `docs/CHANGE_INDEPENDENT_AXES.md`。

---

## 13. 0.3.6 桌面壳残留联锁修正与实机首批 SOP

0.3.5 漏改了桌面壳自己实现的两处 OP 脉冲回零入口（`motion_adapter.py::home_via_output`、
`pages_motion.py` 的回零包装），它们硬编码保留「X 回零必须等所有探针回零」。
11-08 现场 09:54:29 实际操作日志里触发了
`MotionError: 先把探针轴回零，再回零 X：未回零 Z1`，与「各轴独立」需求冲突。

| 项 | 0.3.6 |
|---|---|
| 修复 | 两处改为 `if not config.independent_axes and axis == 'X':`，默认 `True` 时 X 可单独回零，`False` 完整回滚 |
| 版本 | `0.3.6`（`version.py` / `version_info.txt` / `installer.iss`），产物 `dist/TunerLab-0.3.6/` |
| 验证 | compileall 通过；258 passed / 0 failed；MagicMock 行为验证独立模式放行、旧模式仍拦截；`--version` / `--smoke-test` |
| 实机核实 | 11-08 四轴 `AXISSTATUS=0`、全部已回零、IN16–19 回零时均 ON；Z1 原点、Z2 bit22/bit11、软限位与 UNITS 标度、命令-反馈比值均已解决；**硬件互锁/急停接线仍需现场实测** |

**新增随包文档**：`docs/SOP_FIRST_REAL_CALIBRATION.md` —— 实机首批校准操作清单
（切真实源 → 建库 → 采 24 点 → 验收），含现场实测参数速查与常见报错对照。

> 0.3.5 及更早目录保持原样，可随时回退；现场请统一用 0.3.6，不要新旧混用同一台机器。

---

## 14. 0.3.7 单频点校准

11-08 现场 10:37 建库报
`ValueError: frequencies must be finite, positive and strictly increasing`。
根因是「起始 GHz = 终止 GHz = 10.0000、频点 4」：`np.linspace(10e9, 10e9, 4)`
生成 4 个相同频率，违反 `CalibrationDatabase.create()` 的严格递增校验。
现场实际需求是**只在 10 GHz 一个频点做 Tuner 校准**，而 0.3.6 的「频点」
控件最小值是 2，起止填成一样就必然失败。

| 项 | 0.3.7 |
|---|---|
| 驱动 | `TCPVNA.set_sweep(points=1)`：`STAR=STOP=起始频率`、`SWE:POIN 1`、本地网格 `[start]`；`_fetch_trace` 在单点模式兼容仪器仍返回多采样 |
| 前置校验 | 多点扫描若 `start >= stop` 或频率非正，在**下发 SCPI 之前**抛 `ValueError`；Studio 页面另给中文弹窗与「把频点设为 1」的指引 |
| UI | Studio「频点」最小值 `2 → 1`，页面新增单行说明；Web 端两个频点输入加 `min="1"`；`vna_set_sweep` / `vna_connect` 单点时把 `stop_hz` 对齐到 `start_hz` |
| 兼容 | 数据库 `(点数, 1, 2, 2)`、`TunerResponseModel.frequency_slice`、`np.interp`、Smith / 验证报告均按单频点处理，无需改库格式 |
| 验证 | 263 passed / 0 failed（新增 5 个单点用例）；compileall；UI 前置校验与模型单点行为自检通过 |

配置示例（10 GHz 单点）：起始 `10.0000`、终止 `10.0000`、**频点 `1`**。
详见 `docs/SOP_FIRST_REAL_CALIBRATION.md` §2.1 与常见报错对照表。


---

## 15. 0.3.8 网格量化误报与跨重启续扫

11-08 首次真实源采集（5 GHz 单点、X 9 × Z 4³ = 576 点）每走过一个 Z 网格点就
锁存一次运动故障，界面 `failed`。以 `cal_20261108_105400.h5` 为例：

```
Z2 encoder feedback does not match the commanded displacement:
commanded 0.0000, encoder moved 0.0000 (expected 0.0033 at feedback scale 100).
```

**根因是软件校验，不是硬件或标度问题**：

1. 网格用 `np.linspace(0, 1, 4)` 得到 `0 / 0.333333333 / 0.666666667 / 1.0`，
   但控制器 `DPOS` 只回读 4 位小数（库里 `actual` 为 `0.333299994`）。
   下一点 Z2 的「位移」只剩 `3.3e-5 mm`，即 `0.0033` 个编码器计数
   （本机 1 计数 = 0.01 mm），编码器物理上分辨不了。
2. `ZmotionMotionController.move_to` / `move_axis_to` 把 **mm 单位**的
   `position_tolerance = 0.002` 直接和 **编码器计数**的
   `measured - expected` 比较，少了 `* feedback_scale` 换算，容差被缩小 100 倍，
   于是把量化残差误判成「命令标度与反馈标度不一致」并调用 `_latch_fault()`。

| 项 | 0.3.8 |
|---|---|
| 校验 | 容差统一到计数：`max(position_tolerance × feedback_scale, \|expected\| × 5%, 1 计数)`；`1 计数`覆盖量化，真实的 84↔100 标度错仍超出 5% 带被拒绝 |
| 回归 | `tests/test_zmotion.py` 新增 3 例：1/3 mm 残差不锁存（`move_to` 与 `move_axis_to`）、真实标度错仍拒绝 |
| 续扫 | `handlers.studio_setup` 遇到**已存在**的库时不再报 `file exists`，而是校验频率网格与点位一致后直接挂上 `CalibrationManager`（`state='ready'`），保留 `valid` 点并从 `pending_indices()` 续跑；不一致则明确报错，绝不覆盖别人的库 |
| UI | 「布点与设置」页填回旧库路径即可续扫，提示改为「已加载校准库（续扫）… 已有 N / 总点数」；`calibration_progress` 从已有有效点数接续 |
| 验证 | compileall 通过；**268 passed / 1 deselected**（新增 5 例：3 个编码器 + 2 个续扫）；`test_calibration.py` 原有「重启即续扫」用例仍通过 |
| 实机结果 | 11:49 用 0.3.8 挂载 `cal_20261108_105400.h5` 续扫（`resumed=true`，已有 6 点）；11:57 **576 / 576 全部 valid**、`errors` 空、SQLite `pending=0`、5 GHz 单点 `s (576,1,2,2)` 全为有限复数、\|S11\| 0.6438–0.9549。界面显示「575 / 576、99%」是 0 基索引口径，以 `valid`/`complete` 为准 |

> **注意**：0.3.7 及更早版本在「打开校准库」后 `manager=None`，点「开始 / 继续」会报
> `create or open a calibration task first`；那是同一进程内能续扫、重启后不能续扫的缺陷，
> 0.3.8 才补齐跨重启链路。

**现场恢复顺序**（0.3.8）：停机旧版 → 启动 0.3.8 → 通讯设置页连控制器 →
轴移动控制页「复位运动故障」+ 四轴回零 → 布点与设置页按原参数填好（含旧库路径）→
「初始化并建立校准库」（此时显示「续扫」）→ 采集运行页「开始 / 继续」。
详见 `docs/SOP_FIRST_REAL_CALIBRATION.md` §6。

---

## 16. 0.3.9 Smith 图点选 → 移动 → 实测 Z / Γ / VSWR 判定

0.3.8 之前，「Smith 图」页只是只读查看器：目标阻抗必须在「阻抗调谐」页用 R / X
数字框输入；到位后的「实测验证当前位置」只给 `实测 Γ`、`|ΔΓ|` 和是否在容差内，
源码里**完全没有 VSWR 计算**。0.3.9 把这条链路补成一键闭环。

| 环节 | 0.3.9 实现 |
|---|---|
| 点选 | `tuner/ui.py::SmithChart` 增加 `picked` / `hovered` 信号与 `mousePressEvent`；纯函数 `smith_gamma_from_pixel(x, y, w, h)` 把像素映射成 Γ，单位圆外返回 `None`（不会选中无源阻抗之外的点）。紫色十字为点选目标，浅灰十字为光标 |
| 信息卡 | Smith 图页右侧实时显示 反射系数 Γ、`\|Γ\|`/相位、阻抗 Z (Ω)、归一化 z = Z/Z₀、VSWR、回波损耗 dB、Γ 坐标 (Re, Im)、Smith 坐标 (r, x)；光标位置单独显示 |
| 搜索 | 「搜索对应点位」把点选 Γ 换算成 Z 后调用 `studio_tune`，显示 X/Z1/Z2/Z3 机械位置、预测 Γ、预测 Z、`\|ΔΓ\|` 与来源（已标定点 / 插值位置），并刷新图上的琥珀目标环与粉色候选环 |
| 一键闭环 | 「移动并实测验证」调用新增 `handlers.studio_move_to_gamma`：**目标 Γ → 搜索/插值选最佳点位 → `planner.move_to` → 短稳定等待 → 矢网重新测量 → 完整判定 → 写入 HDF5 `validation` 组**。全程在后台线程，按钮期间禁用，失败也会恢复 |
| 判定面板 | 同时给出目标与实测的 Z / Γ / `\|Γ\|` / 相位 / VSWR / 回波损耗，机械位置与位置最大偏差，以及 `\|ΔΓ\|` vs 容差、ΔR / ΔX（Ω 与 %）、ΔVSWR、来源与 RF 硬件认证 |
| 调谐页同步 | 「实测验证当前位置」也补充显示 `\|Γ\|`、相位、实测 Z、VSWR、ΔR / ΔX |
| 措辞修正 | `studio_verify` 原先硬编码 `rf_hardware_verified=False` 和 `Simulated RF verification only`；现在按 `measurement.simulated` 判定，实机数据为 `True` 并改用实机描述 |
| 续扫挂载重建 | `studio_setup` 挂载已有库时调用 `_load_database(..., keep_task=True)` 重建 `search_engine` / `data` / 指纹，同时保留 `CalibrationManager` 与 `ready` 状态。0.3.8 重启后只挂 manager，Smith 图会空白、点选移动会因 `data=None` 被拒，0.3.9 一并补齐 |
| 默认矢网 IP | 现场实测 `192.168.2.111:5025` 可连且 `*IDN?` 返回 `Rohde-Schwarz,ZNA26-2Port,...,2.97`；`182.168.2.111` 与旧默认值 `192.168.0.50` 均超时不可达。把 `pages_studio.py` / `state.py` / `handlers.py` / `pages_vna.py` / `studio_bridge.py` / `vna_drivers.py` 里的默认值统一改为 `192.168.2.111`，重启后无需再手填 IP |
| API | 新增 `POST /api/studio/move_to_gamma` 路由（网页控制台可复用后端闭环） |

**数据口径**：Γ 仍是 `RF.dut_gamma(S)`，输出端 50 Ω 匹配、z0 = 50 Ω；
`VSWR = (1+\|Γ\|)/(1-\|Γ\|)`，`\|Γ\|→1` 时显示 `∞ : 1`（JSON 中为 `null`，避免写出
非标准 `Infinity`）；回波损耗 `−20·log₁₀\|Γ\|`；阻抗偏差 ΔR / ΔX 为「实测 − 目标」。
判定仍以 `\|ΔΓ\| ≤ 容差` 为准，Z / VSWR 数值是并列呈现的辅助量，不另设阈值。

**为什么保留「搜索对应点位」**：一键按钮会自动选择「插值结果与最近标定点中
`\|ΔΓ\|` 更小的一个」，先单独搜索可以在不运动的情况下确认机械位置是否可达、
落在行程内，避免不必要的轴动作。

**验证**：`compileall` 通过；**272 passed / 1 deselected**（新增 4 例：像素→Γ 几何、
一键闭环报告的 Z/Γ/VSWR 与位置、单位圆外拒绝、实机/模拟来源区分）；离屏 Qt 冒烟确认
点选信息卡与判定面板全部渲染正常。

> 0.3.9 只改交互与报告字段，不改 `s / positions / valid / actual` 主数据；
> 每次闭环仅在 `cal_*.h5` 的 `validation` 组追加一条报告。
