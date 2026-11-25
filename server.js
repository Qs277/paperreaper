'use strict';

/* 零依赖本地静态服务器：双击「启动.bat」时自动调用 */

const http = require('http');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { execFile, spawn } = require('child_process');

const ROOT = __dirname;
const PORT = Number(process.argv[2] || 8765);
const PY = process.env.HANDWRITING_PY || 'python';
const TOOL_DIR = process.env.HANDWRITING_TOOL_DIR || path.join(ROOT, 'engine');

// 引擎默认用仓库自带的 engine/，也可用环境变量指向别处
function pickScript(envVal, name) {
  if (envVal) return envVal;
  return path.join(TOOL_DIR, name);
}
const SCRIPT = pickScript(process.env.HANDWRITING_SCRIPT, 'auto_render.py');
const AGENT_SCRIPT = pickScript(process.env.HANDWRITING_AGENT, 'agent_runtime.py');
const RUNS_DIR = path.join(ROOT, '.agent_runs');

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.js': 'application/javascript; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.webp': 'image/webp',
  '.svg': 'image/svg+xml',
  '.ico': 'image/x-icon',
  '.txt': 'text/plain; charset=utf-8'
};

function openBrowser(url) {
  const { exec } = require('child_process');
  try {
    if (process.platform === 'win32') {
      exec(`start "" "${url}"`);
    } else if (process.platform === 'darwin') {
      exec(`open "${url}"`);
    } else {
      exec(`xdg-open "${url}"`);
    }
  } catch {
    /* 打不开浏览器时由用户手动访问 */
  }
}

function readBody(req, limit = 40 * 1024 * 1024) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    req.on('data', c => {
      size += c.length;
      if (size > limit) {
        reject(new Error('图片太大，请压缩后再试'));
        req.destroy();
        return;
      }
      chunks.push(c);
    });
    req.on('end', () => resolve(Buffer.concat(chunks)));
    req.on('error', reject);
  });
}

function sendJson(res, code, obj) {
  const body = Buffer.from(JSON.stringify(obj), 'utf8');
  res.writeHead(code, {
    'Content-Type': 'application/json; charset=utf-8',
    'Cache-Control': 'no-store'
  });
  res.end(body);
}

/* 把识图结果交给本地字迹引擎（auto_render.py）落笔 */
async function handleRender(req, res) {
  let dir = null;
  try {
    const raw = await readBody(req);
    const payload = JSON.parse(raw.toString('utf8'));
    const m = String(payload.image || '').match(/^data:image\/(\w+);base64,(.+)$/);
    if (!m) return sendJson(res, 400, { ok: false, error: '缺少有效的图片数据' });

    const commands = payload.commands;
    if (!commands || !Array.isArray(commands.items) || !commands.items.length) {
      return sendJson(res, 400, { ok: false, error: '没有可落笔的答案' });
    }

    dir = fs.mkdtempSync(path.join(os.tmpdir(), 'exam-render-'));
    const ext = m[1] === 'jpeg' ? 'jpg' : m[1];
    const inPath = path.join(dir, 'paper.' + ext);
    const cmdPath = path.join(dir, 'commands.json');
    const outPath = path.join(dir, 'out.png');
    const calibPath = path.join(dir, 'calib.png');
    fs.writeFileSync(inPath, Buffer.from(m[2], 'base64'));
    fs.writeFileSync(cmdPath, JSON.stringify(commands, null, 2), 'utf8');

    const stdout = await new Promise((resolve, reject) => {
      execFile(
        PY,
        [SCRIPT, inPath, cmdPath, outPath, '--calib', calibPath],
        { timeout: 120000, maxBuffer: 8 * 1024 * 1024, windowsHide: true },
        (err, so, se) => {
          if (err) return reject(new Error(String(se || err.message || '').slice(0, 300)));
          resolve(so);
        }
      );
    });

    const outB64 = fs.readFileSync(outPath).toString('base64');
    const calibB64 = fs.existsSync(calibPath)
      ? fs.readFileSync(calibPath).toString('base64')
      : '';
    let report = [];
    try {
      report = JSON.parse(stdout).items || [];
    } catch {
      report = [];
    }
    sendJson(res, 200, {
      ok: true,
      image: 'data:image/png;base64,' + outB64,
      calib: calibB64 ? 'data:image/png;base64,' + calibB64 : '',
      report
    });
  } catch (e) {
    const msg = e.message || '落笔失败';
    try {
      fs.appendFileSync(
        path.join(ROOT, 'render_error.log'),
        `[${new Date().toISOString()}] ${msg}\n`
      );
    } catch {
      /* 日志写不进去也不影响返回 */
    }
    sendJson(res, 500, { ok: false, error: msg });
  } finally {
    if (dir) fs.rm(dir, { recursive: true, force: true }, () => {});
  }
}

