// One-shot n8n setup: create the credentials, configure, import and publish the four workflows.
// Runs in the n8n image before n8n starts (the "n8n-setup" service), against n8n's database.
//
// Credentials are built from Docker secrets and environment variables, written to a file on
// tmpfs only for the n8n CLI to read, and deleted afterwards; n8n stores them encrypted with
// N8N_ENCRYPTION_KEY. Nothing secret is printed. The workflow files hold no credential values,
// only references by id and name.
//
// Idempotent: workflows and credentials have fixed ids, so a rerun updates them; when nothing
// changed since the last run (a hash kept in n8n's data volume) it does nothing.
import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { join } from 'node:path';

const bcrypt = createRequire('/usr/local/lib/node_modules/n8n/package.json')('bcryptjs');
const DATA = '/home/node/.n8n';
const OWNER_HASH_FILE = `${DATA}/pack-owner-password.bcrypt`;
const STATE_FILE = `${DATA}/pack-setup.sha256`;
const WORKFLOW_DIR = process.env.PACK_WORKFLOW_DIR || '/opt/pack/workflows';
// Error workflows must be published too: n8n 2.x runs only published versions, and an error
// workflow without one is skipped silently.
const PUBLISH = ['packErrorHandler', 'packSocialApprov', 'packEmailToSheet', 'packLeadFollowUp'];

function log(message) {
  console.log(`[pack-setup] ${new Date().toISOString()} ${message}`);
}

function secret(name) {
  let value = '';
  try {
    value = readFileSync(`/run/secrets/${name}`, 'utf8').trim();
  } catch {}
  if (!value) throw new Error(`secret ${name} is missing or empty`);
  return value;
}

function setting(name, fallback = '') {
  return (process.env[name] || fallback).trim();
}

function number(name, fallback) {
  const value = Number(setting(name, String(fallback)));
  if (!Number.isFinite(value) || value <= 0) throw new Error(`${name} must be a positive number`);
  return value;
}

const CREDENTIALS = {
  packWebhookAuth1: () => ({ name: 'X-Webhook-Token', value: secret('webhook_token') }),
  packDownstream01: () => ({ name: 'Authorization', value: `Bearer ${secret('downstream_token')}` }),
  packLedgerPg0001: () => ({
    host: setting('PACK_LEDGER_HOST', 'postgres'),
    port: number('PACK_LEDGER_PORT', 5432),
    database: setting('PACK_LEDGER_DB', 'automation'),
    user: setting('PACK_LEDGER_USER', 'automation'),
    password: secret('ledger_db_password'),
    ssl: setting('PACK_LEDGER_SSL', 'disable'),
    allowUnauthorizedCerts: false,
    sshTunnel: false,
  }),
  packSmtpSink0001: () => ({
    user: setting('PACK_SMTP_USER'),
    password: setting('PACK_SMTP_USER') ? secret('smtp_password') : '',
    host: setting('PACK_SMTP_HOST', 'mailpit'),
    port: number('PACK_SMTP_PORT', 1025),
    secure: setting('PACK_SMTP_SECURE', 'false') === 'true',
    disableStartTls: setting('PACK_SMTP_STARTTLS', 'false') !== 'true',
  }),
};

function credentials(workflows) {
  const byId = new Map();
  for (const workflow of workflows) {
    for (const node of workflow.nodes) {
      for (const [type, ref] of Object.entries(node.credentials || {})) {
        if (!CREDENTIALS[ref.id]) throw new Error(`unknown credential ${ref.id} (${ref.name})`);
        byId.set(ref.id, { id: ref.id, name: ref.name, type, data: CREDENTIALS[ref.id]() });
      }
    }
  }
  return [...byId.values()];
}

