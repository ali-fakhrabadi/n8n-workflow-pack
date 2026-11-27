// Unit tests of the workflows' Code nodes: `node --test tests/unit/*.test.mjs`.
//
// Each script runs as n8n runs it: the body of an async function with n8n's helpers in scope
// ($, $json, $execution, $runIndex). Placeholders are filled as scripts/build_workflows.py does.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';

const CODE = join(dirname(fileURLToPath(import.meta.url)), '..', '..', 'src', 'code');
const AsyncFunction = Object.getPrototypeOf(async () => {}).constructor;
const SETTINGS = {
  max_attempts: 4,
  retry_base_s: 2,
  approval_timeout_s: 60,
  sheet_tab: 'Invoices',
  review_tab: 'Review',
};
const RESUME = 'http://n8n:5678/webhook-waiting/42?signature=abc123';

async function run(file, { replace = {}, nodes = {}, json = {}, runIndex = 0 } = {}) {
  let script = readFileSync(join(CODE, file), 'utf8');
  for (const [key, value] of Object.entries(replace)) script = script.replaceAll(`__${key}__`, value);
  const all = { Settings: SETTINGS, ...nodes };
  const $ = (name) => {
    if (!(name in all)) throw new Error(`no output of ${name}`);
    const item = { json: all[name] };
    return { first: () => item, last: () => item, item };
  };
  // URL is shadowed: n8n's task runner sandbox does not provide it (found by the harness).
  const [item] = await new AsyncFunction('$', '$json', '$execution', '$runIndex', 'URL', script)(
    $,
    json,
    { id: '42', resumeUrl: RESUME },
    runIndex,
    undefined,
  );
  return item.json;
}

const webhook = (file) => (body) => run(file, { replace: { WEBHOOK_NODE: 'Webhook' }, nodes: { Webhook: { body } } });
const social = webhook('validate_social.js');
const email = webhook('validate_email.js');
const lead = webhook('validate_lead.js');

// --- 01 social ----------------------------------------------------------------------------------
test('social: a valid LinkedIn request is normalised', async () => {
  const r = await social({ request_id: 'post-1', channel: ' LinkedIn ', text: '  Hello\r\nworld  ', requested_by: 'Dana' });
  assert.equal(r.valid, true);
  assert.equal(r.key, 'post-1');
  assert.deepEqual(r.post, { request_id: 'post-1', channel: 'linkedin', text: 'Hello\nworld', media_url: null, requested_by: 'Dana', length: 11 });
});

test('social: length is counted in code points against the channel limit', async () => {
  assert.equal((await social({ request_id: 'a', channel: 'x', text: '😀'.repeat(280) })).valid, true);
  const r = await social({ request_id: 'a', channel: 'x', text: 'a'.repeat(281) });
  assert.equal(r.valid, false);
  assert.match(r.reason, /281 characters; x allows 280/);
});

test('social: rejects unknown channels, empty text, bad ids and control characters', async () => {
  assert.match((await social({ request_id: 'a', channel: 'myspace', text: 'hi' })).reason, /channel must be one of/);
  assert.match((await social({ request_id: 'a', channel: 'x', text: '   ' })).reason, /text is empty/);
  assert.match((await social({ request_id: 'a', channel: 'x' })).reason, /text is required/);
  assert.match((await social({ request_id: 'bad id!', channel: 'x', text: 'hi' })).reason, /request_id/);
  assert.match((await social({ channel: 'x', text: 'hi' })).reason, /request_id/);
  assert.match((await social({ request_id: 'a', channel: 'x', text: 'a\u0007b' })).reason, /control characters/);
  assert.match((await social({ request_id: 'a', channel: '__proto__', text: 'hi' })).reason, /channel/);
  assert.match((await social([1, 2])).reason, /JSON object/);
});

test('social: media_url must be https', async () => {
  assert.match((await social({ request_id: 'a', channel: 'x', text: 'hi', media_url: 'http://cdn.example.com/a.png' })).reason, /https/);
  assert.match((await social({ request_id: 'a', channel: 'x', text: 'hi', media_url: 'not a url' })).reason, /not a URL/);
  assert.match((await social({ request_id: 'a', channel: 'x', text: 'hi', media_url: 'https://user@cdn.example.com/a' })).reason, /not a URL/);
  assert.match((await social({ request_id: 'a', channel: 'x', text: 'hi', media_url: 'https://cdn.example.com/a b.png' })).reason, /not a URL/);
  const ok = await social({ request_id: 'a', channel: 'x', text: 'hi', media_url: 'https://cdn.example.com/a.png?v=2' });
  assert.equal(ok.post.media_url, 'https://cdn.example.com/a.png?v=2');
  assert.equal((await social({ request_id: 'a', channel: 'x', text: 'hi', media_url: 'HTTPS://cdn.example.com' })).valid, true);
});

