# AGENTS.md

## Project Overview

"想得美" (XiangDeMei) — AI video generation tool. Two deployable pieces:

- **Frontend**: Single `index.html` (~1700 lines, vanilla JS + Tailwind CDN) → GitHub Pages
- **Backend**: `video_proxy.py` (Flask) → Railway (Docker)

## Architecture

```
index.html (GitHub Pages) → video_proxy.py (Railway) → DeepSeek API (text)
                                               → Volcengine ARK / Seedance (video)
                                               → Tongyi Wanxiang (image gen)
```

User enters a story → backend proxies to DeepSeek for script → ARK generates video segments → frontend polls for results.

## Critical Facts

- **Two copies of `video_proxy.py` exist**: root-level (deployed via Dockerfile) and `xiangdem/server/` (older version). The root copy is what runs in production. Changes must go to the root file.
- **ARK max single-segment duration is 11 seconds**. Longer videos are auto-split into segments; each returns an independent ARK CDN URL.
- **No ffmpeg in Railway container**. Video concatenation is not possible server-side; all segments are served as separate CDN URLs.
- **No tests, no lint, no typecheck, no CI** in this repo. There is nothing to run for verification.

## Running Locally

```bash
pip install -r requirements.txt
export ARK_KEY=<key>
export DEEPSEEK_KEY=<key>
export WANXIANG_KEY=<key>
python3 video_proxy.py          # starts on 0.0.0.0:8080
```

Frontend is a static HTML file — open directly in browser or serve via any static server. The `VIDEO_PROXY` constant inside `index.html` must point at the running backend.

## Deployment

- **Backend**: Railway builds from `Dockerfile` (python:3.11-slim + ffmpeg install, but ffmpeg is only useful for local dev concat). Health check: `GET /api/health`. Start command: `python3 video_proxy.py`.
- **Frontend**: Push `index.html` to GitHub Pages branch.
- Railway auto-deploy from `main` can be flaky; verify via GitHub Deployments API if a push doesn't trigger.

## Environment Variables

| Variable | Purpose |
|----------|---------|
| `ARK_KEY` | Volcengine ARK video generation |
| `DEEPSEEK_KEY` | DeepSeek chat completions |
| `WANXIANG_KEY` | Tongyi Wanxiang image generation |
| `PORT` | Server port (default 8080, Railway sets this) |

All three API keys are required for full functionality. Missing keys cause 500 errors on their respective endpoints.

## API Endpoints (video_proxy.py)

| Route | Method | Purpose |
|-------|--------|---------|
| `/api/health` | GET | Health check |
| `/api/deepseek` | POST | Proxy to DeepSeek chat |
| `/api/image/generate` | POST | Submit Wanxiang image task |
| `/api/image/status/<id>` | GET | Poll Wanxiang image task |
| `/api/video/generate` | POST | Submit single ARK video task |
| `/api/video/generate-long` | POST | Submit multi-segment video task |
| `/api/video/generate-auto` | POST | Auto-split prompt by ` \| ` delimiter |
| `/api/video/status/<id>` | GET | Poll video task status |
| `/api/proxy/ark/submit` | POST | Direct ARK task submission |
| `/api/proxy/ark/status/<id>` | GET | Direct ARK status poll |
| `/api/debug/env` | GET | Key length diagnostics |

## Known Quirks

- `ssl._create_unverified_context()` used throughout — SSL verification is disabled for all outbound requests.
- CORS is fully open (`Access-Control-Allow-Origin: *`).
- Circuit breaker locks out a prompt hash for 10 minutes after ARK failure.
- Task state is in-memory only (`_task_store` dict); restarts lose all in-flight tasks.
- `math.random` on line ~474 of root `video_proxy.py` should be `random.random` (the `random` module isn't imported; `math` has no `.random`). This causes a runtime error on 429 retries.
- `DEVLOG.md` contains plaintext API keys — do not commit additional secrets.
