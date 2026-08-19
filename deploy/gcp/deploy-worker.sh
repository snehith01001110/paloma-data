#!/usr/bin/env bash
set -euo pipefail

: "${GCP_PROJECT_ID:?GCP_PROJECT_ID is required}"
: "${GCP_REGION:?GCP_REGION is required}"
: "${GCP_ARTIFACT_REPOSITORY:?GCP_ARTIFACT_REPOSITORY is required}"
: "${GCP_RUNTIME_SERVICE_ACCOUNT:?GCP_RUNTIME_SERVICE_ACCOUNT is required}"
: "${GCP_DB_SECRET:?GCP_DB_SECRET is required}"
: "${GCP_YELP_SECRET:?GCP_YELP_SECRET is required}"
: "${SUPABASE_DB_URL:?SUPABASE_DB_URL is required}"
: "${YELP_API_KEY:?YELP_API_KEY is required}"

PALOMA_WORKER_JOB="${PALOMA_WORKER_JOB:-paloma-pipeline-worker}"
PALOMA_IMAGE_NAME="${PALOMA_IMAGE_NAME:-paloma-data}"
PALOMA_IMAGE_TAG="${PALOMA_IMAGE_TAG:-${GITHUB_SHA:-$(git rev-parse HEAD)}}"
script_directory="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repository_root="$(cd -- "${script_directory}/../.." && pwd)"
image_repository="${GCP_REGION}-docker.pkg.dev/${GCP_PROJECT_ID}/${GCP_ARTIFACT_REPOSITORY}/${PALOMA_IMAGE_NAME}"
tagged_image="${image_repository}:${PALOMA_IMAGE_TAG}"

gcloud auth configure-docker "${GCP_REGION}-docker.pkg.dev" --quiet

image_digest="$(
  gcloud artifacts docker images describe "${tagged_image}" \
    --project="${GCP_PROJECT_ID}" \
    --format='value(image_summary.digest)' 2>/dev/null || true
)"
if [[ -z "${image_digest}" ]]; then
  if command -v docker >/dev/null 2>&1; then
    docker build \
      --pull \
      --label="org.opencontainers.image.revision=${PALOMA_IMAGE_TAG}" \
      --tag="${tagged_image}" \
      "${repository_root}"
    docker push "${tagged_image}"
  else
    gcloud builds submit "${repository_root}" \
      --project="${GCP_PROJECT_ID}" \
      --tag="${tagged_image}" \
      --quiet
  fi
  image_digest="$(
    gcloud artifacts docker images describe "${tagged_image}" \
      --project="${GCP_PROJECT_ID}" \
      --format='value(image_summary.digest)'
  )"
fi
if [[ -z "${image_digest}" ]]; then
  echo "Artifact Registry did not return an image digest" >&2
  exit 1
fi

db_secret_version_resource="$(
  printf '%s' "${SUPABASE_DB_URL}" | gcloud secrets versions add "${GCP_DB_SECRET}" \
    --project="${GCP_PROJECT_ID}" \
    --data-file=- \
    --format='value(name)'
)"
yelp_secret_version_resource="$(
  printf '%s' "${YELP_API_KEY}" | gcloud secrets versions add "${GCP_YELP_SECRET}" \
    --project="${GCP_PROJECT_ID}" \
    --data-file=- \
    --format='value(name)'
)"
db_secret_version="${db_secret_version_resource##*/}"
yelp_secret_version="${yelp_secret_version_resource##*/}"

gcloud run jobs deploy "${PALOMA_WORKER_JOB}" \
  --project="${GCP_PROJECT_ID}" \
  --region="${GCP_REGION}" \
  --image="${image_repository}@${image_digest}" \
  --service-account="${GCP_RUNTIME_SERVICE_ACCOUNT}" \
  --tasks=1 \
  --parallelism=1 \
  --max-retries=0 \
  --task-timeout=3600s \
  --cpu=1 \
  --memory=1Gi \
  --args="pipeline-worker,--drain,--max-jobs,5000,--batch-size,10,--visibility-seconds,900,--poll-seconds,2,--fail-on-error" \
  --env-vars-file="${script_directory}/worker-env.yaml" \
  --set-secrets="SUPABASE_DB_URL=${GCP_DB_SECRET}:${db_secret_version},YELP_API_KEY=${GCP_YELP_SECRET}:${yelp_secret_version}" \
  --labels="app=paloma-data,component=pipeline-worker,managed-by=github-actions" \
  --execute-now \
  --wait

echo "Deployed ${PALOMA_WORKER_JOB} at ${image_repository}@${image_digest}"
