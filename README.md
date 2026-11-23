# AJZIO Education — Free HSC Learning Community

**Share knowledge. Grow together.** AJZIO Education is a bilingual, free-for-every-student HSC learning community for Bangladesh. It is a self-hosted, server-backed application built with Python's standard library, SQLite, HTML, CSS and JavaScript.

The site starts at a sign-in/create-account screen. Students must authenticate before entering the learning dashboard. Account creation asks only for a display name, email, password, HSC class, group, education board and preferred language. No phone number, home address or school ID is requested.

## Included features

- Dedicated AJZIO Education brand, custom SVG logo/favicon and responsive animated sign-in experience.
- Email/password account registration and login with PBKDF2 password hashing and HTTP-only session cookies.
- Google OAuth 2.0 sign-in integration (ready to enable with server-side credentials; no client secret is shipped).
- Post-sign-in welcome questionnaire, saved persistently to SQLite.
- Student feedback and feature-request center, with status tracking and an admin review queue.
- HSC XI/XII profiles, study groups, education boards and Bengali/English interface.
- Permanent sequential AJZIO Member IDs beginning at 1000; IDs and permission roles are server-assigned and cannot be changed from profile forms.
- Editable student profile (display name, class, group, board and language) plus a privacy-limited public profile lookup.
- Student note submissions (PDF/JPG/PNG), copyright-permission confirmation, moderation and revision workflow.
- Public library exposes approved notes only.
- Timed examinations, server-side marking, answer explanations and attempt protection.
- Note requests, leaderboard, student activity and admin/moderator tools.
- Public academic chat room with `@admin` mention, message reporting, rate limits and a server-side contact/profanity filter. Private student DMs are intentionally not included.
- Rule-breaking contact details and configured abusive terms are rejected before a message is stored. First violation receives a warning; a second violation in seven days triggers a six-hour suspension; subsequent violations trigger a 24-hour suspension.
- Restricted accounts are routed to an appeal page; administrators can review appeals, restore accounts, suspend or lock accounts.
- Admin-only panel includes member controls, chat moderation, reports, appeals, moderation history and an administrator audit trail.
- SQLite-backed accounts, feedback, notes, exam attempts and community activity. Uploaded files are saved on the server.
- No subscription, Premium plan or paywall. All student learning features are free.

A starter ICT practice set is seeded on a fresh database. It is explicitly labelled independent practice, not an official board exam. Staff can add examinations from the review center.

## Run on CachyOS / Arch / Fedora / Ubuntu

Python 3.10+ is recommended. No `pip install` or `npm install` is needed.

```bash
unzip ajzio-education.zip
cd ajzio-education
chmod +x run.sh
./run.sh
```

Open `http://127.0.0.1:4173`.

The database and uploaded files are created under `data/`. Preserve and back up that entire directory. The database filename `hsc-circle.sqlite3` is retained for compatibility with the previous local HSC Circle build; the app and public brand are now AJZIO Education. If you already have the older `data/` directory, copy it into this project before the first run to keep existing accounts and notes. The database migration adds new profile, survey and feedback tables without deleting earlier records.

Change the port if necessary:

```bash
PORT=8080 ./run.sh
```

## Create the first administrator

Before the first registration, run:

```bash
HSC_ADMIN_EMAIL=you@example.com ./run.sh
```

Create an account using that exact email. It receives the administrator role. After the account is created, start normally with `./run.sh`.

If the account already exists, stop the app and promote it:

```bash
python3 server.py --promote you@example.com
```

Give moderator access only to a trusted account:

```bash
python3 server.py --make-moderator moderator@example.com
```

Roles are controlled on the server; there is no browser-side role switch. Staff cannot approve their own submitted notes.

## Enable Google sign-in

The Google button is part of the login and registration experience. It stays safely disabled until valid OAuth credentials are configured. Email/password login works without Google credentials.

1. In Google Cloud Console, create or select a project and configure the OAuth consent screen.
2. Create an OAuth Client ID of type **Web application**.
3. Add the exact local JavaScript origin `http://127.0.0.1:4173` for local testing.
4. Add this exact authorized redirect URI: `http://127.0.0.1:4173/auth/google/callback`.
5. Export the credentials in the same terminal before starting the server:

```bash
export GOOGLE_CLIENT_ID='YOUR_CLIENT_ID.apps.googleusercontent.com'
export GOOGLE_CLIENT_SECRET='YOUR_CLIENT_SECRET'
export GOOGLE_REDIRECT_URI='http://127.0.0.1:4173/auth/google/callback'
./run.sh
```

For a public domain, replace the origin and redirect URI with the HTTPS domain registered in Google Cloud. Keep the client secret only on the server; never put it in `app.js`, HTML, screenshots or a public repository. The callback validates Google's ID-token audience, issuer, expiry, verified email and nonce. New Google users complete their HSC class/group/board profile before account creation. An existing password account with the same email is not silently linked; the student must use their existing sign-in method.

## Welcome questionnaire and suggestions

