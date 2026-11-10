# 开发清单

| Phase | 状态 | 交付及依赖 |
|---|---|---|
| 0 架构 | 完成 | SPEC / API / models / 目录 / 任务依赖；模型测试 |
| 1 机械 | 完成（Mock + 实机后端） | homing / jog / limits / estop / timeout / retract；运动测试 |
| 2 VNA 与校准 | 完成（Mock及通用VISA接口） | 完整S2P / 全组合扫描 / retry / pause / restart |
| 3 数据库 | 完成 | HDF5 + SQLite / 有效提交标记 / 版本分叉 |
| 4 RF | 完成 | Z/Γ / termination / fixture / KDTree / 多频 / 局部插值；数学测试 |
| 5 UI | 完成（Mock + 实机控制台） | 8 页面校准工作台 / 3 页面运动控制台 / 后台扫描 / Smith / 搜索运动 / 验证 |
| 6 集成 | 完成（Mock） | 双/三探针端到端 / 示例数据库 / 报告 / 无头UI测试 |
| **实机运动控制** | **完成（接入 Zmotion）** | 通讯设置 / 四轴移动 / 轴参数设定 / 设置持久化 / 实机测试 |
| **各轴独立运动** | **完成（2026-11-07）** | 去掉 X 与探针的相互限制及自动回缩；`TunerConfig.independent_axes` 默认 True，False 可回滚；UI/文案/文档/测试同步，详见 TunerLabApp/docs/CHANGE_INDEPENDENT_AXES.md |
| VNA 实机资格确认 | 未完成，需硬件验证 | VISA资源/SCPI profile/校准/参考平面/触发/数据格式 |
| 实机 RF 性能验收 | 未完成 | 独立测量、重复性、测量不确定度与定位误差预算 |
| 实机行程限位与方向确认 | 未完成 | FS/RS_LIMIT 仍为出厂默认；各轴正方向未对照机械确认 |
| Windows 独立exe/安装器 | 未完成 | 当前交付跨平台Python源码，未构建安装器 |

执行：6个独立Agent分别完成机械、VNA、数据库/校准、RF、UI、Mock集成，主控冻结接口并集成、复查与打包。阶段测试及全套结果见 TEST_RESULTS.md。

## 实机运动控制（新增）

| 模块 | 职责 |
|---|---|
| `tuner/zmotion.py` | ctypes 绑定 `zauxdll.dll`；连接、轴参数读写、运动指令、Basic 命令通道 |
| `tuner/zmotion_motion.py` | 适配 `MotionController` 契约；限位、回读校验、急停锁定；各轴独立（默认），`independent_axes=False` 可恢复 retract-before-X |
| `tuner/axis_params.py` | 每轴参数模型与校验；默认值取自实测控制器 |
| `tuner/settings.py` | JSON 设置持久化（通讯设置 + 四轴参数），原子写入、导入导出 |
| `tuner/ui_hardware.py` | 通讯设置页、轴移动控制页、轴参数设定页 |
| `tuner/ui_theme.py` | 浅色主题与字体规范（中文 Noto Sans SC，纯英文/数字 Gilroy） |
| `tuner/app.py` | 运动控制台入口 `python -m tuner.app` |

实测参数、验证结论与未决项见 HARDWARE.md。

实现限制：网格是有限离散位置；不承诺连续空间全覆盖。数据库当前整体读入内存。未做多进程同文件并发写入；一个校准文件只允许一个任务写入。通用VISA适配器仅按用户提供profile执行，不自动生成仪器校准。
