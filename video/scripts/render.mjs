// Render scenes/index.html frame-by-frame with Playwright (deterministic window.render(t)) and encode H.264.
// Usage: node scripts/render.mjs                -> build/video.mp4 (all frames, parallel workers)
//        node scripts/render.mjs --stills 3,20  -> build/stills/t-<sec>.png
import { chromium } from 'playwright';
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const B = path.join(ROOT, 'build');
const conf = JSON.parse(fs.readFileSync(path.join(ROOT, 'config.json'), 'utf8'));
const tlSrc = fs.readFileSync(path.join(B, 'timeline.js'), 'utf8');
const TL = JSON.parse(tlSrc.replace(/^window\.TL = /, '').replace(/;\s*$/, ''));
const FPS = TL.fps, N = TL.frames;
const url = pathToFileURL(path.join(ROOT, 'scenes', 'index.html')).href;
const args = process.argv.slice(2);

async function openPage(browser) {
  const page = await browser.newPage({ viewport: { width: 1920, height: 1080 }, deviceScaleFactor: 1 });
  page.on('console', m => { if (m.type() === 'warning' || m.type() === 'error') console.log('[page]', m.text()); });
  page.on('pageerror', e => console.log('[pageerror]', e.message));
  await page.goto(url, { waitUntil: 'load' });
  await page.evaluate(() => window.ready);
  return page;
}

async function launch() {
  const opts = { args: ['--font-render-hinting=none'] };
  try { return await chromium.launch(opts); } catch (e) {
    // fall back to any Playwright headless shell already in the cache (e.g. a newer revision)
    const cache = path.join(process.env.HOME, 'Library/Caches/ms-playwright');
    const cands = fs.existsSync(cache) ? fs.readdirSync(cache).filter(d => d.startsWith('chromium_headless_shell-')).sort().reverse() : [];
    for (const d of cands) for (const sub of ['chrome-headless-shell-mac-arm64/chrome-headless-shell', 'chrome-mac/headless_shell', 'chrome-headless-shell-linux64/chrome-headless-shell']) {
      const exe = path.join(cache, d, sub); if (fs.existsSync(exe)) { console.log('using', exe); return chromium.launch({ ...opts, executablePath: exe }); }
    }
    throw e;
  }
}
const browser = await launch();
if (args[0] === '--stills') {
  const outDir = path.join(B, 'stills'); fs.mkdirSync(outDir, { recursive: true });
  const page = await openPage(browser);
  for (const s of args[1].split(',').map(Number)) {
    await page.evaluate(t => window.render(t), s);
    await page.screenshot({ path: path.join(outDir, `t-${s.toFixed(1)}.png`) });
  }
  console.log('stills written to', outDir);
  await browser.close(); process.exit(0);
}

const W = Math.max(1, conf.workers || 6);
const segDir = path.join(B, 'segments'); fs.rmSync(segDir, { recursive: true, force: true }); fs.mkdirSync(segDir, { recursive: true });
const per = Math.ceil(N / W); let done = 0; const t0 = Date.now();
async function worker(w) {
  const a = w * per, b = Math.min(N, a + per); if (a >= b) return null;
  const page = await openPage(browser);
  const out = path.join(segDir, `seg-${String(w).padStart(2, '0')}.mp4`);
  const ff = spawn('ffmpeg', ['-y', '-v', 'error', '-f', 'image2pipe', '-framerate', String(FPS), '-c:v', 'mjpeg', '-i', '-',
    '-vf', 'scale=in_range=full:out_range=tv,format=yuv420p', '-c:v', 'libx264', '-profile:v', 'high', '-preset', 'medium', '-crf', '18', '-pix_fmt', 'yuv420p', '-color_range', 'tv', '-colorspace', 'bt709', '-color_primaries', 'bt709', '-color_trc', 'bt709', '-r', String(FPS), out], { stdio: ['pipe', 'inherit', 'inherit'] });
  for (let f = a; f < b; f++) {
    await page.evaluate(t => window.render(t), f / FPS);
    const buf = await page.screenshot({ type: 'jpeg', quality: 95 });
    if (!ff.stdin.write(buf)) await new Promise(r => ff.stdin.once('drain', r));
    done++;
    if (done % 300 === 0) console.log(`  ${done}/${N} frames (${((Date.now() - t0) / 1000).toFixed(0)} s)`);
  }
  ff.stdin.end(); await new Promise((res, rej) => ff.on('close', c => (c ? rej(new Error('ffmpeg ' + c)) : res())));
  await page.close(); return out;
}
const segs = (await Promise.all([...Array(W).keys()].map(worker))).filter(Boolean);
await browser.close();
fs.writeFileSync(path.join(segDir, 'list.txt'), segs.map(s => `file '${s}'`).join('\n'));
await new Promise((res, rej) => spawn('ffmpeg', ['-y', '-v', 'error', '-f', 'concat', '-safe', '0', '-i', path.join(segDir, 'list.txt'), '-c', 'copy', path.join(B, 'video.mp4')], { stdio: 'inherit' }).on('close', c => (c ? rej(new Error('concat ' + c)) : res())));
console.log(`video: ${N} frames in ${((Date.now() - t0) / 1000).toFixed(0)} s -> build/video.mp4`);