test('approval message: links are the resume URL plus a decision, token kept', async () => {
  const request = await social({ request_id: 'post-9', channel: 'x', text: 'Launch day' });
  const r = await run('approval_message.js', { nodes: { 'Validate request': request } });
  assert.equal(r.key, 'post-9');
  const approve = new URL(r.approve_url);
  assert.equal(approve.searchParams.get('signature'), 'abc123');
  assert.equal(approve.searchParams.get('decision'), 'approve');
  assert.equal(new URL(r.reject_url).searchParams.get('decision'), 'reject');
  assert.ok(Date.parse(r.expires_at) > Date.now());
});

test('read decision: approve, reject, anything else and timeout', async () => {
  assert.equal((await run('read_decision.js', { json: { query: { decision: 'approve', by: 'sam' } } })).decision, 'approve');
  assert.equal((await run('read_decision.js', { json: { query: { decision: 'approve', by: 'sam' } } })).decided_by, 'sam');
  assert.equal((await run('read_decision.js', { json: { query: { decision: 'reject' } } })).final_status, 'rejected');
  assert.equal((await run('read_decision.js', { json: { query: { decision: 'yes please' } } })).decision, 'reject');
  const timedOut = await run('read_decision.js', { json: { kind: 'approval_request', approve_url: 'x' } });
  assert.equal(timedOut.decision, 'expired');
  assert.equal(timedOut.final_status, 'expired');
});

// --- retries ------------------------------------------------------------------------------------
const classify = (json, runIndex = 0) => run('classify_response.js', { replace: { STEP: 'publish' }, json, runIndex });

test('classify: 2xx is ok and keeps a bounded outcome', async () => {
  const r = await classify({ statusCode: 201, body: { id: 'p1', url: 'https://social.example/p1' } });
  assert.equal(r.route, 'ok');
  assert.deepEqual(r.outcome, { id: 'p1', url: 'https://social.example/p1' });
  const big = await classify({ statusCode: 200, body: { blob: 'x'.repeat(5000) } });
  assert.deepEqual(big.outcome, { truncated: true });
});

test('classify: transient errors retry with growing, jittered waits until attempts run out', async () => {
  for (const statusCode of [0, 408, 429, 500, 502, 503]) {
    assert.equal((await classify({ statusCode, body: {} })).route, 'retry', String(statusCode));
  }
  const first = await classify({ statusCode: 503 }, 0);
  assert.ok(first.wait_s >= 1 && first.wait_s <= 2, String(first.wait_s));
  const third = await classify({ statusCode: 503 }, 2);
  assert.ok(third.wait_s >= 4 && third.wait_s <= 8, String(third.wait_s));
  const last = await classify({ statusCode: 503 }, 3);
  assert.equal(last.route, 'failed');
  assert.match(last.error, /gave up after 4 attempts \(HTTP 503\)/);
});

test('classify: Retry-After is honoured; connection errors count as transient', async () => {
  const r = await classify({ statusCode: 429, headers: { 'retry-after': '7' } });
  assert.equal(r.wait_s, 7);
  const conn = await classify({ error: { message: 'connect ECONNREFUSED' } });
  assert.equal(conn.route, 'retry');
  const gaveUp = await classify({ error: { message: 'connect ECONNREFUSED' } }, 3);
  assert.match(gaveUp.error, /no response: connect ECONNREFUSED/);
});

test('classify: other 4xx fail at once with the API message', async () => {
  const r = await classify({ statusCode: 422, body: { error: { message: 'text rejected by network' } } });
  assert.equal(r.route, 'failed');
  assert.equal(r.error, 'publish: permanent error (HTTP 422: text rejected by network)');
});

// --- 02 email -----------------------------------------------------------------------------------
const MAIL = {
  message_id: '<INV-Mail-1@billing.example.com>',
  from: 'Acme Billing <Billing@Acme.example.com>',
  subject: 'Invoice INV-1001',
  date: 'Tue, 24 Nov 2026 09:15:00 +0000',
  text: 'Invoice number: INV-1001\nPO number: PO-77\nTotal due: $1,250.00\nDue date: 2026-12-24',
};

test('email: normalises Message-ID and sender, converts the date', async () => {
  const r = await email(MAIL);
  assert.equal(r.valid, true);
  assert.equal(r.key, 'inv-mail-1@billing.example.com');
  assert.equal(r.email.from, 'billing@acme.example.com');
  assert.equal(r.email.from_name, 'Acme Billing');
  assert.equal(r.email.received_at, '2026-11-24T09:15:00.000Z');
});

test('email: HTML-only messages are reduced to text', async () => {
  const r = await email({ ...MAIL, text: '', html: '<p>Invoice number: INV-2</p><style>p{}</style><div>Total: 5 &amp; more</div>' });
  assert.equal(r.email.text, 'Invoice number: INV-2\nTotal: 5 & more');
});

