'use strict';

/* ------------------------------------------------------------------ helpers */
const $ = (id) => document.getElementById(id);
let HW_CONTEXT = null;
let PARAM_CONTEXT = null;
let PARAM_DIRTY = false;

async function api(method, path, body) {
  const opts = { method, headers: { 'Content-Type': 'application/json' } };
  if (method === 'POST' && (path.startsWith('/api/hardware/') || path.startsWith('/api/studio/'))) {
    body = { ...(body || {}), context: path.includes('/params/') ? PARAM_CONTEXT : HW_CONTEXT };
  }
  if (body !== undefined) opts.body = JSON.stringify(body);
  try {
    const res = await fetch(path, opts);
    const result = await res.json();
    if (method === 'POST' && path.startsWith('/api/hardware/') && result.ok && result.data.context) {
      HW_CONTEXT = result.data.context;
    }
    return result;
  } catch (e) {
    return { ok: false, error: '网络错误: ' + e.message };
  }
}

function showMsg(el, text, kind) {
  el.textContent = text;
  el.className = 'scale-msg ' + (kind || 'info');
}

// The active tuner model decides the axis list: a 3-axis tuner has X/Z1/Z2,
// a 4-axis tuner adds Z3. The value is refreshed from the backend.
let AXES = ['X', 'Z1', 'Z2', 'Z3'];

/* ------------------------------------------------------------- navigation */
function showPage(page) {
  document.querySelectorAll('.page').forEach((p) => { p.hidden = p.id !== 'page-' + page; });
  document.querySelectorAll('.nav-item').forEach((b) => {
    b.classList.toggle('active', b.dataset.page === page);
  });
  if (page === 'hw-comm') loadHwSettings();
  if (page === 'hw-params') loadHwParams();
  if (page === 'studio-dashboard') refreshStudioDash();
  if (page === 'studio-smith') drawSmith();
  if (page === 'studio-validation') loadValidation();
  if (page === 'hw-motion') startHwPoll(); else stopHwPoll();
  if (page === 'studio-run') startStudioPoll(); else stopStudioPoll();
  if (page === 'vna') loadVnaStatus();
  if (page === 'oplog') { loadOplog(); startLogPoll(); } else stopLogPoll();
  if (page === 'p1-tools') loadP1ReferencePlanes();
}

document.querySelectorAll('.nav-item').forEach((btn) => {
  btn.addEventListener('click', () => showPage(btn.dataset.page));
});

/* ------------------------------------------------------- hardware: comm */
async function loadHwSettings() {
  const r = await api('GET', '/api/hardware/settings');
  if (r.ok) {
    $('hw-ip').value = r.data.ip || '192.168.0.11';
    $('hw-auto').checked = !!r.data.connect_on_start;
    $('hw-settings-path').textContent = r.data.path || '—';
    $('hw-model').value = r.data.layout_id;
    HW_CONTEXT = r.data.context;
    $('hw-model').disabled = false;
    $('hw-mode').value = r.data.mode || 'real';
    $('hw-ip').disabled = ($('hw-mode').value !== 'real');
    applyUnitList(r.data.units || [], r.data.active_unit || '');
    applyAxisSet(r.data.axes || AXES);
  }
  updateHwStatus();
}

// Rebuild the tuner selector from the server's unit list.
function applyUnitList(units, activeKey) {
  const sel = $('hw-unit');
  if (!sel) return;
  const current = activeKey || sel.value;
  sel.innerHTML = '';
  units.forEach((u) => {
    const opt = document.createElement('option');
    opt.value = u.key;
    opt.textContent = u.label + ' · 轴 ' + (u.physical_axes || []).join('/');
    sel.appendChild(opt);
  });
  if (units.some((u) => u.key === current)) sel.value = current;
  else if (activeKey) sel.value = activeKey;
}

// Rebuild every axis-dependent control for the active tuner model.
function applyAxisSet(names) {
  AXES = names && names.length ? names.slice() : ['X', 'Z1', 'Z2', 'Z3'];
  ['hw-axis', 'st-axis'].forEach((id) => {
    const sel = $(id);
    if (!sel) return;
    const current = sel.value;
    sel.innerHTML = '';
    AXES.forEach((n) => {
      const opt = document.createElement('option');
      opt.value = n; opt.textContent = n; sel.appendChild(opt);
    });
    if (AXES.includes(current)) sel.value = current;
  });
  if (!PARAM_DIRTY) buildParamsTable();
  $('hw-model-info').textContent = AXES.length + ' 轴 (' + AXES.join(', ') + ')';
  const drill = $('hw-drill-axis');
  if (drill) {
    const current = drill.value;
    drill.innerHTML = '';
    AXES.forEach((n) => {
      const opt = document.createElement('option');
      opt.value = n; opt.textContent = n; drill.appendChild(opt);
    });
    if (AXES.includes(current)) drill.value = current;
  }
}

async function updateHwStatus() {
  const r = await api('GET', '/api/hardware/status');
  if (!r.ok) return;
  const d = r.data;
  if (d.context && JSON.stringify(d.context) !== JSON.stringify(HW_CONTEXT)) {
    if (!PARAM_DIRTY) {
      applyAxisSet(d.active_axes || []);
      PARAM_CONTEXT = null;
    }
    HW_CONTEXT = d.context;
  }
  $('hw-model').disabled = d.connected;
  $('hw-confirm-layout').disabled = d.connected;
  $('hw-state').textContent = d.connected ? '已连接' : '未连接';
  $('hw-firmware').textContent = d.firmware || '—';
  $('hw-address').textContent = d.address || '—';
  $('hw-library').textContent = d.library || '—';
  if (d.active_axes && d.active_axes.length) {
    $('hw-model-info').textContent =
      d.axis_count + ' 轴 (' + d.active_axes.join(', ') + ')';
  }
  if (d.unit_label) {
    $('hw-unit-info').textContent = d.unit_label;
  }
  if (d.axis_map) {
    const map = d.axis_map;
    $('hw-axis-map').textContent =
      Object.keys(map).map((k) => k + '→' + map[k]).join('  ');
  }
  const mode = d.mode || 'real';
  $('hw-mode-info').textContent = mode === 'virtual' ? '虚拟控制器' : '真实控制器';
  const drill = $('hw-drill-card');
  if (drill) drill.hidden = (mode !== 'virtual');
  const dataCard = $('hw-data-card');
  if (dataCard) dataCard.hidden = (mode !== 'virtual');
  const ind = $('conn-indicator');
  ind.textContent = d.connected ? '硬件已连接' : '硬件未连接';
  ind.className = d.connected ? 'conn-on' : 'conn-off';
  $('hw-disconnect').disabled = !d.connected;
  $('hw-connect').disabled = d.connected;
  if (d.axes && d.axes.length) renderHwTable(d.axes);
}

function renderHwTable(axes) {
  const tb = $('hw-status-table').querySelector('tbody');
  tb.innerHTML = '';
  axes.forEach((a) => {
    const tr = document.createElement('tr');
    if (a.status_fault) tr.className = 'row-fault';
    const err = (a.following_error !== undefined) ? a.following_error : (a.dpos - a.mpos);
    const cells = [
      a.axis + ' · 轴' + a.physical_axis, a.dpos.toFixed(4), a.mpos.toFixed(4), err.toFixed(4),
      a.idle ? '是' : '否', a.home_status === 1 ? '已回零' : '未回零',
      a.status, a.status_text || '正常', a.stop_reason_text || '—',
    ];
    cells.forEach((c, i) => {
      const td = document.createElement('td');
      if (i > 0 && i < 7) td.className = 'num';
      if (i === 7 || i === 8) td.className = 'flag';
      td.textContent = c;
      if (i === 8 && a.stop_reason !== undefined) {
        td.title = 'AXIS_STOPREASON = ' + a.stop_reason + ' (0x' +
          (a.stop_reason >>> 0).toString(16) + ')';
      }
      tr.appendChild(td);
    });
    tb.appendChild(tr);
  });
}

async function readHwErrors() {
  const box = $('hw-error-box');
  const r = await api('POST', '/api/hardware/errors', {});
  if (!r.ok) { showMsg(box, '读取控制器错误码失败: ' + r.error, 'error'); return; }
  const d = r.data;
  const faults = (d.axes || []).filter((a) => a.status_fault);
  let text = '控制器错误码：' + d.text;
  if (faults.length) {
    text += ' ｜ 报警轴：' + faults.map((a) => `${a.axis}（${a.status_text}）`).join('、');
  } else {
    text += ' ｜ 各轴无报警';
  }
  showMsg(box, text, faults.length ? 'error' : 'ok');
  if (d.axes && d.axes.length) renderHwTable(d.axes);
}

async function refreshHwDetail() {
  const r = await api('GET', '/api/hardware/axis_status');
  if (!r.ok) { showMsg($('hw-error-box'), '读取详细状态失败: ' + r.error, 'error'); return; }
  if (r.data.axes && r.data.axes.length) renderHwTable(r.data.axes);
  showMsg($('hw-error-box'), '已刷新详细状态', 'info');
}

$('hw-connect').onclick = async () => {
  const mode = $('hw-mode').value;
  const r = await api('POST', '/api/hardware/connect', {
    ip: $('hw-ip').value.trim(), connect_on_start: $('hw-auto').checked, mode,
  });
  const label = mode === 'virtual' ? '虚拟控制器' : $('hw-ip').value;
  showMsg($('hw-scale'), r.ok ? '已连接 ' + label : '连接失败: ' + r.error,
    r.ok ? 'ok' : 'error');
  updateHwStatus();
};

