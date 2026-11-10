# TunerLab 0.5.5 — Smith 图交互改进（框选放大 + 模式切换）

**日期**：2026-11-09
**触发**：用户反馈当前 Smith 图缩放/移动难操作，建议「手动按钮切换缩放/移动模式，鼠标框选区域可局部放大」。

## 改动内容

| 项 | 说明 |
|---|---|
| 新模块 | `TunerLab/tuner/smith_interaction.py` — `SmithViewInteraction` 交互 mixin（Qt 事件逻辑：模式 + 框选） |
| 几何 | `TunerLab/tuner/smith_view.py` 新增 `zoom_to_rect()`：把屏幕框选矩形逆变换到 Γ，并把视图居中缩放填满（留 8% 边距） |
| 暗色图 | `TunerLab/tuner/ui.py` `SmithChart` 接入 mixin，paintEvent 末尾画虚线框选框 + 角标 |
| 亮色图 | `TunerLabApp/tunerlab_app/ui.py` `SmithPreview` 接入 mixin，同样画框选框 |
| 工具栏 | `tunerlab_app/ui.py` 新增 `smith_control_row()`：「缩放 / 移动 / 放大 / 缩小 / 复位」，当前模式按钮高亮 |
| 接入页面 | `pages_tune`（阻抗调谐）、`pages_pattern`（图案编辑）、`pages_studio`（向导布点预览 + 采集运行实时 Smith）、旧版 mock 窗口 |
| 主题 | `theme.py` 加 `QPushButton[modeactive="true"]` 高亮当前模式 |
| 版本 | `version.py` / `verify_release.py` / `version_info.txt`（4 处）→ 0.5.5 |

## 新交互规则

- **「缩放」模式（默认）**：左键**拖框** → 松开后局部放大到该区域；左键**单击不拖动** → 仍点选 Γ（阻抗调谐页选目标用）；滚轮随时以光标为中心缩放。
- **「移动」模式**：左键**拖拽** → 平移整图。
- **任何时候**：中键或 Ctrl+左键拖拽平移；双击复位为 1:1；「放大/缩小」按钮步进；「复位」按钮回到全图。
- 框选时显示蓝色虚线框 + 四角标记，便于确认选区。

## 验证结果

| 验证 | 结果 |
|---|---|
| 新增 `verification/test_smith_mode_boxzoom.py` | **7/7**（含 QMouseEvent 模拟：缩放模式单击点选、拖框放大；移动模式拖拽平移；滚轮随时缩放；退化框选拒变） |
| 既有 `verification/test_progress_and_smith_zoom.py` | **8/8**（未回归） |
| 源码 `python -m tunerlab_app --smoke-test` | 16 页全部构建 exit 0（0.5.5） |
| 冻结包 `dist_059/TunerLab/TunerLab.exe` | `--version` → 0.5.5；`--smoke` 连跑 3 次 exit 0（16 页） |
| 正常模式连真机 | 启动 12s 无 error / traceback / crash（exit 124 为预期超时） |
| 跨机自包含 | `zauxdll.dll` 仅用包内目录即可加载（handle≠0）；`mfcm140/mfc140u/msvcp140` 已补入 `_internal` 与 `vendor/zmotion` |

## 部署

- 最终程序：`TunerLabApp/dist_059/TunerLab/TunerLab.exe`（整目录复制，保持 `_internal` 结构；已含 VC++ 运行库，干净机无需安装 redist）。
- 现场确认：关于页看 0.5.5 → 打开任一 Smith 图（阻抗调谐 / 图案 / 采集运行实时 Smith / 向导布点预览）→ 试「缩放」拖框、「移动」拖拽、滚轮、双击复位、模式按钮高亮。
- 升级提示：dist_058 功能等价，仅版本号为 0.5.4；现场统一用 **dist_059**。
