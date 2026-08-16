# CLAUDE.md — Jupiter (Luminque)

Orientation for anyone, human or AI, arriving at this repo cold. Component
detail lives in the per-directory `README.md` / `CLAUDE.md` files; this file is
the map, the current status, and the account-level facts that **cannot be
derived by reading the code**.

## What this is

Luminque records what people actually do on their workstations and reconstructs
the business process from it. A Windows agent captures screenshots locally, a
cloud service ingests them, and an analysis pipeline turns a day of frames into
a described workflow with the evidence attached.

```
Windows workstation                AWS (us-east-1)                 Operator's laptop
┌────────────────────┐             ┌──────────────────┐            ┌──────────────────┐
│ agent/             │  HTTPS      │ ingestion/       │            │ discovery/       │
│  captureV2 ──▶ db  │ ──────────▶ │  FastAPI on ECS  │ ──▶ S3 ──▶ │  map ▶ reduce    │
│  sender            │  X-Device-  │  + RDS Postgres  │   frames   │  ▶ workflow.json │
│  watchdog          │  Token      │                  │            │  ▶ serve.py      │
└────────────────────┘             └──────────────────┘            └──────────────────┘
                                    terraform/ provisions all of the middle column
```

A frame's identity is its capture timestamp, end to end: the agent stamps
`captured_at`, ingestion dedupes on `(agent_id, captured_at)`, S3 keys are
`{tenant}/{agent}/{YYYY-MM-DD}/{captured_at}.png`, and discovery links workflow
steps back to frames by resolving timestamps against those keys. Nothing
downstream invents an id.

## Components

| Directory | What it is | Start here |
|---|---|---|
| `agent/` | Windows capture agent. One PyInstaller `.exe`, five modes (`--onboard`, `--capture`, `--send`, `--watchdog`, `--stop`). | `agent/CLAUDE.md` |
| `ingestion/` | FastAPI service. `/healthz`, `/v1/enroll`, `/v1/screenshots`, `/v1/heartbeat`. Frames to S3, metadata to Postgres. | `ingestion/README.md` |
| `terraform/` | The AWS estate: VPC, RDS, S3, ECR, ECS Fargate + ALB, Secrets Manager, GitHub OIDC deploy role. | `terraform/README.md` |
| `discovery/` | The analysis pipeline: frames → segment notes → report + workflow graph → browsable viewer. | `discovery/README.md` |

`sop-discovery.nosync/` is **not** part of this system. It is a checkout of the
previous iteration's prototype (`github.com/luminiq-hq/sop-discovery`), kept for
reference and gitignored. Its data model (sessions, devices, deviations)
contradicts the current contract — do not copy its plumbing.

## Status — what is actually running (as of 2026-08-16)

Terraform describes a target state; this is what has been applied and used.

**Deployed and exercised**
- The full Terraform stack is applied in us-east-1, and CI deploys ingestion on
  every push to `main` (`.github/workflows/deploy.yml`, GitHub OIDC, no keys).
- Agent capture → sender → ingestion → S3 has run end to end; there are real
  frames in the screenshots bucket.
- Discovery `map`/`reduce`/`sample` run **from a laptop** against S3, producing
  reports and workflow viewers.

**Deliberately not done yet**
- **No domain.** `domain_name` is empty, so the ALB serves plain HTTP on :80.
  Agents bake the endpoint URL into their keyring at enrollment, so *do not
  enroll real agents until a domain exists* — they would need re-onboarding.
- **The agent `.exe` is unsigned.** `build.yml` uploads an unsigned artifact.
  Whether to buy an EV certificate is an open decision, not a task.
- **The retention sweep is a placeholder.** The EventBridge rule exists but is
  `DISABLED` with a `/bin/true` command. S3 objects still expire on their own
  via the bucket lifecycle rule (`retention_days`, default 30) — the Postgres
  row sweep is what is missing.
- **Discovery has no cloud compute.** The `luminque-discovery` IAM role is
  read-only on the screenshots bucket, trusts only `ecs-tasks.amazonaws.com`,
  and has **no Bedrock permissions and nothing attached to it**. Steps 5 and
  7–9 of `luminque-discovery-p1.md` (artifacts bucket, EC2 + instance profile,
  `run-discovery.sh`, CloudFront viewer) are designed but unbuilt. Discovery
  runs on a laptop under a human's SSO session today.
- **No PII scrubbing.** Phase 2 (`luminque-sender-p2.md`). Frames are stored raw.
- **No read API or dashboard.** Output is files plus `discovery/serve.py`.

## AWS access

```bash
aws sso login --profile kangaroo
export AWS_PROFILE=kangaroo
```

Account **047285411146**, primary region **us-east-1**. `discovery/analyze.py`
sets `AWS_PROFILE=kangaroo` itself if nothing else is set.

SSO sessions expire quickly, and botocore/terraform fail before the AWS CLI
does — the CLI keeps cached role credentials in `~/.aws/cli/cache`. A sudden
crop of credential errors from Python or Terraform while `aws` still works
means the session lapsed; re-run `aws sso login`.

