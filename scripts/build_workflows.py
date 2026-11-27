#!/usr/bin/env python3
"""Build the import-ready workflow exports in workflows/ from this definition and src/code/*.js.

The Code nodes' JavaScript lives in src/code/ (unit-tested with `node --test tests/unit`); this
script embeds it. Node ids are derived from names, so the output is the same on every run and
CI fails when the committed JSON is stale. No credential values are written: nodes refer to
credentials by id and name, and setup/setup.mjs creates them from secrets.

    python3 scripts/build_workflows.py [--check]
"""

from __future__ import annotations

import argparse
import itertools
import json
import re
import sys
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CODE = ROOT / "src" / "code"
OUT = ROOT / "workflows"

ERRORS_ID = "packErrorHandler"
SOCIAL_ID = "packSocialApprov"
EMAIL_ID = "packEmailToSheet"
LEAD_ID = "packLeadFollowUp"

WEBHOOK_AUTH = {"id": "packWebhookAuth1", "name": "Inbound webhook token"}
DOWNSTREAM = {"id": "packDownstream01", "name": "Downstream API token"}
LEDGER = {"id": "packLedgerPg0001", "name": "Idempotency ledger (PostgreSQL)"}
SMTP = {"id": "packSmtpSink0001", "name": "Outgoing mail (SMTP)"}

_NAMESPACE = uuid.UUID("7d1e4c2a-5b3f-4e8d-9a60-2f1c8b7e4d35")
SETTINGS = "$('Settings').first().json"
TRUE = {"type": "boolean", "operation": "true", "singleValue": True}
EXISTS = {"type": "string", "operation": "exists", "singleValue": True}

# Production defaults; setup/setup.mjs overrides them from environment variables.
DEFAULTS: dict[str, Any] = {
    "chat_url": "http://mock-sinks:8000/chat/messages",
    "social_api_url": "http://mock-sinks:8000/social",
    "sheets_api_url": "http://mock-sinks:8000/sheets",
    "sheet_id": "demo-invoices",
    "sheet_tab": "Invoices",
    "review_tab": "Review",
    "crm_api_url": "http://mock-sinks:8000/crm",
    "mail_from": "hello@example.com",
    "approval_timeout_s": 172800,
    "follow_up_delay_s": 259200,
    "max_attempts": 4,
    "retry_base_s": 5,
    "claim_stale_after_s": 3600,
}

CLAIM_SQL = """\
INSERT INTO idempotency_keys (workflow, key, status, execution_id)
VALUES ($1, $2, 'processing', $3)
ON CONFLICT (workflow, key) DO UPDATE
   SET status = 'processing', execution_id = EXCLUDED.execution_id,
       attempts = idempotency_keys.attempts + 1, error = NULL, updated_at = now()
 WHERE idempotency_keys.status = 'failed'
    OR (idempotency_keys.status = 'processing'
        AND idempotency_keys.updated_at < now() - make_interval(secs => $4::double precision))
RETURNING workflow, key, attempts;"""

FINISH_SQL = """\
UPDATE idempotency_keys
   SET status = $3, outcome = $4::jsonb, updated_at = now()
 WHERE workflow = $1 AND key = $2 AND execution_id = $5
RETURNING workflow, key, status, attempts, outcome;"""

RELEASE_SQL = """\
UPDATE idempotency_keys
   SET status = 'failed', error = $2, updated_at = now()
 WHERE execution_id = $1 AND status = 'processing'
RETURNING workflow, key, attempts;"""


class Flow:
    """Nodes and connections of one workflow, with ids derived from the workflow and node name."""

    def __init__(self, prefix: str) -> None:
        self.prefix = prefix
        self.nodes: list[dict[str, Any]] = []
        self.connections: dict[str, Any] = {}

    def id(self, name: str) -> str:
        return str(uuid.uuid5(_NAMESPACE, f"{self.prefix}/{name}"))

    def add(
        self,
        name: str,
        kind: str,
        version: float,
        position: tuple[int, int],
        parameters: dict[str, Any],
        **extra: Any,
    ) -> str:
        self.nodes.append(
            {
                "parameters": parameters,
                "id": self.id(name),
                "name": name,
                "type": kind if "." in kind else f"n8n-nodes-base.{kind}",
                "typeVersion": version,
                "position": list(position),
                **extra,
            }
        )
        return name

    def note(self, name: str, position: tuple[int, int], size: tuple[int, int], text: str) -> None:
        self.add(
            name,
            "stickyNote",
            1,
            position,
            {"content": text, "width": size[0], "height": size[1], "color": 7},
        )

    def link(self, source: str, *targets: str, output: int = 0) -> None:
        outputs = self.connections.setdefault(source, {"main": []})["main"]
        while len(outputs) <= output:
            outputs.append([])
        outputs[output].extend({"node": t, "type": "main", "index": 0} for t in targets)

    def chain(self, *names: str) -> None:
        for a, b in itertools.pairwise(names):
            self.link(a, b)


