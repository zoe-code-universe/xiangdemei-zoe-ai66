#!/usr/bin/env python3
"""
火山方舟(ARK)视频生成代理 - 最终修复版
核心变化（v5）：
1. 每段视频生成完成后立即返回ARK公网CDN链接（无需下载到本地）
2. 多段任务不等待全部完成，实时更新每段的URL
3. 前端直接下载ARK公网链接（浏览器可直接访问）
4. 移除Railway本地文件拼接逻辑
5. 新增auto-split功能：根据总时长自动分镜头
"""
import os, ssl, uuid, json, urllib.request, threading, time, math, sys
from flask import Flask, request, jsonify, Response

app = Flask(__name__)

PORT = int(os.environ.get('PORT', 8080))
ARK_BASE = 'https://ark.cn-beijing.volces.com'
MODEL = 'doubao-seedance-2-0-260128'
MAX_CONCURRENT = 3   # 每批最多3个并发（用户要求不超过3个）
MAX_TASK_AGE_SECONDS = 3600
_task_sem = threading.Semaphore(MAX_CONCURRENT)
_task_store = {}
_task_lock = threading.Lock()

def _ark_key():
    return os.environ.get('ARK_KEY', '')

def _ds_key():
    return os.environ.get('DEEPSEEK_KEY', '')

def _wanx_key():
    return os.environ.get('WANXIANG_KEY', '').strip()

def _cleanup_old_tasks():
    with _task_lock:
        now = time.time()
        expired = [k for k, v in _task_store.items()
                   if now - v.get('created_at', 0) > MAX_TASK_AGE_SECONDS]
        for k in expired:
            del _task_store[k]

@app.after_request
def add_cors(res):
    res.headers['Access-Control-Allow-Origin'] = '*'
    res.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    res.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    return res

@app.route('/', methods=['OPTIONS'])
@app.route('/<path:path>', methods=['OPTIONS'])
def options(path=None):
    return Response(status=200)

@app.route('/api/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok'})

# ===== DeepSeek 代理 =====
DEEPSEEK_BASE = 'https://api.deepseek.com'

@app.route('/api/deepseek', methods=['POST'])
def deepseek_proxy():
    ds = _ds_key()
    if not ds:
        return jsonify({'error': 'DEEPSEEK_KEY not configured'}), 500
    try:
        body = request.json or {}
        body.setdefault('model', 'deepseek-chat')
        data = json.dumps(body, ensure_ascii=False).encode()
        req = urllib.request.Request(
            f'{DEEPSEEK_BASE}/chat/completions', data=data,
            headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {ds}'},
            method='POST'
        )
        with urllib.request.urlopen(req, timeout=60, context=ssl._create_unverified_context()) as r:
            return Response(r.read(), mimetype='application/json')
    except urllib.error.HTTPError as e:
        return Response(json.dumps({'error': f'HTTP {e.code}', 'detail': e.read().decode() if e.fp else ''}),
                        status=e.code, mimetype='application/json')
    except Exception as e:
        return Response(json.dumps({'error': str(e)}), status=500, mimetype='application/json')

# ===== 通义万相文生图代理 =====
WANX_BASE = 'https://dashscope.aliyuncs.com'

@app.route('/api/image/generate', methods=['POST'])
def image_generate():
    wanx = _wanx_key()
    if not wanx:
        return Response(json.dumps({'error': 'WANXIANG_KEY not configured'}), status=500, mimetype='application/json')
    try:
        body = request.json or {}
        prompt = str(body.get('prompt', ''))[:2000]
        if not prompt:
            return Response(json.dumps({'error': 'prompt is required'}), status=400, mimetype='application/json')
        payload = json.dumps({
            'model': body.get('model', 'wanx2.1-t2i-turbo'),
            'input': {'prompt': prompt},
            'parameters': {'size': body.get('size', '1024*1024'), 'n': min(int(body.get('n', 1)), 4)}
        }, ensure_ascii=False).encode()
        req = urllib.request.Request(
            f'{WANX_BASE}/api/v1/services/aigc/text2image/image-synthesis',
            data=payload,
            headers={
                'Content-Type': 'application/json',
                'Authorization': f'Bearer {wanx}',
                'X-DashScope-Async': 'enable'
            },
            method='POST'
        )
        with urllib.request.urlopen(req, timeout=30, context=ssl._create_unverified_context()) as r:
            result = json.loads(r.read())
            task_id = (result.get('output') or {}).get('task_id')
            if not task_id:
                return Response(json.dumps({'error': 'no task_id', 'detail': result}), status=500, mimetype='application/json')
            return Response(json.dumps({'task_id': task_id, 'status': 'pending'}), mimetype='application/json')
    except urllib.error.HTTPError as e:
        return Response(json.dumps({'error': f'HTTP {e.code}', 'detail': e.read().decode() if e.fp else ''}),
                        status=e.code, mimetype='application/json')
    except Exception as e:
        return Response(json.dumps({'error': str(e)}), status=500, mimetype='application/json')

