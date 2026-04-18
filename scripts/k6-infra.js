/**
 * k6-infra.js — Infra issue: high traffic → CPU spike + scaling pressure
 *
 * Usage:
 *   k6 run scripts/k6-infra.js
 *   k6 run scripts/k6-infra.js -e BASE_URL=https://order-api-546580006264.asia-south1.run.app
 *
 * What it simulates:
 *   Ramps to 100 VUs firing /orders (CPU-heavy) requests.
 *   Triggers: CPU throttle alert, high request rate alert.
 *   Expected AI classification: high_cpu or oom → scale-up fix.
 */
import http from 'k6/http';
import { sleep, check } from 'k6';
import { Rate, Trend } from 'k6/metrics';

const errorRate   = new Rate('custom_errors');
const p95Latency  = new Trend('p95_latency', true);

export const options = {
  stages: [
    { duration: '30s', target: 20  },   // warm-up
    { duration: '60s', target: 80  },   // ramp up — CPU starts spiking
    { duration: '90s', target: 100 },   // sustained high load
    { duration: '30s', target: 0   },   // ramp down
  ],
  thresholds: {
    http_req_duration:  ['p(95)<8000'],   // accept slow under pressure
    http_req_failed:    ['rate<0.20'],  // allow up to 20% errors (OOM kills)
    custom_errors:      ['rate<0.30'],
  },
};

const BASE = __ENV.BASE_URL || 'https://order-api-546580006264.asia-south1.run.app';

export default function () {
  // /orders is CPU-bound (iterates a list of N items)
  const res = http.post(
    `${BASE}/orders`,
    JSON.stringify({ items: 50 }),
    { headers: { 'Content-Type': 'application/json' }, timeout: '15s' },
  );

  const ok = check(res, {
    'status 200':    (r) => r.status === 200,
    'no 5xx':        (r) => r.status < 500,
    'latency < 10s': (r) => r.timings.duration < 10_000,
  });

  errorRate.add(!ok);
  p95Latency.add(res.timings.duration);

  sleep(0.1);   // 10 req/s per VU → 1000 req/s at 100 VUs
}

export function handleSummary(data) {
  const p95 = data.metrics.http_req_duration?.values?.['p(95)'] || 0;
  const rps  = data.metrics.http_reqs?.values?.rate || 0;
  const errs = data.metrics.http_req_failed?.values?.rate || 0;

  console.log('\n=== k6 Infra Scenario Summary ===');
  console.log(`  p95 latency : ${p95.toFixed(0)} ms`);
  console.log(`  req/s       : ${rps.toFixed(1)}`);
  console.log(`  error rate  : ${(errs * 100).toFixed(1)}%`);
  console.log('Watch Telegram — CPU/scaling alert should fire within 2-3 minutes.');
  return {};
}
