"""Verify neutral level-policy key + adaptive UI type scale.

Run from the repo root with the project venv:
    python verification/verify_ui_scale.py
"""
from __future__ import annotations

import itertools
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'TunerLab'))
sys.path.insert(0, str(ROOT / 'TunerLabApp'))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

RESULTS: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, detail: str = '') -> None:
    RESULTS.append((label, bool(ok), detail))
    print(('PASS  ' if ok else 'FAIL  ') + label + (('  -> ' + detail) if detail else ''),
          flush=True)


# --- 1) neutral level-policy key, with legacy mapping --------------------
import numpy as np
from tuner.interp import LEVEL_POLICIES, TensorInterpolator, GridLayout

check('LEVEL_POLICIES 用中性键 classic（无 focus）',
      'classic' in LEVEL_POLICIES and 'focus' not in LEVEL_POLICIES,
      str(LEVEL_POLICIES))

lookup = {key: i for i, key in enumerate(itertools.product(range(3), repeat=3))}
layout = GridLayout(levels=[np.array([0.0, 1.0, 2.0])] * 3, lookup=lookup)
positions = np.array([[float(a), float(b), float(c)]
                      for a, b, c in itertools.product(range(3), repeat=3)])
values = np.array([complex(0.1 * (i + 1), 0.05 * (i + 1)) for i in range(27)])
try:
    legacy = TensorInterpolator(positions, values, layout, level_policy='focus')
    check('旧值 focus 自动映射为 classic', legacy.level_policy == 'classic',
          legacy.level_policy)
except Exception as exc:  # noqa: BLE001
    check('旧值 focus 自动映射为 classic', False, repr(exc))
modern = TensorInterpolator(positions, values, layout, level_policy='classic')
check('新值 classic 正常工作', modern.level_policy == 'classic')
try:
    TensorInterpolator(positions, values, layout, level_policy='bogus')
    check('未知策略仍被拒绝', False)
except ValueError:
    check('未知策略仍被拒绝', True)


# --- 2) brand/business focus word scan ----------------------------------
brand_hits: list[str] = []
business_hits: list[str] = []
scan_ext = {'.py', '.html', '.js', '.json', '.md', '.txt'}
for path in ROOT.rglob('*'):
    if not path.is_file() or path.suffix not in scan_ext:
        continue
    if path.name == 'verify_ui_scale.py' or '.venv' in path.parts or '__pycache__' in path.parts:
        continue
    text = path.read_text(encoding='utf-8', errors='ignore')
    for m in re.finditer(r'(FOCUS|Maury|MAURY|MTUNE|iTuner|Microwaves)', text):
        brand_hits.append(f'{path.relative_to(ROOT)}:{text.count(chr(10), 0, m.start()) + 1}')
    for m in re.finditer(r'focus', text, re.I):
        line_start = text.rfind('\n', 0, m.start()) + 1
        line_end = text.find('\n', m.end())
        line = text[line_start: line_end if line_end >= 0 else None]
        if ':focus' in line or '_LEGACY_LEVEL_POLICY' in line:
            continue  # CSS pseudo-class / deliberate legacy key
        business_hits.append(f'{path.relative_to(ROOT)}:'
                             f'{text.count(chr(10), 0, m.start()) + 1}  {line.strip()[:70]}')
check('第三方品牌大写词已清除', not brand_hits, str(brand_hits[:5]))
check('小写 focus 业务字眼已清除（保留 CSS :focus 与兼容键）',
      not business_hits, str(business_hits[:6]))

web_css = (ROOT / 'TunerLab/tuner/web/static/style.css').read_text(encoding='utf-8')
fixed_px = re.findall(r'font-size:\s*[\d.]+px', web_css)
check('Web 控制台字号视口自适应（无固定 px，含 clamp）',
      not fixed_px and web_css.count('clamp(') >= 15,
      f'fixed={len(fixed_px)} clamp={web_css.count("clamp(")}')


# --- 3) stylesheet scaling ----------------------------------------------
from tuner.ui_theme import STYLESHEET
from tunerlab_app.theme import SHELL_QSS, scaled_stylesheet

base = STYLESHEET + SHELL_QSS
small = scaled_stylesheet(base, 0.92)
normal = scaled_stylesheet(base, 1.0)
large = scaled_stylesheet(base, 1.5)


def all_sizes(qss: str) -> list[float]:
    return [float(x) for x in re.findall(r'font-size\s*:\s*([0-9.]+)px', qss)]


