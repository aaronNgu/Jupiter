# Luminque infrastructure

Terraform for everything in `design-docs/luminque-infra-p1.md`: VPC, RDS
Postgres 16, S3 (SSE, retention lifecycle), ECR, ECS Fargate service behind an
ALB, Secrets Manager `DATABASE_URL`, the GitHub OIDC deploy role, and the
EventBridge retention-sweep placeholder. App deploys are CI
(`.github/workflows/deploy.yml`), not Terraform.

Prereqs: `terraform >= 1.7`, AWS CLI with a live SSO session
(`aws sso login --profile kangaroo`; `export AWS_PROFILE=kangaroo`), Docker.

## Bootstrap (once per AWS account)

The state bucket and lock table are the only resources not managed by
Terraform. Create them and generate `backend.hcl`:

```sh
./scripts/bootstrap-state.sh          # idempotent; writes terraform/backend.hcl
terraform init -backend-config=backend.hcl
```

`backend.hcl` is gitignored (it embeds the account id); re-run the script on
a new machine to regenerate it.

## First deploy (chicken-and-egg)

The ECS service can't start before an image exists in ECR, so the first
rollout is split:

```sh
# 1. ECR only
terraform apply -target=aws_ecr_repository.ingestion

# 2. Build and push the first image BY HAND (CI takes over afterwards).
#    --platform matters: Fargate task is X86_64, Apple Silicon builds arm64
#    by default. A mismatch = instant crash-loop with no useful logs.
aws ecr get-login-password | docker login --username AWS \
  --password-stdin "$(terraform output -raw ecr_repository_url | cut -d/ -f1)"
docker buildx build --platform linux/amd64 \
  -t "$(terraform output -raw ecr_repository_url):latest" --push ../ingestion

# 3. Everything else
terraform apply

# 4. Verify (no domain yet → plain HTTP against the ALB)
curl -i "$(terraform output -raw api_url)/healthz"          # expect 200
aws ecs describe-services --cluster luminque --services luminque-ingestion \
  --query 'services[0].deployments'                          # 1 deployment, runningCount 1
```

Step 2's image must include `GET /healthz` — the ALB health-checks it and
recycles tasks that don't answer 200.

After the first apply, every push to `main` deploys automatically: tests →
build → push `:sha` + `:latest` → `ecs update-service --force-new-deployment`.
CI authenticates via GitHub OIDC assuming `luminque-ci-deploy` (an output;
must match `AWS_ROLE_ARN` in the workflow). No AWS keys exist in GitHub.

## Day-2 notes

- **Task-definition changes** (env vars, CPU/memory): the service has
  `ignore_changes = [task_definition]` so applies never roll back CI deploys.
  After such an apply, roll out the new revision explicitly:
  `aws ecs update-service --cluster luminque --service luminque-ingestion
  --task-definition luminque-ingestion --force-new-deployment`
- **Adding the domain later** (none configured yet — the ALB serves plain
  HTTP, so don't enroll real agents until this is done): set `domain_name`
  (+ `hosted_zone_id` if the zone is in Route53) in a `*.tfvars` file and
  apply. That creates the ACM cert for `api.<domain>`, the :443 listener, and
  flips :80 to a 301 redirect. With external DNS, create the CNAMEs from the
  `acm_validation_records` output while the apply waits on validation, then
  point `api.<domain>` at `alb_dns_name`.
- **Retention**: S3 objects expire after `retention_days` (default 30). The
  EventBridge rule for the Postgres row sweep exists but is DISABLED with a
  no-op command until the sweep is implemented.
- **Logs**: `/ecs/luminque-ingestion` in CloudWatch (30-day retention).

## Destroy

```sh
terraform destroy
```

Notes: the S3 bucket must be empty first
(`aws s3 rm "s3://$(terraform output -raw s3_bucket)" --recursive`); RDS is
destroyed **without** a final snapshot while `db_skip_final_snapshot=true`
(the phase-1 default — flip it before there's customer data). The state
bucket/lock table are not Terraform-managed; delete them by hand last, if at
all. The ECR repo will refuse to delete while images remain
(`aws ecr batch-delete-image` or empty it in the console).
