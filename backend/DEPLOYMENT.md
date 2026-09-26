# Deploying the backend

This is what the API server needs to run in production, and why. Nothing here contains a secret: values are
placeholders, and settings are named, never shown. `.env.example` lists every setting; `README.md` explains the
application.

Anything marked **NOT VERIFIED** has not been tried on a real host and must be checked at deployment.

## The one constraint that shapes everything: ONE instance, ONE worker

This process is not just an HTTP API. It also runs the live phone calls, so its state lives in its own memory:

| What | Where (in the code) |
|---|---|
| The Twilio media WebSocket (`/api/telephony/stream`) | `app/telephony/routes.py`, served by this process |
| The Deepgram speech-to-text and text-to-speech connections | opened by this process (`app/telephony/deepgram.py`) |
| The voice runtime that conducts a call (legacy or Pipecat) | `create_voice_runtime`, in this process |
| Every call in progress (browser and phone) | `SessionStore`, an in-memory dict (`app/store.py`) |
| The rate limits | `RateLimiter`, in memory (`app/security.py`) |
| The sweeper, callback retries, housekeeping and the optional workflow scheduler | asyncio tasks started in the app's lifespan (`app/main.py`) |

There is no separate voice worker, queue or cache (no Redis, Celery or subprocess). So, for now:

- **ONE instance and ONE worker.** Do not pass `--workers`, do not autoscale, do not run two replicas. A second process
  would not know the first one's calls (`404 Unknown call` mid-conversation) and would keep its own rate-limit counters.
- **NO serverless.** A function that starts per request cannot hold a call.
- **NO scale-to-zero, no free tier that sleeps.** A sleeping process drops calls and misses scheduler ticks.
- A deploy or restart ends the calls in progress. Deploy when nothing is ringing.

(The workflow scheduler alone is safe to overlap: jobs are de-duplicated and claimed in the database. The calls and rate
limits are what require a single process.)

## Build, migrate, start, check

Set the service's **root directory to `backend/`**. (The `requirements.txt` at the repository root is a pip command
written down as a note, not a requirements file.)

| Step | Command |
|---|---|
| Build / install | `pip install -r requirements.txt` |
| Before each deploy (pre-deploy / release command) | `alembic upgrade head` |
| Start | `uvicorn app.main:app --host 0.0.0.0 --port $PORT --no-access-log` |
| Health check | `GET /health` (liveness only: it touches no database, LLM or telephony) |
| First administrator (once, from a shell with the same settings) | `python -m app.cli create-user you@example.com --role admin` |

Why these flags:

- `--host 0.0.0.0`: the default (127.0.0.1) is unreachable from the host's router. The port comes from the platform:
  the application does not read `PORT`, the command does. Locally, `uvicorn app.main:app --reload --port 8000` is unchanged.
- `--no-access-log`: uvicorn's access log records the whole request line, and some URLs carry bearer-like secrets
  (`/api/calls/<call id>/turn`, `/api/call-jobs/<id>/ring?token=<answer token>`). The application takes care not to log
  them; the access log would. **NOT VERIFIED:** whether your host's own edge or router logs also record request URLs.
- No `--workers`, no `--reload`.
- No `--proxy-headers` change: the application reads `X-Forwarded-For` itself (see `TRUSTED_PROXY_HOPS`).
- The server starts even if the database is down; if it has not been migrated it says so in the log and every query
  then fails. Alembic is the only thing that creates the schema, so always run the migration first.

Python: 3.11 or newer (README); the tests were last run on 3.13. Set your host's Python version explicitly; each host reads
a different setting, so there is deliberately no version file in the repository. Dependencies are `>=` floors with no
lockfile, so two builds can differ. Pinning them is a decision for you to make.

## Database: PostgreSQL

`DATABASE_URL` must be `postgresql+psycopg://USER:PASSWORD@HOST:5432/DBNAME`. Hosts often hand out `postgres://` or
`postgresql://`; those select a driver (psycopg2) that is not installed, so rewrite the prefix. URL-encode special characters
in the password. SQLite stays supported for local development (it is what a laptop without PostgreSQL uses), but not in
production: hosting disks are usually wiped on every deploy, and a file cannot be shared between instances.

