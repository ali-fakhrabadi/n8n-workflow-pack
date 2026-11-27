// Code node "Validate request" (01 social post). Run once for all items.
//
// Checks a post request before anything is claimed or sent. An invalid request is answered
// with 400 and the reason; nothing else happens. Text length is counted in Unicode code
// points, as the networks count it, against the channel's limit.
const WEBHOOK_NODE = '__WEBHOOK_NODE__';
const CHANNEL_LIMITS = { x: 280, mastodon: 500, linkedin: 3000, facebook: 5000 };
const KEY_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,99}$/;

const body = ($(WEBHOOK_NODE).first().json || {}).body;

function fail(reason) {
  return [{ json: { valid: false, reason } }];
}

if (!body || typeof body !== 'object' || Array.isArray(body)) return fail('body must be a JSON object');
const requestId = typeof body.request_id === 'string' ? body.request_id.trim() : '';
if (!KEY_PATTERN.test(requestId)) return fail('request_id is missing or malformed');
const channel = typeof body.channel === 'string' ? body.channel.trim().toLowerCase() : '';
if (!Object.hasOwn(CHANNEL_LIMITS, channel)) {
  return fail(`channel must be one of ${Object.keys(CHANNEL_LIMITS).join(', ')}`);
}
if (typeof body.text !== 'string') return fail('text is required');
const text = body.text.replace(/\r\n/g, '\n').trim();
if (!text) return fail('text is empty');
// Control characters other than newline and tab are never wanted in a post.
if (/[\u0000-\u0008\u000B-\u001F\u007F]/.test(text)) return fail('text contains control characters');
const length = [...text].length;
if (length > CHANNEL_LIMITS[channel]) {
  return fail(`text is ${length} characters; ${channel} allows ${CHANNEL_LIMITS[channel]}`);
}
// The task runner's sandbox has no URL class, so the URL is checked with a pattern.
let mediaUrl = null;
if (body.media_url !== undefined && body.media_url !== null && body.media_url !== '') {
  const raw = String(body.media_url).trim();
  const parts = raw.match(/^([a-z][a-z0-9+.-]*):\/\/([^\s/?#@]+)([/?#]\S*)?$/i);
  if (!parts || raw.length > 2000) return fail('media_url is not a URL');
  if (parts[1].toLowerCase() !== 'https') return fail('media_url must use https');
  mediaUrl = raw;
}
const requestedBy = typeof body.requested_by === 'string' ? body.requested_by.trim().slice(0, 120) : '';

return [
  {
    json: {
      valid: true,
      key: requestId,
      post: {
        request_id: requestId,
        channel,
        text,
        media_url: mediaUrl,
        requested_by: requestedBy || null,
        length,
      },
    },
  },
];
