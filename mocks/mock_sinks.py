"""Mock downstream services for the workflows: chat, social network, spreadsheet and CRM.

The harness points the workflows here instead of real accounts. Each mock answers in the
shape of the real API it stands for (a chat incoming webhook, a social "create post" call,
the Google Sheets ``values:append`` call, a CRM contact upsert), checks the bearer token and
records every call. The mocks are deliberately not idempotent: every append adds a row and
every publish adds a post, exactly like the real APIs, so a duplicate run shows up as an extra
row or post. The CRM upserts by email, as CRMs do.

A test can script the next answers for a step and key (status, body, headers, delay) to
simulate rate limits, outages and permanent errors. ``GET /_mock/view`` renders the recorded
state as a page, for screenshots.

Standard library only.
"""

from __future__ import annotations

import html
import json
import os
import re
import threading
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

TOKEN = Path(os.environ.get("MOCK_TOKEN_FILE", "/run/secrets/downstream_token")).read_text().strip()
LOCK = threading.Lock()
CALLS: list[dict[str, Any]] = []
SCRIPTS: dict[tuple[str, str], list[dict[str, Any]]] = {}
MESSAGES: list[dict[str, Any]] = []
POSTS: list[dict[str, Any]] = []
ROWS: dict[str, list[list[Any]]] = {}
CONTACTS: dict[str, dict[str, Any]] = {}

ROUTES = [
    ("POST", re.compile(r"^/chat/messages$"), "chat"),
    ("POST", re.compile(r"^/social/v1/posts$"), "social.publish"),
    ("POST", re.compile(r"^/sheets/v4/spreadsheets/([^/]+)/values/(.+):append$"), "sheets.append"),
    ("POST", re.compile(r"^/crm/v1/contacts:upsert$"), "crm.upsert"),
    ("GET", re.compile(r"^/crm/v1/contacts/([0-9a-f-]{36})$"), "crm.get"),
    ("PATCH", re.compile(r"^/crm/v1/contacts/([0-9a-f-]{36})$"), "crm.patch"),
]
STAGES = {"new", "contacted", "replied", "follow_up_sent"}


def contact_by_id(contact_id: str) -> dict[str, Any] | None:
    return next((c for c in CONTACTS.values() if c["id"] == contact_id), None)


def default(step: str, groups: tuple[str, ...], payload: Any, key: str) -> tuple[int, Any]:
    """What the real service would answer (and the state change it would make)."""
    body = payload if isinstance(payload, dict) else {}
    if step == "chat":
        MESSAGES.append({"ts": time.time(), **body})
        return 200, {"ok": True}
    if step == "social.publish":
        if not body.get("text") or body.get("channel") not in {"x", "mastodon", "linkedin", "facebook"}:
            return 422, {"error": {"code": "invalid_post", "message": "channel and text are required"}}
        post_id = f"post-{len(POSTS) + 1:04d}"
        POSTS.append({"id": post_id, "key": key, "channel": body["channel"], "text": body["text"], "ts": time.time()})
        return 201, {
            "id": post_id,
            "url": f"https://social.example/{body['channel']}/{post_id}",
            "channel": body["channel"],
        }
    if step == "sheets.append":
        tab = urllib.parse.unquote(groups[1]).split("!", 1)[0]
        values = body.get("values")
        if not isinstance(values, list) or not values or not all(isinstance(r, list) for r in values):
            return 400, {"error": {"code": 400, "message": "values must be a list of rows"}}
        rows = ROWS.setdefault(tab, [])
        start = len(rows) + 2  # row 1 holds the headers
        rows.extend(values)
        end = len(rows) + 1
        return 200, {
            "spreadsheetId": groups[0],
            "tableRange": f"{tab}!A1:K{start - 1}",
            "updates": {"updatedRange": f"{tab}!A{start}:K{end}", "updatedRows": len(values)},
        }
    if step == "crm.upsert":
        email = str(body.get("email") or "").strip().lower()
        if "@" not in email:
            return 422, {"error": {"code": "invalid_email", "message": "email is required"}}
        existing = CONTACTS.get(email)
        if existing:
            existing["updated"] = time.time()
            existing["submissions"].append(body.get("submission_id"))
            return 200, {**public(existing), "created": False}
        contact = {
            "id": str(uuid.uuid4()),
            "email": email,
            "name": body.get("name"),
            "company": body.get("company"),
            "stage": "new",
            "submissions": [body.get("submission_id")],
            "created": time.time(),
            "updated": time.time(),
        }
        CONTACTS[email] = contact
        return 201, {**public(contact), "created": True}
    if step in ("crm.get", "crm.patch"):
        contact = contact_by_id(groups[0])
        if not contact:
            return 404, {"error": {"code": "not_found"}}
        if step == "crm.patch":
            stage = body.get("stage")
            if stage not in STAGES:
                return 422, {"error": {"code": "invalid_stage"}}
            contact["stage"] = stage
            contact["updated"] = time.time()
        return 200, public(contact)
    return 404, {"error": {"code": "not_found"}}