Connections are pinged before use (`pool_pre_ping`), so a database that closed an idle connection does not fail the next
request. The tests render the PostgreSQL migrations as SQL but do not run them against a live PostgreSQL:
**NOT VERIFIED** until `alembic upgrade head` has run on the real database. Moving data from a local SQLite file is not
automated; recreate users with the CLI.

## Settings a production server must have

| Setting | Production value |
|---|---|
| `APP_ENV` | `production`: no tracebacks, no `/docs` `/redoc` `/openapi.json`, and start-up warnings (below) |
| `DATABASE_URL` | PostgreSQL, as above |
| `COOKIE_SECURE` | `true` |
| `AUTH_REQUIRED` | `true` (the default; never `false` where others can reach it) |
| `PUBLIC_BASE_URL` | the frontend's public https address, no trailing slash |
| `CORS_ORIGINS` | JSON list of the frontend's exact origins, e.g. `["https://<frontend-domain>"]` |
| `TRUSTED_PROXY_HOPS` | how many proxies sit in front of the process (see below) |
| `GOOGLE_CLIENT_ID` | the OAuth client id, if Google sign-in is offered |
| `SIGNUP_ENABLED` | your decision (see below) |
| `GEMINI_API_KEY` | for live LLM calls |
| `VOICE_AGENT_API_KEY` | if a business system uses `/api/call-jobs` (a long random value) |
| `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER`, `PUBLIC_API_URL`, `DEEPGRAM_API_KEY` | only for phone calls, and only all five together |

With `APP_ENV=production` the server logs one warning per risky setting at start-up (secure cookie off, sign-in off, a SQLite or
wrongly-prefixed `DATABASE_URL`, `TRUSTED_PROXY_HOPS=0`, private callbacks allowed, `PUBLIC_BASE_URL` still on localhost). They name
the variable, never its value, and never stop the server.

### `TRUSTED_PROXY_HOPS` (rate limits depend on it)

Rate limits are per client address. Behind a reverse proxy the socket address is the proxy's, so with the default `0` **every
client shares one bucket** (for example, 10 login attempts a minute for everyone together). Set it to the number of proxies in
your chain; the server then takes the client address from the right of `X-Forwarded-For`, where only your own proxies write. Too
high a value lets clients choose their own address. The right number differs between hosts and CDNs, and nothing in this
repository can tell it: **NOT VERIFIED — requires runtime verification on the real host.** Check it by failing logins from two
networks (one bucket for both means too low), and by sending a made-up `X-Forwarded-For` (if it changes the bucket, too high).

## The frontend, the cookie and CORS

The session cookie is `va_session`: `HttpOnly`, `SameSite=Lax`, `Secure` when `COOKIE_SECURE=true`, path `/`, no domain, 12 hours
(`AUTH_SESSION_HOURS`). It is set and cleared in one place with the same attributes. `Lax` cookies are only stored and sent when the
browser sees the API as the **same site** as the page, so the topology matters:

1. **Recommended: the frontend host forwards `/api` to this server** (a Vercel rewrite). The browser talks to one origin,
   `VITE_API_BASE` stays unset, and there is no cross-site cookie and no CORS request. Vercel needs a `vercel.json` in the frontend,
   which is **not in this repository**, because it names your backend's address:

   ```json
   {
     "rewrites": [
       { "source": "/api/:path*", "destination": "https://<backend-domain>/api/:path*" },
       { "source": "/(.*)", "destination": "/index.html" }
     ]
   }
   ```

   The second rule is needed too: the frontend routes with the History API, so a direct load of `/anything` (a callee's answer
   link, for one) must fall back to `index.html`. **NOT VERIFIED:** that this proxy streams the call's server-sent events
   (`POST /api/calls/<id>/turn`) without buffering or cutting them off at your plan's limits. Try a real call.
   Even here the browser sends `Origin: https://<frontend-domain>`, so `PUBLIC_BASE_URL` / `CORS_ORIGINS` must name it or
   every signed-in write is refused with 403.
2. **Also fine: two subdomains of one registrable domain** (`app.<domain>` and `api.<domain>`). They are the same site, so
   `Lax` works. The frontend sets `VITE_API_BASE=https://api.<domain>` at build time; the browser then makes cross-origin requests
   with credentials, which is what `CORS_ORIGINS` is for.
3. **Not supported: different registrable domains** (`x.vercel.app` calling `y.onrender.com`). The cookie would need
   `SameSite=None`, which is deliberately not offered, and browsers that block third-party cookies would break sign-in anyway.

