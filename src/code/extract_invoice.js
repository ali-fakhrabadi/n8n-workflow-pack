// Code node "Extract fields" (02 email to sheet). Run once for all items.
//
// Reads invoice fields from labelled lines of the email (no model, so the same email always
// gives the same row). Amounts in "1,234.50", "1.234,50" or "1 234,50" form are normalised; a
// slash date is accepted only when the day/month order is unambiguous. A row with a missing or
// doubtful field still reaches the sheet, but on the review tab with the reasons.
const settings = $('Settings').first().json;
const { email } = $('Validate email').first().json;
const text = `${email.subject}\n${email.text}`;

const SYMBOLS = { $: 'USD', '€': 'EUR', '£': 'GBP' };
const CURRENCIES = ['USD', 'EUR', 'GBP', 'CAD', 'AUD', 'CHF', 'SEK', 'NOK', 'DKK', 'AED'];
const MONTHS = ['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'];
const TOKEN = /[A-Za-z0-9][A-Za-z0-9/_-]*/;

function labelled(labels) {
  for (const label of labels) {
    const match = text.match(new RegExp(`(?:^|\\n)[ \\t]*(?:${label})[ \\t]*[:#-]?[ \\t]*([^\\n]+)`, 'i'));
    if (match) return match[1].trim();
  }
  return null;
}

function parseAmount(raw) {
  if (!raw) return { amount: null, currency: null };
  let currency = null;
  for (const [symbol, code] of Object.entries(SYMBOLS)) if (raw.includes(symbol)) currency = code;
  const code = raw.toUpperCase().match(new RegExp(`\\b(${CURRENCIES.join('|')})\\b`));
  if (code) currency = code[1];
  const number = raw.match(/-?\d[\d.,'  ]*/);
  if (!number) return { amount: null, currency };
  let digits = number[0].replace(/['  ]/g, '').replace(/[.,]$/, '');
  const lastComma = digits.lastIndexOf(',');
  const lastDot = digits.lastIndexOf('.');
  if (lastComma > lastDot) {
    // "1.234,50" or "1234,50": the comma is the decimal separator when two digits follow it.
    digits = /,\d{2}$/.test(digits) ? digits.replace(/\./g, '').replace(',', '.') : digits.replace(/,/g, '');
  } else {
    digits = digits.replace(/,/g, '');
  }
  const amount = Number(digits);
  return { amount: Number.isFinite(amount) ? Math.round(amount * 100) / 100 : null, currency };
}

function iso(year, month, day) {
  const date = new Date(Date.UTC(year, month - 1, day));
  const valid = date.getUTCFullYear() === year && date.getUTCMonth() === month - 1 && date.getUTCDate() === day;
  return valid ? date.toISOString().slice(0, 10) : null;
}

function month(name) {
  const index = MONTHS.indexOf(name.slice(0, 3).toLowerCase());
  return index === -1 ? null : index + 1;
}

function parseDate(raw) {
  if (!raw) return { date: null, issue: null };
  let m = raw.match(/(\d{4})-(\d{2})-(\d{2})/);
  if (m) return { date: iso(+m[1], +m[2], +m[3]), issue: null };
  m = raw.match(/(\d{1,2})\s+([A-Za-z]{3,9})\.?\s+(\d{4})/);
  if (m && month(m[2])) return { date: iso(+m[3], month(m[2]), +m[1]), issue: null };
  m = raw.match(/([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})/);
  if (m && month(m[1])) return { date: iso(+m[3], month(m[1]), +m[2]), issue: null };
  m = raw.match(/(\d{1,2})[/.](\d{1,2})[/.](\d{4})/);
  if (m) {
    const [a, b, year] = [+m[1], +m[2], +m[3]];
    if (a > 12 && b <= 12) return { date: iso(year, b, a), issue: null };
    if (b > 12 && a <= 12) return { date: iso(year, a, b), issue: null };
    if (a === b) return { date: iso(year, a, b), issue: null };
    return { date: null, issue: `due date "${m[0]}" is ambiguous (day/month order)` };
  }
  return { date: null, issue: `due date "${raw.slice(0, 40)}" not understood` };
}

const invoiceNumber = (labelled(['invoice[ \\t]*(?:number|no\\.?|#)', 'inv[ \\t]*#']) || '').match(TOKEN);
const poNumber = (labelled(['po[ \\t]*(?:number|no\\.?|#)', 'purchase[ \\t]+order']) || '').match(TOKEN);
const { amount, currency } = parseAmount(labelled(['total[ \\t]+due', 'amount[ \\t]+due', 'total', 'amount']));
const due = parseDate(labelled(['due[ \\t]+date', 'payment[ \\t]+due', 'due']));
const vendor = (labelled(['vendor', 'supplier', 'company']) || email.from_name || '').slice(0, 120) || null;

const missing = [];
const issues = [];
if (!invoiceNumber) missing.push('invoice_number');
if (amount === null) missing.push('amount');
if (!currency) missing.push('currency');
if (!due.date) missing.push('due_date');
for (const field of missing) issues.push(field === 'due_date' && due.issue ? due.issue : `${field} missing`);
const status = missing.length ? 'needs_review' : 'ok';
const tab = status === 'ok' ? settings.sheet_tab : settings.review_tab;

const row = [
  email.message_id,
  email.received_at || '',
  email.from,
  vendor || '',
  invoiceNumber ? invoiceNumber[0] : '',
  poNumber ? poNumber[0] : '',
  amount === null ? '' : amount,
  currency || '',
  due.date || '',
  status,
  issues.join('; '),
];
return [{ json: { status, tab, missing, issues, row, range: `${tab}!A1:K1` } }];