// Switching the controller backend drops the current link.
$('hw-mode').onchange = async () => {
  const mode = $('hw-mode').value;
  $('hw-ip').disabled = (mode !== 'real');
  const r = await api('POST', '/api/hardware/mode', { mode });
  if (!r.ok) {
    showMsg($('hw-scale'), '切换模式失败: ' + r.error, 'error');
    return;
  }
  showMsg($('hw-scale'),
    mode === 'virtual' ? '已切换到虚拟控制器，点「连接」开始演练'
                        : '已切换到真实控制器', 'info');
  updateHwStatus();
};
$('hw-disconnect').onclick = async () => {
  const r = await api('POST', '/api/hardware/disconnect', {});
  showMsg($('hw-scale'), r.ok ? '已断开' : '断开失败: ' + r.error, r.ok ? 'info' : 'error');
  updateHwStatus();
};
$('hw-search').onclick = async () => {
  const r = await api('POST', '/api/hardware/search', { timeout_ms: 1500 });
  showMsg($('hw-scale'), r.ok ? (r.data.found ? '本网段发现控制器' : '未发现控制器，请检查上电/网线/网段')
    : '搜索失败: ' + r.error, r.ok ? (r.data.found ? 'ok' : 'warn') : 'error');
};
$('hw-save-settings').onclick = async () => {
  const r = await api('POST', '/api/hardware/settings/save', {
    ip: $('hw-ip').value.trim(), connect_on_start: $('hw-auto').checked,
  });
  showMsg($('hw-scale'), r.ok ? '通讯设置已保存' : '保存失败: ' + r.error, r.ok ? 'ok' : 'error');
};

// Switching the tuner model changes which axes the software commands.
$('hw-model').onchange = async () => {
  if (PARAM_DIRTY && !confirm('放弃未保存参数并切换设备组合？')) { $('hw-model').value = HW_CONTEXT.layout_id; return; }
  const r = await api('POST', '/api/hardware/layout', { layout_id: $('hw-model').value });
  if (!r.ok) {
    $('hw-model').value = HW_CONTEXT.layout_id;
    showMsg($('hw-scale'), '切换组合失败: ' + r.error, 'error');
    return;
  }
  applyAxisSet(r.data.axes || []);
  showMsg($('hw-scale'),
    '设备组合已切换：'  + (r.data.axes || []).join(', '), 'ok');
  await loadHwParams();
  updateHwStatus();
};

$('hw-confirm-layout').onclick = () => $('hw-model').onchange();

// Switching the tuner picks which physical axis block the software commands.
$('hw-unit').onchange = async () => {
  if (PARAM_DIRTY && !confirm('放弃未保存参数并切换 Tuner？')) { $('hw-unit').value = HW_CONTEXT.unit; return; }
  const key = $('hw-unit').value;
  const r = await api('POST', '/api/hardware/unit', { unit: key });
  if (!r.ok) {
    $('hw-unit').value = HW_CONTEXT.unit;
    showMsg($('hw-scale'), '切换 Tuner 失败: ' + r.error, 'error');
    return;
  }
  applyUnitList(r.data.units || [], r.data.active_unit || key);
  applyAxisSet(r.data.axes || []);
  const unit = (r.data.units || []).find((u) => u.key === (r.data.active_unit || key));
  const map = unit ? Object.keys(unit.axis_map || {}).map((k) => k + '→' + unit.axis_map[k]).join('  ') : '';
  showMsg($('hw-scale'),
    '已切换到 ' + (unit ? unit.label : key) + '，物理轴 ' + map, 'ok');
  await loadHwParams();
  updateHwStatus();
};
$('hw-read-errors').onclick = readHwErrors;
$('hw-drill-inject').onclick = async () => {
  const r = await api('POST', '/api/hardware/inject_fault', {
    axis: $('hw-drill-axis').value, bit: parseInt($('hw-drill-bit').value, 10),
  });
  if (!r.ok) { showMsg($('hw-error-box'), '注入失败: ' + r.error, 'error'); return; }
  showMsg($('hw-error-box'),
    '已注入故障，状态字 ' + r.data.status + '；该轴在状态表中标红', 'info');
  updateHwStatus();
};
$('hw-data-generate').onclick = async () => {
  const r = await api('POST', '/api/hardware/generate_data', {
    x_count: parseInt($('hw-data-x').value, 10),
    z_count: parseInt($('hw-data-z').value, 10),
  });
  if (!r.ok) { showMsg($('hw-data-msg'), '生成失败: ' + r.error, 'error'); return; }
  showMsg($('hw-data-msg'),
    '已生成 ' + r.data.points + ' 点虚拟校准数据（' + r.data.valid + ' 点有效），'
    + '已自动载入，可直接到 Smith 图 / 阻抗调谐查看', 'ok');
  refreshStudioDash();
};
$('hw-drill-clear').onclick = async () => {
  const r = await api('POST', '/api/hardware/inject_fault', { clear: true });
  if (!r.ok) { showMsg($('hw-error-box'), '清除失败: ' + r.error, 'error'); return; }
  showMsg($('hw-error-box'), '已清除全部注入故障', 'ok');
  updateHwStatus();
};
$('hw-refresh-status').onclick = refreshHwDetail;

/* ------------------------------------------------------- hardware: motion */
let hwPoll = null;
function startHwPoll() { stopHwPoll(); updateHwStatus(); hwPoll = setInterval(updateHwStatus, 300); }
function stopHwPoll() { if (hwPoll) { clearInterval(hwPoll); hwPoll = null; } }

function axis() { return $('hw-axis').value; }

$('hw-jog-pos').onclick = () => hwJog(parseFloat($('hw-step').value));
$('hw-jog-neg').onclick = () => hwJog(-parseFloat($('hw-step').value));
async function hwJog(delta) {
  const r = await api('POST', '/api/hardware/jog', { axis, delta });
  showMsg($('hw-scale'), r.ok ? axis() + ' 点动 ' + (delta >= 0 ? '+' : '') + delta : '失败: ' + r.error,
    r.ok ? 'info' : 'error');
}
$('hw-abs').onclick = async () => {
  const r = await api('POST', '/api/hardware/move', { axis: axis(), target: parseFloat($('hw-target').value) });
  showMsg($('hw-scale'), r.ok ? axis() + ' → ' + parseFloat($('hw-target').value) : '失败: ' + r.error,
    r.ok ? 'info' : 'error');
};
$('hw-home').onclick = async () => {
  const r = await api('POST', '/api/hardware/home', { axis: axis() });
  showMsg($('hw-scale'), r.ok ? axis() + ' 回零结束，状态码 ' + r.data.home_status : '失败: ' + r.error,
    r.ok ? 'ok' : 'error');
};
$('hw-stop').onclick = async () => {
  const r = await api('POST', '/api/hardware/stop', {});
  showMsg($('hw-scale'), r.ok ? '已发送停止指令' : '失败: ' + r.error, r.ok ? 'info' : 'error');
};
$('hw-probe').onclick = async () => {
  const r = await api('POST', '/api/hardware/probe', { axis: axis(), distance: 0.005 });
  if (!r.ok) {
    showMsg($('hw-scale'), '标度自检失败: ' + r.error, 'error');
    $('hw-apply-scale').hidden = true;
    return;
  }
  const d = r.data; const ratio = d.ratio;
  if (Math.abs(ratio - 1.0) <= 0.05) {
    showMsg($('hw-scale'),
      `${d.axis} 标度自检：指令 ${d.commanded.toFixed(4)} → 编码器 ${d.feedback >= 0 ? '+' : ''}${d.feedback.toFixed(4)}，比值 ${ratio.toFixed(3)}（一致）`, 'ok');
    $('hw-apply-scale').hidden = true;
  } else {
    const suggested = d.suggested_units || d.units;
    showMsg($('hw-scale'),
      `${d.axis} 标度自检：指令 ${d.commanded.toFixed(4)} → 编码器 ${d.feedback >= 0 ? '+' : ''}${d.feedback.toFixed(4)}，比值 ${ratio.toFixed(2)}。
该轴实际行程约为指令数字的 ${ratio.toFixed(1)} 倍，说明 UNITS 与反馈标度不匹配。
当前 UNITS=${d.units}，若要"一个指令单位 = 1 mm"，UNITS 应改为约 ${suggested}（= 当前 UNITS ÷ ${ratio.toFixed(1)}）。`, 'error');
    const btn = $('hw-apply-scale');
    btn.hidden = false;
    btn.textContent = `采纳并写入修正后的 UNITS（${d.axis} → ${suggested}）`;
    btn.onclick = async () => {
      const w = await api('POST', '/api/hardware/apply_scale',
        { axis: d.axis, units: suggested });
      if (!w.ok) { showMsg($('hw-scale'), '写入 UNITS 失败: ' + w.error, 'error'); return; }
      btn.hidden = true;
      showMsg($('hw-scale'),
        `${d.axis} 的 UNITS 已改为 ${w.data.units} 并保存到设置文件。坐标系随之改变，请重新回零后再运动。`, 'ok');
      loadHwParams();
      updateHwStatus();
    };
  }
};
$('hw-estop').onclick = async () => {
  const r = await api('POST', '/api/hardware/estop', {});
  showMsg($('hw-scale'), r.ok ? '急停已触发，需复位后重新回零' : '失败: ' + r.error, r.ok ? 'warn' : 'error');
  updateHwStatus();
};
$('hw-reset').onclick = async () => {
  const r = await api('POST', '/api/hardware/reset_estop', {});
  showMsg($('hw-scale'), r.ok ? '急停已复位，请重新回零' : '失败: ' + r.error, r.ok ? 'info' : 'error');
  updateHwStatus();
};

