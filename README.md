# Meetings app

Record a meeting on the phone and get back a transcript, a summary and action items,
with Arabic as the main language.

- [`backend/`](backend/): FastAPI service for Google Cloud Run with PostgreSQL: accounts and
  admin roles, meetings, transcription, summaries, PDF/Word export, admin-managed settings.
  Setup, API and deployment are in [backend/README.md](backend/README.md).
- The Android app (APK) talks to the backend over its REST API.
