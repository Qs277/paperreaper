# -*- coding: utf-8 -*-
"""API 版阅卷 Agent 运行时：把 Trae 里的工具流程复刻给任意多模态模型。

工具与 Trae 的对应关系：
  Read(图片)      -> view_image + 对话里直接带图
  Write/Edit      -> write_text
  Shell           -> run_command（白名单 + 工作目录限制）
  Read(文本)      -> read_text
  Glob/LS         -> list_files
  记忆 AGENTS.md  -> append_memory

用法（单模型，视觉模型自己调工具）：
  set LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
  set LLM_API_KEY=sk-xxxx
  set LLM_MODEL=qwen-vl-max-latest
  python agent_runtime.py --image 试卷.jpg --task "识别并作答这张卷子"

用法（双模型 B 方案：视觉看图 + DeepSeek 解题/调工具）：
  set VISION_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
  set VISION_API_KEY=sk-xxxx
  set VISION_MODEL=qwen-vl-max-latest
  set LLM_BASE_URL=https://api.deepseek.com/v1
  set LLM_API_KEY=sk-xxxx
  set LLM_MODEL=deepseek-chat

默认是受控快速模式（推荐）：
  python agent_runtime.py --image 试卷.jpg --workspace 输出目录
  流程：整页转写 -> 版面扫描 -> 两遍解题 + 交叉裁决 -> 边界复核 -> 固定落笔
  旧的多步工具循环保留在 --mode free。

自检（不调模型）：
  python agent_runtime.py --dry-run --image 试卷.jpg
"""
import argparse
import base64
import io
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import cv2
import numpy as np
import requests
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parent))
import align  # noqa: E402
import rectify  # noqa: E402

TOOL_DIR = Path(__file__).resolve().parent
DEFAULT_WORKSPACE = Path.cwd().resolve()
ALLOWED_ROOTS = [TOOL_DIR]


def _roots(workspace):
    return ALLOWED_ROOTS + [Path(workspace).resolve()]


def _safe_path(p, workspace):
    q = Path(p)
    q = (q if q.is_absolute() else Path(workspace) / q).resolve()
    if not any(q == r or r in q.parents for r in _roots(workspace)):
        raise ValueError(f'路径不在允许范围：{q}')
    return q