/* ------------------------------------------------------- hardware: params */
const PARAM_COLS = [
  ['atype', 'ATYPE'], ['units', 'UNITS'], ['speed', 'SPEED'], ['accel', 'ACCEL'],
  ['decel', 'DECEL'], ['creep', 'CREEP'], ['jog_speed', 'JOG'], ['invert_step', 'INVERT_STEP'],
  ['home_wait', 'HOME_WAIT'], ['home_output', 'HOME_OUTPUT'], ['home_pulse', 'HOME_PULSE'],
  ['home_output_active', 'OP_ACTIVE'], ['datum_active', 'DATUM_ACTIVE'],
  ['datum_in', 'DATUM_IN'], ['fs_limit', 'FS_LIMIT'], ['rs_limit', 'RS_LIMIT'],
  ['datum_mode', 'DATUM_MODE'], ['feedback_scale', 'FEEDBACK_SCALE'],
];

function buildParamsTable() {
  const table = $('hw-params-table');
  const thead = table.querySelector('thead tr');
  const tbody = table.querySelector('tbody');
  thead.innerHTML = '<th>轴</th>' + PARAM_COLS.map((c) => `<th>${c[1]}</th>`).join('');
  tbody.innerHTML = '';
  AXES.forEach((name) => {
    const tr = document.createElement('tr');
    const th = document.createElement('th'); th.textContent = name; tr.appendChild(th);
    PARAM_COLS.forEach((c) => {
      const td = document.createElement('td');
      const inp = document.createElement('input');
      inp.oninput = () => { PARAM_DIRTY = true; };
      inp.type = 'number'; inp.step = 'any'; inp.dataset.axis = name; inp.dataset.key = c[0];
      td.appendChild(inp); tr.appendChild(td);
    });
    tbody.appendChild(tr);
  });
}

function fillParams(data) {
  document.querySelectorAll('#hw-params-table input').forEach((inp) => {
    const v = data[inp.dataset.axis] && data[inp.dataset.axis][inp.dataset.key];
    if (v !== undefined) inp.value = v;
  });
}

async function loadHwParams() {
  buildParamsTable();
  const r = await api('GET', '/api/hardware/params', {});
  if (r.ok) { fillParams(r.data.axes); PARAM_CONTEXT = r.data.context; PARAM_DIRTY = false; }
}
$('hw-params-read').onclick = async () => {
  const r = await api('POST', '/api/hardware/params/read', {});
  if (r.ok) { showMsg($('hw-params-hint'), JSON.stringify(r.data.states), 'info'); }
  else showMsg($('hw-params-hint'), '读取失败: ' + r.error, 'error');
};
$('hw-params-write').onclick = async () => {
  if (PARAM_DIRTY) { showMsg($('hw-params-hint'), '请先保存配置，再写入控制器', 'warn'); return; }
  const axes = {};
  document.querySelectorAll('#hw-params-table input').forEach((inp) => {
    (axes[inp.dataset.axis] = axes[inp.dataset.axis] || {})[inp.dataset.key] = parseFloat(inp.value);
  });
  const r = await api('POST', '/api/hardware/params/write', { axes });
  showMsg($('hw-params-hint'), r.ok ? JSON.stringify(r.data.results) : '写入失败: ' + r.error,
    r.ok ? 'ok' : 'error');
};
$('hw-params-save').onclick = async () => {
  const axes = {};
  document.querySelectorAll('#hw-params-table input').forEach((inp) => {
    (axes[inp.dataset.axis] = axes[inp.dataset.axis] || {})[inp.dataset.key] = parseFloat(inp.value);
  });
  const r = await api('POST', '/api/hardware/params/save', { axes });
  if (r.ok) { PARAM_CONTEXT = r.data.context; HW_CONTEXT = r.data.context; PARAM_DIRTY = false; }
  showMsg($('hw-params-hint'), r.ok ? '已保存本地配置（未写入控制器）' : '保存失败: ' + r.error,
    r.ok ? 'ok' : 'error');
};
$('hw-params-load').onclick = async () => {
  const r = await api('POST', '/api/hardware/params/load', {});
  if (r.ok) { fillParams(r.data); showMsg($('hw-params-hint'), '已从文件加载（尚未写入控制器）', 'info'); }
  else showMsg($('hw-params-hint'), '加载失败: ' + r.error, 'error');
};

/* ------------------------------------------------------- studio: dashboard */
async function refreshStudioDash() {
  const r = await api('GET', '/api/studio/overview', {});
  if (r.ok) {
    const d = r.data;
    let txt = '尚未初始化。';
    if (d.has_hardware) {
      const pos = await api('GET', '/api/studio/position', {});
      const src = d.motion_source === 'real' ? '实机 Zmotion' : '模拟运动';
      const vna = d.vna_source === 'real' ? '真实矢网' : '模拟矢网';
      txt = `已初始化（运动：${src} · 矢网：${vna}${d.config ? ' · ' + d.config.probes + ' 探针' : ''}）。`;
      if (d.motion_source === 'real') {
        txt += ` 已回零轴：${(d.homed_axes && d.homed_axes.length) ? d.homed_axes.join(', ') : '无'}。`;
      }
      txt += (pos.ok ? ' 当前坐标: ' + pos.data.position.map((v) => v.toFixed(4)).join(' | ') : '');
      $('st-pos').textContent = pos.ok ? pos.data.position.map((v) => v.toFixed(4)).join('  |  ') : '—';
    }
    if (d.has_database) txt += `  已载入数据库: ${d.valid}/${d.total} 有效。`;
    showMsg($('st-dash-summary'), txt, d.has_hardware ? 'ok' : 'info');
  }
}
$('st-init').onclick = async () => {
  const probes = parseInt($('st-probes').value, 10);
  const offsets = $('st-offsets').value.split(',').map((s) => parseFloat(s.trim()));
  const motionSource = $('st-motion-source').value;
  const body = {
    probes, offsets,
    motion_source: motionSource,
    vna_source: $('st-vna-real').checked ? 'real' : 'mock',
  };
  if (motionSource === 'real') {
    body.x_min = parseFloat($('st-x-min').value);
    body.x_max = parseFloat($('st-x-max').value);
    body.z_min = parseFloat($('st-z-min').value);
    body.z_max = parseFloat($('st-z-max').value);
    body.safe_z = parseFloat($('st-safe-z').value);
  }
  const useReal = $('st-vna-real').checked;
  if (useReal) {
    body.vna_vendor = $('st-vna-vendor').value;
    body.vna_interface = $('st-vna-interface').value;
    body.vna_resource = $('st-vna-resource').value.trim();
    body.vna_ip = $('st-vna-ip').value.trim();
    body.vna_start_hz = parseFloat($('st-vna-start').value);
    body.vna_stop_hz = parseFloat($('st-vna-stop').value);
    body.vna_points = parseInt($('st-vna-points').value, 10);
  }
  const r = await api('POST', '/api/studio/init_hardware', body);
  const label = motionSource === 'real' ? '实机运动' : '模拟运动';
  showMsg($('st-dash-summary'),
    r.ok ? `${label}已初始化` + (useReal ? '，真实矢网已接入' : '')
      : '初始化失败: ' + r.error,
    r.ok ? 'ok' : 'error');
  if (r.ok) refreshStudioDash();
};

$('st-home').onclick = async () => {
  const r = await api('POST', '/api/studio/home', {});
  showMsg($('st-dash-summary'),
    r.ok ? '回零完成，已回零轴：' + ((r.data.homed_axes || []).join(', ') || '无')
      : '回零失败: ' + r.error,
    r.ok ? 'ok' : 'error');
  refreshStudioDash();
};

// The travel box only matters for real hardware.
function syncMotionSource() {
  const real = $('st-motion-source').value === 'real';
  $('st-travel-row').style.display = real ? '' : 'none';
  // On real hardware the tuner model fixes the number of probes, so the
  // operator cannot select a geometry the machine does not have.
  const probesSelect = $('st-probes');
  if (real) {
    probesSelect.disabled = true;
    probesSelect.value = String(Math.max(2, AXES.length - 1));
  } else {
    probesSelect.disabled = false;
  }
  updateGridCount();
}
$('st-motion-source').onchange = syncMotionSource;
$('st-jog').onclick = async () => {
  const r = await api('POST', '/api/studio/jog', { axis: $('st-axis').value, delta: parseFloat($('st-delta').value) });
  if (r.ok) $('st-pos').textContent = r.data.position.map((v) => v.toFixed(4)).join('  |  ');
  else showMsg($('st-dash-summary'), '点动失败: ' + r.error, 'error');
};
$('st-retract').onclick = async () => {
  const r = await api('POST', '/api/studio/retract', {});
  if (r.ok) $('st-pos').textContent = r.data.position.map((v) => v.toFixed(4)).join('  |  ');
  else showMsg($('st-dash-summary'), '回缩失败: ' + r.error, 'error');
};
$('st-estop').onclick = async () => {
  const r = await api('POST', '/api/studio/estop', {});
  showMsg($('st-dash-summary'), r.ok ? '急停已锁定' : '失败: ' + r.error, r.ok ? 'warn' : 'error');
};
$('st-reset').onclick = async () => {
  const r = await api('POST', '/api/studio/reset_home', {});
  if (r.ok) $('st-pos').textContent = r.data.position.map((v) => v.toFixed(4)).join('  |  ');
  showMsg($('st-dash-summary'), r.ok ? '已复位并重新回零' : '失败: ' + r.error, r.ok ? 'info' : 'error');
};

/* ------------------------------------------------------- studio: setup */
function updateGridCount() {
  const x = parseInt($('st-xcount').value, 10);
  const z = parseInt($('st-zcount').value, 10);
  const p = parseInt($('st-probes').value, 10);
  $('st-grid-count').textContent = (x * Math.pow(z, p)).toLocaleString();
}
['st-xcount', 'st-zcount', 'st-probes'].forEach((id) => $(id).addEventListener('input', updateGridCount));
$('st-create').onclick = async () => {
  const plan = $('st-plan').value;
  const body = {
    x_count: parseInt($('st-xcount').value, 10),
    z_count: parseInt($('st-zcount').value, 10),
    path: $('st-path').value.trim() || undefined,
    plan: plan,
  };
  if (plan === 'smith') Object.assign(body, planParams());
  const r = await api('POST', '/api/studio/setup', body);
  showMsg($('st-setup-msg'), r.ok ? `已创建 ${r.data.positions} 点模拟任务: ${r.data.path}` : '创建失败: ' + r.error,
    r.ok ? 'ok' : 'error');
};

