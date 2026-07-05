# Luminque Ingestion — Phase 1: Contract & Data Model

> **Status: in progress.** This doc covers the sender↔server contract and the
> server data model. Hosting, IaC, CI/CD, and the security baseline live in
> `luminque-infra-p1.md`. The discovery service internals and the viewing UI
> are still under discussion.

## What this project is

Luminque records what users do on their computers for retrospective business
process analysis:

1. An **agent** (existing, `agent/` in this repo) runs on each user machine.
   It captures screenshots locally (activity-gated, ~1 fps, deduped) into
   SQLite, and a separate sender process ships them to the cloud on a schedule.
2. An **ingestion service** (to be built) receives screenshots over HTTP,
   writes the PNG to S3 and a metadata row to Postgres.
3. A **discovery service** (to be built, driven manually for now) pulls
   screenshots for an agent over a time window and uses an LLM to figure out
   what the user was doing.

Multiple agents run concurrently, one per machine. Agents belong to tenants.

## Design decisions

- **No sessions.** Every screenshot stands alone with `agent_id` +
  `captured_at`. If "work session" boundaries are ever needed, the discovery
  service derives them from timestamps at analysis time. This removes the
  session-create/404-recreate state machine from the current sender.
- **Identity comes from the token.** The server derives `agent_id` and
  `tenant_id` from `X-Device-Token` on every request. The agent never sends
  tenant or device ids in request bodies.
- **At-least-once delivery, server-side dedupe.** The sender's cursor only
  advances on success, so retries resend. Dedupe on a unique
  `(agent_id, captured_at)` index; duplicates return 200 (success), never an
  error.
- **Per-screenshot uploads, no batching.** Simplest on both ends; batching is
  the first optimization if HTTP overhead ever matters. No presigned URLs —
  the ingestion service receives bytes and writes to S3 itself.
- **The events stream is gone.** Foreground window title/app travel as form
  fields on the screenshot upload. Action events (mouse/key) stay local — they
  only exist to gate capture sampling.
- **No `User` table yet.** Users belong to the future read side (dashboard
  login). Nothing on the ingest path needs one.

## Tech stack

Python (uv-managed, same toolchain as the agent repo) with FastAPI + uvicorn;
SQLAlchemy + Alembic for models/migrations; boto3 confined to a thin storage
module (`put`/`get`/`list` — see `luminque-infra-p1.md` for why); pytest.
Ships as a Docker container (the deploy artifact). Local dev runs uvicorn
directly on the host against the dockerized Postgres + MinIO from
`luminque-infra-p1.md`; the app reads all config from env vars and never
knows which environment it is in.

## API

Three endpoints, plus an unauthenticated `GET /healthz` returning
`200 {"status": "ok"}` for load-balancer health checks (no DB or S3 calls —
liveness only). All responses JSON unless noted.

### `POST /v1/enroll`

Called once by onboarding. Creates the agent row and issues its token.

```
Auth:  none (enrollment_token in body is the auth)
Body:  JSON
  enrollment_token   string   required — tenant-scoped; server derives tenant
  hostname           string   required
  platform           string   required   e.g. "windows"
  os_version         string   required

201  {"agent_id": "<uuid>", "auth_token": "<token>"}
     Token returned in plaintext exactly once; server stores only its sha256.
401  unknown/disabled enrollment token
```

Re-running onboarding on an already-enrolled machine creates a new agent row;
the old one goes stale. Intentional — no hostname dedupe.

### `POST /v1/screenshots`

Called by the sender in a loop, one request per screenshot.

```
Auth:  X-Device-Token header
Body:  multipart/form-data
  file           PNG bytes                      required
  captured_at    ISO-8601 UTC string            required
  window_title   string                         optional
  app_name       string                         optional

201  {"id": "<uuid>"}   stored
200  {"id": "<uuid>"}   duplicate (agent_id, captured_at) — client treats as success
401  unknown/revoked token, or agent status = disabled
413  file too large
```

Handler order: S3 put first, then DB insert (an orphaned S3 object on crash is
harmless; a row pointing at nothing is not). Bumps `agent.last_seen_at`.

### `POST /v1/heartbeat`

Called by the sender once per cycle, even with nothing to upload. The sender
is the agent's only networked process, so this signals "sender ran and reached
the server" — not that capture is alive (that's the local watchdog's job).

```
Auth:  X-Device-Token header
Body:  empty for now (room for capture-liveness/queue-depth fields later)

204  — bumps agent.last_seen_at
401  unknown/revoked/disabled token
```

## Data model (Postgres)

```sql
tenant
  id                uuid PK
  name              text
  enrollment_token  text UNIQUE      -- reusable, tenant-scoped
  created_at        timestamptz

agent
  id            uuid PK
  tenant_id     uuid FK → tenant
  token_hash    text UNIQUE            -- sha256 of the device token
  hostname      text
  platform      text
  os_version    text
  status        text DEFAULT 'active'  -- active | disabled (admin kill switch;
                                       -- online/offline is derived from last_seen_at)
  last_seen_at  timestamptz
  created_at    timestamptz

screenshot
  id            uuid PK
  agent_id      uuid FK → agent
  tenant_id     uuid                 -- denormalized from agent at insert
  captured_at   timestamptz          -- client capture time
  received_at   timestamptz          -- server receipt time (upload-lag signal)
  s3_key        text                 -- key only; bucket/region live in config
  window_title  text
  app_name      text
  size_bytes    int

  UNIQUE (agent_id, captured_at)     -- idempotency
  INDEX  (tenant_id, captured_at)    -- discovery's query pattern
```

### S3 layout

```
{tenant_id}/{agent_id}/{YYYY-MM-DD}/{captured_at_iso}.png
```

Prefixed so manual discovery runs are a single `aws s3 sync` on an
agent/date prefix, no DB query required.

## Sender changes required (agent repo)

The capture side is untouched. In `luminque/sender/`:

- Delete session handling: `create_session`, `server_session_id` state,
  the 404-recreate path, and `build_session_request`.
- Delete the events envelope (`build_events_request`, `post_events`) and the
  action/window event serializers. Join the foreground window title/app for
  each frame from the capture DB and send them as form fields on the
  screenshot upload.
- Replace `post_media` with `POST /v1/screenshots` (multipart, per frame,
  `captured_at` from the capture DB). Treat 200 and 201 both as success;
  advance the cursor per accepted frame.
- Replace `post_health` with `POST /v1/heartbeat`.
- Keyring shrinks to two entries: `auth_token`, `endpoint_url`
  (`tenant_id` and `device_id` are no longer sent anywhere). Enrollment
  (`luminque/onboarding/enrollment.py`) stops sending `tenant_id` and stops
  persisting `tenant_id`/`device_id`.

## Later / open

- Discovery service internals and its link to ingestion (manual for now).
- Viewing UI + `User` model (read side; won't touch this contract).
- Heartbeat body fields for capture liveness, if the dashboard needs to
  distinguish "capture dead" from "user idle".
- Batch upload endpoint, if per-frame HTTP overhead becomes a problem.