/* 把整张卷子交给本地 Agent（agent_runtime.py），用 SSE 实时回传日志和结果 */
function sseWrite(res, obj) {
  if (res.writableEnded) return;
  res.write('data: ' + JSON.stringify(obj) + '\n\n');
}

function pickPng(dir, re, exclude) {
  const files = fs.readdirSync(dir).filter(f => /\.png$/i.test(f));
  const hit = files.filter(f => re.test(f) && !(exclude && exclude.test(f)));
  const list = (hit.length ? hit : []).map(f => {
    const full = path.join(dir, f);
    return { f, t: fs.statSync(full).mtimeMs };
  });
  list.sort((a, b) => b.t - a.t);
  return list[0] ? list[0].f : '';
}

/* 引擎没写 layout.json 时的兜底：直接用 commands.json 的锁定框生成可编辑布局 */
function buildLayoutFromCommands(runDir) {
  const cp = path.join(runDir, 'commands.json');
  if (!fs.existsSync(cp)) return null;
  let data = null;
  try { data = JSON.parse(fs.readFileSync(cp, 'utf8')); } catch { return null; }
  const items = [];
  for (const c of data.items || []) {
    let box = c.lock;
    if (!Array.isArray(box) || box.length !== 4) {
      if (c.type === 'work' && typeof c.x === 'number') {
        box = [c.x, c.y, Math.min(1, c.x + (c.w || 0.8)),
          Math.min(1, c.y + 0.18)];
      } else {
        const y = Number(c.y || 0.5);
        box = [0.55, y - 0.014, 0.62, y + 0.014];
      }
    }
    items.push({
      q: c.q, type: c.type, box,
      text: c.text, num: c.num, den: c.den, lines: c.lines || null,
      size: c.size || null
    });
  }
  return items.length ? {
    style: data.style || 'pen',
    profile: data.profile || null,
    items,
    traces: Array.isArray(data.traces) ? data.traces : []
  } : null;
}

