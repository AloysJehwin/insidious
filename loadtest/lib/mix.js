// Shared festive traffic mix for rehearse.js and festive-rush.js (k6 v2).
// Mix per iteration: ~60% browse, ~25% product detail + recommendations, ~15% add to cart + checkout.
// That averages REQS_PER_ITER HTTP requests per iteration, so arrival rates are converted from
// requests/second (what guardian and the readiness report talk about) to iterations/second.
import http from 'k6/http';
import { check } from 'k6';

export const REQS_PER_ITER = 1.4;

export function iterRate(rps) {
  return Math.max(1, Math.round(Number(rps) / REQS_PER_ITER));
}

export function baseUrl() {
  return (__ENV.STORE_URL || __ENV.BASE_URL || 'http://localhost:8000').replace(/\/+$/, '');
}

export const THRESHOLDS = {
  // Always-passing thresholds: they only make k6 report per-route submetrics in the summary.
  'http_req_duration{name:checkout}': ['p(95)>=0'],
  'http_req_failed{name:checkout}': ['rate>=0'],
  'http_req_duration{name:home}': ['p(95)>=0'],
  'http_req_failed{name:home}': ['rate>=0'],
};

export const SUMMARY_TREND_STATS = ['avg', 'min', 'med', 'max', 'p(90)', 'p(95)', 'p(99)'];

const FALLBACK_IDS = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12];

export function setupProducts(base) {
  const res = http.get(`${base}/api/products`, { tags: { name: 'setup' }, responseType: 'text', timeout: '10s' });
  if (res.status !== 200) return FALLBACK_IDS;
  try {
    let body = res.json();
    if (!Array.isArray(body)) body = body.products || body.items || body.results || [];
    const ids = body.map((p) => (p && typeof p === 'object' ? p.id : p)).filter((x) => x !== undefined && x !== null);
    return ids.length ? ids : FALLBACK_IDS;
  } catch (e) {
    return FALLBACK_IDS;
  }
}

// One cookie jar per VU (module scope is per VU), so the store's fk_sid cart survives across requests.
let jar = null;
function vuJar() {
  if (jar === null) jar = new http.CookieJar();
  return jar;
}

function params(name, json) {
  const p = { jar: vuJar(), tags: { name }, timeout: '10s' };
  if (json) p.headers = { 'Content-Type': 'application/json' };
  return p;
}

const ok = { 'status < 500': (r) => r.status > 0 && r.status < 500 };

export function journey(data) {
  const base = baseUrl();
  const ids = (data && data.ids) || FALLBACK_IDS;
  const pid = ids[Math.floor(Math.random() * ids.length)];
  const r = Math.random();
  if (r < 0.6) {
    // browse
    if (Math.random() < 0.5) check(http.get(`${base}/api/products`, params('products')), ok);
    else check(http.get(`${base}/`, params('home')), ok);
  } else if (r < 0.85) {
    // product detail + recommendations
    check(http.get(`${base}/api/products/${pid}`, params('product')), ok);
    check(http.get(`${base}/api/recommendations?pid=${pid}`, params('recommendations')), ok);
  } else {
    // add to cart + checkout
    check(http.post(`${base}/api/cart`, JSON.stringify({ product_id: pid, qty: 1 }), params('cart', true)), ok);
    check(http.post(`${base}/api/checkout`, '{}', params('checkout', true)), ok);
  }
}

function metricVal(data, name, stat) {
  const m = data.metrics[name];
  if (!m || !m.values) return null;
  const v = m.values[stat];
  return v === undefined ? null : v;
}

// Compact numbers the CLI reads, plus the full k6 summary under "k6".
export function summaryOutputs(data, meta) {
  const compact = {
    meta,
    checkout_p95_ms: metricVal(data, 'http_req_duration{name:checkout}', 'p(95)'),
    checkout_error_rate: metricVal(data, 'http_req_failed{name:checkout}', 'rate'),
    home_p95_ms: metricVal(data, 'http_req_duration{name:home}', 'p(95)'),
    home_error_rate: metricVal(data, 'http_req_failed{name:home}', 'rate'),
    p95_ms: metricVal(data, 'http_req_duration', 'p(95)'),
    error_rate: metricVal(data, 'http_req_failed', 'rate'),
    rps: metricVal(data, 'http_reqs', 'rate'),
    requests: metricVal(data, 'http_reqs', 'count'),
    dropped_iterations: metricVal(data, 'dropped_iterations', 'count') || 0,
    k6: data,
  };
  const fmt = (v, d = 0) => (v === null || v === undefined ? 'n/a' : Number(v).toFixed(d));
  const line =
    `[insidious] ${meta.scenario} target=${meta.target_rps}rps actual=${fmt(compact.rps, 1)}rps ` +
    `p95=${fmt(compact.p95_ms)}ms checkout_p95=${fmt(compact.checkout_p95_ms)}ms ` +
    `errors=${fmt((compact.error_rate || 0) * 100, 2)}% dropped=${compact.dropped_iterations}\n`;
  const out = { stdout: line };
  if (__ENV.SUMMARY_PATH) out[__ENV.SUMMARY_PATH] = JSON.stringify(compact, null, 2);
  return out;
}
