#!/bin/bash
# Runs once at startup (the minio-init service). Creates the bucket and a restricted
# account for the Backend, so the app never uses MinIO's root credentials.
set -euo pipefail

export MC_CONFIG_DIR=/tmp/.mc
mc alias set local http://minio:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null

mc mb --ignore-existing "local/$S3_BUCKET"
# Nothing in the bucket is public: every file goes through the Backend's access checks
mc anonymous set none "local/$S3_BUCKET"

cat > /tmp/policy.json <<POLICY
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:ListBucket", "s3:GetBucketLocation"],
      "Resource": ["arn:aws:s3:::$S3_BUCKET"]
    },
    {
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
      "Resource": ["arn:aws:s3:::$S3_BUCKET/*"]
    }
  ]
}
POLICY

mc admin user add local "$S3_ACCESS_KEY" "$S3_SECRET_KEY"
mc admin policy create local maat-app /tmp/policy.json
mc admin policy attach local maat-app --user "$S3_ACCESS_KEY" 2>/dev/null || true

echo "MinIO is ready: bucket '$S3_BUCKET', app user '$S3_ACCESS_KEY'."