/* ------------------------------------------------------- studio: run */
let studioPoll = null;
let lastStudioState = null;
function startStudioPoll() { stopStudioPoll(); pollStudio(); studioPoll = setInterval(pollStudio, 500); }
function stopStudioPoll() { if (studioPoll) { clearInterval(studioPoll); studioPoll = null; } }

/* F-11: draw the acquisition grid — one column per X layer, one cell per
   (Z1,Z2,Z3) combination.  A filled cell means the point was written to the
   library; a red cell means it failed. */
function drawAcquisitionCloud(cloud) {
  const host = $('st-cloud');
  if (!host) return;
  host.innerHTML = '';
  if (!cloud || !cloud.layers || !cloud.layers.length) {
    const empty = document.createElement('div');
    empty.className = 'cloud-empty';
    empty.textContent = '建库后这里显示采集点云';
    host.appendChild(empty);
    $('st-cloud-text').textContent = '—';
    return;
  }
  const failed = new Set((cloud.failed || []).map((f) => f.index));
  cloud.layers.forEach((layer) => {
    const column = document.createElement('div');
    column.className = 'cloud-layer';
    column.title = `X = ${layer.x}`;
    (layer.rows || []).forEach((row, index) => {
      const cell = document.createElement('div');
      cell.className = 'cloud-cell';
      const pointIndex = (layer.indices || [])[index];
      if (failed.has(pointIndex)) cell.classList.add('failed');
      else if ((layer.done_rows || [])[index]) cell.classList.add('done');
      cell.title = `X=${layer.x} 点#${pointIndex}`;
      column.appendChild(cell);
    });
    host.appendChild(column);
  });
  $('st-cloud-text').textContent =
    `${cloud.done} / ${cloud.total} 点 · ${cloud.complete_layers}/${cloud.layer_count} 层完成` +
    (cloud.failed && cloud.failed.length ? ` · 失败 ${cloud.failed.length}` : '');
}

async function pollStudio() {
  const r = await api('GET', '/api/studio/task', {});
  if (!r.ok) return;
  const d = r.data;
  const total = d.progress[1] || 1;
  const pct = Math.round((100 * (d.progress[0] + 1)) / total);
  $('st-progress-bar').style.width = Math.min(100, pct) + '%';
  $('st-progress-text').textContent = `${d.progress[0] + 1} / ${d.progress[1]}` + (d.error ? '  错误: ' + d.error : '');
  $('st-run-state').textContent = d.state + (d.running ? ' (运行中)' : '');
  if (d.state !== lastStudioState) {
    lastStudioState = d.state;
    const log = $('st-run-log');
    log.value += `${new Date().toLocaleTimeString()}  状态: ${d.state}\n`;
    log.scrollTop = log.scrollHeight;
  }
  // The cloud needs a library, so a missing one is simply "no cloud yet".
  const c = await api('GET', '/api/studio/progress_cloud', {});
  drawAcquisitionCloud(c.ok ? c.data : null);
}
$('st-run').onclick = async () => {
  const r = await api('POST', '/api/studio/run', {});
  showMsg($('st-setup-msg'), r.ok ? '校准运行中…' : '启动失败: ' + r.error, r.ok ? 'info' : 'error');
};
$('st-pause').onclick = async () => { await api('POST', '/api/studio/pause', {}); };
$('st-cancel').onclick = async () => { await api('POST', '/api/studio/cancel', {}); };

/* ------------------------------------------------------- studio: database */
$('st-db-open').onclick = async () => {
  const r = await api('POST', '/api/studio/open_db', { path: $('st-db-path').value.trim() });
  if (!r.ok) { showMsg($('st-dash-summary'), '打开失败: ' + r.error, 'error'); return; }
  const info = await api('GET', '/api/studio/database', {});
  if (info.ok) {
    $('st-db-path-out').textContent = info.data.path;
    $('st-db-valid').textContent = `${info.data.valid} / ${info.data.total}`;
    $('st-db-freq').textContent = (info.data.frequencies[0] / 1e9).toFixed(3) + ' – '
      + (info.data.frequencies[1] / 1e9).toFixed(3);
    $('st-db-meta').textContent = JSON.stringify(info.data.metadata, null, 2);
    showMsg($('st-dash-summary'), `已载入校准: ${info.data.valid} 有效点`, 'ok');
  }
};

/* ------------------------------------------------------- studio: smith */
function gammaFromZ(r, x) {
  const num = { re: r - 1, im: x };
  const den = { re: r + 1, im: x };
  const d = den.re * den.re + den.im * den.im;
  return { re: (num.re * den.re + num.im * den.im) / d, im: (num.im * den.re - num.re * den.im) / d };
}
function linspace(a, b, n) { const out = []; for (let i = 0; i < n; i++) out.push(a + (b - a) * i / (n - 1)); return out; }

function drawSmith() {
  const canvas = $('smith-canvas');
  const ctx = canvas.getContext('2d');
  const W = canvas.width, H = canvas.height;
  ctx.clearRect(0, 0, W, H);
  const cx = W / 2, cy = H / 2;
  const rad = Math.min(W, H) / 2 - 20;
  const xy = (g) => ({ x: cx + rad * g.re, y: cy - rad * g.im });
  const poly = (gs) => { ctx.beginPath(); gs.forEach((g, i) => { const p = xy(g); i ? ctx.lineTo(p.x, p.y) : ctx.moveTo(p.x, p.y); }); ctx.stroke(); };

  ctx.strokeStyle = '#35506b'; ctx.lineWidth = 1;
  ctx.beginPath(); ctx.arc(cx, cy, rad, 0, 2 * Math.PI); ctx.stroke();
  ctx.beginPath(); ctx.moveTo(xy({ re: -1, im: 0 }).x, xy({ re: -1, im: 0 }).y);
  ctx.lineTo(xy({ re: 1, im: 0 }).x, xy({ re: 1, im: 0 }).y); ctx.stroke();

  ctx.strokeStyle = '#35506b';
  [0, 0.2, 0.5, 1, 2, 5].forEach((r) => {
    const gs = linspace(-70, 70, 1600).map((x) => gammaFromZ(r, x));
    poly(gs);
  });
  [0.2, 0.5, 1, 2, 5, -0.2, -0.5, -1, -2, -5].forEach((react) => {
    const gs = linspace(0, 70, 700).map((zr) => gammaFromZ(zr, react));
    poly(gs);
  });

  // fetch points and draw
  const freq = parseFloat($('st-smith-freq').value) * 1e9;
  api('POST', '/api/studio/smith', { frequency: freq }).then((r) => {
    if (!r.ok || !r.data.gammas) return;
    ctx.fillStyle = '#42c5b7';
    r.data.gammas.forEach((g) => { const p = xy(g); ctx.fillRect(p.x - 1.2, p.y - 1.2, 2.4, 2.4); });
    const ring = (g, color) => { if (!g) return; const p = xy(g); ctx.strokeStyle = color; ctx.lineWidth = 3;
      ctx.beginPath(); ctx.arc(p.x, p.y, 6, 0, 2 * Math.PI); ctx.stroke(); };
    ring(r.data.target, '#ffbc5a');
    ring(r.data.selected, '#ff6582');
  });
}
$('st-smith-refresh').onclick = drawSmith;

/* ------------------------------------------------------- studio: tuning */
let selectedCandidate = 0;
$('st-tune-search').onclick = async () => {
  const mode = $('st-tune-mode').value;
  const r = await api('POST', '/api/studio/tune', {
    frequency: parseFloat($('st-tune-freq').value) * 1e9,
    R: parseFloat($('st-tune-r').value), X: parseFloat($('st-tune-x').value),
    tolerance: parseFloat($('st-tune-tol').value),
    method: $('st-tune-method').value,
    level_policy: $('st-tune-policy').value,
    ...(mode ? { tune_to_mode: mode } : {}),
  });
  const tb = $('st-tune-table').querySelector('tbody');
  tb.innerHTML = '';
  if (!r.ok) { showMsg($('st-tune-result'), '搜索失败: ' + r.error, 'error'); return; }
  r.data.candidates.forEach((c, i) => {
    const tr = document.createElement('tr');
    tr.innerHTML = `<td>${c.index}</td><td class="num">[${c.position.map((v) => v.toFixed(4)).join(', ')}]</td>`
      + `<td class="num">${c.gamma.re.toFixed(4)}${c.gamma.im >= 0 ? '+' : ''}${c.gamma.im.toFixed(4)}i</td>`
      + `<td class="num">${c.impedance.re.toFixed(2)}${c.impedance.im >= 0 ? '+' : ''}${c.impedance.im.toFixed(2)}i</td>`
      + `<td class="num">${c.error.toFixed(6)}</td><td>${c.reachable ? '是' : '否'}</td>`;
    tr.addEventListener('click', () => { selectedCandidate = i; markSelected(tb, tr); });
    tb.appendChild(tr);
  });
  if (r.data.candidates.length) { selectedCandidate = 0; markSelected(tb, tb.firstChild); }
  const best = r.data.candidates[0];
  renderTuned(r.data.tuned);
  showMsg($('st-tune-result'),
    best ? `最近预测 |ΔΓ|=${best.error.toFixed(6)}；可达: ${best.reachable}。无连续覆盖保证。` : '无候选',
    best ? 'info' : 'warn');
};