test('email: rejects missing ids, bad senders, empty and oversized bodies', async () => {
  assert.match((await email({ ...MAIL, message_id: '' })).reason, /message_id/);
  assert.match((await email({ ...MAIL, message_id: 'no-at-sign' })).reason, /message_id/);
  assert.match((await email({ ...MAIL, from: 'Billing team' })).reason, /from is not an email/);
  assert.match((await email({ ...MAIL, text: ' ', html: undefined })).reason, /no text/);
  assert.match((await email({ ...MAIL, text: 'a'.repeat(100001) })).reason, /longer than 100000/);
});

const extract = async (overrides) => {
  const validated = await email({ ...MAIL, ...overrides });
  assert.equal(validated.valid, true, validated.reason);
  return run('extract_invoice.js', { nodes: { 'Validate email': validated } });
};

test('extract: a complete invoice email gives an ok row on the main tab', async () => {
  const r = await extract({});
  assert.equal(r.status, 'ok');
  assert.equal(r.tab, 'Invoices');
  assert.equal(r.range, 'Invoices!A1:K1');
  assert.deepEqual(r.row, ['inv-mail-1@billing.example.com', '2026-11-24T09:15:00.000Z', 'billing@acme.example.com', 'Acme Billing', 'INV-1001', 'PO-77', 1250, 'USD', '2026-12-24', 'ok', '']);
});

test('extract: European and spaced amounts, currency codes', async () => {
  assert.deepEqual((await extract({ text: 'Invoice no. 88\nAmount due: EUR 1.234,50\nDue date: 15 Jan 2027' })).row.slice(4, 9), ['88', '', 1234.5, 'EUR', '2027-01-15']);
  assert.deepEqual((await extract({ text: 'Invoice #A-9\nTotal: 12 500,00 SEK\nDue: January 5, 2027' })).row.slice(6, 9), [12500, 'SEK', '2027-01-05']);
  assert.deepEqual((await extract({ text: 'Invoice #A-9\nTotal: £99\nDue date: 31/01/2027' })).row.slice(6, 9), [99, 'GBP', '2027-01-31']);
});

test('extract: missing or ambiguous fields go to the review tab with reasons', async () => {
  const missing = await extract({ text: 'Invoice number: INV-5\nDue date: 2026-12-01' });
  assert.equal(missing.status, 'needs_review');
  assert.equal(missing.tab, 'Review');
  assert.deepEqual(missing.missing, ['amount', 'currency']);
  const ambiguous = await extract({ text: 'Invoice number: INV-6\nTotal: 10 USD\nDue date: 03/04/2027' });
  assert.deepEqual(ambiguous.missing, ['due_date']);
  assert.match(ambiguous.row[10], /ambiguous/);
  const impossible = await extract({ text: 'Invoice number: INV-7\nTotal: 10 USD\nDue date: 2027-02-30' });
  assert.deepEqual(impossible.missing, ['due_date']);
});

test('extract: a value on the next line is not taken for an empty label', async () => {
  const r = await extract({ text: 'Invoice number:\nTotal: 10 USD\nDue date: 2027-01-01' });
  assert.deepEqual(r.missing, ['invoice_number']);
});

// --- 03 lead ------------------------------------------------------------------------------------
const LEAD = { submission_id: 'form-1', name: '  Robin   Avery ', email: ' Robin@Example.COM ', company: 'Avery Tools', consent: true };

test('lead: a valid submission is normalised', async () => {
  const r = await lead(LEAD);
  assert.equal(r.valid, true);
  assert.deepEqual(r.lead, { submission_id: 'form-1', name: 'Robin Avery', first_name: 'Robin', email: 'robin@example.com', company: 'Avery Tools', message: null, source: 'web-form' });
});

test('lead: consent, email, name, honeypot and size rules', async () => {
  assert.match((await lead({ ...LEAD, consent: 'yes' })).reason, /consent/);
  assert.match((await lead({ ...LEAD, consent: undefined })).reason, /consent/);
  assert.match((await lead({ ...LEAD, email: 'robin@example' })).reason, /email/);
  assert.match((await lead({ ...LEAD, name: '   ' })).reason, /name/);
  assert.match((await lead({ ...LEAD, website: 'http://spam.example' })).reason, /honeypot/);
  assert.match((await lead({ ...LEAD, message: 'x'.repeat(5001) })).reason, /5000/);
  assert.match((await lead({ ...LEAD, submission_id: '' })).reason, /submission_id/);
});

// --- error handler ------------------------------------------------------------------------------
test('error details: ids and a short single-line message only', async () => {
  const r = await run('error_details.js', {
    replace: { TRIGGER_NODE: 'Workflow failed' },
    nodes: { 'Workflow failed': { execution: { id: 17, lastNodeExecuted: 'Fail run (publish)', error: { message: 'publish:\n  permanent error' } }, workflow: { name: '01 Social' } } },
  });
  assert.deepEqual(r, { execution_id: '17', workflow: '01 Social', node: 'Fail run (publish)', message: 'publish: permanent error' });
});
