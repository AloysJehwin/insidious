// Festive rush (`make festive-rush` / `make break-demo`): ramp 20 -> PEAK_RPS over 20s, then hold (k6 v2).
//
//   STORE_URL=http://localhost:8000 PEAK_RPS=600 HOLD=90s k6 run loadtest/festive-rush.js
//
// Rates are HTTP requests/second, converted to iterations/second (/1.4, see lib/mix.js).
// Optional: SUMMARY_PATH=reports/rush.json to keep the k6 summary.
import { iterRate, journey, setupProducts, baseUrl, summaryOutputs, THRESHOLDS, SUMMARY_TREND_STATS } from './lib/mix.js';

const PEAK_RPS = Number(__ENV.PEAK_RPS || 600);
const START_RPS = Number(__ENV.START_RPS || 20);
const RAMP = __ENV.RAMP || '20s';
const HOLD = __ENV.HOLD || '90s';
const PEAK = iterRate(PEAK_RPS);

export const options = {
  discardResponseBodies: true,
  scenarios: {
    festive_rush: {
      executor: 'ramping-arrival-rate',
      startRate: iterRate(START_RPS),
      timeUnit: '1s',
      preAllocatedVUs: Math.min(Math.max(PEAK, 20), 800),
      maxVUs: Math.min(Math.max(PEAK * 4, 100), 4000),
      stages: [
        { target: PEAK, duration: RAMP },
        { target: PEAK, duration: HOLD },
      ],
    },
  },
  thresholds: THRESHOLDS,
  summaryTrendStats: SUMMARY_TREND_STATS,
};

export function setup() {
  return { ids: setupProducts(baseUrl()) };
}

export default function (data) {
  journey(data);
}

export function handleSummary(data) {
  return summaryOutputs(data, { scenario: 'festive-rush', target_rps: PEAK_RPS, duration: `${RAMP}+${HOLD}`, store_url: baseUrl() });
}
