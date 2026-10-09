#!/usr/bin/env bash
# Deploy the backend to Cloud Run with a Cloud SQL (PostgreSQL) database, from Google Cloud Shell.
#
#   bash backend/deploy/cloud-shell-deploy.sh            # run from the repository root
#
# Optional settings (environment variables):
#   PROJECT=my-project-id   default: the project selected in Cloud Shell
#   REGION=europe-west1     Cloud Run and Cloud SQL region
#   ADMIN_EMAIL=me@x.com    the seeded admin account; default: your Google account
#
# Safe to run again: it creates only what is missing, then redeploys the current code.
# It never changes existing secrets: rotating SECRET_KEY would make the stored API keys unreadable.
set -euo pipefail

PROJECT=${PROJECT:-$(gcloud config get-value project 2>/dev/null || true)}
REGION=${REGION:-europe-west1}
ADMIN_EMAIL=${ADMIN_EMAIL:-$(gcloud config get-value account 2>/dev/null || true)}
SERVICE=meetings-api
INSTANCE=meetings-db
DB_NAME=meetings
DB_USER=meetings
RUNTIME_SA_NAME=meetings-api

step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
fail() { printf '\033[31mError: %s\033[0m\n' "$*" >&2; exit 1; }
random() { python3 -c "import secrets; print(secrets.token_urlsafe($1), end='')"; }
secret_exists() { gcloud secrets describe "$1" --project "$PROJECT" >/dev/null 2>&1; }
create_secret() { printf '%s' "$2" | gcloud secrets create "$1" --project "$PROJECT" --data-file=- --quiet >/dev/null; }

[ -f backend/Dockerfile ] || fail "run this from the repository root (the folder that contains backend/)"
[ -n "$PROJECT" ] || fail "no project selected. Run: PROJECT=your-project-id bash $0   (see: gcloud projects list)"
[ -n "$ADMIN_EMAIL" ] || fail "set ADMIN_EMAIL=you@example.com"
gcloud config set project "$PROJECT" >/dev/null 2>&1
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')
RUNTIME_SA="$RUNTIME_SA_NAME@$PROJECT.iam.gserviceaccount.com"
BUILD_SA="$PROJECT_NUMBER-compute@developer.gserviceaccount.com"
echo "Project $PROJECT, region $REGION, admin $ADMIN_EMAIL"

step "Enabling APIs (needs billing enabled on the project)"
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  secretmanager.googleapis.com sqladmin.googleapis.com iam.googleapis.com --project "$PROJECT" \
  || fail "could not enable APIs. Link a billing account: https://console.cloud.google.com/billing/linkedaccount?project=$PROJECT"

step "Service accounts"
if ! gcloud iam service-accounts describe "$RUNTIME_SA" --project "$PROJECT" >/dev/null 2>&1; then
  gcloud iam service-accounts create "$RUNTIME_SA_NAME" --project "$PROJECT" --display-name "Meetings API"
  sleep 10 # let the new account propagate before granting it roles
fi
gcloud projects add-iam-policy-binding "$PROJECT" --member "serviceAccount:$RUNTIME_SA" \
  --role roles/cloudsql.client --condition None --quiet >/dev/null
# Source deploys build with the default compute account; new projects don't give it the rights.
gcloud projects add-iam-policy-binding "$PROJECT" --member "serviceAccount:$BUILD_SA" \
  --role roles/run.builder --condition None --quiet >/dev/null

step "Cloud SQL instance $INSTANCE (the first run takes about 10 minutes)"
if ! gcloud sql instances describe "$INSTANCE" --project "$PROJECT" >/dev/null 2>&1; then
  gcloud sql instances create "$INSTANCE" --project "$PROJECT" --region "$REGION" \
    --database-version POSTGRES_16 --edition ENTERPRISE --tier db-f1-micro \
    --storage-size 10 --storage-auto-increase --backup-start-time 02:00
fi
CONNECTION=$(gcloud sql instances describe "$INSTANCE" --project "$PROJECT" --format='value(connectionName)')
if ! gcloud sql databases describe "$DB_NAME" --instance "$INSTANCE" --project "$PROJECT" >/dev/null 2>&1; then
  gcloud sql databases create "$DB_NAME" --instance "$INSTANCE" --project "$PROJECT"
fi

step "Secrets"
if ! secret_exists meetings-database-url; then
  DB_PASSWORD=$(random 32)
  if gcloud sql users list --instance "$INSTANCE" --project "$PROJECT" --format='value(name)' | grep -qx "$DB_USER"; then
    gcloud sql users set-password "$DB_USER" --instance "$INSTANCE" --project "$PROJECT" --password "$DB_PASSWORD"
  else
    gcloud sql users create "$DB_USER" --instance "$INSTANCE" --project "$PROJECT" --password "$DB_PASSWORD"
  fi
  # Cloud Run reaches Cloud SQL over a Unix socket mounted at /cloudsql/<connection name>.
  create_secret meetings-database-url \
    "postgresql+psycopg://$DB_USER:$DB_PASSWORD@/$DB_NAME?host=/cloudsql/$CONNECTION"
  unset DB_PASSWORD
fi
secret_exists meetings-secret-key || create_secret meetings-secret-key "$(random 48)"
secret_exists meetings-admin-password || create_secret meetings-admin-password "$(random 18)"
for s in meetings-database-url meetings-secret-key meetings-admin-password; do
  gcloud secrets add-iam-policy-binding "$s" --project "$PROJECT" --member "serviceAccount:$RUNTIME_SA" \
    --role roles/secretmanager.secretAccessor --quiet >/dev/null
done

step "Building and deploying $SERVICE (about 5 minutes)"
gcloud run deploy "$SERVICE" --source backend --project "$PROJECT" --region "$REGION" --quiet \
  --service-account "$RUNTIME_SA" --allow-unauthenticated \
  --add-cloudsql-instances "$CONNECTION" \
  --no-cpu-throttling --cpu 1 --memory 1Gi \
  --concurrency 10 --min-instances 0 --max-instances 3 --timeout 900 \
  --set-env-vars "ENVIRONMENT=production,ADMIN_EMAIL=$ADMIN_EMAIL" \
  --set-secrets "DATABASE_URL=meetings-database-url:latest,SECRET_KEY=meetings-secret-key:latest,ADMIN_PASSWORD=meetings-admin-password:latest"

URL=$(gcloud run services describe "$SERVICE" --project "$PROJECT" --region "$REGION" --format='value(status.url)')
step "Checking $URL/healthz"
for _ in $(seq 1 10); do
  if curl -fsS "$URL/healthz" >/dev/null 2>&1; then HEALTH=ok; break; fi
  sleep 6
done
[ "${HEALTH:-}" = ok ] || fail "the service did not answer. Logs: gcloud run services logs read $SERVICE --region $REGION --limit 50"

ADMIN_PASSWORD=$(gcloud secrets versions access latest --secret meetings-admin-password --project "$PROJECT")
cat <<EOF

Deployed.

  API / app server address : $URL
  Admin panel              : $URL/panel/
  Admin email              : $ADMIN_EMAIL
  Admin password (initial) : $ADMIN_PASSWORD

Next: sign in to the admin panel, open Settings, paste the Groq (transcription) and
DeepSeek (summaries) API keys and save. The initial password stays in Secret Manager
(meetings-admin-password); running this script again prints it again.
In the Android app, enter $URL as the server address.
EOF
