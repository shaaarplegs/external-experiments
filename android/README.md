# Android app

Kotlin + Jetpack Compose client for the backend in [`../backend`](../backend). minSdk 26 (Android 8), targetSdk 35.
The UI follows the phone's language: Arabic (right-to-left) or English.

## What it does

- **Sign in / create account.** The server address is entered on the sign-in screen and remembered.
  Tokens live in encrypted preferences, and the access token refreshes itself on a 401.
- **Meetings list.** Search titles and transcripts, filter by category, pull to refresh, load more.
- **New meeting.** Record in the app (AAC mono 16 kHz / 32 kbps, about 14 MB per hour) or pick an audio or video file.
  Set the title, category, attendees and spoken language, then upload. Recording runs in a foreground
  service, so it keeps going with the screen off. Pause and resume are supported.
- **Meeting detail.** It checks the server every 4 s while the meeting is queued, transcribing or summarizing. It shows:
  - the overview, what happened, decisions, open questions, parties and topics;
  - action items: tick, add or delete;
  - the transcript.

  From the detail screen you can also:
  - edit the title, category and attendees;
  - export a PDF, a PDF with the transcript, or a Word file through the share sheet;
  - summarize again, or upload the audio again after a failure;
  - delete the meeting.
- **Categories:** add, rename and delete.
- **Settings:**
  - this month's uploads and audio minutes against your quota;
  - change password;
  - sign out;
  - delete account.

Server errors are shown as the server words them (for example the quota message, or why access was revoked).

## Build

Requires JDK 17+ and the Android SDK (platform 35).

```bash
cd android
export ANDROID_HOME=/path/to/android-sdk
./gradlew assembleDebug                      # app/build/outputs/apk/debug/app-debug.apk
./gradlew assembleDebug -PserverUrl=https://meetings-api-xxxx.run.app   # prefill your Cloud Run URL
```

The default server address is `http://10.0.2.2:8000`, which is the host machine as seen from the
Android emulator (`docker compose up` in `backend/`). Debug builds allow plain HTTP for this. Release
builds require HTTPS, which Cloud Run provides.

Install on a phone with `adb install app/build/outputs/apk/debug/app-debug.apk`, or copy the APK over and open it
(allow installs from unknown sources). CI builds the debug APK on every push that touches `android/`
and keeps it as the `app-debug` artifact.

Uploads are capped at 31 MB (Cloud Run's request limit is 32 MiB), which is about two hours of in-app recording.
