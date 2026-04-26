#!/usr/bin/env python3
"""火山方舟(ARK)视频生成代理 - Flask版本"""
import os, ssl, uuid, tempfile, json, urllib.request, subprocess
from flask import Flask, request, jsonify, send_file, Response

app = Flask(__name__)

PORT = int(os.environ.get('PORT', 8080))
ARK_KEY = os.environ.get('ARK_KEY', 'ark-2a29718e-e5b8-47d7-b454-792d948835fd-6fb13')
ARK_BASE = 'https://ark.cn-beijing.volces.com'
MODEL = 'doubao-seedance-2-0-260128'
TEMP_DIR = tempfile.mkdtemp(prefix='xiangdem_')

# ===== 全局CORS头 =====
@app.after_request
def add_cors(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    response.headers['Access-Control-Max-Age'] = '86400'
    return response

# ===== 预检请求集中处理 =====
@app.route('/<path:unused>', methods=['OPTIONS'])
@app.route('/', methods=['OPTIONS'])
def options_preflight(unused=None):
    return Response(status=200)

# ===== 业务接口 =====
@app.route('/api/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok'})

@app.route('/api/video/generate', methods=['POST'])
def handle_generate():
    try:
        body = request.json or {}
        task_id = ark_submit(str(body.get('prompt', ''))[:500], min(max(int(body.get('duration', 5)), 5), 11))
        return jsonify({'task_id': task_id, 'status': 'pending'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/video/status/<task_id>', methods=['GET'])
def handle_status(task_id):
    try:
        status, video_url = ark_poll(task_id)
        return jsonify({'status': status, 'video_url': video_url})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/video/generate-long', methods=['POST'])
def handle_generate_long():
    try:
        body = request.json or {}
        segments = body.get('segments', [])
        num = len(segments)
        print(f'分段生成请求: {num}段')
        run_id = uuid.uuid4().hex[:8]
        seg_files, seg_urls = [], []
        for i, seg in enumerate(segments):
            tid = ark_submit(str(seg.get('prompt',''))[:500], min(int(seg.get('duration',10)),11))
            st, url = wait_ark(tid)
            if st != 'succeeded':
                return jsonify({'status':'failed','error':f'第{i+1}段失败'}), 500
            p = os.path.join(TEMP_DIR, f'{run_id}_s{i+1:02d}.mp4')
            download(url, p)
            seg_files.append(p); seg_urls.append(url)
            print(f'  第{i+1}段完成')
        if num == 1:
            return jsonify({'status':'succeeded','video_url':seg_urls[0],'segments':seg_urls})
        out = os.path.join(TEMP_DIR, f'{run_id}_final.mp4')
        concat(seg_files, out)
        for f in seg_files: os.remove(f)
        sz = os.path.getsize(out)
        return jsonify({'status':'succeeded','video_url':f'{request.url_root}api/video/serve/{out}','size_bytes':sz})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/video/serve/<path:f>', methods=['GET'])
def serve(f):
    if '..' in f: return 'Forbidden', 403
    if os.path.exists(f): return send_file(f, mimetype='video/mp4')
    return 'Not found', 404

# ===== ARK API =====
def ark_submit(prompt, duration):
    body = json.dumps({'model':MODEL,'content':[{'type':'text','text':prompt}],'ratio':'16:9','duration':duration,'watermark':False}, ensure_ascii=False).encode()
    req = urllib.request.Request(f'{ARK_BASE}/api/v3/contents/generations/tasks', data=body, headers={'Content-Type':'application/json','Authorization':f'Bearer {ARK_KEY}'}, method='POST')
    with urllib.request.urlopen(req, timeout=30, context=ssl._create_unverified_context()) as r:
        return json.loads(r.read()).get('id')

def ark_poll(tid):
    req = urllib.request.Request(f'{ARK_BASE}/api/v3/contents/generations/tasks/{tid}', headers={'Authorization':f'Bearer {ARK_KEY}'}, method='GET')
    with urllib.request.urlopen(req, timeout=30, context=ssl._create_unverified_context()) as r:
        t = json.loads(r.read())
        return t.get('status'), (t.get('content') or {}).get('video_url','')

def download(url, path):
    with urllib.request.urlopen(url, timeout=60, context=ssl._create_unverified_context()) as r:
        with open(path,'wb') as f:
            while chunk := r.read(65536): f.write(chunk)

def concat(paths, out):
    cf = os.path.join(TEMP_DIR, f'c{uuid.uuid4().hex}.txt')
    with open(cf,'w') as f:
        for p in paths: f.write(f"file '{p.replace(chr(39),chr(39)+chr(92)+chr(39)+chr(39))}'\n")
    r = subprocess.run(['ffmpeg','-y','-f','concat','-safe','0','-i',cf,'-c','copy',out], capture_output=True, text=True)
    os.remove(cf)
    if r.returncode != 0: raise RuntimeError(f'ffmpeg失败: {r.stderr[:200]}')

def wait_ark(tid, maxt=600):
    import time
    s = time.time()
    while time.time()-s < maxt:
        st, url = ark_poll(tid)
        if st == 'succeeded': return 'succeeded', url
        if st == 'failed': return 'failed', ''
        time.sleep(15)
    return 'timeout', ''

if __name__ == '__main__':
    print(f'ARK proxy starting on 0.0.0.0:{PORT}')
    app.run(host='0.0.0.0', port=PORT)