def code(file: str, **replace: str) -> str:
    text = (CODE / file).read_text(encoding="utf-8")
    for key, value in replace.items():
        text = text.replace(f"__{key}__", value)
    if re.search(r"'__[A-Z_]+__'", text):
        raise SystemExit(f"{file}: unfilled placeholder")
    return text


def condition(cid: str, left: str, operator: dict[str, Any], right: Any = "") -> dict[str, Any]:
    return {
        "options": {
            "caseSensitive": True,
            "leftValue": "",
            "typeValidation": "loose",
            "version": 2,
        },
        "conditions": [{"id": cid, "leftValue": left, "rightValue": right, "operator": operator}],
        "combinator": "and",
    }


def if_node(flow: Flow, name: str, position: tuple[int, int], left: str, op: dict[str, Any]) -> str:
    return flow.add(
        name,
        "if",
        2.2,
        position,
        {"conditions": condition(flow.id(f"{name}-c"), left, op), "options": {}},
    )


def switch(flow: Flow, name: str, position: tuple[int, int], field: str, outs: list[str]) -> str:
    rules = [
        {
            "conditions": condition(
                flow.id(f"{name}-{out}"),
                f"={{{{ $json.{field} }}}}",
                {"type": "string", "operation": "equals"},
                out,
            ),
            "renameOutput": True,
            "outputKey": out,
        }
        for out in outs
    ]
    return flow.add(name, "switch", 3.2, position, {"rules": {"values": rules}, "options": {}})


def settings(flow: Flow, position: tuple[int, int], names: list[str]) -> str:
    assignments = []
    for key in names:
        value = DEFAULTS[key]
        kind = "number" if isinstance(value, int) else "string"
        assignments.append({"id": flow.id(f"setting-{key}"), "name": key, "value": value, "type": kind})
    return flow.add(
        "Settings",
        "set",
        3.4,
        position,
        {"assignments": {"assignments": assignments}, "options": {}},
    )


def js(flow: Flow, name: str, position: tuple[int, int], source: str) -> str:
    return flow.add(name, "code", 2, position, {"jsCode": source})


def sql(
    flow: Flow,
    name: str,
    position: tuple[int, int],
    query: str,
    values: list[str],
    *,
    always_output: bool = False,
) -> str:
    """Postgres 2.8 query: each {{ }} in the replacement list is exactly one parameter."""
    extra: dict[str, Any] = {"credentials": {"postgres": LEDGER}}
    if always_output:
        extra["alwaysOutputData"] = True
    return flow.add(
        name,
        "postgres",
        2.8,
        position,
        {
            "operation": "executeQuery",
            "query": query,
            "options": {"queryReplacement": "=" + ",".join(f"{{{{ {v} }}}}" for v in values)},
        },
        **extra,
    )


def http(
    flow: Flow,
    name: str,
    position: tuple[int, int],
    method: str,
    url: str,
    body: str | None = None,
    *,
    headers: dict[str, str] | None = None,
    classify: bool = False,
    retries: int = 0,
    best_effort: bool = False,
    timeout_ms: int = 15000,
) -> str:
    """HTTP call to a downstream API with the downstream token credential.

    classify=True returns the full response without throwing, for a Classify node to judge.
    retries>0 uses n8n's built-in retry, for idempotent calls (reads, notices, stage updates).
    """
    params: dict[str, Any] = {
        "method": method,
        "url": url,
        "authentication": "genericCredentialType",
        "genericAuthType": "httpHeaderAuth",
        "options": {"timeout": timeout_ms},
    }
    if headers:
        params["sendHeaders"] = True
        params["headerParameters"] = {"parameters": [{"name": k, "value": v} for k, v in headers.items()]}
    if body is not None:
        params.update({"sendBody": True, "specifyBody": "json", "jsonBody": body})
    extra: dict[str, Any] = {"credentials": {"httpHeaderAuth": DOWNSTREAM}}
    if classify:
        params["options"]["response"] = {"response": {"fullResponse": True, "neverError": True}}
        extra["onError"] = "continueRegularOutput"
    if retries:
        extra.update({"retryOnFail": True, "maxTries": retries, "waitBetweenTries": 2000})
    if best_effort:
        extra["onError"] = "continueRegularOutput"
    return flow.add(name, "httpRequest", 4.2, position, params, **extra)


