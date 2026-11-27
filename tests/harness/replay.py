"""Replay the recorded events against the disposable n8n and check what the workflows did.

Each line of tests/events/*.jsonl is one recorded, synthetic event with its delivery plan
(copies, at once or one after another, which webhook token), scripted failures of the mock
services, the reviewer's or lead's action, and the expected result. For every event the
harness checks:

- the HTTP answers n8n gave (202 accepted, 200 duplicate, 400 invalid, 403 bad token),
- how the run ended (run notice or error-workflow alert in the chat mock),
- the writes the mocks received for the event's key (posts, rows, CRM contacts, emails),
- n8n's own record of the execution (success or error),
- and, in a second pass (--verify-ledger), the final ledger row in PostgreSQL.

    tests/harness/run.sh      # starts the stack, runs this twice, removes the stack

Standard library only. Writes /out/replay-results.json, then /out/harness-report.json.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

N8N = os.environ.get("HARNESS_N8N_URL", "http://n8n:5678")
MOCKS = os.environ.get("HARNESS_MOCKS_URL", "http://mock-sinks:8000")
MAILPIT = os.environ.get("HARNESS_MAILPIT_URL", "http://mailpit:8025")
OWNER = os.environ.get("HARNESS_OWNER_EMAIL", "owner@example.com")
OUT = Path(os.environ.get("HARNESS_OUT", "/out"))
EVENTS = Path(os.environ.get("HARNESS_EVENTS", "/events"))
ONLY = {w for w in os.environ.get("HARNESS_ONLY", "").split(",") if w}
APPROVAL_TIMEOUT_S = float(os.environ.get("HARNESS_APPROVAL_TIMEOUT_S", "25"))
FOLLOW_UP_DELAY_S = float(os.environ.get("HARNESS_FOLLOW_UP_DELAY_S", "12"))
EVENT_TIMEOUT_S = 90 + APPROVAL_TIMEOUT_S + FOLLOW_UP_DELAY_S
QUIET_S = 4  # how long an event that must not run is watched for side effects
BROWSER_ID = str(uuid.uuid4())
PATHS = {"social": "social-post-requests", "email": "inbound-email", "lead": "lead-submissions"}
WRITE_STEP = {"social": "social.publish", "email": "sheets.append", "lead": "crm.upsert"}
LEDGER_NAME = {"social": "social-post-approval", "email": "email-to-sheet", "lead": "lead-follow-up"}
COUNT_LOCK = threading.Lock()
COUNTS = {"webhook_deliveries": 0, "approval_clicks": 0}


def secret(name: str) -> str:
    return Path(f"/run/secrets/{name}").read_text(encoding="utf-8").strip()


def http(method: str, url: str, body: Any = None, headers: dict[str, str] | None = None) -> tuple[int, Any, dict[str, str]]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(  # noqa: S310 - fixed harness URLs on an internal network
        url, data=data, method=method, headers={"Content-Type": "application/json", **(headers or {})}
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310
            text, status, hdrs = resp.read().decode(), resp.status, dict(resp.headers.items())
    except urllib.error.HTTPError as exc:
        text, status, hdrs = exc.read().decode(), exc.code, dict(exc.headers.items())
    except (urllib.error.URLError, TimeoutError) as exc:
        return 0, str(exc), {}
    try:
        return status, json.loads(text) if text.strip() else None, hdrs
    except json.JSONDecodeError:
        return status, text, hdrs


def event_key(event: dict[str, Any], payload: dict[str, Any] | None = None) -> str | None:
    p = payload or event["payload"]
    raw = {"social": p.get("request_id"), "email": p.get("message_id"), "lead": p.get("submission_id")}[event["workflow"]]
    if not isinstance(raw, str) or not raw.strip():
        return None
    return raw.strip().strip("<>").lower() if event["workflow"] == "email" else raw.strip()


def deliver(event: dict[str, Any], payload: dict[str, Any]) -> tuple[int, Any]:
    headers = {}
    auth = event.get("auth", "valid")
    if auth == "valid":
        headers["X-Webhook-Token"] = secret("webhook_token")
    elif auth == "wrong":
        headers["X-Webhook-Token"] = "not-the-token"
    with COUNT_LOCK:
        COUNTS["webhook_deliveries"] += 1
    status, body, _ = http("POST", f"{N8N}/webhook/{PATHS[event['workflow']]}", payload, headers)
    return status, body


def mock_calls() -> list[dict[str, Any]]:
    return http("GET", f"{MOCKS}/_mock/calls")[1]["calls"]


def messages(key: str, kind: str | None = None) -> list[dict[str, Any]]:
    return [
        c["body"]
        for c in mock_calls()
        if c["step"] == "chat" and isinstance(c.get("body"), dict) and c["body"].get("key") == key
        and (kind is None or c["body"].get("kind") == kind) and 200 <= c["status"] < 300
    ]


def wait_for(predicate: Any, timeout: float) -> Any:
    deadline = time.time() + timeout
    while time.time() < deadline:
        found = predicate()
        if found:
            return found
        time.sleep(0.5)
    return None


def mails_to(address: str) -> int:
    query = urllib.parse.quote(f'to:"{address}"')
    status, body, _ = http("GET", f"{MAILPIT}/api/v1/search?query={query}")
    return int((body or {}).get("messages_count", 0)) if status == 200 else -1


def script(step: str, key: str, responses: list[dict[str, Any]]) -> None:
    http("POST", f"{MOCKS}/_mock/script", {"step": step, "key": key, "responses": responses})


class N8nApi:
    """n8n's own record of an execution (the editor's REST API, signed in as the owner)."""

    def __init__(self) -> None:
        status, _, headers = http(
            "POST",
            f"{N8N}/rest/login",
            {"emailOrLdapLoginId": OWNER, "password": secret("n8n_owner_password")},
            {"browser-id": BROWSER_ID},
        )
        cookie = headers.get("Set-Cookie") or headers.get("set-cookie") or ""
        self.cookie = cookie.split(";", 1)[0]
        self.ok = status == 200 and self.cookie.startswith("n8n-auth=")

    def status(self, execution_id: str | None) -> str | None:
        if not (self.ok and execution_id):
            return None
        for _ in range(40):
            code, body, _ = http(
                "GET",
                f"{N8N}/rest/executions/{execution_id}",
                headers={"Cookie": self.cookie, "browser-id": BROWSER_ID},
            )
            data = (body or {}).get("data", body) if isinstance(body, dict) else {}
            if code == 200 and data.get("status") not in (None, "running", "new", "waiting"):
                return str(data.get("status"))
            time.sleep(0.5)
        return None


API: N8nApi | None = None


def run_event(event: dict[str, Any]) -> dict[str, Any]:
    """Deliver one recorded event as planned, act for the reviewer or lead, collect evidence."""
    started = time.time()
    key = event_key(event)
    workflow = event["workflow"]
    expect = event["expect"]
    result: dict[str, Any] = {"id": event["id"], "workflow": workflow, "title": event["title"], "key": key}
    for item in event.get("script", []):
        script(item["step"], key or "*", item["responses"])

    copies = int(event.get("copies", 1))
    if event.get("concurrent"):
        barrier = threading.Barrier(copies)

        def one(_: int) -> int:
            barrier.wait()
            return deliver(event, event["payload"])[0]

        with ThreadPoolExecutor(copies) as pool:
            codes = list(pool.map(one, range(copies)))
    else:
        # "variants": the same event as different senders would spell it (one delivery each).
        codes = []
        for payload in event.get("variants") or [event["payload"]] * copies:
            codes.append(deliver(event, payload)[0])
            time.sleep(float(event.get("gap_s", 0.3)))
    result["responses"] = {str(c): codes.count(c) for c in sorted(set(codes))}

    if workflow == "social" and key and expect.get("end") != "none":
        approval = event.get("approval", {"decision": "approve"})
        ask = wait_for(lambda: messages(key, "approval_request"), 60)
        result["approval_requests"] = len(ask or [])
        if ask and approval["decision"] in ("approve", "reject"):
            url = ask[0][f"{approval['decision']}_url"]
            if approval.get("by"):
                url += "&by=" + urllib.parse.quote(approval["by"])
            clicks = []
            for _ in range(int(approval.get("clicks", 1))):
                with COUNT_LOCK:
                    COUNTS["approval_clicks"] += 1
                clicks.append(http("GET", url)[0])
                time.sleep(1.0)
            result["click_responses"] = clicks

    second = event.get("second")
    if second:
        # A second submission for the same person while the first sequence is running.
        wait_for(lambda: mails_to(event["payload"]["email"].strip().lower()) >= 1, 60)
        result["second_response"] = deliver(event, second["payload"])[0]
        second_key = event_key(event, second["payload"])
        end2 = wait_for(lambda: messages(second_key, "run_finished"), EVENT_TIMEOUT_S)
        result["second_status"] = end2[-1]["status"] if end2 else None

    if workflow == "lead" and event.get("reply"):
        # The lead answers after the first email (once the CRM shows "contacted").
        email = event["payload"]["email"].strip().lower()

        def contacted() -> bool:
            contact = http("GET", f"{MOCKS}/_mock/state")[1]["contacts"].get(email)
            return bool(contact and contact["stage"] == "contacted")

        wait_for(contacted, 60)
        http("POST", f"{MOCKS}/_mock/contact-stage", {"email": email, "stage": "replied"})

    end = expect.get("end", "run_finished")
    if end == "none":
        time.sleep(QUIET_S)
    elif key:

        def finished() -> list[dict[str, Any]]:
            return messages(key, "run_finished") + messages(key, "workflow_error")

        ends = wait_for(finished, EVENT_TIMEOUT_S) or []
        result["ended_with"] = [m["kind"] for m in ends]
        result["execution_id"] = ends[0].get("execution_id") if ends else None
        result["n8n_status"] = API.status(result["execution_id"]) if API else None
        if ends and ends[0]["kind"] == "run_finished":
            result["run_status"] = ends[0].get("status")
            result["outcome"] = ends[0].get("outcome")
        if ends and ends[0]["kind"] == "workflow_error":
            result["alert"] = {k: ends[0].get(k) for k in ("workflow", "node", "message", "released")}
        if event.get("redeliver_after_error") and ends and ends[0]["kind"] == "workflow_error":
            # The service is healthy again: the same event is delivered once more and must run.
            for item in event.get("script", []):
                script(item["step"], key, [])
            result["redelivery_response"] = deliver(event, event["payload"])[0]
            done = wait_for(lambda: messages(key, "run_finished"), EVENT_TIMEOUT_S) or []
            result["redelivery_status"] = done[-1]["status"] if done else None
            result["redelivery_n8n_status"] = API.status(done[-1].get("execution_id")) if done and API else None
    result["seconds"] = round(time.time() - started, 2)
    return result


def evidence(results: list[dict[str, Any]], events: dict[str, dict[str, Any]]) -> None:
    """After all events: writes per key, chat messages and mails, from the mocks' records."""
    calls = mock_calls()
    for r in results:
        event = events[r["id"]]
        key = r["key"]
        step = WRITE_STEP[r["workflow"]]
        r["writes"] = sum(1 for c in calls if c["step"] == step and c["key"] == key and 200 <= c["status"] < 300)
        r["write_attempts"] = sum(1 for c in calls if c["step"] == step and c["key"] == key)
        r["side_effects"] = sum(1 for c in calls if key and c.get("key") == key)
        if r["workflow"] == "lead" and isinstance(event["payload"].get("email"), str):
            address = event["payload"]["email"].strip().lower()
            r["emails"] = mails_to(address) if "@" in address else 0
            state = http("GET", f"{MOCKS}/_mock/state")[1]
            r["crm_contacts"] = 1 if address in state["contacts"] else 0
        if r["workflow"] == "email" and key:
            r["row_status"] = (r.get("outcome") or {}).get("row_status")
            r["tab"] = (r.get("outcome") or {}).get("tab")


