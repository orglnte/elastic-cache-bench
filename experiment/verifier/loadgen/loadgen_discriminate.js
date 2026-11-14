// DISCRIMINATION loadgen — the test that separates a good scaler from a
// trigger-happy one. Not a single spike: baseline -> brief BLIP -> baseline ->
// brief BLIP -> baseline -> real SUSTAINED spike -> down.
//
// A correct control law must (a) REJECT the blips — each crosses the store's
// knee but for less than the provision time, so a cache would land only after
// the blip is gone (pure churn to mount it); and (b) MOUNT + HOLD the sustained
// spike, then RELEASE when demand recedes. This is the true-positive /
// false-positive test of the acquire logic.
//
// The spike is a RAMP from the baseline to the top rate, then a hold there:
// it crosses the store's knee wherever this host puts it, and the verifier's
// law measures from that crossing (the store's own saturation), not from
// the schedule. The blips are NOT this generator's: loadgen_blip.js drives
// each one on top of the baseline this one keeps, and stops at the first
// sign of pressure — so here a blip window is plain baseline.
//
// Env: BASE_URL, TASK, VU_PREALLOC, VU_MAX,
//      DISC_BASELINE (default 18, below any knee),
//      DISC_SPIKE (default 360, the top of the ramp; must exceed the knee).
import http from 'k6/http';
import { Counter } from 'k6/metrics';

http.setResponseCallback(http.expectedStatuses({ min: 200, max: 399 }, 404));

const BASE_URL    = __ENV.BASE_URL    || 'http://127.0.0.1:8080';
const TASK        = (__ENV.TASK       || 'T1').toUpperCase();
const BASELINE    = parseInt(__ENV.DISC_BASELINE || '18',  10);
const SPIKE       = parseInt(__ENV.DISC_SPIKE    || '360', 10);
// Stage durations (seconds), env-tunable so a time-boxed cell verify can run a
// compressed cycle without changing the shape (same blips/spike/tail semantics).
const T_BASE      = parseInt(__ENV.DISC_T_BASELINE || '8',  10);
const T_RAMP      = parseInt(__ENV.DISC_T_RAMP     || '20', 10);
const T_SPIKE     = parseInt(__ENV.DISC_T_SPIKE    || '30', 10);
const T_TAIL      = parseInt(__ENV.DISC_T_TAIL     || '12', 10);
const VU_PREALLOC = parseInt(__ENV.VU_PREALLOC   || '4000', 10);
const VU_MAX      = parseInt(__ENV.VU_MAX        || '8000', 10);

// The verifier always hands over DISC_STAGES (JSON array of {target, duration},
// symbolic targets BASELINE/SPIKE), a SEEDED PERMUTATION of the same three
// events (blip, blip, spike) so an agent iterating on trace feedback cannot
// hardcode the timeline; only a signal-driven policy passes every arrangement.
// Without it: baseline -> the spike ramp and hold -> release tail. The spike
// hold (30s) after the ramp is long enough that even the slower external
// (KEDA) mount lands with room to show cache-served load before the tail
// drives rps back below the knee and forces the release.
const NAMED = { BASELINE, SPIKE };
const STAGES = __ENV.DISC_STAGES ? JSON.parse(__ENV.DISC_STAGES).map(
  (s) => ({ target: NAMED[s.target] !== undefined ? NAMED[s.target] : s.target, duration: s.duration })
) : [
  { target: BASELINE, duration: `${T_BASE}s` },
  { target: SPIKE,    duration: `${T_RAMP}s` }, { target: SPIKE, duration: `${T_SPIKE}s` }, { target: BASELINE, duration: '3s' },
  { target: BASELINE, duration: `${T_TAIL}s` },
];

console.log(
  `discriminate task=${TASK} baseline=${BASELINE} spike=${SPIKE} over ${T_RAMP}s, hold ${T_SPIKE}s ` +
  `(blips are loadgen_blip.js's; preAlloc=${VU_PREALLOC} maxVUs=${VU_MAX}) base=${BASE_URL}`
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
    discriminate: {
      executor: 'ramping-arrival-rate',
      startRate: BASELINE,
      timeUnit: '1s',
      preAllocatedVUs: VU_PREALLOC,
      maxVUs: VU_MAX,
      stages: STAGES,
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