def public(contact: dict[str, Any]) -> dict[str, Any]:
    return {k: contact[k] for k in ("id", "email", "name", "company", "stage")}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: ARG002 - quiet
        return

    def reply(self, status: int, body: Any, headers: dict[str, str] | None = None, *, page: bool = False) -> None:
        data = body.encode() if page else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8" if page else "application/json")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, str(v))
        self.end_headers()
        self.wfile.write(data)

    def body(self) -> Any:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            return json.loads(raw) if raw else None
        except json.JSONDecodeError:
            return raw.decode(errors="replace")

    def handle_any(self, method: str) -> None:
        payload = self.body()
        path = self.path.split("?", 1)[0]
        if path.startswith("/_mock/"):
            return self.admin(method, path, payload)
        if self.headers.get("Authorization") != f"Bearer {TOKEN}":
            with LOCK:
                CALLS.append({"step": "unauthorized", "path": path, "ts": time.time()})
            return self.reply(401, {"error": {"code": "unauthorized"}})
        for m, pattern, step in ROUTES:
            match = pattern.match(path)
            if not (m == method and match):
                continue
            groups = match.groups()
            key = self.headers.get("Idempotency-Key") or (
                str(payload.get("key"))
                if isinstance(payload, dict) and payload.get("key")
                else (groups[0] if groups else "-")
            )
            with LOCK:
                queue = SCRIPTS.get((step, key)) or SCRIPTS.get((step, "*"))
                scripted = queue.pop(0) if queue else None
            if scripted and scripted.get("delay_s"):
                time.sleep(float(scripted["delay_s"]))
            with LOCK:
                if scripted and "status" in scripted:
                    status, body, headers = (
                        scripted["status"],
                        scripted.get("body") or {},
                        scripted.get("headers") or {},
                    )
                else:
                    (status, body), headers = default(step, groups, payload, key), {}
                CALLS.append(
                    {
                        "step": step,
                        "key": key,
                        "status": status,
                        "scripted": scripted is not None,
                        "kind": payload.get("kind") if isinstance(payload, dict) else None,
                        "body": payload,
                        "ts": time.time(),
                    }
                )
            return self.reply(status, body, headers)
        return self.reply(404, {"error": {"code": "not_found"}})

    def admin(self, method: str, path: str, payload: Any) -> None:
        with LOCK:
            if path == "/_mock/reset" and method == "POST":
                for store in (CALLS, SCRIPTS, MESSAGES, POSTS, ROWS, CONTACTS):
                    store.clear()
                return self.reply(200, {"ok": True})
            if path == "/_mock/script" and method == "POST":
                SCRIPTS[(payload["step"], payload.get("key", "*"))] = list(payload["responses"])
                return self.reply(200, {"ok": True})
            if path == "/_mock/calls" and method == "GET":
                return self.reply(200, {"calls": CALLS})
            if path == "/_mock/state" and method == "GET":
                return self.reply(200, {"messages": MESSAGES, "posts": POSTS, "rows": ROWS, "contacts": CONTACTS})
            if path == "/_mock/contact-stage" and method == "POST":
                contact = CONTACTS.get(str(payload.get("email", "")).lower())
                if not contact or payload.get("stage") not in STAGES:
                    return self.reply(404, {"error": {"code": "not_found"}})
                contact["stage"] = payload["stage"]
                return self.reply(200, public(contact))
            if path == "/_mock/view" and method == "GET":
                return self.reply(200, view(), page=True)
        return self.reply(404, {"error": {"code": "not_found"}})

    def do_GET(self) -> None:
        self.handle_any("GET")

    def do_POST(self) -> None:
        self.handle_any("POST")

    def do_PATCH(self) -> None:
        self.handle_any("PATCH")


def view() -> str:
    """The recorded state as one page: chat channel, published posts, sheet tabs, CRM."""
    e = html.escape

    def table(head: list[str], rows: list[list[Any]]) -> str:
        cells = "".join(f"<th>{e(h)}</th>" for h in head)
        body = "".join("<tr>" + "".join(f"<td>{e(str(c))}</td>" for c in r) + "</tr>" for r in rows)
        return f"<table><tr>{cells}</tr>{body or '<tr><td colspan=99>(empty)</td></tr>'}</table>"

    def clock(ts: float) -> str:
        return time.strftime("%H:%M:%S", time.gmtime(ts))

    chat = [
        [
            clock(m["ts"]),
            m.get("kind", ""),
            m.get("workflow") or m.get("ledger_workflow") or "",
            m.get("key") or "",
            m.get("status") or m.get("message") or (m.get("text") or "")[:90],
        ]
        for m in MESSAGES[-12:]
    ]
    posts = [[clock(p["ts"]), p["id"], p["channel"], p["key"], p["text"][:90]] for p in POSTS]
    sheet_head = [
        "message_id",
        "received_at",
        "from",
        "vendor",
        "invoice",
        "po",
        "amount",
        "currency",
        "due",
        "status",
        "issues",
    ]
    tabs = "".join(f"<h3>Sheet tab: {e(tab)}</h3>" + table(sheet_head, rows) for tab, rows in sorted(ROWS.items()))
    crm = [[c["email"], c["name"], c["company"] or "", c["stage"], len(c["submissions"])] for c in CONTACTS.values()]
    return (
        "<!doctype html><meta charset=utf-8><title>Mock sinks</title><style>"
        "body{font:14px system-ui,sans-serif;margin:24px;color:#1f2933;background:#f7f9fb}"
        "table{border-collapse:collapse;width:100%;margin-bottom:18px;background:#fff}"
        "td,th{border:1px solid #d9e2ec;padding:5px 8px;text-align:left;vertical-align:top}"
        "th{background:#e9eff5}h2{margin-top:28px}</style>"
        "<h1>Mock downstream services</h1><p>Recorded by the test harness; no real accounts.</p>"
        "<h2>Chat channel (last 12 messages)</h2>"
        + table(["time (UTC)", "kind", "workflow", "key", "detail"], chat)
        + "<h2>Social posts published</h2>"
        + table(["time (UTC)", "post", "channel", "idempotency key", "text"], posts)
        + "<h2>Spreadsheet</h2>"
        + (tabs or "<p>(no rows)</p>")
        + "<h2>CRM contacts</h2>"
        + table(["email", "name", "company", "stage", "submissions"], crm)
    )


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
