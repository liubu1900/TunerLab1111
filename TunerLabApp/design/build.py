"""Generate five HTML design proposals for the "设备" pages.

Each file is a self-contained, clickable mock-up of the same four pages
(通讯设置 / 轴移动控制 / 轴参数设定 / 矢网控制) in a different visual system, so the
layouts can be compared side by side instead of argued about in the abstract.

    python design/build.py            # writes scheme-*.html
    python design/build.py --shots    # also renders PNGs with Edge headless

The mock-ups are prototypes: no Qt code, no data binding.  They exist to settle
layout, density, colour and typographic decisions before the real pages are
rewritten.
"""
from __future__ import annotations

import math
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
EDGE = Path(r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe')

NAV_ITEMS = [
    ('设备', ['通讯设置', '轴移动控制', '轴参数设定', '矢网控制']),
    ('校准', ['校准概览', '布点方案', '数据采集', '校准库', 'Smith 圆图', '调谐', '验证']),
    ('记录', ['操作日志', '设置与关于']),
]

AXES = [
    ('X', '1.0000', '1.0000', '已回零 · IN16=ON', 'ON'),
    ('Z1', '0.4000', '0.3999', '未回零', 'OFF'),
    ('Z2', '0.1000', '0.1000', '已回零 · IN18=ON', 'ON'),
    ('Z3', '0.0000', '0.0000', '已回零 · IN19=ON', 'ON'),
]


def trace_points(dip_ghz: float, floor_db: float, depth_db: float,
                 width_ghz: float, points: int = 260,
                 flare: float = 0.0) -> str:
    """SVG polyline points for a resonance, in a 0..100 × 0..100 viewport."""
    out = []
    for i in range(points):
        x = i / (points - 1)
        f = 0.4 + x * 19.6
        v = floor_db - depth_db / (1 + ((f - dip_ghz) / width_ghz) ** 2)
        v -= flare * x
        # SVG y grows downwards, dB grows upwards: a deeper notch must sit lower.
        y = (floor_db - v) / depth_db * 100
        out.append(f'{x * 100:.2f},{max(2, min(98, y)):.2f}')
    return ' '.join(out)


def trace_svg(name: str, colour: str, dip: float, floor: float, depth: float,
              width: float, flare: float = 0.0) -> str:
    return f'''
      <div class="trace">
        <div class="trace-head"><span class="dot" style="background:{colour}"></span>
          <b>{name}</b><span class="unit">dB</span></div>
        <svg viewBox="0 0 100 100" preserveAspectRatio="none" class="trace-body">
          <g class="grid">
            <line x1="0" y1="25" x2="100" y2="25"/><line x1="0" y1="50" x2="100" y2="50"/>
            <line x1="0" y1="75" x2="100" y2="75"/>
            <line x1="25" y1="0" x2="25" y2="100"/><line x1="50" y1="0" x2="50" y2="100"/>
            <line x1="75" y1="0" x2="75" y2="100"/>
          </g>
          <polyline points="{trace_points(dip, floor, depth, width, flare=flare)}"
                    fill="none" stroke="{colour}" stroke-width="1.1"
                    vector-effect="non-scaling-stroke"/>
        </svg>
        <div class="trace-axes"><span>0.4</span><span>10</span><span>20 GHz</span></div>
      </div>'''


def page_comm() -> str:
    return '''
    <div class="page" id="comm">
      <div class="cols two">
        <section class="card">
          <h3>控制器连接</h3>
          <div class="form">
            <label>控制器 IP</label><input value="192.168.0.11">
            <label>控制器模式</label>
            <select><option>真实控制器 (Zmotion)</option><option>虚拟控制器</option></select>
            <label>tuner 型号</label>
            <select><option>4 轴 tuner (X, Z1, Z2, Z3)</option></select>
            <label>启动</label>
            <span class="check on"><i></i>启动时自动连接</span>
          </div>
          <p class="hint ok">已保存：下次启动自动连接 192.168.0.11 · 真实控制器</p>
          <div class="btnrow">
            <button class="primary">连接</button>
            <button>断开</button><button>广播搜索</button>
          </div>
        </section>
        <section class="card">
          <h3>连接状态</h3>
          <dl class="stats">
            <dt>状态</dt><dd class="ok">已连接</dd>
            <dt>固件版本</dt><dd class="mono">2.9600</dd>
            <dt>控制器地址</dt><dd class="mono">192.168.0.11</dd>
            <dt>SDK 库</dt><dd class="mono small">vendor\\zmotion\\zauxdll.dll</dd>
            <dt>设置文件</dt><dd class="mono small">%APPDATA%\\TunerLab\\settings.json</dd>
          </dl>
          <div class="btnrow"><button>保存通讯设置</button><button>打开所在文件夹</button></div>
        </section>
      </div>
      <section class="card note">
        <h3>诊断</h3>
        <p>控制器走以太网 UDP 广播（ZAux_OpenEth），上位机需与控制器同网段。
        最近一次握手耗时 38 ms，丢包 0/20。</p>
      </section>
    </div>'''


def page_motion() -> str:
    cards = ''
    for name, dpos, mpos, homed, level in AXES:
        cards += f'''
        <div class="axis">
          <div class="axis-head"><b>{name}</b>
            <span class="lamp {'on' if level == 'ON' else ''}"></span>
            <span class="tiny">{'已回零' if level == 'ON' else '未回零'}</span></div>
          <div class="axis-body">
            <div class="bar"><span style="height:{20 + float(dpos) / 17.5 * 70:.0f}%"></span></div>
            <div class="axis-form">
              <div class="kv"><span>指令</span><b class="mono">{dpos}</b></div>
              <div class="kv"><span>反馈</span><b class="mono">{mpos}</b></div>
              <input class="mono" value="0.1000 mm">
              <div class="btnrow tight"><button>点动 −</button><button>点动 +</button></div>
              <input class="mono" value="0.0000 mm">
              <button class="primary">定位</button>
              <button>回零</button>
              <button class="ghost">标度自检</button>
            </div>
          </div>
        </div>'''
    rows = ''.join(
        f'<tr><td><b>{n}</b></td><td class="mono">{d}</td><td class="mono">{m}</td>'
        f'<td class="mono">{float(d) - float(m):+.4f}</td><td>是</td>'
        f'<td class="{"ok" if lv == "ON" else "warn"}">{h}</td>'
        f'<td class="mono">0</td><td>正常</td></tr>'
        for n, d, m, h, lv in AXES)
    return f'''
    <div class="page" id="motion">
      <section class="card">
        <h3>实时状态 <span class="hint">X 0–400 mm · Z 0–17.5 mm</span></h3>
        <table class="grid">
          <thead><tr><th>轴</th><th>指令 DPOS (mm)</th><th>反馈 MPOS (mm)</th>
            <th>跟随误差 (mm)</th><th>空闲</th><th>回零 / 原点</th><th>AXISSTATUS</th>
            <th>状态</th></tr></thead>
          <tbody>{rows}</tbody>
        </table>
        <p class="hint">原点输入：X=IN16:1  Z1=IN17:0  Z2=IN18:1  Z3=IN19:1</p>
      </section>
      <div class="axes">{cards}</div>
      <section class="card btnbar">
        <button class="danger">停止</button><button class="danger">急停</button>
        <button>复位急停</button><button>复位运动故障</button>
        <span class="hint">申报行程 (mm)：X 0–400 · Z1 0–17.5 · Z2 0–17.5 · Z3 0–17.5</span>
      </section>
    </div>'''


PARAM_ROWS = [
    ('X', '2500', '10', '10', '10', '5', '0', '1', '0', '16', '400', '0'),
    ('Z1', '10000', '10', '5', '5', '2', '0', '4', '0', '17', '17.5', '0'),
    ('Z2', '10000', '10', '5', '5', '2', '0', '4', '0', '18', '17.5', '0'),
    ('Z3', '10000', '10', '5', '5', '2', '0', '4', '0', '19', '17.5', '0'),
]


def page_params() -> str:
    head = ('<th>轴</th><th>ATYPE</th><th>UNITS</th><th>SPEED</th><th>ACCEL</th>'
            '<th>DECEL</th><th>CREEP</th><th>INVERT</th><th>DATUM<br>MODE</th>'
            '<th>DATUM<br>IN</th><th>FS_LIMIT</th><th>RS_LIMIT</th>')
    rows = ''.join(
        f'<tr><td><b>{r[0]}</b></td>' + ''.join(f'<td class="mono">{v}</td>' for v in r[1:])
        + '</tr>' for r in PARAM_ROWS)
    return f'''
    <div class="page" id="params">
      <section class="card">
        <div class="toolbar">
          <button class="primary">从控制器读取</button>
          <button>写入控制器</button>
          <span class="sep"></span>
          <button>保存到文件</button><button>从文件加载</button>
          <span class="sep"></span>
          <button>导出…</button><button>导入…</button>
          <span class="grow"></span><span class="hint">4 轴 · 12 项 · 未修改</span>
        </div>
        <table class="grid params">
          <thead><tr>{head}</tr></thead>
          <tbody>{rows}</tbody>
        </table>
        <p class="hint">参数修改后需「写入控制器」才下发；「保存到文件」只更新本机配置。
        编辑 UNITS / INVERT_STEP / DATUM_IN / DATUM_MODE 会清除该轴回零状态。</p>
      </section>
      <div class="cols three">
        <section class="card"><h3>坐标基准</h3>
          <p class="hint">UNITS 为脉冲/mm；MPOS 单位为 10 µm，DPOS 为 mm。</p></section>
        <section class="card"><h3>回零策略</h3>
          <p class="hint">Z 轴 DATUM_MODE=4（反向找原点开关），X 轴=1。</p></section>
        <section class="card"><h3>保护</h3>
          <p class="hint">控制器 FS/RS_LIMIT 仍为出厂 ±2e8，软件按申报行程拦截。</p></section>
      </div>
    </div>'''


def page_vna() -> str:
    traces = (trace_svg('S11', '#2f6f9f', 9.0, -34, 20, 0.25)
              + trace_svg('S21', '#1f7a4d', 9.0, -47, 44, 0.12, 0.4)
              + trace_svg('S12', '#b5651d', 9.0, -41, 38, 0.16, 0.3)
              + trace_svg('S22', '#8e44ad', 11.5, -30, 15, 0.35))
    return f'''
    <div class="page" id="vna">
      <div class="vna">
        <aside class="rail">
          <section class="card">
            <h3>仪器连接</h3>
            <div class="form">
              <label>厂商</label><select><option>自动识别 (*IDN?)</option></select>
              <label>IP 地址</label><input class="mono" value="192.168.0.50">
              <label>端口</label><input class="mono" value="5025">
              <label>超时</label><input class="mono" value="5000 ms">
            </div>
            <div class="btnrow"><button class="primary">连接并配置</button><button>断开</button></div>
          </section>
          <section class="card">
            <h3>扫描参数</h3>
            <div class="form">
              <label>起始</label><input class="mono" value="0.400000 GHz">
              <label>终止</label><input class="mono" value="20.000000 GHz">
              <label>点数</label><input class="mono" value="2000">
              <label>步进</label><input class="mono" value="9.8049 MHz">
              <label>功率</label><input class="mono" value="-5.00 dBm">
              <label>中频带宽</label><input class="mono" value="1000 Hz">
            </div>
            <p class="hint">跨度 19.6000 GHz · 2000 点 · 步进 9.8049 MHz</p>
            <div class="btnrow"><button class="primary">下发扫描参数</button></div>
          </section>
          <section class="card">
            <h3>仪器状态</h3>
            <dl class="stats">
              <dt>状态</dt><dd class="ok">已连接</dd>
              <dt>识别厂商</dt><dd>Keysight</dd>
              <dt>IDN</dt><dd class="mono small">Keysight,E5071C,MY12345678</dd>
            </dl>
          </section>
        </aside>
        <div class="work">
          <div class="toolbar">
            <button class="primary">单次扫描（4 路）</button>
            <button>读取错误队列</button><button>清除迹线</button>
            <span class="grow"></span><span class="hint">鼠标移到图内可读取频点与幅度</span>
          </div>
          <div class="traces">{traces}</div>
          <p class="hint">实测：2000 点 · 0.400000–20.000000 GHz · 步进 9.8049 MHz</p>
          <table class="grid dense">
            <thead><tr><th>频率 (GHz)</th><th>Δf (MHz)</th><th>|S11| dB</th>
              <th>|S21| dB</th><th>|S12| dB</th><th>|S22| dB</th></tr></thead>
            <tbody>
              <tr><td class="mono">0.400000</td><td class="mono">9.8049</td>
                <td class="mono">-18.21</td><td class="mono">-0.62</td>
                <td class="mono">-1.18</td><td class="mono">-20.04</td></tr>
              <tr><td class="mono">5.000000</td><td class="mono">9.8049</td>
                <td class="mono">-19.07</td><td class="mono">-0.71</td>
                <td class="mono">-1.25</td><td class="mono">-20.11</td></tr>
              <tr><td class="mono">9.000000</td><td class="mono">9.8049</td>
                <td class="mono">-33.84</td><td class="mono">-46.71</td>
                <td class="mono">-40.92</td><td class="mono">-21.66</td></tr>
              <tr><td class="mono">14.000000</td><td class="mono">9.8049</td>
                <td class="mono">-18.65</td><td class="mono">-0.83</td>
                <td class="mono">-1.31</td><td class="mono">-20.28</td></tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>'''


BASE_CSS = '''
*{box-sizing:border-box}
html,body{margin:0;height:100%}
body{font-family:"Noto Sans SC","Microsoft YaHei",sans-serif;font-size:13px;font-weight:400;-webkit-font-smoothing:antialiased}
.mono{font-family:"Noto Sans SC","Microsoft YaHei",sans-serif;font-variant-numeric:tabular-nums;font-weight:500}
button{font:inherit;font-weight:500;padding:5px 12px;border-radius:4px;cursor:pointer}
button.primary{font-weight:700}
input,select{font:inherit;padding:4px 7px;width:100%}
table{width:100%;border-collapse:collapse}
th,td{padding:5px 8px;text-align:right;font-weight:400}
th:first-child,td:first-child{text-align:left}
.card h3{margin:0 0 10px;font-size:12.5px;letter-spacing:.02em;font-weight:600}
.form{display:grid;grid-template-columns:84px 1fr;gap:8px 10px;align-items:center}
.form label{font-size:12px;opacity:.75;font-weight:400}
.hint{font-size:11.5px;opacity:.62;margin:6px 0;font-weight:300}
.hint.ok{color:#1f7a4d;opacity:1}
.btnrow{display:flex;gap:8px;margin-top:12px;flex-wrap:wrap}
.btnrow.tight{gap:5px}
.btnrow.tight button{flex:1;padding:4px 6px}
.toolbar{display:flex;gap:8px;align-items:center;margin-bottom:10px;flex-wrap:wrap}
.toolbar .grow,.grow{flex:1}
.sep{width:1px;height:18px;background:currentColor;opacity:.15}
'''


SCHEMES = {
    'scheme-1': {
        'name': '方案 1 · 浅色仪器面板（Light Instrument）',
        'desc': '浅灰面板 + 硬边框 + 等宽数字 + 青绿状态色；保留仪器前面板的秩序感，但在明亮实验室里不刺眼。',
        'css': '''
body{background:#eceff3;color:#1c2a36}
.app{display:grid;grid-template-columns:78px 1fr;height:100vh}
.rail-nav{background:#e2e7ed;border-right:1px solid #c9d2dc;padding:10px 0;
  display:flex;flex-direction:column;align-items:center;gap:6px}
.rail-nav .logo{width:34px;height:34px;border-radius:6px;background:#0f7b8a;
  color:#fff;font-weight:700;display:grid;place-items:center;margin-bottom:10px}
.rail-nav a{width:58px;padding:9px 0;text-align:center;font-size:10.5px;color:#5d7183;
  border-radius:5px;text-decoration:none}
.rail-nav a.active{background:#fff;color:#0f6d7a;box-shadow:inset 0 0 0 1px #c9d2dc;
  font-weight:600}
.shell{display:flex;flex-direction:column;min-width:0}
.topbar{height:44px;display:flex;align-items:center;gap:14px;padding:0 16px;
  border-bottom:1px solid #c9d2dc;background:#f6f8fa}
.topbar b{font-size:13px;color:#14313f;letter-spacing:.03em}
.topbar .crumb{font-size:11.5px;color:#6b7f90}
.topbar .grow{flex:1}
.badge{font-size:11px;padding:3px 9px;border-radius:3px;background:#e6f4f1;
  color:#0f6d4a;border:1px solid #bcdcd2}
.page{display:none;padding:12px 14px 14px;gap:12px;flex-direction:column;
  flex:1;min-height:0;overflow:auto}
.page.on{display:flex}
.card{background:#fff;border:1px solid #c9d2dc;border-radius:3px;padding:12px 14px}
.card h3{color:#4a6072;text-transform:uppercase;letter-spacing:.09em;font-size:10.5px;
  border-bottom:1px solid #e3e8ee;padding-bottom:6px}
table.grid{font-size:12px}
table.grid th{background:#eaeff4;color:#3f5568;font-weight:600;font-size:11px;
  border-bottom:1px solid #c9d2dc;padding:6px 8px}
table.grid td{border-bottom:1px solid #e6ebf1;color:#22323f}
table.grid tbody tr:nth-child(even) td{background:#fafbfd}
.ok{color:#0f7a4d;font-weight:600}.warn{color:#a8661a;font-weight:600}
input,select{background:#fff;border:1px solid #c2ccd7;color:#1c2a36;border-radius:2px}
input:focus,select:focus{outline:none;border-color:#0f7b8a;box-shadow:0 0 0 2px #dcefF2}
button{background:#f4f7fa;border:1px solid #c2ccd7;color:#22323f;border-radius:2px}
button:hover{background:#e9eff5}
button.primary{background:#0f7b8a;border-color:#0f7b8a;color:#fff;font-weight:600}
button.danger{background:#fdf1ef;border-color:#dfb4ad;color:#a3382c}
button.ghost{background:transparent}
.cols{display:grid;gap:12px}
.cols.two{grid-template-columns:1.15fr 1fr}
.cols.three{grid-template-columns:repeat(3,1fr)}
.stats{display:grid;grid-template-columns:96px 1fr;gap:7px 12px;margin:0}
.stats dt{color:#627687;font-size:12px}.stats dd{margin:0;color:#22323f}
.stats dd.small{font-size:11px;color:#5b6b7c}
.check{display:inline-flex;align-items:center;gap:8px;color:#22323f;font-size:12.5px}
.check i{width:15px;height:15px;border:1px solid #b4c0cc;border-radius:2px;
  display:inline-block;background:#fff}
.check.on i{background:#0f7b8a;border-color:#0f7b8a;position:relative}
.check.on i::after{content:"";position:absolute;left:4px;top:1px;width:4px;height:8px;
  border:2px solid #fff;border-top:0;border-left:0;transform:rotate(42deg)}
.axes{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}
.axis{background:#fff;border:1px solid #c9d2dc;border-radius:3px;padding:10px}
.axis-head{display:flex;align-items:center;gap:8px;margin-bottom:8px}
.axis-head b{color:#14313f;font-size:13px}
.lamp{width:9px;height:9px;border-radius:50%;background:#c0392b;margin-left:auto}
.lamp.on{background:#12a06a;box-shadow:0 0 0 2px #d9f0e6}
.tiny{font-size:11px;color:#627687}
.axis-body{display:flex;gap:10px}
.bar{width:16px;height:150px;background:#eef2f6;border:1px solid #d6dee6;
  display:flex;align-items:flex-end;border-radius:2px}
.bar span{display:block;width:100%;background:linear-gradient(#18a0b0,#0f6d7a)}
.axis-form{flex:1;display:flex;flex-direction:column;gap:6px}
.kv{display:flex;justify-content:space-between;font-size:11.5px;color:#627687}
.kv b{color:#22323f}
.btnbar{display:flex;gap:8px;align-items:center}
.vna{display:grid;grid-template-columns:330px 1fr;gap:12px;min-height:0}
.rail{display:flex;flex-direction:column;gap:12px;overflow:auto}
.work{display:flex;flex-direction:column;gap:10px;min-width:0}
.traces{display:grid;grid-template-columns:1fr 1fr;gap:10px;flex:none;height:min(57vh,560px)}
.trace{background:#fff;border:1px solid #c9d2dc;border-radius:3px;
  display:flex;flex-direction:column;padding:8px 10px 4px;min-height:150px}
.trace-head{display:flex;align-items:center;gap:7px;font-size:11.5px;color:#4a6072;
  padding-bottom:6px;border-bottom:1px solid #e6ebf1;margin-bottom:6px}
.trace-head b{color:#14313f;font-size:12px}
.trace-head .unit{opacity:.55;font-size:10.5px}
.dot{width:8px;height:8px;border-radius:2px;display:inline-block}
.trace-body{flex:1;width:100%;min-height:100px}
.trace .grid line{stroke:#eef2f6;stroke-width:.7}
.trace-axes{display:flex;justify-content:space-between;font-size:10px;color:#7b8a99}
.runtime{display:flex}
table.dense th,table.dense td{padding:3px 8px;font-size:11.5px}
.work table.grid{max-height:19vh;overflow:auto;display:block}
''',
        'nav': '''
<nav class="rail-nav">
  <div class="logo">TL</div>
  <a class="active">设备</a><a>校准</a><a>记录</a>
</nav>''',
        'topbar': '<div class="topbar"><b id="page-title">通讯设置</b><span class="crumb" id="page-crumb">设备 / 通讯设置</span>'
                  '<span class="grow"></span><span class="badge">已连接 192.168.0.11</span></div>',
        'tabs': True,
    },
    'scheme-2': {
        'name': '方案 2 · 浅色工程卡片（Light Cards）',
        'desc': '白底卡片 + 细分隔线 + 蓝色主色，信息分组清晰，适合长时间在明亮实验室使用。',
        'css': '''
body{background:#f3f5f8;color:#1f2937}
.app{display:grid;grid-template-columns:212px 1fr;height:100vh}
.side{background:#ffffff;border-right:1px solid #e2e8f0;padding:14px 10px}
.side .brand{display:flex;align-items:center;gap:9px;padding:4px 8px 16px;font-weight:700;
  color:#12324a;font-size:14px}
.side .brand i{width:24px;height:24px;border-radius:6px;background:#2f6f9f;display:block}
.side .group{font-size:10.5px;letter-spacing:.09em;text-transform:uppercase;color:#94a3b8;
  padding:12px 10px 6px}
.side a{display:block;padding:7px 10px;border-radius:5px;color:#334155;text-decoration:none;
  font-size:12.5px}
.side a.active{background:#eaf2f9;color:#1d4f74;font-weight:600}
.shell{display:flex;flex-direction:column;min-width:0;background:#f3f5f8}
.topbar{height:50px;background:#fff;border-bottom:1px solid #e2e8f0;display:flex;
  align-items:center;gap:12px;padding:0 20px}
.topbar b{font-size:15px;color:#0f2b3f}
.topbar .crumb{font-size:12px;color:#94a3b8}
.topbar .grow{flex:1}
.badge{font-size:11.5px;padding:4px 10px;border-radius:20px;background:#e8f6ef;color:#1f7a4d;
  border:1px solid #c9ebd9}
.page{display:none;padding:14px 20px 16px;gap:14px;flex-direction:column;
  flex:1;min-height:0;overflow:auto}
.page.on{display:flex}
.card{background:#fff;border:1px solid #e2e8f0;border-radius:8px;padding:14px 16px;
  box-shadow:0 1px 2px rgba(15,43,63,.04)}
.card h3{margin:0 0 12px;font-size:13px;color:#12324a}
.card.note{border-style:dashed}
table.grid{font-size:12.5px}
table.grid th{background:#f7f9fc;color:#64748b;font-weight:600;font-size:11.5px;
  border-bottom:1px solid #e2e8f0;padding:8px}
table.grid td{border-bottom:1px solid #eef2f7;color:#243746}
table.grid tbody tr:nth-child(even){background:#fbfcfe}
.ok{color:#1f7a4d;font-weight:600}.warn{color:#b5651d;font-weight:600}
input,select{background:#fff;border:1px solid #cfd9e4;color:#1f2937;border-radius:5px;padding:5px 8px}
input:focus,select:focus{outline:none;border-color:#2f6f9f;box-shadow:0 0 0 3px #e3eefa}
button{background:#fff;border:1px solid #cfd9e4;color:#243746;border-radius:5px}
button:hover{background:#f5f8fc}
button.primary{background:#2f6f9f;border-color:#2f6f9f;color:#fff}
button.danger{background:#fff;border-color:#e6b3ad;color:#b03a2e}
button.ghost{background:#f8fafc}
.cols{display:grid;gap:14px}
.cols.two{grid-template-columns:1.1fr 1fr}
.cols.three{grid-template-columns:repeat(3,1fr)}
.stats{display:grid;grid-template-columns:100px 1fr;gap:9px 14px;margin:0}
.stats dt{color:#7b8a99;font-size:12px}.stats dd{margin:0;color:#1f2937}
.stats dd.small{font-size:11.5px;color:#5b6b7c}
.check{display:inline-flex;align-items:center;gap:8px;color:#243746;font-size:12.5px}
.check i{width:16px;height:16px;border:1px solid #cfd9e4;border-radius:4px;background:#fff;
  display:inline-block}
.check.on i{background:#2f6f9f;border-color:#2f6f9f;position:relative}
.check.on i::after{content:"";position:absolute;left:4px;top:1px;width:4px;height:8px;
  border:2px solid #fff;border-top:0;border-left:0;transform:rotate(42deg)}
.axes{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}
.axis{background:#fff;border:1px solid #e2e8f0;border-radius:8px;padding:12px}
.axis-head{display:flex;align-items:center;gap:8px;margin-bottom:10px}
.axis-head b{font-size:13.5px;color:#12324a}
.lamp{width:9px;height:9px;border-radius:50%;background:#d9534f;margin-left:auto}
.lamp.on{background:#2fa36b}
.tiny{font-size:11px;color:#7b8a99}
.axis-body{display:flex;gap:10px}
.bar{width:16px;height:150px;background:#f1f5f9;border:1px solid #e2e8f0;border-radius:3px;
  display:flex;align-items:flex-end}
.bar span{display:block;width:100%;background:#2f6f9f;border-radius:2px}
.axis-form{flex:1;display:flex;flex-direction:column;gap:6px}
.kv{display:flex;justify-content:space-between;font-size:11.5px;color:#7b8a99}
.kv b{color:#243746}
.vna{display:grid;grid-template-columns:330px 1fr;gap:14px;min-height:0}
.rail{display:flex;flex-direction:column;gap:12px;overflow:auto}
.work{display:flex;flex-direction:column;gap:10px;min-width:0}
.traces{display:grid;grid-template-columns:1fr 1fr;gap:10px;flex:none;height:min(57vh,560px)}
.trace{background:#fff;border:1px solid #e2e8f0;border-radius:8px;display:flex;
  flex-direction:column;padding:8px 10px 4px;min-height:0}
.trace-head{display:flex;align-items:center;gap:7px;font-size:11.5px;color:#5b6b7c;
  padding-bottom:6px;border-bottom:1px solid #eef2f7;margin-bottom:6px}
.trace-head b{color:#12324a;font-size:12px}
.trace-head .unit{opacity:.55;font-size:10.5px}
.dot{width:8px;height:8px;border-radius:2px;display:inline-block}
.trace-body{flex:1;min-height:100px}
.trace .grid line{stroke:#eef2f7;stroke-width:.8}
.trace-axes{display:flex;justify-content:space-between;font-size:10px;color:#94a3b8}
table.dense th,table.dense td{padding:4px 8px;font-size:11.5px}
.work table.grid{max-height:23vh;overflow:auto;display:block}
''',
        'nav': '''
<aside class="side">
  <div class="brand"><i></i>TunerLab</div>
  <div class="group">设备</div>
  <a class="active">通讯设置</a><a>轴移动控制</a><a>轴参数设定</a><a>矢网控制</a>
  <div class="group">校准</div>
  <a>校准概览</a><a>布点方案</a><a>数据采集</a>
  <div class="group">记录</div><a>操作日志</a><a>设置与关于</a>
</aside>''',
        'topbar': '<div class="topbar"><b id="page-title">通讯设置</b><span class="crumb" id="page-crumb">设备</span>'
                  '<span class="grow"></span><span class="badge">● 已连接</span></div>',
        'tabs': True,
    },
    'scheme-3': {
        'name': '方案 3 · 双栏工作台（Workbench）',
        'desc': '无全局侧栏，顶部页签 + 左参数栏 / 右工作区；像 PathWave 一类仪器软件，屏幕利用率最高。',
        'css': '''
body{background:#eef1f5;color:#1e2a35}
.app{display:flex;flex-direction:column;height:100vh}
.topnav{height:52px;background:#16324a;color:#dbe7f1;display:flex;align-items:center;
  gap:20px;padding:0 18px}
.topnav .brand{font-weight:700;letter-spacing:.04em;color:#fff}
.topnav .tabs{display:flex;gap:4px}
.topnav .tabs span{padding:6px 14px;border-radius:4px;font-size:12.5px;color:#b7cddd}
.topnav .tabs span.active{background:#265273;color:#fff}
.topnav .grow{flex:1}
.topnav .badge{font-size:11.5px;color:#8fe3bd}
.page{display:none;flex:1;min-height:0;padding:10px 12px 12px;gap:10px;flex-direction:column;overflow:hidden}
.page.on{display:flex}
.card{background:#fff;border:1px solid #d8e0e8;border-radius:4px;padding:12px 14px}
.card h3{margin:0 0 10px;font-size:12px;color:#29465f;letter-spacing:.03em;
  text-transform:uppercase}
table.grid{font-size:12px}
table.grid th{background:#f2f6fa;color:#51677b;font-size:11px;font-weight:600;
  border-bottom:1px solid #d8e0e8;padding:6px 8px}
table.grid td{border-bottom:1px solid #eaeff5;color:#243746}
.ok{color:#1f7a4d;font-weight:600}.warn{color:#b5651d;font-weight:600}
input,select{border:1px solid #c9d4df;border-radius:3px;background:#fff;padding:4px 7px}
button{background:#f6f9fc;border:1px solid #c9d4df;border-radius:3px;color:#23384b}
button.primary{background:#1f5f8b;border-color:#1f5f8b;color:#fff}
button.danger{background:#fff;border-color:#dcb0ab;color:#a3382c}
button.ghost{background:transparent;border-color:transparent;color:#4a6b85;text-decoration:underline}
.cols{display:grid;gap:10px}
.cols.two{grid-template-columns:340px 1fr}
.cols.three{grid-template-columns:repeat(3,1fr)}
.stats{display:grid;grid-template-columns:92px 1fr;gap:8px 12px;margin:0}
.stats dt{color:#6a7f92;font-size:12px}.stats dd{margin:0}
.stats dd.small{font-size:11px;color:#5b6b7c}
.check{display:inline-flex;align-items:center;gap:8px;font-size:12.5px}
.check i{width:15px;height:15px;border:1px solid #b9c6d3;border-radius:2px;background:#fff;
  display:inline-block}
.check.on i{background:#1f5f8b;border-color:#1f5f8b;position:relative}
.check.on i::after{content:"";position:absolute;left:4px;top:1px;width:4px;height:8px;
  border:2px solid #fff;border-top:0;border-left:0;transform:rotate(42deg)}
.axes{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px;flex:1;min-height:0}
.axis{background:#fff;border:1px solid #d8e0e8;border-radius:4px;padding:10px;
  display:flex;flex-direction:column}
.axis-head{display:flex;align-items:center;gap:8px;margin-bottom:8px}
.axis-head b{color:#16324a;font-size:13px}
.lamp{width:8px;height:8px;border-radius:50%;background:#c0392b;margin-left:auto}
.lamp.on{background:#1f9d63}
.tiny{font-size:10.5px;color:#6a7f92}
.axis-body{display:flex;gap:9px;flex:1}
.bar{width:14px;background:#eef3f8;border:1px solid #dbe4ec;display:flex;align-items:flex-end}
.bar span{display:block;width:100%;background:#2f8fbf}
.axis-form{flex:1;display:flex;flex-direction:column;gap:5px}
.kv{display:flex;justify-content:space-between;font-size:11px;color:#6a7f92}
.kv b{color:#1e2a35}
.vna{display:flex;flex:1;gap:10px;min-height:0}
.rail{width:330px;display:flex;flex-direction:column;gap:10px;overflow:auto;flex:none}
.work{flex:1;display:flex;flex-direction:column;gap:8px;min-width:0}
.traces{display:grid;grid-template-columns:1fr 1fr;gap:8px;flex:none;height:min(57vh,560px)}
.trace{background:#fff;border:1px solid #d8e0e8;display:flex;flex-direction:column;
  padding:6px 9px 3px;min-height:0}
.trace-head{display:flex;align-items:center;gap:6px;font-size:11px;color:#5b6b7c;
  border-bottom:1px solid #eef2f7;padding-bottom:5px;margin-bottom:5px}
.trace-head b{color:#16324a;font-size:11.5px}
.trace-head .unit{opacity:.5;font-size:10px}
.dot{width:7px;height:7px;display:inline-block}
.trace-body{flex:1;min-height:90px}
.trace .grid line{stroke:#eef2f7}
.trace-axes{display:flex;justify-content:space-between;font-size:9.5px;color:#8a9aa8}
table.dense th,table.dense td{padding:3px 8px;font-size:11px}
.work table.grid{max-height:19vh;overflow:auto;display:block}
''',
        'nav': '''
<header class="topnav">
  <span class="brand">TunerLab</span>
  <div class="tabs"><span class="active">设备</span><span>校准</span><span>记录</span></div>
  <span class="grow"></span><span class="badge">● 控制器 2.9600</span>
</header>''',
        'topbar': '',
        'tabs': True,
    },
    'scheme-4': {
        'name': '方案 4 · 高密度 HMI（Dense HMI）',
        'desc': '12px 等宽、表格优先、无卡片留白，一屏放下全部参数与 4 条迹线；适合 1366×768 工控机。',
        'css': '''
body{background:#e9edf1;color:#101b24;font-size:12px}
.app{display:grid;grid-template-columns:170px 1fr;height:100vh}
.tree{background:#dfe5eb;border-right:1px solid #c3ccd6;padding:8px 0;font-size:11.5px}
.tree .brand{padding:6px 12px 10px;font-weight:700;color:#173a52;font-size:13px}
.tree .grp{padding:8px 12px 3px;color:#5d7183;font-size:10px;letter-spacing:.08em}
.tree a{display:block;padding:4px 12px 4px 20px;color:#233746;text-decoration:none}
.tree a.active{background:#c9d8e5;color:#0d2f46;font-weight:600;
  box-shadow:inset 3px 0 0 #1f6fa0}
.shell{display:flex;flex-direction:column;min-width:0}
.topbar{height:30px;background:#d5dde5;border-bottom:1px solid #bcc7d2;display:flex;
  align-items:center;padding:0 10px;gap:10px;font-size:11.5px}
.topbar b{color:#0d2f46}.topbar .grow{flex:1}
.topbar .badge{color:#1f7a4d;font-weight:600}
.page{display:none;padding:6px 8px 8px;gap:6px;flex-direction:column;
  flex:1;min-height:0;overflow:auto}
.page.on{display:flex}
.card{background:#f7f9fb;border:1px solid #c3ccd6;padding:6px 8px}
.card h3{margin:0 0 6px;font-size:11px;color:#3d566b;text-transform:uppercase;
  letter-spacing:.06em;border-bottom:1px solid #d5dde5;padding-bottom:4px}
table.grid{font-size:11.5px}
table.grid th{background:#e3e9ef;color:#33475a;font-size:10.5px;padding:3px 6px;
  border:1px solid #c9d2dc;font-weight:600}
table.grid td{border:1px solid #d9e0e7;padding:3px 6px;background:#fff}
table.grid tbody tr:nth-child(even) td{background:#f4f7fa}
.ok{color:#1f7a4d;font-weight:700}.warn{color:#b5651d;font-weight:700}
input,select{border:1px solid #b9c6d3;background:#fff;padding:2px 5px;border-radius:2px;
  font-size:11.5px}
button{background:#eef2f6;border:1px solid #b9c6d3;border-radius:2px;padding:3px 9px;
  font-size:11.5px;color:#1e3345}
button.primary{background:#1f6fa0;border-color:#1f6fa0;color:#fff}
button.danger{background:#fdeceb;border-color:#d9a9a4;color:#a3382c}
button.ghost{background:transparent}
.cols{display:grid;gap:6px}
.cols.two{grid-template-columns:1fr 1fr}
.cols.three{grid-template-columns:repeat(3,1fr)}
.stats{display:grid;grid-template-columns:78px 1fr;gap:3px 8px;margin:0;font-size:11.5px}
.stats dt{color:#5d7183}.stats dd{margin:0}
.stats dd.small{font-size:10.5px}
.check{display:inline-flex;align-items:center;gap:6px;font-size:11.5px}
.check i{width:13px;height:13px;border:1px solid #9fb0c0;background:#fff;display:inline-block}
.check.on i{background:#1f6fa0;border-color:#1f6fa0;position:relative}
.check.on i::after{content:"";position:absolute;left:3px;top:0;width:3px;height:7px;
  border:2px solid #fff;border-top:0;border-left:0;transform:rotate(42deg)}
.axes{display:grid;grid-template-columns:repeat(4,1fr);gap:6px}
.axis{background:#f7f9fb;border:1px solid #c3ccd6;padding:6px 7px}
.axis-head{display:flex;align-items:center;gap:6px;font-size:12px;margin-bottom:4px}
.axis-head b{color:#0d2f46}
.lamp{width:7px;height:7px;border-radius:50%;background:#c0392b;margin-left:auto}
.lamp.on{background:#1f9d63}
.tiny{font-size:10px;color:#5d7183}
.axis-body{display:flex;gap:6px}
.bar{width:12px;height:118px;background:#e3e9ef;border:1px solid #c9d2dc;display:flex;
  align-items:flex-end}
.bar span{display:block;width:100%;background:#2f8fbf}
.axis-form{flex:1;display:flex;flex-direction:column;gap:3px}
.kv{display:flex;justify-content:space-between;font-size:10.5px;color:#5d7183}
.kv b{color:#101b24}
.vna{display:grid;grid-template-columns:296px 1fr;gap:6px;flex:1;min-height:0}
.rail{display:flex;flex-direction:column;gap:6px;overflow:auto}
.work{display:flex;flex-direction:column;gap:6px;min-width:0}
.traces{display:grid;grid-template-columns:1fr 1fr;gap:6px;flex:none;height:min(50vh,430px)}
.trace{background:#fff;border:1px solid #c3ccd6;display:flex;flex-direction:column;
  padding:4px 7px 2px;min-height:0}
.trace-head{display:flex;align-items:center;gap:5px;font-size:10.5px;color:#3d566b;
  border-bottom:1px solid #dde4ea;padding-bottom:3px;margin-bottom:3px}
.trace-head b{font-size:11px;color:#0d2f46}
.trace-head .unit{opacity:.55;font-size:9.5px}
.dot{width:6px;height:6px;display:inline-block}
.trace-body{flex:1;min-height:78px}
.trace .grid line{stroke:#eaf0f5}
.trace-axes{display:flex;justify-content:space-between;font-size:9px;color:#7b8a99}
table.dense th,table.dense td{padding:2px 6px;font-size:10.5px}
.work table.grid{max-height:17vh;overflow:auto;display:block}
''',
        'nav': '''
<aside class="tree">
  <div class="brand">TunerLab 0.3</div>
  <div class="grp">设备</div>
  <a class="active">通讯设置</a><a>轴移动控制</a><a>轴参数设定</a><a>矢网控制</a>
  <div class="grp">校准</div><a>概览</a><a>布点</a><a>采集</a><a>库</a><a>调谐</a>
  <div class="grp">记录</div><a>操作日志</a>
</aside>''',
        'topbar': '<div class="topbar"><b>通讯设置</b><span>设备 / 通讯设置</span>'
                  '<span class="grow"></span><span class="badge">已连接 · 2.9600</span></div>',
        'tabs': True,
    },
    'scheme-5': {
        'name': '方案 5 · 仪表盘（Dashboard）',
        'desc': '顶部 KPI 指标条 + 大面积图形区，圆角与留白最多；演示与巡检友好，参数密度最低。',
        'css': '''
body{background:linear-gradient(180deg,#f7f9fc,#eef2f8 380px);color:#16233a}
.app{display:grid;grid-template-columns:64px 1fr;height:100vh}
.iconrail{background:#101b2d;padding:12px 0;display:flex;flex-direction:column;
  align-items:center;gap:10px}
.iconrail .logo{width:34px;height:34px;border-radius:10px;
  background:linear-gradient(135deg,#4f8ef7,#7b5cf0);display:grid;place-items:center;
  color:#fff;font-weight:700;margin-bottom:8px}
.iconrail a{width:40px;height:34px;border-radius:9px;display:grid;place-items:center;
  color:#8ea3bd;font-size:10px;text-decoration:none}
.iconrail a.active{background:#1c2c46;color:#7fb0ff}
.shell{display:flex;flex-direction:column;min-width:0}
.topbar{height:56px;display:flex;align-items:center;gap:14px;padding:0 22px}
.topbar b{font-size:17px;letter-spacing:-.01em}
.topbar .crumb{font-size:12px;color:#8496ad}
.topbar .grow{flex:1}
.badge{font-size:11.5px;padding:5px 12px;border-radius:20px;background:#fff;
  border:1px solid #e2e9f3;color:#1f7a4d;box-shadow:0 1px 2px rgba(16,27,45,.05)}
.page{display:none;padding:0 22px 16px;gap:14px;flex-direction:column;
  flex:1;min-height:0;overflow:auto}
.page.on{display:flex}
.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}
.kpi{background:#fff;border:1px solid #e6ecf5;border-radius:12px;padding:12px 14px;
  box-shadow:0 2px 6px rgba(16,27,45,.04)}
.kpi .label{font-size:11.5px;color:#8496ad}
.kpi .value{font-size:21px;font-weight:650;margin-top:4px;letter-spacing:-.02em}
.kpi .sub{font-size:11px;color:#9aa9bd;margin-top:2px}
.card{background:#fff;border:1px solid #e6ecf5;border-radius:12px;padding:14px 16px;
  box-shadow:0 2px 6px rgba(16,27,45,.04)}
.card h3{margin:0 0 12px;font-size:13px;color:#20304a}
table.grid{font-size:12.5px}
table.grid th{background:#f6f8fc;color:#6b7d95;font-size:11.5px;font-weight:600;
  border-bottom:1px solid #e6ecf5;padding:8px}
table.grid td{border-bottom:1px solid #f0f4fa;color:#22304a}
.ok{color:#12996a;font-weight:600}.warn{color:#c07a12;font-weight:600}
input,select{border:1px solid #dbe4f0;border-radius:8px;background:#fbfcfe;padding:6px 9px}
input:focus,select:focus{outline:none;border-color:#4f8ef7;box-shadow:0 0 0 3px #e8f0ff}
button{background:#fff;border:1px solid #dbe4f0;border-radius:8px;color:#22304a;
  padding:6px 14px}
button.primary{background:linear-gradient(135deg,#4f8ef7,#6a6ef0);border-color:transparent;
  color:#fff;font-weight:600;box-shadow:0 2px 6px rgba(79,142,247,.3)}
button.danger{background:#fff5f4;border-color:#f3cdc8;color:#c0392b}
button.ghost{background:#f6f9fe;border-color:transparent;color:#3b6fb8}
.cols{display:grid;gap:14px}
.cols.two{grid-template-columns:1.05fr 1fr}
.cols.three{grid-template-columns:repeat(3,1fr)}
.stats{display:grid;grid-template-columns:100px 1fr;gap:10px 14px;margin:0}
.stats dt{color:#8496ad;font-size:12px}.stats dd{margin:0}
.stats dd.small{font-size:11.5px;color:#6b7d95}
.check{display:inline-flex;align-items:center;gap:9px;font-size:12.5px}
.check i{width:34px;height:19px;border-radius:11px;background:#dbe4f0;display:inline-block;
  position:relative;transition:.2s}
.check i::after{content:"";position:absolute;left:2px;top:2px;width:15px;height:15px;
  border-radius:50%;background:#fff;box-shadow:0 1px 2px rgba(0,0,0,.2)}
.check.on i{background:linear-gradient(135deg,#4f8ef7,#6a6ef0)}
.check.on i::after{left:17px}
.axes{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}
.axis{background:#fff;border:1px solid #e6ecf5;border-radius:12px;padding:12px;
  box-shadow:0 2px 6px rgba(16,27,45,.04)}
.axis-head{display:flex;align-items:center;gap:8px;margin-bottom:10px}
.axis-head b{font-size:13.5px}
.lamp{width:9px;height:9px;border-radius:50%;background:#e05e52;margin-left:auto}
.lamp.on{background:#20b57f;box-shadow:0 0 0 3px #d8f3e8}
.tiny{font-size:11px;color:#8496ad}
.axis-body{display:flex;gap:10px}
.bar{width:16px;height:146px;background:#f2f6fc;border-radius:8px;display:flex;
  align-items:flex-end;overflow:hidden}
.bar span{display:block;width:100%;background:linear-gradient(180deg,#4f8ef7,#7b5cf0)}
.axis-form{flex:1;display:flex;flex-direction:column;gap:6px}
.kv{display:flex;justify-content:space-between;font-size:11.5px;color:#8496ad}
.kv b{color:#22304a}
.vna{display:grid;grid-template-columns:322px 1fr;gap:14px;min-height:0}
.rail{display:flex;flex-direction:column;gap:12px;overflow:auto}
.work{display:flex;flex-direction:column;gap:10px;min-width:0}
.traces{display:grid;grid-template-columns:1fr 1fr;gap:10px;flex:none;height:min(57vh,560px)}
.trace{background:#fff;border:1px solid #e6ecf5;border-radius:12px;display:flex;
  flex-direction:column;padding:8px 11px 4px;min-height:150px;
  box-shadow:0 2px 6px rgba(16,27,45,.04)}
.trace-head{display:flex;align-items:center;gap:7px;font-size:11.5px;color:#6b7d95;
  padding-bottom:6px;border-bottom:1px solid #f0f4fa;margin-bottom:5px}
.trace-head b{color:#20304a;font-size:12px}
.trace-head .unit{opacity:.5;font-size:10.5px}
.dot{width:8px;height:8px;border-radius:50%;display:inline-block}
.trace-body{flex:1;min-height:96px}
.trace .grid line{stroke:#f0f4fa}
.trace-axes{display:flex;justify-content:space-between;font-size:10px;color:#9aa9bd}
table.dense th,table.dense td{padding:3px 8px;font-size:11.5px}
.work table.grid{max-height:19vh;overflow:auto;display:block}
''',
        'nav': '''
<aside class="iconrail">
  <div class="logo">TL</div>
  <a class="active">设备</a><a>校准</a><a>记录</a>
</aside>''',
        'topbar': '<div class="topbar"><b id="page-title">通讯设置</b><span class="crumb" id="page-crumb">设备</span>'
                  '<span class="grow"></span><span class="badge">● 已连接 192.168.0.11</span></div>',
        'tabs': True,
    },
}


PAGES = [
    ('comm', '通讯设置', page_comm),
    ('motion', '轴移动控制', page_motion),
    ('params', '轴参数设定', page_params),
    ('vna', '矢网控制', page_vna),
]


def render(scheme_key: str) -> str:
    scheme = SCHEMES[scheme_key]
    pages = [fn() for _, _, fn in PAGES]
    # Only the first page is visible until the hash says otherwise.
    pages_html = ''
    for index, html in enumerate(pages):
        if index == 0:
            html = html.replace('class="page"', 'class="page on"', 1)
        pages_html += html
    tabs = ''
    if scheme['tabs']:
        tabs = ('<div id="tabs" style="display:flex;gap:6px;padding:8px 18px 0">'
                + ''.join(f'<button data-p="{key}" class="tabbtn">{label}</button>'
                          for key, label, _ in PAGES)
                + '</div>')
    return f'''<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>{scheme['name']}</title>
<style>{BASE_CSS}{scheme['css']}
#tabs .tabbtn{{opacity:.7}}
#tabs .tabbtn.on{{opacity:1;font-weight:600}}
</style></head>
<body>
<div class="app">
{scheme['nav']}
<div class="shell">
{scheme['topbar']}
{tabs}
{pages_html}
</div></div>
<script>
const TITLES = {{comm:'通讯设置', motion:'轴移动控制', params:'轴参数设定', vna:'矢网控制'}};
function applyTitle(name) {{
  const t = document.getElementById('page-title');
  if (t) t.textContent = TITLES[name] || '';
  const c = document.getElementById('page-crumb');
  if (c) c.textContent = '设备 / ' + (TITLES[name] || '');
}}
const wanted = (location.hash || '#comm').slice(1);
applyTitle(wanted);
document.querySelectorAll('.page').forEach(p => p.classList.toggle('on', p.id === wanted));
document.querySelectorAll('.tabbtn').forEach(btn => {{
  btn.classList.toggle('on', btn.dataset.p === wanted);
  btn.onclick = () => {{ location.hash = btn.dataset.p; }};
}});
window.addEventListener('hashchange', () => {{
  const name = (location.hash || '#comm').slice(1);
  applyTitle(name);
  document.querySelectorAll('.page').forEach(p => p.classList.toggle('on', p.id === name));
  document.querySelectorAll('.tabbtn').forEach(btn =>
    btn.classList.toggle('on', btn.dataset.p === name));
}});
</script>
</body></html>'''


def main() -> int:
    shots = '--shots' in sys.argv
    written = []
    for key in SCHEMES:
        path = HERE / f'{key}.html'
        path.write_text(render(key), encoding='utf-8')
        written.append(path)
        print('wrote', path.name)
    if shots:
        out_dir = HERE / 'shots'
        out_dir.mkdir(exist_ok=True)
        for key in SCHEMES:
            for page in ('vna', 'motion'):
                target = out_dir / f'{key}-{page}.png'
                subprocess.run([
                    str(EDGE), '--headless=new', '--disable-gpu', '--hide-scrollbars',
                    '--force-device-scale-factor=1',
                    '--window-size=1600,980',
                    f'--screenshot={target}',
                    f'file:///{(HERE / (key + ".html")).as_posix()}#{page}',
                ], check=False, capture_output=True, timeout=120)
                print('shot', target.name, target.exists())
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
