#!/usr/bin/env python3
"""Lay narration clips on a scene timeline, write build/narration.wav, build/pad.wav and build/timeline.js
(scene times, clip times + word anchors, captions, per-frame voice envelope, asset list)."""
import json, os, re, subprocess, wave
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
B = os.path.join(ROOT, 'build')
cfg = json.load(open(os.path.join(ROOT, os.environ.get('NARRATION', 'narration.json'))))
conf = json.load(open(os.path.join(ROOT, 'config.json')))
FPS = conf.get('fps', 30)
SR = 24000
DEFAULT_GAP = 0.35

def load(path):
    w = wave.open(path)
    assert w.getnchannels() == 1 and w.getframerate() == SR, path
    return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0

def trim(a, thr=0.004):
    e = np.sqrt(np.convolve(a ** 2, np.ones(240) / 240, 'same'))
    idx = np.nonzero(e > thr)[0]
    if not len(idx):
        return a
    s = max(0, idx[0] - 480); t = min(len(a), idx[-1] + 1200)
    return a[s:t]

def pauses(a, min_len=0.11, thr=0.006):
    e = np.sqrt(np.convolve(a ** 2, np.ones(240) / 240, 'same'))[::240]  # 10 ms
    sil = e < thr; out = []; s = None
    for i, x in enumerate(list(sil) + [False]):
        if x and s is None: s = i
        if not x and s is not None:
            if (i - s) * 0.01 >= min_len and s > 0 and i < len(sil): out.append((s + i) * 0.005)
            s = None
    return out

def chunks(text, maxlen=62):
    parts = [p.strip() for p in re.split(r'(?<=[.?!:])\s+', text) if p.strip()]
    out = []
    for p in parts:
        while len(p) > maxlen:
            cut = max((m.end() for m in re.finditer(r',\s', p[:maxlen + 1])), default=0)
            if cut < 18:
                cut = p.rfind(' ', 0, maxlen)
            out.append(p[:cut].strip().rstrip(',')); p = p[cut:].strip()
        out.append(p)
    # merge tiny tails
    merged = []
    for c in out:
        if merged and len(c) < 14 and len(merged[-1]) + len(c) < maxlen:
            merged[-1] += ' ' + c
        else:
            merged.append(c)
    return merged

clips = {c['id']: c for c in cfg['clips']}
AUDDIR = os.path.join(B, 'audio' + os.environ.get('NARRATION', 'narration.json').replace('narration', '').replace('.json', ''))
audio = {cid: trim(load(os.path.join(AUDDIR, f'{cid}.wav'))) for cid in clips}

t = 0.0; scenes = []; placed = {}
for si, sc in enumerate(cfg['scenes']):
    start = t
    J = sc.get('jcut', 0.0) if si else 0.0
    ct = start - J if J > 0 else start + sc.get('lead', 0.5)   # J-cut: narration leads the picture cut
    ids = [c['id'] for c in cfg['clips'] if c['scene'] == sc['id']]
    for i, cid in enumerate(ids):
        if i: ct += sc.get('gap', DEFAULT_GAP)
        d = len(audio[cid]) / SR
        placed[cid] = (ct, ct + d); ct += d
    t = ct + sc.get('tail', 0.8)
    t = max(t, start + sc.get('min', 0))
    scenes.append({'id': sc['id'], 'chapter': sc.get('chapter', ''), 'start': round(start, 3), 'end': round(t, 3)})
total = round(t, 3)
nframes = int(round(total * FPS))
total = nframes / FPS
scenes[-1]['end'] = total

# narration track
track = np.zeros(int(total * SR) + SR, dtype=np.float32)
for cid, (s, e) in placed.items():
    a = audio[cid]; i0 = int(s * SR); track[i0:i0 + len(a)] += a
track = track[:int(total * SR)]
peak = np.max(np.abs(track)) or 1
track = track / peak * 0.89
with wave.open(os.path.join(B, 'narration.wav'), 'wb') as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR)
    w.writeframes((np.clip(track, -1, 1) * 32767).astype(np.int16).tobytes())

# per-frame envelope (RMS, normalised, attack/release smoothed)
hop = SR / FPS; env = []
for f in range(nframes):
    seg = track[int(f * hop):int((f + 1) * hop)]
    env.append(float(np.sqrt(np.mean(seg ** 2))) if len(seg) else 0.0)
env = np.array(env); ref = np.percentile(env[env > 0.01], 90) if np.any(env > 0.01) else 1
env = np.clip(env / ref, 0, 1.2); sm = np.zeros_like(env); v = 0
for i, x in enumerate(env):
    v = v + (x - v) * (0.6 if x > v else 0.25); sm[i] = v

