#!/usr/bin/env bash
set -euo pipefail

GCP_PROJECT_ID="${GCP_PROJECT_ID:-paloma-506006}"
GCP_REGION="${GCP_REGION:-us-west1}"
GCP_ARTIFACT_REPOSITORY="${GCP_ARTIFACT_REPOSITORY:-paloma-pipeline}"
GCP_WORKLOAD_POOL="${GCP_WORKLOAD_POOL:-paloma-github}"
GCP_WORKLOAD_PROVIDER="${GCP_WORKLOAD_PROVIDER:-paloma-data}"
GCP_DB_SECRET="${GCP_DB_SECRET:-paloma-db-url}"
GCP_YELP_SECRET="${GCP_YELP_SECRET:-paloma-yelp-api-key}"
GCP_RUNTIME_SERVICE_ACCOUNT_ID="${GCP_RUNTIME_SERVICE_ACCOUNT_ID:-paloma-pipeline-worker}"
GCP_DEPLOY_SERVICE_ACCOUNT_ID="${GCP_DEPLOY_SERVICE_ACCOUNT_ID:-paloma-github-deployer}"
GCP_SCHEDULER_SERVICE_ACCOUNT_ID="${GCP_SCHEDULER_SERVICE_ACCOUNT_ID:-paloma-pipeline-scheduler}"
PALOMA_GITHUB_REPOSITORY="${PALOMA_GITHUB_REPOSITORY:-snehith01001110/paloma-data}"
PALOMA_GITHUB_REPOSITORY_ID="${PALOMA_GITHUB_REPOSITORY_ID:-1337595710}"
PALOMA_GITHUB_OWNER_ID="${PALOMA_GITHUB_OWNER_ID:-92058509}"
PALOMA_GITHUB_WORKFLOW_REF="${PALOMA_GITHUB_WORKFLOW_REF:-snehith01001110/paloma-data/.github/workflows/sync.yml@refs/heads/main}"

required_commands=(gcloud gh)
for required_command in "${required_commands[@]}"; do
  if ! command -v "${required_command}" >/dev/null 2>&1; then
    echo "Required command is unavailable: ${required_command}" >&2
    exit 1
  fi
done

active_project="$(gcloud config get-value project 2>/dev/null)"
if [[ "${active_project}" != "${GCP_PROJECT_ID}" ]]; then
  echo "Expected active gcloud project ${GCP_PROJECT_ID}; found ${active_project}" >&2
  exit 1
fi

billing_enabled="$(
  gcloud billing projects describe "${GCP_PROJECT_ID}" \
    --format='value(billingEnabled)'
)"
if [[ "${billing_enabled}" != "True" && "${billing_enabled}" != "true" ]]; then
  echo "Billing is not enabled for ${GCP_PROJECT_ID}" >&2
  exit 1
fi

GCP_PROJECT_NUMBER="$(
  gcloud projects describe "${GCP_PROJECT_ID}" --format='value(projectNumber)'
)"
GCP_RUNTIME_SERVICE_ACCOUNT="${GCP_RUNTIME_SERVICE_ACCOUNT_ID}@${GCP_PROJECT_ID}.iam.gserviceaccount.com"
GCP_DEPLOY_SERVICE_ACCOUNT="${GCP_DEPLOY_SERVICE_ACCOUNT_ID}@${GCP_PROJECT_ID}.iam.gserviceaccount.com"
GCP_SCHEDULER_SERVICE_ACCOUNT="${GCP_SCHEDULER_SERVICE_ACCOUNT_ID}@${GCP_PROJECT_ID}.iam.gserviceaccount.com"

required_services=(
  artifactregistry.googleapis.com
  cloudscheduler.googleapis.com
  iamcredentials.googleapis.com
  logging.googleapis.com
  monitoring.googleapis.com
  run.googleapis.com
  secretmanager.googleapis.com
  sts.googleapis.com
)
gcloud services enable "${required_services[@]}" --project="${GCP_PROJECT_ID}"

if ! gcloud artifacts repositories describe "${GCP_ARTIFACT_REPOSITORY}" \
  --project="${GCP_PROJECT_ID}" \
  --location="${GCP_REGION}" >/dev/null 2>&1; then
  gcloud artifacts repositories create "${GCP_ARTIFACT_REPOSITORY}" \
    --project="${GCP_PROJECT_ID}" \
    --location="${GCP_REGION}" \
    --repository-format=docker \
    --immutable-tags \
    --description="Immutable Paloma pipeline worker images" \
    --async
  repository_ready=false
  for _ in {1..36}; do
    if gcloud artifacts repositories describe "${GCP_ARTIFACT_REPOSITORY}" \
      --project="${GCP_PROJECT_ID}" \
      --location="${GCP_REGION}" >/dev/null 2>&1; then
      repository_ready=true
      break
    fi
    sleep 5
  done
  if [[ "${repository_ready}" != "true" ]]; then
    echo "Artifact Registry repository did not become ready" >&2
    exit 1
  fi
