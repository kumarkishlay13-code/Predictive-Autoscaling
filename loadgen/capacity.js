// Step-load capacity test against a fixed number of replicas (normally 1).
//
// Holds each rate in STEPS for STEP_SECONDS and reports latency and errors per step, so
// `pas capacity` can find the highest rate one pod sustains within the SLO.
//
// Environment:
//   TARGET         base URL of demo-api        (default http://demo-api)
//   STEPS          comma-separated RPS levels  (default 5,10,15,20,25,30,35,40)
//   STEP_SECONDS   hold time per level         (default 120)
//   SETTLE_SECONDS ignore this long after each step change (default 20)
//   WORK_UNITS     ?units= per request         (default: app default)
import http from 'k6/http';

const TARGET = (__ENV.TARGET || 'http://demo-api').replace(/\/$/, '');
const STEPS = (__ENV.STEPS || '5,10,15,20,25,30,35,40').split(',').map(Number);
const HOLD = parseInt(__ENV.STEP_SECONDS || '120', 10);
const SETTLE = parseInt(__ENV.SETTLE_SECONDS || '20', 10);
const URL = __ENV.WORK_UNITS ? `${TARGET}/work?units=${__ENV.WORK_UNITS}` : `${TARGET}/work`;
const RAMP = 5;

const stages = [];
STEPS.forEach((rps) => {
  stages.push({ target: rps, duration: `${RAMP}s` });
  stages.push({ target: rps, duration: `${HOLD}s` });
});

// Declaring a threshold per step makes k6 report each step's sub-metric in the summary.
const thresholds = {};
STEPS.forEach((_, i) => {
  thresholds[`http_req_duration{step:${i}}`] = ['max>=0'];
  thresholds[`http_req_failed{step:${i}}`] = ['rate>=0'];
  thresholds[`http_reqs{step:${i}}`] = ['count>=0'];
});

export const options = {
  discardResponseBodies: true,
  noConnectionReuse: true,
  summaryTrendStats: ['avg', 'med', 'p(95)', 'p(99)', 'max'],
  thresholds,
  scenarios: {
    capacity: {
      executor: 'ramping-arrival-rate',
      startRate: STEPS[0],
      timeUnit: '1s',
      preAllocatedVUs: 50,
      maxVUs: 1000,
      stages,
    },
  },
};

export function setup() {
  return { start: Date.now() };
}

export default function (data) {
  const elapsed = (Date.now() - data.start) / 1000;
  const span = RAMP + HOLD;
  const i = Math.min(Math.floor(elapsed / span), STEPS.length - 1);
  const inStep = elapsed - i * span;
  // Requests sent while the rate is still settling are tagged "settling" and not scored.
  const step = inStep < RAMP + SETTLE ? 'settling' : String(i);
  http.get(URL, { timeout: '5s', tags: { name: 'work', step } });
}

export function handleSummary(data) {
  const steps = STEPS.map((rps, i) => {
    const m = (name) => data.metrics[`${name}{step:${i}}`];
    const dur = m('http_req_duration');
    const failed = m('http_req_failed');
    const reqs = m('http_reqs');
    return {
      rps,
      requests: reqs ? reqs.values.count : 0,
      achieved_rps: reqs ? reqs.values.count / Math.max(HOLD - SETTLE, 1) : 0,
      p95_ms: dur ? dur.values['p(95)'] : null,
      p99_ms: dur ? dur.values['p(99)'] : null,
      error_rate: failed ? failed.values.rate : null,
    };
  });
  const dropped = data.metrics.dropped_iterations ? data.metrics.dropped_iterations.values.count : 0;
  return { stdout: `\nPAS_CAPACITY ${JSON.stringify({ steps, dropped_iterations: dropped })}\n` };
}
