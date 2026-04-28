#!/usr/bin/env python3
"""
火山方舟(ARK)视频生成代理 - 可靠异步版本
关键修复：
1. 任务状态存内存，不用文件（避免读文件读到旧数据）
2. gunicorn 兼容的启动方式（--preload 预加载进程）
3. 每个后台任务有完整日志
"""
import os, ssl, uuid, tempfile, json, urllib.request, subprocess, threading, time, math, sys
from flask import Flask, request, jsonify, send_file, Response

app = Flask(__name__)

PORT = int(os.environ.get('PORT', 8080))
ARK_BASE = 'https://ark.cn-beijing.volces.com'
MODEL = 'doubao-seedance-2-0-260128'
TEMP_DIR = tempfile.mkdtemp(prefix='xiangdem_')

MAX_CONCURRENT = 2
_task_sem = threading.Semaphore(MAX_CONCURRENT)
_task_store = {}
_task_lock = threading.Lock()

def _ark_key():
    return os.environ.get('ARK_KEY', '')

def _ds_key():
    return os.environ.get('DEEPSEEK_KEY', '')

def _wanx_key():
    return os.environ.get('WANXIANG_KEY', '')

# ===== CORS =====
@app.after_request
def add_cors(res):
    res.headers['Access-Control-Allow-Origin'] = '*'
    res.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    res.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    res.headers['Access-Control-Max-Age'] = '86400'
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
        return Response(json.dumps({'error': f'HTTP {e.code}'}), status=e.code, mimetype='application/json')
    except Exception as e:
        return Response(json.dumps({'error': str(e)}), status=500, mimetype='application/json')

# ===== 通义万相文生图代理 =====
WANX_BASE = 'https://dashscope.aliyuncs.com'

