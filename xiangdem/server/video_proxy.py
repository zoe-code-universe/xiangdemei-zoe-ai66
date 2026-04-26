#!/usr/bin/env python3
"""火山方舟(ARK)视频生成代理 - Flask版本"""
import os, ssl, uuid, tempfile, json, urllib.request, subprocess, threading
from flask import Flask, request, jsonify, send_file, Response

app = Flask(__name__)

PORT = int(os.environ.get('PORT', 8080))
ARK_KEY = os.environ.get('ARK_KEY', 'ark-2a29718e-e5b8-47d7-b454-792d948835fd-6fb13')
ARK_BASE = 'https://ark.cn-beijing.volces.com'
MODEL = 'doubao-seedance-2-0-260128'
TEMP_DIR = tempfile.mkdtemp(prefix='xiangdem_')

# 任务状态存储
TASK_RESULTS = {}  # task_id -> {status, video_url, error}

# ===== 全局CORS头 =====
@app.after_request
def add_cors(res):
    res.headers['Access-Control-Allow-Origin'] = '*'
    res.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    res.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    res.headers['Access-Control-Max-Age'] = '86400'
    return res

@app.route('/<path:path>', methods=['OPTIONS'])
@app.route('/', methods=['OPTIONS'])
def options(path=None):
    return Response(status=200)

# ===== 健康检查 =====
@app.route('/api/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok'})

