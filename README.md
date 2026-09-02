# Xiangdemei AI Video / 想得美

Xiangdemei is an AI video generation web prototype. It gives users a focused
flow for turning a short story idea into an AI-video generation request with
story style, visual style, and duration controls.

Live demo: https://zoe-code-universe.github.io/xiangdemei-zoe-ai66/

## What It Does

- Collects a short video story idea from the user.
- Lets the user choose a narrative style inspired by well-known film directors.
- Lets the user choose a visual style such as cinematic, cyberpunk, ink wash,
  anime, oil painting, or black-and-white film.
- Supports short video duration options from 10 seconds to 3 minutes.
- Provides a prototype generation flow with API key prompts for external
  services.

## Product Shape

This repository contains both a public GitHub Pages prototype and server-side
experiments for video generation integration.

- `index.html`: public web prototype.
- `video_proxy.py`: proxy/server experiment for AI video generation calls.
- `Dockerfile`, `Procfile`, `railway.toml`: deployment experiments.
- `xiangnile.html`: related memorial-space prototype experiment.

## Audience

This prototype is for creators who want to describe a visual story in plain
language and quickly explore how it could become an AI-generated video.

## Tech

- HTML
- CSS
- JavaScript
- Python
- GitHub Pages
- Railway/Render-style deployment experiments

## Status

Prototype. The public page demonstrates the product flow and interface direction.
Production use would require hardened API handling, account management, payment,
usage limits, and provider-specific generation reliability checks.

## Safety And Cost Boundary

The prototype asks users to provide their own API keys for generation after the
free trial boundary. API costs, provider terms, and generated media rights should
be reviewed before any production launch.
