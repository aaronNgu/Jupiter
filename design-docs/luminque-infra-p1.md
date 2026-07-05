# Luminque Infrastructure — Phase 1

> **Status: in progress.** Covers hosting, IaC, CI/CD, local dev, and the
> security baseline for the ingestion service defined in
> `luminque-ingestion-p1.md`. Discovery-service compute and the viewing UI
> are not covered yet.

## Context

The ingestion service (see `luminque-ingestion-p1.md`) receives screenshots
from Windows agents, writes PNGs to object storage and metadata to Postgres.
Screenshots contain sensitive customer data, so the infrastructure choices are
security-driven: single cloud vendor, data stays inside one perimeter,
least-privilege access, retention limits.

Traffic is low (a handful of agents, ~1 fps active each, ≤1280px PNGs of
100–300 KB). Cost is a secondary concern; simplicity and the ability to move
are primary.

## Decisions

| Concern | Choice | Why |
|---|---|---|
| Cloud | AWS, single-vendor | One subprocessor, one pinnable region, compliance artifacts customers accept. Revisit only on customer signal (e.g. an Azure-shop pilot). |
| Compute | ECS Fargate + ALB, one always-on task | App Runner closed to new customers (Apr 2026). Fargate is the un-deprecatable substrate, runs in-VPC (native RDS access), and Fargate+ALB Terraform is the standard shape for future single-tenant/customer-VPC deploys. |
| Database | RDS Postgres (pin a major version, e.g. 16) | Vanilla Postgres via `DATABASE_URL`; `pg_dump` moves anywhere. |
| Object storage | S3, IAM task role (no static keys) | The S3 API is the de facto standard. Accessed only through the app's thin storage module (`put`/`get`/`list` in one file) so a cloud move touches one file — Azure Blob does not speak S3. |
| LLM (discovery) | Bedrock | Screenshots never leave the AWS perimeter. Discovery runs next to the data; laptops only ever receive outputs, never pixels. |
| Domain | `api.<domain>` CNAME in front, before first enrollment | The endpoint URL is baked into agents' keyrings at enrollment; the domain must outlive any hosting choice. |
| Not chosen | Lambda | 6 MB sync payload cap sits too close to multipart PNG uploads (and would force presigned URLs); per-invoke DB connections want RDS Proxy. Cold starts don't matter here, but the footguns do. |
| Not chosen | Lightsail Containers | Same strategic risk App Runner had; awkward VPC/RDS connectivity. |
| Not chosen | PaaS (Railway/Render) + R2 | Fine for a demo; three-subprocessor story fails security-sensitive customers. |

Portability rule that makes every choice above reversible: the app is a
Docker container, all config via env vars (`DATABASE_URL`, `S3_ENDPOINT_URL`,
`S3_BUCKET`), no vendor SDK outside the storage module, no Postgres
extensions.

## Terraform (from day one)

Infrastructure is provisioned exclusively by Terraform. No click-ops — the
Terraform module doubles as the future single-tenant product offering.

State: S3 backend (bucket + DynamoDB lock table, created once by hand or a
bootstrap script — the only resources not in state).

Resources:

- VPC: 2 AZs, public subnets (ALB) + private subnets (Fargate task, RDS),
  NAT gateway or VPC endpoints for ECR/S3/Secrets Manager pulls.
- RDS Postgres 16, smallest instance, encrypted, not publicly accessible,
  security group admitting only the Fargate task SG.
- S3 bucket: block all public access, default SSE, lifecycle rule for the
  retention policy (expire objects after N days; N configurable, see
  security baseline).
- ECR repository.
- ECS: cluster, task definition (0.25 vCPU / 512 MB to start), service
  (desired count 1) behind an ALB with an ACM cert on `api.<domain>`;
  HTTP→HTTPS redirect.
- IAM: task role scoped to `s3:PutObject` on the bucket prefix (ingestion
  needs write-only) + Secrets Manager read for its own secrets. Separate
  read-only role for discovery, defined now, used later.
- Secrets Manager: `DATABASE_URL` (and any future secrets), injected into
  the task definition — never in Terraform variables, tfstate is still
  treated as sensitive, or GitHub.
- EventBridge scheduled Fargate task (same image, different command) —
  placeholder for the retention sweep of Postgres rows; S3 lifecycle handles
  the objects themselves.
- Terraform must `ignore_changes` on the task definition image (or track a
  stable tag) so `terraform apply` never rolls back a CI deploy.