def button_size(qss: str) -> float | None:
    m = re.search(r'QPushButton\s*\{[^}]*font-size:\s*([0-9.]+)px', qss)
    return float(m.group(1)) if m else None


check('字号随窗口放大', max(all_sizes(large)) > max(all_sizes(normal)) > max(all_sizes(small)),
      f'max: {max(all_sizes(small))} < {max(all_sizes(normal))} < {max(all_sizes(large))}')
check('小窗口字号有下限（>=12px）', min(all_sizes(small)) >= 12.0,
      f'min={min(all_sizes(small))}')
check('按钮字号随窗口放大',
      button_size(large) is not None and button_size(small) is not None
      and button_size(large) > button_size(small),
      f'button: {button_size(small)} -> {button_size(large)}')
check('小窗口按钮文字仍可读（>=12px）',
      button_size(small) is not None and button_size(small) >= 12.0,
      f'button={button_size(small)}')


# --- 4) font helpers track the scale ------------------------------------
from tunerlab_app import fonts

fonts.set_ui_scale(0.92)
p_small, u_small = fonts.px_font(12).pixelSize(), fonts.ui_font(10).pointSizeF()
fonts.set_ui_scale(1.50)
p_large, u_large = fonts.px_font(12).pixelSize(), fonts.ui_font(10).pointSizeF()
fonts.set_ui_scale(1.0)
check('px_font 随 scale 变化', p_large > p_small, f'{p_small} -> {p_large}')
check('ui_font 随 scale 变化', u_large > u_small, f'{u_small} -> {u_large}')


# --- 5) MainWindow applies scale from its real width --------------------
from PySide6.QtWidgets import QApplication, QPushButton

app = QApplication.instance() or QApplication([])
app.setStyleSheet(scaled_stylesheet(base, 1.0))
from tunerlab_app.shell import MainWindow

win = MainWindow(smoke_test=True)
app.processEvents()
expected = min(fonts.UI_SCALE_MAX, max(fonts.UI_SCALE_MIN, win.width() / 1280.0))
check('构造时按窗口宽度设定 scale', abs(fonts.get_ui_scale() - expected) < 1e-6,
      f'width={win.width()} scale={fonts.get_ui_scale():.3f}')

buttons = [b for b in win.findChildren(QPushButton) if b.text()]
actual_px = sorted({b.font().pixelSize() for b in buttons if b.font().pixelSize() > 0})
print(f'      实测按钮字号(px) = {actual_px[:10]}', flush=True)
check('窗口中存在按钮且字号可读(>=12px)', bool(actual_px) and min(actual_px) >= 12,
      str(actual_px[:8]))

for width, want in ((1140, fonts.UI_SCALE_MIN), (1920, fonts.UI_SCALE_MAX)):
    win.resize(width, 880)
    win._apply_ui_scale(force=True)
    app.processEvents()
    got = fonts.get_ui_scale()
    check(f'resize 到 {width}px -> scale {want}', abs(got - want) < 1e-6, f'got={got:.3f}')
    m = re.search(r'\*\s*\{[^}]*font-size:\s*([0-9.]+)px', app.styleSheet())
    check(f'resize 到 {width}px -> 全局字号已重算', m is not None,
          f'* font-size={m.group(1) if m else None}')
    live_px = sorted({b.font().pixelSize() for b in buttons if b.font().pixelSize() > 0})
    print(f'      {width}px 实测按钮字号(px) = {live_px[:6]}', flush=True)
    if width == 1140:
        check('小窗口按钮实际字号 >=12px', bool(live_px) and min(live_px) >= 12,
              str(live_px[:6]))
    if width == 1920:
        check('大窗口按钮实际字号放大(>=17px)', bool(live_px) and max(live_px) >= 17,
              str(live_px[:6]))

if hasattr(win, '_scale_timer'):
    win._scale_timer.stop()
    try:
        win._scale_timer.timeout.disconnect()
    except Exception:  # noqa: BLE001
        pass
passed = sum(1 for _, ok, _ in RESULTS if ok)
code = 0 if passed == len(RESULTS) else 1
print(f'\n{passed}/{len(RESULTS)} checks passed', flush=True)
# Tear the QWebEngine-backed console down through Qt's own event loop, the same
# path --smoke-test uses; closing it and exiting Python directly segfaults on
# interpreter shutdown.
from PySide6.QtCore import QTimer
win.close()
QTimer.singleShot(0, app.quit)
app.exec()
sys.exit(code)