def _load_env():
    """从 engine/.env 读取配置，避免把 Key 写进对话或代码。"""
    p = TOOL_DIR / '.env'
    if not p.exists():
        return
    for line in p.read_text(encoding='utf-8', errors='replace').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        k, v = line.split('=', 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _image_data_url(path, max_side=1600):
    im = Image.open(path).convert('RGB')
    w, h = im.size
    if max(w, h) > max_side:
        k = max_side / max(w, h)
        im = im.resize((int(w * k), int(h * k)), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, format='JPEG', quality=92)
    b64 = base64.b64encode(buf.getvalue()).decode()
    return f'data:image/jpeg;base64,{b64}'


ALLOWED_CMDS = {'python', 'python.exe', 'dir', 'get-childitem', 'type',
                'get-content', 'copy', 'copy-item'}
BLOCK = ('rm -rf', 'del /f', 'del /s', 'rd /s', 'format ', 'shutdown',
         'remove-item -recurse', 'mkfs', 'diskpart', 'curl ', 'wget ',
         'invoke-webrequest', 'pip install')


def _check_command(cmd):
    low = cmd.strip().lower()
    if any(b in low for b in BLOCK):
        return '命令被安全策略拦截：包含危险/联网操作'
    head = low.split()[0] if low.split() else ''
    if head not in ALLOWED_CMDS:
        return f'命令不在白名单：{head}（只允许 python / dir / type / copy 等）'
    return None


def execute_tool(name, args, workspace, vision=None):
    """执行一个工具，返回 (文本结果, 可选图片路径)。"""
    try:
        if name == 'list_files':
            p = _safe_path(args.get('path', '.'), workspace)
            rows = []
            for x in sorted(p.iterdir()):
                rows.append(f"{'DIR ' if x.is_dir() else 'FILE'} {x.name} {x.stat().st_size}")
            return '\n'.join(rows[:200]) or '(空目录)', None

        if name == 'read_text':
            p = _safe_path(args['path'], workspace)
            n = int(args.get('max_lines', 200))
            lines = p.read_text(encoding='utf-8', errors='replace').splitlines()
            return '\n'.join(lines[:n]) + (f'\n...(共 {len(lines)} 行)' if len(lines) > n else ''), None

        if name == 'write_text':
            p = _safe_path(args['path'], workspace)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(args['content'], encoding='utf-8')
            return f'已写入 {p}（{len(args["content"])} 字符）', None

        if name == 'run_command':
            err = _check_command(args['command'])
            if err:
                return err, None
            r = subprocess.run(args['command'], shell=True, cwd=str(workspace),
                               capture_output=True, text=True, timeout=int(args.get('timeout', 180)))
            out = (r.stdout or '')[-6000:]
            er = (r.stderr or '')[-3000:]
            return f'returncode={r.returncode}\nstdout:\n{out}\nstderr:\n{er}', None

        if name == 'view_image':
            p = _safe_path(args['path'], workspace)
            if vision and vision.get('model'):
                q = args.get('question') or '描述这张图，重点检查文字、括号、横线和涂改位置。'
                return vision_describe(p, q, vision), None
            im = Image.open(p)
            return f'图片 {p}，尺寸 {im.size[0]}x{im.size[1]}', str(p)

        if name == 'crop_image':
            p = _safe_path(args['path'], workspace)
            im = Image.open(p).convert('RGB')
            W, H = im.size
            def _c(v, d):
                try:
                    return min(1.0, max(0.0, float(v)))
                except Exception:
                    return d
            x0, y0 = _c(args.get('x0'), 0.0), _c(args.get('y0'), 0.0)
            x1, y1 = _c(args.get('x1'), 1.0), _c(args.get('y1'), 1.0)
            box = (int(x0 * W), int(y0 * H),
                   max(int(x1 * W), int(x0 * W) + 10),
                   max(int(y1 * H), int(y0 * H) + 10))
            crop = im.crop(box)
            if max(crop.size) < 900:
                k = min(3.0, 1200.0 / max(1, max(crop.size)))
                crop = crop.resize((int(crop.width * k), int(crop.height * k)), Image.LANCZOS)
            out = workspace / '_crop.png'
            crop.save(out)
            if vision and vision.get('model'):
                q = args.get('question') or '看清这段卷面：题号、括号或横线位置、已经写了什么。'
                return vision_describe(str(out), q, vision), None
            return f'已裁剪到 {out}，尺寸 {crop.size[0]}x{crop.size[1]}', str(out)

        if name == 'render_paper':
            img = _safe_path(args['image'], workspace)
            cmd_p = _safe_path(args['commands'], workspace)
            out_p = _safe_path(args['output'], workspace)
            calib_p = out_p.with_name(out_p.stem + '_校准.png')
            script = str(TOOL_DIR / 'auto_render.py')
            report = None
            for attempt in range(3):
                r = subprocess.run(
                    [sys.executable, script, str(img), str(cmd_p), str(out_p), '--calib', str(calib_p)],
                    capture_output=True, text=True, encoding='utf-8', timeout=300)
                try:
                    report = json.loads(r.stdout)
                except Exception:
                    return ('渲染失败：\n' + ((r.stdout or '') + (r.stderr or ''))[-2000:]), None
                items = report.get('items', [])
                bad = [x for x in items if not x.get('ok')]
                if not bad:
                    break
                if attempt == 2:
                    break
                try:
                    data = json.loads(cmd_p.read_text(encoding='utf-8'))
                except Exception:
                    break
                drift_map = {x['q']: x.get('drift', [0, 0]) for x in bad}
                changed = False
                for it in data.get('items', []):
                    d = drift_map.get(it.get('q'))
                    if d and it.get('type') in ('choice', 'fill', 'fraction'):
                        dy = float(d[1])
                        if abs(dy) > 0.0008:
                            it['y'] = round(min(0.98, max(0.02,
                                             float(it.get('y', 0.5)) + dy)), 4)
                            changed = True
                if not changed:
                    break
                cmd_p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
            items = report.get('items', [])
            lines = [f"off_y={report.get('off_y')}"]
            for x in items:
                lines.append(f"q{x['q']} ok={str(x.get('ok')).lower()} "
                             f"det={str(x.get('detected')).lower()} drift={x.get('drift')}")
            bad = [x['q'] for x in items if not x.get('ok')]
            lines.append('全部通过' if not bad else '仍有失败: ' + ', '.join(f'q{q}' for q in bad))
            lines.append(f'结果图: {out_p}')
            lines.append(f'校准图: {calib_p}')
            return '\n'.join(lines), None

        if name == 'append_memory':
            p = _safe_path('AGENTS.md', workspace)
            with p.open('a', encoding='utf-8') as f:
                f.write('\n- ' + args['note'].strip() + '\n')
            return f'已追加到 {p}', None

        return f'未知工具：{name}', None
    except Exception as e:
        return f'工具执行失败：{type(e).__name__}: {e}', None


TOOLS = [
    {'type': 'function', 'function': {
        'name': 'list_files', 'description': '列出目录内容',
        'parameters': {'type': 'object', 'properties': {'path': {'type': 'string'}}}}},
    {'type': 'function', 'function': {
        'name': 'read_text', 'description': '读取文本文件（默认前 200 行）',
        'parameters': {'type': 'object', 'properties': {
            'path': {'type': 'string'}, 'max_lines': {'type': 'integer'}},
            'required': ['path']}}},
    {'type': 'function', 'function': {
        'name': 'write_text', 'description': '写入文本文件，自动建目录',
        'parameters': {'type': 'object', 'properties': {
            'path': {'type': 'string'}, 'content': {'type': 'string'}},
            'required': ['path', 'content']}}},
    {'type': 'function', 'function': {
        'name': 'run_command', 'description': '运行本地命令（python/dir/type 等白名单）',
        'parameters': {'type': 'object', 'properties': {
            'command': {'type': 'string'}, 'timeout': {'type': 'integer'}},
            'required': ['command']}}},
    {'type': 'function', 'function': {
        'name': 'view_image', 'description': '打开图片；双模型模式下由视觉模型返回画面描述',
        'parameters': {'type': 'object', 'properties': {
            'path': {'type': 'string'},
            'question': {'type': 'string', 'description': '想让视觉模型重点看什么'}},
            'required': ['path']}}},
    {'type': 'function', 'function': {
        'name': 'append_memory', 'description': '把本次经验追加到 AGENTS.md',
        'parameters': {'type': 'object', 'properties': {
            'note': {'type': 'string'}}, 'required': ['note']}}},
    {'type': 'function', 'function': {
        'name': 'crop_image', 'description': '裁剪图片局部并放大给视觉模型看（坐标 0~1），用于看不清的题目或校准图',
        'parameters': {'type': 'object', 'properties': {
            'path': {'type': 'string'},
            'x0': {'type': 'number'}, 'y0': {'type': 'number'},
            'x1': {'type': 'number'}, 'y1': {'type': 'number'},
            'question': {'type': 'string', 'description': '想让视觉模型重点看什么'}},
            'required': ['path', 'x0', 'y0', 'x1', 'y1']}}},
    {'type': 'function', 'function': {
        'name': 'render_paper',
        'description': '落笔主工具：运行本地引擎、自动按 drift 修正失败项、返回逐题验收报告和校准图路径',
        'parameters': {'type': 'object', 'properties': {
            'image': {'type': 'string'}, 'commands': {'type': 'string'},
            'output': {'type': 'string'}},
            'required': ['image', 'commands', 'output']}}},
]

VISION_PROMPT = """识别这张试卷，只做忠实转写，不要解题、不要写过程说明。直接输出一个 JSON（不要 markdown 代码块、不要多余文字）：
{"items":[{"q":1,"type":"choice","text":"题干转写","options":"A)… B)… C)… D)…","y":0.113}]}
- type 只能是 choice / fill / fraction / work。
- y 是“该题作答处（括号或横线）所在行”的相对高度，0~1 小数，只求大致（误差 0.01 内最好）。
- 填空题写成 fill 并给出 y（不需要 x，工具会自己找横线）。
- 大题写成 work 并给出题号与起始行 y。
- 公式用 ^2 表示平方，* 表示乘号，不要用 LaTeX。
- 必须覆盖整页所有题目，不要只写前几题。"""

SYSTEM = """你是“阅卷作答”Agent，工作方式必须和 AGENTS.md 完全一致。

核心分工：
- 你负责语义：识题、解题、判断题型、决定每个空写什么，并给“第几题/答案/大概哪一行 y”。
- 本地工具负责机械：找括号/横线、缩字号、贴线落笔、回检。
- 绝对不要自己编造像素坐标，也不要跳过校准图。

标准流程（按顺序，不要跳步）：
1. 先看用户给的试卷图（识图结果已在对话里），识题并解题。
2. 直接用 write_text 写最小 commands.json：{style, fix_rate?, items:[{q,type,text/num/den/lines,y}]}。
3. 调用 render_paper（image=试卷原图, commands=commands.json, output=作答图路径）。
   不要用 run_command 去跑 auto_render.py。
4. 读 render_paper 返回的逐题报告：只有出现“全部通过”才算完成；
   失败项工具会自动按 drift 修正并重试。
5. 若仍有 ok:false：用 crop_image 裁出该题区域（上下各留 0.03）看真实括号/横线，
   再改 y/x/size 后重新 render_paper。不要用 view_image 看整页去猜。
6. 全部通过后，用 append_memory 记录本卷参数或新踩的坑。
7. 最后用一段话汇报：生成了什么、几题 ok、哪里有涂改。

效率与纪律：
- 不要阅读 auto_render.py / api.py / align.py 的源码，也不要用 python 去测字体宽度；这些是工具内部的事。
- 同一件事只能查一次；不要重复执行相同命令。
- 拿到识图结果后，最多 1~2 步就要写出 commands.json 并开跑。
- y 用识图结果里给出的相对行位置，误差 ±0.01 以内即可，工具会自己找括号/横线。
- 最终验收只看工具 JSON 里的 item.ok：全部 ok:true 就是通过，立刻停止，不要再改 commands.json、不要再重跑。
- 校准图的视觉复查只做粗筛；视觉模型说“红十字没居中 / 有小白边”不算失败，以 ok 字段为准。只有出现 ok:false 才调整。
- fill / fraction 不需要写 x；工具会自己选该行最右一条横线。长公式也走 fill，会自动向右延伸，不要改成 work。
- 只有真正的大题（计算/综合/解答/证明）才用 type:"work"。

解题纪律：
- 逐题核对问法：问“错误的是”要逐项验证并尽量举反例；问“取值范围”必须写不等式范围（如 α≥3），不能用单点代替。
- 用反例时必须先确认该反例满足所否定命题的前提（例如“lim 存在”的前提是否真的成立，注意双侧极限）；前提不成立的反例无效。
- 选择题至少验证两个选项再定答案；填空题检查极限存在条件与定义域。
- 写完 commands.json 后，在脑中把每题答案再验算一遍，确认无误再运行。
- 用户没明确要求涂改时，禁止设置 fix_rate / fix_style。
- 纯分数答案（1/2、1/e 等）必须用 type:"fraction" 的 num/den 上下结构，禁止用 fill 写成 1/e 斜杠。

约束：
- 一次作答只用一种笔：pen（默认）或 pencil。
- 选择题字号 78、填空 50、α≥3 用 46；只按横向缩。
- 大题用 type:"work" 并给逐行过程，不能只给答案。
- 涂改只用 fix_rate（0~1）和 fix_style（double/cross/single/horizontal/zigzag/circle/mix）。
"""


def chat(messages, base_url, api_key, model, timeout=120):
    r = requests.post(
        base_url.rstrip('/') + '/chat/completions',
        headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
        json={'model': model, 'messages': messages, 'tools': TOOLS,
              'tool_choice': 'auto', 'temperature': 0.0},
        timeout=timeout,
    )
    r.raise_for_status()
    return r.json()['choices'][0]['message']


def _vision_cfg():
    return {
        'base': os.getenv('VISION_BASE_URL', ''),
        'key': os.getenv('VISION_API_KEY', ''),
        'model': os.getenv('VISION_MODEL', ''),
    }


def vision_describe(path, question, cfg):
    """让视觉模型看图并返回文字描述，供纯文本推理模型使用。"""
    url = cfg['base'].rstrip('/') + '/chat/completions'
    payload = {
        'model': cfg['model'], 'temperature': 0.1, 'max_tokens': 6000,
        'messages': [{'role': 'user', 'content': [
            {'type': 'text', 'text': question},
            {'type': 'image_url', 'image_url': {'url': _image_data_url(path)}},
        ]}],
    }
    r = requests.post(url, headers={
        'Authorization': f"Bearer {cfg['key'] or 'ollama'}",
        'Content-Type': 'application/json'}, json=payload, timeout=180)
    r.raise_for_status()
    return r.json()['choices'][0]['message'].get('content') or ''


def _read_agents_md(workspace, limit=6000):
    p = Path(workspace) / 'AGENTS.md'
    if not p.exists():
        return ''
    txt = p.read_text(encoding='utf-8', errors='replace')
    return txt[:limit]


def _all_ok(text):
    """从 run_command 返回里解析 auto_render 报告，判断是否所有 item.ok 都为真。"""
    if 'stdout:' not in text:
        return False
    body = text.split('stdout:', 1)[1]
    i, j = body.find('{'), body.rfind('}')
    if i < 0 or j <= i:
        return False
    try:
        rep = json.loads(body[i:j + 1])
    except Exception:
        return False
    items = rep.get('items') or []
    return bool(items) and all(bool(it.get('ok')) for it in items)


FAST_VISION_PROMPT = """你是试卷转写器，不解题。看完整张图，逐题输出 JSON，不要输出任何解释。

格式：
{"rotate":0,"questions":[
 {"q":1,"type":"choice","stem":"题干原文","options":["A. ...","B. ...","C. ...","D. ..."],"y":0.113,
  "area":{"x":0.575,"y":0.105,"w":0.05,"h":0.024}},
 {"q":7,"type":"fill","stem":"题干原文","y":0.765,"x":0.64,
  "area":{"x":0.590,"y":0.752,"w":0.10,"h":0.026}},
 {"q":19,"type":"work","stem":"题干原文","y":0.20,"area":{"x":0.07,"y":0.27,"w":0.86}}
]}

规则：
0. rotate 是整页文字转正需要顺时针旋转的角度，只能是 0/90/180/270；文字已经是正的填 0。
1. q 用卷面题号；题号看不清就按顺序推断。
2. type 只能是 choice / fill / work；选择/填空/解答必须分清。
3. 选择题必须给 options 完整选项；填空不给 options。
4. y 是答题横线或括号中心的纵向位置（0~1），不是题干位置。
5. 每一题都必须给 area：归一化到整张图的落笔区域 [x,y,w,h]（0~1），
   x/y 是左上角，w/h 是宽高；宁可略宽松，也不要偏到别的行。
   - choice：area 框住答题括号内部空档（紧贴括号内缘，高度允许略超括号）；
   - fill：area 是该横线的左右范围，高度取横线上下各约 1% 页高，底线压在横线上；
   - work：area 是真正可书写的空白区，必须避开印刷文字、插图和已有手写；没有空白就缩小高度。
   坐标必须逐题按实际位置读取，禁止所有题使用同一个 x。
6. 解答/证明题若含 (1)(2) 小问，必须拆成多条：q 写成 19(1)、19(2)，每条各自一个 area（各自题下空白）。
7. 作答位置规则：选择题字母写在括号内；填空题写在横线上；分数写在横线上、上下结构；
   解答/证明题写在题目下方空白处，避让插图、印刷文字和已有手写。
8. 只输出 JSON。"""

FAST_SOLVE_PROMPT = """你是数学解题器。根据题目逐题给出最终答案，只输出 JSON，不要解释。

格式：
{"solutions":[
 {"q":1,"type":"choice","answer":"C"},
 {"q":7,"type":"fill","answer":"1/2"},
 {"q":19,"type":"work","lines":["证明：...","...","所以 ..."]}
]}

规则：
1. 选择题必须逐项验证后再定答案，answer 只写一个大写字母。
2. 填空只写最终值；纯分数写 a/b；禁止 LaTeX（不许出现 \\frac、\\sqrt、$），
   上标用 ^2，根号用 sqrt()，乘号用 *，小于等于用 <=。
3. work 写 4~8 行完整过程，每行不超过 34 个字，结论单独一行；证明题必须写出关键定理。
4. 必须覆盖所有题号，不确定也要给最可能的答案。
5. 填空题若答案含参数范围，必须验证边界（等号）能否取到；
   含幂/上标（如 x_n^n）时不要漏掉指数。
6. 遇到 lim x_n^(y_n)（1^∞ 型）必须先取对数：x_n^(y_n)=e^(y_n ln x_n)。
7. 只输出 JSON。"""

FAST_ALT_PROMPT = """你是第二位解题人，独立完成，不要参考任何现成答案。
只输出 JSON，格式：
{"solutions":[{"q":1,"type":"choice","answer":"C"},{"q":7,"type":"fill","answer":"1/2"}]}

要求：
1. 选择题逐项验证，对“错误的是”必须给出反例；
2. 填空题检查定义域、极限存在条件、边界等号是否可取、指数/幂次是否漏掉；
3. 证明/解答题写出关键定理与结论；
4. 填空题答案若是不等式，必须把边界值代回题目验证：边界成立写 ≥/≤，不成立才写 >/<；
5. 覆盖所有题号，只输出 JSON。"""

FAST_ADJUDGE_PROMPT = """下面每题有两个答案。请独立判断哪个正确；都错就给出正确答案。
如果两个答案只差等号，必须把边界值代回原题验证后再定。
只输出 JSON：
{"solutions":[{"q":1,"type":"choice","answer":"D"},{"q":7,"type":"fill","answer":"1/2"}]}
不要照抄，不要解释。"""


def _extract_json(text):
    """从模型输出里取出第一个能解析的 JSON 对象。"""
    if not text:
        return None
    t = text.strip()
    t = re.sub(r'^```(?:json)?\s*|\s*```$', '', t, flags=re.M).strip()
    end = t.rfind('}')
    start = t.find('{')
    while start >= 0 and end > start:
        try:
            return json.loads(t[start:end + 1])
        except Exception:
            end = t.rfind('}', 0, end)
    return None


def _normalize_math(s):
    """把 LaTeX 和特殊字符转成字体能写的写法。"""
    s = str(s or '').strip().replace('$', '')
    for a, b in (('−', '-'), ('·', '*'), ('²', '^2'), ('³', '^3'), ('×', '*'), ('÷', '/')):
        s = s.replace(a, b)
    s = (s.replace('\\left', '').replace('\\right', '').replace('\\limits', '')
         .replace('\\,', '').replace('\\!', ''))
    s = re.sub(r'\^\{([^{}]*)\}', r'^(\1)', s)
    s = re.sub(r'_\{([^{}]*)\}', r'_(\1)', s)
    s = re.sub(r'\\[dt]?frac\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}',
               lambda m: f'({m.group(1)})/({m.group(2)})', s)
    s = re.sub(r'\\sqrt\[([^\]]+)\]\{([^{}]*)\}', lambda m: f'({m.group(2)})^(1/{m.group(1)})', s)
    s = re.sub(r'\\sqrt\{([^{}]*)\}', lambda m: f'sqrt({m.group(1)})', s)
    for a, b in (('\\leq', '<='), ('\\le', '<='), ('\\geq', '>='), ('\\ge', '>='),
                 ('\\neq', '!='), ('\\times', '*'), ('\\cdot', '*'), ('\\div', '/'),
                 ('\\pi', 'π'), ('\\alpha', 'α'), ('\\beta', 'β'), ('\\theta', 'θ'),
                 ('\\infty', '∞'), ('\\to', '->'), ('\\sin', ' sin '), ('\\cos', ' cos '),
                 ('\\tan', ' tan '), ('\\ln', ' ln '), ('\\log', ' log '), ('\\lim', 'lim')):
        s = s.replace(a, b)
    s = re.sub(r'\\[a-zA-Z]+', '', s)
    s = s.replace('{', '').replace('}', '')
    # 只去掉单 token 的括号：1/2、(1)/(e)；保留 (1+x^2) 这类分母
    s = re.sub(r'\((\w{1,3})\)/\((\w{1,3})\)', r'\1/\2', s)
    s = re.sub(r'(?<=[a-zA-Z0-9])(sin|cos|tan|ln|log)\b', r' \1', s)
    return re.sub(r'\s+', ' ', s).strip()


def _as_fraction(ans):
    m = re.fullmatch(r'(-?\d+)/(\d+|[a-zA-Zα-ω])', ans or '')
    return (m.group(1), m.group(2)) if m else None


def _vision_json(image, task, cfg, rounds=2):
    """整页一次转写，返回 {"questions":[...]}；失败重试一次。"""
    url = cfg['base'].rstrip('/') + '/chat/completions'
    prompt = FAST_VISION_PROMPT + ('\n\n用户要求：' + task if task else '')
    for k in range(rounds):
        payload = {
            'model': cfg['model'], 'temperature': 0.0, 'max_tokens': 8000,
            'messages': [{'role': 'user', 'content': [
                {'type': 'text', 'text': prompt},
                {'type': 'image_url', 'image_url': {'url': _image_data_url(image)}},
            ]}],
        }
        r = requests.post(url, headers={
            'Authorization': f"Bearer {cfg['key'] or 'ollama'}",
            'Content-Type': 'application/json'}, json=payload, timeout=300)
        if r.status_code >= 400:
            print(f'[vision] HTTP {r.status_code}: {r.text[:400]}')
            return None
        txt = r.json()['choices'][0]['message'].get('content') or ''
        data = _extract_json(txt)
        if data and isinstance(data.get('questions'), list) and data['questions']:
            return data
        print(f'[fast] 转写 JSON 解析失败，重试 {k + 1}/{rounds}')
    return None


def _llm_text(messages, timeout=240, prefer=None):
    """纯文本模型调用；没配 LLM 时退回视觉模型，支持单 Key 运行。"""
    if prefer == 'vision':
        base = os.getenv('VISION_BASE_URL')
        key = os.getenv('VISION_API_KEY')
        model = os.getenv('VISION_MODEL')
    else:
        base = os.getenv('LLM_BASE_URL') or os.getenv('VISION_BASE_URL')
        key = os.getenv('LLM_API_KEY') or os.getenv('VISION_API_KEY')
        model = os.getenv('LLM_MODEL') or os.getenv('VISION_MODEL')
    models = [model]
    # 个别账号还没开放 v4.1 别名时，退回 deepseek-chat，避免整页拿不到答案
    if model and 'v4.1' in model.lower():
        models.append('deepseek-chat')
    last_err = ''
    for m in models:
        r = requests.post(base.rstrip('/') + '/chat/completions',
                          headers={'Authorization': f'Bearer {key}',
                                   'Content-Type': 'application/json'},
                          json={'model': m, 'messages': messages, 'temperature': 0.0},
                          timeout=timeout)
        if r.status_code < 400:
            if m != model:
                print(f'[llm] {model} 不可用，已回退到 {m}')
            return r.json()['choices'][0]['message'].get('content') or ''
        last_err = f'HTTP {r.status_code}: {r.text[:300]}'
        print(f'[llm] 模型 {m} 调用失败：{last_err}')
    raise RuntimeError('解题模型调用失败：' + last_err)


def _scan_layout(image):
    gray = cv2.imdecode(np.fromfile(str(image), dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
    if gray is None:
        return [], []
    return _scan_layout_gray(gray)


def _scan_layout_gray(gray):
    """整页扫描答题括号行与横线，替代模型目测的 y。"""
    if gray is None:
        return [], []
    choices, unders = [], []
    for i in range(60, 988, 8):
        b = align.detect_choice(gray, i / 1000.0, half=0.012)
        # 答题括号的内间距通常 >=5% 页宽；更窄的是选项/函数括号
        if b and (b['x1'] - b['x0']) >= 0.05:
            choices.append(b)
    choices.sort(key=lambda b: b['cy'])
    ch = []
    for b in choices:
        if ch and abs(b['cy'] - ch[-1]['cy']) < 0.010:
            if b['x0'] > ch[-1]['x0']:
                ch[-1] = b
        elif ch and b['cy'] - ch[-1]['cy'] < 0.05:
            # 选项行紧贴在题干下方，间隔 <0.05 的一律不是答题括号行
            continue
        else:
            ch.append(b)
    for i in range(60, 995, 10):
        # kern=16：扫描件横线有轻微倾斜，25px 核会把长横线切碎
        unders.extend(align._morph_underlines(gray, i / 1000.0, 0.012,
                                              max_w=0.60, kern=16))
    unders.sort(key=lambda c: c['cy'])
    un = []
    for c in unders:
        if (un and abs(c['cy'] - un[-1]['cy']) < 0.008
                and not (c['x1'] < un[-1]['x0'] or c['x0'] > un[-1]['x1'])):
            if (c['x1'] - c['x0']) > (un[-1]['x1'] - un[-1]['x0']):
                un[-1] = c
        else:
            un.append(c)
    # 页面底部 5% 是页脚/装饰线，不允许当答题线
    un = [c for c in un if c['cy'] <= 0.95]
    return ch, un


def _match_rows(qs, rows, window, start_cy=None, use_x=False, min_gap=0.0):
    """按题号顺序把题目配到检测行，保持单调，避免串到相邻题。

    use_x=True 时用模型给的粗 x 排除同一题里的分数线（教材页/计算题常见）；
    min_gap 保证相邻两题不会落到同一条线上（防止分数线上沿被当成答题线）。
    """
    picked, j, last_cy = {}, 0, None
    for q in qs:
        y = float(q.get('y', 0.5))
        ax = float(q['x']) if (use_x and q.get('x') is not None) else None
        def _pick(limit):
            best = None
            for k in range(j, len(rows)):
                cy = rows[k]['cy']
                if start_cy is not None and cy <= start_cy + 0.008:
                    continue
                if cy > 0.97:
                    break
                if min_gap and last_cy is not None and cy - last_cy < min_gap:
                    continue
                dy = abs(cy - y)
                if dy > limit:
                    if cy > y + window:
                        break
                    continue
                cx = (rows[k]['x0'] + rows[k]['x1']) / 2
                if ax is not None and abs(cx - ax) > 0.30:
                    continue
                score = dy + (0.25 * abs(cx - ax) if ax is not None else 0.0)
                if best is None or score < best[0]:
                    best = (score, k)
            return best

        best = _pick(min(0.10, window)) or _pick(window)
        if best:
            picked[id(q)] = rows[best[1]]
            j = best[1] + 1
            last_cy = rows[best[1]]['cy']
    return picked


def _build_commands(questions, solutions, choice_rows=None, fill_rows=None):
    """把转写 + 答案合成最小 commands.json。"""
    sols = {}
    for s in solutions:
        try:
            sols[str(s.get('q'))] = s
        except Exception:
            continue
    # 有些卷子每大题重新编号（1~6 后又是 1~5），此时按顺序配对，避免串答案
    uniq = len({str(q.get('q')) for q in questions}) == len(questions)
    ch_q = [q for q in questions if (q.get('type') or '').lower() == 'choice']
    fl_q = [q for q in questions if (q.get('type') or '').lower() != 'choice']
    crows = choice_rows or []
    if len(crows) == len(ch_q) and ch_q:
        # 行数正好对上时按题号顺序绑定，完全不依赖模型目测的 y
        ch_map = {id(q): crows[i] for i, q in enumerate(ch_q)}
    else:
        ch_map = _match_rows(ch_q, crows, 0.13, min_gap=0.020)
    start = max([r['cy'] for r in crows], default=None)
    fl_map = _match_rows(fl_q, fill_rows or [], 0.16, start_cy=start, use_x=True, min_gap=0.028)
    items = []
    for idx, q in enumerate(questions):
        key = str(q.get('q'))
        s = sols.get(key) or {}
        if not uniq and idx < len(solutions):
            s = solutions[idx]
        if not s:
            continue
        typ = (q.get('type') or s.get('type') or 'fill').lower()
        y = float(q.get('y', 0.5))
        base = {'q': q.get('q')}
        row = ch_map.get(id(q)) if typ == 'choice' else fl_map.get(id(q))
        if row and typ != 'work':
            y = round(float(row['cy']), 4)
        if typ == 'work' or s.get('lines'):
            area = q.get('area') or {}
            lines = s.get('lines') or [s.get('answer', '')]
            if isinstance(lines, str):
                lines = lines.split('\n')
            lines = [_normalize_math(x) for x in lines if str(x).strip()]
            if not lines:
                continue
            items.append({**base, 'type': 'work',
                          'x': float(area.get('x', 0.07)),
                          'y': float(area.get('y', min(0.94, y + 0.03))),
                          'w': float(area.get('w', 0.86)),
                          'size': float(area.get('size', 30)),
                          'lines': lines})
        elif typ == 'choice':
            ans = _normalize_math(s.get('answer', ''))
            m = re.search(r'[A-Da-d]', ans)
            if not m:
                continue
            it = {**base, 'type': 'choice', 'text': m.group().upper(), 'y': y}
            if q.get('x') is not None:
                it['x'] = float(q['x'])
            items.append(it)
        else:
            ans = _normalize_math(s.get('answer', ''))
            ans = re.sub(r'\balpha\b', 'α', ans)
            ans = re.sub(r'\bbeta\b', 'β', ans)
            ans = ans.replace('>=', '≥').replace('<=', '≤')
            if not ans:
                continue
            frac = _as_fraction(ans)
            it = {**base, 'type': 'fraction', 'num': frac[0], 'den': frac[1], 'y': y} \
                if frac else {**base, 'type': 'fill', 'text': ans, 'y': y}
            if row:
                it['x'] = round((float(row['x0']) + float(row['x1'])) / 2, 4)
            elif q.get('x') is not None:
                it['x'] = float(q['x'])
            items.append(it)
    return {'style': 'pen', 'items': items}


def _render(image, workspace, commands, out_png, calib, no_align=False, rectify_off=False):
    cmd = Path(workspace) / 'commands.json'
    cmd.write_text(json.dumps(commands, ensure_ascii=False, indent=1), encoding='utf-8')
    argv = [sys.executable, str(TOOL_DIR / 'auto_render.py'),
            str(Path(image).resolve()), str(cmd), str(out_png), '--calib', str(calib)]
    if no_align:
        argv.append('--no-align')
    if rectify_off:
        argv += ['--rectify', 'off']
    r = subprocess.run(argv, capture_output=True, text=True, timeout=240,
                       cwd=str(workspace))
    return r, _extract_json(r.stdout)


def _solve(questions, alt=False):
    txt = _llm_text([
        {'role': 'system', 'content': FAST_ALT_PROMPT if alt else FAST_SOLVE_PROMPT},
        {'role': 'user', 'content': json.dumps({'questions': questions}, ensure_ascii=False)},
    ])
    return (_extract_json(txt) or {}).get('solutions') or []


def _answer_key(s):
    if s.get('lines'):
        return 'W|' + '|'.join(_normalize_math(x) for x in s['lines'])
    return _normalize_math(s.get('answer', '')).replace(' ', '').lower()


def _reconcile(questions, first, second):
    """两遍独立解题；不一致的题交给第三遍裁决。"""
    if not second or len(first) != len(second):
        return first
    diff = [i for i, (a, b) in enumerate(zip(first, second)) if _answer_key(a) != _answer_key(b)]
    if not diff:
        return first
    print(f'[fast] 两遍答案有 {len(diff)} 题不一致，进入裁决')
    payload = []
    for i in diff:
        q = questions[i] if i < len(questions) else {}
        payload.append({'q': q.get('q'), 'type': q.get('type'), 'stem': q.get('stem'),
                        'options': q.get('options'), 'A': first[i], 'B': second[i]})
    txt = _llm_text([
        {'role': 'system', 'content': FAST_ADJUDGE_PROMPT},
        {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)},
    ])
    adj = (_extract_json(txt) or {}).get('solutions') or []
    if len(adj) != len(diff):
        return first
    out = list(first)
    for k, i in enumerate(diff):
        out[i] = adj[k]
    return out


WORK_PROMPT = """你是解答题写作器。对每道解答/证明题写出完整过程，只输出 JSON：
{"solutions":[{"q":19,"lines":["证明：...","...","所以 ..."]}]}

要求：
1. 4~8 行，每行不超过 34 个字；
2. 证明题写清使用的定理和关键推理；计算题写关键步骤和结论；
3. 不能只给一个答案或一个字母；
4. 只输出 JSON。"""


def _ensure_work(questions, solutions):
    """解答/证明题必须有多行过程；模型只给一个字母时补写完整过程。"""
    need = []
    for i, q in enumerate(questions):
        if (q.get('type') or '').lower() != 'work':
            continue
        s = solutions[i] if i < len(solutions) else {}
        lines = s.get('lines')
        if not isinstance(lines, list) or len(lines) < 2:
            need.append(i)
    if not need:
        return solutions
    print(f'[fast] 补写解答过程：{len(need)} 题')
    payload = [questions[i] for i in need]
    txt = _llm_text([
        {'role': 'system', 'content': WORK_PROMPT},
        {'role': 'user', 'content': json.dumps({'questions': payload}, ensure_ascii=False)},
    ])
    got = (_extract_json(txt) or {}).get('solutions') or []
    if len(got) != len(need):
        return solutions
    out = list(solutions)
    for k, i in enumerate(need):
        out[i] = got[k]
    return out


def _audit_bounds(questions, solutions):
    """对含不等式的填空答案做边界复核：把等号端点代回题目。"""
    out = list(solutions)
    for i, s in enumerate(solutions):
        ans = _normalize_math(s.get('answer', ''))
        if not re.search(r'[<>]=?', ans):
            continue
        q = questions[i] if i < len(questions) else {}
        txt = _llm_text([
            {'role': 'system', 'content':
             '你是边界审查员。把候选答案的等号端点代回原题，判断等号能否取到：'
             '能取到写 >= 或 <=，不能取到写 > 或 <。'
             '必须真的做一次等价无穷小/代入计算，不能凭感觉；'
             '若端点代入后极限是有限数，就说明等号可取。'
             '只输出 JSON：{"answer":"..."}'},
            {'role': 'user', 'content': json.dumps(
                {'stem': q.get('stem'), 'candidate': ans}, ensure_ascii=False)},
        ], prefer='vision')
        fixed = (_extract_json(txt) or {}).get('answer')
        if fixed:
            out[i] = {**s, 'answer': fixed}
    return out


def run_fast(image, task, workspace, retries=2, trace='more'):
    """受控流程：一次转写 -> 一次解题 -> 固定 commands -> 落笔 -> 只修失败题。"""
    if not image:
        print('缺少 --image')
        return 2
    vision = _vision_cfg()
    if not (vision.get('base') and vision.get('model')):
        print('缺少视觉模型配置 VISION_BASE_URL / VISION_MODEL / VISION_API_KEY')
        return 2
    ws = Path(workspace).resolve()
    ws.mkdir(parents=True, exist_ok=True)
    print('[fast] 1/4 整页转写')
    data = _vision_json(image, task, vision)
    if not data:
        print('[fast] 转写失败，未生成作答')
        return 1
    questions = data['questions']
    print(f'[fast] 识别到 {len(questions)} 题：' + ', '.join(str(q.get('q')) for q in questions))

    print('[fast] 1.5/4 扫描版面（括号行 / 横线）')
    ch_rows, fl_rows = _scan_layout(image)
    print(f'[fast] 括号行 {len(ch_rows)}，横线 {len(fl_rows)}')

    print('[fast] 2/4 解题')
    solutions = _solve(questions)
    print(f'[fast] 得到 {len(solutions)} 个答案')

    print('[fast] 2.5/4 第二遍独立解题 + 交叉核对')
    alt = _solve(questions, alt=True)
    solutions = _reconcile(questions, solutions, alt)
    solutions = _audit_bounds(questions, solutions)
    solutions = _ensure_work(questions, solutions)
    print(f'[fast] 最终答案 {len(solutions)} 个')

    (ws / 'questions.json').write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding='utf-8')
    (ws / 'solutions.json').write_text(json.dumps(solutions, ensure_ascii=False, indent=1), encoding='utf-8')
    gray = cv2.imdecode(np.fromfile(str(image), dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
    ocr_lines = None
    if gray is not None:
        try:
            ocr_lines = _ocr_lines(_enhance_gray(gray))
        except Exception as e:
            print(f'[fast] OCR 失败，跳过：{e}')
    items = _plan_items(questions, ch_rows, fl_rows, gray, ocr_lines)
    commands = _commands_from_plan(items, solutions, gray)
    if commands['items'] and gray is not None:
        try:
            tr = _auto_traces(gray, items, solutions, trace)
            if tr:
                commands['traces'] = tr
                print(f'[fast] 做题痕迹 {len(tr)} 条（{trace}）')
        except Exception as e:
            print(f'[fast] 做题痕迹生成失败：{type(e).__name__}: {e}')
    if not commands['items']:
        print('[fast] 没有可落笔的答案')
        return 1

    print('[fast] 3/4 落笔')
    stem = Path(image).stem
    out_png = ws / f'{stem}_作答_API.png'
    calib = ws / f'{stem}_校准_API.png'
    for attempt in range(retries + 1):
        r, rep = _render(image, ws, commands, out_png, calib)
        if rep is None:
            print('[fast] 渲染报告解析失败：' + (r.stderr or r.stdout)[-400:])
            return 1
        items = rep.get('items') or []
        for it in items:
            d = it.get('drift') or [0, 0]
            print(f"[fast] q{it.get('q')} {it.get('type')} ok={it.get('ok')} "
                  f"size={it.get('size')} drift=({d[0]},{d[1]})")
        bad = [it for it in items if not it.get('ok')]
        if not bad:
            _write_layout(ws, out_png, commands, items)
            print(f'[fast] 4/4 完成，全部通过：{out_png}')
            return 0
        if attempt >= retries:
            break
        print(f'[fast] 第 {attempt + 1} 次修正失败题：' + ', '.join(str(b.get('q')) for b in bad))
        bad_idx = {i for i, it in enumerate(items) if not it.get('ok')}
        for i, it in enumerate(commands['items']):
            if i not in bad_idx or i >= len(items):
                continue
            d = items[i].get('drift') or [0, 0]
            step = float(d[1]) if abs(float(d[1])) > 1e-4 else (0.010 if attempt == 0 else -0.008)
            it['y'] = round(max(0.02, min(0.97, float(it['y']) + step)), 4)
        print('[fast] 按 drift 修正后重跑')
    print(f'[fast] 仍有未通过项，作答图已保留：{out_png}')
    _write_layout(ws, out_png, commands, items)
    return 1


def _normalize_types(questions):
    """没有选项的“选择题”是误判：按题干改成填空或解答题。"""
    for q in questions:
        opts = q.get('options') or []
        typ = (q.get('type') or '').lower()
        stem = q.get('stem') or ''
        if typ == 'choice' and len(opts) < 2:
            q['type'] = 'work' if re.search(r'证明|求证|推导|推出|说明理由', stem) else 'fill'
        elif typ in ('fill', 'fraction') \
                and not re.search(r'_{2,}|[（(]\s*[）)]', stem) \
                and re.search(r'lim|求下列|计算|证明|解方程|试求', stem):
            # “(1) lim …”这种没有横线的小题是解答题，不是填空
            q['type'] = 'work'
    return questions


def _snap_choice_box(gray, y, x):
    """在模型给的括号中心附近，吸附到真正的空白括号对。"""
    if gray is None or x is None:
        return None
    H = gray.shape[0]
    best = None
    for p in align._choice_pairs(gray, y, 0.020, 0.03):
        if p['h'] * H < 16:                     # 太矮的是数学符号/字母碎片
            continue
        cx = (p['x0'] + p['x1']) / 2
        if abs(cx - x) > 0.07 or abs(p['cy'] - y) > 0.022:
            continue
        d = abs(cx - x) + 0.6 * abs(p['cy'] - y)
        if best is None or d < best[0]:
            best = (d, p)
    if not best:
        return None
    p = best[1]
    return [round(p['x0'] - 0.004, 4), round(p['cy'] - 0.014, 4),
            round(p['x1'] + 0.004, 4), round(p['cy'] + 0.014, 4)]


def _snap_fill_box(gray, y, x):
    """在模型给的横线附近，吸附到真正的答题横线。"""
    if gray is None or x is None:
        return None
    best = None
    for c in align._morph_underlines(gray, y, 0.022, max_w=0.30):
        if abs(c['cy'] - y) > 0.022:
            continue
        if x is not None and abs(c['cx'] - x) > 0.09:
            continue
        d = (abs(c['cx'] - x) if x is not None else 0.0) + 0.8 * abs(c['cy'] - y)
        if best is None or d < best[0]:
            best = (d, c)
    if not best:
        return None
    c = best[1]
    return [round(c['x0'], 4), round(c['cy'] - 0.030, 4),
            round(c['x1'], 4), round(c['cy'], 4)]


def _row_box(row, typ, mb=None):
    """把检测到的行转成框；mb 存在时要求行落在 mb 附近。"""
    y = round(float(row['cy']), 4)
    x0, x1 = float(row['x0']), float(row['x1'])
    if mb:
        if not (mb[1] - 0.018 <= y <= mb[3] + 0.018):
            return None
        if not (min(x1, mb[2]) - max(x0, mb[0]) > 0):
            return None
    pad = 0.02 if typ == 'choice' else 0.012
    return [round(x0, 4), round(y - pad, 4), round(x1, 4), round(y + pad, 4)]


def _line_end_box(gray, y, typ):
    """兜底：把框放在该行墨迹的最右端（答题空档通常紧跟题干末尾）。"""
    if gray is None:
        return None
    H, W = gray.shape
    y0 = max(0, int((y - 0.012) * H))
    y1 = min(H, int((y + 0.012) * H))
    if y1 - y0 < 6:
        return None
    ink = gray[y0:y1, :] < 118
    cols = ink.sum(axis=0)
    xs = [i for i, c in enumerate(cols) if c >= 2 and 0.05 * W < i < 0.92 * W]
    if not xs:
        return None
    right = max(xs) / W
    if typ == 'choice':
        box = [right - 0.075, y - 0.014, right - 0.005, y + 0.014]
    else:
        box = [right - 0.10, y - 0.024, right, y + 0.004]
    return [round(v, 4) for v in box]


def _drop_bad_x(questions):
    """模型常把整列写成同一个 x；同题型 x 几乎相同时视为无效，直接丢弃。"""
    for typ in ('choice', 'fill'):
        xs = [v for v in (_num(q.get('x'), None) for q in questions
                          if (q.get('type') or '').lower() == typ) if v is not None]
        if len(xs) >= 3 and (max(xs) - min(xs)) < 0.012:
            for q in questions:
                if (q.get('type') or '').lower() == typ:
                    q.pop('x', None)
        # 非数字的 x 也清掉，避免后面 float 崩
        for q in questions:
            if (q.get('type') or '').lower() == typ and q.get('x') is not None:
                if _num(q.get('x'), None) is None:
                    q.pop('x', None)


_OCR_ENGINE = None


def _ocr_lines(gray):
    """本地 OCR：返回按行分组的文字框（归一化坐标）。没装依赖就返回 None。"""
    global _OCR_ENGINE
    try:
        from rapidocr_onnxruntime import RapidOCR
    except Exception:
        return None
    try:
        if _OCR_ENGINE is None:
            _OCR_ENGINE = RapidOCR()
        bgr = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
        res, _ = _OCR_ENGINE(bgr)
    except Exception:
        return None
    if not res:
        return None
    H, W = gray.shape
    items = []
    for box, text, _s in res:
        xs = [float(p[0]) / W for p in box]
        ys = [float(p[1]) / H for p in box]
        items.append({'x0': min(xs), 'x1': max(xs),
                      'y': (min(ys) + max(ys)) / 2,
                      'h': max(ys) - min(ys), 'text': str(text)})
    items.sort(key=lambda t: t['y'])
    lines = []
    for it in items:
        if lines and abs(it['y'] - lines[-1]['y']) <= 0.012:
            ln = lines[-1]
            ln['items'].append(it)
            ln['x0'] = min(ln['x0'], it['x0'])
            ln['x1'] = max(ln['x1'], it['x1'])
            ln['h'] = max(ln.get('h', it['h']), it['h'])
            ln['text'] += ' ' + it['text']
            ln['y'] = sum(x['y'] for x in ln['items']) / len(ln['items'])
        else:
            lines.append({'y': it['y'], 'x0': it['x0'], 'x1': it['x1'],
                          'h': it['h'], 'text': it['text'], 'items': [it]})
    for ln in lines:
        ln['items'].sort(key=lambda t: t['x0'])
    return lines


_Q_START = re.compile(r'^\s*[（(]?\s*(\d{1,2})\s*[.．、)）]')


# ---------------- 做题痕迹（画在题目内容上） ----------------
_TRACE_KW = ('极小值', '极大值', '最小值', '最大值', '拐点', '曲率', '渐近线',
             '可导', '连续', '单调', '极值', '凹凸', '切线', '法线', '极限',
             '导数', '面积', '体积', '零点', '积分', '方程', '必有')
_TRACE_KINDS = ('circle', 'wave', 'double', 'tick')


def _line_near(lines, y, tol=0.035):
    best = None
    for ln in lines:
        d = abs(float(ln.get('y', -9)) - y)
        if d <= tol and (best is None or d < abs(best['y'] - y)):
            best = ln
    return best


def _keyword_box(ln, kws=_TRACE_KW, limit_x=None):
    """在行内的 OCR 片段里按字数比例定位关键词，返回圈画框。"""
    for tok in ln.get('items') or []:
        t = tok.get('text') or ''
        if not t:
            continue
        for kw in kws:
            i = t.find(kw)
            if i < 0:
                continue
            w = max(0.004, float(tok['x1']) - float(tok['x0']))
            x0 = float(tok['x0']) + w * i / len(t)
            x1 = float(tok['x0']) + w * (i + len(kw)) / len(t)
            if limit_x is not None and x1 > limit_x:
                continue
            hh = max(0.008, min(0.020, float(tok.get('h', 0.02))))
            return [x0 - 0.004, float(tok['y']) - hh * 0.72,
                    x1 + 0.004, float(tok['y']) + hh * 0.72]
    return None


def _option_line(lines, letter, y0, max_len=70):
    pat = re.compile(r'[（(]\s*%s\s*[)）]' % re.escape(letter))
    best = None
    for ln in lines:
        y = float(ln.get('y', -9))
        if not (y0 - 0.012 <= y <= y0 + 0.10):
            continue
        if float(ln['x0']) < 0.05:
            continue
        t = (ln.get('text') or '').strip()
        if pat.search(t) and len(t) <= max_len:
            if best is None or abs(y - y0) < abs(best['y'] - y0):
                best = ln
    return best


def _wrong_option_line(lines, ans, y0):
    for letter in ('D', 'C', 'B', 'E', 'A'):
        if letter == ans:
            continue
        ln = _option_line(lines, letter, y0)
        if ln is not None:
            return ln
    return None


def _question_line(lines, y, box=None):
    """题目行：必须横向覆盖答案框左端，避免落到相邻题/选项/页边噪声上。"""
    bx0 = float(box[0]) if box and len(box) >= 4 else None
    best, best_s = None, 9.0
    for ln in lines:
        if float(ln['x1']) - float(ln['x0']) < 0.07:
            continue
        if len(re.findall(r'[（(]\s*[A-E]\s*[)）]', ln.get('text') or '')) >= 2:
            continue  # 两行选项被 OCR 粘成一行，不作为题干行
        d = abs(float(ln['y']) - y)
        if d > 0.045:
            continue
        if bx0 is not None and not (float(ln['x0']) - 0.015 <= bx0 <= float(ln['x1']) + 0.015):
            continue
        s = d + (0.03 if float(ln['x0']) < 0.05 else 0.0)
        if s < best_s:
            best_s, best = s, ln
    return best


def _text_bottom(gray, x0, x1, y, win=0.012):
    """在行附近找印刷字的最低墨迹行，保证下划线贴在字下方而不是穿字。"""
    if gray is None:
        return None
    H, W = gray.shape
    ya = int(max(0, (y - win) * H))
    yb = int(min(H, (y + win) * H))
    xa = int(max(0, x0) * W)
    xb = int(min(W, x1 * W))
    if yb - ya < 4 or xb - xa < 8:
        return None
    band = gray[ya:yb, xa:xb]
    mask = band < 110
    need = max(2, int((xb - xa) * 0.02))
    rows = mask.sum(1)
    idx = [i for i, v in enumerate(rows) if v >= need]
    if not idx:
        return None
    return (ya + idx[-1]) / H


def _trace_under(gray, ln, frac=1.0, extra=0.0, height=0.0068, stop_x=None):
    x0 = max(0.02, float(ln['x0']) + 0.004)
    x1 = float(ln['x1']) - 0.004
    if frac < 1.0:
        x1 = min(x1, x0 + max(0.02, (x1 - x0) * frac))
    if stop_x is not None:
        x1 = min(x1, float(stop_x))
    x1 = max(x0 + 0.01, x1)
    bottom = _text_bottom(gray, x0, x1, float(ln['y']))
    if bottom is not None:
        y = bottom + 0.0035 + extra
    else:
        y = float(ln['y']) + max(0.008, float(ln.get('h', 0.02)) / 2) + 0.004 + extra
    return [x0, y, x1, y + height]


def _keyword_box_any(ln, kws=_TRACE_KW, limit_x=None):
    """先按 OCR 片段定位关键词；片段切碎时退回整行按字数比例定位。"""
    kb = _keyword_box(ln, kws, limit_x)
    if kb is not None:
        return kb
    t = ln.get('text') or ''
    w = float(ln['x1']) - float(ln['x0'])
    if w <= 0.01 or not t:
        return None
    for kw in kws:
        i = t.find(kw)
        if i < 0:
            continue
        x0 = float(ln['x0']) + w * i / len(t)
        x1 = float(ln['x0']) + w * (i + len(kw)) / len(t)
        if limit_x is not None and x1 > limit_x:
            continue
        hh = max(0.008, min(0.022, float(ln.get('h', 0.02))))
        return [x0 - 0.004, float(ln['y']) - hh * 0.72,
                x1 + 0.004, float(ln['y']) + hh * 0.72]
    return None


def _trim_traces(traces, level):
    """按挡位裁剪痕迹：none=全不留，less=保留对勾/涂改 + 隔一条留一条，more=全量。"""
    if level == 'none' or not traces:
        return []
    if level != 'less' or len(traces) <= 3:
        return traces
    core = {i for i, t in enumerate(traces) if t.get('type') in ('tick', 'scribble')}
    others = [i for i, t in enumerate(traces) if i not in core]
    keep = core | set(others[::2])
    return [t for i, t in enumerate(traces) if i in keep]


def _auto_traces(gray, items, solutions, level='more'):
    """按题目内容自动生成做题痕迹：圈关键词/正确选项、划条件、题号打勾、划掉错项。"""
    if gray is None or level == 'none':
        return []
    lines = _ocr_lines(_enhance_gray(gray))
    if not lines:
        return []
    out = []
    scribbled = False
    ticked = False
    choice_seen = 0
    work_total = sum(1 for x in items if (x.get('type') or '').lower() == 'work')
    work_idx = 0
    for i, it in enumerate(items):
        if it.get('skip') or it.get('no_trace'):
            continue
        typ = (it.get('type') or 'fill').lower()
        if typ == 'choice':
            choice_seen += 1
        box = it.get('box') or []
        y = ((float(box[1]) + float(box[3])) / 2) if len(box) == 4 \
            else float(it.get('y', 0.5))
        ans = ''
        if i < len(solutions):
            ans = (_answer_key(solutions[i]) or '').upper()
        stem = str(it.get('stem') or '')
        try:
            qn = int(str(it.get('q')).strip())
        except (TypeError, ValueError):
            qn = None
        # 题目区域：题号行往下到下一个题号行之前
        start = _qnum_line(lines, qn, y) if qn is not None else None
        if start is not None:
            y0 = float(start['y']) - 0.012
            y1 = min(y0 + 0.10, float(start['y']) + 0.060)
            for ln in lines:
                if y0 < float(ln['y']) < y1 and ln is not start:
                    txt = ln.get('text') or ''
                    other = re.search(r'[（(]\s*(\d{1,2})\s*[)）]', txt)
                    if (_qnum(txt) not in (None, qn)
                            or (other and int(other.group(1)) != qn)):
                        y1 = float(ln['y']) - 0.004
                        break
            region = [ln for ln in lines if y0 <= float(ln['y']) <= y1]
        else:
            region = [ln for ln in lines if abs(float(ln['y']) - y) <= 0.038]
        stem_lines = [ln for ln in region if not _OPT_RE.match(ln.get('text') or '')]
        if not stem_lines:
            continue
        base = min(stem_lines, key=lambda l: abs(float(l['y']) - y))
        opt = _option_line(lines, ans, y) \
            if typ == 'choice' and re.fullmatch(r'[A-E]', ans) else None
        # 圈关键词：先看正确选项，再看题干
        kb = None
        for ln in ([opt] if opt is not None else []) + stem_lines:
            kb = _keyword_box_any(ln)
            if kb:
                break
        if (not ticked) and typ == 'choice':
            kind = 'tick'
            ticked = True
            yy = float(base['y']) - 0.014
            tb = [0.098, yy - 0.013, 0.118, yy + 0.013]
        elif typ == 'work':
            # 同一行并列几道小题：按序号把横线切成等份，各划各的
            wide = [ln for ln in lines
                    if (y - 0.02) <= float(ln['y']) <= (y + 0.10)
                    and re.search(r'lim|极限', ln.get('text') or '')]
            if wide:
                base = max(wide, key=lambda l: float(l['x1']) - float(l['x0']))
            k, work_idx = work_idx, work_idx + 1
            xa, xb = float(base['x0']), float(base['x1'])
            n = max(1, work_total)
            seg = {**base, 'x0': xa + (xb - xa) * k / n,
                   'x1': xa + (xb - xa) * (k + 1) / n}
            kind, tb = 'wave', _trace_under(gray, seg, frac=0.80)
        elif kb is not None:
            kind, tb = 'circle', kb
        elif re.search(r'lim|极限', stem) or re.search(r'lim|极限', base.get('text') or ''):
            kind, tb = 'wave', _trace_under(gray, base, frac=0.70)
        elif (float(base['x1']) - float(base['x0'])) > 0.42:
            # 长题干只划末尾一段，避免长横线压到下一行选项
            seg = {**base, 'x0': float(base['x0']) + 0.62 * (float(base['x1']) - float(base['x0']))}
            kind, tb = 'double', _trace_under(gray, seg, frac=1.0, height=0.0095)
        else:
            kind, tb = 'wave', _trace_under(gray, base, frac=0.62)
        if tb[2] - tb[0] < 0.015 or tb[3] - tb[1] < 0.004:
            continue
        if kind != 'circle' and kind != 'tick' and (tb[2] - tb[0]) < 0.06:
            continue
        out.append({'q': it.get('q'), 'type': kind,
                    'box': [round(max(0.0, min(1.0, v)), 4) for v in tb]})
        # 第二道选择题顺手划掉一个错误选项，补齐“涂改”痕迹
        if (not scribbled) and typ == 'choice' and choice_seen == 2 \
                and re.fullmatch(r'[A-E]', ans):
            wrong = _wrong_option_line(lines, ans, y)
            if wrong is not None:
                hh = min(0.009, max(0.007, float(wrong.get('h', 0.02)) * 0.45))
                wx0 = max(0.05, float(wrong['x0']) + 0.002)
                wx1 = float(wrong['x1']) - 0.002
                if wx1 - wx0 < 0.08:
                    wrong = None
            if wrong is not None:
                out.append({'q': it.get('q'), 'type': 'scribble',
                            'box': [round(wx0, 4),
                                    round(float(wrong['y']) - hh, 4),
                                    round(wx1, 4),
                                    round(float(wrong['y']) + hh, 4)]})
                scribbled = True
    seen, dedup = set(), []
    for t in out:
        key = (t['type'],) + tuple(t['box'])
        if key in seen:
            continue
        seen.add(key)
        dedup.append(t)
    return _trim_traces(dedup, level)


def _ocr_questions(gray):
    """没有视觉模型时，用本地 OCR 把卷面拆成题目（题号 / 题干 / 选项 / 类型）。"""
    lines = _ocr_lines(gray)
    if not lines:
        return []
    first = min(ln['y'] for ln in lines)
    last = max(ln['y'] for ln in lines)
    blocks, cur = [], None
    for ln in lines:
        text = (ln.get('text') or '').strip()
        if not text:
            continue
        m = _Q_START.match(text)
        qn = int(m.group(1)) if m else None
        if qn is not None and 1 <= qn <= 80:
            if cur:
                blocks.append(cur)
            cur = {'q': qn, 'texts': [text], 'y': ln['y'],
                   'x0': ln['x0'], 'x1': ln['x1']}
        elif cur is not None:
            # 只收同一题范围内的后续行，避免把下一段页脚算进来
            if ln['y'] - cur['y'] <= 0.14:
                cur['texts'].append(text)
                cur['x0'] = min(cur['x0'], ln['x0'])
                cur['x1'] = max(cur['x1'], ln['x1'])
    if cur:
        blocks.append(cur)
    # 去掉页眉页脚里误识别成题号的块
    blocks = [b for b in blocks
              if b['y'] > first + 0.01 and b['y'] < last - 0.005]
    out, seen = [], set()
    for b in blocks:
        if b['q'] in seen:
            continue
        seen.add(b['q'])
        joined = ' '.join(b['texts'])[:500]
        has_opt = bool(re.search(r'[（(]\s*[A-DＡ-Ｄ]\s*[)）]', joined))
        has_blank = bool(re.search(r'_{2,}|[（(]\s*[)）]', joined))
        if has_opt:
            typ = 'choice'
        elif re.search(r'证明|计算|解\s*[：:]|求\s*[：:]', joined) and not has_blank:
            typ = 'work'
        else:
            typ = 'fill'
        out.append({
            'q': b['q'], 'type': typ, 'stem': joined,
            'options': re.findall(r'[（(]\s*([A-DＡ-Ｄ])\s*[)）]\s*([^\s（(]{0,20})', joined),
            'y': round(b['y'], 4),
            'area': {'x': round(b['x0'], 4), 'y': round(b['y'], 4),
                     'w': round(max(0.1, b['x1'] - b['x0']), 4), 'h': 0.05}
        })
    return out


def _doc_font_path():
    for p in ('C:/Windows/Fonts/msyh.ttc', 'C:/Windows/Fonts/simsun.ttc',
              str(TOOL_DIR / 'fonts' / 'xiongdi.ttf')):
        if Path(p).exists():
            return p
    return None


def _enhance_gray(gray):
    """去阴影、提白、增强：只用于 OCR，坐标与 scan.png 完全一致。"""
    bg = cv2.GaussianBlur(gray, (0, 0), 25)
    norm = cv2.divide(gray, bg, scale=255)
    doc = cv2.convertScaleAbs(norm, alpha=1.28, beta=-42)
    return cv2.medianBlur(doc, 3)


def _make_document(gray, lines, items, out):
    """生成“扫描文档”：去阴影、提白、增强，不改任何位置。

    与 scan.png 同尺寸同坐标，所以版面与照片完全一致，
    后续答题框可以原样盖回，不会出现“重排后对不上”的问题。
    """
    bgr = cv2.cvtColor(_enhance_gray(gray), cv2.COLOR_GRAY2BGR)
    cv2.imencode('.png', bgr)[1].tofile(str(out))


_QNUM_RE = re.compile(r'^\s*(?:[（(]\s*(\d{1,2})\s*[)）]|(\d{1,2})\s*[.、．])')
_OPT_RE = re.compile(r'\s*[（(]\s*[A-Ea-e]\s*[)）]')


def _qnum(text):
    m = _QNUM_RE.match(text or '')
    if not m:
        return None
    return int(m.group(1) or m.group(2))


def _qnum_line(lines, qn, y):
    cands = [ln for ln in lines if _qnum(ln['text']) == qn]
    if not cands and qn is not None:
        # 题号被 OCR 并进了同一行的其它文字里（如分段函数那一行）
        pat = re.compile(r'[（(]\s*%d\s*[)）]' % qn)
        cands = [ln for ln in lines
                 if pat.search(ln['text']) and abs(ln['y'] - y) <= 0.06]
    # 题号会在每个大题里重复（1. / (1)），离当前题太远的不能要
    cands = [ln for ln in cands if abs(float(ln['y']) - y) <= 0.09]
    return min(cands, key=lambda l: abs(l['y'] - y)) if cands else None


def _q_region(lines, start):
    """题号行到下一题号/第一个选项之间的行。"""
    y0 = start['y'] - 0.022
    y1 = start['y'] + 0.064
    for ln in lines:
        if ln['y'] <= start['y'] + 0.004:
            continue
        if _qnum(ln['text']) is not None or _OPT_RE.search(ln['text']):
            y1 = min(y1, ln['y'] - 0.004)
            break
    return [ln for ln in lines if y0 <= ln['y'] <= y1]


def _ocr_choice_box(gray, y, lines, qn=None):
    """在 OCR 定位到的题目区域内，取最靠右的空白括号对。"""
    H = gray.shape[0]
    start = _qnum_line(lines, qn, y) if qn is not None else None
    if start is not None:
        win = _q_region(lines, start)
    else:
        win = [ln for ln in lines if abs(ln['y'] - y) <= 0.055]
        marked = [ln for ln in win
                  if not _OPT_RE.match(ln['text'])
                  and ('（）' in ln['text'] or '()' in ln['text'])
                  and ln['text'].rstrip().rstrip('．.。,，；;、').endswith(('）', ')'))]
        if marked:
            win = sorted(marked, key=lambda l: abs(l['y'] - y))[:1]
    if not win:
        return None
    best = None
    # 题干以“（”结尾时，开括号本身就是答案位置的强信号
    for ln in win:
        if (ln['text'] or '').rstrip().endswith(('（', '(')):
            p = {'x0': ln['x1'] - 0.004,
                 'x1': min(0.95, ln['x1'] + 0.075),
                 'cy': ln['y'], 'h': ln.get('h', 0.02)}
            key = (p['x0'], 0.0)
            if best is None or key > best[0]:
                best = (key, p)
    for ln in win:
        pairs = []
        for oy in (0.0, -0.008, 0.008, -0.016, 0.016):
            for p in align._choice_pairs(gray, ln['y'] + oy, 0.012, 0.03):
                if p['h'] * H >= 14:
                    pairs.append(p)
        for it in ln['items']:
            if _OPT_RE.match(it['text']):
                continue
            for p in pairs:
                # 括号必须紧跟在“这段文字”的末尾，避免吃到同行的选项/公式括号
                if abs(p['cy'] - ln['y']) > 0.014:
                    continue
                # 括号必须正好套住或紧接“这段文字”的结尾
                if not (p['x0'] - 0.02 <= it['x1'] <= p['x1'] + 0.05):
                    continue
                key = (p['x0'], -abs(ln['y'] - (start['y'] if start else y)))
                if best is None or key > best[0]:
                    best = (key, p)
    if best:
        p = best[1]
        # 行尾还有明显更靠右的墨迹（答题括号），说明刚才那一对只是公式括号
        end = _line_end_box(gray, p['cy'], 'choice')
        if end and end[2] > p['x1'] + 0.06:
            return [round(v, 4) for v in end]
        return [round(p['x0'] - 0.004, 4), round(p['cy'] - 0.014, 4),
                round(p['x1'] + 0.004, 4), round(p['cy'] + 0.014, 4)]
    ln = max(win, key=lambda l: (l['x1'], -abs(l['y'] - y)))
    # 没找到成对括号：以 OCR 行中心为准，取该行最右墨迹（通常是答题括号）
    b = _line_end_box(gray, ln['y'], 'choice')
    if b:
        return [round(v, 4) for v in b]
    yb = ln['y']
    return [round(max(0.03, ln['x1'] - 0.075), 4), round(yb - 0.014, 4),
            round(ln['x1'] - 0.005, 4), round(yb + 0.014, 4)]


def _ocr_fill_box(gray, y, lines, qn=None):
    """在 OCR 定位到的填空行上，找紧贴文字右侧（或左侧）的答题横线。"""
    start = _qnum_line(lines, qn, y) if qn is not None else None
    if start is not None:
        # 题干换行时空白常在题号行的下一行，向下多给一点窗口
        win = [ln for ln in lines if -0.012 <= ln['y'] - start['y'] <= 0.036]
    else:
        win = [ln for ln in lines if abs(ln['y'] - y) <= 0.032]
    if not win:
        return None
    best = None
    for ln in win:
        for u in align._morph_underlines(gray, ln['y'], 0.018, max_w=0.30):
            if u['x1'] - u['x0'] < 0.03:
                continue
            near_end = u['x0'] >= ln['x1'] - 0.06
            near_text = any(abs(u['x1'] - it['x0']) <= 0.03 for it in ln['items'])
            if not (near_end or near_text):
                continue
            # 同一行可能有多条线（分式横线、根号线）：答题横线在最右边
            key = (u['x0'], -abs(u['cy'] - y))
            if best is None or key > best[0]:
                best = (key, u)
    if best:
        u = best[1]
        return [round(u['x0'], 4), round(u['cy'] - 0.030, 4),
                round(u['x1'], 4), round(u['cy'], 4)]
    ln = min(win, key=lambda l: abs(l['y'] - y))
    gap = None
    for a, b in zip(ln['items'], ln['items'][1:]):
        w = b['x0'] - a['x1']
        if 0.04 <= w <= 0.30 and (gap is None or w > gap[0]):
            gap = (w, a['x1'], b['x0'])
    if gap:
        return [round(gap[1], 4), round(ln['y'] - 0.026, 4),
                round(gap[2], 4), round(ln['y'] + 0.004, 4)]
    return None


def _num(v, d):
    """把模型给的任意值安全转成有限浮点数，坏值一律用默认值。"""
    try:
        f = float(v)
        return f if np.isfinite(f) else d
    except (TypeError, ValueError):
        return d


def _sanitize(questions):
    """把模型可能给出的 '?'、None 等脏坐标清成合法数字，避免定位阶段崩掉。"""
    for q in questions:
        y = _num(q.get('y'), None)
        q['y'] = y if y is not None else 0.5
        if q.get('x') is not None:
            x = _num(q.get('x'), None)
            if x is None:
                q.pop('x', None)
            else:
                q['x'] = x
        a = q.get('area')
        if isinstance(a, dict):
            for k in ('x', 'y', 'w', 'h', 'size'):
                if k in a:
                    v = _num(a.get(k), None)
                    if v is None:
                        a.pop(k, None)
                    else:
                        a[k] = v
    return questions


def _fallback_items(questions):
    """定位失败时的兜底框：保证第一轮仍能返回扫描件和可拖动框。"""
    out = []
    for q in questions:
        cy = _num(q.get('y'), 0.5)
        cx = _num(q.get('x'), 0.6)
        if (q.get('type') or '').lower() == 'work':
            a = q.get('area') or {}
            ax, ay = _num(a.get('x'), 0.07), _num(a.get('y'), min(0.94, cy + 0.03))
            aw, ah = _num(a.get('w'), 0.86), _num(a.get('h'), 0.16)
            box = [ax, ay, min(0.98, ax + aw), min(0.97, ay + ah)]
            out.append({**q, 'y': box[1], 'x': box[0], 'w': round(box[2] - box[0], 4),
                        'size': _num(a.get('size'), 30), 'box': box})
        else:
            out.append({**q, 'y': cy, 'x': cx,
                        'box': [round(cx - 0.035, 4), round(cy - 0.014, 4),
                                round(cx + 0.035, 4), round(cy + 0.014, 4)]})
    return out


def _right_ink_extent(gray, cy, x1, half=0.009, span=0.30):
    """括号右侧还剩多少墨迹：答题括号后面基本没字，选项/公式括号后面还有正文。"""
    H, W = gray.shape
    y0 = max(0, int((cy - half) * H))
    y1 = min(H, int((cy + half) * H))
    xa = int(min(W - 2, (x1 + 0.003) * W))
    xb = int(min(0.90 * W, xa + span * W))      # 右侧页边是暗的，不能算正文
    if y1 - y0 < 4 or xb - xa < 8:
        return 0.0
    cols = (gray[y0:y1, xa:xb] < 118).sum(0)
    idx = [i for i, c in enumerate(cols) if c >= 3]
    return round((idx[-1] + xa) / W - x1, 4) if idx else 0.0


def _answer_slots(gray):
    """整页找“答题括号”：行尾、内部空白、右侧几乎没有正文。"""
    H, W = gray.shape
    raw = []
    y = 0.05
    while y <= 0.95:
        for p in align._choice_pairs(gray, float(y), 0.012, 0.02):
            if p['h'] * H < 15:
                continue
            if not (0.030 <= p['x1'] - p['x0'] <= 0.095):
                continue
            if p['x0'] < 0.12 or p['x1'] > 0.90:
                continue
            if _right_ink_extent(gray, p['cy'], p['x1']) > 0.05:
                continue
            raw.append(dict(p))
        y += 0.004
    raw.sort(key=lambda p: (p['cy'], p['x0']))
    uniq = []
    for p in raw:
        merged = False
        for q in uniq:
            if abs(q['cy'] - p['cy']) > 0.006:
                continue
            ov = min(q['x1'], p['x1']) - max(q['x0'], p['x0'])
            if ov >= 0.65 * min(q['x1'] - q['x0'], p['x1'] - p['x0']):
                if (p['h'], -p['x0']) > (q['h'], -q['x0']):
                    q.update(p)
                merged = True
                break
        if not merged:
            uniq.append(p)
    return uniq


def _choice_stem_y(q, lines, yh):
    """找这道选择题题干行：模型给的 y 只是粗先验，题号行才靠得住。"""
    try:
        qn = int(str(q.get('q')).strip())
    except (TypeError, ValueError):
        qn = None
    ln = _qnum_line(lines, qn, yh) if lines else None
    if ln is not None:
        return float(ln['y'])
    cand = [l for l in (lines or [])
            if not _OPT_RE.match(l.get('text') or '')
            and abs(float(l['y']) - yh) <= 0.05]
    if cand:
        return float(min(cand, key=lambda l: abs(float(l['y']) - yh))['y'])
    return yh


def _assign_choice_slots(ch_q, pairs, lines=None):
    """按题目顺序单调配括号：同一行取最右一对；没配到的题留空，交给兜底。"""
    bands = []
    for p in sorted(pairs, key=lambda t: t['cy']):
        if bands and abs(bands[-1]['cy'] - p['cy']) <= 0.012:
            if p['x0'] > bands[-1]['x0']:
                bands[-1] = p
        else:
            bands.append(p)
    picked, used, last = {}, set(), -1.0
    for q in ch_q:
        yh = _num(q.get('y'), 0.5)
        sy = _choice_stem_y(q, lines, yh)
        best = None
        for i, p in enumerate(bands):
            if i in used or p['cy'] <= last + 0.006:
                continue
            # 答题括号必须落在题干行上（允许倾斜），不能跑到选项行
            if not (sy - 0.018 <= p['cy'] <= sy + 0.042):
                continue
            d = abs(p['cy'] - sy)
            if best is None or d < best[0] - 1e-6 or (
                    abs(d - best[0]) <= 0.008 and p['x0'] > bands[best[1]]['x0']):
                best = (d, i)
        if best:
            p = bands[best[1]]
            picked[id(q)] = p
            used.add(best[1])
            last = p['cy']
    return picked


def _blank_count(stem):
    s = stem or ''
    n = len(re.findall(r'_{2,}', s))
    if n:
        return n
    return max(1, len(re.findall(r'[（(]\s*[)）]', s)))


def _assign_fill_slots(fl_q, unders):
    """按题目顺序单调配横线；一题多个空时取同一行相邻的几条。"""
    picked, last = {}, -1.0
    for q in fl_q:
        yh = _num(q.get('y'), 0.5)
        n = _blank_count(q.get('stem'))
        cand = [u for u in unders
                if u['cy'] > last + 0.006 and yh - 0.10 <= u['cy'] <= yh + 0.10]
        cand.sort(key=lambda u: (abs(u['cy'] - yh), u['x0']))
        if not cand:
            continue
        first = cand[0]
        group = [u for u in cand if abs(u['cy'] - first['cy']) <= 0.014]
        group.sort(key=lambda u: u['x0'])
        group = group[:max(1, n)]
        picked[id(q)] = group
        last = max(u['cy'] for u in group)
    return picked


def _free_right(gray, y0, y1, x_start, x_stop):
    """横线上方从 x_start 到 x_stop 是否是空白，返回可安全延伸到的 x。"""
    if gray is None:
        return x_stop
    H, W = gray.shape
    ya, yb = int(max(0, y0) * H), int(min(1, y1) * H)
    xa, xb = int(max(0, x_start) * W), int(min(1, x_stop) * W)
    if yb - ya < 4 or xb - xa < 4:
        return x_start
    cols = (gray[ya:yb, xa:xb] < 118).sum(0)
    for i, c in enumerate(cols):
        if c >= 2:
            return max(x_start, (xa + i) / W - 0.004)
    return x_stop


def _extend_fill_box(gray, box, text):
    """答案比横线长时向右借空白，避免字号被压成小字；借不到就保持原框。"""
    if gray is None or not text:
        return box
    x0, y0, x1, y1 = [float(v) for v in box]
    want = min(0.30, max(x1 - x0, 0.011 * len(text)))
    if want <= x1 - x0 + 0.004:
        return box
    stop = _free_right(gray, y0, y1 - 0.010, x1, min(0.93, x0 + want))
    if stop <= x1 + 0.004:
        return box
    return [x0, y0, round(stop, 4), y1]


def _plan_items(questions, ch_rows, fl_rows, gray=None, ocr_lines=None):
    """把转写结果 + 版面检测合成“作答位置计划”，供第一轮确认。

    位置不依赖模型目测的 y：选择题找“行尾空白括号”，填空题找“答题横线”，
    再按题目顺序单调配对；题干没有空档才退回“题号行下方写答案”。
    """
    ch_q = [q for q in questions if (q.get('type') or '').lower() == 'choice']
    fl_q = [q for q in questions if (q.get('type') or '').lower() in ('fill', 'fraction')]
    ch_slots, fl_slots = {}, {}
    if gray is not None:
        try:
            ch_slots = _assign_choice_slots(ch_q, _answer_slots(gray), ocr_lines)
        except Exception as e:
            print(f'[plan] 括号定位失败：{e}')
        try:
            unds = [c for c in (fl_rows or []) if c['cy'] <= 0.95]
            fl_slots = _assign_fill_slots(fl_q, unds)
        except Exception as e:
            print(f'[plan] 横线定位失败：{e}')
    items = []
    for q in questions:
        typ = (q.get('type') or 'fill').lower()
        y_hint = _num(q.get('y'), 0.5)
        x_hint = _num(q.get('x'), None) if q.get('x') is not None else None
        try:
            qn = int(str(q.get('q')).strip())
        except (TypeError, ValueError):
            qn = None
        if typ == 'work':
            area = q.get('area') or {}
            ax = _num(area.get('x'), 0.07)
            ay = _num(area.get('y'), min(0.94, y_hint + 0.03))
            aw = _num(area.get('w'), 0.86)
            ah = _num(area.get('h'), 0.20)
            ah = min(ah, max(0.03, 0.97 - ay))      # 不能超出页面底部
            box = [round(ax, 4), round(ay, 4),
                   round(min(0.98, ax + aw), 4), round(min(0.97, ay + ah), 4)]
            # 页面底部一行并列几道小题：只剩不到 6% 屏高，写整段过程必压下一题
            compact = (box[3] - box[1]) < 0.06
            items.append({**q, 'y': box[1], 'x': box[0],
                          'w': round(box[2] - box[0], 4),
                          'size': _num(area.get('size'), 30), 'box': box,
                          'compact': compact})
            continue
        if typ == 'choice':
            slot = ch_slots.get(id(q))
            if slot is not None:
                box = [round(slot['x0'] - 0.006, 4), round(slot['cy'] - 0.014, 4),
                       round(slot['x1'] + 0.006, 4), round(slot['cy'] + 0.014, 4)]
            else:
                ln = _qnum_line(ocr_lines, qn, y_hint) if ocr_lines else None
                if ln is None and ocr_lines:
                    ln = _question_line(ocr_lines, y_hint, None)
                if ln is not None:
                    yy = round(float(ln['y']) + 0.014, 4)
                    box = [round(max(0.12, float(ln['x1']) - 0.10), 4), yy,
                           round(min(0.95, float(ln['x1']) - 0.005), 4), round(yy + 0.030, 4)]
                else:
                    cx = x_hint if x_hint is not None else 0.6
                    box = [round(cx - 0.03, 4), round(y_hint - 0.015, 4),
                           round(cx + 0.03, 4), round(y_hint + 0.015, 4)]
        else:
            grp = fl_slots.get(id(q))
            if grp:
                blanks = [[round(u['x0'] - 0.006, 4), round(u['cy'] - 0.028, 4),
                           round(u['x1'] + 0.006, 4), round(u['cy'] + 0.004, 4)]
                          for u in grp]
                box = [min(b[0] for b in blanks), min(b[1] for b in blanks),
                       max(b[2] for b in blanks), max(b[3] for b in blanks)]
                # 同一行多个空：合成一条长答案框，让整段答案连写（和人工一致）
                if len(blanks) > 1 and max(b[0] for b in blanks) - min(b[2] for b in blanks) < 0.30:
                    blanks = [box]
            else:
                box = None
                ln = _qnum_line(ocr_lines, qn, y_hint) if ocr_lines else None
                if ln is not None:
                    yy = round(float(ln['y']) + 0.028, 4)
                    box = [round(max(0.12, float(ln['x1']) - 0.12), 4), yy,
                           round(min(0.95, float(ln['x1'])), 4), round(yy + 0.030, 4)]
                if box is None:
                    b = _snap_fill_box(gray, y_hint, x_hint) if gray is not None else None
                    if b is None and gray is not None:
                        b = _line_end_box(gray, y_hint, 'fill')
                    if b is not None:
                        box = b
                    else:
                        cx = x_hint if x_hint is not None else 0.6
                        box = [round(cx - 0.045, 4), round(y_hint - 0.022, 4),
                               round(cx + 0.045, 4), round(y_hint + 0.008, 4)]
                blanks = [box]
            box = [round(float(v), 4) for v in box]
            y = round((box[1] + box[3]) / 2, 4)
            x = round((box[0] + box[2]) / 2, 4)
            items.append({**q, 'y': y, 'x': x, 'box': box,
                          'blanks': blanks if len(blanks) > 1 else None})
            continue
        y = round((box[1] + box[3]) / 2, 4)
        x = round((box[0] + box[2]) / 2, 4)
        items.append({**q, 'y': y, 'x': x, 'box': box})
    return items


def _draw_plan(image, items, out_png):
    im = Image.open(image).convert('RGB')
    d = ImageDraw.Draw(im)
    W, H = im.size
    try:
        font = ImageFont.truetype('arial.ttf', 30)
    except Exception:
        font = ImageFont.load_default()
    colors = {'choice': (30, 90, 220), 'fill': (220, 60, 40),
              'fraction': (220, 60, 40), 'work': (20, 150, 90)}
    for it in items:
        x0, y0, x1, y1 = it['box']
        c = colors.get(it.get('type'), (120, 120, 120))
        d.rectangle([x0 * W, y0 * H, x1 * W, y1 * H], outline=c, width=3)
        d.text((x0 * W + 4, max(0, y0 * H - 34)),
               f"Q{it.get('q')} {it.get('type')}", font=font, fill=c)
    im.save(out_png)


def _make_scan(image, ws):
    """扫描件保持原图方向；另存一份“文字横向”的工作图只用于检测。

    返回 (scan.png, work.png, kind)；kind 是工作图相对原图的旋转：
    'cw'=顺时针 90°，'ccw'=逆时针 90°，None=未旋转。
    """
    bgr = cv2.imdecode(np.fromfile(str(image), dtype=np.uint8), cv2.IMREAD_COLOR)
    if bgr is None:
        raise RuntimeError('图片读取失败')
    scan = ws / 'scan.png'
    cv2.imencode('.png', bgr)[1].tofile(str(scan))

    # 不做自动旋转：原图方向和视觉模型看到的坐标一致，
    # 页面上有竖向表格线时，按投影方差判断方向会误判成横版。
    work, kind = bgr, None
    work_path = ws / 'work.png'
    cv2.imencode('.png', work)[1].tofile(str(work_path))
    return scan, work_path, kind


def _map_box_back(box, kind):
    """把工作图归一化坐标映射回原图归一化坐标。"""
    x0, y0, x1, y1 = box
    if kind == 'cw':
        return [y0, 1 - x1, y1, 1 - x0]
    if kind == 'ccw':
        return [1 - y1, x0, 1 - y0, x1]
    return [x0, y0, x1, y1]


def run_scan(image, task, workspace):
    """第一轮：只扫描定位，输出 plan.json + 框选图，不落笔。"""
    if not image:
        print('缺少 --image')
        return 2
    vision = _vision_cfg()
    if not (vision.get('base') and vision.get('model')):
        print('[scan] 未配置视觉模型，改用本地 OCR')
    ws = Path(workspace).resolve()
    ws.mkdir(parents=True, exist_ok=True)
    print('[scan] 1/4 生成扫描件')
    scan, work, kind = _make_scan(image, ws)
    print(f'[scan] 扫描件完成：{scan.name}')
    gray = cv2.imdecode(np.fromfile(str(work), dtype=np.uint8), cv2.IMREAD_GRAYSCALE)

    print('[scan] 2/4 整页转写')
    data = None
    if vision.get('base') and vision.get('model') and vision.get('key'):
        data = _vision_json(image, task, vision)
    if data:
        questions = _sanitize(_normalize_types(data.get('questions') or []))
    else:
        print('[scan] 视觉模型不可用，改用本地 OCR 提取题目')
        questions = _sanitize(_normalize_types(_ocr_questions(_enhance_gray(gray))))
    _drop_bad_x(questions)
    if not questions:
        print('[scan] 没有识别到题目')
        return 1
    print(f"[scan] 识别到 {len(questions)} 题")
    print('[scan] 3/4 扫描答题位置')
    try:
        ch_rows, fl_rows = _scan_layout_gray(gray)
    except Exception as e:
        print(f'[scan] 版面检测失败，跳过：{e}')
        ch_rows, fl_rows = [], []
    try:
        ocr_lines = _ocr_lines(_enhance_gray(gray))
    except Exception as e:
        print(f'[scan] OCR 失败，跳过：{e}')
        ocr_lines = None
    print(f'[scan] OCR 行数：{0 if not ocr_lines else len(ocr_lines)}')
    try:
        items = _plan_items(questions, ch_rows, fl_rows, gray, ocr_lines)
    except Exception as e:
        print(f'[scan] 定位失败，改用兜底框：{e}')
        items = _fallback_items(questions)
    # 工作图上的框映射回原图方向，扫描件保持用户看到的方向
    for it in items:
        it['box'] = _map_box_back(it['box'], kind)
        b = it['box']
        it['area'] = {'x': b[0], 'y': b[1],
                      'w': round(b[2] - b[0], 4), 'h': round(b[3] - b[1], 4)}
        if (it.get('type') or '').lower() == 'work':
            it['x'], it['y'] = b[0], b[1]
            it['w'] = round(b[2] - b[0], 4)
        else:
            it['x'] = (b[0] + b[2]) / 2
            it['y'] = (b[1] + b[3]) / 2
    plan = {'image': Path(image).name, 'scan': scan.name,
            'questions': questions, 'items': items}
    (ws / 'plan.json').write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding='utf-8')
    doc_png = ws / 'document.png'
    try:
        _make_document(gray, ocr_lines, items, doc_png)
        print(f'[scan] 文档页：{doc_png.name}')
    except Exception as e:
        print(f'[scan] 文档页生成失败：{e}')
    # 导出一份“文档”：每题的文本 + 落笔框，方便核对扫描和定位是否一致
    sh, sw = cv2.imdecode(np.fromfile(str(scan), dtype=np.uint8),
                          cv2.IMREAD_GRAYSCALE).shape[:2]
    doc = {'scan': scan.name, 'doc': doc_png.name if doc_png.exists() else '',
           'size': [sw, sh], 'questions': questions, 'targets': items}
    (ws / 'document.json').write_text(json.dumps(doc, ensure_ascii=False, indent=1),
                                      encoding='utf-8')
    lines_txt = [f"扫描件 {scan.name}  {sw}x{sh}", '']
    for it in items:
        b = it['box']
        stem = re.sub(r'\s+', ' ', str(it.get('stem') or ''))[:60]
        lines_txt.append(f"Q{it.get('q')} [{it.get('type')}] {stem}")
        lines_txt.append(f"    落笔框 [{b[0]:.3f},{b[1]:.3f}] - [{b[2]:.3f},{b[3]:.3f}]")
    (ws / 'document.txt').write_text('\n'.join(lines_txt), encoding='utf-8')
    out = ws / f'{Path(image).stem}_框选图.png'
    try:
        _draw_plan(scan, items, out)
    except Exception as e:
        print(f'[scan] 框选图绘制失败：{e}')
    print(f'[scan] 4/4 完成：{len(items)} 题，框选图 {out.name}')
    return 0


def _split_answer(ans, n):
    """一题多个空时按逗号/分号拆答案；拆不出对应份数就整段放在第一个空。"""
    if n <= 1:
        return [ans]
    parts = [p.strip() for p in re.split(r'[，,；;]\s*', ans) if p.strip()]
    return parts if len(parts) == n else [ans]


def _commands_from_plan(items, solutions, gray=None):
    commands = []
    for i, it in enumerate(items):
        if it.get('skip'):
            continue
        s = solutions[i] if i < len(solutions) else {}
        typ = (it.get('type') or 'fill').lower()
        q = it.get('q')
        if typ == 'work':
            wbox = it.get('box') or []
            if it.get('compact'):
                continue
            lines = s.get('lines') or []
            lines = [_normalize_math(x) for x in lines if str(x).strip()]
            # 教材页一行并列三道小题时，单题高度不足 5% 屏高，写整段过程必压下一题
            if len(wbox) == 4 and (float(wbox[3]) - float(wbox[1])) < 0.05:
                continue
            if not lines:
                continue
            commands.append({'q': q, 'type': 'work',
                             'x': float(it.get('x', 0.07)), 'y': float(it.get('y', 0.3)),
                             'w': float(it.get('w', 0.86)), 'size': float(it.get('size', 30)),
                             'lines': lines})
        elif typ == 'choice':
            m = re.search(r'[A-Da-d]', _normalize_math(s.get('answer', '')))
            if not m:
                continue
            commands.append({'q': q, 'type': 'choice', 'text': m.group().upper(),
                             'y': float(it.get('y', 0.5)),
                             'lock': [float(v) for v in it['box']]})
        else:
            ans = _normalize_math(s.get('answer', ''))
            ans = re.sub(r'\balpha\b', 'α', ans)
            ans = re.sub(r'\bbeta\b', 'β', ans)
            ans = ans.replace('>=', '≥').replace('<=', '≤')
            if not ans:
                continue
            blanks = it.get('blanks') or ([it['box']] if it.get('box') else [])
            parts = _split_answer(ans, len(blanks)) if blanks else [ans]
            if len(parts) != len(blanks):
                blanks = [it.get('box')] if it.get('box') else []
            for bi, part in enumerate(parts):
                box = blanks[bi] if bi < len(blanks) else None
                if box:
                    box = _extend_fill_box(gray, box, part)
                frac = _as_fraction(part)
                cy = ((box[1] + box[3]) / 2) if box else float(it.get('y', 0.5))
                cmd = {'q': q, 'type': 'fraction', 'num': frac[0], 'den': frac[1],
                       'y': round(cy, 4)} if frac else \
                      {'q': q, 'type': 'fill', 'text': part, 'y': round(cy, 4)}
                if frac and box and (box[2] - box[0]) < 0.12:
                    # 教材页行距密，分数不能按默认 50 号，否则会压到上一行
                    cmd['size'] = 30
                if box:
                    cmd['lock'] = [float(v) for v in box]
                elif it.get('x') is not None:
                    cmd['x'] = float(it['x'])
                if len(parts) > 1:
                    cmd['q'] = q
                commands.append(cmd)
    return {'style': 'pen', 'items': commands}


def run_render(image, task, workspace, retries=2, trace='more'):
    """第二轮：严格按 plan.json 里确认过的位置解题、落笔。"""
    ws = Path(workspace).resolve()
    p = ws / 'plan.json'
    if not p.exists():
        print('[render] 缺少 plan.json，请先做第一轮扫描')
        return 2
    plan = json.loads(p.read_text(encoding='utf-8'))
    questions = plan.get('questions') or []
    items = plan.get('items') or []
    if not questions or not items:
        print('[render] plan.json 不完整')
        return 1
    print(f'[render] 按确认位置作答：{len(items)} 题')
    stem = Path(image).stem
    out_png = ws / f'{stem}_作答_API.png'
    calib = ws / f'{stem}_校准_API.png'
    render_img = ws / plan.get('scan', 'scan.png')
    if not render_img.exists():
        render_img = Path(image)
    gray2 = None
    try:
        gray2 = cv2.imdecode(np.fromfile(str(render_img), dtype=np.uint8),
                             cv2.IMREAD_GRAYSCALE)
    except Exception:
        gray2 = None
    solutions = _solve(questions)
    alt = _solve(questions, alt=True)
    solutions = _reconcile(questions, solutions, alt)
    solutions = _audit_bounds(questions, solutions)
    solutions = _ensure_work(questions, solutions)
    commands = _commands_from_plan(items, solutions, gray2)
    if plan.get('profile'):
        commands['profile'] = plan['profile']
    trace_level = plan.get('trace') or trace or 'more'
    if trace_level != 'none' and plan.get('traces', True):
        try:
            tr = _auto_traces(gray2, items, solutions, trace_level)
            if tr:
                commands['traces'] = tr
                print(f'[render] 做题痕迹：{len(tr)} 条（{trace_level}）')
        except Exception as e:
            print(f'[render] 做题痕迹生成失败：{type(e).__name__}: {e}')
    if not commands['items']:
        print('[render] 没有可落笔的答案')
        return 1
    last_rep = []
    for attempt in range(retries + 1):
        r, rep = _render(render_img, ws, commands, out_png, calib, no_align=True)
        if rep is None:
            print('[render] 渲染报告解析失败：' + (r.stderr or r.stdout)[-400:])
            return 1
        items_rep = rep.get('items') or []
        last_rep = items_rep
        for it in items_rep:
            d = it.get('drift') or [0, 0]
            print(f"[render] q{it.get('q')} {it.get('type')} ok={it.get('ok')} "
                  f"size={it.get('size')} drift=({d[0]},{d[1]})")
        bad = [it for it in items_rep if not it.get('ok')]
        if not bad:
            _write_layout(ws, render_img, commands, items_rep)
            print(f'[render] 完成，全部通过：{out_png}')
            return 0
        if attempt >= retries:
            break
        bad_idx = {i for i, it in enumerate(items_rep) if not it.get('ok')}
        for i, it in enumerate(commands['items']):
            if i not in bad_idx or i >= len(items_rep):
                continue
            d = items_rep[i].get('drift') or [0, 0]
            step = float(d[1]) if abs(float(d[1])) > 1e-4 else (0.010 if attempt == 0 else -0.008)
            lock = it.get('lock')
            if lock:
                # 锁定的框跟着移动，不能只改 y（锁定后 y 不参与定位）
                b = [float(v) for v in lock]
                b[1] = max(0.0, b[1] + step)
                b[3] = max(b[1] + 0.01, b[3] + step)
                it['lock'] = b
                it['y'] = round((b[1] + b[3]) / 2, 4)
            else:
                it['y'] = round(max(0.02, min(0.97, float(it['y']) + step)), 4)
    print(f'[render] 仍有未通过项，作答图已保留：{out_png}')
    _write_layout(ws, render_img, commands, last_rep)
    return 1


def _write_layout(ws, render_img, commands, report):
    """把每题墨迹的最终位置写成可编辑布局，供网页拖拽/缩放后二次合成。"""
    try:
        _write_layout_impl(ws, render_img, commands, report)
    except Exception as e:
        print(f'[render] 写布局失败：{type(e).__name__}: {e}')


def _write_layout_impl(ws, render_img, commands, report):
    try:
        im = cv2.imdecode(np.fromfile(str(render_img), dtype=np.uint8),
                          cv2.IMREAD_GRAYSCALE)
        H, W = im.shape[:2]
    except Exception:
        return
    items = []
    cmds = commands.get('items') or []
    for idx, cmd in enumerate(cmds):
        rep = report[idx] if idx < len(report) else None
        ink = (rep or {}).get('ink') or (rep or {}).get('blank')
        if ink:
            x0, y0, x1, y1 = [float(v) for v in ink]
            box = [max(0.0, min(1.0, x0 / W)), max(0.0, min(1.0, y0 / H)),
                   max(0.0, min(1.0, x1 / W)), max(0.0, min(1.0, y1 / H))]
        else:
            b = cmd.get('lock')
            if isinstance(b, list) and len(b) == 4:
                box = [float(v) for v in b]
            elif (cmd.get('type') or '') == 'work':
                x = float(cmd.get('x', 0.07)); y = float(cmd.get('y', 0.3))
                box = [x, y, min(1.0, x + float(cmd.get('w', 0.8))),
                       min(1.0, y + 0.18)]
            else:
                continue
        box = [round(max(0.0, min(1.0, v)), 4) for v in box]
        if box[2] <= box[0] or box[3] <= box[1]:
            continue
        items.append({'q': cmd.get('q'), 'type': cmd.get('type'), 'box': box,
                      'size': (rep or {}).get('size') or cmd.get('size'),
                      'text': cmd.get('text'), 'num': cmd.get('num'),
                      'den': cmd.get('den'), 'lines': cmd.get('lines')})
    data = {'size': [W, H], 'style': commands.get('style', 'pen'), 'items': items,
            'profile': commands.get('profile'),
            'traces': commands.get('traces') or []}
    (Path(ws) / 'layout.json').write_text(
        json.dumps(data, ensure_ascii=False, indent=1), encoding='utf-8')


def run_compose(image, task, workspace, trace='more'):
    """定稿：只按网页上确认过的布局重新落笔，不调用任何模型。"""
    ws = Path(workspace).resolve()
    lp = ws / 'layout.json'
    if not lp.exists():
        print('[compose] 缺少 layout.json')
        return 2
    layout = json.loads(lp.read_text(encoding='utf-8'))
    cmds = []
    for it in layout.get('items') or []:
        typ = (it.get('type') or 'fill').lower()
        box = [float(v) for v in (it.get('box') or [])]
        if len(box) != 4 or box[2] <= box[0] or box[3] <= box[1]:
            continue
        if typ == 'work':
            lines = it.get('lines') or []
            if not lines:
                continue
            cmds.append({'q': it.get('q'), 'type': 'work',
                         'x': round(box[0], 4), 'y': round(box[1], 4),
                         'w': round(max(0.05, box[2] - box[0]), 4),
                         'size': float(it.get('size') or 30), 'lines': lines})
            continue
        cmd = {'q': it.get('q'), 'type': typ,
               'y': round((box[1] + box[3]) / 2, 4),
               'lock': [round(v, 4) for v in box]}
        if typ == 'choice':
            cmd['text'] = it.get('text') or ''
        elif typ == 'fraction':
            cmd['num'] = it.get('num') or '1'
            cmd['den'] = it.get('den') or '1'
        else:
            cmd['text'] = it.get('text') or ''
        if it.get('size'):
            cmd['size'] = float(it['size'])
        cmds.append(cmd)
    if not cmds:
        print('[compose] 布局里没有可落笔项')
        return 1
    commands = {'style': layout.get('style', 'pen'), 'items': cmds}
    if layout.get('profile'):
        commands['profile'] = layout['profile']
    if layout.get('traces'):
        tr = _trim_traces(layout['traces'], trace)
        if tr:
            commands['traces'] = tr
    base = ws / 'scan.png'
    if not base.exists():
        base = Path(image)
    stem = Path(image).stem
    out_png = ws / f'{stem}_定稿.png'
    calib = ws / f'{stem}_定稿_校准.png'
    r, rep = _render(base, ws, commands, out_png, calib,
                     no_align=True, rectify_off=True)
    if rep is None:
        print('[compose] 渲染失败：' + (r.stderr or r.stdout)[-400:])
        return 1
    bad = [x.get('q') for x in rep.get('items') or [] if not x.get('ok')]
    print(f"[compose] 完成：{len(rep.get('items') or [])} 项"
          + ('' if not bad else f'，未通过 {bad}'))
    return 0


def run_agent(image, task, workspace, max_steps=20):
    base_url = os.getenv('LLM_BASE_URL', '')
    api_key = os.getenv('LLM_API_KEY', '')
    model = os.getenv('LLM_MODEL', '')
    if not (base_url and api_key and model):
        print('缺少 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL。')
        print('请在 engine/.env 写入三行配置（LLM_BASE_URL / LLM_API_KEY / LLM_MODEL）后重试。')
        return 2
    vision = _vision_cfg()
    img_path = str(Path(image).resolve()) if image else ''
    header = ''
    if img_path:
        header = (f'【试卷原图（唯一可用底图）】{img_path}\n'
                  f'【输出位置】作答图写到 {workspace} 下，文件名 <原图名>_作答_API.png；'
                  f'校准图 <原图名>_校准_API.png；commands.json 也写到 {workspace}。\n'
                  '禁止改用工作目录里的其它试卷图片（例如 目标试卷.jpg），'
                  '所有渲染必须以上面这张原图为底图。\n\n')
    if image and vision.get('model'):
        seen = vision_describe(image, VISION_PROMPT, vision)
        print(f'【视觉识别】{len(seen)} 字符：{seen[:600]}')
        user_content = header + task + '\n\n【视觉模型识别结果】\n' + seen
    elif image:
        user_content = header + task
        user_content = [{'type': 'text', 'text': user_content},
                        {'type': 'image_url', 'image_url': {'url': _image_data_url(image)}}]
    else:
        user_content = task
    messages = [{'role': 'system', 'content': SYSTEM + (
        '\n\n【工作区 AGENTS.md 规范（必须遵守）】\n' + _read_agents_md(workspace)
        if _read_agents_md(workspace) else '')},
                {'role': 'user', 'content': user_content}]
    sig_count = {}
    passed = False
    for step in range(1, max_steps + 1):
        print(f'\n=== step {step} ===')
        msg = chat(messages, base_url, api_key, model)
        if msg.get('tool_calls'):
            messages.append(msg)
            for tc in msg['tool_calls']:
                name = tc['function']['name']
                try:
                    raw = tc['function'].get('arguments')
                    args = raw if isinstance(raw, dict) else json.loads(raw or '{}')
                except json.JSONDecodeError:
                    args = {}
                if passed and name in ('write_text', 'run_command', 'render_paper'):
                    messages.append({'role': 'tool', 'tool_call_id': tc['id'],
                                     'content': '验收已通过（全部 item.ok=true），禁止再修改或重跑，请直接给出最终汇报。'})
                    print(f'-> {name} 已通过，拦截修改')
                    continue
                sig = name + '|' + json.dumps(args, ensure_ascii=False, sort_keys=True)
                sig_count[sig] = sig_count.get(sig, 0) + 1
                if name == 'run_command':
                    lim = 4 if 'auto_render' in str(args.get('command', '')) else 1
                elif name == 'render_paper':
                    lim = 3
                elif name in ('write_text', 'view_image', 'crop_image', 'append_memory'):
                    lim = 999
                else:
                    lim = 1
                if sig_count[sig] > lim:
                    text = ('重复调用已拦截：这条命令刚刚执行过，结果就在上面的对话里。'
                            '不要重复探测或读源码，请立刻用 write_text 写 commands.json，'
                            '然后运行 auto_render.py --calib。')
                    print(f'-> {name} 重复调用，已拦截')
                    messages.append({'role': 'tool', 'tool_call_id': tc['id'], 'content': text})
                    continue
                print(f'-> {name} {json.dumps(args, ensure_ascii=False)[:300]}')
                text, img = execute_tool(name, args, workspace, vision=vision)
                print(f'<- {text[:500]}')
                messages.append({'role': 'tool', 'tool_call_id': tc['id'], 'content': text})
                if (name == 'run_command' and 'auto_render' in str(args.get('command', ''))
                        and _all_ok(text)) or (name == 'render_paper' and '全部通过' in text):
                    passed = True
                    print('*** 工具报告全部 ok:true，判定通过，禁止后续修改 ***')
                    messages.append({'role': 'user', 'content':
                                     '【系统】工具报告 JSON 显示全部 item.ok=true，验收通过。'
                                     '禁止再改 commands.json 或重跑 auto_render.py。'
                                     '最多用 append_memory 记录一次，然后立刻给出最终汇报。'})
                if img:
                    messages.append({'role': 'user', 'content': [
                        {'type': 'text', 'text': f'这是 view_image 返回的图片：{img}'},
                        {'type': 'image_url', 'image_url': {'url': _image_data_url(img)}}]})
        else:
            print('\n=== 最终回复 ===\n' + (msg.get('content') or ''))
            return 0
    print('达到最大步数，未完成。')
    return 1


def dry_run(image, workspace):
    print('1) list_files')
    print(execute_tool('list_files', {'path': '.'}, workspace)[0][:400])
    print('2) run_command')
    print(execute_tool('run_command', {'command': 'python -c "print(1+1)"'}, workspace)[0])
    print('3) view_image')
    print(execute_tool('view_image', {'path': image}, workspace)[0])
    print('dry-run ok')


def main():
    _load_env()
    ap = argparse.ArgumentParser()
    ap.add_argument('--image', default='')
    ap.add_argument('--task', default='识别并作答这张试卷，严格按 AGENTS.md 流程执行。')
    ap.add_argument('--workspace', default=str(DEFAULT_WORKSPACE))
    ap.add_argument('--max-steps', type=int, default=20)
    ap.add_argument('--mode', choices=('fast', 'free'), default='fast',
                    help='fast=一次转写+一次解题+落笔（默认）；free=旧的多步工具循环')
    ap.add_argument('--stage', choices=('full', 'scan', 'render', 'compose'), default='full',
                    help='scan=只扫描定位；render=按 plan.json 落笔；compose=按 layout.json 定稿')
    ap.add_argument('--trace', choices=('none', 'less', 'more'), default='more',
                    help='做题痕迹：none=无，less=少，more=多（默认）')
    ap.add_argument('--dry-run', action='store_true')
    a = ap.parse_args()
    ws = Path(a.workspace).resolve()
    if a.dry_run:
        dry_run(a.image, ws)
        return 0
    if a.stage == 'scan':
        return run_scan(a.image, a.task, ws)
    if a.stage == 'render':
        return run_render(a.image, a.task, ws, trace=a.trace)
    if a.stage == 'compose':
        return run_compose(a.image, a.task, ws, trace=a.trace)
    if a.mode == 'free':
        return run_agent(a.image, a.task, ws, a.max_steps)
    return run_fast(a.image, a.task, ws, trace=a.trace)


if __name__ == '__main__':
    sys.exit(main())