function renderTuned(tuned) {
  const stages = $('st-tune-stages').querySelector('tbody');
  stages.innerHTML = '';
  if (!tuned) {
    $('st-tune-interp').textContent = '—';
    $('st-tune-position').textContent = '—';
    $('st-tune-gamma').textContent = '—';
    $('st-tune-error').textContent = '—';
    return;
  }
  if (typeof tuned.error === 'string') {
    $('st-tune-interp').textContent = '调谐失败';
    $('st-tune-position').textContent = '—';
    $('st-tune-gamma').textContent = '—';
    $('st-tune-error').textContent = tuned.error;
    return;
  }
  $('st-tune-interp').textContent = tuned.interpolator === 'tensor'
    ? '张量网格 · 二阶 Lagrange' : '散点 · 局部二阶拟合';
  $('st-tune-position').textContent = '[' + tuned.position.map((v) => v.toFixed(4)).join(', ') + ']';
  $('st-tune-gamma').textContent = tuned.gamma.re.toFixed(6)
    + (tuned.gamma.im >= 0 ? '+' : '') + tuned.gamma.im.toFixed(6) + 'i';
  $('st-tune-error').textContent = tuned.error.toFixed(6);
  (tuned.stages || []).forEach((s) => {
    const tr = document.createElement('tr');
    const names = { general_search: '一般搜索', gradient_search: '梯度搜索', fine_grid: '细网格' };
    tr.innerHTML = `<td>${names[s.stage] || s.stage}</td>`
      + `<td class="num">[${s.position.map((v) => v.toFixed(4)).join(', ')}]</td>`
      + `<td class="num">${s.gamma.re.toFixed(6)}${s.gamma.im >= 0 ? '+' : ''}${s.gamma.im.toFixed(6)}i</td>`
      + `<td class="num">${s.error.toFixed(6)}</td>`;
    stages.appendChild(tr);
  });
}

$('st-tune-move-tuned').onclick = async () => {
  const r = await api('POST', '/api/studio/move_tuned', {});
  showMsg($('st-tune-result'), r.ok
    ? `已移动到调谐结果：${r.data.position.map((v) => v.toFixed(4)).join(' | ')}（预测 |ΔΓ|=${r.data.error.toFixed(6)}）`
    : '移动失败: ' + r.error, r.ok ? 'ok' : 'error');
};
function markSelected(tb, row) {
  Array.from(tb.children).forEach((tr) => tr.style.background = '');
  row.style.background = '#eef3f8';
}
$('st-tune-move').onclick = async () => {
  const r = await api('POST', '/api/studio/move_candidate', { index: selectedCandidate });
  showMsg($('st-tune-result'), r.ok
    ? `已移动到候选 ${selectedCandidate}。预测 Γ=(${r.data.predicted_gamma.re.toFixed(4)}, ${r.data.predicted_gamma.im.toFixed(4)})，|ΔΓ|=${r.data.error.toFixed(6)}。RF 未实测。`
    : '移动失败: ' + r.error, r.ok ? 'info' : 'error');
};
$('st-tune-verify').onclick = async () => {
  const r = await api('POST', '/api/studio/verify', {
    frequency: parseFloat($('st-tune-freq').value) * 1e9,
    R: parseFloat($('st-tune-r').value), X: parseFloat($('st-tune-x').value),
    tolerance: parseFloat($('st-tune-tol').value),
  });
  if (r.ok) {
    const d = r.data;
    showMsg($('st-tune-result'),
      `模拟 RF 测量: Γ=(${d.measured_gamma_real.toFixed(4)}, ${d.measured_gamma_imag.toFixed(4)})，|ΔΓ|=${d.vector_error.toFixed(6)}。物理 RF 验证: false。`,
      'info');
  } else showMsg($('st-tune-result'), '验证失败: ' + r.error, 'error');
};

/* ------------------------------------------------------- studio: validation */
async function loadValidation() {
  const r = await api('GET', '/api/studio/validation_report', {});
  if (r.ok && r.data) { $('st-val-report').textContent = JSON.stringify(r.data, null, 2); }
  loadInterpolationReport();
}

async function loadInterpolationReport() {
  const r = await api('GET', '/api/studio/interpolation_report', {});
  const tb = $('st-iv-table').querySelector('tbody');
  tb.innerHTML = '';
  if (!r.ok || !r.data) {
    $('st-iv-verdict').textContent = '—';
    $('st-iv-max').textContent = '—';
    $('st-iv-mean').textContent = '—';
    return;
  }
  const d = r.data;
  $('st-iv-verdict').textContent = d.passed ? '通过（插值模型可信）' : '建议重新标定';
  $('st-iv-verdict').style.color = d.passed ? '#047857' : '#b91c1c';
  $('st-iv-max').textContent = d.max_error.toFixed(6)
    + (isFinite(d.max_error_db) ? ` (${d.max_error_db.toFixed(1)} dB)` : '');
  $('st-iv-mean').textContent = d.mean_error.toFixed(6);
  (d.points || []).forEach((p) => {
    const tr = document.createElement('tr');
    if (!p.within_tolerance) tr.className = 'row-fault';
    const g = (v) => v.re.toFixed(5) + (v.im >= 0 ? '+' : '') + v.im.toFixed(5) + 'i';
    tr.innerHTML = `<td>${p.index + 1}</td>`
      + `<td class="num">[${p.position.map((v) => v.toFixed(4)).join(', ')}]</td>`
      + `<td class="num">${g(p.predicted_gamma)}</td>`
      + `<td class="num">${g(p.measured_gamma)}</td>`
      + `<td class="num">${p.error.toFixed(6)}</td>`
      + `<td class="num">${p.magnitude_error.toFixed(6)}</td>`
      + `<td class="num">${p.phase_error_deg.toFixed(2)}°</td>`
      + `<td>${p.within_tolerance ? '通过' : '超差'}</td>`;
    tb.appendChild(tr);
  });
}

$('st-iv-run').onclick = async () => {
  const r = await api('POST', '/api/studio/validate_interpolation', {
    count: parseInt($('st-iv-count').value, 10),
    tolerance: parseFloat($('st-iv-tol').value),
    strategy: $('st-iv-strategy').value,
    frequency: parseFloat($('st-iv-freq').value) * 1e9,
  });
  showMsg($('st-iv-msg'), r.ok
    ? `插值验证运行中（${r.data.points} 个点，容差 ${r.data.tolerance}）…`
    : '启动失败: ' + r.error, r.ok ? 'info' : 'error');
  if (!r.ok) return;
  const timer = setInterval(async () => {
    const st = await api('GET', '/api/studio/overview', {});
    if (!st.ok) return;
    const state = st.data.calibration_state;
    $('st-iv-msg').textContent = `验证中… ${st.data.calibration_progress[0]} / `
      + `${st.data.calibration_progress[1]}`;
    if (state === 'validated' || state === 'failed') {
      clearInterval(timer);
      const report = await api('GET', '/api/studio/interpolation_report', {});
      if (report.ok && report.data) {
        showMsg($('st-iv-msg'), report.data.message,
          report.data.passed ? 'ok' : 'error');
      } else {
        showMsg($('st-iv-msg'), '验证结束（未产生报告）', 'warn');
      }
      loadInterpolationReport();
    }
  }, 600);
};
$('st-validate').onclick = async () => {
  const r = await api('POST', '/api/studio/validate', {});
  $('st-val-state').textContent = r.ok ? '验证中…' : '失败: ' + r.error;
  if (!r.ok) return;
  // poll until done
  const timer = setInterval(async () => {
    const st = await api('GET', '/api/studio/overview', {});
    if (st.ok) {
      $('st-val-state').textContent = st.data.calibration_state;
      if (st.data.calibration_state === 'validated' || st.data.calibration_state === 'failed') {
        clearInterval(timer);
        loadValidation();
      }
    }
  }, 600);
};

/* ------------------------------------------------------- VNA control */
function syncVnaInterface() {
  const gpib = $('vna-interface').value === 'gpib';
  ['vna-ip-label', 'vna-ip', 'vna-port-label', 'vna-port'].forEach((id) => {
    $(id).style.display = gpib ? 'none' : '';
  });
  ['vna-resource-label', 'vna-resource', 'vna-visa-detect'].forEach((id) => {
    $(id).style.display = gpib ? '' : 'none';
  });
}
$('vna-interface').onchange = syncVnaInterface;

function syncStVnaInterface() {
  const gpib = $('st-vna-interface').value === 'gpib';
  $('st-vna-ip-label').style.display = gpib ? 'none' : '';
  $('st-vna-ip').style.display = gpib ? 'none' : '';
  $('st-vna-resource-label').style.display = gpib ? '' : 'none';
  $('st-vna-resource').style.display = gpib ? '' : 'none';
}
$('st-vna-interface').onchange = syncStVnaInterface;

$('vna-visa-detect').onclick = async () => {
  const r = await api('GET', '/api/vna/visa_resources');
  if (!r.ok) {
    showMsg($('vna-msg'), 'VISA 检测失败: ' + r.error, 'error');
    return;
  }
  const found = (r.data.resources || []).filter((x) => x.toUpperCase().startsWith('GPIB'));
  if (found.length) {
    $('vna-resource').value = found[0];
    showMsg($('vna-msg'), 'VISA 后端 ' + (r.data.backend || '') + ' · GPIB 资源: ' + found.join(', '), 'ok');
  } else {
    showMsg($('vna-msg'), 'VISA 后端 ' + (r.data.backend || '') + ' 未发现 GPIB 资源，请检查 82357B 与仪器电源', 'error');
  }
};

async function loadVnaVendors() {
  const r = await api('GET', '/api/vna/vendors');
  if (!r.ok) return;
  const sel = $('vna-vendor');
  sel.innerHTML = '';
  const add = (k, label) => { const o = document.createElement('option'); o.value = k; o.textContent = label; sel.appendChild(o); };
  add('auto', '自动识别');
  Object.entries(r.data.vendors).forEach(([k, label]) => add(k, label));
}

