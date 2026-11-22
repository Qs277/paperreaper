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

import requests
from PIL import Image

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


def run_agent(image, task, workspace, max_steps=20):
    base_url = os.getenv('LLM_BASE_URL', '')
    api_key = os.getenv('LLM_API_KEY', '')
    model = os.getenv('LLM_MODEL', '')
    if not (base_url and api_key and model):
        print('缺少 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL。')
        print(r'请新建 D:\手写生成工具\.env，写入三行配置后重试。')
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
    ap.add_argument('--dry-run', action='store_true')
    a = ap.parse_args()
    ws = Path(a.workspace).resolve()
    if a.dry_run:
        dry_run(a.image, ws)
        return 0
    return run_agent(a.image, a.task, ws, a.max_steps)


if __name__ == '__main__':
    sys.exit(main())
