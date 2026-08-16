#!/usr/bin/env bash
# One-off Fargate task that prints the agent table — same mechanism as
# terraform/scripts/create-tenant.sh, different command override.
set -euo pipefail

cd "$(dirname "$0")/.."   # terraform/

CLUSTER=$(terraform output -raw ecs_cluster)
SERVICE=$(terraform output -raw ecs_service)

if [[ -z "${AWS_REGION:-}${AWS_DEFAULT_REGION:-}" && -z "$(aws configure get region 2>/dev/null || true)" ]]; then
  AWS_REGION=$(terraform output -raw alb_dns_name | sed -E 's/^[^.]+\.([^.]+)\.elb\.amazonaws\.com$/\1/')
  export AWS_REGION
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

PYCODE='
import os
from sqlalchemy import create_engine, text
e = create_engine(os.environ["DATABASE_URL"])
with e.connect() as c:
    rows = c.execute(text(
        "SELECT id, hostname, platform, os_version, created_at, last_seen_at "
        "FROM agent ORDER BY created_at DESC LIMIT 10"
    )).fetchall()
print("AGENT_ROWS_BEGIN")
for r in rows:
    print(" | ".join(str(v) for v in r))
print("AGENT_ROWS_END")
'

OVERRIDES=$(jq -nc --arg c "$CONTAINER" --arg py "$PYCODE" \
  '{containerOverrides: [{name: $c, command: ["uv", "run", "--no-sync", "python", "-c", $py]}]}')

echo "==> Running one-off query task" >&2
TASK_ARN=$(aws ecs run-task \
  --cluster "$CLUSTER" \
  --task-definition "$TASK_DEF" \
  --launch-type FARGATE \
  --network-configuration "$NET_CONF" \
  --overrides "$OVERRIDES" \
  --started-by "query-agents-script" \
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

LOGS=""
for _ in $(seq 1 10); do
  LOGS=$(fetch_logs)
  [[ "$LOGS" == *AGENT_ROWS_END* ]] && break
  sleep 3
done

if [[ "$EXIT_CODE" != "0" || "$LOGS" != *AGENT_ROWS_END* ]]; then
  echo "error: task exited with code $EXIT_CODE; full output:" >&2
  printf '%s\n' "$LOGS" >&2
  exit 1
fi

sed -n '/AGENT_ROWS_BEGIN/,/AGENT_ROWS_END/p' <<<"$LOGS"
