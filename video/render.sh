#!/usr/bin/env bash
# One command: narration (Sarvam Bulbul, cached) -> timeline -> frames -> mix -> out/festivekit-demo-final.mp4 + out/contact.jpg
set -euo pipefail
cd "$(dirname "$0")"
OUT=${OUT:-out/insidious-demo-final.mp4}
[ -d node_modules/playwright ] || { npm i --no-audit --no-fund; npx playwright install chromium; }
python3 scripts/gen_tts.py
python3 scripts/build_timeline.py
node scripts/render.mjs
python3 scripts/mix_audio.py
mkdir -p out
ffmpeg -y -v error -i build/video.mp4 -i build/mix.wav -map 0:v:0 -map 1:a:0 -c:v copy -c:a aac -b:a 192k -ar 48000 -ac 2 -movflags +faststart -shortest "$OUT"
ffmpeg -y -v error -i "$OUT" -vf "fps=1,scale=320:180,tile=8x$(( ($(ffprobe -v error -show_entries format=duration -of csv=p=0 "$OUT" | cut -d. -f1) + 8) / 8 )):padding=4:color=0x0A0B10" -frames:v 1 -q:v 3 "${CONTACT:-out/contact.jpg}"
python3 scripts/write_script_md.py
python3 scripts/check_frames.py "$OUT"
ffprobe -v error -show_entries format=duration:stream=codec_name,profile,width,height,r_frame_rate,pix_fmt,sample_rate,channels,bit_rate -of default=nw=1 "$OUT"
