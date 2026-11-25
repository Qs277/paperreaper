'use strict';

/* =========================================================
 * PaperReaper · 作业终结者
 * 识图：视觉模型解析卷面 -> 结构化题目 / 答案 / 作答坐标
 * 生图：Canvas 精准落笔 或 图像模型整页重绘
 * ========================================================= */

/* ---------------- 工具 ---------------- */
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
// 给可能已被移除的控件绑定事件（不存在就跳过）
const on = (sel, ev, fn) => { const el = $(sel); if (el) el.addEventListener(ev, fn); };

const clamp = (v, min, max) => Math.min(max, Math.max(min, v));
const pct = v => Math.round(v * 100) + '%';

function escapeHtml(str) {
  return String(str ?? '').replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[c]));
}

function fileToDataUrl(file) {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(r.result);
    r.onerror = () => reject(new Error('读取文件失败'));
    r.readAsDataURL(file);
  });
}

function blobToDataUrl(blob) {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(r.result);
    r.onerror = () => reject(new Error('转换图片失败'));
    r.readAsDataURL(blob);
  });
}

function loadImage(src) {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => resolve(img);
    img.onerror = () => reject(new Error('图片加载失败'));
    img.src = src;
  });
}

function dataUrlToBlob(dataUrl) {
  const [head, body] = dataUrl.split(',');
  const mime = (head.match(/data:([^;]+)/) || [])[1] || 'image/png';
  const bin = atob(body);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return new Blob([bytes], { type: mime });
}

/* ---------------- 状态 ---------------- */
const CONFIG_KEY = 'paperreaper-config-v1';

const DEFAULTS = {
  vision: {
    base: 'https://dashscope.aliyuncs.com/compatible-mode/v1',
    model: 'qwen3-vl-plus',
    key: ''
  },
  llm: {
    base: 'https://api.deepseek.com/v1',
    model: 'deepseek-chat',
    key: ''
  }
};

const FONTS = {
  kai: '"Segoe Script","Ink Free","华文楷体","KaiTi","STKaiti",serif',
  hand: '"Segoe Script","Ink Free","华文行楷","STXingkai","KaiTi",cursive',
  print: '"Microsoft YaHei","微软雅黑","PingFang SC",sans-serif'
};

const INK_TONES = {
  heavy: { color: '#0d0d0d', label: '浓黑' },
  normal: { color: '#1c1c1c', label: '标准黑' },
  light: { color: '#3b3b3b', label: '淡墨' }
};

const TRACE_LEVELS = ['none', 'less', 'more'];
const TRACE_HINTS = {
  none: '不留做题痕迹，只写答案。',
  less: '只圈重点题的关键词，痕迹更干净。',
  more: '每题都圈画关键词、题号打勾、划掉错误选项。'
};

const state = {
  vision: { ...DEFAULTS.vision },
  llm: { ...DEFAULTS.llm },
  pages: [],
  active: 0,
  output: 'agent',
  style: 'kai',
  selectedQ: -1,
  inkTone: 'normal',
  ansInk: 'normal',
  traceLevel: 'more',
  requirement: '',
  reqScale: 1,
  fontProfile: null,
  human: { accuracy: 95, correction: 35, neatness: 35, size: 120 },
  busy: false
};

function inkColor() {
  return (INK_TONES[state.inkTone] || INK_TONES.normal).color;
}

function hexToRgb(hex) {
  const m = String(hex || '').replace('#', '').match(/^([0-9a-f]{6})$/i);
  if (!m) return null;
  const n = parseInt(m[1], 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

/* 把「字迹细节」和已学习的字迹档案翻译成引擎的风格指纹（整页统一，不逐字随机） */
function styleProfile() {
  const neat = clamp(Number(state.human.neatness) || 50, 0, 100);
  const size = clamp(Number(state.human.size) || 100, 80, 150);
  const tone = state.inkTone || 'normal';
  const prof = {
    x_height: clamp(0.96 + (size - 100) / 100 * 0.30, 0.93, 1.08),
    spacing: clamp(0.95 + (neat - 50) / 100 * 0.10, 0.93, 1.07),
    slant: clamp(0.02 + (neat - 55) / 100 * 0.07, -0.05, 0.10),
    baseline: clamp(1.30 - neat / 100 * 0.60, 0.65, 1.35),
    pressure: clamp(0.06 + (100 - neat) / 100 * 0.12, 0.06, 0.20),
    stroke: tone === 'heavy' ? 1 : (tone === 'light' ? -1 : 0)
  };
  const rgb = hexToRgb(inkColor());
  if (tone !== 'normal' && rgb) prof.ink = rgb;
  return prof;
}

let pageSeq = 0;
let boxEls = [];

/* ---------------- 配置 ---------------- */
function loadConfig() {
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem(CONFIG_KEY) || 'null'); } catch { saved = null; }
  const pick = (v, fallback) => (typeof v === 'string' && v.trim() ? v.trim() : fallback);
  // 兼容旧版：单个 apiKey 迁移为识图 Key
  state.vision.key = pick(saved?.vision?.key, pick(saved?.apiKey, DEFAULTS.vision.key));
  state.vision.base = pick(saved?.vision?.base, pick(saved?.baseUrl, DEFAULTS.vision.base));
  state.vision.model = pick(saved?.vision?.model, pick(saved?.visionModel, DEFAULTS.vision.model));
  state.llm.key = pick(saved?.llm?.key, DEFAULTS.llm.key);
  state.llm.base = pick(saved?.llm?.base, DEFAULTS.llm.base);
  state.llm.model = pick(saved?.llm?.model, DEFAULTS.llm.model);
  if (/v4\.1/i.test(state.llm.model)) state.llm.model = DEFAULTS.llm.model;
  // 识图恢复千问；若旧配置被改成了 DeepSeek，自动改回千问
  if (/deepseek/i.test(state.vision.base + ' ' + state.vision.model)) {
    state.vision.base = DEFAULTS.vision.base;
    state.vision.model = DEFAULTS.vision.model;
  }
  state.human.accuracy = Number.isFinite(saved?.accuracy) ? saved.accuracy : 95;
  state.human.correction = Number.isFinite(saved?.correction) ? saved.correction : 35;
  state.human.neatness = Number.isFinite(saved?.neatness) ? saved.neatness : 35;
  state.human.size = Number.isFinite(saved?.size) ? saved.size : 120;
  if (saved?.inkTone && INK_TONES[saved.inkTone]) state.inkTone = saved.inkTone;
  state.traceLevel = TRACE_LEVELS.includes(saved?.traceLevel) ? saved.traceLevel : 'more';
  state.requirement = typeof saved?.requirement === 'string' ? saved.requirement : '';
  if (saved?.fontProfile && typeof saved.fontProfile === 'object') state.fontProfile = saved.fontProfile;
}

function saveConfig(silent = false) {
  const payload = {
    vision: { ...state.vision },
    llm: { ...state.llm },
    accuracy: state.human.accuracy,
    correction: state.human.correction,
    neatness: state.human.neatness,
    size: state.human.size,
    inkTone: state.inkTone,
    traceLevel: state.traceLevel,
    requirement: state.requirement,
    fontProfile: state.fontProfile
  };
  try {
    localStorage.setItem(CONFIG_KEY, JSON.stringify(payload));
  } catch {
    // 字迹缩略图可能撑爆配额，退化成只存关键字段
    delete payload.fontProfile;
    try { localStorage.setItem(CONFIG_KEY, JSON.stringify(payload)); } catch { /* 忽略 */ }
  }
  if (!silent) toast('配置已保存', 'ok');
}

function syncSettingsUI() {
  $('#visionBase').value = state.vision.base;
  $('#visionModel').value = state.vision.model;
  $('#visionKey').value = state.vision.key;
  $('#llmBase').value = state.llm.base;
  $('#llmModel').value = state.llm.model;
  $('#llmKey').value = state.llm.key;
  updateStatusChip();
}

function updateKeyState(sel, key) {
  const el = $(sel);
  if (!el) return;
  el.textContent = key ? '已配置' : '未配置';
  el.className = 'key-state ' + (key ? 'ok' : 'off');
}

function updateStatusChip() {
  const chip = $('#statusChip');
  const text = $('#statusText');
  const v = (state.vision.key || '').trim();
  const l = (state.llm.key || '').trim();
  updateKeyState('#visionState', v);
  updateKeyState('#llmState', l);
  if (v && l) {
    chip.classList.add('ok');
    text.textContent = `${state.vision.model} + ${state.llm.model}`;
  } else {
    chip.classList.remove('ok');
    text.textContent = v ? '缺解题 Key' : (l ? '缺识图 Key' : '未配置模型 Key');
  }
}

function syncHumanUI() {
  const h = state.human;
  $('#sizeRange').value = h.size;
  $('#corRange').value = h.correction;
  $('#accRange').value = h.accuracy;
  $('#neatRange').value = h.neatness;
  $('#sizeNum').textContent = h.size + '%';
  $('#corNum').textContent = h.correction + '%';
  $('#accNum').textContent = h.accuracy + '%';
  $('#neatNum').textContent = h.neatness + '%';
  syncInkUI();
  updateFontLock();
}

function syncInkUI() {
  $$('#inkSwatches button').forEach(b => b.classList.toggle('active', b.dataset.ink === state.inkTone));
}

function syncTraceUI() {
  const level = TRACE_LEVELS.includes(state.traceLevel) ? state.traceLevel : 'more';
  $$('#traceSeg button').forEach(b => b.classList.toggle('active', b.dataset.trace === level));
  const hint = $('#traceHint');
  if (hint) hint.textContent = TRACE_HINTS[level];
}

function updateFontLock() {
  const learned = !!state.fontProfile;
  $('#sizeRange').disabled = learned;
  $('#neatRange').disabled = learned;
  $$('#inkSwatches button').forEach(b => { b.disabled = learned; });

  const badge = $('#fontBadge');
  if (badge) badge.hidden = !learned;
  const clearBtn = $('#btnLearnClear');
  if (clearBtn) clearBtn.hidden = !learned;

  const note = $('#fontNote');
  if (!note) return;
  if (learned) {
    const p = state.fontProfile;
    const tone = INK_TONES[p.tone]?.label || '标准黑';
    note.textContent = `已接管：墨色 ${tone} · 字号 ${p.size}% · 工整 ${p.neatness}%${p.note ? ' · ' + p.note : ''}`;
    $('#sizeRange').title = '已由字迹档案接管，清除学习后可手动调节';
    $('#neatRange').title = '已由字迹档案接管，清除学习后可手动调节';
  } else {
    note.textContent = '';
    $('#sizeRange').title = '';
    $('#neatRange').title = '';
  }
}

/* ---------------- 日志 / 提示 / 忙碌 ---------------- */
function log(msg, type = '') {
  const box = $('#log');
  const empty = box.querySelector('.empty');
  if (empty) empty.remove();
  const line = document.createElement('div');
  line.className = 'line';
  const time = new Date().toLocaleTimeString('zh-CN', { hour12: false });
  line.innerHTML = `<span class="t">${time}</span><span class="${type}">${escapeHtml(msg)}</span>`;
  box.appendChild(line);
  box.scrollTop = box.scrollHeight;
}

function toast(msg, type = '') {
  const wrap = $('#toastWrap');
  const el = document.createElement('div');
  el.className = 'toast ' + type;
  el.textContent = msg;
  wrap.appendChild(el);
  setTimeout(() => {
    el.style.transition = 'opacity .25s, transform .25s';
    el.style.opacity = '0';
    el.style.transform = 'translateY(6px)';
    setTimeout(() => el.remove(), 260);
  }, type === 'error' ? 5200 : 3200);
}

function setBusy(flag, text) {
  state.busy = flag;
  const mask = $('#busyMask');
  if (mask) mask.hidden = !flag;
  if (text) $('#busyText').textContent = text;
  updateButtons();
}

/* ---------------- 标签页 ---------------- */
function switchTab(name) {
  $$('.tab').forEach(b => b.classList.toggle('active', b.dataset.tab === name));
  $$('.view').forEach(v => v.classList.toggle('active', v.id === 'view-' + name));
}

/* ---------------- 页面与图片 ---------------- */
function activePage() {
  return state.pages[state.active] || null;
}

async function addFiles(files) {
  const list = Array.from(files || []).filter(f => f.type && f.type.startsWith('image/'));
  if (!list.length) {
    toast('没有可用的图片文件', 'warn');
    return;
  }
  setBusy(true, '正在读取图片…');
  try {
    for (const file of list) {
      const dataUrl = await fileToDataUrl(file);
      const img = await loadImage(dataUrl);
      state.pages.push({
        id: ++pageSeq,
        name: file.name || `图片 ${pageSeq}`,
        dataUrl,
        w: img.naturalWidth,
        h: img.naturalHeight,
        analysis: null,
        result: null,
        showResult: false
      });
    }
    state.active = state.pages.length - 1;
    state.selectedQ = -1;
    renderAll();
    log(`已载入 ${list.length} 页图片`);
  } catch (e) {
    log('载入图片失败：' + e.message, 'err');
    toast(e.message, 'error');
  } finally {
    setBusy(false);
  }
}

function renderPages() {
  const strip = $('#pageStrip');
  strip.innerHTML = '';
  state.pages.forEach((p, i) => {
    const btn = document.createElement('button');
    btn.className = 'thumb' + (i === state.active ? ' active' : '') + (p.result ? ' done' : '');
    btn.title = p.name;
    btn.innerHTML = `<img src="${p.dataUrl}" alt=""><span class="no">${i + 1}</span><span class="flag"></span>`;
    btn.addEventListener('click', () => {
      if (state.busy) return;
      state.active = i;
      state.selectedQ = -1;
      renderAll();
    });
    strip.appendChild(btn);
  });
}

function renderStage() {
  const p = activePage();
  $('#dropzone').hidden = !!p;
  $('#canvasInner').hidden = !p;
  if (!p) {
    $('#stageMeta').textContent = '等待上传图片';
    $('#boxLayer').innerHTML = '';
    if ($('#btnScanView')) $('#btnScanView').hidden = true;
    if ($('#btnDocView')) $('#btnDocView').hidden = true;
    return;
  }
  const showingGrid = !!p.showGrid && !!p.gridUrl;
  const showingResult = !showingGrid && p.showResult && p.result;
  const showingCalib = !showingGrid && !showingResult && !!p.showCalib && !!p.calib;
  const showingScan = !showingGrid && !showingResult && !showingCalib && !!p.showScan && !!p.agentScan;
  $('#pageImg').src = showingGrid
    ? p.gridUrl
    : (showingResult ? p.result
      : (showingCalib ? p.calib : (showingScan ? p.agentScan : p.dataUrl)));
  const editing = !!(p.agentLayout && showingResult);
  $('#boxLayer').style.display = (showingGrid || showingCalib
    || (showingResult && !editing)) ? 'none' : 'block';
  const view = showingGrid ? 'grid'
    : (showingResult ? 'result' : (showingCalib ? 'calib' : 'origin'));
  $$('#viewSeg button').forEach(b => b.classList.toggle('active', b.dataset.view === view));
  const kind = showingScan ? ' · 扫描件' : (showingResult ? ' · 作答结果'
    : (showingCalib ? ' · 校准图' : ''));
  $('#stageMeta').textContent = `第 ${state.active + 1} / ${state.pages.length} 页 · ${p.w}×${p.h}${kind}${p.analysis ? ' · 已识图' : ''}`;
  if ($('#btnScanView')) $('#btnScanView').hidden = !p.agentScan;
  if ($('#btnDocView')) $('#btnDocView').hidden = !p.agentDoc;
  renderBoxes();
}

