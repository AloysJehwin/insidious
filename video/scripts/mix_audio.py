#!/usr/bin/env python3
"""Narration -> -16 LUFS (two-pass loudnorm); ambient pad ducked -18 dB under speech with 300 ms ramps; mix -> build/mix.wav (48 kHz)."""
import json, os, re, subprocess, wave
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
B = os.path.join(ROOT, 'build')
conf = json.load(open(os.path.join(ROOT, 'config.json')))

def run(args):
    return subprocess.run(args, capture_output=True, text=True, check=True)

def read(path):
    w = wave.open(path); sr = w.getframerate()
    a = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
    return a, sr

def write(path, a, sr):
    with wave.open(path, 'wb') as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes((np.clip(a, -1, 1) * 32767).astype(np.int16).tobytes())

def lufs(path):
    err = subprocess.run(['ffmpeg', '-nostats', '-i', path, '-af', 'ebur128', '-f', 'null', '-'], capture_output=True, text=True).stderr
    return float(re.findall(r'I:\s+(-?[\d.]+) LUFS', err)[-1])

# 1. narration to -16 LUFS integrated, -1.5 dBTP, 48 kHz (two-pass, linear)
nar = os.path.join(B, 'narration.wav')
err = subprocess.run(['ffmpeg', '-nostats', '-i', nar, '-af', 'loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json', '-f', 'null', '-'], capture_output=True, text=True).stderr
m = json.loads(err[err.rindex('{'):err.rindex('}') + 1])
af = (f"loudnorm=I=-16:TP=-1.5:LRA=11:measured_I={m['input_i']}:measured_TP={m['input_tp']}:measured_LRA={m['input_lra']}"
      f":measured_thresh={m['input_thresh']}:offset={m['target_offset']}:linear=true,aresample=48000")
run(['ffmpeg', '-y', '-v', 'error', '-i', nar, '-af', af, '-ar', '48000', os.path.join(B, 'narration48.wav')])
n48, sr = read(os.path.join(B, 'narration48.wav'))

# 2. ducking gain from speech activity: -18 dB under speech, 300 ms ramps
win = int(0.02 * sr)
env = np.sqrt(np.convolve(n48 ** 2, np.ones(win) / win, 'same'))
speech = (env > 0.01).astype(np.float32)
hold = int(0.25 * sr)
speech = (np.convolve(speech, np.ones(hold), 'same') > 0).astype(np.float32)  # fill short gaps
duck = 10 ** (-18 / 20)
target = 1.0 - (1.0 - duck) * speech
ramp = int(0.3 * sr)
gain = np.convolve(target, np.ones(ramp) / ramp, 'same')

# 3. pad scaled so its unducked level is about -30 LUFS, then ducked
pad, psr = read(os.path.join(B, 'pad.wav')); assert psr == sr
pad = pad[:len(n48)]
if len(pad) < len(n48): pad = np.pad(pad, (0, len(n48) - len(pad)))
pl = lufs(os.path.join(B, 'pad.wav'))
pad = pad * 10 ** ((conf.get('musicLufs', -30) - pl) / 20) * gain
mix = n48 + pad
peak = np.max(np.abs(mix))
if peak > 0.89: mix *= 0.89 / peak
write(os.path.join(B, 'mix.wav'), mix, sr)
print(f"narration in {m['input_i']} LUFS -> -16; pad {pl:.1f} -> {conf.get('musicLufs', -30)} LUFS (ducked -18 dB); mix {lufs(os.path.join(B, 'mix.wav')):.1f} LUFS, peak {20*np.log10(np.max(np.abs(mix))):.1f} dBFS")