function values(workflowId) {
  const approval = number('PACK_APPROVAL_TIMEOUT_S', 172800);
  const followUp = number('PACK_FOLLOW_UP_DELAY_S', 259200);
  // A claim older than this while still "processing" is treated as abandoned (n8n lost the
  // run) and may be taken over by a redelivery. Long-waiting workflows get their wait added.
  const stale = {
    packSocialApprov: approval + 3600,
    packLeadFollowUp: followUp + 3600,
  }[workflowId] || number('PACK_CLAIM_STALE_AFTER_S', 900);
  return {
    chat_url: setting('PACK_CHAT_URL', 'http://mock-sinks:8000/chat/messages'),
    social_api_url: setting('PACK_SOCIAL_API_URL', 'http://mock-sinks:8000/social').replace(/\/+$/, ''),
    sheets_api_url: setting('PACK_SHEETS_API_URL', 'http://mock-sinks:8000/sheets').replace(/\/+$/, ''),
    sheet_id: setting('PACK_SHEET_ID', 'demo-invoices'),
    sheet_tab: setting('PACK_SHEET_TAB', 'Invoices'),
    review_tab: setting('PACK_REVIEW_TAB', 'Review'),
    crm_api_url: setting('PACK_CRM_API_URL', 'http://mock-sinks:8000/crm').replace(/\/+$/, ''),
    mail_from: setting('PACK_MAIL_FROM', 'hello@example.com'),
    approval_timeout_s: approval,
    follow_up_delay_s: followUp,
    max_attempts: number('PACK_MAX_ATTEMPTS', 4),
    retry_base_s: number('PACK_RETRY_BASE_S', 5),
    claim_stale_after_s: stale,
  };
}

function configure(workflow) {
  const settingsValues = values(workflow.id);
  for (const node of workflow.nodes.filter((n) => n.name === 'Settings')) {
    for (const assignment of node.parameters.assignments.assignments) {
      if (assignment.name in settingsValues) assignment.value = settingsValues[assignment.name];
    }
  }
  return workflow;
}

function n8n(...args) {
  execFileSync('n8n', args, { stdio: ['ignore', 'inherit', 'inherit'] });
}

function ownerHash() {
  const password = secret('n8n_owner_password');
  try {
    if (bcrypt.compareSync(password, readFileSync(OWNER_HASH_FILE, 'utf8').trim())) return;
  } catch {}
  writeFileSync(OWNER_HASH_FILE, `${bcrypt.hashSync(password, 12)}\n`, { mode: 0o600 });
  log('owner password hash written');
}

function main() {
  ownerHash();
  const files = readdirSync(WORKFLOW_DIR).filter((f) => f.endsWith('.json')).sort();
  const workflows = files.map((f) => configure(JSON.parse(readFileSync(join(WORKFLOW_DIR, f), 'utf8'))));
  const creds = credentials(workflows);
  const digest = createHash('sha256').update(JSON.stringify({ workflows, creds })).digest('hex');
  let previous = '';
  try {
    previous = readFileSync(STATE_FILE, 'utf8').trim();
  } catch {}
  if (previous === digest && !process.argv.includes('--force')) {
    log('workflows and credentials are up to date');
    return;
  }
  const dir = mkdtempSync('/tmp/pack-setup-');
  try {
    const credentialsFile = join(dir, 'credentials.json');
    const workflowsFile = join(dir, 'workflows.json');
    writeFileSync(credentialsFile, JSON.stringify(creds), { mode: 0o600 });
    writeFileSync(workflowsFile, JSON.stringify(workflows), { mode: 0o600 });
    log('importing');
    n8n('import:credentials', `--input=${credentialsFile}`);
    n8n('import:workflow', `--input=${workflowsFile}`);
    for (const id of PUBLISH) n8n('publish:workflow', `--id=${id}`);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
  writeFileSync(STATE_FILE, `${digest}\n`, { mode: 0o600 });
  log(`imported ${creds.length} credentials and ${workflows.length} workflows; published ${PUBLISH.length}`);
}

try {
  main();
} catch (error) {
  console.error(`[pack-setup] ${error.message}`);
  process.exit(1);
}