/* ---------------- 画布落笔框 ---------------- */
function renderBoxes() {
  const layer = $('#boxLayer');
  layer.innerHTML = '';
  boxEls = [];
  const p = activePage();
  if (!p || !p.analysis || !Array.isArray(p.analysis.questions)) return;

  p.analysis.questions.forEach((q, i) => {
    const on = q._checked !== false;
    const el = document.createElement('div');
    el.className = 'q-box' + (state.selectedQ === i ? ' selected' : '') + (on ? '' : ' off');
    el.style.left = q.area.x * 100 + '%';
    el.style.top = q.area.y * 100 + '%';
    el.style.width = q.area.w * 100 + '%';
    el.style.height = q.area.h * 100 + '%';
    el.innerHTML = `<span class="qi">第${escapeHtml(q.index)}题</span><span class="resize"></span>`;

    el.addEventListener('pointerdown', ev => {
      if (state.busy) return;
      if (ev.target.classList.contains('resize')) {
        startBoxDrag(ev, q, i, 'resize');
      } else {
        startBoxDrag(ev, q, i, 'move');
      }
    });

    layer.appendChild(el);
    boxEls.push({ el, q });
  });
}

function applyBoxPositions() {
  boxEls.forEach(({ el, q }) => {
    el.style.left = q.area.x * 100 + '%';
    el.style.top = q.area.y * 100 + '%';
    el.style.width = q.area.w * 100 + '%';
    el.style.height = q.area.h * 100 + '%';
  });
}

function startBoxDrag(e, q, index, mode) {
  e.preventDefault();
  e.stopPropagation();
  const layer = $('#boxLayer');
  const rect = layer.getBoundingClientRect();
  if (!rect.width || !rect.height) return;

  const startX = e.clientX;
  const startY = e.clientY;
  const orig = { ...q.area };
  let moved = false;

  const onMove = ev => {
    const dx = (ev.clientX - startX) / rect.width;
    const dy = (ev.clientY - startY) / rect.height;
    if (Math.abs(ev.clientX - startX) + Math.abs(ev.clientY - startY) > 3) moved = true;
    if (mode === 'resize') {
      q.area.w = clamp(orig.w + dx, 0.02, 1 - orig.x);
      q.area.h = clamp(orig.h + dy, 0.012, 1 - orig.y);
    } else {
      q.area.x = clamp(orig.x + dx, 0, 1 - q.area.w);
      q.area.y = clamp(orig.y + dy, 0, 1 - q.area.h);
    }
    applyBoxPositions();
  };

  const onUp = () => {
    document.removeEventListener('pointermove', onMove);
    document.removeEventListener('pointerup', onUp);
    if (!moved) selectQuestion(index);
  };

  document.addEventListener('pointermove', onMove);
  document.addEventListener('pointerup', onUp);
}

function selectQuestion(index) {
  state.selectedQ = index;
  $$('.q-item').forEach((row, i) => row.classList.toggle('active', i === index));
  boxEls.forEach(({ el, q }) => {
    const qi = state.pages[state.active]?.analysis?.questions?.indexOf(q);
    el.classList.toggle('selected', qi === index);
  });
  const row = $$('.q-item')[index];
  if (row) row.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
}

/* ---------------- 题目列表 ---------------- */
function renderQuestions() {
  const list = $('#qList');
  const count = $('#qCount');
  if (!list || !count) return;
  const p = activePage();
  const qs = p?.analysis?.questions || [];
  if (!qs.length) {
    count.textContent = '0 题';
    list.innerHTML = '<p class="rail-empty">上传试卷并识图后，这里会出现题目。</p>';
    return;
  }

  const checked = qs.filter(q => q._checked !== false).length;
  count.textContent = `${qs.length} 题 / 写 ${checked} 题`;
  list.innerHTML = '';

  qs.forEach((q, i) => {
    const row = document.createElement('label');
    row.className = 'q-item' + (state.selectedQ === i ? ' active' : '');
    row.dataset.i = String(i);
    const diff = clamp(Math.round(Number(q.difficulty) || 3), 1, 5);
    row.innerHTML = `
      <input type="checkbox">
      <span class="q-no">${escapeHtml(q.index)}</span>
      <span class="q-type">${escapeHtml(q.type || '题目')}</span>
      <span class="q-diff">${'●'.repeat(diff)}</span>`;

    const cb = row.querySelector('input');
    cb.checked = q._checked !== false;
    cb.addEventListener('change', () => {
      q._checked = cb.checked;
      count.textContent = `${qs.length} 题 / 写 ${qs.filter(x => x._checked !== false).length} 题`;
      renderBoxes();
    });
    row.addEventListener('click', e => {
      if (e.target === cb) return;
      selectQuestion(i);
    });

    list.appendChild(row);
  });
}

/* ---------------- 分析结果整理 ---------------- */
function num(v, fallback) {
  const n = Number(v);
  return Number.isFinite(n) ? n : fallback;
}

function normalizeArea(raw) {
  let x, y, w, h;
  if (Array.isArray(raw)) {
    x = num(raw[0], 0.1); y = num(raw[1], 0.3); w = num(raw[2], 0.6); h = num(raw[3], 0.08);
  } else {
    const a = raw && typeof raw === 'object' ? raw : {};
    x = num(a.x, 0.1); y = num(a.y, 0.3); w = num(a.w, 0.6); h = num(a.h, 0.08);
    if (a.width !== undefined) w = num(a.width, w);
    if (a.height !== undefined) h = num(a.height, h);
  }
  // 模型偶尔会返回百分数（如 10 / 35 / 70 / 6），这里统一换算成 0~1
  const biggest = Math.max(Math.abs(x), Math.abs(y), Math.abs(w), Math.abs(h));
  if (biggest > 1.5 && biggest <= 100) {
    x /= 100; y /= 100; w /= 100; h /= 100;
  }
  w = clamp(w, 0.03, 1); h = clamp(h, 0.015, 1);
  x = clamp(x, 0, 1 - w);
  y = clamp(y, 0, 1 - h);
  return { x, y, w, h };
}

function normalizeOptions(raw) {
  if (!raw) return [];
  let list = [];
  if (Array.isArray(raw)) {
    list = raw.map(o => {
      if (!o) return null;
      if (Array.isArray(o)) return { letter: '', area: normalizeArea(o) };
      const letter = String(o.letter || o.key || o.name || '').toUpperCase().replace(/[^A-H]/g, '').slice(0, 1);
      return { letter, area: normalizeArea(o.area || o.box || o) };
    });
  } else if (typeof raw === 'object') {
    list = Object.keys(raw).map(k => ({
      letter: String(k).toUpperCase().replace(/[^A-H]/g, '').slice(0, 1),
      area: normalizeArea(raw[k])
    }));
  }
  return list.filter(o => o && o.letter && o.area);
}

function normalizeAnalysis(data, page) {
  if (!data || typeof data !== 'object') throw new Error('识图返回内容不是有效对象');
  if (Array.isArray(data)) data = { questions: data };
  let qs = data.questions || data.items || data.questions_list;
  if (!Array.isArray(qs) || qs.length === 0) {
    const fallback = data.answer || data.content || data.text;
    if (!fallback) throw new Error('未从图片中识别到可作答的题目');
    qs = [{
      index: 1,
      type: data.page_type || '文档',
      stem: data.title || '整页内容',
      answer: String(fallback),
      area: { x: 0.08, y: 0.18, w: 0.84, h: 0.6 }
    }];
  }
  data.questions = qs.map((q, i) => {
    if (!q || typeof q !== 'object') q = { stem: String(q || '') };
    return {
      index: q.index ?? q.no ?? i + 1,
      type: q.type || '题目',
      stem: String(q.stem || q.question || ''),
      answer: String(q.answer ?? ''),
      answered: !!q.answered,
      existing_answer: String(q.existing_answer || ''),
      area: normalizeArea(q.area || q.answer_area || q.box),
      answer_y: Number.isFinite(Number(q.answer_y)) ? Number(q.answer_y) : null,
      answer_x: Number.isFinite(Number(q.answer_x)) ? Number(q.answer_x) : null,
      difficulty: clamp(num(q.difficulty, 3), 1, 5),
      _checked: true
    };
  });
  data.page_type = data.page_type || '文档';
  data.title = data.title || page.name;
  return data;
}

function extractJson(text) {
  if (!text) throw new Error('模型没有返回内容');
  let t = String(text).trim();
  t = t.replace(/^```(?:json|JSON)?\s*/i, '').replace(/```\s*$/i, '').trim();
  const start = t.search(/[[{]/);
  if (start < 0) throw new Error('模型返回中找不到 JSON：' + t.slice(0, 160));
  const open = t[start];
  const close = open === '{' ? '}' : ']';
  const end = t.lastIndexOf(close);
  if (end <= start) throw new Error('模型返回的 JSON 不完整：' + t.slice(0, 160));
  const body = t.slice(start, end + 1);
  try {
    return JSON.parse(body);
  } catch (e) {
    throw new Error('JSON 解析失败：' + e.message + '，原文开头：' + body.slice(0, 160));
  }
}

/* ---------------- 提示词 ---------------- */
function buildAnalyzePrompt() {
  return [
    '你是一名严谨的试卷识别与全科解题引擎。请仔细阅读这张图片，完成三件事：',
    '1. 判断页面类型（试卷 / 答题卡 / 练习册 / 文档 / 其他）；',
    '2. 逐题提取题干、题型、选项以及学生已有的作答；',
    '3. 为每一道题给出正确、完整、可以直接誊写到卷面上的答案。',
    '',
    '严格输出下面结构的 JSON，不要 markdown 代码块，不要任何额外解释：',
    '{',
    '  "page_type": "试卷",',
    '  "title": "页面标题",',
    '  "subject": "学科",',
    '  "language": "中文",',
    '  "questions": [',
    '    {',
    '      "index": 1,',
    '      "type": "选择题 | 填空题 | 解答题 | 判断题 | 其他",',
    '      "stem": "题干文字（可精简）",',
    '      "answer": "最终答案；选择题只写单个选项字母（A/B/C/D），不要写选项内容；填空题只写最终结果；解答题给出完整解题过程",',
    '      "answered": false,',
    '      "existing_answer": "学生原有作答，没有则为空字符串",',
    '      "difficulty": 3,',
    '      "answer_y": 0.123,',
    '      "answer_x": 0.780,',
    '      "area": { "x": 0.10, "y": 0.35, "w": 0.70, "h": 0.06 }',
    '    }',
    '  ],',
    '  "notes": "版式说明"',
    '}',
    '',
    '要求：',
    '- area 表示“答案应该书写的位置”，使用相对整张图片的归一化坐标（0~1，左上角为原点）；',
    '- 选择题 area 指向题号右侧的空白处；填空题指向横线位置；解答题覆盖该题预留的答题空白区域；',
    '- 坐标必须是 0~1 之间的小数，禁止返回像素值或百分数；',
    '- difficulty 为 1~5 的整数，1 最简单、5 最难，后面的题通常更难；',
    '- answer_y 是“答案要写的那一行”的垂直中心（选择题取题干行尾括号的中心，填空题取横线中心），0~1 小数，必须尽量准确；',
    '- answer_x 是该空档（括号/横线）的水平中心，0~1 小数；选择题可省略，填空题尽量给；',
    '- 选择题的 answer 必须是单个大写字母，例如 "B"，禁止写公式、选项内容或解释；',
    '- 填空题的 answer 只写最终结果，不要写“答案：”或过程；',
    '- 观察卷面原有笔迹的墨色、字号、倾斜与潦草程度，写入 notes，供后续模拟；',
    '- 坐标要尽量贴合真实卷面，宁可小一点也不要覆盖题干文字；',
    '- 若某题学生已经作答，answered 置为 true，并在 existing_answer 中记录原笔迹，answer 仍给出标准答案；',
    '- 若页面不是试卷，把整页内容按自然段落拆成若干条，type 记为“文档”，answer 写该段要点或应写下的内容；',
    '- 题目数量多时 stem 可以精简，但 answer 必须完整、可直接写在卷面上。',
    '',
    buildRequireBlock()
  ].join('\n');
}

/* ---------------- 作答要求 ---------------- */
function checkedIndexes(page = activePage()) {
  const qs = page?.analysis?.questions || [];
  return qs.filter(q => q._checked !== false).map(q => q.index);
}

/* 从自然语言里解析“第几题” */
function parseQuestionSet(text) {
  const set = new Set();
  const t = String(text || '').replace(/[～~至到—－]/g, '-');
  let m;
  const re = /(?:第\s*)?(\d+)\s*(?:-\s*(\d+))?\s*题/g;
  while ((m = re.exec(t))) {
    const a = Number(m[1]);
    const b = m[2] ? Number(m[2]) : a;
    for (let i = Math.min(a, b); i <= Math.max(a, b) && i < 200; i++) set.add(i);
  }
  if (!set.size) {
    const re2 = /(?:只做|只答|做|答|写)\s*(\d+)\s*-\s*(\d+)/g;
    while ((m = re2.exec(t))) {
      const a = Number(m[1]);
      const b = Number(m[2]);
      for (let i = Math.min(a, b); i <= Math.max(a, b) && i < 200; i++) set.add(i);
    }
  }
  return set;
}

/* 把用户要求转成给模型的硬性提示词 */
function buildRequireBlock() {
  const req = String(state.requirement || '').trim();
  if (!req) return '';
  const fromText = [...parseQuestionSet(req)].sort((a, b) => a - b);
  const idxs = fromText.length ? fromText : checkedIndexes();
  const lines = [
    '【考生作答要求（最高优先级，必须严格遵守）】',
    req,
    idxs.length ? `本次只作答第 ${idxs.join('、')} 题；其余题目的 answer 必须返回空字符串。` : '',
    '若要求“只写选项 / 只写字母”，选择题 answer 只返回选项字母；若要求“完整过程”，解答题必须写出完整步骤。',
    '题干、选项与落笔位置仍然要完整识别，方便后续在卷面上落笔。'
  ];
  return lines.filter(Boolean).join('\n');
}

function oneLine(text) {
  return String(text ?? '').replace(/\s*\n+\s*/g, '；').replace(/\s+/g, ' ').trim();
}

/* 把模型常见的 LaTeX 写法转成可以直接画在卷面上的文本 */
function toDisplayText(text) {
  let t = String(text ?? '');
  t = t.replace(/\$\$?/g, '');
  t = t.replace(/\\d?frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}/g, '($1)/($2)');
  t = t.replace(/\\sqrt\s*\[([^\]]*)\]\s*\{([^{}]*)\}/g, '$2^(1/$1)');
  t = t.replace(/\\sqrt\s*\{([^{}]*)\}/g, '√($1)');
  t = t.replace(/\^\{?\\circ\}?/g, '°').replace(/\^\{?\\degree\}?/g, '°');
  t = t.replace(/\\times/g, '×').replace(/\\div/g, '÷').replace(/\\pm/g, '±')
    .replace(/\\leq/g, '≤').replace(/\\geq/g, '≥').replace(/\\neq/g, '≠')
    .replace(/\\cdot/g, '·').replace(/\\pi/g, 'π').replace(/\\angle/g, '∠')
    .replace(/\\triangle/g, '△').replace(/\\circ/g, '°').replace(/\\degree/g, '°')
    .replace(/\\alpha/g, 'α').replace(/\\beta/g, 'β').replace(/\\theta/g, 'θ')
    .replace(/\\because/g, '因为').replace(/\\therefore/g, '所以')
    .replace(/\\left|\\right/g, '');
  t = t.replace(/\\[a-zA-Z]+/g, '');
  t = t.replace(/\^\{?2\}?/g, '²').replace(/\^\{?3\}?/g, '³').replace(/\^\{([^{}]+)\}/g, '^$1');
  t = t.replace(/_\{([^{}]+)\}/g, '$1').replace(/_([a-zA-Z0-9])/g, '$1');
  t = t.replace(/[{}]/g, '');
  return t.trim();
}