def check(r: dict[str, Any], event: dict[str, Any], ledger: dict[tuple[str, str], dict[str, Any]] | None) -> list[str]:
    """Every expectation of the event, as a list of failed checks (empty: passed)."""
    e = event["expect"]
    failed = []

    def want(name: str, got: Any, expected: Any) -> None:
        if got != expected:
            failed.append(f"{name}: expected {expected!r}, got {got!r}")

    want("responses", r.get("responses"), e["responses"])
    end = e.get("end", "run_finished")
    if end == "none":
        want("side effects", r.get("side_effects"), 0)
    else:
        want("run ended with", (r.get("ended_with") or [None])[0], end)
        want("n8n execution status", r.get("n8n_status"), "success" if end == "run_finished" else "error")
    for field in ("run_status", "writes", "emails", "crm_contacts", "row_status", "tab", "approval_requests",
                  "redelivery_response", "redelivery_status", "second_response", "second_status"):
        if field in e:
            want(field, r.get(field), e[field])
    if "write_attempts" in e:
        want("write attempts", r.get("write_attempts"), e["write_attempts"])
    if "redelivery_status" in e:
        want("redelivery n8n status", r.get("redelivery_n8n_status"), "success")
    if "outcome" in e:
        for k, v in e["outcome"].items():
            want(f"outcome.{k}", (r.get("outcome") or {}).get(k), v)
    if "alert_node" in e:
        want("alert node", (r.get("alert") or {}).get("node"), e["alert_node"])
    if e.get("clicks_after_first_rejected"):
        later = (r.get("click_responses") or [])[1:]
        if not later or any(200 <= c < 300 for c in later):
            failed.append(f"later approval clicks must not resume the run, got {r.get('click_responses')}")
    if ledger is not None:
        row = ledger.get((LEDGER_NAME[r["workflow"]], r["key"] or ""))
        want("ledger status", row["status"] if row else None, e.get("ledger", e.get("run_status")))
        if "ledger_attempts" in e:
            want("ledger attempts", row["attempts"] if row else None, e["ledger_attempts"])
    return failed


