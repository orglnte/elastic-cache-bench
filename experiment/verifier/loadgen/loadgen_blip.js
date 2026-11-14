// BLIP loadgen — one short elevation that the store must absorb on its own.
//
// A blip is the spike's own ramp with a stop rule: arrivals rise from zero at
// the spike's slope (BLIP_TOP over BLIP_RAMP_S, on top of the baseline the
// main loadgen keeps offering) until the first sign of pressure — a request
// slower than BLIP_P99_MS, or a request waiting for a pool connection in
// /health — then climb on until BLIP_EXCESS requests have been offered above
// the rate of that moment, and ABORT. So on any host it turns back just past
// the store's knee, with a queue of a known size the store drains in a few
// seconds: a policy that mounts on a fixed count of slow or queued requests
// sees that count here, with nothing sustained behind it. On a host whose
// knee is above BLIP_TOP it ramps to the top and ends — and the spike will
// then void the arrangement for never saturating the store.
//
// The VUs share no state, so the VU that sees the pressure holds the abort
// for the time the ramp needs to offer the excess: at slope s (rps per
// second) that is ½·s·t² requests over the rate at the trigger, t = √(2·E/s).
//
// Env: BASE_URL, TASK, BLIP_TOP (rps), BLIP_RAMP_S, BLIP_P99_MS, BLIP_EXCESS (requests),
//      VU_PREALLOC, VU_MAX
import http from 'k6/http';
import exec from 'k6/execution';
import { sleep } from 'k6';
import { Counter } from 'k6/metrics';

http.setResponseCallback(http.expectedStatuses({ min: 200, max: 399 }, 404));

const BASE_URL    = __ENV.BASE_URL    || 'http://127.0.0.1:8080';
const TASK        = (__ENV.TASK       || 'T1').toUpperCase();
const TOP         = parseInt(__ENV.BLIP_TOP     || '360', 10);
const RAMP_S      = parseInt(__ENV.BLIP_RAMP_S  || '20',  10);
const P99_MS      = parseFloat(__ENV.BLIP_P99_MS || '50');
const EXCESS      = parseFloat(__ENV.BLIP_EXCESS || '10');
const SLOPE       = TOP / RAMP_S;                       // rps per second
const OVERSHOOT_S = EXCESS > 0 ? Math.sqrt(2 * EXCESS / SLOPE) : 0;
const VU_PREALLOC = parseInt(__ENV.VU_PREALLOC  || '400', 10);
const VU_MAX      = parseInt(__ENV.VU_MAX       || '2000', 10);

const SEARCH_TERMS = ['sunset', 'beach', 'mountain', 'city', 'forest', 'river'];
const ID_SPACE = 30;
const HOT_MARKER = '"photos"';

const hot   = new Counter('hot_reads');
const byid  = new Counter('by_id');
const fired = new Counter('fired');
const content_errors = new Counter('content_errors');

console.log(`blip task=${TASK} 0 -> ${TOP} rps over ${RAMP_S}s, pressure = ${P99_MS}ms or a queued request, ` +
            `then ${EXCESS} req over that rate (${OVERSHOOT_S.toFixed(1)}s more); base=${BASE_URL}`);

export const options = {
  scenarios: {
    blip: {
      executor: 'ramping-arrival-rate',
      startRate: 0,
      timeUnit: '1s',
      preAllocatedVUs: VU_PREALLOC,
      maxVUs: VU_MAX,
      stages: [{ target: TOP, duration: `${RAMP_S}s` }],
      gracefulStop: '2s',
    },
    watch: {
      executor: 'constant-vus',
      vus: 1,
      duration: `${RAMP_S + 5}s`,
      exec: 'watch',
      gracefulStop: '1s',
    },
  },
  thresholds: {},
  summaryTrendStats: ['avg', 'min', 'med', 'p(90)', 'p(95)', 'p(99)', 'max'],
};

function rand(arr) { return arr[Math.floor(Math.random() * arr.length)]; }

function stop(why) {
  const at = (exec.instance.currentTestRunDuration / 1000).toFixed(1);
  if (OVERSHOOT_S > 0) sleep(OVERSHOOT_S);
  exec.test.abort(`blip: ${why} at ${at}s; ${EXCESS} req over the knee offered, stopping`);
}

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
  if (res.timings.duration > P99_MS) {
    stop(`a request took ${res.timings.duration.toFixed(0)}ms > ${P99_MS}ms`);
  }
}

export function watch() {
  const res = http.get(`${BASE_URL}/health`, { timeout: '2s', tags: { name: 'health' } });
  if (res.status === 200) {
    let waiting = 0;
    try { waiting = parseInt(JSON.parse(res.body).pool_waiting || 0, 10); } catch (e) { waiting = 0; }
    if (waiting > 0) {
      stop(`${waiting} request(s) queued for the pool`);
    }
  }
  sleep(0.25);
}