function buildHumanStylePrompt(total) {
  const acc = state.human.accuracy;
  const cor = state.human.correction;
  const wrongCount = Math.max(0, Math.round(total * (100 - acc) / 100));
  const fixCount = Math.max(0, Math.round(total * cor / 100));
  const tone = INK_TONES[state.inkTone]?.label || '标准黑';

  const rules = [
    '【真人笔迹与涂改要求】',
    '你现在不是打印机，而是一名坐在考场里、用同一支笔答题的学生。请严格模拟真实手写痕迹：',
    '1. 字形：每个字的大小、倾斜、重心都略有差异，单字允许 ±3° 的随机倾斜，不要像电脑字体那样整齐划一；',
    '2. 基线：整行文字有轻微上下起伏和整体倾斜，行距不完全相等，越写到后面越容易压线或上飘；',
    '3. 笔画：起笔重、收笔轻，横竖笔画粗细有变化，允许少量连笔、飞白、抖笔和顿笔；',
    '4. 墨色：同一支笔写出来也有深浅变化，个别字积墨更重，个别字偏淡，不要全篇一个颜色；',
    '5. 间距：字距不均，标点前后空白大小随机，长答案可能越写越挤，也可能越写越开；',
    '6. 布局：答案不必严丝合缝地居中，允许整体偏左、偏上、略微压线，甚至碰到括号边缘；',
    '7. 涂改：允许先写错再划掉重写。涂改线要随意、快速、不平行，通常是 2~4 道斜线或来回划的短线；',
    '8. 补写：改对的答案可以挤在旁边、上方或行末，字号可能比正文略小，不要擦得干干净净；',
    '9. 小瑕疵：允许漏笔、连笔导致字形略糊、标点遗漏或重复，但最终答案必须仍然可以辨认；',
    '10. 真实感优先：不要出现整段文字字体完全一致、间距完全相等、边缘过于锐利的情况；',
    `11. 正确率约 ${acc}%：本次约 ${total} 题，预计 ${wrongCount} 题会出现错误答案（选择题写成其他选项），这是刻意保留的人类失误；`,
    `12. 涂改率约 ${cor}%：预计 ${fixCount} 题会先写一个错误答案，划掉后在旁边补写正确答案；`,
    '13. 涂改与正文使用同一支笔、同一种墨色，划掉的字仍然隐约可见，禁止使用涂白、橡皮擦或打印体删除线；',
    `14. 书写工整度约 ${state.human.neatness}%：数值越低，字迹越潦草、大小越不统一、连笔和涂画越多；数值越高越接近工整楷书；`,
    `15. 只用黑色笔：本次墨色为${tone}（黑色系中性笔或圆珠笔），绝不允许蓝色、蓝黑、红色或彩色笔迹；`,
    '16. 笔压偏重：笔画要粗、墨色要实，起笔有明显顿点，转折处略重，收笔有轻微回锋；个别字有积墨和洇纸感；',
    `17. 字号偏大：默认按 ${state.human.size}% 的字号书写，字要占满答题空间的大部分，但不能溢出答题区、压住题干或互相重叠；`,
    '18. 墨迹要有真实落笔的颗粒感和轻微深浅变化，不能像印刷体，边缘不能过于锐利均匀；',
    '19. 不要添加任何“AI生成”“示例”“标准答案”等标注、水印或边框。'
  ];

  if (state.fontProfile?.note) {
    rules.push(`22. 参考学生往期字迹特征：${state.fontProfile.note}；本次书写要向这个风格靠拢。`);
  }
  return rules.join('\n');
}

function buildRedrawPrompt(page, items) {
  const styleWord = {
    kai: '工整的黑色中性笔楷书，像真实学生手写作答',
    hand: '自然的黑色手写体，带轻微连笔与顿笔',
    print: '清晰规范的黑色等线印刷体，像标准答案'
  }[state.style];

  const lines = items.map(q => {
    const a = q.area;
    return `第${q.index}题（难度 ${q.difficulty || 3}，横向 ${pct(a.x)}–${pct(a.x + a.w)}，纵向 ${pct(a.y)}–${pct(a.y + a.h)}）：${oneLine(toDisplayText(q.answer))}`;
  }).join('\n');

  const parts = [
    '你是试卷作答机器人。请在这张图片上直接书写答案，并输出修改后的同一张图片。',
    '硬性要求：',
    '1. 原图的版式、纸张纹理、标题、题号、题干、印刷字体以及已有笔迹必须保持完全不变，只新增答案；',
    '2. 不要重排整页，不要重画题目，不要改变图片尺寸、比例与透视；',
    '3. 答案写在对应题目预留的空白作答区域内，绝对不能遮挡题干文字；',
    `4. 字迹风格：${styleWord}；`,
    '5. 不要添加水印、边框、题注或任何解释性文字。',
    '',
    buildHumanStylePrompt(items.length),
    ''
  ];

  if (page?.analysis?.notes) {
    parts.push('卷面笔迹观察（尽量照着这个风格写）：' + page.analysis.notes);
    parts.push('');
  }

  parts.push('需要书写的答案（位置为相对整页的百分比）：');
  parts.push(lines);

  const requireBlock = buildRequireBlock();
  if (requireBlock) {
    parts.push('');
    parts.push(requireBlock);
  }
  return parts.join('\n');
}

/* ---------------- 无 Key：直接给模型提示词出图 ---------------- */
function copyText(text) {
  if (navigator.clipboard?.writeText) {
    return navigator.clipboard.writeText(text);
  }
  return new Promise((resolve, reject) => {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    try {
      document.execCommand('copy') ? resolve() : reject(new Error('浏览器不允许复制'));
    } catch (e) {
      reject(e);
    } finally {
      ta.remove();
    }
  });
}

function buildDirectImagePrompt() {
  const tone = INK_TONES[state.inkTone]?.label || '标准黑';
  const req = String(state.requirement || '').trim();
  return [
    '我会按顺序给你两张图片：',
    '【图片1】我的往期作答 / 笔记，代表我真实的笔迹风格；',
    '【图片2】需要作答的试卷。',
    '',
    '任务：只修改【图片2】，在每道题的答题位置写出正确答案；字迹必须与【图片1】100% 接近。',
    '',
    '【最高优先级：笔迹必须和图片1一模一样】',
    '1. 逐字对照图片1，复刻字形结构、笔画顺序、起笔与收笔、连笔和断笔习惯；',
    '2. 复刻同样的字重与压感：黑色中性笔，笔画偏重，转折处有顿笔，收笔带锋；',
    '3. 复刻单字大小比例、字间距、行距、整体倾斜角度和基线起伏，允许大小不一、行距不齐；',
    '4. 复刻图片1的涂改方式：写错就划掉、涂黑或在旁边补写，涂改线随意、不平行；',
    '5. 保留轻微歪斜、飞白、积墨和笔尖抖动，不要美化，不要写得比图片1工整；',
    '6. 禁止任何标准字体、印刷体、电脑手写体；禁止蓝色、红色或彩色笔迹。',
    '',
    '【作答要求】',
    '- 先读题、解题，再把答案写在题目预留的空白处，不遮挡题干和选项；',
    `- 正确率约 ${state.human.accuracy}%：允许少量题目故意写错；`,
    `- 涂改率约 ${state.human.correction}%；`,
    `- 书写工整度约 ${state.human.neatness}%，字号约 ${state.human.size}%；`,
    `- 统一使用${tone}的黑色中性笔。`,
    req ? `- 【考生作答要求（最高优先级）】${req}` : '',
    '',
    '【输出】',
    '只返回修改后的【图片2】。图片2的纸张、印刷文字、题号、题干、选项、版式、尺寸和透视必须保持原样，只新增手写答案；',
    '不要返回图片1，不要添加边框、水印、说明文字，不要输出 JSON 或任何解释。'
  ].filter(Boolean).join('\n');
}

async function handleCopyPrompt() {
  if (!activePage()) return toast('请先上传图片', 'warn');
  try {
    await copyText(buildDirectImagePrompt());
    toast('整页作答提示词已复制：和试卷图片一起发给改图模型', 'ok');
    log('已复制整页作答提示词');
  } catch (e) {
    toast('复制失败：' + e.message, 'error');
  }
}

async function handleImportResult(file) {
  const page = activePage();
  if (!page) return toast('请先上传试卷图片', 'warn');
  if (!file || !file.type.startsWith('image/')) return toast('请选择图片文件', 'warn');
  setBusy(true, '正在导入作答图…');
  try {
    page.result = await fileToDataUrl(file);
    page.showResult = true;
    renderAll();
    log(`已导入作答图：${file.name || '图片'}`, 'ok');
    toast('作答图已导入，可直接下载', 'ok');
  } catch (e) {
    toast(e.message, 'error');
  } finally {
    setBusy(false);
  }
}

/* ---------------- 服务商适配 ---------------- */
function getSvc() {
  const key = (state.vision.key || '').trim();
  if (!key) {
    switchTab('settings');
    toast('请先在设置里填写识图模型的 API Key', 'warn');
    throw new Error('缺少识图 API Key');
  }
  let base = (state.vision.base || DEFAULTS.vision.base).trim().replace(/\/+$/, '');
  base = base.replace(/\/chat\/completions$/, '').replace(/\/+$/, '');
  if (!/\/v\d+(\.\d+)?$/.test(base)) base += '/v1';
  return { key, base, model: (state.vision.model || DEFAULTS.vision.model).trim() };
}

async function fetchWithTimeout(url, options = {}, ms = 240000) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), ms);
  try {
    return await fetch(url, { ...options, signal: ctrl.signal });
  } catch (e) {
    if (e.name === 'AbortError') throw new Error('请求超时（超过 4 分钟），请检查网络后重试');
    throw e;
  } finally {
    clearTimeout(timer);
  }
}

async function postJson(url, body, headers = {}) {
  let res;
  try {
    res = await fetchWithTimeout(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...headers },
      body: JSON.stringify(body)
    });
  } catch (e) {
    throw new Error('网络请求失败：' + e.message + '（若为浏览器跨域拦截，可使用本地服务器方式打开）');
  }
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : {}; } catch { data = null; }
  if (!res.ok) {
    const msg = data?.error?.message || data?.message || data?.msg || text.slice(0, 300) || res.statusText;
    throw new Error(`接口返回 ${res.status}：${msg}`);
  }
  return data || {};
}

function geminiText(data) {
  const cand = data?.candidates?.[0];
  const parts = cand?.content?.parts || [];
  const text = parts.map(p => p.text).filter(Boolean).join('\n').trim();
  if (!text) {
    const block = data?.promptFeedback?.blockReason;
    if (block) throw new Error('内容被安全策略拦截：' + block);
    throw new Error('模型没有返回文本' + (cand?.finishReason ? `（${cand.finishReason}）` : ''));
  }
  return text;
}

function geminiImage(data) {
  const parts = data?.candidates?.[0]?.content?.parts || [];
  for (const p of parts) {
    const inline = p.inlineData || p.inline_data;
    if (inline?.data) {
      const mime = inline.mimeType || inline.mime_type || 'image/png';
      return `data:${mime};base64,${inline.data}`;
    }
  }
  const text = parts.map(p => p.text).filter(Boolean).join('\n').trim();
  throw new Error('模型没有返回图片' + (text ? '，返回文字：' + text.slice(0, 200) : ''));
}

async function prepareForApi(dataUrl, maxSide = 1600) {
  const img = await loadImage(dataUrl);
  const w = img.naturalWidth;
  const h = img.naturalHeight;
  const mime = (dataUrl.match(/^data:([^;]+)/) || [])[1] || 'image/png';
  const scale = Math.min(1, maxSide / Math.max(w, h));
  // 一律过一遍 canvas：把浏览器已经转正的方向真正写进像素，
  // 否则原图可能带着旋转元数据，后端 OpenCV 读到的是横着的。
  const canvas = document.createElement('canvas');
  canvas.width = Math.max(1, Math.round(w * scale));
  canvas.height = Math.max(1, Math.round(h * scale));
  const ctx = canvas.getContext('2d');
  ctx.fillStyle = '#ffffff';
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
  const out = canvas.toDataURL('image/jpeg', 0.92);
  return { dataUrl: out, base64: out.split(',')[1], mime: 'image/jpeg', width: canvas.width, height: canvas.height };
}

/* 视觉识图 */
async function apiVision(image, prompt) {
  const s = getSvc();
  const url = `${s.base}/chat/completions`;
  const makeBody = jsonMode => ({
    model: s.model,
    temperature: 0.2,
    max_tokens: 8000,
    messages: [
      { role: 'system', content: '你是严谨的试卷识别与解题引擎，只输出合法 JSON，不要输出 markdown 代码块。' },
      {
        role: 'user',
        content: [
          { type: 'text', text: prompt },
          { type: 'image_url', image_url: { url: image.dataUrl } }
        ]
      }
    ],
    ...(jsonMode ? { response_format: { type: 'json_object' } } : {})
  });

  let data;
  try {
    data = await postJson(url, makeBody(true), { Authorization: `Bearer ${s.key}` });
  } catch (e) {
    if (!/response_format|json_object|json mode/i.test(e.message)) throw e;
    data = await postJson(url, makeBody(false), { Authorization: `Bearer ${s.key}` });
  }
  const content = data?.choices?.[0]?.message?.content;
  if (typeof content === 'string') return content;
  if (Array.isArray(content)) return content.map(p => p.text || '').join('');
  throw new Error('模型没有返回可解析的内容');
}

