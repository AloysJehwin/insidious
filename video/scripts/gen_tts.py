#!/usr/bin/env python3
"""Generate one Sarvam Bulbul TTS clip per narration line (cached by text hash).
Reads SARVAM_API_KEY from ../../.env (never printed). Writes build/audio/<id>.wav + manifest.json."""
import base64, hashlib, json, os, subprocess, sys, time, urllib.error, urllib.request
from dotenv import dotenv_values

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AUD = os.path.join(ROOT, 'build', 'audio' + os.environ.get('NARRATION', 'narration.json').replace('narration', '').replace('.json', ''))
os.makedirs(AUD, exist_ok=True)
cfg = json.load(open(os.path.join(ROOT, os.environ.get('NARRATION', 'narration.json'))))
key = os.environ.get('SARVAM_API_KEY') or dotenv_values(os.path.join(ROOT, '..', '.env')).get('SARVAM_API_KEY')
if not key:
    sys.exit('SARVAM_API_KEY not found (env or festivekit/.env)')

def tts(body):
    req = urllib.request.Request('https://api.sarvam.ai/text-to-speech', data=json.dumps(body).encode(),
                                 headers={'api-subscription-key': key, 'Content-Type': 'application/json'})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            msg = e.read().decode()[:300]
            if e.code in (429, 500, 502, 503) and attempt < 2:
                time.sleep(2 + attempt * 3); continue
            sys.exit(f'Sarvam TTS {e.code}: {msg}')

def duration(path):
    return float(subprocess.check_output(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', path]).decode())

manifest = {}
v = cfg['voice']
for c in cfg['clips']:
    body = {'text': c['text'][:2500], 'language_code': c.get('lang', v['language_code']), 'model': v['model'],
            'speaker': c.get('speaker', v['speaker']), 'pace': c.get('pace', v['pace']), 'speech_sample_rate': 24000}
    h = hashlib.sha1(json.dumps(body, sort_keys=True).encode()).hexdigest()[:12]
    wav = os.path.join(AUD, f"{c['id']}.wav")
    meta = os.path.join(AUD, f"{c['id']}.hash")
    if not (os.path.exists(wav) and os.path.exists(meta) and open(meta).read() == h):
        res = tts(body)
        audios = res.get('audios') or []
        if not audios:
            sys.exit(f"no audio for {c['id']}")
        raw = b''.join(base64.b64decode(a) for a in audios[:1])
        open(wav, 'wb').write(raw)
        open(meta, 'w').write(h)
        print(f"generated {c['id']} ({len(c['text'])} chars)")
    manifest[c['id']] = {'file': wav, 'duration': duration(wav), 'chars': len(c['text'])}
json.dump(manifest, open(os.path.join(AUD, 'manifest.json'), 'w'), indent=1)
tot = sum(m['duration'] for m in manifest.values())
print(f"clips: {len(manifest)}  narration chars: {sum(len(c['text']) for c in cfg['clips'] if not c.get('sfx'))}  speech seconds: {tot:.1f}")
