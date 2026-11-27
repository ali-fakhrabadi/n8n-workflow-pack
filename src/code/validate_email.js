// Code node "Validate email" (02 email to sheet). Run once for all items.
//
// The webhook receives an inbound-parse JSON (the shape Postmark, Mailgun or SendGrid post).
// The Message-ID is the idempotency key, normalised so "<ABC@x>" and "abc@x" are the same
// message. An HTML-only message is reduced to text.
const WEBHOOK_NODE = '__WEBHOOK_NODE__';
const MAX_TEXT = 100000;

const body = ($(WEBHOOK_NODE).first().json || {}).body;

function fail(reason) {
  return [{ json: { valid: false, reason } }];
}

function address(value) {
  const raw = typeof value === 'string' ? value.trim() : '';
  const angled = raw.match(/^(.*)<([^<>\s]+)>$/);
  const email = (angled ? angled[2] : raw).trim().toLowerCase();
  const name = angled ? angled[1].trim().replace(/^"|"$/g, '') : '';
  return /^[^@\s]+@[^@\s]+\.[a-z]{2,}$/i.test(email) ? { email, name } : null;
}

function htmlToText(html) {
  return html
    .replace(/<(script|style)[^>]*>[\s\S]*?<\/\1>/gi, ' ')
    .replace(/<br\s*\/?>|<\/(p|div|tr|li|h[1-6])>/gi, '\n')
    .replace(/<[^>]+>/g, ' ')
    .replace(/&nbsp;/g, ' ')
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&amp;/g, '&')
    .replace(/[ \t]+/g, ' ')
    .replace(/\n\s+/g, '\n')
    .trim();
}

if (!body || typeof body !== 'object' || Array.isArray(body)) return fail('body must be a JSON object');
const messageId =
  typeof body.message_id === 'string' ? body.message_id.trim().replace(/^<|>$/g, '').toLowerCase() : '';
if (!/^[^\s<>]{3,250}$/.test(messageId) || !messageId.includes('@')) {
  return fail('message_id is missing or malformed');
}
const from = address(body.from);
if (!from) return fail('from is not an email address');
let text = typeof body.text === 'string' ? body.text : '';
if (!text.trim() && typeof body.html === 'string') text = htmlToText(body.html);
text = text.replace(/\r\n/g, '\n').trim();
if (!text) return fail('message has no text');
if (text.length > MAX_TEXT) return fail(`message text is longer than ${MAX_TEXT} characters`);
const subject = typeof body.subject === 'string' ? body.subject.trim().slice(0, 300) : '';
const received = body.date ? new Date(body.date) : null;

return [
  {
    json: {
      valid: true,
      key: messageId,
      email: {
        message_id: messageId,
        from: from.email,
        from_name: from.name || null,
        subject,
        received_at: received && !Number.isNaN(received.getTime()) ? received.toISOString() : null,
        text,
      },
    },
  },
];
