# Recorded harness runs

Four full runs of `tests/harness/run.sh` on 2026-11-27, each on a fresh stack (empty volumes,
new secrets), on a lab VM (Debian 12, 10 vCPU, 9.7 GB RAM) shared with other running
services. Images were present beforehand, so no download time is included. Each folder holds
the harness report, the final ledger, the timing marks and the setup log.

| Run | Events passed | Webhook deliveries | Approval clicks | Setup done | n8n ready | First successful run |
|---|---|---|---|---|---|---|
| [a](run-2026-11-27-a/harness-report.json) | 50 / 50 | 89 | 13 | 45.86 s | 60.04 s | 68.05 s |
| [b](run-2026-11-27-b/harness-report.json) | 50 / 50 | 89 | 13 | 46.22 s | 60.50 s | 68.47 s |
| [c](run-2026-11-27-c/harness-report.json) | 50 / 50 | 89 | 13 | 46.67 s | 61.01 s | 68.80 s |
| [d](run-2026-11-27-d/harness-report.json) | 50 / 50 | 89 | 13 | 49.22 s | 62.99 s | 71.06 s |

Times are measured from the start of the import on an empty n8n database (migrations,
credentials, workflow import and publish all included). Run d also produced the screenshots
in [`assets/screenshots/`](../../assets/screenshots). Between runs c and d the synthetic leads
got names matching their addresses (they were all "Robin Avery" before); nothing else changed.

Duplicate tests, identical in all four runs:

| | Events | Copies sent | Accepted (`202`) | Answered as duplicate (`200`) | Writes |
|---|---|---|---|---|---|
| 10 copies released at the same moment | 3 | 30 | 3 | 27 | 3 |
| Repeats one after another (incl. three spellings of one Message-ID) | 4 | 12 | 4 | 8 | 4 |
| **Total** | **7** | **42** | **7** | **35** | **7** |

A write is a post published, a sheet row appended or a CRM contact upserted for that event's
key, counted from the mock services' own records. The mocks do not de-duplicate anything, so
a duplicate run would have shown as a second write.

Two earlier runs that day (37/50, then 49/50) found the issues described in
[docs/testing.md](../testing.md#what-the-harness-found); their reports were overwritten by the
next run and are not kept.
