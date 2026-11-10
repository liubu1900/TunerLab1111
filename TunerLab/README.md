# TunerLab — Load Pull Tuner Calibration

Python / PySide6 桌面应用，包含两个入口：

| 应用 | 命令 | 内容 |
|---|---|---|
| **运动控制台** | `python -m tuner.app` | Zmotion 实机运动控制：通讯、移动控制、轴参数设定（支持 3 轴 / 4 轴两种 tuner 型号） |
| **校准工作台** | `python -m tuner.ui` | Mock 校准系统、Smith 图、阻抗调谐 |
| **Web 控制台** | `python -m tuner.web` | 零依赖本地 Web 服务（默认 `http://127.0.0.1:8000`），浏览器呈现上述全部界面 |

运动控制台已接入真实 Zmotion 控制器（`192.168.0.11`，固件 2.9600）；校准工作台仍可
选用模拟数据，或在 Web 控制台接入**真实矢网（VNA）**做实物校准。

### 矢网（VNA）驱动

`tuner/vna_drivers.py` 提供**多厂商、纯 TCP/IP（SCPI，默认端口 5025）**的矢网驱动，
无需安装任何 VISA 运行时/厂商驱动。支持的仪器：

| 厂商 | 标识 key | 代表机型 |
|---|---|---|
| 罗德 Rohde & Schwarz | `rsp` | ZNB / ZVA / ZND |
| Keysight ENA | `keysight_ena` | E5071C、E5080 |
| Keysight PNA-X | `keysight_pna` | N5247A、N522x |
| 思仪 Ceyear | `ceyear` | AV3672 |
| 安立 Anritsu | `anritsu` | VectorStar MS46xxx、ShockLine |

连接时通过 `*IDN?` 自动识别厂商并切换差异命令（测量定义、取数助记符 SDATA/SDAT、功率/中频表头、
触发）。每条差异命令带 trial 容错：首选命令报错会自动回退到同义写法（如 R&S 的 `SDEF` ↔ 其它家的 `DEF`）。

Web 控制台新增「仪器控制 › 矢网控制 (VNA)」页面：填厂商/IP/扫频/功率 → 连接并配置 → 单次扫描并读取
S11/S21/S12/S22（幅频曲线 + 数据表）。校准工作台 Dashboard 也可勾选「使用真实矢网」直接接入实机跑校准。

**使用前确认**：① 上位机与矢网同网段互通；② 仪器已开启 SCPI 且允许 LAN 控制（如 Keysight 需 `:SYST:COMM:LAN:ENAB`、
R&S 需开启 SCPI 服务器）；③ 防火墙放行 TCP 5025。

## 安装与运行

Python 3.11+，建议虚拟环境。Windows / Linux / macOS 源码运行：

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate
python -m pip install -e ".[test]"
python -m tuner.app          # 运动控制台（实机）
python -m tuner.ui           # 校准工作台（Mock）
python -m tuner.web          # Web 控制台（浏览器，仅标准库）
python -m tuner.demo --help
python -m pytest -q
```

依赖：PySide6 桌面；NumPy/SciPy 数学与 KDTree/插值；h5py 数据；SQLite 使用标准库；PyVISA 通信；scikit-rf 独立数学验证；pytest 测试。没有额外运行依赖。真实 VISA 通信另需厂商 VISA backend 或自行安装 pyvisa-py。

## Web 控制台

`python -m tuner.web` 启动一个**仅依赖标准库**（`http.server`）的本地 Web 服务，浏览器打开
`http://127.0.0.1:8000` 即可使用。它复用与桌面版完全相同的后端：实机硬件走
`tuner.ui_hardware.HardwareSession`，校准工作台走纯 Python 的 Mock 模块。左侧导航分两组——
运动控制台（通讯设置 / 轴移动控制 / 轴参数设定）与校准工作台（Dashboard / 校准设置 / 校准运行 /
数据库 / Smith 图 / 阻抗调谐 / 验证报告）。实时状态每 300 ms 轮询刷新。

