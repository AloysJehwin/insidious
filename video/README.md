# Insidious demo video (formerly FestiveKit)

A ~3-minute narrated demo (1920×1080, 30 fps, H.264 High CRF 18 + AAC 192 kHz-48k, faststart), built from HTML scenes
rendered frame by frame. The narrator is **Sarvam AI Bulbul v4** (`bulbul:v4-flash`, `simran_en_conversation`,
`en-IN`, pace 1.2). The bot's Tanglish voice notes are shown in the chat scene but not played.

## Re-render (one command)

```bash
./render.sh
```

Output: `out/insidious-demo-final.mp4`, `out/contact.jpg` (one frame per second) and `out/script.md` (narration with exact timings). It prints a near-empty-frame check and an ffprobe summary
at the end. A full render takes about 2 minutes on an M-series Mac.

Requirements: `python3` with `numpy` and `python-dotenv`, `node` ≥ 18, `ffmpeg`/`ffprobe`. `render.sh` installs
Playwright on first run. `SARVAM_API_KEY` is read from the environment or from `../.env` (never printed).

## What each step does

| Step | Script | Output |
|---|---|---|
| Narration | `scripts/gen_tts.py` reads `narration.json`, one Sarvam TTS call per line, cached by text+voice hash | `build/audio/<id>.wav` |
| Timeline | `scripts/build_timeline.py` lays clips on scenes (`narration.json` → `scenes`: lead/gap/tail), aligns captions to real pauses, computes the per-frame voice envelope for the narrator badge, generates the ambient pad with ffmpeg | `build/timeline.js`, `build/narration.wav`, `build/pad.wav` |
| Frames | `scripts/render.mjs` opens `scenes/index.html` in headless Chromium and calls `window.render(t)` for every frame (deterministic, parallel workers) | `build/video.mp4` |
| Audio | `scripts/mix_audio.py`: narration two-pass loudnorm to −16 LUFS, pad at ≈ −30 LUFS ducked −18 dB under speech with 300 ms ramps | `build/mix.wav` |
| Mux + checks | `render.sh` (ffmpeg), `scripts/check_frames.py` | `out/insidious-demo-final.mp4`, `out/contact.jpg` |

## Editing

- **Words**: edit `narration.json` (only changed lines are re-synthesised). Scene timing follows the audio.
- **Visuals**: `scenes/index.html` + `scenes/style.css`. Elements are timed with `data-t`, either in seconds from the
  scene start or locked to narration: `@c6a~Kubernetes|-0.2` = 0.2 s before the narrator says "Kubernetes".
  Animation types: `data-a="fade|up|left|right|pop|grow-x|draw|count|type"`; `data-out` fades out; `data-on` adds a
  class. Per-scene custom motion lives in `scenes/engine.js` (`hooks`).
- **Screenshots**: drop files into `assets/` with these names and re-run. Missing ones are hidden.
  `flagsmith-features.jpg`, `nudgebee-incident-triage.jpg`, `nudgebee-evidence.jpg`, `nudgebee-rca.jpg`,
  `nudgebee-workflow.jpg`, `ops-demo-desktop.png` (end card), `aws*.png|jpg` (under-the-hood AWS panel).
- **URLs**: `config.json` → `liveUrl`, `repoUrl` (the repo URL is a placeholder until set).
- **Preview stills**: `node scripts/render.mjs --stills 12,40,95` → `build/stills/t-<sec>.png`.

## Honesty notes baked into the video

- Money figures are labelled **estimate** on screen; 4.8 s and 180 ms / 0 errors are measured on our k3s cluster
  (the 180 ms run shed `heavy_features` at 80 rps; the recorded incident recovered to 442.8 ms per NudgeBee's RCA,
  which the fix scene's footnote and the RCA scene both show).
- The chat scene is marked "Replayed from live logs"; it is a generic chat UI (no WhatsApp branding), labelled
  "WhatsApp Business API · test number". Bot texts are the exact take-3 messages from the ops-bot approval store; the tap and read-back are a replay.
- The RDS read replica is narrated as "in another run", where the founder chose a database copy.
- `insidious scan` numbers come from `examples/jeffi-scan` (heuristic mode).

## Live console footage

`live.json` maps 1 fps frame captures (`assets/live-ops*/fNNNN.jpg`) onto the timeline: `keys` are
`[time, source frame]` pairs (speed ramps come from their spacing), `zoom` are `[time, scale, focusX, focusY]`
punch-ins (eased), `highlights` draw labelled boxes in source coordinates. The final cut uses take 1 (surge →
incident → ₹ at risk) only; no take recorded the founder's tap, so the fix scene is the replay built from real logs.
The console header in the footage still read "FestiveKit"; `engine.js` masks it with "Insidious · Jeffi Stores ·
Mission control".
