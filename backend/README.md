# Meetings API

Backend for the meetings app. The Android app uploads a recording; the server transcribes it,
asks an LLM for structured minutes (summary, what happened, decisions, action items, parties),
and stores everything as a **meeting** the user can edit, categorize and export to PDF or Word.
Admins manage registered accounts and all provider settings (API keys, models, the summary
prompt, limits) from inside the app; those settings live in the database, not in config files.

```
APK ──upload──▶ Cloud Run (FastAPI) ──▶ ffmpeg: any format → 16 kHz mono FLAC chunks
                   │                 ──▶ Whisper (Groq, OpenAI-compatible) → transcript
                   │                 ──▶ DeepSeek (OpenAI-compatible) → JSON minutes
                   └──▶ PostgreSQL on Neon: users & roles, meetings, attendees, action items,
                        categories, admin settings (API keys encrypted)
```

| Piece | Choice | Why |
|---|---|---|
| API | Python 3.12, FastAPI | Typed, auto-generates OpenAPI docs the app can be built against |
| Hosting | Google Cloud Run | Scales to zero, so idle costs nothing |
| Database | PostgreSQL: [Neon](https://neon.tech) in production, Docker locally | A real server-side database that every Cloud Run instance shares. Cloud Run's own disk is temporary, so nothing is stored there. Neon's free plan scales to zero like Cloud Run |
| Speech-to-text | Groq's hosted Whisper large-v3 | Strong Arabic, very cheap, has a free tier; admins can switch providers |
| Summaries | DeepSeek `deepseek-chat` in JSON mode | Cheap; any OpenAI-compatible API works |
| Exports | WeasyPrint (PDF), python-docx (Word) | Proper Arabic shaping and right-to-left layout |

## API

Interactive docs: `GET /docs` (Swagger UI); machine-readable: `GET /openapi.json`.

| Method & path | Purpose |
|---|---|
| `POST /auth/register` · `POST /auth/login` | Returns `access_token` (30 min), `refresh_token` (30 days), `user` (including `role`) |
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
| `GET/PATCH /admin/settings` | **Admin only.** Provider URLs, models, API keys (write-only, shown masked), summary prompt, limits |
| `GET /admin/users` · `GET/PATCH /admin/users/{id}` | **Admin only.** Registered accounts: search, meeting counts, last login, change role, disable/enable |

### Notes for the Android app

- **Processing is asynchronous.** After `POST /meetings`, poll `GET /meetings/{id}` every few
  seconds. Status goes `queued → transcribing → summarizing → completed`, or `failed` with a
  readable `error_message`. If it failed with no `transcript`, retry with `POST /meetings/{id}/audio`;
  if a transcript exists, retry with `POST /meetings/{id}/summarize`.
- **Upload size.** Cloud Run caps request bodies at 32 MiB; the default upload limit is 30 MB
  (an admin setting). Record speech as AAC, mono, 32–48 kbps (`MediaRecorder` → `.m4a`): about
  15–20 MB per hour. mp4, m4a, 3gp/amr, ogg/opus, webm, wav and mp3 all work.
- **Auth.** Keep the refresh token in `EncryptedSharedPreferences`. On a `401`, call
  `/auth/refresh` once and retry; if refresh also returns `401`, send the user to login.
  A `403` from login means an admin disabled the account.
- **Roles.** `user.role` is `"user"` or `"admin"`; show admin screens only for admins (the
  server enforces it either way).
- **Dates** are ISO 8601 in UTC. Send `meeting_date` with a time zone offset.
- A Kotlin/Retrofit client can be generated from `/openapi.json` with openapi-generator.

## Admins

Every account has a role: `user` (default) or `admin`. Admins can:

- **Configure providers and the summary format** with `GET/PATCH /admin/settings`: base URLs,
  models, API keys, temperature, the summary prompt, and upload limits. A change applies to the
  next upload on every instance; nothing needs redeploying. API keys are encrypted with
  `SECRET_KEY` before they are stored and the API only ever shows their last 4 characters.
  In a PATCH, send only what changes; `null` resets a field to its default (or removes a key).
- **Manage accounts** with `/admin/users`: search, see meeting counts and last login, promote
  to admin, or disable an account (signs it out everywhere immediately). The last active admin
  can't be demoted or disabled.

Until the admin panel exists, Swagger UI at `/docs` works as one: log in with
`POST /auth/login`, click **Authorize**, paste the `access_token`, then use the `admin` endpoints.

**Creating the first admin** (once, after the first deploy): register normally in the app or
with `POST /auth/register`, then promote that account, either in the **Neon console → SQL Editor**:

```sql
UPDATE users SET role = 'admin' WHERE email = 'you@example.com';
```

or with the command-line tool, from any machine with `DATABASE_URL` set:

```bash
python -m app.cli create-admin you@example.com
```

## Changing the summary format

The "global format" is the system prompt sent before every transcript: the `summary_prompt`
admin setting. Its default is [`app/prompts/meeting_summary.md`](app/prompts/meeting_summary.md);
admins can replace it with `PATCH /admin/settings`. Edit the wording, tone, section rules or
length freely. The JSON keys it asks for must match `LLMMeetingSummary` in
[`app/schemas.py`](app/schemas.py); adding a new section is a code change (schema, prompt and
the export templates in `app/exports/`). Very long transcripts are first condensed part by part
with the `notes_prompt` setting.

Try a prompt on a real recording before saving it, without creating a meeting (uses the keys
and models saved in the admin settings):

```bash
python -m app.cli try recording.m4a --prompt-file new-prompt.md --attendee "Ahmed (CFO)"
python -m app.cli try --transcript saved-transcript.txt --prompt-file new-prompt.md
```

Existing meetings pick up a new prompt with `POST /meetings/{id}/summarize`.

## Local development

Needs Docker, Python 3.12, ffmpeg and Pango
(`sudo apt install ffmpeg fonts-noto-core libpango-1.0-0 libpangoft2-1.0-0`, or
`brew install ffmpeg pango` on macOS).

```bash
cd backend
docker compose up -d db         # PostgreSQL on localhost:5432 (plus a meetings_test database)
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env
alembic upgrade head
uvicorn app.main:create_app --factory --reload      # http://localhost:8000/docs
python -m app.cli create-admin you@example.com      # then set the API keys in /admin/settings
pytest                          # uses meetings_test and fake transcription/LLM providers
```

`docker compose up --build` runs the API in Docker too. Schema changes: edit `app/models.py`,
then `alembic revision --autogenerate -m "..."` and review the result.

## Deploying to Google Cloud Run

Everything below can be run from [Cloud Shell](https://shell.cloud.google.com) (it has `gcloud`
and `git`), so nothing needs installing locally.

### 1. Database

Create a [Neon](https://neon.tech) project in a region close to Cloud Run (for `europe-west1`,
pick AWS Europe Central, Frankfurt) and a database named `meetings`. Copy the **pooled**
connection string and change `postgresql://` to `postgresql+psycopg://` (keep `?sslmode=require`).

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

# Two secrets: the Neon URL (typed at a hidden prompt) and a random SECRET_KEY
read -rsp "Neon DATABASE_URL: " DB_URL && echo
printf '%s' "$DB_URL" | gcloud secrets create meetings-database-url --data-file=- && unset DB_URL
python3 -c "import secrets; print(secrets.token_urlsafe(48), end='')" \
  | gcloud secrets create meetings-secret-key --data-file=-

for s in meetings-database-url meetings-secret-key; do
  gcloud secrets add-iam-policy-binding $s --member "serviceAccount:$RUNTIME_SA" \
    --role roles/secretmanager.secretAccessor --quiet
done
```

Keep `meetings-secret-key` as it is: changing it signs everyone out and the provider API keys
would have to be entered again. Also set a budget alert (Billing → Budgets & alerts), e.g. at $5.

### 3. Deploy

From the repository root:

```bash
gcloud run deploy meetings-api --source backend --region $REGION \
  --service-account $RUNTIME_SA --allow-unauthenticated \
  --no-cpu-throttling --cpu 1 --memory 1Gi \
  --concurrency 10 --min-instances 0 --max-instances 3 --timeout 900 \
  --set-env-vars ENVIRONMENT=production \
  --set-secrets DATABASE_URL=meetings-database-url:latest,SECRET_KEY=meetings-secret-key:latest
```

The command prints the service URL; `GET <url>/healthz` should return `{"status":"ok"}`.
Database migrations run automatically when the container starts. Then:

1. Register your account (in the app, or `POST /auth/register` in `<url>/docs`).
2. Make it an admin (see [Admins](#admins)).
3. In `<url>/docs`, `PATCH /admin/settings` with your Groq and DeepSeek keys:
   `{"transcription_api_key": "gsk_…", "llm_api_key": "sk-…"}`.

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
- **Neon free plan**: enough to start; transcripts and summaries are text. Audio is never
  stored: it is deleted right after transcription. Neon keeps the data on its servers with
  backups; Cloud Run itself stores nothing.
- **Groq / DeepSeek**: pay per audio minute / per token; a one-hour meeting costs cents.
- **Artifact Registry**: the image is about 1 GB uncompressed (mostly ffmpeg's libraries); keep
  only recent images with a cleanup policy.

## Configuration

Environment variables (infrastructure only):

| Variable | Default | Notes |
|---|---|---|
| `DATABASE_URL` | required | `postgresql+psycopg://…`; Neon's pooled URL in production |
| `SECRET_KEY` | dev value | Signs logins and encrypts stored API keys. Required in production (≥ 32 chars) |
| `ENVIRONMENT` | `development` | `production` refuses to start with the dev `SECRET_KEY` |
| `ACCESS_TOKEN_TTL_MINUTES` / `REFRESH_TOKEN_TTL_DAYS` | `30` / `30` | |
| `MAX_REQUEST_MB` | `32` | Hard cap on any request body |
| `PROCESSING_TIMEOUT_MINUTES` | `30` | A job stuck longer is shown as failed so the user can retry |
| `RUN_MIGRATIONS` | `true` | Run `alembic upgrade head` when the container starts |

Admin settings (`/admin/settings`, stored in the database):

| Setting | Default |
|---|---|
| `transcription_base_url` · `transcription_model` | `https://api.groq.com/openai/v1` · `whisper-large-v3` |
| `transcription_api_key` | not set |
| `transcription_language` | `ar` (`auto` to detect; uploads can override it) |
| `transcription_chunk_seconds` | `600` |
| `llm_base_url` · `llm_model` | `https://api.deepseek.com` · `deepseek-chat` |
| `llm_api_key` | not set |
| `llm_temperature` · `llm_max_tokens` | `0.3` · `8000` |
| `summary_prompt` · `notes_prompt` | the files in `app/prompts/` |
| `summary_max_transcript_chars` | `60000` (longer transcripts are condensed first) |
| `max_upload_mb` · `max_audio_minutes` | `30` · `180` |
