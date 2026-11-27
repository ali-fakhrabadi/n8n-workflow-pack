// Code node "Approval message" (01 social post). Run once for all items.
//
// Builds the chat message a reviewer acts on. The approve and reject links are this
// execution's resume URL (it carries n8n's resume token) plus a decision. The Wait node that
// follows resumes on the first click; a later click finds no waiting execution.
const settings = $('Settings').first().json;
const request = $('Validate request').first().json;
const timeoutS = Number(settings.approval_timeout_s);

// No URL class in the task runner's sandbox: the resume URL already carries a query string
// (n8n's resume token), so the decision is appended to it.
function link(decision) {
  const base = String($execution.resumeUrl);
  return `${base}${base.includes('?') ? '&' : '?'}decision=${decision}`;
}

const text = request.post.text;
const preview = [...text].length > 280 ? `${[...text].slice(0, 277).join('')}...` : text;
return [
  {
    json: {
      kind: 'approval_request',
      workflow: 'social-post-approval',
      key: request.key,
      channel: request.post.channel,
      requested_by: request.post.requested_by,
      text: `Approve this ${request.post.channel} post?\n\n${preview}`,
      approve_url: link('approve'),
      reject_url: link('reject'),
      expires_at: new Date(Date.now() + timeoutS * 1000).toISOString(),
    },
  },
];
