# Meetings API

Backend for the meetings app. The Android app uploads a recording; the server transcribes it,
asks an LLM for structured minutes (summary, what happened, decisions, action items, parties),
and stores everything as a **meeting** the user can edit, categorize and export to PDF or Word.

```
APK ──upload──▶ Cloud Run (FastAPI) ──▶ ffmpeg: any format → 16 kHz mono FLAC chunks
                   │                 ──▶ Whisper (Groq, OpenAI-compatible) → transcript
                   │                 ──▶ DeepSeek (OpenAI-compatible) → JSON minutes
                   └──▶ Neon Postgres (users, meetings, attendees, action items, categories)
```

| Piece | Choice | Why |
|---|---|---|
| API | Python 3.12, FastAPI | Typed, auto-generates OpenAPI docs the app can be built against |
| Hosting | Google Cloud Run | Scales to zero, so idle costs nothing |
| Database | [Neon](https://neon.tech) serverless Postgres | Free plan, also scales to zero; plain Postgres, so no lock-in |
| Speech-to-text | Groq's hosted Whisper large-v3 | Strong Arabic, very cheap, has a free tier; swap providers with env vars |
| Summaries | DeepSeek `deepseek-chat` in JSON mode | Cheap; any OpenAI-compatible API works |
| Exports | WeasyPrint (PDF), python-docx (Word) | Proper Arabic shaping and right-to-left layout |

## API

Interactive docs: `GET /docs` (Swagger UI); machine-readable: `GET /openapi.json`.

| Method & path | Purpose |
|---|---|
| `POST /auth/register` · `POST /auth/login` | Returns `access_token` (30 min), `refresh_token` (30 days), `user` |
| `POST /auth/refresh` · `POST /auth/logout` | Rotate / revoke a refresh token (each one works once) |
| `GET/PATCH/DELETE /users/me` · `POST /users/me/password` | Profile, account deletion (required by Google Play), password change |
| `GET/POST /categories` · `PATCH/DELETE /categories/{id}` | User-defined categories with optional color and meeting counts |
| `POST /meetings` | **Multipart upload**: `audio` file + optional `title`, `category_id`, `meeting_date`, `duration_seconds`, `attendees` (JSON list), `language` (`ar`, `en`, `auto`). Returns `202` with status `queued` |
| `GET /meetings` | List with `q` (title + transcript search), `category_id`, `uncategorized`, `status`, `date_from`, `date_to`, `limit`, `offset` |
| `GET/PATCH/DELETE /meetings/{id}` | Full meeting: transcript, summary sections, attendees, action items |
| `POST /meetings/{id}/audio` | Re-upload the recording and reprocess (e.g. transcription failed) |
| `POST /meetings/{id}/summarize` | Regenerate the summary from the stored transcript (e.g. after a prompt change) |
| `POST /meetings/{id}/action-items` · `PATCH/DELETE …/action-items/{item_id}` | To-dos: add, tick off, edit, remove |
| `GET /meetings/{id}/export?format=pdf\|docx&include_transcript=false` | Download minutes |

### Notes for the Android app

- **Processing is asynchronous.** After `POST /meetings`, poll `GET /meetings/{id}` every few
  seconds. Status goes `queued → transcribing → summarizing → completed`, or `failed` with a
  readable `error_message`. If it failed with no `transcript`, retry with `POST /meetings/{id}/audio`;
  if a transcript exists, retry with `POST /meetings/{id}/summarize`.
- **Upload size.** Cloud Run caps request bodies at 32 MiB, so uploads are limited to 30 MB.
  Record speech as AAC, mono, 32–48 kbps (`MediaRecorder` → `.m4a`): about 15–20 MB per hour.
  mp4, m4a, 3gp/amr, ogg/opus, webm, wav and mp3 all work; ffmpeg handles the conversion.
- **Auth.** Keep the refresh token in `EncryptedSharedPreferences`. On a `401`, call
  `/auth/refresh` once and retry; if refresh also returns `401`, send the user to login.
- **Dates** are ISO 8601 in UTC. Send `meeting_date` with a time zone offset.
- A Kotlin/Retrofit client can be generated from `/openapi.json` with openapi-generator.

## Changing the summary format

The "global format" lives in [`app/prompts/meeting_summary.md`](app/prompts/meeting_summary.md):
the system prompt sent before every transcript. Edit the wording, tone, section rules or length
freely. The JSON keys it asks for must match `LLMMeetingSummary` in [`app/schemas.py`](app/schemas.py);
to add a section, add the key to both (and to the export templates in `app/exports/`).
Very long transcripts are first condensed part by part with
[`app/prompts/transcript_notes.md`](app/prompts/transcript_notes.md).

Try prompt changes on a real recording without the app or a database:

```bash
python -m app.cli recording.m4a --title "Weekly sync" --attendee "Ahmed (CFO)"
python -m app.cli --transcript saved-transcript.txt    # summary only
```

Existing meetings pick up a new prompt with `POST /meetings/{id}/summarize`.

## Local development

Needs Python 3.12, ffmpeg and Pango (`sudo apt install ffmpeg fonts-noto-core libpango-1.0-0 libpangoft2-1.0-0`,
or `brew install ffmpeg pango` on macOS).

```bash
cd backend
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env            # add TRANSCRIPTION_API_KEY and LLM_API_KEY
alembic upgrade head            # creates meetings.db (SQLite) locally
uvicorn app.main:create_app --factory --reload
# open http://localhost:8000/docs
pytest                          # uses fake transcription/LLM providers; no keys needed
TEST_DATABASE_URL=postgresql+psycopg://... pytest   # same suite against Postgres
```

Schema changes: edit `app/models.py`, then `alembic revision --autogenerate -m "..."` and review it.

## Deploying to Google Cloud Run

Everything below can be run from [Cloud Shell](https://shell.cloud.google.com) (it has `gcloud`
and `git`), so nothing needs installing locally.

### 1. Accounts and keys

- **Neon**: create a project in a region close to Cloud Run (for `europe-west1`, pick AWS
  Europe Central, Frankfurt) and a database named `meetings`. Copy the **pooled** connection
  string and change `postgresql://` to `postgresql+psycopg://`
  (keep `?sslmode=require`).
- **Groq** (speech-to-text) and **DeepSeek** (summaries): create an API key in each console.

### 2. One-time Google Cloud setup

```bash
PROJECT=your-project-id
REGION=europe-west1
gcloud config set project $PROJECT
gcloud services enable run.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com secretmanager.googleapis.com

# The identity the service runs as
gcloud iam service-accounts create meetings-api --display-name "Meetings API"
RUNTIME_SA=meetings-api@$PROJECT.iam.gserviceaccount.com

# Secrets (typed at a hidden prompt, so they don't land in shell history)
add_secret() { read -rsp "$1: " value && echo && printf '%s' "$value" | gcloud secrets create "$1" --data-file=- && unset value; }
add_secret meetings-database-url            # the Neon postgresql+psycopg://... string
add_secret meetings-transcription-api-key   # Groq key
add_secret meetings-llm-api-key             # DeepSeek key
python3 -c "import secrets; print(secrets.token_urlsafe(48), end='')" \
  | gcloud secrets create meetings-jwt-secret --data-file=-

for s in meetings-database-url meetings-transcription-api-key meetings-llm-api-key meetings-jwt-secret; do
  gcloud secrets add-iam-policy-binding $s --member "serviceAccount:$RUNTIME_SA" \
    --role roles/secretmanager.secretAccessor --quiet
done
```

Also set a budget alert (Billing → Budgets & alerts), e.g. at $5.

### 3. Deploy

From the repository root:

```bash
gcloud run deploy meetings-api --source backend --region $REGION \
  --service-account $RUNTIME_SA --allow-unauthenticated \
  --no-cpu-throttling --cpu 1 --memory 1Gi \
  --concurrency 10 --min-instances 0 --max-instances 3 --timeout 900 \
  --set-env-vars ENVIRONMENT=production \
  --set-secrets DATABASE_URL=meetings-database-url:latest,JWT_SECRET=meetings-jwt-secret:latest,TRANSCRIPTION_API_KEY=meetings-transcription-api-key:latest,LLM_API_KEY=meetings-llm-api-key:latest
```

The command prints the service URL; `GET <url>/healthz` should return `{"status":"ok"}` and
`<url>/docs` shows the API. Database migrations run automatically when the container starts.

Why these flags:
- `--no-cpu-throttling`: the transcription/summary job runs after the upload response is sent;
  without it Cloud Run pauses the CPU between requests and jobs stall.
- `--min-instances 0`: nothing runs (or bills) while the app is idle. The first request after
  a quiet period takes a few seconds longer.
- `--max-instances 3`: a hard ceiling on cost.
- `--allow-unauthenticated`: the API does its own login; Cloud Run must let the app reach it.

### 4. Automatic deploys from GitHub (optional)

[`.github/workflows/backend-deploy.yml`](../.github/workflows/backend-deploy.yml) deploys every
push to `main` that touches `backend/`. It authenticates with Workload Identity Federation, so no
Google key is stored in GitHub. One-time setup:

```bash
REPO=shaaarplegs/external-experiments
PROJECT_NUMBER=$(gcloud projects describe $PROJECT --format 'value(projectNumber)')
gcloud artifacts repositories create meetings --repository-format docker --location $REGION

gcloud iam service-accounts create github-deployer
DEPLOYER=github-deployer@$PROJECT.iam.gserviceaccount.com
for role in roles/run.admin roles/artifactregistry.writer; do
  gcloud projects add-iam-policy-binding $PROJECT --member "serviceAccount:$DEPLOYER" --role $role --quiet
done
gcloud iam service-accounts add-iam-policy-binding $RUNTIME_SA \
  --member "serviceAccount:$DEPLOYER" --role roles/iam.serviceAccountUser

gcloud iam workload-identity-pools create github --location global
gcloud iam workload-identity-pools providers create-oidc github --location global \
  --workload-identity-pool github --issuer-uri https://token.actions.githubusercontent.com \
  --attribute-mapping google.subject=assertion.sub,attribute.repository=assertion.repository \
  --attribute-condition "assertion.repository == '$REPO'"
gcloud iam service-accounts add-iam-policy-binding $DEPLOYER --role roles/iam.workloadIdentityUser \
  --member "principalSet://iam.googleapis.com/projects/$PROJECT_NUMBER/locations/global/workloadIdentityPools/github/attribute.repository/$REPO"

echo "GCP_WORKLOAD_IDENTITY_PROVIDER=projects/$PROJECT_NUMBER/locations/global/workloadIdentityPools/github/providers/github"
echo "GCP_DEPLOY_SERVICE_ACCOUNT=$DEPLOYER"
```

Then in GitHub → Settings → Secrets and variables → Actions → **Variables**, add
`GCP_PROJECT_ID`, `GCP_REGION` and the two values printed above.

## Costs and limits

- **Cloud Run**: billed only while an instance is up (plus up to ~15 idle minutes after the last
  request with `--no-cpu-throttling`); light use stays within the monthly free tier.
- **Neon free plan**: enough for text data (transcripts and summaries are small). Audio is
  never stored: it is deleted right after transcription.
- **Groq / DeepSeek**: pay per audio minute / per token; a one-hour meeting costs cents.
- **Artifact Registry**: the image is about 1 GB uncompressed (mostly ffmpeg's libraries); keep
  only recent images with a cleanup policy.
- Recordings up to 180 minutes (`MAX_AUDIO_MINUTES`) and 30 MB (`MAX_UPLOAD_MB`).

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./meetings.db` | Neon: `postgresql+psycopg://…-pooler…/meetings?sslmode=require` |
| `JWT_SECRET` | dev value | Required in production (≥ 32 chars) |
| `ENVIRONMENT` | `development` | `production` refuses to start with the dev JWT secret |
| `TRANSCRIPTION_API_KEY` | — | Groq key (or OpenAI's, with the two settings below) |
| `TRANSCRIPTION_BASE_URL` | `https://api.groq.com/openai/v1` | Any OpenAI-compatible transcription API |
| `TRANSCRIPTION_MODEL` | `whisper-large-v3` | |
| `TRANSCRIPTION_LANGUAGE` | `ar` | Default hint; the app can send `language` per upload (`auto` to detect) |
| `LLM_API_KEY` | — | DeepSeek key |
| `LLM_BASE_URL` / `LLM_MODEL` | `https://api.deepseek.com` / `deepseek-chat` | Any OpenAI-compatible chat API |
| `LLM_TEMPERATURE` / `LLM_MAX_TOKENS` | `0.3` / `8000` | |
| `ACCESS_TOKEN_TTL_MINUTES` / `REFRESH_TOKEN_TTL_DAYS` | `30` / `30` | |
| `MAX_UPLOAD_MB` / `MAX_AUDIO_MINUTES` | `30` / `180` | |
| `PROCESSING_TIMEOUT_MINUTES` | `30` | A job stuck longer is shown as failed so the user can retry |
| `RUN_MIGRATIONS` | `true` | Run `alembic upgrade head` when the container starts |