def wait_seconds(flow: Flow, name: str, position: tuple[int, int], amount: str) -> str:
    return flow.add(
        name,
        "wait",
        1.1,
        position,
        {"resume": "timeInterval", "amount": amount, "unit": "seconds"},
        webhookId=flow.id(f"{name}-webhook"),
    )


def set_outcome(flow: Flow, name: str, position: tuple[int, int], status: str, outcome: str) -> str:
    return flow.add(
        name,
        "set",
        3.4,
        position,
        {
            "assignments": {
                "assignments": [
                    {"id": flow.id(f"{name}-s"), "name": "final_status", "value": status, "type": "string"},
                    {"id": flow.id(f"{name}-o"), "name": "outcome", "value": outcome, "type": "object"},
                ]
            },
            "options": {},
        },
    )


def entry(
    flow: Flow,
    *,
    path: str,
    validator: str,
    validate_name: str,
    workflow_key: str,
    setting_names: list[str],
    y: int = 300,
) -> None:
    """Webhook, Settings, validation, claim, and the three immediate answers (400/200/202)."""
    webhook = "Webhook"
    flow.add(
        webhook,
        "webhook",
        2.1,
        (0, y),
        {
            "httpMethod": "POST",
            "path": path,
            "authentication": "headerAuth",
            "responseMode": "responseNode",
            "options": {},
        },
        webhookId=flow.id("webhook-id"),
        credentials={"httpHeaderAuth": WEBHOOK_AUTH},
    )
    settings(flow, (220, y), [*setting_names, "claim_stale_after_s"])
    js(flow, validate_name, (440, y), code(validator, WEBHOOK_NODE=webhook))
    if_node(flow, "Valid?", (660, y), "={{ $json.valid }}", TRUE)
    flow.add(
        "Reject 400",
        "respondToWebhook",
        1.1,
        (880, y + 200),
        {
            "respondWith": "json",
            "responseBody": "={{ JSON.stringify({ accepted: false, error: $json.reason }) }}",
            "options": {"responseCode": 400},
        },
    )
    sql(
        flow,
        "Claim key",
        (880, y - 20),
        CLAIM_SQL,
        [
            f"'{workflow_key}'",
            "$json.key",
            "$execution.id",
            f"{SETTINGS}.claim_stale_after_s",
        ],
        always_output=True,
    )
    if_node(flow, "First delivery?", (1100, y - 20), "={{ $json.key }}", EXISTS)
    flow.add(
        "Duplicate 200",
        "respondToWebhook",
        1.1,
        (1320, y + 160),
        {
            "respondWith": "json",
            "responseBody": (
                f"={{{{ JSON.stringify({{ accepted: false, duplicate: true, "
                f"key: $('{validate_name}').first().json.key }}) }}}}"
            ),
            "options": {"responseCode": 200},
        },
    )
    flow.add(
        "Accept 202",
        "respondToWebhook",
        1.1,
        (1320, y - 20),
        {
            "respondWith": "json",
            "responseBody": ("={{ JSON.stringify({ accepted: true, key: $json.key, execution_id: $execution.id }) }}"),
            "options": {"responseCode": 202},
        },
    )
    flow.chain(webhook, "Settings", validate_name, "Valid?")
    flow.link("Valid?", "Claim key", output=0)
    flow.link("Valid?", "Reject 400", output=1)
    flow.link("Claim key", "First delivery?")
    flow.link("First delivery?", "Accept 202", output=0)
    flow.link("First delivery?", "Duplicate 200", output=1)


