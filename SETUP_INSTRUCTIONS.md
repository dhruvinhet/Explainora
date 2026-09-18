# Simi-like Realtime Explainer Video Generator — Setup Instructions

This is the full project zip (nothing removed). It converts text prompts into narrated, animated SVG-scene MP4 videos in **real time** — the video plays while scenes are still being generated, exactly like Simi by Lamina Labs.

---

## 1. Requirements

| Component | What you need |
|---|---|
| OS | Linux or WSL2 on Windows (macOS works too, untested) |
| Python | 3.10+ |
| ffmpeg | with **librsvg** support (`ffmpeg -version` should list librsvg) |
| rsvg-convert | used as SVG rasterizer (`sudo apt install librsvg2-bin`) |
| Piper TTS | binary at `/usr/local/bin/piper` + model file |
| Groq API keys | 1 primary + 7 backup keys (you already have 8) |

## 2. Quick setup (Linux/WSL)

```bash
# 1. Extract
unzip simi_mvp_realtime_v5_full.zip -d simi_realtime
cd simi_realtime

# 2. System packages (Ubuntu/WSL)
sudo apt update
sudo apt install -y ffmpeg librsvg2-bin

# 3. Piper TTS
mkdir -p piper && cd piper
wget -q https://github.com/rhasspy/piper/releases/download/v0.0.1/piper_linux_x86_64.tar.gz
tar -xzf piper_linux_x86_64.tar.gz
cd ..
# The zip already contains the model file (en_US-lessac-medium.onnx)
# If missing, download:
# wget -q https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium/en_US-lessac-medium.onnx

# 4. Python deps
pip3 install -r requirements.txt

# 5. Config (.env already included — edit with your keys)
#    GROQ_API_KEY        = your primary key
#    GROQ_API_KEYS       = comma-separated 7 backup keys
#    STYLE_SKIN          = default|ocean|forest|sunset|mono
#    CAPTIONS_ENABLED    = 1 (burnt-in captions)
#    MUSIC_TRACK_PATH    = path to music_bed.mp3 (included)
```

## 3. Run

```bash
python3 app.py
# open http://localhost:5000 in your browser
```

For debug logs: `FLASK_DEBUG=1 python3 app.py` (auto-reloads code on change).

## 4. Usage

1. Type a prompt (e.g. "Explain how caching speeds up computers")
2. Pick a **style skin** (default/ocean/forest/sunset/mono)
3. Toggle **captions** on/off
4. Optionally upload a **document** (PDF/Word/PPT/MD/TXT) — the video anchors to its content
5. Choose **streaming mode** → the video starts playing while scenes generate; all scenes appear smoothly with no black flash or timeline reset
6. Final MP4 lands in `outputs/<job_id>/final.mp4`

## 5. Where to look at details

| Need | Location |
|---|---|
| Per-run logs (tokens, latency, model, key, retries) | `logs/runs/<job_id>.jsonl` |
| Server console log | `logs/server.log` |
| Per-scene assets (SVG/PNG/WAV/MP4) | `work/<job_id>/segments/scene_NNN/` |
| Live stream | `work/<job_id>/stream.mp4` |
| API pool status | http://localhost:5000/api/pool |

## 6. Troubleshooting

- **403 Forbidden from Groq**: the dev plan keys occasionally get temporarily blocked (this happened today from a specific network). Retry later; the pool automatically quarantine-blocks bad keys and fails over to the remaining 7.
- **"librsvg not found"**: install `librsvg2-bin` (rsvg-convert).
- **No audio**: check piper binary is at `/usr/local/bin/piper` or symlink it.
- **Slow scenes**: animated scenes render ~1 frame at 4fps; static scenes are instant. The 8-key pool parallelizes LLM planning across scenes.
- **Captions cut off**: automatic fix applied (max 11 words per phrase + wrapping).

---

## What's fixed in this version

1. Narration integrity — video length always equals actual narration; no tempo manipulation
2. Animated text-leak — `opacity="..."` strings no longer appear as visible text
3. Streaming player — all scenes show; smooth appends, no black flash, no timeline reset (Blob-based swap)
4. Captions — never overflow the screen (11-word phrase limit + line wrapping)
5. Mixed-fps concat — assembler normalizes fps before concatenation
6. Style skins — correctly applied per-job at render time
7. API pool — day-level 429 quarantine with failover across all 8 keys
8. Document upload — pdftotext + pypdf fallback, TPM-safe anchors
9. Adaptive scene budget (80 elements total), narration-timed animations, deterministic fallback animations
10. Full per-run logging with token/latency/cost details
