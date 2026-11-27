// Code node "Read decision" (01 social post), right after the Wait node. Run once for all items.
//
// A click on a link resumes the Wait node with the request's query string. When the time
// limit passes first, the Wait node hands on its own input (the approval message), which has
// no query: the request expired. Anything but "approve" counts as a rejection, so a mangled
// link can never publish.
const resumed = $json || {};
const query = resumed.query && typeof resumed.query === 'object' ? resumed.query : null;

let decision;
if (!query) decision = 'expired';
else if (String(query.decision || '').toLowerCase() === 'approve') decision = 'approve';
else decision = 'reject';

const decidedBy = query && typeof query.by === 'string' ? query.by.slice(0, 120) : null;
const finalStatus = { approve: 'processing', reject: 'rejected', expired: 'expired' }[decision];
return [
  {
    json: {
      decision,
      decided_by: decidedBy,
      final_status: finalStatus,
      outcome: { decision, decided_by: decidedBy, published: false },
    },
  },
];