def retry_loop(
    flow: Flow,
    *,
    call: str,
    step: str,
    x: int,
    y: int,
) -> tuple[str, str]:
    """Classify -> Switch(ok/retry/failed) -> Wait -> back to the call; failed -> Stop and Error."""
    classify = js(flow, f"Classify {step}", (x, y), code("classify_response.js", STEP=step))
    route = switch(flow, f"{step.capitalize()} result", (x + 220, y), "route", ["ok", "retry", "failed"])
    waiter = wait_seconds(flow, f"Back off ({step})", (x + 440, y + 180), "={{ $json.wait_s }}")
    stop = flow.add(
        f"Fail run ({step})",
        "stopAndError",
        1,
        (x + 440, y + 360),
        {"errorType": "errorMessage", "errorMessage": "={{ $json.error }}"},
    )
    flow.link(call, classify)
    flow.link(classify, route)
    flow.link(route, waiter, output=1)
    flow.link(route, stop, output=2)
    flow.link(waiter, call)
    return route, classify


def finish(flow: Flow, workflow_key: str, key_expr: str, x: int, y: int) -> None:
    """Record the final status in the ledger, then post a run notice (best effort)."""
    sql(
        flow,
        "Record outcome",
        (x, y),
        FINISH_SQL,
        [
            f"'{workflow_key}'",
            key_expr,
            "$json.final_status",
            "JSON.stringify($json.outcome || {})",
            "$execution.id",
        ],
    )
    http(
        flow,
        "Post run notice",
        (x + 220, y),
        "POST",
        f"={{{{ {SETTINGS}.chat_url }}}}",
        (
            "={{ JSON.stringify({ kind: 'run_finished', workflow: $json.workflow, key: $json.key, "
            "status: $json.status, attempts: $json.attempts, outcome: $json.outcome, "
            "execution_id: $execution.id }) }}"
        ),
        retries=3,
        best_effort=True,
    )
    flow.link("Record outcome", "Post run notice")


def workflow(flow: Flow, wid: str, name: str, *, error_workflow: bool = True, **settings_: Any) -> dict[str, Any]:
    settings_block: dict[str, Any] = {
        "executionOrder": "v1",
        "saveDataSuccessExecution": "all",
        "saveDataErrorExecution": "all",
        "saveManualExecutions": True,
        "callerPolicy": "workflowsFromSameOwner",
        **settings_,
    }
    if error_workflow:
        settings_block["errorWorkflow"] = ERRORS_ID
    return {
        "id": wid,
        "name": name,
        "nodes": flow.nodes,
        "connections": flow.connections,
        "active": False,
        "settings": settings_block,
        "pinData": {},
        "tags": [],
    }


# --- 00 error handler ---------------------------------------------------------------------------
def error_handler() -> dict[str, Any]:
    f = Flow("errors")
    f.note(
        "Note: errors",
        (-40, 80),
        (900, 200),
        "## Error handler (shared by all three workflows)\nAny failed run lands here. The "
        "ledger row of that run is set to **failed**, so a redelivery of the same event runs "
        "again instead of being treated as a duplicate. An alert with ids only (workflow, node, "
        "execution, key; never the payload) goes to the chat channel.",
    )
    f.add("Workflow failed", "errorTrigger", 1, (0, 400), {})
    settings(f, (220, 400), ["chat_url"])
    js(f, "Error details", (440, 400), code("error_details.js", TRIGGER_NODE="Workflow failed"))
    sql(
        f,
        "Release key",
        (660, 400),
        RELEASE_SQL,
        ["$json.execution_id", "$json.message"],
        always_output=True,
    )
    http(
        f,
        "Send alert",
        (880, 400),
        "POST",
        f"={{{{ {SETTINGS}.chat_url }}}}",
        (
            "={{ JSON.stringify({ kind: 'workflow_error', "
            "workflow: $('Error details').first().json.workflow, "
            "node: $('Error details').first().json.node, "
            "execution_id: $('Error details').first().json.execution_id, "
            "message: $('Error details').first().json.message, "
            "ledger_workflow: $json.workflow || null, key: $json.key || null, "
            "released: Boolean($json.key) }) }}"
        ),
        retries=5,
    )
    f.chain("Workflow failed", "Settings", "Error details", "Release key", "Send alert")
    return workflow(f, ERRORS_ID, "00 Error handler", error_workflow=False, executionTimeout=300)


