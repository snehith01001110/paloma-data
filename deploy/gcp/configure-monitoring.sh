#!/usr/bin/env bash
set -euo pipefail

GCP_PROJECT_ID="${GCP_PROJECT_ID:-paloma-506006}"
PALOMA_ALERT_EMAIL="${PALOMA_ALERT_EMAIL:?PALOMA_ALERT_EMAIL is required}"
script_directory="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
monitoring_directory="${script_directory}/monitoring"
monitoring_api="https://monitoring.googleapis.com/v3"

required_commands=(curl gcloud jq mktemp)
for required_command in "${required_commands[@]}"; do
  if ! command -v "${required_command}" >/dev/null 2>&1; then
    echo "Required command is unavailable: ${required_command}" >&2
    exit 1
  fi
done

gcp_monitoring_token="$(gcloud auth print-access-token)"
channels_json="$(
  curl --fail --silent --show-error \
    -H "Authorization: Bearer ${gcp_monitoring_token}" \
    "${monitoring_api}/projects/${GCP_PROJECT_ID}/notificationChannels"
)"
channel_name="$(
  jq -r \
    '.notificationChannels[]? | select(.displayName == "Paloma pipeline alerts") | .name' \
    <<<"${channels_json}" | head -n 1
)"
channel_created=false

if [[ -z "${channel_name}" ]]; then
  channel_body="$(
    jq -cn --arg email "${PALOMA_ALERT_EMAIL}" '{
      type: "email",
      displayName: "Paloma pipeline alerts",
      description: "Critical production alerts for the Paloma establishment pipeline",
      labels: {email_address: $email},
      enabled: true,
      userLabels: {application: "paloma", environment: "production"}
    }'
  )"
  channel_response="$(
    curl --fail --silent --show-error \
      -X POST \
      -H "Authorization: Bearer ${gcp_monitoring_token}" \
      -H "Content-Type: application/json" \
      --data "${channel_body}" \
      "${monitoring_api}/projects/${GCP_PROJECT_ID}/notificationChannels"
  )"
  channel_name="$(jq -er '.name' <<<"${channel_response}")"
  channel_created=true
fi

policy_file="$(mktemp)"
cleanup() {
  rm -f "${policy_file}"
}
trap cleanup EXIT

for policy_template in "${monitoring_directory}"/*.json; do
  display_name="$(jq -er '.displayName' "${policy_template}")"
  jq --arg channel "${channel_name}" \
    '.notificationChannels = [$channel]' \
    "${policy_template}" >"${policy_file}"
  policies_json="$(
    gcloud monitoring policies list \
      --project="${GCP_PROJECT_ID}" \
      --format=json
  )"
  existing_policy="$(
    jq -r --arg display_name "${display_name}" \
      '.[] | select(.displayName == $display_name) | .name' \
      <<<"${policies_json}" | head -n 1
  )"
  if [[ -n "${existing_policy}" ]]; then
    gcloud monitoring policies update "${existing_policy}" \
      --project="${GCP_PROJECT_ID}" \
      --policy-from-file="${policy_file}" >/dev/null
  else
    gcloud monitoring policies create \
      --project="${GCP_PROJECT_ID}" \
      --policy-from-file="${policy_file}" >/dev/null
  fi
  echo "Configured alert policy: ${display_name}"
done

if [[ "${channel_created}" == "true" ]]; then
  curl --fail --silent --show-error \
    -X POST \
    -H "Authorization: Bearer ${gcp_monitoring_token}" \
    -H "Content-Type: application/json" \
    --data '{}' \
    "${monitoring_api}/${channel_name}:sendVerificationCode" >/dev/null
  echo "Sent a notification-channel verification code to ${PALOMA_ALERT_EMAIL}"
fi

channel_status="$(
  curl --fail --silent --show-error \
    -H "Authorization: Bearer ${gcp_monitoring_token}" \
    "${monitoring_api}/${channel_name}" \
    | jq -r '.verificationStatus // "verification-required-or-unspecified"'
)"
unset gcp_monitoring_token

echo "Notification channel: ${channel_name} (${channel_status})"
