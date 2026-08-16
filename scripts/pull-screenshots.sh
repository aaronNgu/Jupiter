#!/usr/bin/env bash
# Pull screenshots from the Luminque S3 bucket for local discovery runs.
#
# Usage:
#   ./scripts/pull-screenshots.sh                       # entire bucket
#   ./scripts/pull-screenshots.sh <tenant>/<agent>      # one agent
#   ./scripts/pull-screenshots.sh <tenant>/<agent>/<YYYY-MM-DD>  # one day
#
# Sync is incremental: re-running only downloads new frames.
# Env overrides: AWS_PROFILE (default kangaroo), BUCKET, DEST.
set -euo pipefail

AWS_PROFILE="${AWS_PROFILE:-kangaroo}"
export AWS_PROFILE

if [[ -z "${BUCKET:-}" ]]; then
  account_id=$(aws sts get-caller-identity --query Account --output text)
  BUCKET="luminque-screenshots-${account_id}"
fi

PREFIX="${1:-}"
DEST="${DEST:-$(cd "$(dirname "$0")/.." && pwd)/screenshots}"

src="s3://${BUCKET}"
dest="$DEST"
if [[ -n "$PREFIX" ]]; then
  PREFIX="${PREFIX%/}"
  src="${src}/${PREFIX}"
  dest="${dest}/${PREFIX}"
fi

echo "Syncing ${src} -> ${dest}"
aws s3 sync "$src" "$dest" --exclude '*' --include '*.png'

count=$(find "$dest" -name '*.png' | wc -l | tr -d ' ')
echo "Done: ${count} PNGs in ${dest}"