Zmotion DLL 为 Windows 专用，服务须运行在物理连接控制器的机器上；浏览器可在同机（localhost）
或局域网内其它设备访问。绑定地址可用 `--host` / `--port` 调整，`--no-browser` 可跳过自动开浏览器。

## 真实 / 虚拟模式

运动控制台可在**真实控制器**（Zmotion 运动控制卡）与**虚拟控制器**（内置模拟器）之间切换，
选择在「通讯设置 › 控制器模式」里完成并持久化到设置文件。

| | 真实模式 | 虚拟模式 |
|---|---|---|
| 需要硬件 | 是 | **否** |
| 通讯 | UDP 广播发现实机 | 无（`virtual://zmotion`） |
| 初始状态 | 读取实机位置与回零状态 | **自动回零并生成初始位置** |
| 标度自检 | 实测编码器比例 | 默认一致（可注入故障模拟偏差） |
| 故障演练 | 不可 | **可注入状态位**（如 bit22 告警信号输入，即实机 Z2 曾出现的那一位） |
| 虚拟校准数据 | 不可 | **一键生成**合成校准库（结构与实测库一致） |

切换模式会**断开当前连接**——两种后端没有共同点，半连接状态最危险。

虚拟模式下额外提供两项演练工具（仅虚拟模式可见）：

- **虚拟故障演练**：向指定轴注入状态位，练习读取与判读诊断页；
- **生成虚拟校准数据**：一键生成含 S 参数的合成校准库，可直接用于 Smith 图、
  阻抗调谐与插值验证。

## Tuner 型号（3 轴 / 4 轴）

软件支持两种 tuner 型号，通过「通讯设置 › tuner 型号」切换，选择结果持久化在设置文件：

| 型号 | 轴 | 探针 | 校准网格点数 |
|---|---|---|---|
| **3 轴** | X / Z1 / Z2 | 2 | `x_count × z_count²` |
| **4 轴** | X / Z1 / Z2 / Z3 | 3 | `x_count × z_count³` |

关键约定：**控制器的物理轴序号固定不变**（X=0、Z1=1、Z2=2、Z3=3），切换型号只改变
"软件命令哪几根轴"，因此已保存的校准数据、轴参数在两种型号间通用。

切换型号后：运动控制台与 Web 界面的轴表格、轴下拉随之增减；实机校准的探针数由型号
决定（不能手工选成与机器不符的探针数）；离开型号的轴其参数仍保留，切回即可复用。

## Zmotion 控制器

官方 `zauxdll.dll`（64 位）位于 `vendor/zmotion/`，与 `zauxdll2.h` 头文件一并保留，
驱动直接以 ctypes 绑定 C 接口，不依赖当前工作目录。库文件缺失时可用环境变量
`TUNERLAB_ZAUXDLL` 指向其它位置。

上位机需与控制器同网段（本机 `192.168.0.1/24`）。`ZAux_OpenEth` 通过 UDP 广播定位
设备，因此对控制器做 TCP 端口扫描看不到有效信息。

实测参数与验证结论见 [`docs/HARDWARE.md`](docs/HARDWARE.md)。

## 实机测试

```bash
python -m pytest tests/test_zmotion_hardware.py -q   # 需要控制器在线
```

控制器不可达时该文件自动跳过。

文档：[规格](docs/SPEC.md)、[接口](docs/API.md)、[操作手册](docs/USER_MANUAL.md)、[硬件接入](docs/HARDWARE.md)、[开发清单](docs/TASKS.md)。

**各轴独立运动（默认）**：X 轴不再受探针位置约束，校准流程也不会自动回缩探针；X 回零不再等待探针回零。`TunerConfig(independent_axes=False)` 可恢复旧的 retract-before-X 联锁。软件急停与软限位仍在，但**探针与 X 的碰撞避让现在由操作员和硬件联锁负责**。

这是源码可运行版本，非 Windows 打包 exe。Mock 通过不能代替真实硬件验收。首次实机连接前必须验证方向、限位、碰撞约束、退针位置及仪器测量格式。
