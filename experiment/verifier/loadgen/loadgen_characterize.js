// CHARACTERIZATION loadgen — a slow LINEAR RAMP, not the spike. Drives offered
// load from CHAR_START_RPS up to CHAR_END_RPS over DURATION_S so each rps level
// reaches ~steady state, and the sidecar's per-second (offered_rps, p99) trace
// draws the store's latency-vs-load curve. Point run_exp_artifact at this with
// LOADGEN_FILE + a no-cache artifact to find the rps where p99 crosses the SLO
// (= when a cache is actually needed). Same request mix as loadgen.js.
//
// Env: BASE_URL, TASK, DURATION_S, VU_PREALLOC, VU_MAX,
//      CHAR_START_RPS (default 5), CHAR_END_RPS (default 120).
import http from 'k6/http';
import { Counter } from 'k6/metrics';

http.setResponseCallback(http.expectedStatuses({ min: 200, max: 399 }, 404));

const BASE_URL     = __ENV.BASE_URL     || 'http://127.0.0.1:8080';
const TASK         = (__ENV.TASK        || 'T1').toUpperCase();
const DURATION_S   = parseInt(__ENV.DURATION_S     || '120', 10);
const START_RPS    = parseInt(__ENV.CHAR_START_RPS || '20',  10);
const END_RPS      = parseInt(__ENV.CHAR_END_RPS   || '120', 10);
const VU_PREALLOC  = parseInt(__ENV.VU_PREALLOC    || '1000', 10);
const VU_MAX       = parseInt(__ENV.VU_MAX         || '2000', 10);

console.log(
  `characterize task=${TASK} ${START_RPS}->${END_RPS}rps linear over ${DURATION_S}s ` +
  `(preAlloc=${VU_PREALLOC} maxVUs=${VU_MAX}) base=${BASE_URL}`
);

const SEARCH_TERMS = ['sunset', 'beach', 'mountain', 'city', 'forest', 'river'];
const ID_SPACE = 30;
const HOT_MARKER = '"photos"';

const hot   = new Counter('hot_reads');
const byid  = new Counter('by_id');
const fired = new Counter('fired');
const content_errors = new Counter('content_errors');

export const options = {
  scenarios: {
    ramp: {
      executor: 'ramping-arrival-rate',
      startRate: START_RPS,
      timeUnit: '1s',
      preAllocatedVUs: VU_PREALLOC,
      maxVUs: VU_MAX,
      stages: [{ target: END_RPS, duration: `${DURATION_S}s` }],
    },
  },
  thresholds: {},
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
  if (isHot && res.status === 200 && (!res.body || res.body.indexOf(HOT_MARKER) === -1)) {
    content_errors.add(1);
  }
}