/* 基于图片生成 */
async function apiImageEdit(image, prompt) {
  const s = getSvc();
  if (s.provider === 'gemini') {
    const url = `${s.base}/v1beta/models/${encodeURIComponent(state.imageModel)}:generateContent?key=${encodeURIComponent(s.key)}`;
    const body = {
      contents: [{
        role: 'user',
        parts: [
          { text: prompt },
          { inlineData: { mimeType: image.mime, data: image.base64 } }
        ]
      }],
      generationConfig: { responseModalities: ['TEXT', 'IMAGE'], temperature: 0.3 }
    };
    const data = await postJson(url, body);
    return geminiImage(data);
  }

  const fd = new FormData();
  fd.append('model', state.imageModel);
  fd.append('image', dataUrlToBlob(image.dataUrl), 'page.png');
  fd.append('prompt', prompt);
  fd.append('n', '1');
  const size = image.height > image.width * 1.12
    ? '1024x1536'
    : (image.width > image.height * 1.12 ? '1536x1024' : '1024x1024');
  fd.append('size', size);

  const send = async form => {
    let res;
    try {
      res = await fetchWithTimeout(`${s.base}/images/edits`, {
        method: 'POST',
        headers: { Authorization: `Bearer ${s.key}` },
        body: form
      }, 300000);
    } catch (e) {
      throw new Error('网络请求失败：' + e.message);
    }
    const text = await res.text();
    let data = null;
    try { data = text ? JSON.parse(text) : {}; } catch { data = null; }
    if (!res.ok) {
      const msg = data?.error?.message || text.slice(0, 300) || res.statusText;
      throw new Error(`图像编辑接口返回 ${res.status}：${msg}`);
    }
    return data;
  };

  let data;
  try {
    data = await send(fd);
  } catch (e) {
    if (!/size|尺寸/i.test(e.message)) throw e;
    const retry = new FormData();
    retry.append('model', state.imageModel);
    retry.append('image', dataUrlToBlob(image.dataUrl), 'page.png');
    retry.append('prompt', prompt);
    retry.append('n', '1');
    data = await send(retry);
  }

  const item = data?.data?.[0];
  if (item?.b64_json) return `data:image/png;base64,${item.b64_json}`;
  if (item?.url) return item.url;
  throw new Error('接口没有返回图片数据');
}

/* ---------------- 识图与分析 ---------------- */
async function analyzePage(page, keepBusy = false) {
  if (!keepBusy) setBusy(true, '正在识图并解题…');
  try {
    log(`识图：${page.name}`);
    const image = await prepareForApi(page.dataUrl, 1500);
    const text = await apiVision(image, buildAnalyzePrompt());
    const raw = extractJson(text);
    page.analysis = normalizeAnalysis(raw, page);
    page.result = null;
    page.showResult = false;
    state.selectedQ = -1;
    log(`识图完成：${page.analysis.page_type} · 共 ${page.analysis.questions.length} 题`, 'ok');
    return page.analysis;
  } catch (e) {
    log('识图失败：' + e.message, 'err');
    throw e;
  } finally {
    if (!keepBusy) setBusy(false);
  }
}

async function handleAnalyze() {
  const page = activePage();
  if (!page) return toast('请先上传图片', 'warn');
  if (!state.vision.key.trim()) {
    switchTab('settings');
    return toast('请先在设置里填写识图模型的 API Key', 'warn');
  }
  if (state.output === 'agent') {
    await runAgentScan(page);
    return;
  }
  try {
    await analyzePage(page);
    renderAll();
    toast(`识别到 ${page.analysis.questions.length} 道题`, 'ok');
  } catch (e) {
    toast(e.message, 'error');
    renderAll();
  }
}

/* ---------------- 落笔成图 ---------------- */
function targetQuestions(page) {
  const qs = page?.analysis?.questions || [];
  const set = parseQuestionSet(state.requirement);
  return qs.filter(q => q._checked !== false
    && (q.answer || '').trim()
    && (!set.size || set.has(Number(q.index))));
}

function wrapText(ctx, text, maxWidth, fontPx) {
  const lines = [];
  const paragraphs = String(text).split('\n');
  for (const para of paragraphs) {
    if (!para.trim()) { lines.push(''); continue; }
    let line = '';
    for (const ch of para) {
      if (ch === '\r') continue;
      const test = line + ch;
      if (ctx.measureText(test).width > maxWidth && line) {
        lines.push(line);
        line = ch;
      } else {
        line = test;
      }
    }
    if (line) lines.push(line);
  }
  return lines.length ? lines : [''];
}

function randRange(min, max) {
  return min + Math.random() * (max - min);
}

function fontFor(style) {
  if (style === 'print') return FONTS.print;
  const n = state.human.neatness;
  if (n < 35) return '"Segoe Script","Ink Free","方正舒体","FZShuTi","华文行楷","KaiTi",cursive';
  if (n < 70) return '"Segoe Script","Ink Free","华文行楷","STXingkai","KaiTi",cursive';
  return '"Segoe Script","Ink Free","华文楷体","KaiTi",serif';
}

function humanFactor() {
  const base = state.style === 'print' ? 0.2 : 1;
  return base * (0.45 + (100 - state.human.neatness) / 100 * 1.35);
}

/* 逐字手写：每个字的字号、倾角、基线、墨色都有细微差别 */
function drawHandLine(ctx, line, x, y, fs, family, color) {
  const k = humanFactor();
  let cx = x + randRange(-0.5, 0.5) * k;
  const tilt = randRange(-0.012, 0.012) * k;

  for (const ch of line) {
    ctx.save();
    ctx.font = `${fs * (1 + randRange(-0.05, 0.05) * k)}px ${family}`;
    ctx.fillStyle = color;
    ctx.strokeStyle = color;
    ctx.globalAlpha = 1 - randRange(0, 0.1) * k;
    const w = ctx.measureText(ch).width;
    ctx.translate(cx, y + randRange(-0.9, 0.9) * k + (cx - x) * tilt);
    ctx.rotate(randRange(-0.05, 0.05) * k);
    ctx.textBaseline = 'top';
    ctx.textAlign = 'left';
    ctx.fillText(ch, 0, 0);
    // 重笔：沿字形再描一遍，制造压感和墨量
    ctx.globalAlpha = 0.5;
    ctx.lineWidth = Math.max(0.35, fs * 0.022);
    ctx.strokeText(ch, 0, 0);
    ctx.restore();
    cx += w * (1 + randRange(-0.03, 0.05) * k) + randRange(0.15, 0.9) * k;
  }
}

function humanPaint(ctx, text, box, style) {
  if (!text || box.w <= 1 || box.h <= 4) return;
  const family = fontFor(style);
  const color = inkColor();
  const scale = (state.human.size / 100) * (state.reqScale || 1);
  let fs = Math.max(10, Math.min(box.h * 0.62 * scale, box.w * 0.085 * scale, 46 * scale));
  let lines = [];

  for (;;) {
    ctx.font = `${fs}px ${family}`;
    lines = wrapText(ctx, text, box.w * 0.94, fs);
    if (lines.length * fs * 1.36 <= box.h * 0.98 || fs <= 9) break;
    fs -= 1;
  }

  const lh = fs * 1.36;
  const totalH = lines.length * lh;
  let ty = box.y + Math.max(0, (box.h - totalH) / 2);
  if (ty + totalH > box.y + box.h) ty = Math.max(box.y, box.y + box.h - totalH);

  lines.forEach((line, i) => {
    drawHandLine(ctx, line, box.x + box.w * 0.015, ty + i * lh, fs, family, color);
  });
}

/* ---------- 人类差错：写错、划掉、补写 ---------- */
function choiceLetter(text) {
  const t = String(text || '').trim();
  const m = t.match(/^[（(【\[]?\s*([A-Ha-h])\s*(?:[）)】\].、,，:：．.\s]|$)/);
  return m ? m[1].toUpperCase() : null;
}

function wrongLetter(correct) {
  const pool = ['A', 'B', 'C', 'D'].filter(x => x !== correct);
  return pool[Math.floor(Math.random() * pool.length)];
}

function makeWrongAnswer(text) {
  const letter = choiceLetter(text);
  if (letter) return wrongLetter(letter);

  const t = String(text || '').trim();
  const num = t.match(/-?\d+(?:\.\d+)?/);
  if (num) {
    const v = Number(num[0]) + (Math.random() < 0.5 ? 1 : -1);
    return t.replace(num[0], String(v));
  }
  return t.slice(0, Math.max(2, Math.ceil(t.length * 0.42)));
}

/* 随手划改：两三道不平行、不整齐的划线 */
function inkScribble(ctx, x, y, w, h, color) {
  if (w < 4 || h < 4) return;
  ctx.save();
  ctx.strokeStyle = color;
  ctx.lineWidth = Math.max(1, Math.min(2.2, h * 0.08));
  ctx.lineCap = 'round';
  ctx.globalAlpha = 0.72;
  const strokes = 2 + Math.floor(Math.random() * 2);
  for (let i = 0; i < strokes; i++) {
    ctx.beginPath();
    ctx.moveTo(x + w * randRange(-0.06, 0.08), y + h * randRange(0.2, 0.8));
    ctx.quadraticCurveTo(
      x + w * randRange(0.4, 0.6), y + h * randRange(0.02, 0.98),
      x + w * randRange(0.9, 1.04), y + h * randRange(0.2, 0.8)
    );
    ctx.stroke();
  }
  ctx.restore();
}

/* 手绘圈：不闭合、略微重叠、带抖动 */
function handLoop(ctx, box, color) {
  if (!box || box.w < 6 || box.h < 4) return;
  const neat = state.human.neatness / 100;
  const wob = 0.05 + (1 - neat) * 0.2;
  const cx = box.x + box.w / 2;
  const cy = box.y + box.h / 2;
  const rx = box.w / 2 + 2;
  const ry = box.h / 2 + 2;

  ctx.save();
  ctx.strokeStyle = color;
  ctx.lineWidth = 1.4 + (1 - neat) * 0.9;
  ctx.globalAlpha = 0.72;
  ctx.lineCap = 'round';
  ctx.beginPath();
  const start = randRange(-0.5, 0.2);
  const sweep = Math.PI * 2 * randRange(1.03, 1.13);
  const steps = 34;
  for (let i = 0; i <= steps; i++) {
    const a = start + sweep * (i / steps);
    const r = 1 + Math.sin(a * 2.3) * wob * 0.45 + randRange(-wob, wob) * 0.55;
    const x = cx + Math.cos(a) * rx * r;
    const y = cy + Math.sin(a) * ry * r;
    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  }
  ctx.stroke();
  ctx.restore();
}

/* 手绘波浪下划线 */
function handUnderline(ctx, box, color) {
  if (!box || box.w < 8) return;
  const neat = state.human.neatness / 100;
  ctx.save();
  ctx.strokeStyle = color;
  ctx.lineWidth = 1.3 + (1 - neat) * 0.8;
  ctx.globalAlpha = 0.68;
  ctx.lineCap = 'round';
  ctx.beginPath();
  const steps = 14;
  for (let i = 0; i <= steps; i++) {
    const x = box.x + box.w * (i / steps);
    const y = box.y + box.h * 0.94 + Math.sin(i * 1.7) * (1 - neat) * 2.6 + randRange(-0.6, 0.6);
    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  }
  ctx.stroke();
  ctx.restore();
}

function toPx(box, W, H) {
  return { x: box.x * W, y: box.y * H, w: box.w * W, h: box.h * H };
}

/* 题干与选项圈画：已停用 */

function paintAnswer(ctx, box, style, finalText) {
  if (!finalText) return;
  const letter = choiceLetter(finalText);

  // 涂改率：先写错、划掉、再补写
  const willFix = Math.random() * 100 < state.human.correction;
  if (!willFix) {
    humanPaint(ctx, finalText, box, style);
    return;
  }

  const color = inkColor();

  if (letter && box.w >= 24) {
    const wBox = { x: box.x, y: box.y, w: box.w * 0.42, h: box.h };
    const rBox = { x: box.x + box.w * 0.45, y: box.y - box.h * 0.03, w: box.w * 0.55, h: box.h * 0.96 };
    humanPaint(ctx, makeWrongAnswer(finalText), wBox, style);
    inkScribble(ctx, wBox.x + wBox.w * 0.1, wBox.y + wBox.h * 0.15, wBox.w * 0.8, wBox.h * 0.7, color);
    humanPaint(ctx, finalText, rBox, style);
    return;
  }

  if (box.h < 46 || box.w < 90) {
    humanPaint(ctx, finalText, box, style);
    return;
  }

  const topH = Math.min(box.h * 0.34, 30);
  const wBox = { x: box.x, y: box.y, w: box.w * 0.8, h: topH };
  humanPaint(ctx, makeWrongAnswer(finalText), wBox, style);
  inkScribble(ctx, wBox.x + wBox.w * 0.05, wBox.y + wBox.h * 0.2, wBox.w * 0.6, wBox.h * 0.62, color);
  humanPaint(ctx, finalText, {
    x: box.x,
    y: box.y + topH * 1.25,
    w: box.w,
    h: Math.max(8, box.h - topH * 1.25)
  }, style);
}

/* 按作答要求裁剪答案文本 */
function answerTextFor(q) {
  const raw = toDisplayText(q.answer);
  const req = state.requirement || '';
  if (/只写选项|只写字母|只填字母|只写答案/.test(req)) {
    const letter = choiceLetter(raw);
    if (letter) return letter;
  }
  if (/不写过程|不要过程|只写结果/.test(req)) {
    return oneLine(raw).split(/[。；]/)[0];
  }
  return raw;
}

async function renderPrecise(page, items) {
  const img = await loadImage(page.dataUrl);
  const canvas = document.createElement('canvas');
  canvas.width = img.naturalWidth;
  canvas.height = img.naturalHeight;
  const ctx = canvas.getContext('2d');
  ctx.drawImage(img, 0, 0);

  for (const q of items) {
    const a = q.area;
    const box = { x: a.x * canvas.width, y: a.y * canvas.height, w: a.w * canvas.width, h: a.h * canvas.height };
    const answer = answerTextFor(q);
    const letter = choiceLetter(answer);
    let finalText = answer;
    if (letter && Math.random() * 100 > state.human.accuracy) finalText = wrongLetter(letter);
    paintAnswer(ctx, box, state.style, finalText);
  }
  return canvas.toDataURL('image/png');
}