@app.route('/api/image/generate', methods=['POST'])
def image_generate():
    wanx = _wanx_key()
    if not wanx:
        return Response(
            json.dumps({'error': 'WANXIANG_KEY not configured'}), status=500, mimetype='application/json'
        )
    try:
        body = request.json or {}
        prompt = str(body.get('prompt', ''))
        if not prompt:
            return Response(
                json.dumps({'error': 'prompt is required'}), status=400, mimetype='application/json'
            )
        model = body.get('model', 'wanx2.1-t2i-turbo')
        size = str(body.get('size', '1024*1024'))
        n = min(int(body.get('n', 1)), 4)

        payload = json.dumps({
            'model': model,
            'input': {'prompt': prompt},
            'parameters': {'size': size, 'n': n}
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
                return Response(
                    json.dumps({'error': 'no task_id returned', 'detail': result}),
                    status=500, mimetype='application/json'
                )
            return Response(
                json.dumps({'task_id': task_id, 'status': 'pending'}),
                mimetype='application/json'
            )
    except urllib.error.HTTPError as e:
        err_body = e.read().decode() if e.fp else ''
        return Response(
            json.dumps({'error': f'HTTP {e.code}', 'detail': err_body}),
            status=e.code, mimetype='application/json'
        )
    except Exception as e:
        return Response(json.dumps({'error': str(e)}), status=500, mimetype='application/json')

@app.route('/api/image/status/<task_id>', methods=['GET'])
def image_status(task_id):
    wanx = _wanx_key()
    if not wanx:
        return Response(
            json.dumps({'error': 'WANXIANG_KEY not configured'}), status=500, mimetype='application/json'
        )
    try:
        req = urllib.request.Request(
            f'{WANX_BASE}/api/v1/tasks/{task_id}',
            headers={'Authorization': f'Bearer {wanx}'},
            method='GET'
        )
        with urllib.request.urlopen(req, timeout=30, context=ssl._create_unverified_context()) as r:
            result = json.loads(r.read())
            output = result.get('output', {})
            task_status = output.get('task_status', '')
            if task_status == 'SUCCEEDED':
                results = output.get('results', [])
                images = [{'url': item.get('url', ''), 'prompt': item.get('actual_prompt', '')} for item in results]
                return Response(
                    json.dumps({'status': 'succeeded', 'images': images}),
                    mimetype='application/json'
                )
            elif task_status == 'FAILED':
                return Response(
                    json.dumps({'status': 'failed', 'error': output.get('message', 'failed')}),
                    status=500, mimetype='application/json'
                )
            else:
                return Response(
                    json.dumps({'status': task_status, 'task_id': task_id}),
                    mimetype='application/json'
                )
    except Exception as e:
        return Response(json.dumps({'error': str(e)}), status=500, mimetype='application/json')

# ===== 环境诊断 =====
@app.route('/api/debug/env', methods=['GET'])
def debug_env():
    ark = _ark_key()
    ds = _ds_key()
    return jsonify({
        'ARK_KEY_len': len(ark),
        'ARK_KEY_masked': ark[:6] + '...' if ark else 'EMPTY',
        'DEEPSEEK_KEY_len': len(ds),
        'DEEPSEEK_KEY_masked': ds[:6] + '...' if ds else 'EMPTY',
    })

# ===== 提交视频生成任务（立即返回）=====
@app.route('/api/video/generate', methods=['POST'])
def generate():
    body = request.json or {}
    prompt = str(body.get('prompt', ''))[:500]
    duration = min(max(int(body.get('duration', 5)), 5), 11)
    task_id = str(uuid.uuid4().hex[:12])

    with _task_lock:
        _task_store[task_id] = {
            'status': 'pending',
            'video_url': '',
            'error': '',
            'failure_code': '',
            'progress': 0,
            'created_at': time.time()
        }

    t = threading.Thread(target=_bg_generate, args=(task_id, prompt, duration), daemon=False)
    t.start()
    print(f'[generate] task_id={task_id} started thread={t.is_alive()}', flush=True)

    return jsonify({'task_id': task_id, 'status': 'pending'})

# ===== 轮询任务状态 =====
@app.route('/api/video/status/<task_id>', methods=['GET'])
def status(task_id):
    with _task_lock:
        if task_id in _task_store:
            t = _task_store[task_id]
            return jsonify({
                'status': t['status'],
                'video_url': t.get('video_url', ''),
                'error': t.get('error', ''),
                'failure_code': t.get('failure_code', ''),
                'progress': t.get('progress', 0)
            })
    return jsonify({'status': 'not_found', 'video_url': '', 'error': '任务不存在'})

# ===== 多段生成 =====
@app.route('/api/video/generate-long', methods=['POST'])
def generate_long():
    body = request.json or {}
    segments = body.get('segments', [])
    num = len(segments)
    task_id = f'long-{uuid.uuid4().hex[:12]}'

    with _task_lock:
        _task_store[task_id] = {'status': 'running', 'video_url': '', 'error': '', 'progress': 0}

    t = threading.Thread(target=_bg_generate_long, args=(task_id, segments, num), daemon=False)
    t.start()
    print(f'[generate_long] task_id={task_id} started thread={t.is_alive()}', flush=True)

    return jsonify({'task_id': task_id, 'status': 'running'})

# ===== 后台生成（单段）=====
def _bg_generate(task_id, prompt, duration):
    print(f'[_bg_generate] START task_id={task_id}', flush=True)
    _task_sem.acquire()
    print(f'[_bg_generate] SEMAPHORE ACQUIRED task_id={task_id}', flush=True)
    try:
        _set_status(task_id, 'running', progress=10)
        ark_task_id = _ark_submit_with_retry(prompt, duration)
        print(f'[_bg_generate] ARK submitted ark_task_id={ark_task_id} task_id={task_id}', flush=True)
        _set_status(task_id, 'running', progress=30)
        status, video_url, code = _poll_ark(ark_task_id)
        print(f'[_bg_generate] ARK poll done status={status} task_id={task_id}', flush=True)
        if status == 'succeeded':
            _set_status(task_id, 'succeeded', video_url=video_url, progress=100)
        else:
            _set_status(task_id, 'failed', error='ARK task failed', failure_code=code or 'UNKNOWN')
    except Exception as e:
        print(f'[_bg_generate] EXCEPTION task_id={task_id} error={e}', flush=True)
        _set_status(task_id, 'failed', error=str(e), failure_code='CLIENT_ERROR')
    finally:
        _task_sem.release()
        print(f'[_bg_generate] DONE task_id={task_id}', flush=True)

# ===== 后台生成（多段）=====
def _bg_generate_long(task_id, segments, num):
    print(f'[_bg_generate_long] START task_id={task_id} segments={num}', flush=True)
    _task_sem.acquire()
    run_id = uuid.uuid4().hex[:8]
    seg_files, seg_urls = [], []
    try:
        for i, seg in enumerate(segments):
            p = str(seg.get('prompt', ''))[:500]
            d = min(int(seg.get('duration', 10)), 11)
            print(f'[_bg_generate_long] segment {i+1}/{num} submitting task_id={task_id}', flush=True)
            _set_status(task_id, 'running', progress=int((i / num) * 80))
            tid = _ark_submit_with_retry(p, d)
            print(f'[_bg_generate_long] segment {i+1}/{num} ARK tid={tid} task_id={task_id}', flush=True)
            status, url, code = _poll_ark(tid)
            if status != 'succeeded':
                _set_status(task_id, 'failed', error=f'第{i+1}段失败', failure_code=code or 'UNKNOWN')
                return
            path = os.path.join(TEMP_DIR, f'{run_id}_s{i+1:02d}.mp4')
            download(url, path)
            seg_files.append(path)
            seg_urls.append(url)
            print(f'[_bg_generate_long] segment {i+1}/{num} done task_id={task_id}', flush=True)

        _set_status(task_id, 'running', progress=85)
        if num == 1:
            video_url = seg_urls[0]
        else:
            out = os.path.join(TEMP_DIR, f'{run_id}_final.mp4')
            concat(seg_files, out)
            for f in seg_files:
                os.remove(f)
            video_url = f'http://localhost:{PORT}/api/video/serve/{out}'
        _set_status(task_id, 'succeeded', video_url=video_url, progress=100)
        print(f'[_bg_generate_long] ALL DONE task_id={task_id}', flush=True)
    except Exception as e:
        print(f'[_bg_generate_long] EXCEPTION task_id={task_id} error={e}', flush=True)
        _set_status(task_id, 'failed', error=str(e), failure_code='CLIENT_ERROR')
    finally:
        _task_sem.release()

# ===== 状态更新辅助 =====
def _set_status(task_id, status=None, video_url=None, error=None, failure_code=None, progress=None):
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

# ===== ARK 提交（指数退避重试）=====
MAX_RETRIES = 5
BASE_DELAY = 2

def _ark_submit_with_retry(prompt, duration, retries=MAX_RETRIES):
    body = json.dumps({
        'model': MODEL,
        'content': [{'type': 'text', 'text': prompt}],
        'ratio': '16:9',
        'duration': duration,
        'watermark': False
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
                result = json.loads(r.read())
                return result.get('id')
        except urllib.error.HTTPError as e:
            if e.code == 429:
                delay = BASE_DELAY * (2 ** attempt) + math.random()
                print(f'[ARK 429] retry {attempt+1}/{retries} after {delay:.1f}s', flush=True)
                time.sleep(delay)
            elif e.code in (500, 502, 503, 504):
                delay = BASE_DELAY * (2 ** attempt)
                print(f'[ARK HTTP {e.code}] retry {attempt+1}/{retries} after {delay}s', flush=True)
                time.sleep(delay)
            else:
                raise
        except Exception as e:
            if attempt == retries - 1:
                raise
            delay = BASE_DELAY * (2 ** attempt)
            print(f'[ARK error] {e} retry {attempt+1}/{retries} after {delay}s', flush=True)
            time.sleep(delay)
    raise RuntimeError('ARK max retries exceeded')

# ===== ARK 轮询 =====
def _poll_ark(ark_task_id, timeout=600):
    start = time.time()
    interval = 5
    while time.time() - start < timeout:
        try:
            req = urllib.request.Request(
                f'{ARK_BASE}/api/v3/contents/generations/tasks/{ark_task_id}',
                headers={'Authorization': f'Bearer {_ark_key()}'},
                method='GET'
            )
            with urllib.request.urlopen(req, timeout=30, context=ssl._create_unverified_context()) as r:
                t = json.loads(r.read())
                status = t.get('status')
                video_url = (t.get('content') or {}).get('video_url', '')
                if status == 'succeeded':
                    return 'succeeded', video_url, ''
                if status == 'failed':
                    code = (t.get('content') or {}).get('failure_code', '') or t.get('error', '')
                    return 'failed', '', code
                time.sleep(interval)
        except Exception as e:
            print(f'[_poll_ark] error: {e}', flush=True)
            time.sleep(interval)
    return 'timeout', '', 'POLL_TIMEOUT'

# ===== 视频服务 =====
@app.route('/api/video/serve/<path:f>', methods=['GET'])
def serve(f):
    if '..' in f:
        return 'Forbidden', 403
    if os.path.exists(f):
        return send_file(f, mimetype='video/mp4')
    return 'Not found', 404

# ===== 工具 =====
def download(url, path):
    with urllib.request.urlopen(url, timeout=120, context=ssl._create_unverified_context()) as r:
        with open(path, 'wb') as f:
            while chunk := r.read(65536):
                f.write(chunk)

def concat(paths, out):
    cf = os.path.join(TEMP_DIR, f'c{uuid.uuid4().hex}.txt')
    with open(cf, 'w') as f:
        for p in paths:
            safe = p.replace("'", "'\\''")
            f.write(f"file '{safe}'\n")
    r = subprocess.run(['ffmpeg', '-y', '-f', 'concat', '-safe', '0', '-i', cf, '-c', 'copy', out], capture_output=True, text=True)
    os.remove(cf)
    if r.returncode != 0:
        raise RuntimeError(f'ffmpeg failed: {r.stderr[:200]}')

# ===== 启动 =====
if __name__ == '__main__':
    print(f'ARK proxy starting on 0.0.0.0:{PORT}', flush=True)
    app.run(host='0.0.0.0', port=PORT, threaded=True)
