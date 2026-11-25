#!/usr/bin/env python3
"""Write out/script.md: narration per scene with exact timings from build/timeline.json."""
import json, os
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
cfg = json.load(open(os.path.join(ROOT, os.environ.get('NARRATION', 'narration.json'))))
tl = json.load(open(os.path.join(ROOT, 'build', 'timeline.json')))
v = cfg['voice']
fmt = lambda s: f"{int(s // 60)}:{s % 60:05.2f}"
L = ['# Insidious demo: narration script', '',
     f"Total runtime **{fmt(tl['duration'])}** ({tl['duration']:.1f} s). Narrator: Sarvam AI **{v['model']}**, speaker `{v['speaker']}`, "
     f"`{v['language_code']}`, pace {v['pace']}. Tanglish voice-note line: `ta-IN`. Edit `narration.json`, then run `./render.sh`.", '',
     'Money figures are labelled as estimates on screen: ₹1,800 average order × 600 orders/hour; ≈7% fewer orders per extra second; '
     'checkout under 1 s → 4.8 s measured ⇒ ≈28% ⇒ 168 orders ⇒ ₹3,02,400 per hour.', '']
for sc in tl['scenes']:
    chap = next((s.get('chapter') for s in cfg['scenes'] if s['id'] == sc['id']), '') or 'Close'
    L += [f"## {fmt(sc['start'])}–{fmt(sc['end'])} · {chap} (`{sc['id']}`)", '']
    for c in cfg['clips']:
        if c['scene'] != sc['id']: continue
        s, e = tl['clips'][c['id']]
        tag = ' *(Bulbul ta-IN voice note, plays in the chat scene)*' if c.get('lang') else ''
        L.append(f"- **{fmt(s)}–{fmt(e)}** `{c['id']}`{tag}: {c['text']}")
        if c.get('captionSub'): L.append(f"  - caption: {c['caption']} / {c['captionSub']}")
    L.append('')
os.makedirs(os.path.join(ROOT, 'out'), exist_ok=True)
open(os.path.join(ROOT, 'out', os.environ.get('SCRIPT_MD', 'script.md')), 'w').write('\n'.join(L))
print('wrote out/script.md')
