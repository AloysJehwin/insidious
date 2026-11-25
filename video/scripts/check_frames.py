#!/usr/bin/env python3
"""Sample the output every 0.5 s, flag near-empty frames (little detail outside the persistent chrome)."""
import subprocess, sys, numpy as np
path = sys.argv[1]; W, H = 192, 108
raw = subprocess.run(['ffmpeg', '-v', 'error', '-i', path, '-vf', f'fps=2,scale={W}:{H},format=gray', '-f', 'rawvideo', '-'], capture_output=True, check=True).stdout
fr = np.frombuffer(raw, dtype=np.uint8).reshape(-1, H, W).astype(np.float32)
body = fr[:, 14:92, :]                      # skip top chrome and caption band
gx = np.abs(np.diff(body, axis=2)).mean(axis=(1, 2)); gy = np.abs(np.diff(body, axis=1)).mean(axis=(1, 2))
detail = gx + gy
bad = [(i / 2, round(float(d), 2)) for i, d in enumerate(detail) if d < 1.2]
print(f'frames sampled: {len(detail)}  min detail {detail.min():.2f} at {np.argmin(detail)/2:.1f}s  median {np.median(detail):.2f}')
print('near-empty:', bad if bad else 'none')
