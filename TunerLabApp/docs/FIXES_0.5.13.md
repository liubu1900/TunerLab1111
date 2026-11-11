# 0.5.13 VNA 问题修复

本文记录前一轮修复。后续三页联动及最新整体验证见 [实现与验收记录](TUNER_CONFIGURATION_ACCEPTANCE.md)，N5224A现行指令见 [离线手册核对](../../TunerLab/docs/N5224A_OFFLINE_MANUAL_AUDIT.md)。

实际运行目标为 Windows 10/11 x64；Linux offscreen 检查仅用于辅助验证，不能代替 Windows 打包和实机验收。

- 同名测量返回 +104 时查询测量目录（当前PNA使用 `:CALC1:PAR:CAT:EXT?`），核对名称和 S 参数一致后才复用。兼容 PNA 整体加引号和各字段分别加引号的目录响应。冲突或无法验证时明确拒绝连接并提示检查仪器测量；不会自动删除用户已有测量。
- +104 判定解析完整错误码和重复名称语义，避免将 -104 或 1104 等错误当成重复测量。
- “连接并配置”保留合法的 0 dBm 输入。
- 连接及后续扫频、功率、IFBW 配置失败均显式关闭新会话，完整成功后才更新已连接状态。桌面和共用后端的建库初始化路径也执行失败清理。
- 替换/清空方言时，从厂商 profile 重新计算全部有效命令，避免残留旧覆盖。
- 桌面测试保持一个 session 级 QApplication，并在其释放前显式销毁控件、处理延迟删除和 Python 引用环，修复测试结束后退出 139 的生命周期问题。此项不代表已确认 Windows 产品存在同样崩溃。

新增定向回归在 `TunerLab/tests/test_vna_review_regressions.py`，原有 GPIB 同名重连回归增加目录查询响应。

Windows 验证继续使用仓库的 `Desktop validation and Windows package` 工作流：运行全套测试、桌面 smoke、PyInstaller 构建、冻结 EXE smoke 和 ZIP 校验。真实仪器需额外验证 N5224A 首次连接、同名正常重连、同名参数冲突报错、0 dBm 配置及失败后重连。

本次验证（Linux / Python 3.12 / 固定依赖）：完整后端与桌面测试 627 passed、9 skipped，退出码 0；随后补充共用建库失败清理回归，定向回归文件共 21 passed，退出码 0；桌面 smoke 构建 17 页，退出码 0。9 项跳过分别是 Windows DLL 检查和未授权的实机测试。Windows runner、冻结 EXE 和真实仪器尚未在本次会话执行。