@app.route('/api/image/status/<task_id>', methods=['GET'])
def image_status(task_id):
    wanx = _wanx_key()
    if not wanx:
        return Response(json.dumps({'error': 'WANXIANG_KEY not configured'}), status=500, mimetype='application/json')
    try:
        req = urllib.request.Request(
            f'{WANX_BASE}/api/v1/tasks/{task_id}',
            headers={'Authorization': f'Bearer {wanx}'}, method='GET'
        )
        with urllib.request.urlopen(req, timeout=30, context=ssl._create_unverified_context()) as r:
            result = json.loads(r.read())
            output = result.get('output', {})
            task_status = output.get('task_status', '')
            if task_status == 'SUCCEEDED':
                images = [{'url': item.get('url', ''), 'prompt': item.get('actual_prompt', '')}
                          for item in output.get('results', [])]
                return Response(json.dumps({'status': 'succeeded', 'images': images}), mimetype='application/json')
            elif task_status == 'FAILED':
                return Response(json.dumps({'status': 'failed', 'error': output.get('message', 'failed')}),
                                status=500, mimetype='application/json')
            else:
                return Response(json.dumps({'status': task_status, 'task_id': task_id}), mimetype='application/json')
    except Exception as e:
        return Response(json.dumps({'error': str(e)}), status=500, mimetype='application/json')

# ===== 环境诊断 =====
@app.route('/api/debug/env', methods=['GET'])
def debug_env():
    ark = _ark_key()
    return jsonify({
        'ARK_KEY_len': len(ark),
        'ARK_KEY_masked': ark[:6] + '...' if ark else 'EMPTY',
        'DEEPSEEK_KEY_len': len(_ds_key()),
        'TEMP_DIR': '/tmp (in-memory only)',
    })

# ===== 镜头自动拆分 =====
def _auto_split_shots(prompt, total_duration):
    """
    根据总时长自动拆分为合理镜头数：
    - 15秒 → 2-3个镜头
    - 30秒 → 4-5个镜头
    - 60秒 → 8-10个镜头
    每个镜头7-8秒
    """
    if total_duration <= 15:
        n = 2
    elif total_duration <= 30:
        n = 4
    elif total_duration <= 60:
        n = 8
    elif total_duration <= 90:
        n = 10
    else:
        n = min(12, max(8, math.ceil(total_duration / 7)))

    base_dur = total_duration / n
    shots = []
    for i in range(n):
        dur = round(base_dur)
        # 最后一段补齐
        if i == n - 1:
            dur = total_duration - sum(s['duration'] for s in shots)
        dur = min(max(dur, 5), 11)
        shots.append({'prompt': prompt, 'duration': dur})
    return shots

# ===== 单段视频生成 =====
@app.route('/api/video/generate', methods=['POST'])
def generate():
    _cleanup_old_tasks()
    body = request.json or {}
    prompt = str(body.get('prompt', ''))[:2000]
    duration = min(max(int(body.get('duration', 5)), 5), 11)
    task_id = str(uuid.uuid4().hex[:12])

    with _task_lock:
        _task_store[task_id] = {
            'status': 'pending', 'video_url': '', 'error': '',
            'progress': 0, 'created_at': time.time(),
            'segments': [{'index': 0, 'ark_url': '', 'status': 'pending', 'duration': duration, 'prompt': prompt[:200]}]
        }

    t = threading.Thread(target=_bg_generate, args=(task_id, prompt, duration), daemon=False)
    t.start()
    return jsonify({'task_id': task_id, 'status': 'pending'})

