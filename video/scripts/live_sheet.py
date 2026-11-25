#!/usr/bin/env python3
"""Contact sheet of live-ops frames: live_sheet.py out.jpg start step count [cols]"""
import subprocess, sys, os
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
out, a, step, n = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4]); cols = int(sys.argv[5]) if len(sys.argv) > 5 else 3
src = os.path.join(ROOT, 'assets', os.environ.get('SRC', 'live-ops'))
frames = [os.path.join(src, f'f{a + i * step:04d}.jpg') for i in range(n)]
frames = [f for f in frames if os.path.exists(f)]
args = []; fc = ''
for i, f in enumerate(frames):
    args += ['-i', f]; fc += f"[{i}]scale=640:360[v{i}];"
rows = (len(frames) + cols - 1) // cols
while len(frames) < rows * cols:
    fc += f"color=black:s=640x360:d=1[v{len(frames)}];"; frames.append(None)
fc += ''.join(f'[v{i}]' for i in range(rows * cols)) + f"xstack=inputs={rows*cols}:layout=" + '|'.join(f"{(i%cols)*640}_{(i//cols)*360}" for i in range(rows*cols))
subprocess.run(['ffmpeg', '-v', 'error', '-y', *args, '-filter_complex', fc, '-frames:v', '1', out], check=True)