# ===== 单段生成 =====
@app.route('/api/video/generate', methods=['POST'])
def generate():
    try:
        body = request.json or {}
        prompt = str(body.get('prompt', ''))[:500]
        duration = min(max(int(body.get('duration', 5)), 5), 11)
        task_id = ark_submit(prompt, duration)
        return jsonify({'task_id': task_id, 'status': 'pending'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ===== 多段生成（异步模式）====================
@app.route('/api/video/generate-long', methods=['POST'])
def generate_long():
    """
    异步模式：立即返回task_id，后台完成
    前端通过 /api/video/status/{task_id} 查询结果
    """
    try:
        body = request.json or {}
        segments = body.get('segments', [])
        num = len(segments)
        async_mode = body.get('async', True)  # 默认异步

        if not async_mode:
            # 同步模式（保持兼容）
            return do_generate_long_sync(segments, num)
        
        # 异步模式：立即返回，后台处理
        task_id = f"async-{uuid.uuid4().hex[:12]}"
        print(f'异步任务 {task_id}：{num}段，后台处理中')
        TASK_RESULTS[task_id] = {'status': 'running', 'video_url': '', 'error': ''}
        
        # 后台线程处理
        t = threading.Thread(target=background_generate_long, args=(task_id, segments, num))
        t.daemon = True
        t.start()
        
        return jsonify({'task_id': task_id, 'status': 'running'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

def background_generate_long(task_id, segments, num):
    """后台执行多段生成"""
    run_id = uuid.uuid4().hex[:8]
    seg_files, seg_urls = [], []
    try:
        for i, seg in enumerate(segments):
            p = str(seg.get('prompt', ''))[:500]
            d = min(int(seg.get('duration', 10)), 11)
            tid = ark_submit(p, d)
            st, url = wait_ark(tid)
            if st != 'succeeded':
                TASK_RESULTS[task_id] = {'status': 'failed', 'video_url': '', 'error': f'第{i+1}段失败'}
                return
            path = os.path.join(TEMP_DIR, f'{run_id}_s{i+1:02d}.mp4')
            download(url, path)
            seg_files.append(path)
            seg_urls.append(url)
            print(f'  任务{task_id} 第{i+1}段完成')
        
        if num == 1:
            TASK_RESULTS[task_id] = {'status': 'succeeded', 'video_url': seg_urls[0], 'error': ''}
            return
        
        out = os.path.join(TEMP_DIR, f'{run_id}_final.mp4')
        concat(seg_files, out)
        for f in seg_files:
            os.remove(f)
        TASK_RESULTS[task_id] = {
            'status': 'succeeded',
            'video_url': f'{request.url_root}api/video/serve/{out}',
            'error': ''
        }
        print(f'  任务{task_id} 全部完成')
    except Exception as e:
        TASK_RESULTS[task_id] = {'status': 'failed', 'video_url': '', 'error': str(e)}

# ===== 轮询任务状态 =====
@app.route('/api/video/status/<task_id>', methods=['GET'])
def status(task_id):
    # 普通 ARK task_id
    try:
        st, url = ark_poll(task_id)
        return jsonify({'status': st, 'video_url': url})
    except Exception:
        pass
    
    # 异步任务ID
    if task_id in TASK_RESULTS:
        return jsonify({
            'status': TASK_RESULTS[task_id]['status'],
            'video_url': TASK_RESULTS[task_id]['video_url'],
            'error': TASK_RESULTS[task_id]['error']
        })
    
    return jsonify({'status': 'not_found', 'video_url': '', 'error': '任务不存在'})

# ===== 服务本地视频 =====
@app.route('/api/video/serve/<path:f>', methods=['GET'])
def serve(f):
    if '..' in f:
        return 'Forbidden', 403
    if os.path.exists(f):
        return send_file(f, mimetype='video/mp4')
    return 'Not found', 404

# ===== ARK API =====
def ark_submit(prompt, duration):
    body = json.dumps({
        'model': MODEL,
        'content': [{'type': 'text', 'text': prompt}],
        'ratio': '16:9',
        'duration': duration,
        'watermark': False
    }, ensure_ascii=False).encode()
    req = urllib.request.Request(
        f'{ARK_BASE}/api/v3/contents/generations/tasks',
        data=body,
        headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {ARK_KEY}'},
        method='POST'
    )
    with urllib.request.urlopen(req, timeout=30, context=ssl._create_unverified_context()) as r:
        return json.loads(r.read()).get('id')

def ark_poll(task_id):
    req = urllib.request.Request(
        f'{ARK_BASE}/api/v3/contents/generations/tasks/{task_id}',
        headers={'Authorization': f'Bearer {ARK_KEY}'},
        method='GET'
    )
    with urllib.request.urlopen(req, timeout=30, context=ssl._create_unverified_context()) as r:
        t = json.loads(r.read())
        return t.get('status'), (t.get('content') or {}).get('video_url', '')

def download(url, path):
    with urllib.request.urlopen(url, timeout=60, context=ssl._create_unverified_context()) as r:
        with open(path, 'wb') as f:
            while chunk := r.read(65536):
                f.write(chunk)

def concat(paths, out):
    cf = os.path.join(TEMP_DIR, f'c{uuid.uuid4().hex}.txt')
    with open(cf, 'w') as f:
        for p in paths:
            f.write(f"file '{p}'\n")
    r = subprocess.run(['ffmpeg', '-y', '-f', 'concat', '-safe', '0', '-i', cf, '-c', 'copy', out], capture_output=True, text=True)
    os.remove(cf)
    if r.returncode != 0:
        raise RuntimeError(f'ffmpeg失败: {r.stderr[:200]}')

def wait_ark(task_id, maxt=600):
    import time
    start = time.time()
    while time.time() - start < maxt:
        st, url = ark_poll(task_id)
        if st == 'succeeded':
            return 'succeeded', url
        if st == 'failed':
            return 'failed', ''
        time.sleep(15)
    return 'timeout', ''

# 同步多段生成（兼容旧逻辑）
def do_generate_long_sync(segments, num):
    run_id = uuid.uuid4().hex[:8]
    seg_files, seg_urls = [], []
    try:
        for i, seg in enumerate(segments):
            p = str(seg.get('prompt', ''))[:500]
            d = min(int(seg.get('duration', 10)), 11)
            tid = ark_submit(p, d)
            st, url = wait_ark(tid)
            if st != 'succeeded':
                return jsonify({'status': 'failed', 'error': f'第{i+1}段失败'}), 500
            path = os.path.join(TEMP_DIR, f'{run_id}_s{i+1:02d}.mp4')
            download(url, path)
            seg_files.append(path)
            seg_urls.append(url)
        if num == 1:
            return jsonify({'status': 'succeeded', 'video_url': seg_urls[0], 'segments': seg_urls})
        out = os.path.join(TEMP_DIR, f'{run_id}_final.mp4')
        concat(seg_files, out)
        for f in seg_files:
            os.remove(f)
        return jsonify({'status': 'succeeded', 'video_url': f'{request.url_root}api/video/serve/{out}', 'size_bytes': os.path.getsize(out)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    print(f'ARK proxy starting on 0.0.0.0:{PORT}')
    app.run(host='0.0.0.0', port=PORT)
