#!/usr/bin/env bash
# One-time provisioning for the Trino connector's narrow Glue and S3 Iceberg
# suite (the trino_iceberg job in .github/workflows/integration.yml). Run by a
# maintainer whose AWS credentials can administer S3, Glue, IAM and Budgets,
# with Docker running (the seed goes through a local Trino) and gh
# authenticated against the repository.
#
# The exhaustive Trino suite needs none of this: it runs keyless against the
# container pair from scripts/setup_trino_dev.sh, Iceberg included. This suite
# exists to prove the path most real lakehouses take, the Glue catalog and S3,
# reached from CI with no stored key.
#
# What it sets up, keyless wherever the platform allows it:
#   - an S3 bucket with public access blocked, SSE-S3 encryption, a policy that
#     refuses anything but TLS, a lifecycle rule expiring the dbt dev prefix,
#     and one aborting incomplete multipart uploads
#   - two Glue databases located in that bucket: dex_ci (the seeded sources)
#     and dex_ci_dbt_dev (where dbt builds)
#   - an IAM role for CI whose trust policy accepts only GitHub OIDC tokens
#     minted for this repository's trino-iceberg-integration environment. It
#     may read both prefixes and both databases, and write only the dev prefix
#     and tables in dex_ci_dbt_dev: CI can never alter the sources
#   - with --alert-email, a monthly AWS budget alerting at 80% of a small cap,
#     the backstop for anything outside the suite's own statement caps
#   - the seed: scripts/setup_trino_dev.sh --aws starts the local pair with the
#     Glue catalog attached under this machine's AWS session, and
#     scripts/trino_iceberg_seed.sql writes the sources into dex_ci, including
#     the snapshot history the time-travel refusal is tested against
#   - the GitHub environment (deployments restricted to main) carrying the
#     variables the workflow reads; no secret is stored anywhere
#
# Everything account-specific is a parameter, so nothing private lives in this
# script. Idempotent: existing resources are kept, and the policies, the
# lifecycle, the role's permissions and the seed are refreshed.
#
# Usage:
#   scripts/setup_trino_iceberg_ci.sh \
#     [--region us-east-1] [--bucket <name>] [--repo exmergo/dex] \
#     [--environment trino-iceberg-integration] \
#     [--alert-email you@example.com] [--monthly-budget-usd 5] \
#     [--skip-seed] [--skip-github]

set -euo pipefail

REGION="us-east-1"
BUCKET=""
REPO="exmergo/dex"
ENVIRONMENT="trino-iceberg-integration"
ALERT_EMAIL=""
MONTHLY_BUDGET_USD="5"
SKIP_SEED="false"
SKIP_GITHUB="false"
ROLE_NAME="dex-trino-iceberg-integration"
SOURCE_DB="dex_ci"
DEV_DB="dex_ci_dbt_dev"
BUDGET_NAME="dex-trino-iceberg-ci"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --region) REGION="$2"; shift 2 ;;
    --bucket) BUCKET="$2"; shift 2 ;;
    --repo) REPO="$2"; shift 2 ;;
    --environment) ENVIRONMENT="$2"; shift 2 ;;
    --alert-email) ALERT_EMAIL="$2"; shift 2 ;;
    --monthly-budget-usd) MONTHLY_BUDGET_USD="$2"; shift 2 ;;
    --skip-seed) SKIP_SEED="true"; shift ;;
    --skip-github) SKIP_GITHUB="true"; shift ;;
    *) echo "unknown flag: $1" >&2; exit 2 ;;
  esac
done

for tool in aws python3 docker; do
  command -v "$tool" >/dev/null || { echo "$tool is required on PATH" >&2; exit 2; }
done
if [[ "$SKIP_GITHUB" != "true" ]]; then
  command -v gh >/dev/null || { echo "gh is required (or pass --skip-github)" >&2; exit 2; }
fi

export AWS_DEFAULT_REGION="$REGION"
export AWS_REGION="$REGION"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
BUCKET="${BUCKET:-dex-trino-iceberg-ci-${ACCOUNT_ID}}"
SCRIPTS="$(cd "$(dirname "$0")" && pwd)"

echo "==> bucket s3://$BUCKET (private, encrypted, TLS only)"
if ! aws s3api head-bucket --bucket "$BUCKET" >/dev/null 2>&1; then
  if [[ "$REGION" == "us-east-1" ]]; then
    aws s3api create-bucket --bucket "$BUCKET" >/dev/null
  else
    aws s3api create-bucket --bucket "$BUCKET" \
      --create-bucket-configuration "LocationConstraint=$REGION" >/dev/null
  fi
