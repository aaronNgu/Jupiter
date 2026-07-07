#!/usr/bin/env bash
# One-shot bootstrap of the Terraform state backend (human prerequisite #3).
# These are the only two resources not managed by Terraform itself.
# Idempotent: safe to re-run. Requires a live AWS session (aws sso login).
set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
# The SSO profile may have no default region; make every call below explicit.
export AWS_DEFAULT_REGION="$REGION"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
BUCKET="luminque-tfstate-${ACCOUNT_ID}"
TABLE="luminque-tflock"
HERE="$(cd "$(dirname "$0")" && pwd)"

echo "Region:  $REGION"
echo "Bucket:  $BUCKET"
echo "Table:   $TABLE"

if aws s3api head-bucket --bucket "$BUCKET" 2>/dev/null; then
  echo "Bucket already exists — skipping create."
else
  if [ "$REGION" = "us-east-1" ]; then
    aws s3api create-bucket --bucket "$BUCKET" --region "$REGION"
  else
    aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" \
      --create-bucket-configuration "LocationConstraint=$REGION"
  fi
fi

aws s3api put-bucket-versioning --bucket "$BUCKET" \
  --versioning-configuration Status=Enabled
aws s3api put-bucket-encryption --bucket "$BUCKET" \
  --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
aws s3api put-public-access-block --bucket "$BUCKET" \
  --public-access-block-configuration \
  'BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true'

if aws dynamodb describe-table --table-name "$TABLE" --region "$REGION" >/dev/null 2>&1; then
  echo "Lock table already exists — skipping create."
else
  aws dynamodb create-table --table-name "$TABLE" --region "$REGION" \
    --attribute-definitions AttributeName=LockID,AttributeType=S \
    --key-schema AttributeName=LockID,KeyType=HASH \
    --billing-mode PAY_PER_REQUEST
  aws dynamodb wait table-exists --table-name "$TABLE" --region "$REGION"
fi

cat > "$HERE/../backend.hcl" <<EOF
bucket         = "$BUCKET"
key            = "luminque/terraform.tfstate"
region         = "$REGION"
dynamodb_table = "$TABLE"
encrypt        = true
EOF

echo
echo "Wrote terraform/backend.hcl. Next: terraform init -backend-config=backend.hcl"
