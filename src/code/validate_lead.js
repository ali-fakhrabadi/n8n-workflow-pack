// Code node "Validate lead" (03 lead follow-up). Run once for all items.
//
// A form submission is followed up only with consent and a plausible address. A filled
// honeypot field ("website", hidden from people) marks a bot. The email is normalised so the
// CRM finds the same contact however it was typed.
const WEBHOOK_NODE = '__WEBHOOK_NODE__';
const KEY_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,99}$/;

const body = ($(WEBHOOK_NODE).first().json || {}).body;

function fail(reason) {
  return [{ json: { valid: false, reason } }];
}

function clean(value, max) {
  return typeof value === 'string' ? value.replace(/\s+/g, ' ').trim().slice(0, max) : '';
}

if (!body || typeof body !== 'object' || Array.isArray(body)) return fail('body must be a JSON object');
const submissionId = typeof body.submission_id === 'string' ? body.submission_id.trim() : '';
if (!KEY_PATTERN.test(submissionId)) return fail('submission_id is missing or malformed');
if (clean(body.website, 200)) return fail('spam suspected (honeypot field filled)');
const email = typeof body.email === 'string' ? body.email.trim().toLowerCase() : '';
if (email.length > 254 || !/^[^@\s]+@[^@\s]+\.[a-z]{2,}$/.test(email)) return fail('email is not valid');
const name = clean(body.name, 120);
if (!name) return fail('name is required');
if (body.consent !== true) return fail('consent to be contacted is required');
const message = typeof body.message === 'string' ? body.message.trim() : '';
if (message.length > 5000) return fail('message is longer than 5000 characters');

return [
  {
    json: {
      valid: true,
      key: submissionId,
      lead: {
        submission_id: submissionId,
        name,
        first_name: name.split(' ')[0],
        email,
        company: clean(body.company, 200) || null,
        message: message || null,
        source: clean(body.source, 60) || 'web-form',
      },
    },
  },
];