Human prerequisites an agent cannot do (do these first, by hand):

1. AWS account: MFA on root (then never use root). Admin access for the
   operator via an IAM Identity Center user (`aws sso login` — short-lived
   creds; no long-lived access keys). Terraform runs as this identity —
   no separate Terraform user. Pick the region; set a billing alarm.
2. Register/choose the domain; hosted zone or CNAME access.
3. Bootstrap the state bucket + lock table.
4. Nothing else by hand: the GitHub OIDC provider + CI deploy role
   (scoped to ECR push + `ecs:UpdateService`, trust-limited to this repo)
   are Terraform resources, as are all task/service roles. No IAM users
   with static keys anywhere.

## CI/CD (GitHub Actions)

App deploys are CI, not Terraform:

```
push to main
  → run tests
  → docker build; push to ECR tagged :sha (and :latest)
  → aws ecs update-service --force-new-deployment
```

Auth via GitHub OIDC → IAM role (no long-lived AWS keys in GitHub).
Terraform runs separately (locally at first; a plan/apply workflow later).

## Migrations

Applied on container startup (Alembic, or numbered `.sql` files applied in
order) before the server binds. Use a Postgres advisory lock so two starting
tasks can't race. Local and prod schemas come from the same migration files —
never hand-edit either database.

## Local development

`docker compose up` in the ingestion repo provides:

```yaml
services:
  db:
    image: postgres:16        # pin to the RDS major version
    environment: { POSTGRES_PASSWORD: dev }
    ports: ["5432:5432"]
    volumes: ["pgdata:/var/lib/postgresql/data"]
  minio:
    image: minio/minio
    command: server /data --console-address ":9001"
    ports: ["9000:9000", "9001:9001"]
    volumes: ["miniodata:/data"]
  createbucket:                # one-shot: MinIO has no bucket until created
    image: minio/mc
    depends_on: [minio]
    entrypoint: >
      /bin/sh -c "mc alias set local http://minio:9000 minioadmin minioadmin
      && mc mb --ignore-existing local/luminque-dev"
volumes:
  pgdata:
  miniodata:
```

A gitignored `.env` supplies `DATABASE_URL`, `S3_ENDPOINT_URL`
(→ `http://localhost:9000`), `S3_BUCKET`, and the MinIO dev keys. In prod,
`S3_ENDPOINT_URL` is unset (boto3 defaults to real S3) and auth is the task's
IAM role — same code path, different env. Screenshots land in the MinIO
volume, one file per object mirroring the S3 key layout; browse them at
`localhost:9001` or via `aws s3 ls --endpoint-url http://localhost:9000`.

## Security baseline

- Encryption in transit (TLS everywhere, agents use `verify=True`) and at
  rest (SSE-S3, RDS encryption). Customer-managed KMS keys are a later
  upsell; client-side encryption is not worth its complexity.
- Least privilege: ingestion writes S3 but cannot read; discovery reads but
  cannot write; bucket blocks public access; no static credentials anywhere
  (IAM roles + OIDC).
- **Retention is a feature**: screenshots are deleted N days after discovery
  processes them (S3 lifecycle + a scheduled sweep of Postgres rows). Pick N
  before the first customer asks.
- Postgres is sensitive too: `window_title` leaks document names, email
  subjects, patient names. Same handling standard as the pixels.
- Screenshots are never synced to laptops; discovery runs in-account and
  only its outputs leave.
- LLM calls go to Bedrock (in-perimeter). Any external LLM API requires
  zero-data-retention terms and a subprocessor-list entry.

## Suggested implementation split (one AI session each)

1. **Ingestion service** (new repo/dir): FastAPI (or similar) app
   implementing `luminque-ingestion-p1.md` — three endpoints, migrations,
   thin storage module, docker-compose, tests against local Postgres+MinIO.
   No AWS needed.
2. **Terraform + CI** (this doc): after the human prerequisites above.
   Deliverable: `terraform apply` produces a live HTTPS endpoint running the
   container; CI green-path deploy works.
3. **Sender rework** (agent repo): the "Sender changes required" section of
   `luminque-ingestion-p1.md`, validated against the deployed (or local)
   ingestion service.

## Later / open

- Discovery service compute (manual Bedrock-based runs for now).
- Terraform plan/apply CI workflow; multi-env (staging/prod).
- Single-tenant/customer-VPC packaging of the Terraform module.
- Retention value of N; per-tenant overrides.
- Viewing UI hosting + `User` auth.
