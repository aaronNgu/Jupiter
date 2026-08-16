#!/usr/bin/env bash
# Create a tenant in the deployed environment and print its enrollment token.
#
# Runs `luminque-create-tenant` as a one-off Fargate task (same image and
# network as the ingestion service — RDS is only reachable from inside the
# VPC) and reads the result back from CloudWatch logs.
#
# Usage:
#   ./scripts/create-tenant.sh "Acme Corp"
#   ./scripts/create-tenant.sh --dry-run "Acme Corp"   # resolve + print, don't run
#
# Needs: aws (live SSO session), jq, terraform (init'd in terraform/).
# Note: the token is printed by the task, so it also lands in CloudWatch
# logs (30-day retention). Acceptable for this credential; rotate by
# updating the tenant row if it ever leaks.
set -euo pipefail

DRY_RUN=false
if [[ "${1:-}" == "--dry-run" ]]; then
  DRY_RUN=true
  shift
fi
if [[ $# -ne 1 || -z "$1" ]]; then
  echo 'usage: create-tenant.sh [--dry-run] "<tenant name>"' >&2
  exit 2
fi
TENANT_NAME=$1

for cmd in aws jq terraform; do
  command -v "$cmd" >/dev/null || { echo "error: $cmd not found" >&2; exit 1; }
done

cd "$(dirname "$0")/.."   # terraform/

echo "==> Resolving deployment from Terraform outputs" >&2
CLUSTER=$(terraform output -raw ecs_cluster)
SERVICE=$(terraform output -raw ecs_service)

# The SSO profile may carry no default region; the ALB DNS name embeds it.
if [[ -z "${AWS_REGION:-}${AWS_DEFAULT_REGION:-}" && -z "$(aws configure get region 2>/dev/null || true)" ]]; then
  AWS_REGION=$(terraform output -raw alb_dns_name | sed -E 's/^[^.]+\.([^.]+)\.elb\.amazonaws\.com$/\1/')
  export AWS_REGION
  echo "==> Using region $AWS_REGION (derived from ALB DNS name)" >&2
fi

SVC=$(aws ecs describe-services --cluster "$CLUSTER" --services "$SERVICE" \
  --query 'services[0].{td: taskDefinition, net: networkConfiguration}')
TASK_DEF=$(jq -r .td <<<"$SVC")
NET_CONF=$(jq -c .net <<<"$SVC")

TD=$(aws ecs describe-task-definition --task-definition "$TASK_DEF" \
  --query 'taskDefinition.containerDefinitions[0].{name: name, log: logConfiguration.options}')
CONTAINER=$(jq -r .name <<<"$TD")
LOG_GROUP=$(jq -r '.log."awslogs-group"' <<<"$TD")
LOG_PREFIX=$(jq -r '.log."awslogs-stream-prefix"' <<<"$TD")

# The image has no venv on PATH; invoke via uv like the image's CMD does.
OVERRIDES=$(jq -nc --arg c "$CONTAINER" --arg name "$TENANT_NAME" \
  '{containerOverrides: [{name: $c, command: ["uv", "run", "--no-sync", "luminque-create-tenant", $name]}]}')

if $DRY_RUN; then
  echo "cluster:    $CLUSTER"
  echo "task def:   $TASK_DEF"
  echo "container:  $CONTAINER"
  echo "log group:  $LOG_GROUP"
  echo "network:    $NET_CONF"
  echo "overrides:  $OVERRIDES"
  exit 0
fi

echo "==> Running one-off task (image pull + migrations take ~a minute)" >&2
TASK_ARN=$(aws ecs run-task \
  --cluster "$CLUSTER" \
  --task-definition "$TASK_DEF" \
  --launch-type FARGATE \
  --network-configuration "$NET_CONF" \
  --overrides "$OVERRIDES" \
  --started-by "create-tenant-script" \
  --query 'tasks[0].taskArn' --output text)
echo "    $TASK_ARN" >&2

aws ecs wait tasks-stopped --cluster "$CLUSTER" --tasks "$TASK_ARN"

EXIT_CODE=$(aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$TASK_ARN" \
  --query 'tasks[0].containers[0].exitCode' --output text)

TASK_ID=${TASK_ARN##*/}
LOG_STREAM="$LOG_PREFIX/$CONTAINER/$TASK_ID"

fetch_logs() {
  aws logs get-log-events --log-group-name "$LOG_GROUP" \
    --log-stream-name "$LOG_STREAM" --start-from-head \
    --query 'events[].message' --output json 2>/dev/null | jq -r '.[]' || true
}

# Log delivery lags task stop by a few seconds.
LOGS=""
for _ in $(seq 1 10); do
  LOGS=$(fetch_logs)
  [[ "$LOGS" == *enrollment_token:* ]] && break
  sleep 3
done

if [[ "$EXIT_CODE" != "0" || "$LOGS" != *enrollment_token:* ]]; then
  echo "error: task exited with code $EXIT_CODE; full output:" >&2
  printf '%s\n' "$LOGS" >&2
  exit 1
fi

grep -E '^(tenant_id|enrollment_token):' <<<"$LOGS"
