# Testing

Three layers, all run in CI on every push.

| Layer | Command | What it proves |
|---|---|---|
| Code node unit tests | `node --test tests/unit/*.test.mjs` | Validation, extraction, retry classification and approval logic, run as n8n runs them (helpers in scope, no `URL` class, like n8n's task runner) |
| Export checks | `python3 scripts/build_workflows.py --check`, `python3 tests/events/generate.py --check` | The committed workflow JSON and events are exactly what the definitions produce; every connection points at a node; no credential values in the exports |
| Replay harness | `tests/harness/run.sh` | The real exports, imported by the real setup script into a fresh n8n, behave as specified against recorded events |

## The replay harness

`tests/harness/run.sh` builds a disposable stack on an internal Docker network
([`compose.yaml`](../compose.yaml)): PostgreSQL (n8n's database and the ledger), n8n 2.45.6 with
external task runners, the mock services and Mailpit. Every run starts from empty volumes with
secrets generated for that run, and removes everything at the end.

[`tests/harness/replay.py`](../tests/harness/replay.py) then replays every line of
[`tests/events/*.jsonl`](../tests/events). An event is a recorded, synthetic request (invented
people and companies on `example` domains) with its delivery plan and expected result:

| Field | Meaning |
|---|---|
| `copies`, `concurrent` | Send the same request N times, one after another or released together by a barrier |
| `variants` | The same event spelled differently (e.g. a Message-ID in three forms) |
| `auth` | `valid`, `wrong` or `missing` webhook token |
| `script` | Scripted answers of a mock service for this key: 429 with `Retry-After`, 5xx, permanent 4xx |
| `approval` | What the reviewer does: approve (once or twice), reject, or nothing until the time limit |
| `reply` | The lead answers during the follow-up wait (the CRM stage turns `replied`) |
| `second` | A second submission for the same person while the first sequence runs |
| `redeliver_after_error` | After the alert, heal the service and send the same event again |
| `expect` | HTTP answers, how the run ended, writes per key, emails, ledger status and attempts |

For each event the harness checks:

- the HTTP answers n8n gave (`202` accepted, `200` duplicate, `400` invalid, `403` bad token);
- how the run ended: the run notice or the error workflow's alert in the chat mock;
- the writes the mocks recorded for the event's key (posts, sheet rows, CRM upserts) and the
  emails Mailpit received. The mocks are not idempotent, so a duplicate run would show;
- n8n's own record of the execution (`success` or `error`, from n8n's REST API);
- the final ledger row in PostgreSQL (status and attempts).

Events run eight at a time, each with its own key. Short time limits are set for the run
(approval 25 s, follow-up wait 12 s, retry base 1 s) so the expiry and follow-up paths finish
in minutes.

### Timing

`run.sh` pulls the images first (so downloads are not counted), starts PostgreSQL and the mocks,
then records the host clock and starts n8n on an empty database. Measured:

- **setup done:** the setup container exited (n8n migrations, 4 credentials, 4 workflows
  imported and published);
- **ready:** n8n answers `/healthz/readiness`;
- **first success:** the first replayed event (`E01`, an invoice email) finished successfully,
  taken from the mock's timestamp of its run notice.

### Running it

```bash
tests/harness/run.sh
HARNESS_ONLY=email tests/harness/run.sh
HARNESS_KEEP=1 HARNESS_EDITOR=1 tests/harness/run.sh   # editor on 127.0.0.1:5678 afterwards
```

Needs Docker with Compose v2. The report is `tests/harness/out/harness-report.json`; results
of recorded runs are kept in [`docs/results/`](results).

## What the harness found

- **No `URL` class in the task runner.** The first run failed every social post: building the
  approval links with `new URL()` threw `URL is not defined` inside n8n's external task runner,
  and a valid `https` media URL was rejected for the same reason. Both now use plain string
  handling, and the unit tests run the scripts with `URL` hidden. The error workflow reported
  each of these runs with the node name and released the key, as designed.
- **A missing webhook token gets `403`, not `401`.** n8n answers both a wrong and a missing token
  with `403`; the expectation was corrected to n8n's actual behaviour (nothing runs either way).
- **A redelivered post asks for approval again.** After a permanent publish error, the
  redelivered request goes through approval again; the harness now approves that second
  request instead of letting it expire.
- **An approval click can come too early.** On the GitHub runner (faster than the lab VM), the
  harness clicked approve within milliseconds of the chat message for 2 of 20 social events.
  The run was not waiting yet, n8n answered `409`, and the post expired unpublished: safe, but
  the reviewer's click was lost. The message has to be posted before the Wait node starts, so
  the gap cannot be closed inside a generic-webhook workflow; it is documented in
  [workflows.md](workflows.md#01-social-post-with-human-approval), and the harness now does
  what a reviewer does: it clicks again after a `409` on the first click (up to 5 times, 1 s
  apart) and reports these repeats as `early_click_repeats`. The four lab runs in
  [`results/`](results) needed none.
