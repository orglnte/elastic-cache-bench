// FIXED SKELETON — k6 load generator: one cycle (LOOPS=1) of the load shape
// (phase math, VU sizing, 20s timeout, 404-as-success), with a
// response-body check so a faked fast-200 (a build that serves a trivial app
// instead of doing the real read) is counted as an error — anti-gaming.
//
// Open-loop ramping-arrival-rate: a saturated app shows up as timeouts /
// http_req_failed, not a silently slipped schedule. The plateau is floored at
// 21s so the saturation region always exceeds the cache
// acquire_sustain_s + provision window (~15s) — otherwise the spike ends before
// the cache can absorb it.
//
// verify.sh reads the --summary-export JSON -> load_total / load_errors.
//
// Env (verify.sh sets these):
//   BASE_URL     service base URL          (default http://127.0.0.1:8080)
//   TASK         the task (T1)             (default T1)
//   DURATION_S   cycle length seconds      (default 60)
//   PEAK_RPS     plateau arrival rate      (default 500)
//   BASELINE_RPS pre/post baseline rate    (default 50)
//   VU_PREALLOC  warm VU pool              (default 8000)
//   VU_MAX       VU ceiling                (default 12000)

import http from 'k6/http';
import { Counter } from 'k6/metrics';

// 404 on a by-id fetch is honest application state ("no such record"), not a
// failure. Everything else outside 2xx/3xx (incl. 503 admission-shed and the
// 20s timeout) IS a failure.
http.setResponseCallback(http.expectedStatuses({ min: 200, max: 399 }, 404));

const BASE_URL     = __ENV.BASE_URL     || 'http://127.0.0.1:8080';
const TASK         = (__ENV.TASK        || 'T1').toUpperCase();
const DURATION_S   = parseInt(__ENV.DURATION_S   || '60',    10);
const PEAK_RPS     = parseInt(__ENV.PEAK_RPS     || '500',   10);
const BASELINE_RPS = parseInt(__ENV.BASELINE_RPS || '50',    10);
const VU_PREALLOC  = parseInt(__ENV.VU_PREALLOC  || '8000',  10);
const VU_MAX       = parseInt(__ENV.VU_MAX       || '12000', 10);

// Phase math (single cycle). baseline 10% · ramp 10% (cap 10s) · plateau
// = 25%-budget minus ramp, FLOORED at 21s · ramp-down = ramp · post = residual.
const BASELINE_S   = Math.max(1, Math.round(DURATION_S * 0.1));
const RAMP_S       = Math.max(2, Math.min(10, Math.floor(DURATION_S * 0.1)));
const RAMP_DOWN_S  = RAMP_S;
const _SPIKE_BUDGET = Math.round(DURATION_S * 0.25);
const PLATEAU_S    = Math.max(21, _SPIKE_BUDGET - RAMP_S);
const POST_S       = Math.max(0, DURATION_S - BASELINE_S - RAMP_S - PLATEAU_S - RAMP_DOWN_S);

console.log(
  `loadgen task=${TASK} ${BASELINE_RPS}rps x${BASELINE_S}s -> ramp ${RAMP_S}s -> ` +
  `${PEAK_RPS}rps x${PLATEAU_S}s (plateau) -> down ${RAMP_DOWN_S}s -> ${BASELINE_RPS}rps x${POST_S}s ` +
  `(preAlloc=${VU_PREALLOC} maxVUs=${VU_MAX}) base=${BASE_URL}`
);

const SEARCH_TERMS = ['sunset', 'beach', 'mountain', 'city', 'forest', 'river'];
const ID_SPACE = 30;
// The shape a REAL hot response must carry (the fixed routes always return it).
// A build that fakes a fast 200 without the real read path omits it -> counted.
const HOT_MARKER = '"photos"';

const hot   = new Counter('hot_reads');       // the cached hot path
const byid  = new Counter('by_id');           // by-id fetch (10% share)
const fired = new Counter('fired');           // true offered load
const content_errors = new Counter('content_errors');  // anti-gaming: fake/empty hot 200s

export const options = {
  scenarios: {
    spike: {
      executor: 'ramping-arrival-rate',
      startRate: BASELINE_RPS,
      timeUnit: '1s',
      preAllocatedVUs: VU_PREALLOC,
      maxVUs: VU_MAX,
      stages: [
        { target: BASELINE_RPS, duration: `${BASELINE_S}s` },
        { target: PEAK_RPS,     duration: `${RAMP_S}s` },
        { target: PEAK_RPS,     duration: `${PLATEAU_S}s` },
        { target: BASELINE_RPS, duration: `${RAMP_DOWN_S}s` },
        { target: BASELINE_RPS, duration: `${POST_S}s` },
      ],
    },
  },
  thresholds: {},  // saturation is the workload, not a failure — don't exit non-zero.
  summaryTrendStats: ['avg', 'min', 'med', 'p(90)', 'p(95)', 'p(99)', 'max'],
};

function rand(arr) { return arr[Math.floor(Math.random() * arr.length)]; }

export default function () {
  fired.add(1);
  let url, isHot = false;
  const roll = Math.random();
  if (roll < 0.9) { url = `${BASE_URL}/photos/search?q=${rand(SEARCH_TERMS)}`; isHot = true; hot.add(1); }
  else { url = `${BASE_URL}/photos/${Math.floor(Math.random() * ID_SPACE).toString().padStart(8, '0')}`; byid.add(1); }
  const res = http.get(url, { timeout: '20s' });
  // Anti-gaming: a real hot 200 MUST carry the expected shape. A build that
  // returns a fast empty/fake 200 (skipping the real read path the fixed routes
  // run) fails this and is counted as an error, even though its status is 200.
  if (isHot && res.status === 200 && (!res.body || res.body.indexOf(HOT_MARKER) === -1)) {
    content_errors.add(1);
  }
}