# ===== 多段自动拆分生成 =====
@app.route('/api/video/generate-auto', methods=['POST'])
def generate_auto():
    """
    新接口：根据总时长自动拆分镜头，分批并发生成（每批≤3个）
    每段完成后立即更新URL，前端可直接下载ARK公网链接
    """
    _cleanup_old_tasks()
    body = request.json or {}
    prompt = str(body.get('prompt', ''))[:2000]
    total_duration = min(max(int(body.get('duration', 15)), 10), 90)
    shots = _auto_split_shots(prompt, total_duration)
    task_id = f'auto-{uuid.uuid4().hex[:12]}'

    segments = [{
        'index': i,
        'ark_url': '',
        'status': 'pending',
        'duration': s['duration'],
        'prompt': s['prompt'][:200]
    } for i, s in enumerate(shots)]

    with _task_lock:
        _task_store[task_id] = {
            'status': 'running', 'video_url': '', 'error': '',
            'progress': 0, 'created_at': time.time(), 'segments': segments,
            'total': len(shots), 'done': 0
        }

    t = threading.Thread(target=_bg_auto_generate, args=(task_id, shots), daemon=False)
    t.start()
    return jsonify({'task_id': task_id, 'status': 'running', 'total_segments': len(shots)})

# ===== 多段自定义生成（兼容旧接口）=====
@app.route('/api/video/generate-long', methods=['POST'])
def generate_long():
    _cleanup_old_tasks()
    body = request.json or {}
    segments_body = body.get('segments', [])
    if not segments_body:
        return Response(json.dumps({'error': 'segments required'}), status=400, mimetype='application/json')

    task_id = f'long-{uuid.uuid4().hex[:12]}'
    segments = [{
        'index': i,
        'ark_url': '',
        'status': 'pending',
        'duration': min(int(s.get('duration', 10)), 11),
        'prompt': str(s.get('prompt', ''))[:2000]
    } for i, s in enumerate(segments_body)]

    with _task_lock:
        _task_store[task_id] = {
            'status': 'running', 'video_url': '', 'error': '',
            'progress': 0, 'created_at': time.time(), 'segments': segments,
            'total': len(segments), 'done': 0
        }

    t = threading.Thread(target=_bg_auto_generate, args=(task_id, segments_body), daemon=False)
    t.start()
    return jsonify({'task_id': task_id, 'status': 'running'})

# ===== 轮询任务状态 =====
@app.route('/api/video/status/<task_id>', methods=['GET'])
def status(task_id):
    with _task_lock:
        if task_id in _task_store:
            t = _task_store[task_id]
            # 实时计算progress（已完成段/总段数）
            done = sum(1 for s in t.get('segments', []) if s.get('status') == 'succeeded')
            prog = int(done / max(t.get('total', 1), 1) * 100)
            return jsonify({
                'status': t['status'],
                'video_url': t.get('video_url', ''),
                'error': t.get('error', ''),
                'failure_code': t.get('failure_code', ''),
                'progress': t.get('progress', prog),
                'segments': t.get('segments', []),
                'total_segments': t.get('total', 0),
                'done_segments': done
            })
    return jsonify({'status': 'not_found', 'video_url': '', 'error': '任务不存在'})

# ===== 后台生成（单段）=====
def _bg_generate(task_id, prompt, duration):
    _task_sem.acquire()
    try:
        _update_segment(task_id, 0, status='running')
        tid = _ark_submit_with_retry(prompt, duration)
        st, url, code = _poll_ark(tid)
        if st == 'succeeded':
            _update_segment(task_id, 0, status='succeeded', ark_url=url)
            _set_final_status(task_id, 'succeeded', video_url=url, progress=100)
        else:
            _set_final_status(task_id, 'failed', error=f'ARK失败: {code}', failure_code=code or 'UNKNOWN')
    except Exception as e:
        _set_final_status(task_id, 'failed', error=str(e), failure_code='CLIENT_ERROR')
    finally:
        _task_sem.release()

# ===== 后台生成（多段自动分批并发）=====
def _bg_auto_generate(task_id, shots):
    """
    分批并发生成，每批最多3个
    每段完成后立即更新URL，前端可直接下载
    """
    total = len(shots)
    done_count = 0
    try:
        # 分批处理
        batch_size = MAX_CONCURRENT
        for batch_start in range(0, total, batch_size):
            batch = shots[batch_start:batch_start + batch_size]
            batch_threads = []
            for i, shot in enumerate(batch):
                seg_idx = batch_start + i
                t = threading.Thread(target=_gen_one_segment,
                                     args=(task_id, seg_idx, shot['prompt'], shot['duration']),
                                     daemon=False)
                batch_threads.append(t)
                t.start()
            for t in batch_threads:
                t.join()
                done_count += 1

        # 全部完成
        all_done = all(
            s['status'] == 'succeeded'
            for s in _task_store.get(task_id, {}).get('segments', [])
        )
        any_failed = any(s['status'] == 'failed' for s in _task_store.get(task_id, {}).get('segments', []))
        if all_done:
            _set_final_status(task_id, 'succeeded', progress=100)
        elif any_failed:
            _set_final_status(task_id, 'failed', error='部分片段生成失败', failure_code='PARTIAL_FAILURE')
        else:
            _set_final_status(task_id, 'running', progress=100)
    except Exception as e:
        _set_final_status(task_id, 'failed', error=str(e), failure_code='CLIENT_ERROR')

