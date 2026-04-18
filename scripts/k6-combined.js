/**
 * k6-combined.js — Combined issue: CPU spike + latency + errors simultaneously
 *
 * Usage:
 *   k6 run scripts/k6-combined.js
 *   k6 run scripts/k6-combined.js -e BASE_URL=https://order-api-546580006264.asia-south1.run.app
 *
 * What it simulates:
 *   Two parallel executor scenarios:
 *     cpu_spike:    High concurrency on /orders (CPU pressure)
 *     slow_errors:  Steady /heavy + /crash requests (latency + error rate)
 *
 *   Triggers: CPU alert AND latency alert AND error rate alert together.
 *   Expected AI classification: mixed (high_cpu + oom) or deployment_regression
 *   if a recent deployment happened before running this.
 */
import http from 'k6/http';
import { sleep, check } from 'k6';
import { Rate, Trend } from 'k6/metrics';

const errorRate  = new Rate('custom_errors');
const p95Latency = new Trend('p95_latency', true);

export const options = {
  scenarios: {
    // Scenario A: flood /orders to spike CPU
    cpu_spike: {
      executor:          'ramping-vus',
      startVUs:          0,
      gracefulRampDown:  '10s',
      stages: [
        { duration: '30s', target: 50  },
        { duration: '90s', target: 80  },
        { duration: '20s', target: 0   },
      ],
      exec: 'cpuScenario',
    },
    // Scenario B: slow + error requests from t=30s onwards
    slow_errors: {
      executor:  'constant-vus',
      vus:       15,
      duration:  '2m',
      startTime: '30s',
      exec:      'slowErrorScenario',
    },
  },
  thresholds: {
    http_req_duration: ['p(95)<35000'],
    http_req_failed:   ['rate<0.50'],
    custom_errors:     ['rate<0.60'],
  },
};

const BASE = __ENV.BASE_URL || 'https://order-api-546580006264.asia-south1.run.app';

// Executor A: CPU spike via /orders
export function cpuScenario() {
  const res = http.post(
    `${BASE}/orders`,
    JSON.stringify({ items: 30 }),
    { headers: { 'Content-Type': 'application/json' }, timeout: '15s' },
  );
  const ok = check(res, { 'orders ok': (r) => r.status < 500 });
  errorRate.add(!ok);
  p95Latency.add(res.timings.duration);
  sleep(0.1);
}

// Executor B: latency + errors via /heavy and /crash
export function slowErrorScenario() {
  const roll = Math.random();
  let res;

  if (roll < 0.60) {
    res = http.get(`${BASE}/heavy`, { timeout: '35s' });
  } else {
    res = http.get(`${BASE}/crash`, { timeout: '10s' });
  }

  const ok = check(res, { 'not 5xx': (r) => r.status < 500 });
  errorRate.add(!ok);
  p95Latency.add(res.timings.duration);
  sleep(0.5);
}

export function handleSummary(data) {
  const p95  = data.metrics.http_req_duration?.values?.['p(95)'] || 0;
  const errs = data.metrics.http_req_failed?.values?.rate || 0;
  const rps  = data.metrics.http_reqs?.values?.rate || 0;

  console.log('\n=== k6 Combined Scenario Summary ===');
  console.log(`  p95 latency : ${p95.toFixed(0)} ms`);
  console.log(`  req/s       : ${rps.toFixed(1)}`);
  console.log(`  error rate  : ${(errs * 100).toFixed(1)}%`);
  console.log('Expected: Multiple Telegram alerts firing. AI should classify as mixed/oom.');
  return {};
}
