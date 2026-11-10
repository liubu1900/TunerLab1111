# TunerLab 0.5.3 — 真机功能验证报告

日期：2026-11-08　机台：控制器 `192.168.0.11`（固件 2.9600）+ 矢网 `192.168.2.111:5025`（Rohde-Schwarz ZNA26-2Port，固件 2.97）

---

## 一、冻结包段错误（Exit 139）的真因

上一轮怀疑「PyInstaller 环境产出损坏二进制」，**结论是错的**。

`crash.log` 里每次崩溃都留下明确栈帧：

```
Windows fatal exception: access violation
  File "tunerlab_app\shell.py", line 288 in _build_ui
```

那一行是 `group_item.setForeground(0, Qt.GlobalColor(0x97a6b6))`。**PySide6 6.9.3 不接受用任意 RGB int 构造 `Qt.GlobalColor`**，会直接原生访问冲突。`dist_053` 是在修这一行**之前**打的旧包，所以每次必崩。

修复：改用 `QColor('#97a6b6')`。

| | 修复前 | 修复后 |
|---|---|---|
| 源码 smoke | 段错误 | exit 0 |
| 冻结包 smoke | exit 139 | **连续 3 次 exit 0** |

> 无头沙箱里跑 GUI 必须带 `QT_QPA_PLATFORM=offscreen`；默认 `windows` 平台起不来 window station，报的是 Qt6Gui 访问冲突。

---

## 二、逐按钮验证

真实 Qt 窗口 + 真硬件，93 个按钮全点。脚本：`verification/sweep_buttons.py`

```
OK      84      正常
DIALOG   7      守卫提示（均为正确行为）
DEFER    2      断开链路类，最后统一测
未捕获异常  0
```

### 实机能力确认

| 项目 | 结果 |
|---|---|
| 控制器连接 | OK，固件 2.9600，16 页窗口全建 |
| 矢网连接 | OK，*IDN? = Rohde-Schwarz,ZNA26-2Port,1332450022101355,2.97 |
| 矢网错误队列 | 空 |
| 四轴读回 | X/Z1/Z2/Z3 参数、DPOS/MPOS、状态全部正确 |
| 急停链路 | 触发 → 锁存 → 复位急停 → 复位运动故障，全程有日志 |
| 标度自检 | X 0.9932、Z1 0.9980、Z2 0.9983、Z3 0.9987 —— **四轴 UNITS 均正确** |
| 点动 + / 定位 | OK，实际位移与指令一致 |
| 回零 | OK（OP 输出脉冲，驱动自己回零，编码器位移 ~0.1 µm） |
| 长距离运动 | X 可完成 260 mm 级移动 |

---

## 三、查出并修掉的 6 个真实缺陷

### 1　「广播搜索」永远失败 —— `ZAux_SearchEth` 的 ABI 错了
`tuner/zmotion.py`

厂商头文件 `zauxdll2.h` 写的是两参数，**但实际 DLL 和厂商自己的 Python 包装都是三参数** `(ip, uims, &handle)`。绑定成两参 → 第三个寄存器是垃圾 → 恒返回 **20010**。

而且 **空 IP 不是广播**，同样 20010——必须给具体 IP。实测该接口还会限流：同进程第一次成功、之后全失败；网段枚举 `SearchEthlist` 8 次只中 1 次（本机三张网卡，广播回程不可靠）。

改为「广播枚举 + 直连探测」双通道：新增 `search_list()` 与 `probe_controller()`（`ZAux_OpenEth` 直连，**实测 100% 可靠，0.16 s 返回固件**）。

### 2　矢网扫描结果回调崩溃 —— `pages_vna.py::_summarise`
无条件访问 `self.kpi_points` / `self.kpi_step`，而该页**根本没有 KPI 磁贴**（同页其它更新点都有 `hasattr` 守卫）。扫描其实成功，但汇总行永远不更新，`AttributeError` 抛在后台回调里。