# --- 01 social post with approval ----------------------------------------------------------------
def social() -> dict[str, Any]:
    key = "social-post-approval"
    req = "$('Validate request').first().json"
    f = Flow("social")
    f.note(
        "Note: intake",
        (-40, -40),
        (1560, 200),
        "## 01 Social post with human approval\nA post request is checked (channel, length, "
        "media URL), then its **request_id** is claimed in the ledger: a repeat answers "
        "`200 duplicate` and does nothing, even when copies arrive at the same moment. The "
        "sender gets `202` at once; the rest runs in the background.",
    )
    entry(
        f,
        path="social-post-requests",
        validator="validate_social.js",
        validate_name="Validate request",
        workflow_key=key,
        setting_names=["chat_url", "social_api_url", "approval_timeout_s", "max_attempts", "retry_base_s"],
    )
    f.note(
        "Note: approval",
        (1520, -40),
        (900, 200),
        "## Approval\nA reviewer gets approve / reject links in chat. The run waits (no polling) "
        "until the first click or the time limit; after the limit the request **expires** and "
        "nothing is published.",
    )
    js(f, "Approval message", (1540, 280), code("approval_message.js"))
    http(
        f,
        "Ask for approval",
        (1760, 280),
        "POST",
        f"={{{{ {SETTINGS}.chat_url }}}}",
        "={{ JSON.stringify($json) }}",
        retries=4,
    )
    f.add(
        "Wait for decision",
        "wait",
        1.1,
        (1980, 280),
        {
            "resume": "webhook",
            "httpMethod": "GET",
            "responseMode": "onReceived",
            "limitWaitTime": True,
            "limitType": "afterTimeInterval",
            "resumeAmount": f"={{{{ {SETTINGS}.approval_timeout_s }}}}",
            "resumeUnit": "seconds",
            "options": {},
        },
        webhookId=f.id("approval-webhook"),
    )
    js(f, "Read decision", (2200, 280), code("read_decision.js"))
    switch(f, "Decision", (2420, 280), "decision", ["approve", "reject", "expired"])
    f.chain("Accept 202", "Approval message", "Ask for approval", "Wait for decision", "Read decision", "Decision")
    f.note(
        "Note: publish",
        (2620, -40),
        (1100, 200),
        "## Publish with retries\nThe post goes out with an **Idempotency-Key** header. 429 and "
        "5xx are retried with exponential backoff and jitter (Retry-After honoured); other 4xx "
        "stop at once. A failed run raises an error, so the error workflow alerts and releases "
        "the key.",
    )
    http(
        f,
        "Publish post",
        (2640, 200),
        "POST",
        f"={{{{ {SETTINGS}.social_api_url }}}}/v1/posts",
        (
            f"={{{{ JSON.stringify({{ channel: {req}.post.channel, text: {req}.post.text, "
            f"media_url: {req}.post.media_url }}) }}}}"
        ),
        headers={"Idempotency-Key": f"={{{{ {req}.key }}}}"},
        classify=True,
    )
    route, _ = retry_loop(f, call="Publish post", step="publish", x=2860, y=200)
    set_outcome(
        f,
        "Published",
        (3520, 120),
        "done",
        (
            "={{ { decision: 'approve', decided_by: $('Read decision').first().json.decided_by, "
            "published: true, post_id: $json.outcome.id, url: $json.outcome.url, "
            "attempts: $json.attempt } }}"
        ),
    )
    f.link("Decision", "Publish post", output=0)
    f.link("Decision", "Record outcome", output=1)
    f.link("Decision", "Record outcome", output=2)
    f.link(route, "Published", output=0)
    f.link("Published", "Record outcome")
    finish(f, key, f"{req}.key", 3740, 400)
    return workflow(f, SOCIAL_ID, "01 Social post with human approval")