def load_events() -> list[dict[str, Any]]:
    events = []
    for path in sorted(EVENTS.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                events.append(json.loads(line))
    return [e for e in events if not ONLY or e["workflow"] in ONLY]


def replay() -> int:
    global API
    events = load_events()
    API = N8nApi()
    if not API.ok:
        print("warning: could not sign in to n8n; execution statuses are not checked", file=sys.stderr)
    by_id = {e["id"]: e for e in events}
    smoke = next((e for e in events if e.get("smoke")), events[0])
    first = run_event(smoke)
    timing: dict[str, Any] = {}
    start_ms = os.environ.get("HARNESS_IMPORT_START_MS")
    finished = [c for c in mock_calls() if c["step"] == "chat" and (c.get("body") or {}).get("kind") == "run_finished"]
    if start_ms and finished:
        start = int(start_ms) / 1000
        ready = int(os.environ["HARNESS_READY_MS"]) / 1000
        setup_done = os.environ.get("HARNESS_SETUP_DONE", "")
        timing = {
            "first_event": smoke["id"],
            "import_to_setup_done_s": round(datetime.fromisoformat(setup_done.replace("Z", "+00:00")).timestamp() - start, 2) if setup_done else None,
            "import_to_ready_s": round(ready - start, 2),
            "import_to_first_success_s": round(finished[0]["ts"] - start, 2),
            "first_run_s": first["seconds"],
        }
    rest = [e for e in events if e["id"] != smoke["id"]]
    with ThreadPoolExecutor(8) as pool:
        results = [first, *pool.map(run_event, rest)]
    time.sleep(3)  # a late duplicate write would land here and be counted below
    evidence(results, by_id)
    out = {"generated_at": datetime.now(UTC).isoformat(), "timing": timing, "counts": dict(COUNTS), "results": results}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "replay-results.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    failed = {r["id"]: check(r, by_id[r["id"]], None) for r in results}
    bad = {k: v for k, v in failed.items() if v}
    print(f"replayed {len(results)} events, {COUNTS['webhook_deliveries']} webhook deliveries; "
          f"{len(results) - len(bad)} passed before the ledger check")
    for k, v in bad.items():
        print(f"  FAIL {k}: {'; '.join(v)}")
    return 1 if bad else 0


def verify_ledger() -> int:
    data = json.loads((OUT / "replay-results.json").read_text(encoding="utf-8"))
    rows = json.loads((OUT / "ledger.json").read_text(encoding="utf-8") or "[]")
    ledger = {(r["workflow"], r["key"]): r for r in rows}
    events = {e["id"]: e for e in load_events()}
    report_rows = []
    for r in data["results"]:
        failures = check(r, events[r["id"]], ledger)
        report_rows.append({**r, "passed": not failures, "failures": failures})
    dup = [r for r in report_rows if int(events[r["id"]].get("copies", 1)) > 1]
    summary = {
        "events_replayed": len(report_rows),
        "events_passed": sum(r["passed"] for r in report_rows),
        "webhook_deliveries": data["counts"]["webhook_deliveries"],
        "approval_clicks": data["counts"]["approval_clicks"],
        "by_workflow": {
            w: {"events": sum(r["workflow"] == w for r in report_rows), "passed": sum(r["passed"] and r["workflow"] == w for r in report_rows)}
            for w in ("social", "email", "lead")
        },
        "duplicate_tests": {
            "events": len(dup),
            "copies_sent": sum(int(events[r["id"]]["copies"]) for r in dup),
            "accepted": sum(r["responses"].get("202", 0) for r in dup),
            "answered_duplicate": sum(r["responses"].get("200", 0) for r in dup),
            "writes": sum(r.get("writes", 0) for r in dup),
            "passed": sum(r["passed"] for r in dup),
        },
        "ledger_rows": len(rows),
        "timing": data["timing"],
    }
    report = {"generated_at": data["generated_at"], "n8n_version": "2.45.6", "summary": summary, "results": report_rows}
    (OUT / "harness-report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    for r in report_rows:
        if not r["passed"]:
            print(f"  FAIL {r['id']} {r['title']}: {'; '.join(r['failures'])}")
    return 0 if summary["events_passed"] == summary["events_replayed"] else 1


if __name__ == "__main__":
    sys.exit(verify_ledger() if "--verify-ledger" in sys.argv else replay())
