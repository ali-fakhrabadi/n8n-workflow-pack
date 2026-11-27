#!/usr/bin/env python3
"""Write the recorded synthetic events (tests/events/*.jsonl) the harness replays.

All people, companies and addresses are invented (example.com / .example domains). Each event
carries its delivery plan and the expected result; tests/harness/replay.py documents the
fields. Keys are unique per event so events can run in parallel.

    python3 tests/events/generate.py [--check]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent


def social() -> list[dict[str, Any]]:
    def post(rid: str, **extra: Any) -> dict[str, Any]:
        return {
            "request_id": rid,
            "channel": "linkedin",
            "requested_by": "marketing@northwind.example",
            "text": "We are hiring two automation engineers in Lisbon. Remote-friendly, see the link in our profile.",
            **extra,
        }

    ok = {"responses": {"202": 1}, "end": "run_finished", "approval_requests": 1}
    invalid = {"responses": {"400": 1}, "end": "none"}
    return [
        {
            "id": "S01",
            "title": "LinkedIn post approved and published",
            "payload": post("soc-001"),
            "approval": {"decision": "approve", "by": "editor@northwind.example"},
            "expect": {
                **ok,
                "run_status": "done",
                "writes": 1,
                "outcome": {"published": True, "decided_by": "editor@northwind.example"},
            },
        },
        {
            "id": "S02",
            "title": "X post with image approved",
            "payload": post(
                "soc-002",
                channel="x",
                text="Spring release is live: faster sync, offline mode. Changelog on the blog.",
                media_url="https://cdn.northwind.example/release.png",
            ),
            "approval": {"decision": "approve"},
            "expect": {**ok, "run_status": "done", "writes": 1},
        },
        {
            "id": "S03",
            "title": "Mastodon post with emoji counted by code point",
            "payload": post("soc-003", channel="mastodon", text="Thank you for 10k downloads! 🎉🚀 " * 10),
            "approval": {"decision": "approve"},
            "expect": {**ok, "run_status": "done", "writes": 1},
        },
        {
            "id": "S04",
            "title": "Reviewer rejects: nothing published",
            "payload": post("soc-004"),
            "approval": {"decision": "reject", "by": "legal@northwind.example"},
            "expect": {
                **ok,
                "run_status": "rejected",
                "writes": 0,
                "outcome": {"decision": "reject", "published": False},
            },
        },
        {
            "id": "S05",
            "title": "No decision before the time limit: request expires",
            "payload": post("soc-005"),
            "approval": {"decision": "none"},
            "expect": {**ok, "run_status": "expired", "writes": 0, "outcome": {"decision": "expired"}},
        },
        {
            "id": "S06",
            "title": "Invalid: 281 characters for X",
            "payload": post("soc-006", channel="x", text="a" * 281),
            "expect": invalid,
        },
        {
            "id": "S07",
            "title": "Invalid: unknown channel",
            "payload": post("soc-007", channel="myspace"),
            "expect": invalid,
        },
        {"id": "S08", "title": "Invalid: empty text", "payload": post("soc-008", text="   \n "), "expect": invalid},
        {
            "id": "S09",
            "title": "Invalid: missing request_id",
            "payload": {k: v for k, v in post("x").items() if k != "request_id"},
            "expect": invalid,
        },
        {
            "id": "S10",
            "title": "Invalid: media URL over plain http",
            "payload": post("soc-010", media_url="http://cdn.northwind.example/a.png"),
            "expect": invalid,
        },
        {
            "id": "S11",
            "title": "Invalid: control character in text",
            "payload": post("soc-011", text="Hello\u0007world"),
            "expect": invalid,
        },
        {
            "id": "S12",
            "title": "Wrong webhook token",
            "auth": "wrong",
            "payload": post("soc-012"),
            "expect": {"responses": {"403": 1}, "end": "none"},
        },
        {
            "id": "S13",
            "title": "No webhook token",
            "auth": "missing",
            "payload": post("soc-013"),
            "expect": {"responses": {"403": 1}, "end": "none"},
        },
        {
            "id": "S14",
            "title": "Same request sent 3 times in a row",
            "payload": post("soc-014"),
            "copies": 3,
            "approval": {"decision": "approve"},
            "expect": {
                "responses": {"200": 2, "202": 1},
                "end": "run_finished",
                "approval_requests": 1,
                "run_status": "done",
                "writes": 1,
            },
        },
        {
            "id": "S15",
            "title": "Same request sent 10 times at the same moment",
            "payload": post("soc-015"),
            "copies": 10,
            "concurrent": True,
            "approval": {"decision": "approve"},
            "expect": {
                "responses": {"200": 9, "202": 1},
                "end": "run_finished",
                "approval_requests": 1,
                "run_status": "done",
                "writes": 1,
            },
        },
        {
            "id": "S16",
            "title": "Social API 503 twice, then published",
            "payload": post("soc-016"),
            "script": [{"step": "social.publish", "responses": [{"status": 503}, {"status": 503}]}],
            "approval": {"decision": "approve"},
            "expect": {**ok, "run_status": "done", "writes": 1, "write_attempts": 3, "outcome": {"attempts": 3}},
        },
        {
            "id": "S17",
            "title": "Social API 429 with Retry-After, then published",
            "payload": post("soc-017"),
            "script": [{"step": "social.publish", "responses": [{"status": 429, "headers": {"Retry-After": "2"}}]}],
            "approval": {"decision": "approve"},
            "expect": {**ok, "run_status": "done", "writes": 1, "write_attempts": 2},
        },
        {
            "id": "S18",
            "title": "Permanent 422: alert, key released, redelivery publishes once",
            "payload": post("soc-018"),
            "script": [
                {
                    "step": "social.publish",
                    "responses": [{"status": 422, "body": {"error": {"message": "duplicate content"}}}],
                }
            ],
            "approval": {"decision": "approve"},
            "redeliver_after_error": True,
            "expect": {
                "responses": {"202": 1},
                "end": "workflow_error",
                "approval_requests": 1,
                "alert_node": "Fail run (publish)",
                "redelivery_response": 202,
                "approval_requests_after_redelivery": 2,
                "redelivery_status": "done",
                "writes": 1,
                "ledger": "done",
                "ledger_attempts": 2,
            },
        },
        {
            "id": "S19",
            "title": "Social API down (503 on every attempt): alert after 4 attempts",
            "payload": post("soc-019"),
            "script": [{"step": "social.publish", "responses": [{"status": 503}] * 4}],
            "approval": {"decision": "approve"},
            "expect": {
                "responses": {"202": 1},
                "end": "workflow_error",
                "approval_requests": 1,
                "alert_node": "Fail run (publish)",
                "writes": 0,
                "write_attempts": 4,
                "ledger": "failed",
            },
        },
        {
            "id": "S20",
            "title": "Approve link clicked twice: published once",
            "payload": post("soc-020"),
            "approval": {"decision": "approve", "clicks": 2},
            "expect": {**ok, "run_status": "done", "writes": 1, "clicks_after_first_rejected": True},
        },
    ]


def email() -> list[dict[str, Any]]:
    def mail(mid: str, text: str, **extra: Any) -> dict[str, Any]:
        return {
            "message_id": f"<{mid}@mail.harbor-supplies.example>",
            "from": "Harbor Supplies Billing <billing@harbor-supplies.example>",
            "to": "invoices@northwind.example",
            "subject": "Your invoice",
            "date": "Tue, 24 Nov 2026 09:15:00 +0000",
            "text": text,
            **extra,
        }

    full = (
        "Invoice number: HS-{n}\nPO number: PO-5521\nTotal due: $1,250.00\nDue date: 2026-12-24\n\n"
        "Thank you for your business."
    )
    ok = {"responses": {"202": 1}, "end": "run_finished", "run_status": "done", "writes": 1}
    invalid = {"responses": {"400": 1}, "end": "none"}
    return [
        {
            "id": "E01",
            "smoke": True,
            "title": "Complete invoice email: row on the Invoices tab",
            "payload": mail("e001", full.format(n=1001)),
            "expect": {**ok, "tab": "Invoices", "row_status": "ok"},
        },
        {
            "id": "E02",
            "title": "European amount and written date",
            "payload": mail("e002", "Invoice no. HS-1002\nAmount due: EUR 1.234,50\nDue date: 15 Jan 2027"),
            "expect": {**ok, "tab": "Invoices", "row_status": "ok"},
        },
        {
            "id": "E03",
            "title": "HTML-only email",
            "payload": mail(
                "e003", "", html="<p>Invoice number: HS-1003</p><p>Total: £99.00</p><p>Due date: January 5, 2027</p>"
            ),
            "expect": {**ok, "tab": "Invoices", "row_status": "ok"},
        },
        {
            "id": "E04",
            "title": "Amount missing: row on the Review tab",
            "payload": mail("e004", "Invoice number: HS-1004\nDue date: 2026-12-24"),
            "expect": {**ok, "tab": "Review", "row_status": "needs_review"},
        },
        {
            "id": "E05",
            "title": "Ambiguous date 03/04/2027: Review tab",
            "payload": mail("e005", "Invoice number: HS-1005\nTotal: 410.00 USD\nDue date: 03/04/2027"),
            "expect": {**ok, "tab": "Review", "row_status": "needs_review"},
        },
        {
            "id": "E06",
            "title": "Forwarded invoice",
            "payload": mail(
                "e006",
                "---------- Forwarded message ---------\n"
                "Invoice #: HS-1006\nTotal due: 18 900,00 SEK\nDue date: 31/12/2026",
                subject="Fwd: Invoice HS-1006",
            ),
            "expect": {**ok, "tab": "Invoices", "row_status": "ok"},
        },
        {
            "id": "E07",
            "title": "Invalid: no Message-ID",
            "payload": mail("x", full.format(n=1007), message_id=""),
            "expect": invalid,
        },
        {
            "id": "E08",
            "title": "Invalid: sender is not an address",
            "payload": mail("e008", full.format(n=1008), **{"from": "Billing team"}),
            "expect": invalid,
        },
        {"id": "E09", "title": "Invalid: empty body", "payload": mail("e009", "  "), "expect": invalid},
        {
            "id": "E10",
            "title": "Invalid: body over 100,000 characters",
            "payload": mail("e010", "x" * 100001),
            "expect": invalid,
        },
        {
            "id": "E11",
            "title": "Wrong webhook token",
            "auth": "wrong",
            "payload": mail("e011", full.format(n=1011)),
            "expect": {"responses": {"403": 1}, "end": "none"},
        },
        {
            "id": "E12",
            "title": "Provider retries the same email 3 times",
            "payload": mail("e012", full.format(n=1012)),
            "copies": 3,
            "expect": {**ok, "responses": {"200": 2, "202": 1}, "tab": "Invoices"},
        },
        {
            "id": "E13",
            "title": "Same email delivered 10 times at the same moment",
            "payload": mail("e013", full.format(n=1013)),
            "copies": 10,
            "concurrent": True,
            "expect": {**ok, "responses": {"200": 9, "202": 1}, "tab": "Invoices"},
        },
        {
            "id": "E14",
            "title": "Message-ID spelled three ways is one email",
            "payload": mail("e014", full.format(n=1014)),
            "copies": 3,
            "variants": [
                mail("e014", full.format(n=1014)),
                mail("E014", full.format(n=1014)),
                {**mail("e014", full.format(n=1014)), "message_id": "  e014@MAIL.harbor-supplies.example "},
            ],
            "expect": {**ok, "responses": {"200": 2, "202": 1}},
        },
        {
            "id": "E15",
            "title": "Sheets API 429, then appended",
            "payload": mail("e015", full.format(n=1015)),
            "script": [{"step": "sheets.append", "responses": [{"status": 429, "headers": {"Retry-After": "1"}}]}],
            "expect": {**ok, "write_attempts": 2},
        },
        {
            "id": "E16",
            "title": "Sheets API 500 on every attempt: alert, redelivery appends once",
            "payload": mail("e016", full.format(n=1016)),
            "script": [{"step": "sheets.append", "responses": [{"status": 500}] * 4}],
            "redeliver_after_error": True,
            "expect": {
                "responses": {"202": 1},
                "end": "workflow_error",
                "alert_node": "Fail run (append)",
                "redelivery_response": 202,
                "redelivery_status": "done",
                "writes": 1,
                "write_attempts": 5,
                "ledger": "done",
                "ledger_attempts": 2,
            },
        },
        {
            "id": "E17",
            "title": "Sheets API 403 (no access): alert at once, no retry",
            "payload": mail("e017", full.format(n=1017)),
            "script": [
                {
                    "step": "sheets.append",
                    "responses": [
                        {"status": 403, "body": {"error": {"message": "The caller does not have permission"}}}
                    ],
                }
            ],
            "expect": {
                "responses": {"202": 1},
                "end": "workflow_error",
                "alert_node": "Fail run (append)",
                "writes": 0,
                "write_attempts": 1,
                "ledger": "failed",
            },
        },
    ]


def lead() -> list[dict[str, Any]]:
    def form(sid: str, email: str, **extra: Any) -> dict[str, Any]:
        # Name and company follow the invented address: robin.avery@avery-tools.example.
        local, _, domain = email.strip().lower().partition("@")
        return {
            "submission_id": sid,
            "name": " ".join(part.capitalize() for part in local.split(".")),
            "email": email,
            "company": " ".join(part.capitalize() for part in domain.split(".")[0].split("-")),
            "message": "We need a quote for 40 workstations.",
            "consent": True,
            "source": "pricing-page",
            **extra,
        }

    ok = {"responses": {"202": 1}, "end": "run_finished", "run_status": "done", "writes": 1, "crm_contacts": 1}
    invalid = {"responses": {"400": 1}, "end": "none"}
    return [
        {
            "id": "L01",
            "title": "New lead, no reply: first email, wait, follow-up",
            "payload": form("lead-001", "robin.avery@avery-tools.example"),
            "expect": {**ok, "emails": 2, "outcome": {"emails_sent": 2, "replied": False}},
        },
        {
            "id": "L02",
            "title": "Lead replies during the wait: no follow-up",
            "payload": form("lead-002", "sam.okafor@okafor-labs.example", name="Sam Okafor"),
            "reply": True,
            "expect": {**ok, "emails": 1, "outcome": {"emails_sent": 1, "replied": True}},
        },
        {
            "id": "L03",
            "title": "Invalid: email without a domain",
            "payload": form("lead-003", "kim@localhost"),
            "expect": invalid,
        },
        {
            "id": "L04",
            "title": "Invalid: no consent",
            "payload": form("lead-004", "kim.park@park.example", consent=False),
            "expect": invalid,
        },
        {
            "id": "L05",
            "title": "Invalid: empty name",
            "payload": form("lead-005", "lee.chan@chan.example", name="  "),
            "expect": invalid,
        },
        {
            "id": "L06",
            "title": "Bot: honeypot field filled",
            "payload": form("lead-006", "bot@spam.example", website="http://spam.example"),
            "expect": invalid,
        },
        {
            "id": "L07",
            "title": "Wrong webhook token",
            "auth": "wrong",
            "payload": form("lead-007", "ana.ruiz@ruiz.example"),
            "expect": {"responses": {"403": 1}, "end": "none"},
        },
        {
            "id": "L08",
            "title": "Form tool retries 3 times",
            "payload": form("lead-008", "jo.becker@becker.example"),
            "copies": 3,
            "expect": {**ok, "responses": {"200": 2, "202": 1}, "emails": 2},
        },
        {
            "id": "L09",
            "title": "Double-click submit: 10 copies at the same moment",
            "payload": form("lead-009", "mia.novak@novak.example"),
            "copies": 10,
            "concurrent": True,
            "expect": {**ok, "responses": {"200": 9, "202": 1}, "emails": 2},
        },
        {
            "id": "L10",
            "title": "Same person submits again during the sequence: no second sequence",
            "payload": form("lead-010", "ola.berg@berg.example"),
            "second": {"payload": form("lead-010b", "  Ola.Berg@BERG.example ")},
            "expect": {**ok, "emails": 2, "second_response": 202, "second_status": "skipped"},
        },
        {
            "id": "L11",
            "title": "CRM 503, then upserted",
            "payload": form("lead-011", "eva.lind@lind.example"),
            "script": [{"step": "crm.upsert", "responses": [{"status": 503}]}],
            "expect": {**ok, "emails": 2, "write_attempts": 2},
        },
        {
            "id": "L12",
            "title": "CRM 422: alert, redelivery runs the sequence once",
            "payload": form("lead-012", "noah.weber@weber.example"),
            "script": [
                {
                    "step": "crm.upsert",
                    "responses": [{"status": 422, "body": {"error": {"message": "field company too long"}}}],
                }
            ],
            "redeliver_after_error": True,
            "expect": {
                "responses": {"202": 1},
                "end": "workflow_error",
                "alert_node": "Fail run (upsert)",
                "redelivery_response": 202,
                "redelivery_status": "done",
                "writes": 1,
                "emails": 2,
                "crm_contacts": 1,
                "ledger": "done",
                "ledger_attempts": 2,
            },
        },
        {
            "id": "L13",
            "title": "Email typed with spaces and capitals",
            "payload": form("lead-013", "  Lena.Fischer@Fischer.EXAMPLE "),
            "expect": {**ok, "emails": 2},
        },
    ]


def files() -> dict[str, str]:
    out = {}
    for name, build in (("social", social), ("email", email), ("lead", lead)):
        lines = [json.dumps({"workflow": name, **e}, ensure_ascii=False) for e in build()]
        out[f"{name}.jsonl"] = "\n".join(lines) + "\n"
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    stale = []
    keys: set[str] = set()
    for name, text in files().items():
        for line in text.splitlines():
            event = json.loads(line)
            for p in [
                event["payload"],
                *(event.get("variants") or [])[:1],
                (event.get("second") or {}).get("payload") or {},
            ]:
                k = (
                    str(p.get("request_id") or p.get("message_id") or p.get("submission_id") or "")
                    .strip()
                    .strip("<>")
                    .lower()
                )
                if k and k in keys and p is event["payload"]:
                    raise SystemExit(f"duplicate key {k} in {event['id']}")
                keys.add(k)
        path = HERE / name
        if args.check:
            if not path.exists() or path.read_text(encoding="utf-8") != text:
                stale.append(name)
        else:
            path.write_bytes(text.encode("utf-8"))
    if stale:
        print("stale event files:", ", ".join(stale), file=sys.stderr)
        return 1
    print("checked" if args.check else "wrote", sum(t.count("\n") for t in files().values()), "events")
    return 0


if __name__ == "__main__":
    sys.exit(main())
