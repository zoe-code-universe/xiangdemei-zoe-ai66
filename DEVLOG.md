# 想得美 · 开发日志

## 2026-04-30 自动部署失效 + generate-auto 接口缺失问题

### 问题描述
前端调用多段视频生成时报 405 Method Not Allowed，问题是 `generate-auto` 接口从未实际写入代码。

### 排查过程

**1. GitHub 仓库澄清**
- 误传记录：Railway 连的是 `xiangdamei-zoe-ai66`（无i），新代码推到 `xiangdemei-zoe-ai66`（有i），两套仓库分离
- 实际核查：`xiangdamei-zoe-ai66`（无i）在 GitHub 上根本不存在
- Railway 实际连接的是 `xiangdemei-zoe-ai66`（有i），代码推送方向正确
- 结论：仓库名无误，之前记忆中的"混淆"是错误记录

**2. 自动部署失效**
- Railway 的 auto-deploy 确实偶发失效（commit `50abc85` 时正常触发）
- 通过 GitHub Deployments API 发现 railway-app[bot] 会自动创建 deployment 记录
- 最新 commit `e281689` 已成功部署（11:36 UTC）

**3. generate-auto 接口不存在（核心 Bug）**
- 前端 `callJimengVideoMulti` 调用 `/api/video/generate-auto`
- 后端 `video_proxy.py` 中从未实现此路由，只有 `/api/video/generate` 和 `/api/video/generate-long`
- 原因：memory 记录了"计划新增接口"，但未实际落地到代码

### 修复内容

**新增 `/api/video/generate-auto` 接口：**
- 接收 `prompt`（用 ` | ` 分隔的多段描述）和 `duration`
- 自动按 ` | ` 拆分段落，每段 10 秒
- 调用已有 `_bg_generate_long` 后台逻辑

**重构 `_bg_generate_long`：**
- 去掉 Railway 容器内 ffmpeg 本地拼接（容器无 ffmpeg）
- 每段 ARK 任务完成后**立即更新** `segments` 数组和 `done_segments`
- 直接返回 ARK 公网 CDN URL，不再本地合并

**Status 接口增强：**
- 返回 `done_segments`、`total_segments`、`segments` 数组
- 每段含 `status` 和 `ark_url`，前端可实时渲染下载网格

### 关键文件
- `video_proxy.py` — 后端代理（Railway）
- `index.html` — 前端（GitHub Pages）
- `railway.toml` — Railway 部署配置

### Git 提交记录
| SHA | 说明 |
|-----|------|
| `50abc85` | 实现 generate-auto 接口骨架 |
| `e281689` | 完整实现——实时 segments 更新、去除 ffmpeg 拼接依赖 |

### 教训
1. **memory 里的"计划"不等于"已实现"**——每次读到"新增接口"要验证代码是否真实存在
2. **仓库名以 GitHub API 查证为准**，不要凭记忆假设
3. Railway auto-deploy 偶发失效时，GitHub Deployments API 是重要排查工具

---

## 架构说明

### 技术栈
- 前端：单页 HTML + Tailwind CSS（CDN）+ 原生 JS
- 后端：Python Flask（Railway）
- 文字模型：DeepSeek API
- 视频模型：火山方舟 ARK（doubao-seedance-2-0-260128）

### 线上地址
- GitHub Pages 前端：https://zoe-code-universe.github.io/xiangdemei-zoe-ai66/
- Railway 后端：https://thorough-contentment-production-89d3.up.railway.app

### 视频生成流程
1. 用户输入故事 → DeepSeek 生成分镜脚本
2. 客户编辑确认脚本
3. 通义万相生成分镜插画（可选）
4. 火山方舟生成视频（>11秒自动分多段）
5. 每段独立 ARK CDN URL，前端实时显示下载网格

### API 密钥
| 服务 | Key | 备注 |
|------|-----|------|
| 火山方舟 ARK | `ark-a9e81ae4-f2f0-43d9-850a-8bd9a1cccde8-d9e15` | 个人实名，余额 ¥155+ |
| DeepSeek | `sk-2a3e986c69224b9294db3610b414d894` | 平台内置 |
| 通义万相 | `sk-163f200c941145668efe75abcfbbaa6d` | 插画用 |

### 已知限制
- ARK 单段最长 11 秒，多段生成依赖前端拆分逻辑
- Railway 容器无 ffmpeg，不做本地拼接，直接返回多段 ARK URL
