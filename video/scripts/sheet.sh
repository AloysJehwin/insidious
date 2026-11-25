#!/usr/bin/env bash
# contact sheet of 4 stills: scripts/sheet.sh out.png 42 63 70 74
cd "$(dirname "$0")/../build/stills" && out=$1; shift
f() { printf "t-%.1f.png" "$1"; }
ffmpeg -v error -y -i $(f $1) -i $(f $2) -i $(f $3) -i $(f $4) -filter_complex "[0]scale=960:540[a];[1]scale=960:540[b];[2]scale=960:540[c];[3]scale=960:540[d];[a][b]hstack[t];[c][d]hstack[u];[t][u]vstack" "$out"