/* ---------------- 本地字迹引擎 ---------------- */
/* AI 只给答案和大致行；括号、横线、字号、贴线由 auto_render.py 自己检测 */
function buildLocalCommands(items) {
  const out = [];
  for (const q of items) {
    const a = q.area || {};
    const ax = Number(a.x), ay = Number(a.y), aw = Number(a.w), ah = Number(a.h);
    const areaX = clamp((Number.isFinite(ax) ? ax : 0.5) + (Number.isFinite(aw) ? aw : 0) / 2, 0.02, 0.98);
    const areaY = clamp((Number.isFinite(ay) ? ay : 0.5) + (Number.isFinite(ah) ? ah : 0) / 2, 0.02, 0.98);
    const yv = Number(q.answer_y);
    const xv = Number(q.answer_x);
    const y = Number.isFinite(yv)
      ? clamp(yv > 1 && yv <= 100 ? yv / 100 : yv, 0.02, 0.98)
      : areaY;
    const x = Number.isFinite(xv)
      ? clamp(xv > 1 && xv <= 100 ? xv / 100 : xv, 0.02, 0.98)
      : areaX;
    const raw = answerTextFor(q).trim();
    const type = String(q.type || '');
    const letter = choiceLetter(raw);
    if (/选择/.test(type) || (!type && letter)) {
      if (letter) out.push({ q: q.index, type: 'choice', text: letter, y });
      continue;
    }
    if (/解答|计算|证明|综合|大题/.test(type) || raw.split('\n').length >= 3) {
      out.push({
        q: q.index,
        type: 'work',
        x: clamp(Number.isFinite(ax) ? ax : 0.08, 0.02, 0.9),
        y: clamp(Number.isFinite(ay) ? ay : 0.1, 0.02, 0.95),
        w: clamp(Number.isFinite(aw) ? aw : 0.84, 0.2, 0.95),
        size: 36,
        lines: raw.split(/\n+/).map(s => s.trim()).filter(Boolean)
      });
      continue;
    }
    const frac = raw.match(/^(-?[0-9a-zA-Z().+\-]{1,8})\s*\/\s*([0-9a-zA-Z().+\-]{1,8})$/);
    if (frac) {
      out.push({ q: q.index, type: 'fraction', num: frac[1], den: frac[2], y, x });
      continue;
    }
    const text = oneLine(raw);
    const item = { q: q.index, type: 'fill', text, y, x };
    if (text.length > 40) item.size = 28;
    out.push(item);
  }
  return out;
}

async function renderWithLocalTool(page, items) {
  const commands = { style: state.style, profile: styleProfile(), items: buildLocalCommands(items) };
  if (!commands.items.length) throw new Error('没有可落笔的答案');
  const image = await prepareForApi(page.dataUrl, 1700);
  let res;
  try {
    res = await fetch('/api/render', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ image: image.dataUrl, commands })
    });
  } catch {
    throw new Error('连接不上本地落笔服务：请重新运行「启动.bat」并刷新页面');
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok || !data.ok) throw new Error(data.error || `本地落笔服务返回 ${res.status}`);
  return data;
}

/* ---------------- 智能体（本地 agent_runtime.py，SSE 实时日志） ---------------- */
let agentAbort = null;
let agentRunning = false;

async function runAgentStage(page, stage, plan, extra) {
  if (agentRunning) return toast('智能体正在运行，请先中止或等待完成', 'warn');
  if (stage !== 'compose' && (!state.vision.key.trim() || !state.llm.key.trim())) {
    switchTab('settings');
    return toast('请先填好识图（千问）和解题（DeepSeek）两个 Key', 'warn');
  }
  agentRunning = true;
  const ctrl = new AbortController();
  agentAbort = ctrl;
  const stopBtn = $('#btnAgentStop');
  if (stopBtn) stopBtn.hidden = false;
  setBusy(true, stage === 'scan' ? '智能体正在扫描定位…'
    : (stage === 'compose' ? '正在按你调整的位置生成定稿…' : '智能体正在按确认位置作答…'));
  log(stage === 'scan'
    ? '第一轮：扫描定位 → 生成框选图，确认后再作答'
    : (stage === 'compose'
      ? '定稿：按你拖动/缩放后的位置重新落笔，不再调用模型'
      : '第二轮：严格按确认的位置作答'));

  let doneEvt = null;
  try {
    const img = await prepareForApi(page.dataUrl, 1700);
    const req = String(state.requirement || '').trim();
    const task = (stage === 'scan'
      ? '只识别这张试卷并标出每一题的落笔区域，不要解题、不要落笔。'
      : (stage === 'compose'
        ? '按用户确认的布局定稿。'
        : '严格按用户确认的框作答，不要重新寻找位置。'))
      + (req ? '\n用户作答要求：' + req : '');

    let res;
    try {
      res = await fetch('/api/agent', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          image: img.dataUrl,
          filename: page.name,
          task,
          stage,
          plan: plan || null,
          layout: extra?.layout || null,
          dir: extra?.dir || '',
          profile: styleProfile(),
          trace: state.traceLevel,
          keys: {
            vision: { ...state.vision },
            llm: { ...state.llm }
          }
        }),
        signal: ctrl.signal
      });
    } catch {
      throw new Error('连接不上本地服务：请重新运行「启动.bat」并刷新页面');
    }
    if (!res.ok || !res.body) {
      const data = await res.json().catch(() => ({}));
      throw new Error(data.error || `智能体服务返回 ${res.status}`);
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf('\n\n')) >= 0) {
        const chunk = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        for (const line of chunk.split('\n')) {
          if (!line.startsWith('data:')) continue;
          let ev;
          try { ev = JSON.parse(line.slice(5).trim()); } catch { continue; }
          if (ev.type === 'log') {
            log(ev.text, /失败|错误|error|仍有失败/i.test(ev.text) ? 'warn' : '');
          } else if (ev.type === 'error') {
            throw new Error(ev.text || '智能体运行失败');
          } else if (ev.type === 'done') {
            doneEvt = ev;
          }
        }
      }
    }

    if (!doneEvt) throw new Error('智能体连接中断，没有拿到结果');
    if (stage === 'scan') {
      if (!doneEvt.plan) throw new Error('扫描没有返回位置计划');
      page.agentPlan = doneEvt.plan;
      page.agentPreview = doneEvt.preview || '';
      page.agentScan = doneEvt.scan || '';
      page.agentDoc = doneEvt.doc || '';
      page.agentDir = doneEvt.dir || '';
      page.agentLayout = null;
      const planItems = doneEvt.plan.items || [];
      page.analysis = {
        page_type: 'agent_scan',
        questions: planItems.map(it => {
          const b = it.box || [(it.x ?? 0.6) - 0.03, (it.y ?? 0.5) - 0.012,
                               (it.x ?? 0.6) + 0.03, (it.y ?? 0.5) + 0.012];
          return {
            index: it.q,
            type: it.type === 'work' ? '解答' : (it.type === 'choice' ? '选择' : '填空'),
            area: {
              x: b[0], y: b[1],
              w: Math.max(0.02, b[2] - b[0]),
              h: Math.max(0.012, b[3] - b[1])
            },
            _checked: true
          };
        })
      };
      page.showScan = !!(page.agentScan || page.agentPreview);
      page.result = null;
      page.calib = '';
      page.showResult = false;
      page.showCalib = false;
      page.showGrid = false;
      state.selectedQ = -1;
      renderAll();
      if (page.agentPreview) log('框选图：' + page.agentPreview);
      if (page.agentDoc) log('文档页：' + page.agentDoc);
      if (doneEvt.dir) log('扫描件/文档目录：' + doneEvt.dir);
      log('第一轮完成：可直接拖动/缩放框，确认后点“作答”', 'ok');
      toast('第一轮完成，调整框选后点作答', 'ok');
    } else if (stage === 'compose') {
      if (!doneEvt.result) throw new Error('定稿没有产出图片');
      page.result = doneEvt.result;
      page.calib = doneEvt.calib || '';
      page.agentLayout = null;
      page.analysis = null;
      page.showResult = true;
      page.showScan = false;
      page.showCalib = false;
      page.showGrid = false;
      renderAll();
      if (page._ansRefresh) page._ansRefresh();
      log('定稿完成，可直接导出：' + doneEvt.result, 'ok');
      toast('定稿完成，可导出', 'ok');
    } else {
      if (!doneEvt.result) throw new Error('智能体没有产出作答图，请看上方日志里的题号');
      page.result = doneEvt.result;
      page.calib = doneEvt.calib || '';
      page.agentDir = doneEvt.dir || '';
      page.agentLayout = doneEvt.layout || null;
      if (page.agentLayout && Array.isArray(page.agentLayout.items)) {
        page.analysis = {
          page_type: 'agent_layout',
          questions: page.agentLayout.items.map(it => {
            const b = it.box || [];
            return {
              index: it.q,
              type: it.type === 'work' ? '解答' : (it.type === 'choice' ? '选择' : '填空'),
              area: {
                x: b[0] ?? 0.5, y: b[1] ?? 0.5,
                w: Math.max(0.015, (b[2] ?? 0.55) - (b[0] ?? 0.5)),
                h: Math.max(0.012, (b[3] ?? 0.52) - (b[1] ?? 0.5))
              },
              _checked: true
            };
          })
        };
      }
      page.showScan = false;
      page.showResult = true;
      page.showCalib = false;
      page.showGrid = false;
      renderAll();
      log('作答完成：结果图' + (doneEvt.calib ? ' + 校准图' : '（本次没有校准图）'), 'ok');
      if (page.agentLayout) log('可以直接拖动/缩放框，调好后点「确认定稿」重新生成', 'ok');
      toast(page.agentLayout ? '作答图已生成，可调位置后定稿' : '作答图已生成', 'ok');
      if (page.agentLayout) openAnsEditor(page);
    }
  } catch (e) {
    if (e.name === 'AbortError') {
      log('已中止智能体运行', 'warn');
      toast('已中止', 'warn');
    } else {
      log('智能体失败：' + e.message, 'err');
      toast(e.message, 'error');
    }
  } finally {
    agentRunning = false;
    agentAbort = null;
    if (stopBtn) stopBtn.hidden = true;
    setBusy(false);
  }
}

async function runAgentScan(page) {
  return runAgentStage(page, 'scan');
}

/* 把用户拖动/缩放后的框写回 plan，第二轮严格按它落笔 */
function syncPlanFromBoxes(page) {
  const items = page.agentPlan?.items || [];
  const qs = page.analysis?.questions || [];
  items.forEach((it, i) => {
    const q = qs[i];
    if (!q || !q.area) return;
    const a = q.area;
    it.box = [a.x, a.y, a.x + a.w, a.y + a.h];
    it.skip = q._checked === false;
    if ((it.type || '').toLowerCase() === 'work') {
      it.x = a.x;
      it.y = a.y;
      it.w = a.w;
      it.h = a.h;
      it.area = { ...(it.area || {}), x: a.x, y: a.y, w: a.w, h: a.h };
    } else {
      it.y = a.y + a.h / 2;
      it.x = a.x + a.w / 2;
    }
  });
}

async function runAgentRender(page) {
  if (!page.agentPlan) return toast('请先点“分析/扫描定位”生成框选图并确认', 'warn');
  syncPlanFromBoxes(page);
  return runAgentStage(page, 'render', page.agentPlan);
}

/* 把用户拖动/缩放后的作答框写回布局，交给引擎脱机定稿 */
function layoutFromBoxes(page) {
  const layout = JSON.parse(JSON.stringify(page.agentLayout || {}));
  const qs = page.analysis?.questions || [];
  (layout.items || []).forEach((it, i) => {
    const a = qs[i]?.area;
    if (a) it.box = [a.x, a.y, a.x + a.w, a.y + a.h];
  });
  return layout;
}

async function handleCompose() {
  const page = activePage();
  if (!page || !page.agentLayout) {
    return toast('先生成一版作答图，再调整位置', 'warn');
  }
  const layout = layoutFromBoxes(page);
  return runAgentStage(page, 'compose', null, { layout, dir: page.agentDir || '' });
}

/* ---------------- 作答结果编辑器 ---------------- */
let ansItems = [];

function ansLabel(it) {
  const t = String(it.type || '').toLowerCase();
  if (t === 'work') return '过程';
  if (t === 'fraction') return `${it.num ?? ''}/${it.den ?? ''}`;
  return it.text || '答案';
}

function syncAnsLayer() {
  const st = $('#ansStage'), im = $('#ansImg'), ly = $('#ansLayer');
  if (!st || !im || !ly || !im.naturalWidth) return;
  const r = im.getBoundingClientRect(), sr = st.getBoundingClientRect();
  ly.style.left = (r.left - sr.left + st.scrollLeft) + 'px';
  ly.style.top = (r.top - sr.top + st.scrollTop) + 'px';
  ly.style.width = r.width + 'px';
  ly.style.height = r.height + 'px';
}

function renderAnsBoxes() {
  const ly = $('#ansLayer');
  if (!ly) return;
  ly.innerHTML = '';
  ansItems.forEach((it, i) => {
    const b = it.box || [0.5, 0.5, 0.55, 0.52];
    const el = document.createElement('div');
    el.className = 'ans-box';
    el.style.left = (b[0] * 100) + '%';
    el.style.top = (b[1] * 100) + '%';
    el.style.width = Math.max(0.6, (b[2] - b[0]) * 100) + '%';
    el.style.height = Math.max(0.6, (b[3] - b[1]) * 100) + '%';
    el.innerHTML = `<span class="tag">${escapeHtml(ansLabel(it))}</span>`
      + '<span class="grip"></span>';
    el.addEventListener('pointerdown', ev => startAnsDrag(
      ev, i, el, ev.target.classList.contains('grip') ? 'resize' : 'move'));
    ly.appendChild(el);
  });
  const list = $('#ansList');
  if (list) {
    list.innerHTML = ansItems.map(it =>
      `<div><span>第${escapeHtml(it.q)}题</span><b>${escapeHtml(ansLabel(it))}</b></div>`
    ).join('') || '<div>暂无答案</div>';
  }
}