CORS allows `GET`, `POST`, `PUT` and `DELETE` (everything the API serves), the `Content-Type` and `X-API-Key` request headers, and
credentials, for exactly the origins in `CORS_ORIGINS`: never `*`. `Retry-After` is exposed so a cross-origin frontend can show
the rate-limit wait. Vercel preview deployments each have their own origin and are not allowed unless listed.

Frontend build variables (set on Vercel, build time): `VITE_GOOGLE_CLIENT_ID` (public; the same value as the backend's
`GOOGLE_CLIENT_ID`), and `VITE_API_BASE` only for topology 2.

## Google sign-in

The browser gets an ID token from Google Identity Services and posts it to `POST /api/auth/google`. The server verifies it with
google-auth (signature, expiry, issuer, and that the audience is `GOOGLE_CLIENT_ID`), requires a verified email, and signs in the
existing, enabled user with that email. It never creates accounts. Set up in Google Cloud Console (**NOT VERIFIED** from here):

- An OAuth client of type **Web application**. Its client id is `GOOGLE_CLIENT_ID` and `VITE_GOOGLE_CLIENT_ID`.
- **Authorized JavaScript origins**: the frontend's origin (`https://<frontend-domain>`), plus `http://localhost:5173` for development.
- No client secret and no redirect URI are used. The OAuth consent screen must be published for people outside your test users.
- The backend needs outbound https to Google (it fetches Google's signing certificates, with a 10-second limit).

Answers: `401` for an invalid, forged, expired or foreign credential, an unverified email or an unknown or disabled user; `503`
when Google's certificates cannot be fetched or read; `500` for anything unexpected.

## Signup: a decision for you

`SIGNUP_ENABLED` defaults to `true`. There is no email verification, and Google sign-in matches an existing account by verified
email, so someone can register an address that is not theirs and its owner's later Google sign-in lands in that account. Open
signup also lets strangers place phone calls once telephony is configured.

**Production decision required: keep signup open only with an email verification/linking strategy, or disable public signup.**
The configuration-only choice is `SIGNUP_ENABLED=false` (then create users with `python -m app.cli create-user`). This repository
does not decide it for you, and no verification system has been added.

## Phone calls

Only if `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER`, `PUBLIC_API_URL` and `DEEPGRAM_API_KEY` are all set.

- `PUBLIC_API_URL` is the https address Twilio reaches **this server** at, with **no trailing slash**: Twilio's webhook signatures
  are checked against URLs built from it, and the audio WebSocket address (`wss://…/api/telephony/stream`) is derived from it.
  Twilio connects to it directly; it does not go through the frontend host.
- The host must allow long-lived WebSocket connections to this process and keep it running for the length of a call.
- Inbound calls stay off (`TWILIO_INBOUND_ENABLED=false`) unless you accept that anyone who rings gets the demo profile.
- `VOICE_RUNTIME` stays `legacy` unless you have installed `requirements-pipecat.txt` and chosen otherwise. The real-network
  Deepgram path has not been verified end to end from this repository (**NOT VERIFIED**).

## Not in the repository, on purpose

- **No Dockerfile, Procfile or provider file**: the four commands above are all any host needs, and there was no reason to
  choose a provider for you. A Dockerfile would also need a `.dockerignore` so `.env` and any `*.db` never enter an image.
- **No `vercel.json`**: it must contain your backend's address.
- **No lockfile or Python version file**: see above.

## Checklist

- [ ] Root directory `backend/`; `pip install -r requirements.txt`; `alembic upgrade head` before each start
- [ ] `APP_ENV=production`, `COOKIE_SECURE=true`, `AUTH_REQUIRED=true`, PostgreSQL `DATABASE_URL` with `postgresql+psycopg://`
- [ ] `PUBLIC_BASE_URL`, `CORS_ORIGINS`, `GOOGLE_CLIENT_ID` set; the frontend's origin authorized in Google Cloud Console
- [ ] `TRUSTED_PROXY_HOPS` checked on the real host
- [ ] Start: `uvicorn app.main:app --host 0.0.0.0 --port $PORT --no-access-log`, one instance, one worker, always on
- [ ] `SIGNUP_ENABLED` decided; first admin created with the CLI
- [ ] Health check `GET /health`; then sign in from the real frontend domain and place a test call