## Bedrock: the account is capacity-gated (findings Jul 2026)

**Read this before debugging any model call.** None of it is visible in the
code or in Terraform, because model access and quota are granted by AWS
out-of-band — there is no resource to provision. These are point-in-time
findings; re-verify before treating any of them as permanent.

- **us-east-1 and us-west-2 Bedrock inference quotas are all zero** on this
  account (insufficient spend history). A zero quota does not present as a
  permissions error — the first call returns
  `ThrottlingException: Too many tokens per day`. It looks transient. It is not.
  Retrying and backing off will never succeed.
- **A quota increase was requested and denied.** AWS support case
  **178369040200504** (Nova Pro) was refused with a catch-22: use 90% of current
  capacity before requesting more, when current capacity is zero. Do not spend
  time re-filing the same request expecting a different outcome.
- **Quotas are per-region, and that is the workaround.** Capacity exists in
  **us-east-2** (also eu-west-1 / eu-central-1 for Nova). Every discovery script
  therefore defaults to `--region us-east-2`. Measured in us-east-2 on
  2026-07-22: Claude Opus 4.5 cross-region profile = 2M tokens/min, **5
  requests/min**, 6.75M tokens/day; Nova Pro = 230M tokens/day, 25 RPM.
- **Current-generation Claude is blocked, and every documented gate is already
  green.** The use-case form is ON FILE (`auth=AUTHORIZED`) and the offer
  agreements are accepted (`scripts/accept_claude_agreements.py`,
  `agreement=AVAILABLE`) — yet Sonnet 5 / Opus 4.7 / Opus 4.8 still return *"not
  available for this account … contact AWS Sales"* on every endpoint. This is an
  undocumented low-spend restriction. There is no console setting that fixes it.
- **Older generations do work**: Opus 4.6, Opus 4.5, Haiku 4.5, via `us.*`
  cross-region inference profiles in us-east-2. Hence the pinned default
  `us.anthropic.claude-opus-4-5-20251101-v1:0`. Bare dated model ids fail — the
  `us.` prefix is required. Ids beginning `anthropic.*` route to the Anthropic
  SDK's Bedrock client instead of Converse, and that endpoint exists only in
  us-east-1, so they will hit the zero-quota wall above.

The 5 RPM ceiling is why long `map` runs are slow and why retry noise is normal
rather than a failure; boto3's adaptive retries do the pacing.

Check access before a long run:

```bash
uv run discovery/ask_date.py --model us.anthropic.claude-opus-4-5-20251101-v1:0 --region us-east-2
```

## Design docs

`design-docs/` holds the full technical design records — exact signatures, SQL,
schtasks invocations, edge cases. They are point-in-time documents: where one
contradicts the code, the code wins, but the doc usually explains *why*.

| Doc | Covers |
|---|---|
| `luminque-capture-p3.md` | **Current** capture design (captureV2). |
| `luminque-capture-p1/p2.md` | Superseded openadapt-capture design. |
| `luminque-sender-p1.md` | Sender: payload, cursor, credentials, retention. |
| `luminque-sender-p2.md` | (Future) PII scrubbing. |
| `luminque-onboarding-watchdog-p1.md` | Consent flow, watchdog logic. |
| `luminque-packaging-p1.md` | PyInstaller onedir build, SFX installer. |
| `luminque-deployment-p1.md` | Autostart, CI pipeline, signing, on-machine layout. |
| `luminque-deployment-p2.md` | (Future) Phase 2 deployment. |
| `luminque-ingestion-p1.md` | The v1 ingest contract. |
| `luminque-infra-p1.md` | AWS topology and the perimeter rule. |
| `luminque-discovery-p1.md` | Analysis pipeline, workflow artifact, remote-execution plan. |

## Conventions and traps

- **The perimeter rule** (`luminque-infra-p1.md`): customer screenshots stay
  inside AWS. Laptops receive derived outputs, not raw frames. `discovery/`
  streams frames per chunk and never writes them to disk; `serve.py` proxies
  them for viewing rather than downloading them. `scripts/pull-screenshots.sh`
  builds a local mirror and is the deliberate exception — for development.
- **Nothing runs from a laptop path.** Scripts resolve their own location
  (`cd "$(dirname "$0")/.."`). Do not reintroduce absolute paths.
- **Secrets never enter the repo.** `DATABASE_URL` exists only in Secrets
  Manager (and tfstate, which is why the state bucket is private and
  encrypted). The agent stores its device token in Windows Credential Manager
  via `keyring`, never a file. `terraform/backend.hcl` is generated, not
  committed.
- **RDS is private**, so admin work runs as one-off Fargate tasks on the
  service's own image — see `terraform/scripts/create-tenant.sh` and
  `query-agents.sh`.
- **Discovery notes are the expensive artifact.** Losing `discovery/runs/`
  costs one cheap text call; losing `discovery/notes/` means re-paying the
  entire vision pass. They are keyed by exact time range, so changing chunking
  parameters invalidates them.