fi

ensure_service_account() {
  local account_id="$1"
  local display_name="$2"
  local account_email="${account_id}@${GCP_PROJECT_ID}.iam.gserviceaccount.com"
  if ! gcloud iam service-accounts describe "${account_email}" \
    --project="${GCP_PROJECT_ID}" >/dev/null 2>&1; then
    gcloud iam service-accounts create "${account_id}" \
      --project="${GCP_PROJECT_ID}" \
      --display-name="${display_name}"
  fi
}

ensure_service_account "${GCP_RUNTIME_SERVICE_ACCOUNT_ID}" "Paloma pipeline worker runtime"
ensure_service_account "${GCP_DEPLOY_SERVICE_ACCOUNT_ID}" "Paloma GitHub deployer"
ensure_service_account "${GCP_SCHEDULER_SERVICE_ACCOUNT_ID}" "Paloma pipeline scheduler"

ensure_secret() {
  local secret_id="$1"
  if ! gcloud secrets describe "${secret_id}" \
    --project="${GCP_PROJECT_ID}" >/dev/null 2>&1; then
    gcloud secrets create "${secret_id}" \
      --project="${GCP_PROJECT_ID}" \
      --replication-policy=user-managed \
      --locations="${GCP_REGION}"
  fi
}

ensure_secret "${GCP_DB_SECRET}"
ensure_secret "${GCP_YELP_SECRET}"

if ! gcloud iam workload-identity-pools describe "${GCP_WORKLOAD_POOL}" \
  --project="${GCP_PROJECT_ID}" \
  --location=global >/dev/null 2>&1; then
  gcloud iam workload-identity-pools create "${GCP_WORKLOAD_POOL}" \
    --project="${GCP_PROJECT_ID}" \
    --location=global \
    --display-name="Paloma GitHub Actions"
fi

attribute_mapping="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.repository_id=assertion.repository_id,attribute.repository_owner_id=assertion.repository_owner_id,attribute.ref=assertion.ref,attribute.workflow_ref=assertion.workflow_ref,attribute.runner_environment=assertion.runner_environment,attribute.event_name=assertion.event_name"
attribute_condition="assertion.repository == '${PALOMA_GITHUB_REPOSITORY}' && assertion.repository_id == '${PALOMA_GITHUB_REPOSITORY_ID}' && assertion.repository_owner_id == '${PALOMA_GITHUB_OWNER_ID}' && assertion.ref == 'refs/heads/main' && assertion.workflow_ref == '${PALOMA_GITHUB_WORKFLOW_REF}' && assertion.runner_environment == 'github-hosted' && assertion.event_name == 'workflow_dispatch'"

if gcloud iam workload-identity-pools providers describe "${GCP_WORKLOAD_PROVIDER}" \
  --project="${GCP_PROJECT_ID}" \
  --location=global \
  --workload-identity-pool="${GCP_WORKLOAD_POOL}" >/dev/null 2>&1; then
  gcloud iam workload-identity-pools providers update-oidc "${GCP_WORKLOAD_PROVIDER}" \
    --project="${GCP_PROJECT_ID}" \
    --location=global \
    --workload-identity-pool="${GCP_WORKLOAD_POOL}" \
    --issuer-uri="https://token.actions.githubusercontent.com" \
    --attribute-mapping="${attribute_mapping}" \
    --attribute-condition="${attribute_condition}" \
    --display-name="Paloma deployment workflow"
else
  gcloud iam workload-identity-pools providers create-oidc "${GCP_WORKLOAD_PROVIDER}" \
    --project="${GCP_PROJECT_ID}" \
    --location=global \
    --workload-identity-pool="${GCP_WORKLOAD_POOL}" \
    --issuer-uri="https://token.actions.githubusercontent.com" \
    --attribute-mapping="${attribute_mapping}" \
    --attribute-condition="${attribute_condition}" \
    --display-name="Paloma deployment workflow"
fi

GCP_WORKLOAD_IDENTITY_PROVIDER="projects/${GCP_PROJECT_NUMBER}/locations/global/workloadIdentityPools/${GCP_WORKLOAD_POOL}/providers/${GCP_WORKLOAD_PROVIDER}"
workload_principal="principalSet://iam.googleapis.com/projects/${GCP_PROJECT_NUMBER}/locations/global/workloadIdentityPools/${GCP_WORKLOAD_POOL}/attribute.repository/${PALOMA_GITHUB_REPOSITORY}"

