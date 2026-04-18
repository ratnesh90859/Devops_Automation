/**
 * k6-app.js — Application issue: slow endpoint + error rate spike
 *
 * Usage:
 *   k6 run scripts/k6-app.js
 *   k6 run scripts/k6-app.js -e BASE_URL=https://order-api-546580006264.asia-south1.run.app
 *
 * What it simulates:
 *   Mix of /heavy (slow, 29s), /crash (ZeroDivisionError 500), /orders (normal).
 *   Triggers: latency p95 alert + error rate alert.
 *   Expected AI classification:
 *     - /heavy dominant → oom (memory pressure causing slow)
 *     - /crash dominant → code_error (ZeroDivisionError)
 */
import http from 'k6/http';
import { sleep, check } from 'k6';
import { Rate, Trend } from 'k6/metrics';

const errorRate  = new Rate('custom_errors');
const p95Latency = new Trend('p95_latency', true);

export const options = {
  stages: [
    { duration: '20s', target: 10  },  // gentle ramp
    { duration: '90s', target: 25  },  // sustained — latency + errors accumulate
    { duration: '20s', target: 0   },  // ramp down
  ],
  thresholds: {
    http_req_duration: ['p(95)<35000'],  // /heavy takes ~29s
    http_req_failed:   ['rate<0.60'],  // /crash always 500
    custom_errors:     ['rate<0.70'],
  },
};

const BASE     = __ENV.BASE_URL || 'https://order-api-546580006264.asia-south1.run.app';
// SCENARIO env var: 'memory' (default) | 'crash' | 'mixed' | 'leak'
const SCENARIO = __ENV.SCENARIO || 'mixed';

export default function () {
  let res;

  if (SCENARIO === 'leak') {
    // All requests hit /leak — grows a global list 50k items per hit, never freed
    // Memory climbs continuously until Grafana High Memory alert fires (>200MB for 1m)
    res = http.get(`${BASE}/leak`, { timeout: '10s' });

  } else if (SCENARIO === 'memory') {
    // All requests hit /heavy — triggers memory pressure + OOM
    res = http.get(`${BASE}/heavy`, { timeout: '35s' });

  } else if (SCENARIO === 'crash') {
    // All requests hit /crash — triggers ZeroDivisionError (code_error)
    res = http.get(`${BASE}/crash`, { timeout: '10s' });

  } else {
    // Mixed: 40% heavy, 30% crash, 30% normal orders
    const roll = Math.random();
    if (roll < 0.40) {
      res = http.get(`${BASE}/heavy`, { timeout: '35s' });
    } else if (roll < 0.70) {
      res = http.get(`${BASE}/crash`, { timeout: '10s' });
    } else {
      res = http.post(
        `${BASE}/orders`,
        JSON.stringify({ items: 5 }),
        { headers: { 'Content-Type': 'application/json' }, timeout: '10s' },
      );
    }
  }

  const ok = check(res, {
    'not 5xx': (r) => r.status < 500,
  });

  errorRate.add(!ok);
  p95Latency.add(res.timings.duration);

  sleep(0.5);
}

export function handleSummary(data) {
  const p95  = data.metrics.http_req_duration?.values?.['p(95)'] || 0;
  const errs = data.metrics.http_req_failed?.values?.rate || 0;
  const rps  = data.metrics.http_reqs?.values?.rate || 0;

  console.log('\n=== k6 Application Scenario Summary ===');
  console.log(`  Scenario    : ${SCENARIO}`);
  console.log(`  p95 latency : ${p95.toFixed(0)} ms`);
  console.log(`  req/s       : ${rps.toFixed(1)}`);
  console.log(`  error rate  : ${(errs * 100).toFixed(1)}%`);

  if (SCENARIO === 'leak') {
    console.log('Expected Telegram alert: 💾 Memory leak — memory climbs past 200MB');
    console.log('Watch Grafana Memory gauge climb in real time → alert fires ~5-10 min');
  } else if (SCENARIO === 'memory' || SCENARIO === 'mixed') {
    console.log('Expected Telegram alert: 💾 OOM — fix: memory 256Mi → 512Mi');
  }
  if (SCENARIO === 'crash') {
    console.log('Expected Telegram alert: 🐛 code_error — fix: Bitbucket PR with code patch');
  }
  return {};
}
