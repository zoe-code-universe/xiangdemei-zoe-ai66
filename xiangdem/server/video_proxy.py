#!/usr/bin/env python3
"""火山方舟(ARK)视频生成代理 + 多段拼接 - Flask版本"""
import os
import ssl
import uuid
import tempfile
import json
import urllib.request
import subprocess
from flask import Flask, request, jsonify, send_file

app = Flask(__name__)

PORT = int(os.environ.get('PORT', 8080))
ARK_KEY = os.environ.get('ARK_KEY', 'ark-2a29718e-e5b8-47d7-b454-792d948835fd-6fb13')
ARK_BASE = 'https://ark.cn-beijing.volces.com'
MODEL = 'doubao-seedance-2-0-260128'
TEMP_DIR = tempfile.mkdtemp(prefix='xiangdem_')

@app.route('/api/video/generate', methods=['POST'])
def handle_generate():
    body = request.json
    prompt = body.get('prompt', '')[:500]
    duration = min(max(int(body.get('duration', 5)), 5), 11)
    try:
        task_id = ark_submit(prompt, duration)
        return jsonify({'task_id': task_id, 'status': 'pending'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/video/generate-long', methods=['POST'])
def handle_generate_long():
    body = request.json
    segments = body.get('segments', [])
    total_duration = int(body.get('total_duration', len(segments) * 10))
    num_segments = len(segments)
    print(f'收到分段生成请求: {num_segments}段, 总时长{total_duration}秒')
    run_id = uuid.uuid4().hex[:8]
    segment_files = []
    segment_urls = []
    try:
        for i, seg in enumerate(segments):
            prompt = seg.get('prompt', '')[:500]
            duration = min(int(seg.get('duration', 10)), 11)
            print(f'  提交第{i+1}/{num_segments}段')
            task_id = ark_submit(prompt, duration)
            status, video_url = wait_ark(task_id)
            if status != 'succeeded':
                return jsonify({'status': 'failed', 'error': f'第{i+1}段生成失败: {status}'}), 500
            seg_path = os.path.join(TEMP_DIR, f'{run_id}_seg{i+1:02d}.mp4')
            download_video(video_url, seg_path)
            segment_files.append(seg_path)
            segment_urls.append(video_url)
            print(f'  第{i+1}段完成')
        if num_segments == 1:
            return jsonify({'status': 'succeeded', 'video_url': segment_urls[0], 'segments': segment_urls})
        output_path = os.path.join(TEMP_DIR, f'{run_id}_final.mp4')
        print(f'开始拼接{len(segment_files)}个视频段...')
        concat_videos(segment_files, output_path)
        size = os.path.getsize(output_path)
        for f in segment_files:
            os.remove(f)
        serve_url = f'https://thorough-contentment-production-0bd7.up.railway.app/api/video/serve/{output_path}'
        return jsonify({'status': 'succeeded', 'video_url': serve_url, 'segments': segment_urls, 'size_bytes': size})
    except Exception as e:
        print(f'分段生成失败: {e}')
        return jsonify({'error': str(e)}), 500

@app.route('/api/video/status/<task_id>', methods=['GET'])
def handle_status(task_id):
    try:
        status, video_url = ark_poll(task_id)
        return jsonify({'status': status, 'video_url': video_url})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/video/serve/<path:filename>', methods=['GET'])
def serve_video(filename):
    safe_dir = TEMP_DIR.replace('/', '')
    if safe_dir not in filename or '..' in filename:
        return 'Forbidden', 403
    file_path = filename
    if os.path.exists(file_path):
        return send_file(file_path, mimetype='video/mp4')
    return 'Not found', 404

@app.route('/api/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok'})

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
    with urllib.request.urlopen(req, timeout=30, context=ssl._create_unverified_context()) as resp:
        return json.loads(resp.read()).get('id')

def ark_poll(task_id):
    req = urllib.request.Request(
        f'{ARK_BASE}/api/v3/contents/generations/tasks/{task_id}',
        headers={'Authorization': f'Bearer {ARK_KEY}'},
        method='GET'
    )
    with urllib.request.urlopen(req, timeout=30, context=ssl._create_unverified_context()) as resp:
        task = json.loads(resp.read())
        status = task.get('status')
        video_url = (task.get('content') or {}).get('video_url', '')
        return status, video_url

def download_video(url, path):
    with urllib.request.urlopen(url, timeout=60, context=ssl._create_unverified_context()) as resp:
        with open(path, 'wb') as f:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                f.write(chunk)

def concat_videos(video_paths, output_path):
    concat_file = os.path.join(TEMP_DIR, f'concat_{uuid.uuid4().hex}.txt')
    with open(concat_file, 'w') as f:
        for p in video_paths:
            escaped = p.replace("'", "'\\''")
            f.write(f"file '{escaped}'\n")
    cmd = ['ffmpeg', '-y', '-f', 'concat', '-safe', '0', '-i', concat_file, '-c', 'copy', output_path]
    r = subprocess.run(cmd, capture_output=True, text=True)
    os.remove(concat_file)
    if r.returncode != 0:
        raise RuntimeError(f'ffmpeg拼接失败: {r.stderr[:200]}')

def wait_ark(task_id, max_wait=600):
    import time
    start = time.time()
    while time.time() - start < max_wait:
        status, video_url = ark_poll(task_id)
        if status == 'succeeded':
            return 'succeeded', video_url
        elif status == 'failed':
            return 'failed', ''
        time.sleep(15)
    return 'timeout', ''

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=PORT)