gcloud iam service-accounts add-iam-policy-binding "${GCP_DEPLOY_SERVICE_ACCOUNT}" \
  --project="${GCP_PROJECT_ID}" \
  --member="${workload_principal}" \
  --role=roles/iam.workloadIdentityUser >/dev/null

gcloud artifacts repositories add-iam-policy-binding "${GCP_ARTIFACT_REPOSITORY}" \
  --project="${GCP_PROJECT_ID}" \
  --location="${GCP_REGION}" \
  --member="serviceAccount:${GCP_DEPLOY_SERVICE_ACCOUNT}" \
  --role=roles/artifactregistry.writer >/dev/null

for project_role in \
  roles/run.developer \
  roles/run.invoker \
  roles/serviceusage.serviceUsageConsumer; do
  gcloud projects add-iam-policy-binding "${GCP_PROJECT_ID}" \
    --member="serviceAccount:${GCP_DEPLOY_SERVICE_ACCOUNT}" \
    --role="${project_role}" \
    --condition=None >/dev/null
done

gcloud iam service-accounts add-iam-policy-binding "${GCP_RUNTIME_SERVICE_ACCOUNT}" \
  --project="${GCP_PROJECT_ID}" \
  --member="serviceAccount:${GCP_DEPLOY_SERVICE_ACCOUNT}" \
  --role=roles/iam.serviceAccountUser >/dev/null

for secret_id in "${GCP_DB_SECRET}" "${GCP_YELP_SECRET}"; do
  gcloud secrets add-iam-policy-binding "${secret_id}" \
    --project="${GCP_PROJECT_ID}" \
    --member="serviceAccount:${GCP_DEPLOY_SERVICE_ACCOUNT}" \
    --role=roles/secretmanager.secretVersionAdder >/dev/null
  gcloud secrets add-iam-policy-binding "${secret_id}" \
    --project="${GCP_PROJECT_ID}" \
    --member="serviceAccount:${GCP_DEPLOY_SERVICE_ACCOUNT}" \
    --role=roles/secretmanager.viewer >/dev/null
  gcloud secrets add-iam-policy-binding "${secret_id}" \
    --project="${GCP_PROJECT_ID}" \
    --member="serviceAccount:${GCP_RUNTIME_SERVICE_ACCOUNT}" \
    --role=roles/secretmanager.secretAccessor >/dev/null
done

github_variables=(
  "GCP_PROJECT_ID=${GCP_PROJECT_ID}"
  "GCP_PROJECT_NUMBER=${GCP_PROJECT_NUMBER}"
  "GCP_REGION=${GCP_REGION}"
  "GCP_ARTIFACT_REPOSITORY=${GCP_ARTIFACT_REPOSITORY}"
  "GCP_WORKLOAD_IDENTITY_PROVIDER=${GCP_WORKLOAD_IDENTITY_PROVIDER}"
  "GCP_DEPLOY_SERVICE_ACCOUNT=${GCP_DEPLOY_SERVICE_ACCOUNT}"
  "GCP_RUNTIME_SERVICE_ACCOUNT=${GCP_RUNTIME_SERVICE_ACCOUNT}"
  "GCP_SCHEDULER_SERVICE_ACCOUNT=${GCP_SCHEDULER_SERVICE_ACCOUNT}"
  "GCP_DB_SECRET=${GCP_DB_SECRET}"
  "GCP_YELP_SECRET=${GCP_YELP_SECRET}"
)
for github_variable in "${github_variables[@]}"; do
  variable_name="${github_variable%%=*}"
  variable_value="${github_variable#*=}"
  gh variable set "${variable_name}" \
    --repo="${PALOMA_GITHUB_REPOSITORY}" \
    --body="${variable_value}"
done

cat <<SUMMARY
GCP foundation is ready.
Project: ${GCP_PROJECT_ID} (${GCP_PROJECT_NUMBER})
Region: ${GCP_REGION}
Artifact repository: ${GCP_ARTIFACT_REPOSITORY}
Workload identity provider: ${GCP_WORKLOAD_IDENTITY_PROVIDER}
Runtime service account: ${GCP_RUNTIME_SERVICE_ACCOUNT}
Deploy service account: ${GCP_DEPLOY_SERVICE_ACCOUNT}
Scheduler service account: ${GCP_SCHEDULER_SERVICE_ACCOUNT}
SUMMARY