async function handleAgent(req, res) {
  let runDir = null;
  let child = null;
  let finished = false;
  let killed = false;

  try {
    const raw = await readBody(req);
    const payload = JSON.parse(raw.toString('utf8'));
    const m = String(payload.image || '').match(/^data:image\/(\w+);base64,(.+)$/);
    if (!m) return sendJson(res, 400, { ok: false, error: '缺少有效的图片数据' });

    const ext = m[1] === 'jpeg' ? 'jpg' : m[1];
    const base = String(payload.filename || 'paper')
      .replace(/\.[^.]+$/, '')
      .replace(/[\\/:*?"<>|\s]+/g, '_')
      .slice(0, 60) || 'paper';
    const id = Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 8);
    runDir = path.join(RUNS_DIR, id);
    fs.mkdirSync(runDir, { recursive: true });

    const imgPath = path.join(runDir, base + '.' + ext);
    fs.writeFileSync(imgPath, Buffer.from(m[2], 'base64'));
    for (const src of [path.join(ROOT, 'AGENTS.md'), path.join(TOOL_DIR, 'AGENTS.md')]) {
      if (fs.existsSync(src)) {
        fs.copyFileSync(src, path.join(runDir, 'AGENTS.md'));
        break;
      }
    }

    const task = String(payload.task || '识别并作答这张试卷，严格按 AGENTS.md 流程执行。');
    const stage = ['scan', 'render', 'compose'].includes(payload.stage) ? payload.stage : 'full';
    const trace = ['none', 'less', 'more'].includes(payload.trace) ? payload.trace : 'more';
    if (stage === 'render' && payload.plan) {
      fs.writeFileSync(path.join(runDir, 'plan.json'),
        JSON.stringify({ ...payload.plan, profile: payload.profile || null, trace }), 'utf8');
    }
    if (stage === 'compose') {
      if (!payload.layout) return sendJson(res, 400, { ok: false, error: '缺少确认后的布局' });
      let layout = payload.layout;
      // 沿用上一轮导出的扫描件做底图，保证和用户调框时看到的是同一张
      const prev = String(payload.dir || '');
      const prevDir = prev ? path.resolve(prev) : '';
      const prevScan = prevDir ? path.join(prevDir, 'scan.png') : '';
      // 老布局可能只带 items（例如引擎没写 layout.json 时的兜底），痕迹从上一轮命令里补回
      if (!Array.isArray(layout.traces) || !layout.traces.length) {
        const prevCmd = prevDir ? path.join(prevDir, 'commands.json') : '';
        if (prevCmd.startsWith(RUNS_DIR + path.sep) && fs.existsSync(prevCmd)) {
          try {
            const pc = JSON.parse(fs.readFileSync(prevCmd, 'utf8'));
            if (Array.isArray(pc.traces) && pc.traces.length) {
              layout = { ...layout, traces: pc.traces };
            }
          } catch { /* 旧命令损坏就按原布局 */ }
        }
      }
      fs.writeFileSync(path.join(runDir, 'layout.json'),
        JSON.stringify(layout), 'utf8');
      if (prevScan && prevScan.startsWith(RUNS_DIR + path.sep)
          && fs.existsSync(prevScan)) {
        fs.copyFileSync(prevScan, path.join(runDir, 'scan.png'));
      }
    }

    res.writeHead(200, {
      'Content-Type': 'text/event-stream; charset=utf-8',
      'Cache-Control': 'no-cache, no-transform',
      Connection: 'keep-alive',
      'X-Accel-Buffering': 'no'
    });
    if (typeof res.flushHeaders === 'function') res.flushHeaders();

    sseWrite(res, { type: 'log', text: '智能体启动：' + path.basename(imgPath) });

    const env = Object.assign({}, process.env, {
      PYTHONIOENCODING: 'utf-8',
      PYTHONUNBUFFERED: '1'
    });
    // 前端传进来的两套 Key 只进子进程环境，不落盘
    const keys = payload.keys || {};
    const v = keys.vision || {};
    const l = keys.llm || {};
    if (v.key) {
      env.VISION_API_KEY = String(v.key);
      env.VISION_BASE_URL = String(v.base || '');
      env.VISION_MODEL = String(v.model || '');
    }
    if (l.key) {
      env.LLM_API_KEY = String(l.key);
      env.LLM_BASE_URL = String(l.base || '');
      env.LLM_MODEL = String(l.model || '');
    }
    const args = ['-u', AGENT_SCRIPT, '--image', imgPath, '--task', task, '--workspace', runDir];
    if (stage !== 'full') args.push('--stage', stage);
    args.push('--trace', trace);
    child = spawn(PY, args, { cwd: runDir, env, windowsHide: true });

    const tail = [];
    const errTail = () => {
      const hit = tail.filter(l =>
        /\[vision\]|\[llm\]|\[scan\]|\[render\]|HTTP|Error|Traceback/i.test(l));
      const src = hit.length ? hit.slice(-6) : tail.slice(-8);
      return src.join(' / ') || '无输出';
    };
    let buf = '';
    const onData = chunk => {
      buf += chunk.toString('utf8');
      const lines = buf.split(/\r?\n/);
      buf = lines.pop();
      for (const line of lines) {
        if (!line.trim()) continue;
        tail.push(line);
        if (tail.length > 40) tail.shift();
        sseWrite(res, { type: 'log', text: line });
      }
    };
    child.stdout.on('data', onData);
    child.stderr.on('data', onData);
    child.on('error', err => {
      if (finished) return;
      finished = true;
      sseWrite(res, { type: 'error', text: '启动智能体失败：' + err.message });
      res.end();
    });

    const timer = setTimeout(() => {
      killed = true;
      try { child.kill(); } catch { /* 已退出 */ }
    }, 10 * 60 * 1000);

    res.on('close', () => {
      if (finished) return;
      finished = true;
      clearTimeout(timer);
      try { child.kill(); } catch { /* 已退出 */ }
    });

    child.on('close', code => {
      clearTimeout(timer);
      if (finished) return;
      finished = true;
      if (res.writableEnded) return;
      if (buf.trim()) sseWrite(res, { type: 'log', text: buf.trim() });
      try {
        const rel = f => '/.agent_runs/' + encodeURIComponent(id) + '/' + encodeURIComponent(f);
        if (stage === 'compose') {
          const finalFile = pickPng(runDir, /定稿/, /校准/);
          const calibFile = pickPng(runDir, /定稿_校准|定稿校准/);
          if (!finalFile) {
            sseWrite(res, {
              type: 'error',
              text: '定稿失败（退出码 ' + code + '）：'
                + (errTail() || '无输出')
            });
          } else {
            sseWrite(res, {
              type: 'done',
              result: rel(finalFile),
              calib: calibFile ? rel(calibFile) : '',
              dir: runDir,
              code
            });
          }
        } else if (stage === 'scan') {
          const planPath = path.join(runDir, 'plan.json');
          const boxFile = pickPng(runDir, /框选/);
          const scanFile = fs.existsSync(path.join(runDir, 'scan.png')) ? 'scan.png' : '';
          const docFile = fs.existsSync(path.join(runDir, 'document.png')) ? 'document.png' : '';
          if (!fs.existsSync(planPath)) {
            sseWrite(res, {
              type: 'error',
              text: killed ? '扫描超时，已中止'
                : ('扫描没有产出位置计划（退出码 ' + code + '）：' + errTail())
            });
          } else {
            sseWrite(res, {
              type: 'done',
              plan: JSON.parse(fs.readFileSync(planPath, 'utf8')),
              preview: boxFile ? rel(boxFile) : '',
              scan: scanFile ? rel(scanFile) : '',
              doc: docFile ? rel(docFile) : '',
              dir: runDir,
              code
            });
          }
        } else {
          const resultFile = pickPng(runDir, /作答/, /校准/);
          const calibFile = pickPng(runDir, /校准/);
          if (!resultFile) {
            sseWrite(res, {
              type: 'error',
              text: killed
                ? '运行超时，已中止（10 分钟）'
                : ('智能体没有产出作答图（退出码 ' + code + '）：' + errTail())
            });
          } else {
            const layoutPath = path.join(runDir, 'layout.json');
            let layout = null;
            if (fs.existsSync(layoutPath)) {
              try { layout = JSON.parse(fs.readFileSync(layoutPath, 'utf8')); } catch { layout = null; }
            }
            if (!layout) layout = buildLayoutFromCommands(runDir);
            sseWrite(res, {
              type: 'done',
              result: rel(resultFile),
              calib: calibFile ? rel(calibFile) : '',
              layout,
              dir: runDir,
              code
            });
          }
        }
      } catch (e) {
        sseWrite(res, { type: 'error', text: '读取结果失败：' + e.message });
      }
      res.end();
    });
  } catch (e) {
    if (!res.headersSent) {
      sendJson(res, 500, { ok: false, error: e.message || '智能体启动失败' });
    } else {
      sseWrite(res, { type: 'error', text: e.message || '智能体启动失败' });
      res.end();
    }
  }
}

/* 测试两套 Key：请求各自的 /models，不记录、不回显 Key */
async function handleTestKeys(req, res) {
  try {
    const raw = await readBody(req);
    const p = JSON.parse(raw.toString('utf8') || '{}');
    const test = async cfg => {
      const key = String(cfg?.key || '').trim();
      if (!key) return { ok: false, error: '未填写 Key' };
      let base = String(cfg?.base || '').trim().replace(/\/+$/, '');
      if (!base) return { ok: false, error: '未填写 Base URL' };
      base = base.replace(/\/chat\/completions$/, '').replace(/\/+$/, '');
      if (!/\/v\d+(\.\d+)?$/.test(base)) base += '/v1';
      const ctrl = new AbortController();
      const timer = setTimeout(() => ctrl.abort(), 20000);
      try {
        const r = await fetch(base + '/models', {
          headers: { Authorization: 'Bearer ' + key },
          signal: ctrl.signal
        });
        const text = await r.text();
        let data = null;
        try { data = text ? JSON.parse(text) : null; } catch { data = null; }
        if (!r.ok) {
          const msg = data?.error?.message || data?.message || text.slice(0, 200) || ('HTTP ' + r.status);
          return { ok: false, error: msg };
        }
        const list = Array.isArray(data?.data) ? data.data : [];
        const model = String(cfg?.model || '').trim();
        const names = list.map(x => String(x.id || ''));
        return { ok: true, count: list.length, hasModel: model && names.length ? names.includes(model) : null };
      } catch (e) {
        return { ok: false, error: e.name === 'AbortError' ? '请求超时（20 秒）' : e.message };
      } finally {
        clearTimeout(timer);
      }
    };
    const [vision, llm] = await Promise.all([test(p.vision), test(p.llm)]);
    sendJson(res, 200, { ok: true, vision, llm });
  } catch (e) {
    sendJson(res, 500, { ok: false, error: e.message || '测试失败' });
  }
}

function start(port, attempts = 0) {
  const srv = http.createServer((req, res) => {
    let urlPath = '/';
    try {
      urlPath = decodeURIComponent((req.url || '/').split('?')[0]);
    } catch {
      urlPath = '/';
    }

    if (req.method === 'POST' && urlPath === '/api/render') {
      handleRender(req, res);
      return;
    }

    if (req.method === 'POST' && urlPath === '/api/agent') {
      handleAgent(req, res);
      return;
    }

    if (req.method === 'POST' && urlPath === '/api/test-keys') {
      handleTestKeys(req, res);
      return;
    }

    if (urlPath === '/') urlPath = '/index.html';

    const filePath = path.resolve(ROOT, '.' + urlPath);
    if (filePath !== ROOT && !filePath.startsWith(ROOT + path.sep)) {
      res.writeHead(403, { 'Content-Type': 'text/plain; charset=utf-8' });
      res.end('403 禁止访问');
      return;
    }

    fs.readFile(filePath, (err, data) => {
      if (err) {
        res.writeHead(404, { 'Content-Type': 'text/plain; charset=utf-8' });
        res.end('404 页面不存在');
        return;
      }
      res.writeHead(200, {
        'Content-Type': MIME[path.extname(filePath).toLowerCase()] || 'application/octet-stream',
        'Cache-Control': 'no-cache'
      });
      res.end(data);
    });
  });

  srv.once('error', err => {
    if (err.code === 'EADDRINUSE' && attempts < 20) {
      start(port + 1, attempts + 1);
      return;
    }
    console.error('本地服务启动失败：' + err.message);
    setTimeout(() => process.exit(1), 8000);
  });

  srv.listen(port, '127.0.0.1', () => {
    const url = `http://127.0.0.1:${port}`;
    console.log('');
    console.log('  PaperReaper 已启动');
    console.log(`  访问地址： ${url}`);
    console.log('  关闭此窗口即停止服务。');
    console.log('');
    openBrowser(url);
  });
}

start(PORT);