# --- 02 email to spreadsheet ---------------------------------------------------------------------
def email() -> dict[str, Any]:
    key = "email-to-sheet"
    mail = "$('Validate email').first().json"
    ext = "$('Extract fields').first().json"
    f = Flow("email")
    f.note(
        "Note: intake",
        (-40, -40),
        (1560, 200),
        "## 02 Email to spreadsheet\nYour mail provider's inbound webhook (Postmark, Mailgun, "
        "SendGrid) posts each email as JSON. The normalised **Message-ID** is claimed in the "
        "ledger, so a provider retry or a forwarded copy never adds a second row.",
    )
    entry(
        f,
        path="inbound-email",
        validator="validate_email.js",
        validate_name="Validate email",
        workflow_key=key,
        setting_names=[
            "chat_url",
            "sheets_api_url",
            "sheet_id",
            "sheet_tab",
            "review_tab",
            "max_attempts",
            "retry_base_s",
        ],
    )
    f.note(
        "Note: extract",
        (1520, -40),
        (1340, 200),
        "## Extract and append\nLabelled fields (invoice no., PO, total, currency, due date) "
        "are read without a model, so the same email always gives the same row. A row with a "
        "missing or ambiguous field goes to the **Review** tab with the reason. The append "
        "uses the Google Sheets `values:append` call and is retried on 429/5xx.",
    )
    js(f, "Extract fields", (1540, 280), code("extract_invoice.js"))
    http(
        f,
        "Append row",
        (1760, 280),
        "POST",
        (
            f"={{{{ {SETTINGS}.sheets_api_url }}}}/v4/spreadsheets/{{{{ {SETTINGS}.sheet_id }}}}"
            f"/values/{{{{ encodeURIComponent({ext}.range) }}}}:append"
            "?valueInputOption=RAW&insertDataOption=INSERT_ROWS"
        ),
        f"={{{{ JSON.stringify({{ values: [ {ext}.row ] }}) }}}}",
        headers={"Idempotency-Key": f"={{{{ {mail}.key }}}}"},
        classify=True,
    )
    route, _ = retry_loop(f, call="Append row", step="append", x=1980, y=280)
    set_outcome(
        f,
        "Row written",
        (2640, 200),
        "done",
        (
            f"={{{{ {{ tab: {ext}.tab, row_status: {ext}.status, issues: {ext}.issues, "
            "updated_range: ($json.outcome.updates || {}).updatedRange || null, "
            "attempts: $json.attempt } }}"
        ),
    )
    f.chain("Accept 202", "Extract fields", "Append row")
    f.link(route, "Row written", output=0)
    f.link("Row written", "Record outcome")
    finish(f, key, f"{mail}.key", 2860, 200)
    return workflow(f, EMAIL_ID, "02 Email to spreadsheet", executionTimeout=1800)