### 3　三处 `s['vna']` 未判空 —— `tuner/web/handlers.py`
`studio_validate` / `studio_verify` / `studio_move_to_gamma` 在仪器未初始化时抛 `AttributeError: 'NoneType' object has no attribute 'frequencies'/'measure'`，而不是清晰的 `the instrument is not initialised`（同文件 sibling 函数已有该守卫）。

### 4　实机校准根本进不去 —— 标度门禁语义冲突
门禁拿 `settings.axes[].feedback_scale` 与 1.0 比，但**两层对这个字段的定义不同**：

- 核心层 = 「指令/实测比值」，正常值 1.0
- 桌面层 = 「编码器计数/毫米」，本机 **100**（MPOS 是 10 µm 计数、DPOS 是 mm）

桌面层把 `feedback_scale` 定成 100 是**必须的**（否则每一步运动都会被编码器校验拒绝），所以门禁在桌面层**把每一根轴都判成标度错误**，实机建库向导的「初始化并建立校准库」永远失败。

修法：门禁改为问控制器 `controller.scale_is_consistent(axis)`，两层各自实现（核心层按比值，桌面层按**标度自检实测比值**，未自检过则比控制器当前 UNITS 与配置 UNITS）。

### 5　实机长距离运动必然超时 —— planner 另建了一份 config
`studio_init_hardware` 给 planner 另建了一份 `TunerConfig`，**motion_timeout 仍是默认 5 s**（桌面层已把控制器那份改成 60 s）。X 走 50.19 mm @10 mm/s = **5.02 s**，刚好超预算 → 报「motion timed out」并锁存故障，**但机器其实到位了**（DPOS 50.19）。

修法：planner 的 config 继承 `controller.config.motion_timeout`。

### 6　限位告警文案与事实相反 —— `pages_motion.py::_check_travel`
硬编码「控制器 FS/RS_LIMIT 仍是出厂值（±2e8），不能当保护用」。实测控制器里 X 是 380/0、Z 是 10/0，**与申报行程一致**。改为按控制器实际值播报。

---

## 四、查清后判定为「设计如此」的点

| 现象 | 判定 |
|---|---|
| 「点动 −」四根轴全被拒 | **守卫正确**。各轴都在行程 0 点（X 0–380 / Z 0–10），负向行程不存在。但意味着该按钮实际不可用 |
| 回零不动 | **设计如此**。驱动自己回零，控制器只发 OP 脉冲（OP0…OP3 ↔ X…Z3），编码器位移 ~0.1 µm |
| 新会话不知道 X 已回零 | 驱动回零后控制器 `HOME_STATUS` 保持 0，**首次运动前必须再点一次回零** |
| 「读取错误码」无数值 | 固件不支持任何 Basic 错误码查询（`?ERRCODE/?ERRMSG/?ERROR/?ERRNO/?AXISERR` 全 2033/3205），按设计降级 |
| 重复性验证提示「calibration is validating_interpolation」 | **并发守卫正确**（插值验证仍在跑） |
| X 轴停止原因残留 2048(CANCEL) | 控制器里的锁存值，界面在 `AXISSTATUS=0` 时不显示；`has_fault` 也把 bit 11 归为「忙」而非故障。**不是缺陷** |

---

## 五、最终验证

```
源码 smoke            exit 0
冻结包 smoke          exit 0（连续 3 次）
核心测试              443 passed, 8 skipped
桌面测试              24 passed
layout_check          no problems found
```

唯一失败项 `test_skrf_cascade_crosscheck`：`ModuleNotFoundError: No module named 'skrf'` —— 环境未装 skrf（且 skrf 本就在打包 excludes 里），与本次改动无关。

回归测试已补：驱动搜索 ABI（4 例）、矢网汇总（2 例）、标度门禁与 planner 超时继承（2 例）。

---

## 六、机器状态

测试后已复位：四轴全部回零、DPOS=0、无运动故障锁存。探针在行程 0 点（抬起位）。
