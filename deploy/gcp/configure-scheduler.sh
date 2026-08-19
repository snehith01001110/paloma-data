#!/usr/bin/env bash
set -euo pipefail

GCP_PROJECT_ID="${GCP_PROJECT_ID:-paloma-506006}"
GCP_REGION="${GCP_REGION:-us-west1}"
PALOMA_WORKER_JOB="${PALOMA_WORKER_JOB:-paloma-pipeline-worker}"
PALOMA_SCHEDULER_JOB="${PALOMA_SCHEDULER_JOB:-paloma-pipeline-worker-daily}"
GCP_SCHEDULER_SERVICE_ACCOUNT="${GCP_SCHEDULER_SERVICE_ACCOUNT:-paloma-pipeline-scheduler@${GCP_PROJECT_ID}.iam.gserviceaccount.com}"

if ! gcloud run jobs describe "${PALOMA_WORKER_JOB}" \
  --project="${GCP_PROJECT_ID}" \
  --region="${GCP_REGION}" >/dev/null 2>&1; then
  echo "Cloud Run job does not exist: ${PALOMA_WORKER_JOB}" >&2
  exit 1
fi

gcloud run jobs add-iam-policy-binding "${PALOMA_WORKER_JOB}" \
  --project="${GCP_PROJECT_ID}" \
  --region="${GCP_REGION}" \
  --member="serviceAccount:${GCP_SCHEDULER_SERVICE_ACCOUNT}" \
  --role=roles/run.invoker >/dev/null

scheduler_uri="https://run.googleapis.com/v2/projects/${GCP_PROJECT_ID}/locations/${GCP_REGION}/jobs/${PALOMA_WORKER_JOB}:run"
scheduler_common=(
  --project="${GCP_PROJECT_ID}"
  --location="${GCP_REGION}"
  --schedule="0 3,15 * * *"
  --time-zone="Etc/UTC"
  --uri="${scheduler_uri}"
  --http-method=POST
  --oauth-service-account-email="${GCP_SCHEDULER_SERVICE_ACCOUNT}"
  --oauth-token-scope="https://www.googleapis.com/auth/cloud-platform"
  --attempt-deadline=180s
  --max-retry-attempts=0
  --description="Drain Paloma's durable pipeline queue every 12 hours"
)

if gcloud scheduler jobs describe "${PALOMA_SCHEDULER_JOB}" \
  --project="${GCP_PROJECT_ID}" \
  --location="${GCP_REGION}" >/dev/null 2>&1; then
  gcloud scheduler jobs update http "${PALOMA_SCHEDULER_JOB}" \
    "${scheduler_common[@]}"
else
  gcloud scheduler jobs create http "${PALOMA_SCHEDULER_JOB}" \
    "${scheduler_common[@]}"
fi

gcloud scheduler jobs resume "${PALOMA_SCHEDULER_JOB}" \
  --project="${GCP_PROJECT_ID}" \
  --location="${GCP_REGION}" >/dev/null 2>&1 || true

echo "Scheduled ${PALOMA_WORKER_JOB} every 12 hours with ${PALOMA_SCHEDULER_JOB}"
