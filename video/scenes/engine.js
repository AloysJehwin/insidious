/* Deterministic timeline engine: window.render(t) draws the exact frame for time t (seconds).
   Element attributes:
     data-t   start time: "1.2" (scene-relative) | "@clip" | "@clip.end" | "@clip~word" ; optional "|+0.3" offset
     data-a   fade | up | left | right | pop | grow-x | draw | count | type
     data-d   duration (s, default 0.6) · data-out  fade-out time (same syntax)
     data-on / data-on2   add class "on" / "on2" from that time · data-show  display from that time (chat messages)
     data-from/data-to/data-fmt for counters. */
(function () {
  const TL = window.TL, FPS = TL.fps;
  const clamp = (x, a = 0, b = 1) => Math.max(a, Math.min(b, x));
  const eOut = p => 1 - Math.pow(1 - p, 3);
  const eIO = p => (p < 0.5 ? 4 * p * p * p : 1 - Math.pow(-2 * p + 2, 3) / 2);
  const lerp = (a, b, p) => a + (b - a) * p;
  const $ = s => document.querySelector(s);

  function wordT(c, word) {
    const i = c.text.toLowerCase().indexOf(word.toLowerCase());
    if (i < 0) { console.warn('word not found', word, c.text.slice(0, 30)); return c.start; }
    const a = c.anchors;
    for (let k = 0; k < a.length - 1; k++) {
      if (i >= a[k][0] && i <= a[k + 1][0]) {
        const f = (i - a[k][0]) / Math.max(1, a[k + 1][0] - a[k][0]);
        return a[k][1] + f * (a[k + 1][1] - a[k][1]);
      }
    }
    return c.start;
  }
  function R(spec, s0) {
    spec = String(spec).trim(); let off = 0; const bar = spec.indexOf('|');
    if (bar >= 0) { off = parseFloat(spec.slice(bar + 1)); spec = spec.slice(0, bar); }
    if (spec[0] !== '@') return s0 + parseFloat(spec) + off;
    let body = spec.slice(1), word = null; const tl = body.indexOf('~');
    if (tl >= 0) { word = body.slice(tl + 1); body = body.slice(0, tl); }
    let end = false; if (body.endsWith('.end')) { end = true; body = body.slice(0, -4); }
    const c = TL.clips[body]; if (!c) { console.warn('no clip', body); return s0 + off; }
    if (word) return wordT(c, word) + off;
    return (end ? c.end : c.start) + off;
  }
  window.R = R;

  const fmt = {
    inr: v => '₹' + Math.round(v).toLocaleString('en-IN'),
    int: v => Math.round(v).toLocaleString('en-IN'),
    secs: v => Math.floor(v) + ' s',
  };

  // ---- setup ----
  const scenes = TL.scenes.map(s => ({ ...s, el: document.getElementById(s.id) })).filter(s => s.el);
  const anims = [], ons = [], shows = [];
  scenes.forEach(s => {
    s.el.querySelectorAll('[data-t],[data-out]').forEach(el => {
      const a = {
        el, scene: s, type: el.dataset.a || (el.dataset.t ? 'up' : null),
        t0: el.dataset.t ? R(el.dataset.t, s.start) : null,
        d: parseFloat(el.dataset.d || (el.dataset.a === 'count' ? 1.0 : 0.6)),
        out: el.dataset.out ? R(el.dataset.out, s.start) : null,
      };
      if (a.type === 'draw') { el.setAttribute('pathLength', '1'); el.style.strokeDasharray = '1 1'; a.ms = el.getAttribute('marker-start'); a.me = el.getAttribute('marker-end'); el.removeAttribute('marker-start'); el.removeAttribute('marker-end'); a.mk = false; }
      if (a.type === 'type') a.html = el.innerHTML, a.text = el.textContent;
      if (a.type === 'count') { a.from = +el.dataset.from; a.to = +el.dataset.to; a.f = fmt[el.dataset.fmt || 'int']; }
      anims.push(a);
    });
    s.el.querySelectorAll('[data-on],[data-on2]').forEach(el => {
      if (el.dataset.on) ons.push({ el, cls: 'on', t: R(el.dataset.on, s.start), scene: s });
      if (el.dataset.on2) ons.push({ el, cls: 'on2', t: R(el.dataset.on2, s.start), scene: s });
    });
    s.el.querySelectorAll('[data-show]').forEach(el => shows.push({ el, t: R(el.dataset.show, s.start), scene: s }));
  });

  // assets
  const has = f => !!TL.assets[f];
  document.querySelectorAll('img[data-asset]').forEach(img => {
    const f = img.dataset.asset;
    if (has(f)) img.src = '../assets/' + f; else { const p = img.closest('.proof,.frame'); if (p) p.style.visibility = 'hidden'; }
  });
  const awsShot = Object.keys(TL.assets).find(f => /^aws/i.test(f));
  if (awsShot) { $('#aws-img').src = '../assets/' + awsShot; $('#aws-shot').style.display = 'block'; }
  if (has('ops-demo-desktop.png') && !$('#ops-shot').dataset.disabled) { $('#ops-img').src = '../assets/ops-demo-desktop.png'; $('#ops-shot').style.display = 'block'; $('#close').classList.add('has-ops'); }
  const cfg = TL.config || {};
  document.querySelectorAll('.cfg-live').forEach(e => e.textContent = cfg.liveUrl || 'festive.aloysjehwin.com');
  document.querySelectorAll('.cfg-live-url').forEach(e => e.textContent = 'https://' + (cfg.liveUrl || 'festive.aloysjehwin.com'));
  document.querySelectorAll('.cfg-repo').forEach(e => e.textContent = cfg.repoUrl && !/[<>]/.test(cfg.repoUrl) ? cfg.repoUrl : 'see submission repo');

  // waveform bars + bokeh
  const wave = $('#wave'); const bars = [];
  for (let i = 0; i < 24; i++) { const b = document.createElement('i'); wave.appendChild(b); bars.push(b); }
  const bokeh = $('#bokeh'); const dots = [];
  for (let i = 0; i < 26; i++) {
    const d = document.createElement('i'); const r = 30 + ((i * 53) % 70);
    d.style.width = d.style.height = r + 'px'; bokeh.appendChild(d);
    dots.push({ d, r, x: (i * 379) % 1920, y: (i * 241) % 1080, sp: 8 + (i * 7) % 22, a: 0.25 + ((i * 13) % 10) / 20 });
  }

  // fix chart path (x 50..750, y: 400 - ms/5000*380)
  const fixPts = []; for (let i = 0; i <= 70; i++) {
    const x = i / 70; let v;
    if (x < 0.15) v = 4300 + 500 * eOut(x / 0.15); else if (x < 0.54) v = 4800 + 140 * Math.sin(i * 1.3);
    else v = 443 + 4357 * (1 - eOut(clamp((x - 0.54) / 0.22))) + 25 * Math.sin(i * 2.1);
    fixPts.push([50 + x * 700, 400 - (v / 5000) * 380]);
  }
  $('#p95-line').setAttribute('d', fixPts.map((p, i) => (i ? 'L' : 'M') + p[0].toFixed(1) + ' ' + p[1].toFixed(1)).join(''));
  const tapX = 50 + 0.54 * 700; $('#tapline').setAttribute('x1', tapX); $('#tapline').setAttribute('x2', tapX); $('#taptext').setAttribute('x', tapX + 8);

  document.querySelectorAll('.vn .wv').forEach((w, k) => {
    for (let i = 0; i < 30; i++) { const b = document.createElement('b'); b.style.height = (5 + 19 * Math.abs(Math.sin(i * 0.9 + k * 1.7) * Math.cos(i * 0.37 + k))).toFixed(1) + 'px'; w.appendChild(b); }
  });
  const planBars = [...document.querySelectorAll('#vn-plan .wv b')];
  // chat message heights for scroll
  const chatScene = scenes.find(s => s.id === 'chat');
  let chatH = [];
  function measureChat() {
    const sc = chatScene.el; const prev = sc.style.display; sc.style.display = 'block';
    chatH = shows.map(m => { m.el.style.display = 'block'; const h = m.el.offsetHeight + 12; return h; });
    shows.forEach(m => m.el.style.display = 'none'); sc.style.display = prev;
  }

  // RCA zoom rects (source pixels of the 1456x839 screenshot)
  function fitRect(r, dst, cover) {
    const s = cover ? Math.max(dst[2] / r[2], dst[3] / r[3]) : Math.min(dst[2] / r[2], dst[3] / r[3]);
    return { s, x: dst[0] + (dst[2] - r[2] * s) / 2 - r[0] * s, y: dst[1] + (dst[3] - r[3] * s) / 2 - r[1] * s };
  }
  const rcaA = fitRect([0, 0, 1456, 839], [0, 0, 1920, 1080], true);
  const rcaB = fitRect([395, 300, 1030, 170], [60, 150, 1800, 420], false);

  function kb(img, p, s0 = 1.0, s1 = 1.05, dx = -12, dy = -8) {
    const e = eIO(clamp(p)); img.style.transform = `translate(${lerp(0, dx, e)}px,${lerp(0, dy, e)}px) scale(${lerp(s0, s1, e)})`;
  }

  const hooks = {
    hook(t, ls, s) {
      dots.forEach((o, i) => {
        const y = ((o.y - t * o.sp) % 1180 + 1180) % 1180 - 100;
        const x = o.x + 30 * Math.sin(t * 0.3 + i);
        o.d.style.transform = `translate(${x}px,${y}px)`; o.d.style.opacity = o.a * clamp(ls / 1.5) * clamp((s.end - t) / 0.6);
      });
    },
    meet(t, ls, s) {
      // nothing extra; diagram draws via data-a
    },
    detect(t, ls, s) {
      const pr = [['#proof-triage img', R('@c4b|-0.35', 0), R('@c4b~Claude', 0)], ['#proof-evidence img', R('@c4b~Claude|-0.35', 0), s.end]];
      pr.forEach(([sel, a, b]) => kb($(sel), (t - a) / (b - a + 0.5), 1.0, 1.05, -20, -10));
    },
    chat(t, ls, s) {
      let tot = 0;
      shows.forEach((m, i) => {
        const vis = t >= m.t; m.el.style.display = vis ? 'block' : 'none';
        const p = eOut(clamp((t - m.t) / 0.35)); m.el.style.opacity = p; m.el.style.transform = `translateY(${(1 - p) * 16}px)`;
        if (vis) tot += chatH[i] * p;
      });
      const area = 666 - 32; $('#chat-scroll').style.transform = `translateY(${-Math.max(0, tot - area)}px)`;
      const v0 = R('@c5a~replies in kind|+0.2', 0); const pr = clamp((t - v0) / 3.0); planBars.forEach((b, i) => b.classList.toggle('on', i / planBars.length < pr));
      // tap ripple + finger
      const tt = R('@c5b~taps|+0.2', 0); const rp = clamp((t - tt) / 0.5); const rip = $('#btn1 .ripple');
      rip.style.opacity = rp > 0 && rp < 1 ? (1 - rp) * 0.8 : 0; rip.style.width = rip.style.height = (30 + rp * 380) + 'px';
      const b = $('#btn1').getBoundingClientRect(); const f = $('#finger');
      const fp = eIO(clamp((t - (tt - 0.7)) / 0.7));
      f.style.left = (b.left + b.width / 2 - 32 + (1 - fp) * 120) + 'px'; f.style.top = (b.top + b.height / 2 - 32 + (1 - fp) * 140) + 'px';
      f.style.transform = `scale(${t > tt && t < tt + 0.25 ? 0.85 : 1})`;
    },
    fix(t, ls, s) {
      const a0 = s.start + 0.5, a1 = R('@c6a~drops|-0.1', 0);
      const f = t < a1 ? 0.54 * eOut(clamp((t - a0) / 1.4)) : 0.54 + 0.46 * eIO(clamp((t - a1) / 2.6));
      $('#p95-line').style.strokeDasharray = '1 1'; $('#p95-line').style.strokeDashoffset = 1 - f;
      const tapOn = t >= R('@c6a|+0.2', 0); $('#tapline').style.opacity = tapOn ? 1 : 0; $('#taptext').style.opacity = tapOn ? 1 : 0;
      const rd = $('#p95-read').parentElement; const val = parseInt($('#p95-read').textContent.replace(/,/g, ''), 10);
      rd.classList.toggle('bad', val > 800); rd.classList.toggle('good', val <= 800);
    },
    rca(t, ls, s) {
      const z0 = R('@c7|+0.4', 0), z1 = z0 + 2.2; const p = eIO(clamp((t - z0) / (z1 - z0)));
      const drift = 0.012 * clamp((t - z1) / 4);
      const sc = lerp(rcaA.s, rcaB.s, p) * (1 + drift), x = lerp(rcaA.x, rcaB.x, p), y = lerp(rcaA.y, rcaB.y, p);
      $('#rca-wrap').style.transform = `translate(${x}px,${y}px) scale(${sc})`;
      $('#rca-hl').style.opacity = clamp((t - z1 + 0.3) / 0.4);
    },
    hood(t, ls, s) {
      [['#hood-fs img', 'c9a'], ['#hood-nb img', 'c9b'], ['#aws-img', 'c9d']].forEach(([sel, c]) => {
        const cl = TL.clips[c]; const el = $(sel); if (el) kb(el, (t - cl.start + 0.3) / (cl.end - cl.start + 0.6), 1.0, 1.045, -14, -8);
      });
    },
    close(t, ls, s) {
      const el = $('#ops-img'); if (el && el.src) kb(el, ls / (s.end - s.start), 1.0, 1.05, -10, -6);
    },
  };

  function applyAnim(a, t) {
    const el = a.el;
    let o = 1, tf = '';
    if (a.t0 !== null && a.type) {
      const p = clamp((t - a.t0) / a.d), e = eOut(p);
      switch (a.type) {
        case 'fade': o = e; break;
        case 'up': o = e; tf = `translateY(${(1 - e) * 26}px)`; break;
        case 'left': o = e; tf = `translateX(${-(1 - e) * 36}px)`; break;
        case 'right': o = e; tf = `translateX(${(1 - e) * 36}px)`; break;
        case 'pop': o = e; tf = `scale(${0.94 + 0.06 * e})`; break;
        case 'grow-x': tf = `scaleX(${eIO(p)})`; break;
        case 'draw': { el.style.strokeDashoffset = 1 - eIO(p); o = p > 0 ? 1 : 0; const want = p >= 0.97; if (want !== a.mk) { a.mk = want; ['ms','me'].forEach(k => { const at = k === 'ms' ? 'marker-start' : 'marker-end'; if (a[k]) { if (want) el.setAttribute(at, a[k]); else el.removeAttribute(at); } }); } break; }
        case 'count': el.textContent = a.f(lerp(a.from, a.to, eIO(p))); break;
        case 'type': {
          o = p > 0 ? 1 : 0; const n = Math.round(a.text.length * p);
          if (p >= 1) { if (el.innerHTML !== a.html) el.innerHTML = a.html; }
          else el.textContent = a.text.slice(0, n);
          break;
        }
      }
    }
    if (a.out !== null) o *= 1 - eOut(clamp((t - a.out) / 0.4));
    if (a.type !== 'count' || a.out !== null) el.style.opacity = o;
    if (tf) el.style.transform = tf;
  }

  function render(t) {
    const frame = Math.round(t * FPS);
    const XF = 0.45, last = scenes[scenes.length - 1];
    scenes.forEach((s, i) => {
      const vis = t >= s.start - 0.001 && (s === last ? t <= s.end + 0.001 : t < s.end + XF);
      s.el.style.display = vis ? 'block' : 'none';
      if (!vis) return;
      const fi = i === 0 ? 1 : eIO(clamp((t - s.start) / XF)), fo = s === last ? 1 : 1 - eIO(clamp((t - s.end) / XF));
      s.el.style.opacity = Math.min(fi, fo);
    });
    anims.forEach(a => { if (a.scene.el.style.display === 'block') applyAnim(a, t); });
    ons.forEach(o => { if (o.scene.el.style.display === 'block') o.el.classList.toggle(o.cls, t >= o.t); });
    scenes.forEach(s => { if (s.el.style.display === 'block' && hooks[s.id]) hooks[s.id](t, t - s.start, s); });

    // background drift
    $('.g1').style.transform = `translate(${Math.sin(t * 0.05) * 80}px,${Math.cos(t * 0.04) * 50}px)`;
    $('.g2').style.transform = `translate(${Math.cos(t * 0.045) * 90}px,${Math.sin(t * 0.05) * 60}px)`;

    // chrome: chapter, narrator, captions, progress
    const cur = scenes.find(s => t >= s.start && t < s.end) || scenes[scenes.length - 1];
    const ch = $('#chapter'); ch.textContent = cur.chapter || ''; ch.style.display = cur.chapter ? 'inline' : 'none';
    const v = TL.env[Math.min(TL.env.length - 1, frame)] || 0;
    bars.forEach((b, i) => {
      const jitter = 0.35 + 0.65 * Math.abs(Math.sin(i * 1.71 + frame * 0.37 + Math.sin(i * 0.63 + frame * 0.13) * 2));
      const shape = 0.55 + 0.45 * Math.sin((i / 23) * Math.PI);
      b.style.height = Math.max(3, Math.min(38, 3 + 40 * v * jitter * shape)).toFixed(1) + 'px';
      b.style.opacity = 0.45 + 0.55 * clamp(v * 2);
    });
    $('#orb').style.boxShadow = `0 0 ${(6 + 26 * v).toFixed(1)}px ${(2 + 8 * v).toFixed(1)}px rgba(255,181,71,${(0.15 + 0.5 * clamp(v)).toFixed(2)})`;
    $('#orb').style.transform = `scale(${(1 + 0.08 * clamp(v)).toFixed(3)})`;
    const cap = TL.captions.find(c => t >= c.start && t < c.end);
    const cel = $('#caption');
    if (cap) {
      $('#ct').textContent = cap.text; const cs = $('#cs'); cs.textContent = cap.sub || ''; cs.style.display = cap.sub ? 'block' : 'none';
      cel.style.opacity = Math.min(clamp((t - cap.start) / 0.12), clamp((cap.end - t) / 0.12));
    } else cel.style.opacity = 0;
    $('#prog').style.width = (100 * t / TL.duration).toFixed(2) + '%';
    const brand = $('#brand'); brand.style.opacity = t < R('@c1|-0.3', 0) ? 0 : 1;
  }

  // ---- live footage layers (TL.live from live.json): frame sequence time-mapped with speed ramps + punch-in zooms ----
  const lives = [];
  (TL.live && TL.live.sections || []).forEach((sec, k) => {
    const sc = scenes.find(s => s.id === sec.scene); if (!sc) return;
    const el = document.createElement('div'); el.className = 'live-layer';
    el.innerHTML = `<div class="live-win"><div class="live-bar"><i></i><span class="lt"></span><em class="chip-speed"></em></div>
      <div class="live-view"><div class="live-zoom"><img class="la"><img class="lb"><div class="brandmask"><i></i><b>Insidious</b><span>/</span>Jeffi Stores<span>/</span><em>Mission control</em></div><div class="live-hl"></div></div></div></div>`;
    sc.el.appendChild(el);
    const keys = sec.keys.map(([sp, f]) => [R(sp, sc.start), f]).sort((a, b) => a[0] - b[0]);
    const zoom = (sec.zoom || [[sec.from, 1, 0.5, 0.5]]).map(([sp, s, x, y]) => [R(sp, sc.start), s, x, y]).sort((a, b) => a[0] - b[0]);
    const hls = (sec.highlights || []).map(h => ({ t0: R(h.from, sc.start), t1: R(h.to, sc.start), r: h.rect, label: h.label || '' }));
    el.querySelector('.lt').textContent = sec.label || 'LIVE · ' + (cfg.liveUrl || 'festive.aloysjehwin.com') + '/ops';
    lives.push({ sec, sc, el, keys, zoom, hls, t0: R(sec.from, sc.start), t1: R(sec.to, sc.start),
      za: el.querySelector('.live-zoom'), ia: el.querySelector('.la'), ib: el.querySelector('.lb'), hl: el.querySelector('.live-hl'), sp: el.querySelector('.chip-speed') });
  });
  const pw = (arr, t, f) => { // piecewise interpolation with easing f between keys
    if (t <= arr[0][0]) return arr[0].slice(1);
    for (let i = 0; i < arr.length - 1; i++) if (t <= arr[i + 1][0]) {
      const p = f(clamp((t - arr[i][0]) / Math.max(1e-6, arr[i + 1][0] - arr[i][0])));
      return arr[i].slice(1).map((v, j) => lerp(v, arr[i + 1][j + 1], p));
    }
    return arr[arr.length - 1].slice(1);
  };
  const srcPath = (sec, i) => '../' + sec.source + '/' + sec.pattern.replace(/%0(\d)d/, (_, n) => String(i).padStart(+n, '0'));
  async function setImg(img, src) { if (img.dataset.src !== src) { img.dataset.src = src; img.src = src; await img.decode().catch(() => {}); } }
  async function renderLive(t) {
    const jobs = [];
    for (const L of lives) {
      const vis = L.sc.el.style.display === 'block' && t >= L.t0 - 0.01 && t <= L.t1 + 0.4;
      L.el.style.display = vis ? 'block' : 'none'; if (!vis) continue;
      L.el.style.opacity = Math.min(eIO(clamp((t - L.t0) / 0.4)), 1 - eIO(clamp((t - L.t1) / 0.4)));
      const F = clamp(pw(L.keys, t, p => p)[0], 1, L.sec.count);
      const i = Math.floor(F), fr = F - i, j = Math.min(L.sec.count, i + 1);
      jobs.push(setImg(L.ia, srcPath(L.sec, i)), setImg(L.ib, srcPath(L.sec, j)));
      L.ib.style.opacity = fr.toFixed(3);
      const [s, fx, fy] = pw(L.zoom, t, eIO);
      const base = 0.8, Fx = fx * 1920, Fy = fy * 1080;
      L.za.style.transform = `translate(${(base * Fx * (1 - s)).toFixed(2)}px,${(base * Fy * (1 - s)).toFixed(2)}px) scale(${(base * s).toFixed(4)})`;
      // speed chip: source seconds per output second
      const dt = 0.25, rate = (pw(L.keys, t + dt, p => p)[0] - pw(L.keys, t - dt, p => p)[0]) / (2 * dt) * (L.sec.secondsPerFrame || TL.live.secondsPerFrame || 1);
      L.sp.textContent = rate > 1.6 ? `▸▸ ${Math.round(rate)}× speed` : 'real time';
      L.sp.style.opacity = 0.9;
      const h = L.hls.find(h => t >= h.t0 && t <= h.t1);
      if (h) {
        const [x, y, w, hh] = h.r; Object.assign(L.hl.style, { left: x * 100 + '%', top: y * 100 + '%', width: w * 100 + '%', height: hh * 100 + '%' });
        L.hl.style.opacity = Math.min(clamp((t - h.t0) / 0.3), clamp((h.t1 - t) / 0.3)); L.hl.dataset.label = h.label;
      } else L.hl.style.opacity = 0;
    }
    await Promise.all(jobs);
  }

  window.render = async t => { render(t); await renderLive(t); };
  window.ready = (async () => {
    await document.fonts.ready;
    await Promise.all([...document.images].filter(i => i.src).map(i => i.decode().catch(() => {})));
    measureChat();
    render(0);
    return true;
  })();
})();
