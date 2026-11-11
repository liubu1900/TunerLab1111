# TunerLab

面向 Windows 10/11 x64 的负载牵引调谐器运动控制与校准桌面应用。当前版本 **0.5.14**（2026-11-11）。

## Tuner组合与三页联动

通讯设置、轴移动控制和轴参数设定共用设备组合与当前Tuner：

| 组合 | Tuner | 逻辑轴 → 物理轴 |
|---|---|---|
| 单台三轴 | Tuner 1 | X→0、Z1→1、Z2→2 |
| 双台三轴 | 左 / 右 | 左0/1/2；右3/4/5 |
| 单台四轴 | Tuner 1 | X→0、Z1→1、Z2→2、Z3→3 |

每种组合、每台Tuner、每轴参数独立保存。切换左右不重连、不自动写参数或回零；更换组合需要先断开。真实连接前需明确确认安装组合；本地保存、读取对比和写入读回校验分别操作。
详见 [联动验收记录](TunerLabApp/docs/TUNER_CONFIGURATION_ACCEPTANCE.md)。

## Windows下载

[GitHub Releases](https://github.com/liubu1900/TunerLab1111/releases) 提供当前用户安装程序 `TunerLab-0.5.14-Setup.exe`、便携包 `TunerLab-0.5.14-win64.zip` 和SHA256清单。
便携包须完整解压，保留 `_internal` 目录，再启动 `TunerLab.exe`。GPIB/82357B另外需要Keysight IO Libraries Suite；用户现场N5224A地址为 `GPIB0::16::INSTR`。

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

1. **通讯设置**：选择并确认单三轴/双三轴/单四轴组合、当前Tuner和虚拟/真实模式，连接。首次熟悉界面用虚拟模式。
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
..\.venv\Scripts\python packaging\windows_runtime.py
```

生成 `TunerLabApp/dist/TunerLab/TunerLab.exe`。SDK所需VC++2010 SP1 x64运行时应通过Microsoft官方安装器安装；`windows_runtime.py`将实际依赖MSVCR100.dll复制到包内并校验x64架构及SDK加载，不连接控制器。使用Inno Setup 6.5及以上编译 `packaging/installer.iss` 生成简体中文安装程序。

GitHub Actions的 **Desktop validation and Windows package** 会在Linux/Windows运行测试，构建Windows EXE、安装程序及便携ZIP，检查安装后启动和SDK加载，并校验ZIP文件。产物保留为Actions artifact（14天）；推送与应用版本一致的 `v*` 标签时，仅在全部验证通过后自动创建GitHub Release并上传安装器、ZIP和SHA256清单。

### 一键发版

`packaging/release.py` 用于Windows本地打包：自增版本并同步 `version.py`、`version_info.txt` 和 `installer.iss` → PyInstaller构建 → 补齐VC++2010 x64运行时 → EXE版本资源及 `--smoke-test` 核对 → SHA256校验并打包进 `releases/`。失败时不生成新压缩包；脚本不会自行推送GitHub或发布Release。

```powershell
cd TunerLabApp
..\.venv\Scripts\python packaging\release.py --bump patch
```

产物为 `releases/TunerLab-<版本>-dist.zip`、同名目录与 `.sha256.txt` 清单。
`--bump minor` / `--bump major` 提升对应位，`--dry-run` 只打印将要发生的事。

Linux 可用 `QT_QPA_PLATFORM=offscreen` 验证 Mock 和界面逻辑；Windows DLL 加载及显式授权的实机测试会跳过。不能由 Linux 测试推断现场验收通过。

详见 [项目检查与改进报告](TunerLabApp/docs/REVIEW_0.5.6.md)、[实机首次校准 SOP](TunerLabApp/docs/SOP_FIRST_REAL_CALIBRATION.md)、[桌面架构说明](TunerLabApp/docs/DESKTOP_APP.md)。