# captions + word anchors (chunk boundaries snapped to real pauses)
def align(cid, text, s, dur):
    parts = chunks(text); ps = pauses(audio[cid]); total_chars = sum(len(p) for p in parts)
    bounds = [0.0]; acc = 0
    for p in parts[:-1]:
        acc += len(p); est = dur * acc / total_chars
        near = [x for x in ps if abs(x - est) < 0.9]
        bounds.append(min(near, key=lambda x: abs(x - est)) if near else est)
    bounds.append(dur); bounds = sorted(bounds)
    return [(p, s + bounds[i], s + bounds[i + 1]) for i, p in enumerate(parts)]

captions = []; clipinfo = {}
for cid, (s, e) in placed.items():
    c = clips[cid]; dur = e - s
    if c.get('caption'):
        captions.append({'start': round(s, 3), 'end': round(e + 0.2, 3), 'text': c['caption'], 'sub': c.get('captionSub', '')})
        clipinfo[cid] = {'start': round(s, 3), 'end': round(e, 3), 'text': c['text'], 'anchors': [[0, s], [len(c['text']), e]]}
        continue
    anchors = []; pos = 0
    for p, a, b in align(cid, c['text'], s, dur):
        idx = c['text'].find(p.split(' ')[0], pos); idx = pos if idx < 0 else idx
        anchors.append([idx, round(a, 3)]); pos = idx + len(p)
    anchors.append([len(c['text']), round(e, 3)])
    clipinfo[cid] = {'start': round(s, 3), 'end': round(e, 3), 'text': c['text'], 'anchors': anchors}
    al = align(cid, c.get('captionText', c['text']), s, dur)
    for i, (p, a, b) in enumerate(al):
        captions.append({'start': round(a, 3), 'end': round(b + (0.25 if i == len(al) - 1 else 0), 3), 'text': p})

assets = sorted(f for f in os.listdir(os.path.join(ROOT, 'assets')) if re.search(r'\.(png|jpe?g|webp)$', f, re.I))
sizes = {}
for f in assets:
    try:
        out = subprocess.check_output(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=width,height', '-of', 'csv=p=0', os.path.join(ROOT, 'assets', f)]).decode().strip().split(',')
        sizes[f] = [int(out[0]), int(out[1])]
    except Exception:
        pass
live = None
lp = os.path.join(ROOT, 'live.json')
if os.path.exists(lp):
    live = json.load(open(lp))
    def count(src, pattern):
        srcdir = os.path.join(ROOT, src)
        rx = re.compile('^' + re.sub(r'%0(\d)d', lambda m: r'\d{%s}' % m.group(1), re.escape(pattern).replace('\\%', '%')) + '$')
        return len([f for f in os.listdir(srcdir) if rx.match(f)]) if os.path.isdir(srcdir) else 0
    secs = []
    for sec in live.get('sections', []):
        sec.setdefault('source', live.get('source')); sec.setdefault('pattern', live.get('pattern'))
        sec['count'] = count(sec['source'], sec['pattern'])
        if sec['count'] and sec.get('enabled', True): secs.append(sec)
    live['sections'] = secs
    if not secs or not live.get('enabled', True):
        live = None
tl = {'live': live, 'fps': FPS, 'duration': total, 'frames': nframes, 'scenes': scenes, 'clips': clipinfo, 'captions': captions,
      'env': [round(float(x), 3) for x in sm], 'assets': sizes, 'config': conf}
open(os.path.join(B, 'timeline.js'), 'w').write('window.TL = ' + json.dumps(tl, ensure_ascii=False) + ';\n')
json.dump({'duration': total, 'scenes': scenes, 'clips': {k: [v['start'], v['end']] for k, v in clipinfo.items()}}, open(os.path.join(B, 'timeline.json'), 'w'), indent=1)

# ambient pad: soft detuned A-major drone, lowpassed, very quiet, faded
D = total
vol = conf.get('musicVolume', 0.05)
srcs = []
for f in (110.0, 110.35, 164.81, 220.0, 277.18, 329.63):
    srcs += ['-f', 'lavfi', '-i', f'sine=f={f}:d={D}:sample_rate=48000']
srcs += ['-f', 'lavfi', '-i', f'anoisesrc=c=pink:a=0.03:d={D}:r=48000']
n = len(srcs) // 4
fc = ''.join(f'[{i}]' for i in range(n)) + f'amix=inputs={n}:normalize=0,lowpass=f=650,tremolo=f=0.12:d=0.3,volume={vol},afade=t=in:d=4,afade=t=out:st={max(0, D - 5):.2f}:d=5[a]'
subprocess.run(['ffmpeg', '-y', '-v', 'error', *srcs, '-filter_complex', fc, '-map', '[a]', '-ac', '1', os.path.join(B, 'pad.wav')], check=True)
print(f'timeline: {total:.2f}s, {nframes} frames, {len(scenes)} scenes, {len(captions)} captions')
for s in scenes: print(f"  {s['id']:8s} {s['start']:7.2f} -> {s['end']:7.2f}  ({s['end']-s['start']:.1f}s)")