async function loadVnaStatus() {
  const r = await api('GET', '/api/vna/status');
  if (!r.ok) return;
  const d = r.data;
  $('vna-state').textContent = d.connected ? '已连接' : '未连接';
  $('vna-vendor-out').textContent = d.vendor ? (d.vendor) : '—';
  $('vna-idn-out').textContent = d.idn || '—';
  $('vna-error').textContent = d.last_error || '—';
  $('vna-disconnect').disabled = !d.connected;
  $('vna-connect').disabled = d.connected;
  const cfg = d.config || {};
  if (cfg.interface) $('vna-interface').value = cfg.interface;
  if (cfg.resource) $('vna-resource').value = cfg.resource;
  syncVnaInterface();
  if (cfg.host) $('vna-ip').value = cfg.host;
  if (cfg.port) $('vna-port').value = cfg.port;
  if (cfg.timeout_ms) $('vna-timeout').value = cfg.timeout_ms;
  if (cfg.start_hz) $('vna-start').value = cfg.start_hz;
  if (cfg.stop_hz) $('vna-stop').value = cfg.stop_hz;
  if (cfg.points) $('vna-points').value = cfg.points;
  if (cfg.power_dbm !== undefined) $('vna-power').value = cfg.power_dbm;
  if (cfg.ifbw_hz) $('vna-ifbw').value = cfg.ifbw_hz;
  if (cfg.vendor_key) $('vna-vendor').value = cfg.vendor_key;
}

function vnaConfigBody() {
  return {
    vendor_key: $('vna-vendor').value,
    interface: $('vna-interface').value,
    host: $('vna-ip').value.trim(),
    port: parseInt($('vna-port').value, 10),
    resource: $('vna-resource').value.trim(),
    timeout_ms: parseInt($('vna-timeout').value, 10),
    start_hz: parseFloat($('vna-start').value),
    stop_hz: parseFloat($('vna-stop').value),
    points: parseInt($('vna-points').value, 10),
    power_dbm: parseFloat($('vna-power').value),
    ifbw_hz: parseFloat($('vna-ifbw').value),
  };
}

$('vna-connect').onclick = async () => {
  const r = await api('POST', '/api/vna/connect', vnaConfigBody());
  showMsg($('vna-msg'), r.ok ? '已连接: ' + (r.data.idn || '') : '连接失败: ' + r.error,
    r.ok ? 'ok' : 'error');
  loadVnaStatus();
};
$('vna-disconnect').onclick = async () => {
  const r = await api('POST', '/api/vna/disconnect', {});
  showMsg($('vna-msg'), r.ok ? '已断开' : '断开失败: ' + r.error, r.ok ? 'info' : 'error');
  loadVnaStatus();
};
$('vna-idn').onclick = async () => {
  const r = await api('POST', '/api/vna/idn', {});
  showMsg($('vna-msg'), r.ok ? 'IDN: ' + (r.data.idn || '') : '读取失败: ' + r.error,
    r.ok ? 'info' : 'error');
  loadVnaStatus();
};
$('vna-set-sweep').onclick = async () => {
  const r = await api('POST', '/api/vna/set_sweep', {
    start_hz: parseFloat($('vna-start').value), stop_hz: parseFloat($('vna-stop').value),
    points: parseInt($('vna-points').value, 10),
  });
  showMsg($('vna-msg'), r.ok ? '扫频设置已下发' : '下发失败: ' + r.error, r.ok ? 'info' : 'error');
  loadVnaStatus();
};
$('vna-set-power').onclick = async () => {
  const r = await api('POST', '/api/vna/set_power', { power_dbm: parseFloat($('vna-power').value) });
  const r2 = await api('POST', '/api/vna/set_ifbw', { ifbw_hz: parseFloat($('vna-ifbw').value) });
  showMsg($('vna-msg'), (r.ok && r2.ok) ? '功率/中频已下发' : '下发失败: ' + (r.error || r2.error),
    (r.ok && r2.ok) ? 'info' : 'error');
  loadVnaStatus();
};
$('vna-trigger').onclick = async () => {
  const r = await api('POST', '/api/vna/fetch', {});
  if (!r.ok) { showMsg($('vna-msg'), '读取失败: ' + r.error, 'error'); return; }
  showMsg($('vna-msg'), '已触发扫描并读取 ' + r.data.frequencies_hz.length + ' 个频点', 'ok');
  drawVna(r.data);
};

function drawVna(data) {
  const f = data.frequencies_hz;
  const s = data.s;
  // table
  const tb = $('vna-table').querySelector('tbody');
  tb.innerHTML = '';
  const step = Math.max(1, Math.ceil(f.length / 60));
  for (let i = 0; i < f.length; i += step) {
    const tr = document.createElement('tr');
    const row = [f[i], s.S11.mag_db[i], s.S21.mag_db[i], s.S12.mag_db[i], s.S22.mag_db[i]];
    tr.innerHTML = row.map((v, j) => `<td class="num">${j === 0 ? v.toExponential(3) : v.toFixed(2)}</td>`).join('');
    tb.appendChild(tr);
  }
  // plot |S21| and |S11| vs frequency (GHz)
  const canvas = $('vna-canvas');
  const ctx = canvas.getContext('2d');
  const W = canvas.width, H = canvas.height;
  ctx.clearRect(0, 0, W, H);
  const m = Math.min(W, H);
  const padL = 48, padR = 14, padT = 16, padB = 28;
  const x0 = padL, x1 = W - padR, y0 = H - padB, y1 = padT;
  const fg = f.map((v) => v / 1e9);
  const all = [];
  ['S11', 'S21', 'S12', 'S22'].forEach((k) => all.push(...s[k].mag_db));
  let lo = Math.min(...all), hi = Math.max(...all);
  if (!isFinite(lo) || !isFinite(hi) || lo === hi) { lo = -60; hi = 10; }
  const xr = [fg[0], fg[fg.length - 1]];
  const sx = (v) => x0 + (x1 - x0) * (v - xr[0]) / (xr[1] - xr[0] || 1);
  const sy = (v) => y0 + (y1 - y0) * (v - lo) / (hi - lo || 1);
  // axes
  ctx.strokeStyle = '#35506b'; ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(x0, y1); ctx.lineTo(x0, y0); ctx.lineTo(x1, y0); ctx.stroke();
  ctx.fillStyle = '#35506b'; ctx.font = '11px sans-serif';
  ctx.fillText(hi.toFixed(0) + ' dB', 4, y1 + 4);
  ctx.fillText(lo.toFixed(0) + ' dB', 4, y0);
  ctx.fillText(xr[0].toFixed(3) + ' GHz', x0, H - 8);
  ctx.fillText(xr[1].toFixed(3) + ' GHz', x1 - 60, H - 8);
  const series = [['S11', '#42c5b7'], ['S21', '#ff6582'], ['S12', '#ffbc5a'], ['S22', '#9b8cff']];
  series.forEach(([k, color]) => {
    ctx.strokeStyle = color; ctx.lineWidth = 1.4; ctx.beginPath();
    for (let i = 0; i < f.length; i++) {
      const px = sx(fg[i]), py = sy(s[k].mag_db[i]);
      i ? ctx.lineTo(px, py) : ctx.moveTo(px, py);
    }
    ctx.stroke();
  });
  // legend
  ctx.font = '11px sans-serif';
  let lx = x0 + 6;
  series.forEach(([k, color]) => {
    ctx.fillStyle = color; ctx.fillRect(lx, y1 + 2, 10, 10);
    ctx.fillStyle = '#35506b'; ctx.fillText(k, lx + 14, y1 + 11);
    lx += 56;
  });
}

/* ------------------------------------------------------------ operation log */
let logPoll = null;
function startLogPoll() { stopLogPoll(); loadOplog(); logPoll = setInterval(loadOplog, 2000); }
function stopLogPoll() { if (logPoll) { clearInterval(logPoll); logPoll = null; } }

async function loadOplog() {
  const body = {
    limit: parseInt($('log-limit').value, 10) || 200,
    category: $('log-category').value || '',
    level: $('log-level').value || '',
  };
  const r = await api('POST', '/api/log/recent', body);
  if (!r.ok) return;
  const tb = $('log-table').querySelector('tbody');
  tb.innerHTML = '';
  r.data.entries.forEach((e) => {
    const tr = document.createElement('tr');
    if (e.level === 'error') tr.className = 'row-fault';
    else if (e.level === 'warn') tr.className = 'row-warn';
    [e.time_text, e.category, e.action, e.detail, e.level].forEach((c, i) => {
      const td = document.createElement('td');
      if (i === 0 || i === 4) td.className = 'num';
      td.textContent = c;
      tr.appendChild(td);
    });
    tb.appendChild(tr);
  });
}

$('log-refresh').onclick = loadOplog;
$('log-clear').onclick = async () => {
  const r = await api('POST', '/api/log/clear', {});
  showMsg($('log-msg'), r.ok ? '已清空 ' + r.data.removed + ' 条记录' : '清空失败: ' + r.error,
    r.ok ? 'ok' : 'error');
  loadOplog();
};
$('log-export').onclick = async () => {
  const path = window.prompt('导出到哪个文件？', 'tunerlab_operations.json');
  if (!path) return;
  const r = await api('POST', '/api/log/export', { path: path });
  showMsg($('log-msg'), r.ok ? '已导出到 ' + r.data.path : '导出失败: ' + r.error,
    r.ok ? 'ok' : 'error');
};

/* --------------------------------------------- studio: Smith-chart planning */
function planParams() {
  return {
    density: $('st-density').value,
    method: $('st-plan-method').value,
    r_min: parseFloat($('st-rmin').value),
    r_max: parseFloat($('st-rmax').value),
  };
}

