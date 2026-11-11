# TunerLab

面向 Windows 10/11 x64 的负载牵引调谐器运动控制与校准桌面应用。当前版本 **0.5.11**（2026-11-11）。

## 双 Tuner 支持

一台控制器可同时驱动两台 Tuner，各自占用不同的物理轴号：

| Tuner | X | Z1 | Z2 |
|---|---|---|---|
| 左 Tuner | 0 | 1 | 2 |
| 右 Tuner | 3 | 4 | 5 |

在「通讯设置」页选择 **当前 Tuner** 后，所有运动、回零、标定与参数编辑都只作用于所选的那台；
每台 Tuner 有独立的轴参数，切换时互不覆盖。切换不会重连控制器。
轴面板标题与状态栏会显示该轴的物理轴号，便于与接线图核对。
详见 [硬件接入文档](TunerLab/docs/HARDWARE.md)。

## 从源码启动桌面端

在仓库根目录运行（Windows PowerShell，Python 3.12 x64）：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python -m pip install -r TunerLab\requirements-tested.txt -e ".\TunerLab[test]"
cd TunerLabApp
..\.venv\Scripts\python -m tunerlab_app
```

主入口是 `TunerLabApp/tunerlab_app`；`TunerLab/tuner` 是共用后端，包含原有桌面和 Web 入口。不要把两个目录当成两套独立安装的后端。

## 推荐操作顺序

1. **通讯设置**：选择当前 Tuner（左/右）、虚拟/真实控制器、3/4 轴型号，连接。首次熟悉界面用虚拟模式。
2. **轴移动与参数**：检查方向、单位、行程、回零和标度；实机校准前确认控制器状态。
3. **矢网控制**：选择接口（TCP/IP 或 GPIB/VISA）、厂商、地址与频率，连接并做一次扫描。GPIB（如 Agilent 82357B USB-GPIB）需安装 Keysight IO Libraries Suite，详见 [VNA 接入指南](TunerLab/docs/VNA.md)；首次接真机若报 `-113 Undefined header`，用「系统 → SCPI 调试工具」探测该机型的真实命令集，可一键复制报告 / 导出 JSON / 应用为后续连接的方言；模拟运动与模拟 RF 是不同选项，按任务核实来源。
4. **建库向导**：参考面与仪器 → 运动与行程 → 频率方案 → 布点计划 → 前提检查与建库。
5. **采集运行**：开始、暂停、继续或取消；重启续扫时保持库路径及参数一致。完成度以有效点与 complete 状态为准。
6. **Smith 图 / 阻抗调谐**：打开校准库，选频率与目标，先搜索点位，再执行移动与实测验证。
7. **验证与记录**：检查模拟/实测标识、误差和验证报告，导出记录。

任务运行时关闭窗口，会先询问是否停止；确认后等待当前点完成，再次关闭。取消退出会保留浏览器控制台。

## 数据位置

- 设置：`%APPDATA%\TunerLab\settings.json`。
- 日志：`%LOCALAPPDATA%\TunerLab\logs`（未设置时回退 APPDATA）。
- 校准库、报告、目标模式：Windows 系统实际的 Documents 目录下 `TunerLab`，支持 OneDrive/域重定向。
- 可选 `TUNERLAB_DATA_DIR`：指定可写的数据根目录；不会迁移或删除既有数据。

## 验证和 Windows 打包

```powershell
# 仓库根目录
$env:QT_QPA_PLATFORM = 'offscreen'
$env:PYTHONPATH = "$PWD\TunerLabApp"
.\.venv\Scripts\python -m pytest TunerLab\tests TunerLabApp\tests -q
cd TunerLabApp
..\.venv\Scripts\python -m tunerlab_app --smoke-test
..\.venv\Scripts\python -m pip install pyinstaller==6.16.0
..\.venv\Scripts\python -m PyInstaller packaging\tunerlab.spec --noconfirm --clean
```

生成 `TunerLabApp/dist/TunerLab/TunerLab.exe`。可用 Inno Setup 6 编译 `packaging/installer.iss` 生成安装器。GitHub Actions 的 **Desktop validation and Windows package** 会在 Windows 上测试、打包、执行 EXE 冒烟检查、逐文件 SHA-256 校验 ZIP，并将 ZIP 保存为当前仓库的 Actions artifact（保留 14 天）；未配置对外发布步骤，访问权限跟随仓库。

### 一键发版

`packaging/release.py` 负责整条发布链：自增版本号并同步写入 `version.py` 与
`version_info.txt` → PyInstaller 构建 → 补齐 VC++/MFC 运行时 → 冻结包 `--version`
与 `--smoke-test` 自检 → 逐文件 SHA-256 校验打包进 `releases/`。

```powershell
cd TunerLabApp
..\.venv\Scripts\python packaging\release.py --bump patch
```

产物为 `releases/TunerLab-<版本>-dist.zip`、同名目录与 `.sha256.txt` 清单。
`--bump minor` / `--bump major` 提升对应位，`--dry-run` 只打印将要发生的事。

Linux 可用 `QT_QPA_PLATFORM=offscreen` 验证 Mock 和界面逻辑；Windows DLL 加载及显式授权的实机测试会跳过。不能由 Linux 测试推断现场验收通过。

详见 [项目检查与改进报告](TunerLabApp/docs/REVIEW_0.5.6.md)、[实机首次校准 SOP](TunerLabApp/docs/SOP_FIRST_REAL_CALIBRATION.md)、[桌面架构说明](TunerLabApp/docs/DESKTOP_APP.md)。