function startAnsDrag(e, i, el, mode) {
  e.preventDefault();
  e.stopPropagation();
  const ly = $('#ansLayer');
  const rect = ly.getBoundingClientRect();
  if (rect.width < 10 || rect.height < 10) return;
  const start = [...(ansItems[i].box || [0.5, 0.5, 0.55, 0.52])];
  const sx = e.clientX, sy = e.clientY;
  try { el.setPointerCapture(e.pointerId); } catch { /* 老浏览器忽略 */ }
  const onMove = ev => {
    const r = ly.getBoundingClientRect();
    if (r.width < 10 || r.height < 10) return;
    // 用“起点 + 总位移”计算，避免逐帧累计带来的比例误差
    const dx = (ev.clientX - sx) / r.width;
    const dy = (ev.clientY - sy) / r.height;
    const b = [...start];
    if (mode === 'resize') {
      b[2] = clamp(start[2] + dx, start[0] + 0.008, 1);
      b[3] = clamp(start[3] + dy, start[1] + 0.006, 1);
    } else {
      const w = start[2] - start[0], h = start[3] - start[1];
      b[0] = clamp(start[0] + dx, 0, 1 - w);
      b[1] = clamp(start[1] + dy, 0, 1 - h);
      b[2] = b[0] + w;
      b[3] = b[1] + h;
    }
    const box = b.map(v => Math.round(v * 10000) / 10000);
    ansItems[i].box = box;
    // 拖拽过程中只改当前元素，不重建 DOM，保证和鼠标 1:1
    el.style.left = (box[0] * 100) + '%';
    el.style.top = (box[1] * 100) + '%';
    el.style.width = Math.max(0.4, (box[2] - box[0]) * 100) + '%';
    el.style.height = Math.max(0.4, (box[3] - box[1]) * 100) + '%';
  };
  const onUp = () => {
    document.removeEventListener('pointermove', onMove);
    document.removeEventListener('pointerup', onUp);
    document.removeEventListener('pointercancel', onUp);
    try { el.releasePointerCapture(e.pointerId); } catch { /* 忽略 */ }
    renderAnsBoxes();
  };
  document.addEventListener('pointermove', onMove);
  document.addEventListener('pointerup', onUp);
  document.addEventListener('pointercancel', onUp);
}

function answerProfile() {
  const neat = clamp(Number($('#ansSloppy')?.value ?? 35), 0, 100) / 100;
  const inks = { heavy: [13, 13, 13], normal: [28, 28, 28], light: [59, 59, 59] };
  return {
    ink: inks[state.ansInk || 'normal'] || inks.normal,
    spacing: 0.96 + neat * 0.10,
    slant: 0.01 + neat * 0.09,
    pressure: 0.05 + neat * 0.13,
    baseline: 0.90 + neat * 0.45,
    stroke: state.ansInk === 'heavy' ? 1 : (state.ansInk === 'light' ? -1 : 0)
  };
}

function openAnsEditor(page) {
  const ed = $('#ansEditor');
  if (!ed) return toast('页面还是旧版，请 Ctrl+F5 强制刷新', 'warn');
  if (!page || !page.result) return toast('先点「一键作答」生成结果', 'warn');
  const items = page.agentLayout?.items;
  ansItems = Array.isArray(items) ? JSON.parse(JSON.stringify(items)) : [];
  // 默认沿用已学习的字迹风格，用户仍可在右侧改
  const neat = clamp(Number(state.fontProfile?.neatness ?? state.human.neatness ?? 35), 0, 100);
  if ($('#ansSloppy')) {
    $('#ansSloppy').value = neat;
    $('#ansSloppyNum').textContent = neat + '%';
  }
  state.ansInk = state.fontProfile?.tone || state.inkTone || 'normal';
  $$('#ansInkSeg button').forEach(b =>
    b.classList.toggle('active', b.dataset.ink === state.ansInk));
  ed.hidden = false;
  $('#ansMeta').textContent = ansItems.length
    ? `${ansItems.length} 个答案 · 拖动框移动，拖右下角改大小`
    : '可拖动调整';
  const refresh = () => { syncAnsLayer(); renderAnsBoxes(); };
  $('#ansImg').onload = refresh;
  $('#ansImg').src = page.result;
  page._ansRefresh = refresh;
  setTimeout(refresh, 60);
}

async function composeFromEditor() {
  const page = activePage();
  if (!page || !page.agentLayout) return toast('还没有可定稿的作答结果', 'warn');
  const base = JSON.parse(JSON.stringify(page.agentLayout));
  base.items = JSON.parse(JSON.stringify(ansItems));
  base.profile = answerProfile();
  await runAgentStage(page, 'compose', null, { layout: base, dir: page.agentDir || '' });
}

async function handleSolve() {
  const page = activePage();
  if (!page) return toast('请先上传图片', 'warn');
  if (state.output === 'agent') {
    await runAgentRender(page);
    return;
  }
  if (!page.analysis) return toast('请先识图分析', 'warn');

  const items = targetQuestions(page);
  if (!items.length) return toast('没有可作答的题目，请检查勾选与答案', 'warn');

  setBusy(true, '正在按坐标落笔…');
  try {
    const r = await renderWithLocalTool(page, items);
    page.result = r.image;
    page.calib = r.calib || '';
    const pass = (r.report || []).filter(x => x.ok).length;
    const total = (r.report || []).length;
    if (!r.calib) log('提示：这次没有拿到校准图', 'warn');
    const bad = (r.report || []).filter(x => !x.ok).map(x => x.q);
    if (bad.length) log(`未通过：第 ${bad.join('、')} 题（已尝试自动微调）`, 'warn');
    log(`本地字迹引擎：${pass}/${total} 题落笔通过（已附校准图）`, total && pass === total ? 'ok' : 'warn');
    page.showResult = true;
    log(`已生成作答图（精准落位 · ${items.length} 题）`, 'ok');
    renderAll();
    toast('作答图已生成', 'ok');
  } catch (e) {
    log('生成失败：' + e.message, 'err');
    toast(e.message, 'error');
  } finally {
    setBusy(false);
  }
}

/* 一键：识图 + 落笔 */
async function handleAuto() {
  const page = activePage();
  if (!page) return toast('请先上传图片', 'warn');

  if (state.output === 'agent') {
    if (!page.agentPlan) await runAgentStage(page, 'scan');
    if (page.agentPlan) {
      syncPlanFromBoxes(page);
      await runAgentStage(page, 'render', page.agentPlan);
    }
    return;
  }

  if (!page.analysis) {
    if (!state.vision.key.trim()) {
      switchTab('settings');
      return toast('请先在设置里填写识图模型的 API Key', 'warn');
    }
    try {
      await analyzePage(page);
      renderAll();
    } catch (e) {
      toast(e.message, 'error');
      return;
    }
  }
  await handleSolve();
}

/* ---------------- 网格坐标落笔 ---------------- */
async function buildGridCanvas(page) {
  const img = await loadImage(page.dataUrl);
  const W = img.naturalWidth;
  const H = img.naturalHeight;
  const canvas = document.createElement('canvas');
  canvas.width = W;
  canvas.height = H;
  const ctx = canvas.getContext('2d');
  ctx.drawImage(img, 0, 0);

  // 1% 细网格
  ctx.strokeStyle = 'rgba(214, 40, 40, .20)';
  ctx.lineWidth = Math.max(1, Math.round(W / 1600));
  for (let i = 1; i < 100; i++) {
    if (i % 10 === 0) continue;
    const x = i / 100 * W;
    const y = i / 100 * H;
    ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, H); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(W, y); ctx.stroke();
  }

  // 10% 主网格
  ctx.strokeStyle = 'rgba(198, 28, 28, .62)';
  ctx.lineWidth = Math.max(1.5, Math.round(W / 850));
  for (let i = 0; i <= 100; i += 10) {
    const x = i / 100 * W;
    const y = i / 100 * H;
    ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, H); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(W, y); ctx.stroke();
  }

  // 每 10 格标一次坐标
  const fs = Math.max(11, Math.round(W / 100));
  ctx.font = `600 ${fs}px "Microsoft YaHei", sans-serif`;
  const pad = 3;
  for (let i = 0; i <= 100; i += 10) {
    const x = i / 100 * W;
    const y = i / 100 * H;
    const label = String(i);
    const tw = ctx.measureText(label).width;
    // 顶部
    ctx.fillStyle = 'rgba(255,255,255,.88)';
    ctx.fillRect(x - tw / 2 - pad, 1, tw + pad * 2, fs + 3);
    ctx.fillStyle = '#c81c1c';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'top';
    ctx.fillText(label, x, 2);
    // 左侧
    ctx.fillStyle = 'rgba(255,255,255,.88)';
    ctx.fillRect(1, y - fs / 2 - 1, tw + pad * 2, fs + 3);
    ctx.fillStyle = '#c81c1c';
    ctx.textAlign = 'left';
    ctx.textBaseline = 'middle';
    ctx.fillText(label, 2 + pad, y);
  }
  return canvas.toDataURL('image/png');
}

async function handleGrid() {
  const page = activePage();
  if (!page) return toast('请先上传图片', 'warn');
  setBusy(true, '正在生成 100×100 网格图…');
  try {
    page.gridUrl = await buildGridCanvas(page);
    page.showGrid = true;
    page.showResult = false;
    renderStage();
    updateButtons();
    const name = (page.name || '试卷').replace(/\.[^.]+$/, '');
    downloadUrl(page.gridUrl, `${name}_网格.png`);
    log('网格图已生成并下载：把它发给 AI，让它按格子返回矩形', 'ok');
    toast('网格图已生成', 'ok');
  } catch (e) {
    log('生成网格图失败：' + e.message, 'err');
    toast(e.message, 'error');
  } finally {
    setBusy(false);
  }
}

function fillGridAnswers() {
  const page = activePage();
  const box = $('#gridAnswers');
  if (!page || !box) return;
  if (box.value.trim()) return;
  const qs = page.analysis?.questions || [];
  if (qs.length) {
    box.value = qs.map(q => `第${q.index}题：${toDisplayText(answerTextFor(q))}`).join('\n');
  }
}

function buildGridPrompt(page) {
  const answers = ($('#gridAnswers')?.value || '').trim()
    || (page.analysis?.questions || []).map(q => `第${q.index}题：${toDisplayText(q.answer)}`).join('\n');
  return [
    '这张试卷图上叠了 100×100 的红色坐标网格：横向 0-100，纵向 0-100，每 10 格有数字标注。',
    '请为下面每道题的答案，在卷面上找出应该书写的矩形区域。',
    '',
    '答案：',
    answers || '（卷面题目按你的判断作答）',
    '',
    '只输出 JSON，不要 markdown，不要任何解释：',
    '{"items":[{"index":1,"text":"D","type":"choice","rect":{"x":0.48,"y":0.11,"w":0.04,"h":0.03}}]}',
    '说明：',
    '- rect 使用 0~1 归一化坐标，x/y 是矩形左上角，w/h 是宽高；',
    '- 选择题 rect 正好包住括号里的空白，text 只写选项字母；',
    '- 填空题 text 写答案；分数写成 {"type":"fraction","num":"1","den":"3"}；',
    '- 矩形不要盖住题干和选项文字，宁可小一点。'
  ].join('\n');
}

async function handleGridPrompt() {
  const page = activePage();
  if (!page) return toast('请先上传图片', 'warn');
  if (!page.gridUrl) return toast('先点①生成网格图', 'warn');
  fillGridAnswers();
  await copyText(buildGridPrompt(page));
  toast('坐标提示词已复制，连网格图一起发给 AI', 'ok');
}

function normalizeGridRect(r) {
  let x = Number(r.x);
  let y = Number(r.y);
  let w = Number(r.w ?? r.width);
  let h = Number(r.h ?? r.height);
  if (![x, y, w, h].every(Number.isFinite)) return null;
  if (Math.max(Math.abs(x), Math.abs(y), Math.abs(w), Math.abs(h)) > 1.5) {
    x /= 100; y /= 100; w /= 100; h /= 100;
  }
  x = clamp(x, 0, 1);
  y = clamp(y, 0, 1);
  w = clamp(w, 0.004, 1 - x);
  h = clamp(h, 0.004, 1 - y);
  return { x, y, w, h };
}

function handleGridImport() {
  const page = activePage();
  if (!page) return toast('请先上传图片', 'warn');
  let raw = ($('#gridJson').value || '').trim();
  if (!raw) return toast('先把 AI 返回的 JSON 粘进来', 'warn');
  raw = raw.replace(/^```json\s*/i, '').replace(/```$/i, '').trim();
  let data;
  try {
    data = JSON.parse(raw);
  } catch (e) {
    return toast('JSON 解析失败：' + e.message, 'error');
  }
  const list = Array.isArray(data) ? data : (data.items || data.questions || []);
  if (!list.length) return toast('JSON 里没有 items', 'warn');
  const items = [];
  list.forEach((it, i) => {
    const rect = normalizeGridRect(it.rect || it.area || {});
    if (!rect) return;
    if (it.type === 'fraction' || it.num != null) {
      items.push({ index: it.index ?? i + 1, type: 'fraction', num: String(it.num ?? ''), den: String(it.den ?? ''), rect });
    } else {
      items.push({ index: it.index ?? i + 1, type: it.type || 'choice', text: String(it.text ?? it.answer ?? ''), rect });
    }
  });
  if (!items.length) return toast('没有可用的矩形', 'warn');
  page.layout = items;
  page.showGrid = false;
  const badge = $('#gridBadge');
  badge.hidden = false;
  badge.textContent = `已定位 ${items.length} 题`;
  $('#gridNote').textContent = `已导入 ${items.length} 个矩形，点④本机落笔。`;
  updateButtons();
  toast(`已导入 ${items.length} 题矩形`, 'ok');
}

async function handleGridRender() {
  const page = activePage();
  if (!page?.layout?.length) return toast('先导入矩形 JSON', 'warn');
  setBusy(true, '正在本机落笔…');
  try {
    const res = await fetch('http://127.0.0.1:9130/render', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ image: page.dataUrl, items: page.layout })
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
    if (!data.image) throw new Error('接口没有返回图片');
    page.result = data.image;
    page.showResult = true;
    page.showGrid = false;
    renderStage();
    updateButtons();
    log(`本机落笔完成（${page.layout.length} 题）`, 'ok');
    toast('作答图已生成', 'ok');
  } catch (e) {
    log('本机落笔失败：' + e.message, 'err');
    toast('本机落笔服务没启动：先运行「启动.bat」', 'error');
  } finally {
    setBusy(false);
  }
}

/* ---------------- 渲染总控 ---------------- */
function renderAll() {
  renderPages();
  renderStage();
  renderQuestions();
  updateButtons();
  updateHint();
  updateReqPreview();
  fillGridAnswers();
  const p = activePage();
  const badge = $('#gridBadge');
  if (badge) {
    badge.hidden = !p?.layout?.length;
    if (p?.layout?.length) badge.textContent = `已定位 ${p.layout.length} 题`;
  }
  const note = $('#gridNote');
  if (note) {
    note.textContent = p?.layout?.length
      ? `已导入 ${p.layout.length} 个矩形，点④本机落笔。`
      : (p?.gridUrl ? '网格图已生成，复制②发给 AI。' : '先传图，再点①生成网格图。');
  }
}

