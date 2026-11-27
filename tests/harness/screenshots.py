"""Screenshots of a harness stack after a replay run (n8n editor, executions, mock views).

Runs in the Playwright image on the stack's internal network, signed in with the run's
throw-away owner password. Execution ids come from the run's harness report, so every
picture shows a run the harness actually checked. Use tests/harness/screenshots.sh.
"""

from __future__ import annotations

import html
import json
import os
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

N8N = "http://n8n:5678"
OUT = Path(os.environ.get("SHOTS_OUT", "/shots"))
REPORT = json.loads(Path(os.environ.get("SHOTS_REPORT", "/out/harness-report.json")).read_text(encoding="utf-8"))
WORKFLOWS = {
    "00": "packErrorHandler",
    "01": "packSocialApprov",
    "02": "packEmailToSheet",
    "03": "packLeadFollowUp",
}
RESULTS = {r["id"]: r for r in REPORT["results"]}


def secret(name: str) -> str:
    return Path(f"/run/secrets/{name}").read_text(encoding="utf-8").strip()


def shot(page: Page, name: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(OUT / f"{name}.png"))
    print("saved", name)


def settle(page: Page) -> None:
    page.wait_for_load_state("networkidle")
    page.keyboard.press("Escape")
    page.wait_for_timeout(1500)


def fit(page: Page, zoom_in: int = 0) -> None:
    page.keyboard.press("Shift+1")
    page.wait_for_timeout(800)
    button = page.locator("[data-test-id='zoom-in-button']")
    for _ in range(zoom_in):
        if button.count():
            button.first.click()
        else:
            page.keyboard.press("=")
        page.wait_for_timeout(300)


def canvas(page: Page, key: str, name: str, zoom_in: int = 0) -> None:
    """The workflow fitted to the window, cropped to its nodes (rendered at 2x, so readable)."""
    page.goto(f"{N8N}/workflow/{WORKFLOWS[key]}")
    page.locator(".vue-flow").first.wait_for(timeout=30_000)
    settle(page)
    fit(page, zoom_in)
    box = page.evaluate(
        """() => {
          const rects = [...document.querySelectorAll('.vue-flow__node')].map((n) => n.getBoundingClientRect());
          const x = Math.min(...rects.map((r) => r.left)), y = Math.min(...rects.map((r) => r.top));
          const right = Math.max(...rects.map((r) => r.right)), bottom = Math.max(...rects.map((r) => r.bottom));
          return { x, y, width: right - x, height: bottom - y };
        }"""
    )
    pad = 36
    clip = {
        "x": max(0, box["x"] - pad),
        "y": max(0, box["y"] - pad),
        "width": min(page.viewport_size["width"], box["width"] + 2 * pad),
        "height": box["height"] + 2 * pad,
    }
    OUT.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(OUT / f"{name}.png"), clip=clip)
    print("saved", name)


def execution(page: Page, key: str, event: str, name: str, field: str = "execution_id") -> None:
    execution_id = RESULTS[event].get(field)
    if not execution_id:
        print("skipped", name, "(no execution id in the report)")
        return
    page.goto(f"{N8N}/workflow/{WORKFLOWS[key]}/executions/{execution_id}")
    page.locator(".vue-flow").first.wait_for(timeout=30_000)
    settle(page)
    shot(page, name)


def summary_page(page: Page) -> None:
    """The harness report's summary and per-event results as one page."""
    s = REPORT["summary"]
    rows = "".join(
        f"<tr><td>{r['id']}</td><td>{html.escape(r['title'])}</td>"
        f"<td>{html.escape(json.dumps(r['responses']))}</td>"
        f"<td class={'ok' if r['passed'] else 'bad'}>{'passed' if r['passed'] else 'FAILED'}</td></tr>"
        for r in REPORT["results"]
    )
    d = s["duplicate_tests"]
    t = s["timing"]
    page.set_viewport_size({"width": 1400, "height": 1000})
    page.set_content(
        "<!doctype html><meta charset=utf-8><style>"
        "body{font:15px 'Cascadia Code',Consolas,monospace;background:#0d1117;color:#c9d1d9;margin:28px}"
        "h1{font:600 24px system-ui,sans-serif;color:#fff;margin:0 0 6px}p{color:#9da7b3;margin:4px 0}"
        "table{border-collapse:collapse;width:100%;margin-top:16px}td{border-bottom:1px solid #21262d;padding:3px 8px}"
        ".ok{color:#3fb950}.bad{color:#f85149}b{color:#3fb950}</style>"
        f"<h1>Replay harness: n8n {REPORT['n8n_version']}, fresh stack</h1>"
        f"<p>{s['events_replayed']} recorded events, {s['webhook_deliveries']} webhook deliveries, "
        f"{s['approval_clicks']} approval clicks: <b>{s['events_passed']}/{s['events_replayed']} passed</b></p>"
        f"<p>Duplicate tests: {d['copies_sent']} copies in {d['events']} events, {d['accepted']} accepted, "
        f"{d['answered_duplicate']} answered as duplicates, {d['writes']} writes</p>"
        f"<p>Import on an empty database to first successful run: <b>{t.get('import_to_first_success_s')} s</b> "
        f"(setup done {t.get('import_to_setup_done_s')} s, n8n ready {t.get('import_to_ready_s')} s)</p>"
        f"<table>{rows}</table>"
    )
    page.screenshot(path=str(OUT / "11-harness-report.png"), full_page=True)
    print("saved 11-harness-report")


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1600, "height": 1000}, device_scale_factor=2)
        page.goto(f"{N8N}/signin")
        page.locator("input[type=email], input[name=emailOrLdapLoginId]").first.fill("owner@example.com")
        page.locator("input[type=password]").first.fill(secret("n8n_owner_password"))
        page.get_by_role("button", name="Sign in").click()
        page.wait_for_url(lambda url: "/signin" not in url, timeout=30_000)
        canvas(page, "01", "01-social-approval-canvas")
        canvas(page, "02", "02-email-to-sheet-canvas")
        canvas(page, "03", "03-lead-follow-up-canvas")
        canvas(page, "00", "04-error-handler-canvas", zoom_in=1)
        execution(page, "01", "S16", "05-social-retried-and-published")
        execution(page, "02", "E01", "06-email-run-succeeded")
        execution(page, "01", "S19", "07-social-failed-run-alerted")
        execution(page, "03", "L01", "08-lead-sequence-run")
        page.goto("http://mock-sinks:8000/_mock/view")
        page.wait_for_load_state("networkidle")
        page.screenshot(path=str(OUT / "09-mock-services-view.png"), full_page=True)
        print("saved 09-mock-services-view")
        page.goto("http://mailpit:8025/")
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(1500)
        shot(page, "10-mailpit-follow-up-emails")
        summary_page(page)
        browser.close()


if __name__ == "__main__":
    main()