# --- 03 lead follow-up ---------------------------------------------------------------------------
def lead() -> dict[str, Any]:
    key = "lead-follow-up"
    lead_ = "$('Validate lead').first().json"
    crm = "$('Classify upsert').first().json.outcome"
    f = Flow("lead")
    f.note(
        "Note: intake",
        (-40, -40),
        (1560, 200),
        "## 03 Lead follow-up\nA form submission needs consent and a valid email; a filled "
        "honeypot marks a bot. The **submission_id** is claimed in the ledger, so a double "
        "submit or a form-tool retry starts one sequence only.",
    )
    entry(
        f,
        path="lead-submissions",
        validator="validate_lead.js",
        validate_name="Validate lead",
        workflow_key=key,
        setting_names=["chat_url", "crm_api_url", "mail_from", "follow_up_delay_s", "max_attempts", "retry_base_s"],
    )
    f.note(
        "Note: crm",
        (1520, -40),
        (1100, 200),
        "## CRM upsert\nThe contact is found by email or created, never duplicated. 429/5xx are "
        "retried with backoff. A contact that is already in a sequence is not mailed again.",
    )
    http(
        f,
        "Upsert contact",
        (1540, 280),
        "POST",
        f"={{{{ {SETTINGS}.crm_api_url }}}}/v1/contacts:upsert",
        (
            f"={{{{ JSON.stringify({{ email: {lead_}.lead.email, name: {lead_}.lead.name, "
            f"company: {lead_}.lead.company, source: {lead_}.lead.source, "
            f"message: {lead_}.lead.message, submission_id: {lead_}.key }}) }}}}"
        ),
        headers={"Idempotency-Key": f"={{{{ {lead_}.key }}}}"},
        classify=True,
    )
    route, _ = retry_loop(f, call="Upsert contact", step="upsert", x=1760, y=280)
    f.chain("Accept 202", "Upsert contact")
    if_node(
        f,
        "New to the sequence?",
        (2420, 200),
        f"={{{{ {crm}.created === true || {crm}.stage === 'new' }}}}",
        TRUE,
    )
    f.link(route, "New to the sequence?", output=0)
    set_outcome(
        f,
        "Already in sequence",
        (2640, 420),
        "skipped",
        f"={{{{ {{ reason: 'contact already in a sequence', contact_id: {crm}.id, stage: {crm}.stage }} }}}}",
    )
    f.note(
        "Note: sequence",
        (2620, -40),
        (1980, 200),
        "## Sequence\nFirst email at once, then a timed wait (3 days by default; the run is "
        "stored, not held in memory). If the CRM shows a reply by then, the sequence stops; "
        "otherwise one follow-up goes out. Each step updates the CRM stage.",
    )
    mail_params = {
        "fromEmail": f"={{{{ {SETTINGS}.mail_from }}}}",
        "toEmail": f"={{{{ {lead_}.lead.email }}}}",
        "emailFormat": "text",
        "options": {"appendAttribution": False},
    }
    f.add(
        "Send first email",
        "emailSend",
        2.1,
        (2640, 200),
        {
            **mail_params,
            "subject": "Thanks for getting in touch",
            "text": (
                f"=Hi {{{{ {lead_}.lead.first_name }}}},\n\nThanks for your message. We have it "
                "and will reply within one working day.\n\nReference: "
                f"{{{{ {lead_}.key }}}}"
            ),
        },
        credentials={"smtp": SMTP},
        retryOnFail=True,
        maxTries=3,
        waitBetweenTries=2000,
    )
    stage_body = "={{ JSON.stringify({ stage: '%s' }) }}"
    contact_url = f"={{{{ {SETTINGS}.crm_api_url }}}}/v1/contacts/{{{{ {crm}.id }}}}"
    http(f, "Mark contacted", (2860, 200), "PATCH", contact_url, stage_body % "contacted", retries=3)
    wait_seconds(f, "Wait before follow-up", (3080, 200), f"={{{{ {SETTINGS}.follow_up_delay_s }}}}")
    http(f, "Check for reply", (3300, 200), "GET", contact_url, retries=3)
    if_node(f, "Replied?", (3520, 200), "={{ $json.stage === 'replied' }}", TRUE)
    set_outcome(
        f,
        "Stop: replied",
        (3740, 60),
        "done",
        f"={{{{ {{ contact_id: {crm}.id, emails_sent: 1, replied: true }} }}}}",
    )
    f.add(
        "Send follow-up",
        "emailSend",
        2.1,
        (3740, 300),
        {
            **mail_params,
            "subject": "Following up on your enquiry",
            "text": (
                f"=Hi {{{{ {lead_}.lead.first_name }}}},\n\nJust checking whether you had a "
                "chance to read our reply. Answer this email and we will pick it up.\n\n"
                f"Reference: {{{{ {lead_}.key }}}}"
            ),
        },
        credentials={"smtp": SMTP},
        retryOnFail=True,
        maxTries=3,
        waitBetweenTries=2000,
    )
    http(f, "Mark followed up", (3960, 300), "PATCH", contact_url, stage_body % "follow_up_sent", retries=3)
    set_outcome(
        f,
        "Sequence complete",
        (4180, 300),
        "done",
        f"={{{{ {{ contact_id: {crm}.id, emails_sent: 2, replied: false }} }}}}",
    )
    f.link("New to the sequence?", "Send first email", output=0)
    f.link("New to the sequence?", "Already in sequence", output=1)
    f.chain("Send first email", "Mark contacted", "Wait before follow-up", "Check for reply", "Replied?")
    f.link("Replied?", "Stop: replied", output=0)
    f.link("Replied?", "Send follow-up", output=1)
    f.chain("Send follow-up", "Mark followed up", "Sequence complete")
    for done in ("Already in sequence", "Stop: replied", "Sequence complete"):
        f.link(done, "Record outcome")
    finish(f, key, f"{lead_}.key", 4400, 200)
    return workflow(f, LEAD_ID, "03 Lead follow-up sequence")


BUILDERS = {
    "00-error-handler.json": error_handler,
    "01-social-post-approval.json": social,
    "02-email-to-sheet.json": email,
    "03-lead-follow-up.json": lead,
}


def render(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail if a committed export is stale")
    args = parser.parse_args()
    stale = []
    for name, build in BUILDERS.items():
        text = render(build())
        path = OUT / name
        if args.check:
            if not path.exists() or path.read_text(encoding="utf-8") != text:
                stale.append(name)
        else:
            OUT.mkdir(exist_ok=True)
            path.write_bytes(text.encode("utf-8"))
    if stale:
        print("stale exports (run scripts/build_workflows.py):", ", ".join(stale), file=sys.stderr)
        return 1
    print("checked" if args.check else "built", len(BUILDERS), "workflows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