function drawPlanCanvas(targets, measured) {
  const cv = $('st-plan-canvas');
  const ctx = cv.getContext('2d');
  const W = cv.width;
  const H = cv.height;
  ctx.clearRect(0, 0, W, H);
  const cx = W / 2;
  const cy = H / 2;
  const R = Math.min(W, H) / 2 - 18;
  ctx.strokeStyle = '#94a3b8';
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.arc(cx, cy, R, 0, 2 * Math.PI);
  ctx.stroke();
  ctx.beginPath();
  ctx.moveTo(cx - R, cy); ctx.lineTo(cx + R, cy);
  ctx.moveTo(cx, cy - R); ctx.lineTo(cx, cy + R);
  ctx.stroke();
  ctx.fillStyle = '#64748b';
  ctx.font = '11px sans-serif';
  ctx.fillText('Γ 实轴', cx + 6, cy - 6);
  const plot = (list, color, radius) => {
    (list || []).forEach((p) => {
      const x = cx + p.re * R;
      const y = cy - p.im * R;
      ctx.fillStyle = color;
      ctx.beginPath();
      ctx.arc(x, y, radius, 0, 2 * Math.PI);
      ctx.fill();
    });
  };
  plot(targets, 'rgba(37,99,235,0.35)', 2);
  plot(measured, '#dc2626', 2.5);
}

$('st-plan-preview').onclick = async () => {
  const p = planParams();
  const r = await api('POST', '/api/studio/plan/preview', p);
  if (!r.ok) { showMsg($('st-plan-msg'), '预览失败: ' + r.error, 'error'); return; }
  drawPlanCanvas(r.data.targets, r.data.measured);
  const cov = r.data.coverage || {};
  $('st-plan-coverage').textContent = cov.count
    ? `${cov.count} 点 · 平均间距 ${Number(cov.mean_spacing).toFixed(4)} · ` +
      `最大间距 ${Number(cov.max_spacing).toFixed(4)} · 覆盖 ${(Number(cov.coverage) * 100).toFixed(1)}%`
    : '尚无已测点（先运行一次校准）';
  showMsg($('st-plan-msg'),
    `目标布点 ${r.data.count} 个（${r.data.density} · ${r.data.method}）` +
    (r.data.measured.length ? `，已测 ${r.data.measured.length} 点` : ''), 'ok');
};

$('st-plan-refine').onclick = async () => {
  const p = planParams();
  const r = await api('POST', '/api/studio/plan/refine', p);
  if (!r.ok) { showMsg($('st-plan-msg'), '细化布点失败: ' + r.error, 'error'); return; }
  const b = r.data.coverage_before;
  const a = r.data.coverage_projected;
  showMsg($('st-plan-msg'),
    `已追加 ${r.data.added} 个点到当前校准库（待测 ${r.data.pending} 点）。` +
    ` 平均间距 ${Number(b.mean_spacing).toFixed(4)} → ${Number(a.mean_spacing).toFixed(4)}（投影）`,
    'ok');
  $('st-plan-coverage').textContent =
    `${b.count} 点 · 平均间距 ${Number(b.mean_spacing).toFixed(4)} · 投影 ${Number(a.mean_spacing).toFixed(4)}`;
  const pv = await api('POST', '/api/studio/plan/preview', p);
  if (pv.ok) drawPlanCanvas(pv.data.targets, pv.data.measured);
};

/* ------------------------------------------------------- P1 tools (0.4.1) */
async function loadP1ReferencePlanes() {
  const r = await api('POST', '/api/studio/reference_planes', {});
  const sel = $('p1-rp-plane');
  if (!r.ok || !r.data) return;
  sel.innerHTML = '';
  r.data.planes.forEach((p) => {
    const opt = document.createElement('option');
    opt.value = p.id;
    opt.textContent = `${p.name}${p.deembed ? '（可去嵌入）' : ''}`;
    sel.appendChild(opt);
  });
}

$('p1-rp-validate').onclick = async () => {
  const r = await api('POST', '/api/studio/reference_planes', {
    reference_plane: $('p1-rp-plane').value,
    deembed_s2p: $('p1-rp-s2p').value.trim(),
  });
  if (!r.ok) { showMsg($('p1-rp-result'), '校验失败: ' + r.error, 'error'); return; }
  const s = r.data.setup;
  let txt = `参考面: ${s.reference_plane_name}（合法=${s.reference_plane_ok}，可去嵌入=${s.deembed_allowed}）\n`;
  if (s.coverage) {
    txt += `S2P 覆盖: 通过=${s.coverage.passed} 覆盖 ${s.coverage.covered}/${s.coverage.library_count}`;
    if (s.coverage.missing && s.coverage.missing.length)
      txt += `\n缺频点(GHz): ${s.coverage.missing.map((f) => (f / 1e9).toFixed(6)).join(', ')}`;
    if (s.coverage.reason) txt += `\n说明: ${s.coverage.reason}`;
  }
  $('p1-rp-result').textContent = txt;
};

$('p1-br-scan').onclick = async () => {
  const r = await api('POST', '/api/studio/library/scan', {
    directory: $('p1-br-dir').value.trim(),
    recursive: $('p1-br-recursive').checked,
  });
  if (!r.ok) { showMsg($('p1-br-result'), '扫描失败: ' + r.error, 'error'); return; }
  const tb = $('p1-br-table').querySelector('tbody');
  tb.innerHTML = '';
  const a = $('p1-br-a'), b = $('p1-br-b');
  a.innerHTML = ''; b.innerHTML = '';
  r.data.libraries.forEach((lib) => {
    const tr = document.createElement('tr');
    tr.innerHTML = `<td>${lib.name}</td><td>${lib.cal_id}</td>`
      + `<td class="num">${lib.points_valid}/${lib.points_total}</td>`
      + `<td class="num">${lib.gamma_max != null ? lib.gamma_max.toFixed(4) : '—'}</td>`
      + `<td class="num">${lib.frequency_ghz.map((f) => f.toFixed(3)).join(',')}</td>`
      + `<td>${lib.reference_plane || '—'}</td>`;
    tb.appendChild(tr);
    [a, b].forEach((sel) => {
      const opt = document.createElement('option');
      opt.value = lib.path; opt.textContent = lib.name; sel.appendChild(opt);
    });
  });
  let txt = `扫描目录: ${r.data.directory}\n库数量: ${r.data.libraries.length}`;
  if (r.data.errors.length) txt += `\n跳过(损坏): ${r.data.errors.length}`;
  $('p1-br-result').textContent = txt;
};

$('p1-br-compare').onclick = async () => {
  const r = await api('POST', '/api/studio/library/compare', {
    path_a: $('p1-br-a').value, path_b: $('p1-br-b').value,
    frequency_hz: parseFloat($('p1-br-freq').value) * 1e9,
  });
  if (!r.ok) { showMsg($('p1-br-result'), '对比失败: ' + r.error, 'error'); return; }
  const d = r.data;
  let txt = `A: ${d.points_a} 点 | B: ${d.points_b} 点 (Δ=${d.points_delta})\n`
    + `频率网格一致: ${d.freq_match}\n`
    + `|Γ|max A=${d.gamma_max_a != null ? d.gamma_max_a.toFixed(4) : '—'} / B=${d.gamma_max_b != null ? d.gamma_max_b.toFixed(4) : '—'}`;
  if (d.diffable) txt += `\n平均|ΔΓ|=${d.mean_delta_gamma.toFixed(6)} 最大|ΔΓ|=${d.max_delta_gamma.toFixed(6)}`;
  else if (d.diff_error) txt += `\n不可差分: ${d.diff_error}`;
  $('p1-br-result').textContent = txt;
};

$('p1-ex-run').onclick = async () => {
  const r = await api('POST', '/api/studio/export', {
    format: $('p1-ex-fmt').value, path: $('p1-ex-path').value.trim(),
  });
  if (!r.ok) { showMsg($('p1-ex-result'), '导出失败: ' + r.error, 'error'); return; }
  $('p1-ex-result').textContent = JSON.stringify(r.data, null, 2);
};

$('p1-op-run').onclick = async () => {
  const action = $('p1-op-action').value;
  const body = { action, path: $('p1-op-path').value.trim() };
  if (action === 'truncate_gamma') body.gmax = parseFloat($('p1-op-gmax').value);
  if (action === 'reverse_probe') body.axis = $('p1-op-axis').value.trim();
  if (action === 'apply_fixture') {
    body.fixture_s2p = $('p1-op-fx').value.trim();
    body.direction = $('p1-op-dir').value;
  }
  const r = await api('POST', '/api/studio/library/derive', body);
  if (!r.ok) { showMsg($('p1-op-result'), '执行失败: ' + r.error, 'error'); return; }
  $('p1-op-result').textContent = JSON.stringify(r.data, null, 2);
};

$('p1-rep-run').onclick = async () => {
  let measurements;
  try { measurements = JSON.parse($('p1-rep-json').value); }
  catch (e) { showMsg($('p1-rep-result'), 'JSON 解析失败: ' + e.message, 'error'); return; }
  const r = await api('POST', '/api/studio/repeatability', {
    measurements, tolerance: parseFloat($('p1-rep-tol').value),
  });
  if (!r.ok) { showMsg($('p1-rep-result'), '分析失败: ' + r.error, 'error'); return; }
  const d = r.data;
  let txt = `N=${d.n} 容差=${d.tolerance}\n`
    + `平均|ΔΓ|=${d.mean_delta_gamma.toFixed(6)} σ=${d.std_delta_gamma.toFixed(6)}\n`
    + `最大|ΔΓ|=${d.max_delta_gamma.toFixed(6)} reset=${d.reset_dB == null ? '∞' : d.reset_dB.toFixed(2)} dB\n`
    + `全部在容差内: ${d.all_within_tolerance}`;
  if (d.high_gamma) txt += `\n高|Γ|区: N=${d.high_gamma.n} 最大|ΔΓ|=${d.high_gamma.max_delta_gamma.toFixed(6)}`;
  $('p1-rep-result').textContent = txt;
};