A new account gets a short post-login survey about learning goals, the biggest study challenge and desired features. Responses are stored in `onboarding_responses`. Students can skip it and answer later on a subsequent login.

The **Your feedback** page accepts feature ideas, bug reports, content corrections and other suggestions. Students can view the status of their own submissions. Admins/moderators can review and mark items as in review or done. The API limits each account to five feedback submissions per day.

## Member IDs, chat and account restrictions

- Member IDs begin at `1000` on a fresh database and are allocated sequentially by the server. Existing accounts are assigned numbers during the database migration; new accounts receive the next available number.
- A student's own profile can update display name, class, group, board and preferred language. Member ID and permission role (`student`, `moderator`, `admin`) are not accepted from profile update requests.
- The community chat is one shared academic room, not private direct messaging. `@admin` marks a message for the owner's attention in the Admin Panel; the panel refreshes periodically while open.
- Local Bangladesh mobile numbers, international phone-like numbers and email addresses are screened, along with a configurable baseline abusive-language list. These messages are rejected before database insertion. The filter is a safety layer, not a guarantee that every possible bypass or harmful phrase will be detected; students can report messages and admins can remove them.
- Normal chat messages are removed after 30 days. Moderation logs keep the violation type and action, not the rejected message text.
- First detected violation in a rolling seven-day period gives a warning; the second results in a six-hour suspension; later violations result in a 24-hour suspension. Administrators can also suspend for six or 24 hours or permanently lock an account. A locked/restricted student can sign in only to reach the appeal process; the Admin Panel can approve an appeal and restore access.
- Admin-only actions are checked on the server. The browser cannot promote itself or change IDs/roles. The Admin Panel also records administrative lock/suspend/unlock, appeal decisions and chat-message deletions in an audit history.
- Configure extra blocked terms through the server environment variable `AJZIO_BLOCKED_WORDS`, as a comma-separated list. Keep the list conservative to avoid blocking ordinary educational discussion.

## Data and operations

- `data/hsc-circle.sqlite3`: accounts, permanent member IDs, profiles, chat, appeals, moderation/admin audit records, note metadata, surveys, feedback, exams, scores and activity.
- `data/uploads/`: student PDF/JPG/PNG files.
- Back up the full `data/` directory, not just the database file (SQLite uses WAL mode).
- Do not expose the built-in development server directly to the public internet.

## Public launch checklist

To serve students throughout Bangladesh, deploy on a VPS or suitable host with persistent storage, HTTPS reverse proxy, domain, backups and monitoring. Before broad launch, also complete email verification/password recovery, malware scanning of uploads, stronger distributed rate limiting, privacy/retention review, moderation staffing and restore tests. The current built-in rate limiter is per process. For school-age users, publish a clear privacy/guardian contact policy and retain only necessary data.

## API additions

- `GET /api/config` — feature configuration (Google OAuth enabled/disabled).
- `GET /api/auth/google/pending`, `POST /api/auth/google/complete` — Google profile completion.
- `POST /api/onboarding` — save first-login questionnaire.
- `GET /api/feedback`, `POST /api/feedback` — student's own suggestions.
- `GET /api/admin/feedback`, `POST /api/admin/feedback` — staff review and status updates.
- Google OAuth browser routes: `/auth/google/start` and `/auth/google/callback`.
- `GET /api/profile/{member_no}`, `POST /api/profile` — limited public profile and safe editable profile fields.
- `GET /api/chat`, `POST /api/chat`, `POST /api/chat/report` — shared academic room, admin mentions and user reports.
- `GET /api/appeal`, `POST /api/appeal` — restricted-account appeal flow.
- Admin-only `/api/admin/users`, `/api/admin/appeals`, `/api/admin/chat`, `/api/admin/chat-reports`, `/api/admin/moderation`, `/api/admin/audit`, plus their specific action endpoints.

Existing note, exam, leaderboard, request, activity and moderation APIs remain available.

## Licence and content ownership

The application code licence is in `LICENSE`. Student-submitted notes remain their contributors' work. Contributors must upload only material they own or have permission to distribute; the sharing-consent record is stored with each note submission/revision. The platform is free for every student, independently of any choice about source-code licensing.

## Profile and community chat update

The community UI includes a separate profile page. Students can update their display name, class, group, board, language, optional bio, and a PNG/JPG/WebP avatar (2 MB maximum). Member number and permission role are immutable through the profile API. Date of birth is optional and private by default; a separate opt-in is required before public profiles show it.

Chat supports clickable member profiles, replies to a specific message, `@admin`, `@moderator`, `@student`, and `@MemberID` mentions. The admin chat inbox is intentionally limited to messages mentioning the admin; the full public chat is not copied into the admin panel. Students can report another member's message, and administrators can review or dismiss reports. Report and moderation actions are logged.

A basic Bangla/Banglish profanity filter catches a limited set of known terms and common obfuscations. It is not a perfect language understanding or safety system: new slang and evasions may pass, and benign text may occasionally be flagged. Keep user reporting and human moderation enabled. Role mentions other than `@admin` are visible tags, not push/SMS notifications.