def _gen_one_segment(task_id, seg_idx, prompt, duration):
    _task_sem.acquire()
    try:
        _update_segment(task_id, seg_idx, status='running')
        tid = _ark_submit_with_retry(prompt, duration)
        st, url, code = _poll_ark(tid)
        if st == 'succeeded':
            _update_segment(task_id, seg_idx, status='succeeded', ark_url=url)
        else:
            _update_segment(task_id, seg_idx, status='failed')
    except Exception as e:
        _update_segment(task_id, seg_idx, status='failed')
    finally:
        _task_sem.release()

# ===== 更新单段状态 =====
def _update_segment(task_id, seg_idx, status=None, ark_url=None):
    with _task_lock:
        if task_id in _task_store:
            segs = _task_store[task_id].get('segments', [])
            if 0 <= seg_idx < len(segs):
                if status is not None:
                    segs[seg_idx]['status'] = status
                if ark_url is not None:
                    segs[seg_idx]['ark_url'] = ark_url

# ===== 更新最终状态 =====
def _set_final_status(task_id, status=None, video_url=None, error=None, failure_code=None, progress=None):
    with _task_lock:
        if task_id in _task_store:
            if status is not None:
                _task_store[task_id]['status'] = status
            if video_url is not None:
                _task_store[task_id]['video_url'] = video_url
            if error is not None:
                _task_store[task_id]['error'] = error
            if failure_code is not None:
                _task_store[task_id]['failure_code'] = failure_code
            if progress is not None:
                _task_store[task_id]['progress'] = progress

# ===== ARK提交（指数退避重试）=====
MAX_RETRIES = 5
BASE_DELAY = 2

def _ark_submit_with_retry(prompt, duration, retries=MAX_RETRIES):
    body = json.dumps({
        'model': MODEL,
        'content': [{'type': 'text', 'text': prompt}],
        'ratio': '16:9', 'duration': duration, 'watermark': False
    }, ensure_ascii=False).encode()
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                f'{ARK_BASE}/api/v3/contents/generations/tasks',
                data=body,
                headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {_ark_key()}'},
                method='POST'
            )
            with urllib.request.urlopen(req, timeout=60, context=ssl._create_unverified_context()) as r:
                return json.loads(r.read()).get('id')
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(BASE_DELAY * (2 ** attempt) + math.random())
            elif e.code in (500, 502, 503, 504):
                time.sleep(BASE_DELAY * (2 ** attempt))
            else:
                raise
        except Exception as e:
            if attempt == retries - 1:
                raise
            time.sleep(BASE_DELAY * (2 ** attempt))
    raise RuntimeError('ARK max retries exceeded')

# ===== ARK轮询 =====
def _poll_ark(ark_task_id, timeout=600):
    start = time.time()
    interval = 5
    while time.time() - start < timeout:
        try:
            req = urllib.request.Request(
                f'{ARK_BASE}/api/v3/contents/generations/tasks/{ark_task_id}',
                headers={'Authorization': f'Bearer {_ark_key()}'}, method='GET'
            )
            with urllib.request.urlopen(req, timeout=30, context=ssl._create_unverified_context()) as r:
                t = json.loads(r.read())
                st = t.get('status')
                url = (t.get('content') or {}).get('video_url', '')
                if st == 'succeeded':
                    return 'succeeded', url, ''
                if st == 'failed':
                    code = (t.get('content') or {}).get('failure_code', '') or t.get('error', '')
                    return 'failed', '', code
                time.sleep(interval)
        except Exception as e:
            time.sleep(interval)
    return 'timeout', '', 'POLL_TIMEOUT'

if __name__ == '__main__':
    print(f'ARK proxy starting on 0.0.0.0:{PORT}', flush=True)
    app.run(host='0.0.0.0', port=PORT, threaded=True)
