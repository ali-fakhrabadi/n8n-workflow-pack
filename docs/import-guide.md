# Importing into your own n8n

Tested on n8n 2.45.6. Two ways in: the setup script (repeatable, used by the tests) or the
editor (by hand).

## 1. Create the ledger table

In a PostgreSQL database the workflows can reach (it can be n8n's own server):

```sql
CREATE ROLE automation LOGIN PASSWORD '…';
CREATE DATABASE automation;
\c automation
\i setup/ledger.sql
GRANT USAGE ON SCHEMA public TO automation;
GRANT SELECT, INSERT, UPDATE ON idempotency_keys TO automation;
```

## 2a. With the setup script

The script runs in the n8n image against n8n's database, creates the four credentials from
Docker secrets, fills the Settings nodes from `PACK_*` environment variables
([docs/workflows.md](workflows.md#settings)), imports the workflows and publishes them. See
the `n8n-setup` service in [`compose.yaml`](../compose.yaml) for the exact mounts and secrets:

| Secret | Used for |
|---|---|
| `webhook_token` | the value senders put in `X-Webhook-Token` |
| `downstream_token` | `Authorization: Bearer …` on calls to the chat, social, sheet and CRM APIs |
| `ledger_db_password` | the ledger credential (`PACK_LEDGER_HOST`, `_DB`, `_USER`, `_PORT`, `_SSL`) |
| `smtp_password` | only if `PACK_SMTP_USER` is set (`PACK_SMTP_HOST`, `_PORT`, `_SECURE`, `_STARTTLS`) |

Rerunning it updates in place (fixed workflow and credential ids); with nothing changed it
does nothing.

## 2b. In the editor

1. **Credentials → Create**, four of them, with exactly these names (the workflows refer to
   them by name and id; after import, open any node that shows a warning and pick the
   credential once):
   - *Inbound webhook token*: Header Auth, name `X-Webhook-Token`, value your token.
   - *Downstream API token*: Header Auth, name `Authorization`, value `Bearer …`. Use one per
     service in production; the nodes that call each service are named after it.
   - *Idempotency ledger (PostgreSQL)*: Postgres, the `automation` database and role.
   - *Outgoing mail (SMTP)*: your SMTP server.
2. **Workflows → Import from file**, `00-error-handler.json` first, then 01, 02, 03.
3. In each workflow, open **Settings** (the node) and set the URLs and time limits.
4. In 01-03, **Workflow settings → Error workflow** should already read `00 Error handler`;
   if the import gave it a new id, pick it again.
5. **Publish** all four, the error handler included.

## 3. Point the senders at the webhooks

| Workflow | URL | Sender |
|---|---|---|
| 01 | `https://<n8n>/webhook/social-post-requests` | your content calendar, form or script |
| 02 | `https://<n8n>/webhook/inbound-email` | the mail provider's inbound webhook |
| 03 | `https://<n8n>/webhook/lead-submissions` | the website form or form tool |

Every sender must add the `X-Webhook-Token` header and may retry freely: repeats answer
`200 {"duplicate": true}`.

## Swapping the mocks for real services

| Mock | Real service | What to change |
|---|---|---|
| chat (`/chat/messages`) | Slack or Teams incoming webhook | `chat_url`; map `text` if the service needs another field |
| social (`/social/v1/posts`) | LinkedIn, X or a scheduler's API | `social_api_url`, the body of **Publish post**, a per-service credential |
| sheets (`values:append`) | Google Sheets API | `sheets_api_url` = `https://sheets.googleapis.com`, `sheet_id`, an OAuth2 or service-account credential |
| CRM (`contacts:upsert`) | HubSpot, Pipedrive, … | `crm_api_url`, the upsert body and the stage field names |
| Mailpit (SMTP) | your SMTP relay | the SMTP credential |
