# luminque-ingestion

Receives screenshots from Luminque agents over HTTP, writes the PNG to S3
(MinIO locally) and a metadata row to Postgres.

Spec: `../design-docs/luminque-ingestion-p1.md` (contract, data model) and
`../design-docs/luminque-infra-p1.md` (local dev, security baseline).

## Endpoints

| Endpoint | Auth | Success | Notes |
|---|---|---|---|
| `POST /v1/enroll` | `enrollment_token` in body | 201 | Creates the agent row, returns its device token (plaintext, exactly once — only the sha256 is stored). 401 on unknown token. |
| `POST /v1/screenshots` | `X-Device-Token` header | 201 (stored) / 200 (duplicate) | Multipart: `file`, `captured_at` (ISO-8601 UTC), optional `window_title`, `app_name`. Dedupe on `(agent_id, captured_at)` — duplicates are success. 401 unknown/disabled, 413 too large. |
| `POST /v1/heartbeat` | `X-Device-Token` header | 204 | Bumps `agent.last_seen_at`. 401 unknown/disabled. |

Identity comes from the token: the server derives `agent_id`/`tenant_id` from
`X-Device-Token` on every request; request bodies never carry tenant or device
ids.

## Local development

Prerequisites: [uv](https://docs.astral.sh/uv/), Docker.

```bash
# 1. Postgres 16 + MinIO + one-shot bucket init (named volumes persist data)
docker compose up -d

# 2. Config — all of it comes from env vars, nothing else
cp .env.example .env

# 3. Install deps
uv sync

# 4. Run the API on the host (migrations run on startup under a
#    Postgres advisory lock)
uv run --env-file .env uvicorn luminque_ingestion.app:app --reload
```

MinIO console: <http://localhost:9001> (minioadmin / minioadmin), bucket
`luminque-dev`. Or:
`aws s3 ls s3://luminque-dev --recursive --endpoint-url http://localhost:9000`.

## End-to-end walkthrough

**1. Create a tenant.** There is no admin API — the dev CLI is how a tenant
row is born. It prints the tenant id and its reusable enrollment token:

```bash
uv run --env-file .env luminque-create-tenant "Acme Corp"
# tenant_id:        bbd33552-...
# enrollment_token: 8_PYcMtat5...
```

**2. Enroll an agent** (what onboarding does once per machine). The response
is the only time the device token appears in plaintext:

```bash
curl -s -X POST http://localhost:8000/v1/enroll \
  -H 'Content-Type: application/json' \
  -d '{"enrollment_token":"<ENROLLMENT_TOKEN>","hostname":"DESKTOP-01","platform":"windows","os_version":"10.0.22631"}'
# 201 {"agent_id":"...","auth_token":"<TOKEN>"}
```

**3. Upload a screenshot** (what the sender does, one request per frame):

```bash
curl -s -X POST http://localhost:8000/v1/screenshots \
  -H 'X-Device-Token: <TOKEN>' \
  -F 'file=@shot.png;type=image/png' \
  -F 'captured_at=2026-07-04T09:15:00Z' \
  -F 'window_title=Invoice.xlsx - Excel' \
  -F 'app_name=EXCEL.EXE'
# 201 {"id":"..."}
```

**4. Upload the same frame again** — at-least-once delivery means retries
resend; the duplicate is a success, not an error, and stores nothing new:

```bash
# same command as step 3
# 200 {"id":"<same id>"}
```

**5. Heartbeat** (sender runs this once per cycle even with nothing to send):

```bash
curl -s -X POST http://localhost:8000/v1/heartbeat -H 'X-Device-Token: <TOKEN>' -w '%{http_code}\n'
# 204
```

The screenshot is now visible in the MinIO console under the key layout

```
{tenant_id}/{agent_id}/{YYYY-MM-DD}/{captured_at_iso}.png
```

and the `screenshot` row in Postgres points at it via `s3_key`.

## Tests

```bash
uv run pytest -v
```

Unit tests need nothing running. Contract tests run against the compose
Postgres + MinIO (they skip, with a message, if the stack is down); they use
a dedicated `luminque_test` database, dropped and recreated per run — dev
data in the `postgres` database is untouched.

## Layout

```
luminque_ingestion/
  app.py          FastAPI app: the three endpoints
  auth.py         X-Device-Token → agent resolution (sha256 lookup)
  storage.py      thin S3 wrapper (put/get/list) — the ONLY module that imports boto3
  models.py       SQLAlchemy models: tenant / agent / screenshot
  migrations.py   Alembic upgrade at startup under a Postgres advisory lock
  cli.py          dev CLI: luminque-create-tenant
  config.py       env-var config (DATABASE_URL, S3_ENDPOINT_URL, S3_BUCKET, MAX_UPLOAD_BYTES)
  alembic/        migration scripts (hand-written, one per revision)
```

`MAX_UPLOAD_BYTES` (the 413 threshold) defaults to 10 MiB; the design docs
don't fix a number, so it is an env var like everything else.

## Deploy artifact

```bash
docker build -t luminque-ingestion .
```

The container reads the same env vars and never knows which environment it is
in: in prod `S3_ENDPOINT_URL` is unset (boto3 defaults to real S3) and auth is
the task's IAM role. Terraform/CI are out of scope here — see
`luminque-infra-p1.md`.