fi
aws s3api put-public-access-block --bucket "$BUCKET" \
  --public-access-block-configuration \
  "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"
aws s3api put-bucket-encryption --bucket "$BUCKET" \
  --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
aws s3api put-bucket-policy --bucket "$BUCKET" --policy "$(python3 - "$BUCKET" <<'PY'
import json, sys
bucket = sys.argv[1]
print(json.dumps({
    "Version": "2012-10-17",
    "Statement": [{
        "Sid": "DenyInsecureTransport",
        "Effect": "Deny",
        "Principal": "*",
        "Action": "s3:*",
        "Resource": [f"arn:aws:s3:::{bucket}", f"arn:aws:s3:::{bucket}/*"],
        "Condition": {"Bool": {"aws:SecureTransport": "false"}},
    }],
}))
PY
)"
# dbt rebuilds its dev tables on every run, so anything older than a month in
# the dev prefix is debris from a run that died; the sources are left alone.
aws s3api put-bucket-lifecycle-configuration --bucket "$BUCKET" \
  --lifecycle-configuration "$(python3 - "$DEV_DB" <<'PY'
import json, sys
dev = sys.argv[1]
print(json.dumps({
    "Rules": [
        {
            "ID": "expire-dbt-dev",
            "Status": "Enabled",
            "Filter": {"Prefix": f"{dev}/"},
            "Expiration": {"Days": 30},
        },
        {
            "ID": "abort-incomplete-multipart",
            "Status": "Enabled",
            "Filter": {"Prefix": ""},
            "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1},
        },
    ]
}))
PY
)"

echo "==> Glue databases $SOURCE_DB and $DEV_DB"
for db in "$SOURCE_DB" "$DEV_DB"; do
  if ! aws glue get-database --name "$db" >/dev/null 2>&1; then
    aws glue create-database --database-input \
      "{\"Name\":\"$db\",\"LocationUri\":\"s3://$BUCKET/$db/\"}" >/dev/null
  fi
done

echo "==> GitHub OIDC provider + the CI role (trust pinned to repo + environment)"
OIDC_ARN="arn:aws:iam::${ACCOUNT_ID}:oidc-provider/token.actions.githubusercontent.com"
if ! aws iam get-open-id-connect-provider --open-id-connect-provider-arn "$OIDC_ARN" >/dev/null 2>&1; then
  aws iam create-open-id-connect-provider \
    --url https://token.actions.githubusercontent.com \
    --client-id-list sts.amazonaws.com >/dev/null
fi
TRUST=$(python3 - "$OIDC_ARN" "$REPO" "$ENVIRONMENT" <<'PY'
import json, sys
oidc, repo, environment = sys.argv[1:4]
print(json.dumps({
    "Version": "2012-10-17",
    "Statement": [{
        "Effect": "Allow",
        "Principal": {"Federated": oidc},
        "Action": "sts:AssumeRoleWithWebIdentity",
        "Condition": {
            "StringEquals": {
                "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
                "token.actions.githubusercontent.com:sub":
                    f"repo:{repo}:environment:{environment}",
            }
        },
    }],
}))
PY
)
if aws iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1; then
  aws iam update-assume-role-policy --role-name "$ROLE_NAME" \
    --policy-document "$TRUST"
else
  aws iam create-role --role-name "$ROLE_NAME" \
    --assume-role-policy-document "$TRUST" >/dev/null
fi
POLICY=$(python3 - "$BUCKET" "$REGION" "$ACCOUNT_ID" "$SOURCE_DB" "$DEV_DB" <<'PY'
import json, sys
bucket, region, account, source, dev = sys.argv[1:6]
glue = f"arn:aws:glue:{region}:{account}"
print(json.dumps({
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid": "ListTheBucket",
            "Effect": "Allow",
            "Action": ["s3:ListBucket", "s3:GetBucketLocation"],
            "Resource": f"arn:aws:s3:::{bucket}",
        },
        {
            "Sid": "ReadSourcesAndDev",
            "Effect": "Allow",
            "Action": ["s3:GetObject"],
            "Resource": [
                f"arn:aws:s3:::{bucket}/{source}/*",
                f"arn:aws:s3:::{bucket}/{dev}/*",
            ],
        },
        {
            "Sid": "WriteDevOnly",
            "Effect": "Allow",
            "Action": ["s3:PutObject", "s3:DeleteObject", "s3:AbortMultipartUpload"],
            "Resource": f"arn:aws:s3:::{bucket}/{dev}/*",
        },
        {
            "Sid": "ReadTheCatalog",
            "Effect": "Allow",
            "Action": [
                "glue:GetDatabase",
                "glue:GetDatabases",
                "glue:GetTable",
                "glue:GetTables",
                "glue:GetTableVersions",
            ],
            "Resource": [
                f"{glue}:catalog",
                f"{glue}:database/{source}",
                f"{glue}:database/{dev}",
                f"{glue}:table/{source}/*",
                f"{glue}:table/{dev}/*",
            ],
        },
        {
            "Sid": "ChangeDevTablesOnly",
            "Effect": "Allow",
            "Action": ["glue:CreateTable", "glue:UpdateTable", "glue:DeleteTable"],
            "Resource": [
                f"{glue}:catalog",
                f"{glue}:database/{dev}",
                f"{glue}:table/{dev}/*",
            ],
        },
    ],
}))
PY
)
aws iam put-role-policy --role-name "$ROLE_NAME" \
  --policy-name dex-trino-iceberg-integration --policy-document "$POLICY"
ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${ROLE_NAME}"

