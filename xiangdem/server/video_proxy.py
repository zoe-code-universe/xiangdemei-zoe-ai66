#!/usr/bin/env python3
"""
火山方舟(ARK)视频生成代理 - 完整异步任务架构
- generate 立即返回 task_id，后台线程执行
- status 轮询任务状态
- generate-long 后台线程处理，文件存储任务结果
- 并发限制 2
- 429/超时 指数退避重试
"""
import os, ssl, uuid, tempfile, json, urllib.request, subprocess, threading, time, math
from flask import Flask, request, jsonify, send_file, Response

app = Flask(__name__)

PORT = int(os.environ.get('PORT', 8080))
ARK_BASE = 'https://ark.cn-beijing.volces.com'
MODEL = 'doubao-seedance-2-0-260128'
TEMP_DIR = tempfile.mkdtemp(prefix='xiangdem_')
TASK_FILE = os.path.join(TEMP_DIR, 'tasks.json')

MAX_CONCURRENT = 2
_task_sem = threading.Semaphore(MAX_CONCURRENT)
MAX_RETRIES = 5
BASE_DELAY = 2

def _ark_key():
    return os.environ.get('ARK_KEY', '')

def _ds_key():
    return os.environ.get('DEEPSEEK_KEY', '')

# ===== 任务存储（内存 + 文件持久化）=====
_task_store = {}
_task_lock = threading.Lock()

def load_tasks():
    try:
        with open(TASK_FILE, 'r') as f:
            return json.load(f)
    except:
        return {}

def save_tasks(tasks):
    with open(TASK_FILE, 'w') as f:
        json.dump(tasks, f, ensure_ascii=False)

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

# ===== 健康检查 =====
@app.route('/api/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok'})

# ===== DeepSeek API 代理 =====
DEEPSEEK_BASE = 'https://api.deepseek.com'

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

@app.route('/api/deepseek', methods=['POST'])
def deepseek_proxy():
    ds = _ds_key()
    try:
        if not ds:
            return jsonify({'error': 'DEEPSEEK_KEY not configured'}), 500
        body = request.json or {}
        body.setdefault('model', 'deepseek-chat')
        data = json.dumps(body, ensure_ascii=False).encode()
        req = urllib.request.Request(
            f'{DEEPSEEK_BASE}/chat/completions',
            data=data,
            headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {ds}'},
            method='POST'
        )
        with urllib.request.urlopen(req, timeout=60, context=ssl._create_unverified_context()) as r:
            return Response(r.read(), mimetype='application/json')
    except urllib.error.HTTPError as e:
        return Response(json.dumps({'error': f'HTTP {e.code}'}), status=e.code, mimetype='application/json')
    except Exception as e:
        return Response(json.dumps({'error': str(e)}), status=500, mimetype='application/json')