function updateHint() {
  const p = activePage();
  const hint = $('#outputHint');
  if (hint) {
    hint.textContent = state.output === 'agent'
      ? '智能体：自动识题、解题、落笔并逐题自检；失败会明确报出题号。'
      : (state.output === 'precise'
        ? '精准落位：原图一个像素都不改，只在答案位置叠加字迹。'
        : 'AI 整页重绘：模型重画整张卷面并写入答案，笔迹更自然，细节可能轻微变化。');
  }
  const solveHint = $('#solveHint');
  if (solveHint) {
    solveHint.textContent = state.output === 'agent'
      ? (p?.agentPlan
        ? '已扫描定位：核对框选图后点「作答」，将严格按确认位置落笔。'
        : '第一轮点「识图分析」扫描定位并生成框选图，确认后点「作答」；也可点「一键」跑完两轮。')
      : (p?.analysis
        ? `将书写 ${targetQuestions(p).length} 题${state.requirement.trim() ? '，按左侧作答要求执行' : ''}。虚线框可拖动、可缩放。`
        : '上传图片后先识图，或直接点「一键识图 + 落笔」。');
  }
}

function updateButtons() {
  const p = activePage();
  const busy = state.busy;
  const bind = (sel, disabled) => { const el = $(sel); if (el) el.disabled = disabled; };
  bind('#btnAnalyze', !p || busy);
  bind('#btnSolve', !p || busy || (state.output !== 'agent' && !p.analysis));
  bind('#btnAuto', !p || busy);
  bind('#btnDownload', !p || !p.result);
  bind('#btnAnsEdit', !p || !p.result);
  bind('#btnExportImage', !p || !p.result);
  bind('#btnDownloadAll', !state.pages.some(x => x.result) || busy);
  const compEl = $('#btnCompose');
  if (compEl) {
    compEl.hidden = !(p && p.agentLayout);
    compEl.disabled = busy;
  }
  bind('#btnClear', !state.pages.length || busy);
  bind('#btnUpload', busy);
  bind('#btnUploadBottom', busy);
  bind('#btnTest', busy);
  bind('#btnLearnUpload', busy);
  bind('#btnLearnApply', !fontSamples.length || busy);
  bind('#btnApplyReq', busy);
  bind('#btnApplyRange', !p || busy);
  bind('#btnCopyPrompt', !p || busy);
  bind('#btnImportResult', !p || busy);
  bind('#btnGrid', !p || busy);
  bind('#btnGridPrompt', !p || busy);
  bind('#btnGridImport', !p || busy);
  bind('#btnGridRender', !p?.layout?.length || busy);
  $$('#viewSeg button').forEach(b => {
    if (b.dataset.view === 'result') b.disabled = !p?.result;
    if (b.dataset.view === 'grid') b.disabled = !p?.gridUrl;
    if (b.dataset.view === 'calib') b.disabled = !p?.calib;
  });
}

/* ---------------- 导出 ---------------- */
function dataUrlToBlob(dataUrl) {
  const comma = dataUrl.indexOf(',');
  const head = comma >= 0 ? dataUrl.slice(0, comma) : '';
  const body = comma >= 0 ? dataUrl.slice(comma + 1) : dataUrl;
  const mime = (head.match(/data:([^;]+)/) || [])[1] || 'image/png';
  if (/;base64/i.test(head)) {
    const bin = atob(body);
    const u8 = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) u8[i] = bin.charCodeAt(i);
    return new Blob([u8], { type: mime });
  }
  return new Blob([decodeURIComponent(body)], { type: mime });
}

function downloadUrl(url, filename) {
  let href = url;
  let temp = false;
  if (url.startsWith('data:')) {
    href = URL.createObjectURL(dataUrlToBlob(url));
    temp = true;
  }
  const a = document.createElement('a');
  a.href = href;
  a.download = filename;
  a.style.display = 'none';
  document.body.appendChild(a);
  a.click();
  a.remove();
  if (temp) setTimeout(() => URL.revokeObjectURL(href), 15000);
}