if [[ -n "$ALERT_EMAIL" ]]; then
  echo "==> monthly budget alert (\$$MONTHLY_BUDGET_USD, 80% actual)"
  BUDGET=$(python3 - "$BUDGET_NAME" "$MONTHLY_BUDGET_USD" <<'PY'
import json, sys
name, amount = sys.argv[1:3]
print(json.dumps({
    "BudgetName": name,
    "BudgetLimit": {"Amount": amount, "Unit": "USD"},
    "TimeUnit": "MONTHLY",
    "BudgetType": "COST",
}))
PY
)
  NOTIFICATION='{"NotificationType":"ACTUAL","ComparisonOperator":"GREATER_THAN","Threshold":80,"ThresholdType":"PERCENTAGE"}'
  if aws budgets describe-budget --account-id "$ACCOUNT_ID" --budget-name "$BUDGET_NAME" >/dev/null 2>&1; then
    aws budgets update-budget --account-id "$ACCOUNT_ID" --new-budget "$BUDGET"
  else
    aws budgets create-budget --account-id "$ACCOUNT_ID" --budget "$BUDGET" \
      --notifications-with-subscribers \
      "[{\"Notification\":$NOTIFICATION,\"Subscribers\":[{\"SubscriptionType\":\"EMAIL\",\"Address\":\"$ALERT_EMAIL\"}]}]"
  fi
else
  echo "==> budget alert skipped (pass --alert-email to create one)"
fi

if [[ "$SKIP_SEED" != "true" ]]; then
  echo "==> seed $SOURCE_DB through a local Trino with the Glue catalog attached"
  "$SCRIPTS/setup_trino_dev.sh" --aws
  # The tables are replaced rather than the database, which this script owns
  # along with its location.
  docker exec dex-trino trino --user admin --execute "
      DROP TABLE IF EXISTS iceberg_glue.$SOURCE_DB.orders;
      DROP TABLE IF EXISTS iceberg_glue.$SOURCE_DB.customer_profiles"
  docker exec -i dex-trino trino --user admin --catalog iceberg_glue \
    --schema "$SOURCE_DB" <"$SCRIPTS/trino_iceberg_seed.sql"
fi

if [[ "$SKIP_GITHUB" != "true" ]]; then
  echo "==> GitHub environment and variables (no secrets: the job federates)"
  gh api -X PUT "repos/${REPO}/environments/${ENVIRONMENT}" \
    -F "deployment_branch_policy[protected_branches]=false" \
    -F "deployment_branch_policy[custom_branch_policies]=true" >/dev/null
  gh api -X POST "repos/${REPO}/environments/${ENVIRONMENT}/deployment-branch-policies" \
    -f name=main >/dev/null 2>&1 || true
  for pair in \
    "DEX_TEST_TRINO_ROLE_ARN=$ROLE_ARN" \
    "DEX_TEST_TRINO_REGION=$REGION" \
    "DEX_TEST_TRINO_GLUE_SCHEMA=$SOURCE_DB" \
    "DEX_TEST_TRINO_ICEBERG_BUCKET=$BUCKET"; do
    gh variable set "${pair%%=*}" --env "$ENVIRONMENT" --repo "$REPO" --body "${pair#*=}"
  done
fi

echo "==> done"
echo "    bucket: s3://$BUCKET"
echo "    role:   $ROLE_ARN"
echo "    local runs: scripts/setup_trino_dev.sh --aws, then"
echo "      DEX_TEST_TRINO_URL=http://dex_ro@127.0.0.1:8085 DEX_TEST_TRINO_GLUE_SCHEMA=$SOURCE_DB \\"
echo "        uv run pytest tests/integration -q -m trino_iceberg"
