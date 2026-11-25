// insidious rehearse step: constant arrival rate against the Jeffy store (k6 v2).
//
//   TARGET_RPS=300 DURATION=20s STORE_URL=http://localhost:8000 SUMMARY_PATH=/tmp/step.json \
//     k6 run --quiet loadtest/rehearse.js
//
// `insidious rehearse` runs this once per step (TARGET_RPS = step, DURATION = rehearse.step_seconds).
// TARGET_RPS is in HTTP requests/second; the iteration rate is TARGET_RPS / 1.4 (see lib/mix.js).
import { iterRate, journey, setupProducts, baseUrl, summaryOutputs, THRESHOLDS, SUMMARY_TREND_STATS } from './lib/mix.js';

const TARGET_RPS = Number(__ENV.TARGET_RPS || 50);
const DURATION = __ENV.DURATION || '20s';
const RATE = iterRate(TARGET_RPS);

export const options = {
  discardResponseBodies: true,
  scenarios: {
    rehearse: {
      executor: 'constant-arrival-rate',
      rate: RATE,
      timeUnit: '1s',
      duration: DURATION,
      preAllocatedVUs: Math.min(Math.max(RATE, 10), 600),
      maxVUs: Math.min(Math.max(RATE * 4, 50), 3000),
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
  return summaryOutputs(data, { scenario: 'rehearse', target_rps: TARGET_RPS, duration: DURATION, store_url: baseUrl() });
}
