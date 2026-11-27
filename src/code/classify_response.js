// Code node "Classify <step>" (run once for all items), right after an HTTP Request node that
// returns the full response and never throws on HTTP errors.
//
//   ok       2xx: continue
//   retry    transient (no response, 408, 429, 5xx) and attempts are left: wait, call again
//   failed   permanent (other 4xx) or attempts used up: the next node stops the run with an
//            error, so the error workflow releases the key and raises an alert
//
// Backoff: exponential with full jitter, honours Retry-After, capped at 300 s. Attempts and
// the base wait come from the Settings node, so the harness can shrink them.
const STEP = '__STEP__';
const settings = $('Settings').first().json;
const maxAttempts = Math.max(1, Number(settings.max_attempts || 4));
const baseWaitS = Math.max(0.1, Number(settings.retry_base_s || 5));

const response = $json || {};
const status = Number(response.statusCode || 0);
const body = response.body && typeof response.body === 'object' && !Array.isArray(response.body) ? response.body : {};
const attempt = $runIndex + 1;

let route;
let transient = false;
if (status >= 200 && status < 300) {
  route = 'ok';
} else if (status === 0 || status === 408 || status === 429 || status >= 500) {
  transient = true;
  route = attempt < maxAttempts ? 'retry' : 'failed';
} else {
  route = 'failed';
}

let waitS = 0;
if (route === 'retry') {
  const headers = response.headers || {};
  const retryAfter = Number(headers['retry-after'] || 0);
  const ceiling = Math.min(300, baseWaitS * 2 ** (attempt - 1));
  const jittered = ceiling / 2 + Math.random() * (ceiling / 2);
  const chosen = Math.min(300, Math.max(jittered, Number.isFinite(retryAfter) ? retryAfter : 0));
  waitS = Math.round(chosen * 10) / 10;
}

let error = null;
if (route === 'failed') {
  const why = transient ? `gave up after ${attempt} attempts` : 'permanent error';
  const detail =
    (body.error && (body.error.message || body.error.code)) || (response.error && response.error.message) || '';
  error = `${STEP}: ${why} (HTTP ${status || 'no response'}${detail ? `: ${String(detail).slice(0, 160)}` : ''})`;
}

// Only a small, bounded copy of the answer is kept as the run's outcome.
const outcome = route === 'ok' && JSON.stringify(body).length <= 2000 ? body : route === 'ok' ? { truncated: true } : null;
return [{ json: { step: STEP, route, attempt, status_code: status, wait_s: waitS, error, outcome } }];
