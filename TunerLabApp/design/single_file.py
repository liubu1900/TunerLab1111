"""Build one self-contained HTML that shows all five design proposals.

The five prototypes live in separate files because each one is a complete page
with its own CSS.  For review they belong in a single document, so this script

* renders each proposal's four pages with Edge headless (1600×980),
* embeds every PNG as a base64 data URI,
* writes ``tunerlab-device-ui.html`` — one file, no external references.

    python design/single_file.py
"""
from __future__ import annotations

import base64
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
EDGE = Path(r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe')
OUT = HERE / 'tunerlab-device-ui.html'

PAGES = [('comm', '通讯设置'), ('motion', '轴移动控制'),
         ('params', '轴参数设定'), ('vna', '矢网控制')]

SCHEMES = [
    ('scheme-1', '方案 1', '浅色仪器面板',
     '浅灰面板、硬边框、等宽数字、青绿状态色；左侧图标窄轨，顶部页签。',
     '最贴近仪器操作习惯，风格克制不抢数据。', '边框偏多，卡片感弱。'),
    ('scheme-2', '方案 2', '浅色工程卡片',
     '212 px 分组侧栏 + 白色卡片 + 细分隔线 + 蓝色主色。',
     '分组清晰、最易落地，长时间使用不累眼。', '留白较多，同样信息更占屏幕。'),
    ('scheme-3', '方案 3', '双栏工作台',
     '无全局侧栏：顶部页签 + 左参数栏 / 右工作区，可拖动分隔。',
     '屏幕利用率最高，参数与曲线同屏。', '页签层级浅，页面多时不易定位。'),
    ('scheme-4', '方案 4', '高密度 HMI',
     '170 px 树形导航 + 12 px 等宽 + 表格优先，几乎无留白。',
     '一屏放下全部参数与四条迹线，1366×768 友好。', '视觉朴素，演示效果一般。'),
    ('scheme-5', '方案 5', '仪表盘',
     '图标轨 + 顶部 KPI 卡片 + 大面积绘图区，圆角、留白、渐变强调。',
     '巡检与演示友好，重点指标一眼可见。', '参数密度最低，调参需来回切换。'),
]


def shoot(key: str, page: str) -> Path:
    """Render one page of one proposal, retrying until the PNG is really there.

    Edge refuses to start when another headless instance still holds the default
    profile, so each call gets a private ``--user-data-dir``; without it roughly
    every other screenshot silently produced nothing.
    """
    target = HERE / 'shots' / f'{key}-{page}.png'
    target.parent.mkdir(exist_ok=True)
    if target.exists() and target.stat().st_size > 10_000:
        return target
    import shutil
    import tempfile
    import time
    for attempt in range(3):
        profile = Path(tempfile.mkdtemp(prefix='edge-shot-'))
        try:
            subprocess.run([
                str(EDGE), '--headless=new', '--disable-gpu', '--hide-scrollbars',
                '--force-device-scale-factor=1', '--window-size=1600,980',
                '--virtual-time-budget=1200',
                f'--user-data-dir={profile}',
                f'--screenshot={target}',
                f'file:///{(HERE / (key + ".html")).as_posix()}#{page}',
            ], check=False, capture_output=True, timeout=180)
        finally:
            shutil.rmtree(profile, ignore_errors=True)
        if target.exists() and target.stat().st_size > 10_000:
            return target
        time.sleep(0.6)
    raise RuntimeError(f'screenshot failed: {key} {page}')


def data_uri(path: Path) -> str:
    return 'data:image/png;base64,' + base64.b64encode(path.read_bytes()).decode('ascii')


def main() -> int:
    shots: dict[tuple[str, str], str] = {}
    for key, *_ in SCHEMES:
        for page, _label in PAGES:
            path = shoot(key, page)
            shots[(key, page)] = data_uri(path)
            print('embedded', key, page, f'{path.stat().st_size // 1024} KB')

    nav = ''.join(
        f'<button class="tab" data-s="{key}">{label} · {name}</button>'
        for key, label, name, *_ in SCHEMES)

    sections = ''
    for key, label, name, desc, pros, cons in SCHEMES:
        figures = ''
        for page, page_label in PAGES:
            figures += f'''
            <figure>
              <img src="{shots[(key, page)]}" alt="{name} {page_label}"
                   data-s="{key}" data-p="{page}">
              <figcaption>{page_label}</figcaption>
            </figure>'''
        sections += f'''
        <section class="scheme" id="{key}" data-s="{key}">
          <header>
            <h2>{label} · {name}</h2>
            <p>{desc}</p>
          </header>
          <div class="grid">{figures}</div>
          <div class="judge">
            <div><b>优点</b><span class="pro">{pros}</span></div>
            <div><b>代价</b><span class="con">{cons}</span></div>
          </div>
        </section>'''

    matrix_rows = ''
    for page, page_label in PAGES:
        cells = ''.join(
            f'<td><img src="{shots[(key, page)]}" data-s="{key}" data-p="{page}" '
            f'alt="{page_label}"></td>' for key, *_ in SCHEMES)
        matrix_rows += f'<tr><th>{page_label}</th>{cells}</tr>'

    html = f'''<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>TunerLab「设备」四页 · 五套 UI 方案</title>
<style>
:root{{--ink:#16273a;--muted:#5b6b7c;--line:#dfe5ec;--bg:#f1f4f8;--accent:#1f5f8b}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--ink);
  font-family:"Noto Sans SC","Microsoft YaHei",sans-serif;font-weight:400;
  -webkit-font-smoothing:antialiased}}
header.top{{position:sticky;top:0;z-index:20;background:#fff;border-bottom:1px solid var(--line);
  padding:14px 22px 10px;box-shadow:0 1px 4px rgba(20,40,60,.05)}}
h1{{font-size:18px;margin:0 0 4px;font-weight:700;letter-spacing:-.01em}}
.lead{{margin:0 0 12px;color:var(--muted);font-size:12.5px;font-weight:300;line-height:1.7}}
.tabs{{display:flex;gap:6px;flex-wrap:wrap}}
.tab{{border:1px solid var(--line);background:#f7f9fc;color:#33475a;border-radius:6px;
  padding:6px 12px;font:inherit;font-size:12.5px;font-weight:500;cursor:pointer}}
.tab.on{{background:var(--accent);border-color:var(--accent);color:#fff;font-weight:600}}
.wrap{{max-width:1560px;margin:0 auto;padding:18px 22px 60px}}
.scheme{{display:none}}
.scheme.on{{display:block}}
.scheme header h2{{font-size:16px;margin:0 0 4px;font-weight:700}}
.scheme header p{{margin:0 0 14px;color:var(--muted);font-size:12.5px;font-weight:300}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:12px}}
figure{{margin:0;background:#fff;border:1px solid var(--line);border-radius:8px;
  padding:8px;box-shadow:0 1px 3px rgba(20,40,60,.04)}}
figure img{{width:100%;display:block;border-radius:4px;cursor:zoom-in}}
figcaption{{font-size:11.5px;color:#7b8a99;padding-top:7px;font-weight:300}}
.judge{{display:flex;gap:26px;margin-top:14px;padding:12px 14px;background:#fff;
  border:1px solid var(--line);border-radius:8px;font-size:12.5px}}
.judge b{{display:block;font-size:10.5px;color:#8496ad;font-weight:500;margin-bottom:3px;
  letter-spacing:.06em}}
.judge > div{{flex:1}}
.pro{{color:#1f7a4d;font-weight:400}}
.con{{color:#a8661a;font-weight:400}}
#matrix{{display:none}}
#matrix.on{{display:block}}
table.matrix{{border-collapse:collapse;width:100%;background:#fff;
  border:1px solid var(--line);border-radius:8px;overflow:hidden}}
table.matrix th{{font-size:12px;color:#3f5568;font-weight:600;background:#f4f7fa;
  border:1px solid var(--line);padding:8px;text-align:left;width:96px}}
table.matrix td{{border:1px solid var(--line);padding:6px;width:19%}}
table.matrix img{{width:100%;display:block;cursor:zoom-in;border-radius:3px}}
#lightbox{{position:fixed;inset:0;background:rgba(12,22,34,.82);display:none;
  align-items:center;justify-content:center;z-index:50;padding:24px}}
#lightbox.on{{display:flex}}
#lightbox img{{max-width:100%;max-height:100%;border-radius:6px;
  box-shadow:0 18px 50px rgba(0,0,0,.45)}}
.hintbar{{font-size:11.5px;color:#8496ad;font-weight:300;margin-top:8px}}
</style></head>
<body>
<header class="top">
  <h1>TunerLab「设备」四页 · 五套 UI 方案</h1>
  <p class="lead">同一套信息结构（通讯设置 / 轴移动控制 / 轴参数设定 / 矢网控制），五种视觉体系。
  全部为 1600×980 实际渲染，底色浅色，字体统一 Noto Sans SC（300/400/500/600/700 五档字重）。
  点任意图片可放大。</p>
  <div class="tabs">
    {nav}
    <button class="tab" id="toMatrix">五方案横向对比</button>
  </div>
  <p class="hintbar">提示：截图是静态的；交互原型分别是 scheme-1.html … scheme-5.html，可切换四个页面。</p>
</header>
<div class="wrap">
{sections}
<div id="matrix">
  <table class="matrix">
    <tbody>{matrix_rows}</tbody>
  </table>
</div>
</div>
<div id="lightbox"><img alt="放大预览"></div>
<script>
const tabs = [...document.querySelectorAll('.tab[data-s]')];
const schemes = [...document.querySelectorAll('.scheme')];
const matrix = document.getElementById('matrix');
const toMatrix = document.getElementById('toMatrix');

function show(key) {{
  schemes.forEach(s => s.classList.toggle('on', s.dataset.s === key));
  tabs.forEach(t => t.classList.toggle('on', t.dataset.s === key));
  matrix.classList.remove('on');
  window.scrollTo({{top: 0, behavior: 'smooth'}});
}}
tabs.forEach(t => t.onclick = () => show(t.dataset.s));
toMatrix.onclick = () => {{
  schemes.forEach(s => s.classList.remove('on'));
  tabs.forEach(t => t.classList.remove('on'));
  matrix.classList.add('on');
  toMatrix.classList.add('on');
}};
const box = document.getElementById('lightbox');
const big = box.querySelector('img');
document.addEventListener('click', ev => {{
  const img = ev.target.closest('img[data-s]');
  if (img) {{ big.src = img.src; box.classList.add('on'); }}
  else if (ev.target === box) {{ box.classList.remove('on'); }}
}});
document.addEventListener('keydown', ev => {{
  if (ev.key === 'Escape') box.classList.remove('on');
}});
show('scheme-1');
</script>
</body></html>'''
    OUT.write_text(html, encoding='utf-8')
    print('wrote', OUT.name, f'{OUT.stat().st_size / 1024 / 1024:.1f} MB')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
