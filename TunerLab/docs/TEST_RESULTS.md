# 测试结果

2026-10-26，Linux / Python 3.12.14。

最终全套：**42 passed**。命令：`QT_QPA_PLATFORM=offscreen python -m pytest -q --junitxml=examples/test-results.xml`。

| 阶段 | 自动化覆盖 |
|---|---|
| Phase 0 | 坐标模型、双/三探针网格、无效配置 |
| Phase 1 | 回零/限位/急停/超时/退针顺序（旧联锁模式）；各轴独立模式 X 不受探针位置约束 |
| Phase 2–3 | S2P格式/被动性/用户SCPI profile/失败重试/暂停重启/持久化/来源校验/版本分叉 |
| Phase 4 | Z与Γ/终端/夹具/级联scikit-rf对照/候选/多频/插值/定位配置校验 |
| Phase 5 | 双三探针GUI/零点恢复/后台扫描/模拟RF验证/配置不符/重复性 |
| Phase 6 | 双三探针全闭环/独立重载/复数矢量统计/重复性不宣称阻抗精度 |

独立CLI验收：双探针45/45、三探针135/135，19个频率点，simulated=true，rf_hardware_verified=false。界面Qt offscreen实际渲染检查通过。源码editable安装成功。未进行真实仪器、Windows本机、可执行安装器、长时压力与掉电损坏测试。

## 验证环境

- numpy 2.3.5
- scipy 1.17.0
- h5py 3.16.0
- PySide6 6.12.0
- PyVISA 1.16.2
- scikit-rf 2.2.0
- pytest 9.1.1