function calibName(page) {
  const base = (page.name || '试卷').replace(/\.[^.]+$/, '').replace(/[\\/:*?"<>|]/g, '_');
  return `${base}_校准图.png`;
}

function safeName(page) {
  const base = (page.name || '试卷').replace(/\.[^.]+$/, '').replace(/[\\/:*?"<>|]/g, '_');
  return `${base}_已作答.png`;
}

async function ensureDataUrl(url) {
  if (url.startsWith('data:')) return url;
  try {
    const res = await fetch(url);
    const blob = await res.blob();
    return await blobToDataUrl(blob);
  } catch {
    return url;
  }
}

async function handleDownload() {
  const p = activePage();
  if (!p?.result) return;
  const url = await ensureDataUrl(p.result);
  downloadUrl(url, safeName(p));
  if (p.calib) {
    await new Promise(r => setTimeout(r, 350));
    downloadUrl(p.calib, calibName(p));
    toast('已导出：结果图 + 校准图', 'ok');
  } else {
    toast('已导出结果图（本次没有校准图）', 'ok');
  }
}

async function handleDownloadAll() {
  const pages = state.pages.filter(p => p.result);
  if (!pages.length) return;
  log(`开始导出 ${pages.length} 页作答图`);
  for (let i = 0; i < pages.length; i++) {
    const p = pages[i];
    const url = await ensureDataUrl(p.result);
    downloadUrl(url, `第${i + 1}页_${safeName(p)}`);
    if (p.calib) {
      await new Promise(r => setTimeout(r, 350));
      downloadUrl(p.calib, `第${i + 1}页_${calibName(p)}`);
    }
    await new Promise(r => setTimeout(r, 350));
  }
  toast(`已导出 ${pages.length} 页（结果图 + 校准图）`, 'ok');
}

/* ---------------- 学习你的字体 ---------------- */
let fontSamples = [];

function renderFontThumbs() {
  const box = $('#fontThumbs');
  if (!box) return;
  const urls = fontSamples.length ? fontSamples : (state.fontProfile?.thumbs || []);
  box.innerHTML = '';
  urls.slice(0, 5).forEach((url, i) => {
    const img = document.createElement('img');
    img.src = url;
    img.alt = '往期作答 ' + (i + 1);
    box.appendChild(img);
  });
}

function compressImage(dataUrl, maxSide = 300, quality = 0.72) {
  return loadImage(dataUrl).then(img => {
    const scale = Math.min(1, maxSide / Math.max(img.naturalWidth, img.naturalHeight));
    const canvas = document.createElement('canvas');
    canvas.width = Math.max(1, Math.round(img.naturalWidth * scale));
    canvas.height = Math.max(1, Math.round(img.naturalHeight * scale));
    canvas.getContext('2d').drawImage(img, 0, 0, canvas.width, canvas.height);
    return canvas.toDataURL('image/jpeg', quality);
  }).catch(() => dataUrl);
}

/* 本地像素分析：墨色深浅、笔画粗细、行带整齐度、字号 */
async function sampleHandwriting(dataUrl) {
  const img = await loadImage(dataUrl);
  const scale = Math.min(1, 900 / Math.max(img.naturalWidth, img.naturalHeight));
  const w = Math.max(1, Math.round(img.naturalWidth * scale));
  const h = Math.max(1, Math.round(img.naturalHeight * scale));
  const canvas = document.createElement('canvas');
  canvas.width = w;
  canvas.height = h;
  const ctx = canvas.getContext('2d', { willReadFrequently: true });
  ctx.drawImage(img, 0, 0, w, h);
  const data = ctx.getImageData(0, 0, w, h).data;

  let dark = 0;
  let lumSum = 0;
  const rows = new Uint32Array(h);
  const mask = new Uint8Array(w * h);

  for (let y = 0; y < h; y++) {
    let count = 0;
    for (let x = 0; x < w; x++) {
      const i = (y * w + x) * 4;
      const lum = 0.299 * data[i] + 0.587 * data[i + 1] + 0.114 * data[i + 2];
      if (lum < 118) {
        dark++;
        lumSum += lum;
        mask[y * w + x] = 1;
        count++;
      }
    }
    rows[y] = count;
  }
  if (dark < w * h * 0.0008) return null;

  // 水平连续暗像素长度，近似笔画粗细
  let runs = 0;
  let runSum = 0;
  for (let y = 0; y < h; y += 2) {
    let run = 0;
    for (let x = 0; x < w; x++) {
      if (mask[y * w + x]) {
        run++;
      } else if (run) {
        runSum += run;
        runs++;
        run = 0;
      }
    }
    if (run) { runSum += run; runs++; }
  }
  const stroke = runs ? runSum / runs : 1;
  const avgLum = lumSum / dark;
  const density = dark / (w * h);

  // 文本行带
  const thr = Math.max(2, Math.round(w * 0.004));
  const bands = [];
  let start = -1;
  for (let y = 0; y < h; y++) {
    const on = rows[y] >= thr;
    if (on && start < 0) start = y;
    if ((!on || y === h - 1) && start >= 0) {
      const end = y;
      if (end - start >= 3) bands.push({ y: start, h: end - start });
      start = -1;
    }
  }

  let neatness = 55;
  let sizePct = 120;
  if (bands.length >= 2) {
    const hs = bands.map(b => b.h).sort((a, b) => a - b);
    const mean = hs.reduce((a, b) => a + b, 0) / hs.length;
    const sd = Math.sqrt(hs.reduce((a, b) => a + (b - mean) ** 2, 0) / hs.length);
    const cv = mean ? sd / mean : 0;
    const centers = bands.map(b => b.y + b.h / 2);
    let jitter = 0;
    for (let i = 1; i < centers.length; i++) jitter += Math.abs(centers[i] - centers[i - 1]);
    jitter = centers.length > 1 ? jitter / (centers.length - 1) / Math.max(1, mean) : 0;
    neatness = clamp(Math.round(88 - cv * 120 - jitter * 26 + stroke * 2), 18, 90);
    const median = hs[Math.floor(hs.length / 2)];
    sizePct = clamp(Math.round((median / w) * 3600), 85, 150);
  } else {
    neatness = clamp(Math.round(76 - stroke * 5 - (0.03 - Math.min(0.03, density)) * 300), 25, 80);
  }

  let tone = 'normal';
  if (avgLum < 58 || stroke >= 3.4) tone = 'heavy';
  else if (avgLum > 96 || stroke < 1.7) tone = 'light';

  return {
    tone,
    neatness,
    size: sizePct,
    stroke: Number(stroke.toFixed(2)),
    density: Number(density.toFixed(4)),
    avgLum: Math.round(avgLum),
    lines: bands.length
  };
}

function aggregateSamples(samples) {
  const avg = key => samples.reduce((a, s) => a + s[key], 0) / samples.length;
  const votes = {};
  samples.forEach(s => { votes[s.tone] = (votes[s.tone] || 0) + 1; });
  const tone = Object.keys(votes).sort((a, b) => votes[b] - votes[a])[0] || 'normal';
  return {
    tone,
    neatness: clamp(Math.round(avg('neatness')), 18, 92),
    size: clamp(Math.round(avg('size')), 85, 150),
    stroke: Number(avg('stroke').toFixed(2)),
    note: `笔画 ${avg('stroke').toFixed(1)}px · 覆盖率 ${(avg('density') * 100).toFixed(1)}%`
  };
}

function buildFontPrompt() {
  return [
    '你是笔迹分析器。观察这张学生往期手写作答的图片，分析书写风格。',
    '只输出 JSON，不要 markdown，不要任何解释：',
    '{',
    '  "tone": "heavy | normal | light",',
    '  "neatness": 0,',
    '  "size": 120,',
    '  "stroke": 2.6,',
    '  "note": "不超过 20 个字的风格描述"',
    '}',
    '说明：tone 只能是黑色系墨色档位（heavy 浓黑 / normal 标准黑 / light 淡墨），禁止蓝色或彩色；',
    'neatness 为 0~100 的整数，越低越潦草；size 为 80~150 的字号百分比；stroke 为平均笔画粗细（像素）。'
  ].join('\n');
}

async function refineFontWithAI(dataUrl) {
  const image = await prepareForApi(dataUrl, 1400);
  const text = await apiVision(image, buildFontPrompt());
  const raw = extractJson(text);
  const out = {};
  if (['heavy', 'normal', 'light'].includes(raw.tone)) out.tone = raw.tone;
  if (Number.isFinite(Number(raw.neatness))) out.neatness = clamp(Math.round(Number(raw.neatness)), 18, 92);
  if (Number.isFinite(Number(raw.size))) out.size = clamp(Math.round(Number(raw.size)), 85, 150);
  if (Number.isFinite(Number(raw.stroke))) out.stroke = Number(Number(raw.stroke).toFixed(2));
  if (typeof raw.note === 'string' && raw.note.trim()) out.note = raw.note.trim().slice(0, 24);
  return Object.keys(out).length ? out : null;
}

async function handleLearnFont() {
  if (!fontSamples.length) return toast('先上传往期作答图片', 'warn');
  setBusy(true, '正在学习你的字迹…');
  try {
    const samples = [];
    for (const url of fontSamples) {
      const s = await sampleHandwriting(url);
      if (s) {
        samples.push(s);
        log(`字迹取样：墨色 ${INK_TONES[s.tone].label} · 工整 ${s.neatness}% · 字号 ${s.size}%`);
      }
    }
    if (!samples.length) throw new Error('这些图片里没有找到明显笔迹');

    let profile = aggregateSamples(samples);
    if (state.vision.key.trim()) {
      try {
        const refined = await refineFontWithAI(fontSamples[0]);
        if (refined) {
          profile = { ...profile, ...refined };
          log('视觉模型已细化字迹档案', 'ok');
        }
      } catch (e) {
        log('AI 细化失败，使用本地分析：' + e.message, 'warn');
      }
    }

    const thumbs = [];
    for (const url of fontSamples.slice(0, 3)) thumbs.push(await compressImage(url));
    state.fontProfile = { ...profile, thumbs, at: Date.now() };

    state.inkTone = profile.tone;
    state.human.neatness = profile.neatness;
    state.human.size = profile.size;

    renderFontThumbs();
    syncHumanUI();
    saveConfig(true);
    log(`字迹已学习：墨色 ${INK_TONES[profile.tone].label} · 工整 ${profile.neatness}% · 字号 ${profile.size}%`, 'ok');
    toast('已学习你的字体，字迹设置已覆盖', 'ok');
  } catch (e) {
    log('字体学习失败：' + e.message, 'err');
    toast(e.message, 'error');
  } finally {
    setBusy(false);
  }
}

function clearFontProfile() {
  state.fontProfile = null;
  fontSamples = [];
  renderFontThumbs();
  syncHumanUI();
  saveConfig(true);
  toast('已清除字迹学习，滑杆恢复可调', 'ok');
}

async function handleFontFiles(files) {
  const list = Array.from(files || []).filter(f => f.type && f.type.startsWith('image/'));
  if (!list.length) return;
  const room = Math.max(0, 5 - fontSamples.length);
  if (!room) return toast('最多 5 张，先点「学习并应用」或清除', 'warn');
  setBusy(true, '正在读取往期作答…');
  try {
    for (const file of list.slice(0, room)) {
      fontSamples.push(await fileToDataUrl(file));
    }
    renderFontThumbs();
    updateButtons();
    log(`已加入往期作答 ${fontSamples.length} 张，点「学习并应用」开始分析`);
  } catch (e) {
    toast(e.message, 'error');
  } finally {
    setBusy(false);
  }
}

/* ---------------- 作答要求的界面同步 ---------------- */
function updateReqPreview() {
  const text = ($('#reqInput')?.value || '').trim();
  const set = parseQuestionSet(text);
  const p = activePage();
  const qs = p?.analysis?.questions || [];
  const selected = qs.filter(q => q._checked !== false).map(q => `第${q.index}题`);
  const parts = [];
  if (text) parts.push(text);
  if (set.size) parts.push(`题号：第 ${[...set].sort((a, b) => a - b).join('、')} 题`);
  else if (selected.length) parts.push(`当前勾选：${selected.join('、')}`);
  $('#reqPreview').textContent = parts.length ? parts.join('\n') : '还没有作答要求';
}

function reqScaleFor(text) {
  return /字大|大一点|写大|放大/.test(text) ? 1.12
    : (/字小|小一点|写小/.test(text) ? 0.9 : 1);
}

function applyRequirement(notify) {
  const text = ($('#reqInput').value || '').trim();
  state.requirement = text;
  state.reqScale = reqScaleFor(text);

  const set = parseQuestionSet(text);
  const p = activePage();
  if (p?.analysis && set.size) {
    p.analysis.questions.forEach(q => { q._checked = set.has(Number(q.index)); });
    renderQuestions();
    renderBoxes();
  }
  updateReqPreview();
  saveConfig(true);
  if (notify) toast('作答要求已套用，生成时会写进提示词', 'ok');
}

/* ---------------- 设置 ---------------- */
function readSettings() {
  state.vision.base = $('#visionBase').value.trim() || DEFAULTS.vision.base;
  state.vision.model = $('#visionModel').value.trim() || DEFAULTS.vision.model;
  state.vision.key = $('#visionKey').value.trim();
  state.llm.base = $('#llmBase').value.trim() || DEFAULTS.llm.base;
  state.llm.model = $('#llmModel').value.trim() || DEFAULTS.llm.model;
  state.llm.key = $('#llmKey').value.trim();
}

async function handleTest() {
  readSettings();
  saveConfig(true);
  updateStatusChip();
  setBusy(true, '正在测试两个 Key…');
  log('测试连接：识图模型 / 解题模型');
  try {
    const res = await fetchWithTimeout('/api/test-keys', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ vision: state.vision, llm: state.llm })
    }, 60000);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
    let bad = 0;
    for (const [name, r] of [['识图', data.vision], ['解题', data.llm]]) {
      if (r?.ok) log(`${name}模型连接成功${r.count ? `（可用模型 ${r.count} 个）` : ''}`, 'ok');
      else {
        bad++;
        log(`${name}模型连接失败：${r?.error || '未知错误'}`, 'err');
      }
    }
    toast(bad ? `${bad} 个 Key 测试失败` : '两个 Key 都可用', bad ? 'error' : 'ok');
  } catch (e) {
    log('测试失败：' + e.message, 'err');
    toast(e.message, 'error');
  } finally {
    setBusy(false);
  }
}

/* ---------------- 事件绑定 ---------------- */
function bindEvents() {
  // 标签页
  $$('.tab').forEach(btn => btn.addEventListener('click', () => switchTab(btn.dataset.tab)));
  $('#statusChip').addEventListener('click', () => switchTab('settings'));

  // 上传
  const pickPage = () => $('#fileInput').click();
  $('#btnUpload').addEventListener('click', pickPage);
  $('#btnUploadBottom').addEventListener('click', pickPage);
  $('#fileInput').addEventListener('change', e => {
    addFiles(e.target.files);
    e.target.value = '';
  });
  $('#dropzone').addEventListener('click', e => {
    if (state.busy) return;
    if (e.target.closest('button')) return;
    $('#fileInput').click();
  });

  const wrap = $('#canvasWrap');
  ['dragenter', 'dragover'].forEach(ev => wrap.addEventListener(ev, e => {
    e.preventDefault();
    $('#dropzone').classList.add('dragover');
  }));
  ['dragleave', 'drop'].forEach(ev => wrap.addEventListener(ev, e => {
    e.preventDefault();
    $('#dropzone').classList.remove('dragover');
  }));
  wrap.addEventListener('drop', e => {
    if (e.dataTransfer?.files?.length) addFiles(e.dataTransfer.files);
  });

  document.addEventListener('paste', e => {
    const items = Array.from(e.clipboardData?.items || []);
    const files = items.filter(it => it.type.startsWith('image/')).map(it => it.getAsFile()).filter(Boolean);
    if (files.length) {
      e.preventDefault();
      addFiles(files);
    }
  });

  // 墨色
  $$('#inkSwatches button').forEach(btn => btn.addEventListener('click', () => {
    state.inkTone = btn.dataset.ink;
    syncInkUI();
    saveConfig(true);
  }));

  $$('#outputSeg button').forEach(btn => btn.addEventListener('click', () => {
    state.output = btn.dataset.output;
    $$('#outputSeg button').forEach(b => b.classList.toggle('active', b === btn));
    updateHint();
  }));

  $$('#traceSeg button').forEach(btn => btn.addEventListener('click', () => {
    state.traceLevel = btn.dataset.trace;
    syncTraceUI();
    saveConfig(true);
  }));

  // 作答要求
  $('#btnApplyReq').addEventListener('click', () => applyRequirement(true));
  $('#btnClearReq').addEventListener('click', () => {
    $('#reqInput').value = '';
    state.requirement = '';
    state.reqScale = 1;
    const p = activePage();
    if (p?.analysis) {
      p.analysis.questions.forEach(q => { q._checked = true; });
      renderQuestions();
      renderBoxes();
    }
    updateReqPreview();
    saveConfig(true);
  });
  $('#reqInput').addEventListener('input', () => {
    state.requirement = $('#reqInput').value;
    state.reqScale = reqScaleFor(state.requirement);
    updateReqPreview();
    saveConfig(true);
  });
  on('#btnApplyRange', 'click', () => {
    const p = activePage();
    const set = parseQuestionSet($('#qRangeInput')?.value || '');
    if (!p?.analysis) return toast('请先识图分析', 'warn');
    if (!set.size) return toast('没看懂题号，例如填：1-3 或 第2、5题', 'warn');
    p.analysis.questions.forEach(q => { q._checked = set.has(Number(q.index)); });
    renderQuestions();
    renderBoxes();
    updateReqPreview();
    toast(`已选择 ${[...set].join('、')} 题`, 'ok');
  });

  // 人类痕迹滑杆
  $('#accRange').addEventListener('input', e => {
    state.human.accuracy = Number(e.target.value);
    $('#accNum').textContent = state.human.accuracy + '%';
    saveConfig(true);
  });
  $('#corRange').addEventListener('input', e => {
    state.human.correction = Number(e.target.value);
    $('#corNum').textContent = state.human.correction + '%';
    saveConfig(true);
  });
  $('#neatRange').addEventListener('input', e => {
    state.human.neatness = Number(e.target.value);
    $('#neatNum').textContent = state.human.neatness + '%';
    saveConfig(true);
  });
  $('#sizeRange').addEventListener('input', e => {
    state.human.size = Number(e.target.value);
    $('#sizeNum').textContent = state.human.size + '%';
    saveConfig(true);
  });

  // 视图切换
  $$('#viewSeg button').forEach(btn => btn.addEventListener('click', () => {
    const p = activePage();
    if (!p) return;
    if (btn.dataset.view === 'result' && !p.result) {
      toast('还没有生成作答图', 'warn');
      return;
    }
    if (btn.dataset.view === 'grid' && !p.gridUrl) {
      toast('还没有生成网格图', 'warn');
      return;
    }
    if (btn.dataset.view === 'calib' && !p.calib) {
      toast('这次没有校准图（只有本地字迹引擎会生成）', 'warn');
      return;
    }
    p.showGrid = btn.dataset.view === 'grid';
    p.showResult = btn.dataset.view === 'result';
    p.showCalib = btn.dataset.view === 'calib';
    renderStage();
    updateButtons();
  }));

  // 扫描件 / 文档页查看
  function openViewer(kind) {
    const p = activePage();
    const url = p && (kind === 'doc' ? p.agentDoc : p.agentScan);
    if (!url) {
      toast(kind === 'doc'
        ? '这一页还没有文档页，先点「仅识图分析」'
        : '这一页还没有扫描件，先点「仅识图分析」');
      return;
    }
    $('#viewerTitle').textContent = kind === 'doc' ? '扫描文档' : '扫描件';
    $('#scanImg').src = url;
    $('#scanOpen').href = url;
    $('#scanMeta').textContent = `第 ${state.active + 1} 页 · ${p.w}×${p.h} · 与扫描件同坐标`;
    $('#scanModal').hidden = false;
  }
  on('#btnScanView', 'click', () => openViewer('scan'));
  on('#btnDocView', 'click', () => openViewer('doc'));
  on('#scanClose', 'click', () => { $('#scanModal').hidden = true; });
  on('#scanModal', 'click', e => {
    if (e.target && e.target.id === 'scanModal') $('#scanModal').hidden = true;
  });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && !$('#scanModal').hidden) $('#scanModal').hidden = true;
  });

  // 动作
  $('#btnAnalyze').addEventListener('click', handleAnalyze);
  $('#btnSolve').addEventListener('click', handleSolve);
  $('#btnAuto').addEventListener('click', handleAuto);
  $('#btnAgentStop').addEventListener('click', () => {
    if (agentAbort) agentAbort.abort();
  });
  on('#btnDownload', 'click', handleDownload);
  on('#btnAnsEdit', 'click', () => openAnsEditor(activePage()));
  on('#btnCompose', 'click', handleCompose);

  // 作答结果编辑器
  on('#ansCompose', 'click', composeFromEditor);
  on('#ansExport', 'click', () => {
    const p = activePage();
    if (!p || !p.result) return toast('还没有可导出的结果', 'warn');
    const name = String(p.name || '作答').replace(/\.[^.]+$/, '') + '_作答.png';
    downloadUrl(p.result, name);
  });
  on('#ansClose', 'click', () => { $('#ansEditor').hidden = true; });
  on('#ansSloppy', 'input', e => {
    $('#ansSloppyNum').textContent = e.target.value + '%';
  });
  on('#ansInkSeg', 'click', e => {
    const b = e.target.closest('button');
    if (!b) return;
    $$('#ansInkSeg button').forEach(x => x.classList.toggle('active', x === b));
    state.ansInk = b.dataset.ink || 'normal';
  });
  window.addEventListener('resize', () => syncAnsLayer());
  $('#btnExportImage').addEventListener('click', handleDownload);
  on('#btnDownloadAll', 'click', handleDownloadAll);

  $('#btnClear').addEventListener('click', () => {
    if (!state.pages.length) return;
    if (!confirm('清空所有已上传的页面和结果？')) return;
    state.pages = [];
    state.active = 0;
    state.selectedQ = -1;
    log('已清空全部页面');
    renderAll();
  });

  // 无 Key：直接出图（界面已移除，保留兼容绑定）
  on('#btnCopyPrompt', 'click', handleCopyPrompt);
  on('#btnImportResult', 'click', () => $('#resultFileInput')?.click());
  on('#resultFileInput', 'change', e => {
    handleImportResult(e.target.files?.[0]);
    e.target.value = '';
  });

  // 网格坐标落笔（界面已移除，保留兼容绑定）
  on('#btnGrid', 'click', handleGrid);
  on('#btnGridPrompt', 'click', handleGridPrompt);
  on('#btnGridImport', 'click', handleGridImport);
  on('#btnGridRender', 'click', handleGridRender);

  // 学习你的字体
  $('#btnLearnUpload').addEventListener('click', () => $('#fontFileInput').click());
  $('#fontFileInput').addEventListener('change', e => {
    handleFontFiles(e.target.files);
    e.target.value = '';
  });
  $('#btnLearnApply').addEventListener('click', handleLearnFont);
  $('#btnLearnClear').addEventListener('click', clearFontProfile);

  // 设置
  $$('[data-toggle-key]').forEach(btn => btn.addEventListener('click', () => {
    const input = $('#' + btn.dataset.toggleKey);
    if (!input) return;
    const show = input.type === 'password';
    input.type = show ? 'text' : 'password';
    btn.textContent = show ? '隐藏' : '显示';
  }));
  $('#btnSave').addEventListener('click', () => {
    readSettings();
    saveConfig();
    syncSettingsUI();
    log('配置已保存', 'ok');
  });
  $('#btnTest').addEventListener('click', handleTest);
  $('#btnClearKeys').addEventListener('click', () => {
    state.vision.key = '';
    state.llm.key = '';
    saveConfig(true);
    syncSettingsUI();
    log('已清空两个 API Key', 'warn');
    toast('已清空 Key', 'ok');
  });

  ['#visionBase', '#visionModel', '#visionKey', '#llmBase', '#llmModel', '#llmKey'].forEach(sel => {
    $(sel).addEventListener('change', () => {
      readSettings();
      saveConfig(true);
      updateStatusChip();
    });
  });

  // 窗口尺寸变化时保持落笔框对齐
  window.addEventListener('resize', () => {
    applyBoxPositions();
  });
}

/* ---------------- 启动 ---------------- */
let booted = false;

function init() {
  if (booted) return;
  booted = true;

  loadConfig();
  syncSettingsUI();
  syncHumanUI();
  syncTraceUI();
  bindEvents();
  $('#reqInput').value = state.requirement || '';
  state.reqScale = reqScaleFor(state.requirement || '');
  renderFontThumbs();
  updateReqPreview();
  renderAll();

  if (!state.vision.key.trim() || !state.llm.key.trim()) {
    log('还缺模型 Key：请到「设置配置」分别填识图模型和解题模型。', 'warn');
  } else {
    log(`已加载模型：${state.vision.model} + ${state.llm.model}`, 'ok');
  }
  log('就绪：底部选图，左栏定要求，右栏生成。');
  checkRenderService();
}

async function checkRenderService() {
  try {
    const r = await fetch('/api/render', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: '{}'
    });
    if (r.status === 400) {
      log('本地落笔服务：在线', 'ok');
      return true;
    }
    log(`本地落笔服务：不可用（HTTP ${r.status}），请用「启动.bat」启动后刷新`, 'warn');
  } catch (e) {
    log('本地落笔服务：不可用（' + e.message + '），请用「启动.bat」启动后刷新', 'warn');
  }
  return false;
}

document.addEventListener('DOMContentLoaded', init);
if (document.readyState !== 'loading') init();