$('p1-dv-run').onclick = async () => {
  const r = await api('POST', '/api/studio/deviation', {
    predicted_gamma_re: parseFloat($('p1-dv-pg-re').value),
    predicted_gamma_im: parseFloat($('p1-dv-pg-im').value),
    measured_gamma_re: parseFloat($('p1-dv-mg-re').value),
    measured_gamma_im: parseFloat($('p1-dv-mg-im').value),
    tolerance: parseFloat($('p1-dv-tol').value),
  });
  if (!r.ok) { showMsg($('p1-dv-result'), '生成失败: ' + r.error, 'error'); return; }
  const d = r.data;
  let txt = `带: ${d.gamma_band}  矢量误差|ΔΓ|=${d.vector_error.toFixed(6)}  在容差内=${d.within_tolerance}\n`
    + `建议容差: ${d.suggested_tolerance}\n`
    + `预测 Γ=${d.predicted.gamma_label} VSWR=${d.predicted.vswr} Z=${d.predicted.z_label}\n`
    + `实测 Γ=${d.measured.gamma_label} VSWR=${d.measured.vswr} Z=${d.measured.z_label}\n`
    + `Z 偏差 ΔR=${d.impedance_delta_r.toFixed(3)} ΔX=${d.impedance_delta_x.toFixed(3)} Ω\n`
    + `VSWR 偏差: ${d.vswr_delta != null ? d.vswr_delta.toFixed(3) : '—'}`;
  $('p1-dv-result').textContent = txt;
};

/* ------------------------------------------------- P2 tools (0.5.0) */
/* Reference-plane shift: the library is referred to another plane. */
$('p2-rs-run').onclick = async () => {
  const body = {
    kind: $('p2-rs-kind').value,
    length_mm: parseFloat($('p2-rs-len').value),
    velocity_factor: parseFloat($('p2-rs-vf').value),
    fixture_s2p: $('p2-rs-s2p').value.trim(),
    direction: $('p2-rs-dir').value,
    reference_plane: $('p2-rs-plane').value,
    path: $('p2-rs-path').value.trim(),
  };
  const r = await api('POST', '/api/studio/reference_shift', body);
  if (!r.ok) { showMsg($('p2-rs-result'), '平移失败: ' + r.error, 'error'); return; }
  const d = r.data;
  showMsg($('p2-rs-result'),
    `平移完成\n新库: ${d.path}\n点数: ${d.points}\n网络: ${d.network}\n方向: ${d.direction}`
    + `\n参考面: ${d.reference_plane || '(未指定)'}`, 'ok');
};

/* Automatic A→B→A: this one MOVES the machine. */
$('p2-ra-run').onclick = async () => {
  const body = {
    repeats: parseInt($('p2-ra-rep').value, 10),
    tolerance: parseFloat($('p2-ra-tol').value),
    frequency_hz: parseFloat($('p2-ra-freq').value) * 1e9,
  };
  const r = await api('POST', '/api/studio/repeatability_auto', body);
  if (!r.ok) { showMsg($('p2-ra-result'), '启动失败: ' + r.error, 'error'); return; }
  showMsg($('p2-ra-result'),
    `采集已在后台启动（${r.data.repeats} 个循环）。\n完成后刷新验证报告页查看结果。`, 'info');
};

/* Pattern editor: store a target pattern and list what is stored. */
function _patternParameters() {
  return {
    radius: parseFloat($('p2-pt-radius').value),
    phase_start: parseFloat($('p2-pt-p0').value),
    phase_stop: parseFloat($('p2-pt-p1').value),
    count: parseInt($('p2-pt-count').value, 10),
    r_from: parseFloat($('p2-pt-r0').value),
    r_to: parseFloat($('p2-pt-r1').value),
    phase: parseFloat($('p2-pt-phase').value),
  };
}

function _manualPoints() {
  const raw = $('p2-pt-points').value.trim();
  if (!raw) return [];
  return raw.split(/[\s;]+/).filter(Boolean).map((pair) => {
    const [re, im] = pair.split(',').map((v) => parseFloat(v) || 0);
    return [re, im];
  });
}

$('p2-pt-save').onclick = async () => {
  const kind = $('p2-pt-kind').value;
  const body = {
    name: $('p2-pt-name').value.trim(),
    kind,
    parameters: _patternParameters(),
  };
  if (kind === 'points') body.points = _manualPoints();
  const r = await api('POST', '/api/studio/patterns', body);
  if (!r.ok) { showMsg($('p2-pt-result'), '保存失败: ' + r.error, 'error'); return; }
  showMsg($('p2-pt-result'), `已保存: ${r.data.name}（${r.data.count} 个点）`, 'ok');
  listPatterns();
};

async function listPatterns() {
  const r = await api('GET', '/api/studio/patterns', {});
  const box = $('p2-pt-list-box');
  box.innerHTML = '';
  if (!r.ok || !r.data.patterns.length) return;
  const table = document.createElement('table');
  table.innerHTML = '<thead><tr><th>名称</th><th>类型</th><th>点数</th><th>操作</th></tr></thead>';
  const body = document.createElement('tbody');
  r.data.patterns.forEach((p) => {
    const tr = document.createElement('tr');
    tr.innerHTML = `<td>${p.name}</td><td>${p.kind}</td><td>${p.count}</td>`;
    const cell = document.createElement('td');
    const del = document.createElement('button');
    del.className = 'btn';
    del.textContent = '删除';
    del.onclick = async () => {
      await api('POST', '/api/studio/patterns/delete', { path: p.path });
      listPatterns();
    };
    cell.appendChild(del);
    tr.appendChild(cell);
    body.appendChild(tr);
  });
  table.appendChild(body);
  box.appendChild(table);
}

$('p2-pt-list').onclick = listPatterns;

/* Audit: the read-only evidence trail. */
$('p2-au-run').onclick = async () => {
  const r = await api('POST', '/api/studio/audit', {
    directory: $('p2-au-dir').value.trim(),
  });
  const box = $('p2-au-box');
  box.innerHTML = '';
  if (!r.ok) { showMsg(box, '审计失败: ' + r.error, 'error'); return; }
  const d = r.data;
  const head = document.createElement('p');
  head.className = 'hint';
  head.textContent = `${d.library_count} 个校准库 · 合计有效点 ${d.total_points}`
    + ` · ${d.validation_count} 条验证记录 · ${d.fault_count} 条故障`;
  box.appendChild(head);

  const table = document.createElement('table');
  table.innerHTML = '<thead><tr><th>库名</th><th>有效点/总点</th><th>频率 (GHz)</th>'
    + '<th>参考面</th><th>SHA-256</th></tr></thead>';
  const body = document.createElement('tbody');
  d.libraries.forEach((lib) => {
    const tr = document.createElement('tr');
    tr.innerHTML = `<td>${lib.name}</td>`
      + `<td>${lib.points_valid}/${lib.points_total}</td>`
      + `<td>${(lib.frequency_ghz || []).map((v) => v.toFixed(4)).join(', ')}</td>`
      + `<td>${lib.reference_plane || '—'}</td>`
      + `<td>${(lib.file_sha256 || '').slice(0, 16)}</td>`;
    body.appendChild(tr);
  });
  table.appendChild(body);
  box.appendChild(table);
};

/* ------------------------------------------- VNA SCPI debug probe */
let lastVnaProbe = null;

async function runVnaProbe(disconnectFirst) {
  const out = $('vna-probe-out');
  const runBtn = $('vna-probe-run');
  runBtn.disabled = true;
  out.textContent = '探测中：正在连接仪器并逐条试验 SCPI 命令，请稍候…';
  try {
    if (disconnectFirst) {
      const d = await api('POST', '/api/vna/disconnect', {});
      if (!d.ok) { out.textContent = '断开失败: ' + d.error; return; }
    }
    const body = vnaConfigBody();
    body.timeout_ms = Math.min(3000, Math.max(500, parseInt($('vna-timeout').value, 10) || 3000));
    const r = await api('POST', '/api/vna/probe', body);
    if (!r.ok) { out.textContent = '探测失败: ' + r.error; return; }
    lastVnaProbe = r.data;
    out.textContent = r.data.report || JSON.stringify(r.data, null, 2);
    showMsg($('vna-msg'), 'SCPI 探测完成，可复制报告或应用方言', 'ok');
    await loadVnaStatus();
  } finally {
    runBtn.disabled = false;
  }
}
$('vna-probe-run').onclick = () => runVnaProbe(false);
$('vna-probe-disconnect').onclick = () => runVnaProbe(true);

$('vna-probe-apply').onclick = async () => {
  const dialect = lastVnaProbe && lastVnaProbe.suggested_dialect;
  if (!dialect || !Object.keys(dialect).length) {
    showMsg($('vna-msg'), '没有可应用的方言：请先运行探测，或检查探测结果', 'error');
    return;
  }
  const r = await api('POST', '/api/vna/apply_dialect', { dialect });
  showMsg($('vna-msg'), r.ok ? '已应用方言，下次连接生效：' + JSON.stringify(dialect)
                             : '应用失败: ' + r.error, r.ok ? 'ok' : 'error');
};

$('vna-probe-copy').onclick = async () => {
  const text = $('vna-probe-out').textContent || '';
  try {
    await navigator.clipboard.writeText(text);
    showMsg($('vna-msg'), '报告已复制到剪贴板', 'ok');
  } catch (e) {
    showMsg($('vna-msg'), '复制失败，请手动选中文本: ' + e.message, 'error');
  }
};

/* ------------------------------------------------------------- bootstrap */
syncMotionSource();
syncVnaInterface();
syncStVnaInterface();
updateGridCount();
loadVnaVendors();
loadHwSettings();
showPage('hw-comm');