# ===== 提交任务（立即返回 task_id，后台执行）=====
@app.route('/api/video/generate', methods=['POST'])
def generate():
    try:
        body = request.json or {}
        prompt = str(body.get('prompt', ''))[:500]
        duration = min(max(int(body.get('duration', 5)), 5), 11)

        task_id = str(uuid.uuid4().hex[:12])
        with _task_lock:
            _task_store[task_id] = {'status': 'pending', 'video_url': '', 'error': '', 'progress': 0}

        threading.Thread(target=_run_generate, args=(task_id, prompt, duration), daemon=True).start()
        return jsonify({'task_id': task_id, 'status': 'pending'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ===== 轮询任务状态 =====
@app.route('/api/video/status/<task_id>', methods=['GET'])
def status(task_id):
    # 先查 ARK（单段直接提交的任务）
    try:
        st, url, code = _poll_ark(task_id)
        if st != 'not_found':
            return jsonify({'status': st, 'video_url': url, 'failure_code': code})
    except:
        pass

    # 查本地任务存储（generate-long 后台任务）
    with _task_lock:
        tasks = load_tasks()
        if task_id in tasks:
            t = tasks[task_id]
            return jsonify({
                'status': t['status'],
                'video_url': t.get('video_url', ''),
                'error': t.get('error', ''),
                'failure_code': t.get('failure_code', '')
            })
        if task_id in _task_store:
            t = _task_store[task_id]
            return jsonify({
                'status': t['status'],
                'video_url': t.get('video_url', ''),
                'error': t.get('error', ''),
                'failure_code': t.get('failure_code', '')
            })

    return jsonify({'status': 'not_found', 'video_url': '', 'error': '任务不存在'})

# ===== 多段生成（后台线程 + 文件存储）=====
@app.route('/api/video/generate-long', methods=['POST'])
def generate_long():
    try:
        body = request.json or {}
        segments = body.get('segments', [])
        num = len(segments)
        task_id = f'long-{uuid.uuid4().hex[:12]}'

        tasks = load_tasks()
        tasks[task_id] = {'status': 'running', 'video_url': '', 'error': '', 'progress': 0}
        save_tasks(tasks)

        with _task_lock:
            _task_store[task_id] = {'status': 'running', 'video_url': '', 'error': '', 'progress': 0}

        threading.Thread(target=_run_generate_long, args=(task_id, segments, num), daemon=True).start()
        return jsonify({'task_id': task_id, 'status': 'running'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ===== 后台执行：单段生成 =====
def _run_generate(task_id, prompt, duration):
    _task_sem.acquire()
    try:
        with _task_lock:
            _task_store[task_id]['status'] = 'running'
            _task_store[task_id]['progress'] = 10

        ark_task_id = _ark_submit_with_retry(prompt, duration)

        with _task_lock:
            _task_store[task_id]['progress'] = 30

        status, video_url, code = _poll_ark(ark_task_id)

        with _task_lock:
            if status == 'succeeded':
                _task_store[task_id]['status'] = 'succeeded'
                _task_store[task_id]['video_url'] = video_url
                _task_store[task_id]['progress'] = 100
            else:
                _task_store[task_id]['status'] = 'failed'
                _task_store[task_id]['error'] = 'ARK task failed'
                _task_store[task_id]['failure_code'] = code or 'UNKNOWN'
    except Exception as e:
        with _task_lock:
            _task_store[task_id]['status'] = 'failed'
            _task_store[task_id]['error'] = str(e)
            _task_store[task_id]['failure_code'] = 'CLIENT_ERROR'
    finally:
        _task_sem.release()

# ===== 后台执行：多段生成 =====
def _run_generate_long(task_id, segments, num):
    _task_sem.acquire()
    run_id = uuid.uuid4().hex[:8]
    seg_files, seg_urls = [], []
    try:
        for i, seg in enumerate(segments):
            p = str(seg.get('prompt', ''))[:500]
            d = min(int(seg.get('duration', 10)), 11)

            with _task_lock:
                _task_store[task_id]['progress'] = int((i / num) * 80)

            tid = _ark_submit_with_retry(p, d)
            status, url, code = _poll_ark(tid)

            if status != 'succeeded':
                tasks = load_tasks()
                tasks[task_id] = {'status': 'failed', 'video_url': '', 'error': f'第{i+1}段失败', 'failure_code': code or 'UNKNOWN'}
                save_tasks(tasks)
                with _task_lock:
                    _task_store[task_id]['status'] = 'failed'
                return

            path = os.path.join(TEMP_DIR, f'{run_id}_s{i+1:02d}.mp4')
            download(url, path)
            seg_files.append(path)
            seg_urls.append(url)

        with _task_lock:
            _task_store[task_id]['progress'] = 85

        if num == 1:
            video_url = seg_urls[0]
        else:
            out = os.path.join(TEMP_DIR, f'{run_id}_final.mp4')
            concat(seg_files, out)
            for f in seg_files:
                os.remove(f)
            video_url = f'{request.url_root}api/video/serve/{out}'

        tasks = load_tasks()
        tasks[task_id] = {'status': 'succeeded', 'video_url': video_url, 'error': ''}
        save_tasks(tasks)

        with _task_lock:
            _task_store[task_id]['status'] = 'succeeded'
            _task_store[task_id]['video_url'] = video_url
            _task_store[task_id]['progress'] = 100

    except Exception as e:
        tasks = load_tasks()
        tasks[task_id] = {'status': 'failed', 'video_url': '', 'error': str(e), 'failure_code': 'CLIENT_ERROR'}
        save_tasks(tasks)
        with _task_lock:
            _task_store[task_id]['status'] = 'failed'
            _task_store[task_id]['error'] = str(e)
    finally:
        _task_sem.release()

# ===== ARK 提交（指数退避重试）=====
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
            code = e.code
            if code == 429:
                delay = BASE_DELAY * (2 ** attempt) + math.random()
                print(f'[ARK] 429限流，等待{delay:.1f}秒后重试({attempt+1}/{retries})')
                time.sleep(delay)
            elif code in (500, 502, 503, 504):
                delay = BASE_DELAY * (2 ** attempt)
                print(f'[ARK] 服务错误{code}，等待{delay}秒后重试({attempt+1}/{retries})')
                time.sleep(delay)
            else:
                raise
        except Exception as e:
            if attempt == retries - 1:
                raise
            delay = BASE_DELAY * (2 ** attempt)
            print(f'[ARK] 提交异常:{e}，等待{delay}秒后重试({attempt+1}/{retries})')
            time.sleep(delay)

    raise RuntimeError('ARK提交达到最大重试次数')

# ===== ARK 轮询（较短间隔，供内部使用）=====
def _poll_ark(ark_task_id, timeout=600):
    """返回 (status, video_url, failure_code)"""
    start = time.time()
    interval = 5

    # 先检查是不是本地生成的长任务ID（不是 ARK task_id）
    if not ark_task_id or ark_task_id.startswith('long-'):
        return 'not_found', '', ''

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
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(BASE_DELAY * 2)
            else:
                raise
        except Exception:
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

# ===== 底层工具 =====
def ark_submit(prompt, duration):
    body = json.dumps({'model': MODEL, 'content': [{'type': 'text', 'text': prompt}], 'ratio': '16:9', 'duration': duration, 'watermark': False}, ensure_ascii=False).encode()
    req = urllib.request.Request(f'{ARK_BASE}/api/v3/contents/generations/tasks', data=body, headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {_ark_key()}'}, method='POST')
    with urllib.request.urlopen(req, timeout=30, context=ssl._create_unverified_context()) as r:
        return json.loads(r.read()).get('id')

def ark_poll(tid):
    req = urllib.request.Request(f'{ARK_BASE}/api/v3/contents/generations/tasks/{tid}', headers={'Authorization': f'Bearer {_ark_key()}'}, method='GET')
    with urllib.request.urlopen(req, timeout=30, context=ssl._create_unverified_context()) as r:
        t = json.loads(r.read())
        return t.get('status'), (t.get('content') or {}).get('video_url', '')

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
        raise RuntimeError(f'ffmpeg失败: {r.stderr[:200]}')

if __name__ == '__main__':
    print(f'ARK async proxy starting on 0.0.0.0:{PORT}, max concurrent={MAX_CONCURRENT}')
    app.run(host='0.0.0.0', port=PORT, threaded=True)
