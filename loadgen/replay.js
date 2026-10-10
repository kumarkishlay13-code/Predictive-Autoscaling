// Replays a trace window (loadgen/windows/<NAME>.json) as open-model load.
//
// Open model: requests arrive at the trace's rate whatever the response time, so a slow
// system cannot hide its slowness by slowing the load generator (plan §2, I3).
//
// Environment:
//   WINDOW_FILE    path to the window JSON              (default /windows/window.json)
//   TARGET         base URL of demo-api                 (default http://demo-api)
//   FORECASTER     base URL of the forecaster; if set, setup() calls /control/start so the
//                  forecaster aligns its clock with the replay
//   WORK_UNITS     ?units= for each request             (default: app default)
//   STEP_SECONDS   seconds per trace minute             (default 60; smaller = time-compressed
//                  smoke test, never for real experiments)
//   MINUTES        replay only the first N minutes      (default: whole window)
//   PRE_VUS / MAX_VUS  VU pool                          (default 100 / 1500)
//   REQUEST_TIMEOUT    per-request timeout              (default 5s)
import http from 'k6/http';

const WINDOW_FILE = __ENV.WINDOW_FILE || '/windows/window.json';
const win = JSON.parse(open(WINDOW_FILE));
const TARGET = (__ENV.TARGET || 'http://demo-api').replace(/\/$/, '');
const FORECASTER = (__ENV.FORECASTER || '').replace(/\/$/, '');
const STEP = parseInt(__ENV.STEP_SECONDS || String(win.step_seconds || 60), 10);
const MINUTES = parseInt(__ENV.MINUTES || '0', 10) || win.rps.length;
const RPS = win.rps.slice(0, MINUTES);
const URL = __ENV.WORK_UNITS ? `${TARGET}/work?units=${__ENV.WORK_UNITS}` : `${TARGET}/work`;
const TIMEOUT = __ENV.REQUEST_TIMEOUT || '5s';

// Per-minute targets (timeUnit 1m) keep precision at low rates.
const perMinute = (rps) => Math.max(0, Math.round(rps * 60));

export const options = {
  discardResponseBodies: true,
  // A new connection per request lets kube-proxy spread load over pods that were just added.
  // With keep-alive, connections stay pinned to the pods that existed when they were opened.
  noConnectionReuse: true,
  setupTimeout: '30s',
  summaryTrendStats: ['avg', 'med', 'p(90)', 'p(95)', 'p(99)', 'max'],
  scenarios: {
    replay: {
      executor: 'ramping-arrival-rate',
      startRate: perMinute(RPS[0]),
      timeUnit: '1m',
      preAllocatedVUs: parseInt(__ENV.PRE_VUS || '100', 10),
      maxVUs: parseInt(__ENV.MAX_VUS || '1500', 10),
      stages: RPS.map((r) => ({ target: perMinute(r), duration: `${STEP}s` })),
      gracefulStop: '10s',
    },
  },
};

export function setup() {
  const startedAt = Date.now() / 1000;
  if (FORECASTER) {
    const res = http.post(`${FORECASTER}/control/start`, JSON.stringify({ at: startedAt }),
      { headers: { 'Content-Type': 'application/json' }, timeout: '10s' });
    if (res.status !== 200) {
      console.error(`forecaster /control/start returned ${res.status}: ${res.body}`);
    }
  }
  console.log(`replaying window=${win.name} minutes=${RPS.length} step=${STEP}s ` +
    `peak=${Math.max(...RPS).toFixed(1)}rps target=${TARGET}`);
  return { startedAt };
}

export default function () {
  http.get(URL, { timeout: TIMEOUT, tags: { name: 'work' } });
}

// One machine-readable line that `pas` parses from the Job log.
export function handleSummary(data) {
  const pick = (name) => (data.metrics[name] ? data.metrics[name].values : null);
  const summary = {
    window: win.name,
    // Epoch seconds when load started; the analysis aligns minute 0 of the window to this.
    started_at: data.setup_data ? data.setup_data.startedAt : null,
    minutes: RPS.length,
    step_seconds: STEP,
    http_reqs: pick('http_reqs'),
    http_req_failed: pick('http_req_failed'),
    http_req_duration: pick('http_req_duration'),
    dropped_iterations: pick('dropped_iterations'),
    vus_max: pick('vus_max'),
  };
  return { stdout: `\nPAS_SUMMARY ${JSON.stringify(summary)}\n` };
}
