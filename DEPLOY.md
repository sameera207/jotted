# Deploying rmtasks to Railway

One service, built from the `Dockerfile`, with a volume at `/data`. The image contains the web app, the background scheduler and `rmapi`. `railway.json` sets the health check (`/healthz`), a single replica (the scheduler must run exactly once) and restart on failure.

## What lives where

| Thing | Where | Why |
| --- | --- | --- |
| Code, rmapi binary | The image | Rebuilt on each deploy |
| Database, AI cache, downloads, run reports, rmapi token | Volume at `/data` | Survives redeploys; rmapi refreshes its token in place |
| Sign-in secrets, API keys, first rmapi token | Railway variables | Never in the repo or the image |

Paths are set in `config.railway.toml`; the image points `RMTASKS_CONFIG` at it.

## Variables

| Variable | Required | Value |
| --- | --- | --- |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | Yes* | Sign in with Google (see below) |
| `RMTASKS_ALLOWED_EMAILS` | With Google | Who may sign in, comma-separated: `sameera207@gmail.com`. The server refuses to start with Google on and this empty |
| `RMTASKS_PUBLIC_URL` | With Google | The app's URL, e.g. `https://rmtasks-production.up.railway.app`; Google redirects back to it |
| `RMTASKS_PASSWORD` | No* | Password sign-in, as a fallback or instead of Google |
| `ANTHROPIC_API_KEY` | Yes | Handwriting recognition |
| `TYPESAFE_API_KEY` | Yes | Jev |
| `RMAPI_TOKEN_B64` | First boot | Your rmapi token file, base64-encoded. Copied to the volume once; after that the volume's copy is used |
| `RMTASKS_NOTEBOOK` | No | The Tasks notebook's name, e.g. `Tasks-sandbox` (default: `notebook.name`, `Tasks`) |
| `RMTASKS_SECRET_KEY` | Recommended | Signs login cookies; any long random string (`openssl rand -hex 32`). Changing it signs everyone out |

\* At least one sign-in method is required: the server refuses to start on a public address without one.

## Sign in with Google: one-time setup

1. In [Google Cloud Console](https://console.cloud.google.com/), create a project (or use one), then open **APIs & Services → OAuth consent screen**:
   - User type **External**, app name `rmtasks`, your email as support and developer contact.
   - Scopes: `openid`, `email`, `profile` (nothing sensitive, so no Google review).
   - Leave it in **Testing** and add `sameera207@gmail.com` under **Test users**. Only test users can sign in, a second lock on top of the allow-list.
2. **APIs & Services → Credentials → Create credentials → OAuth client ID**:
   - Application type **Web application**.
   - Authorised redirect URI: `https://<your-railway-domain>/auth/google/callback` (from step 2 of the first deploy below).
   - For local testing, also add `http://127.0.0.1:8765/auth/google/callback`.
3. Copy the client ID and secret into the variables below.

The app checks Google's signed ID token (issuer, audience, expiry, nonce), uses PKCE, requires a Google-verified email, and then requires that email to be on `RMTASKS_ALLOWED_EMAILS`.

## First deploy

Run these from the repository root. Each step is one command.

```bash
# 1. Project and service
railway init --name rmtasks
railway add --service rmtasks
railway link --service rmtasks   # or pass --service rmtasks to each command below

# 2. The volume (must exist before the first deploy; the container refuses to start without it),
#    and the public URL (needed for Google's redirect URI)
railway volume add --mount-path /data
railway domain

# 3. Sign-in. Create the Google client first (see above), then:
railway variable set RMTASKS_ALLOWED_EMAILS=sameera207@gmail.com --skip-deploys
railway variable set RMTASKS_PUBLIC_URL=https://<your-railway-domain> --skip-deploys
printf '%s' '<client id>'        | railway variable set GOOGLE_CLIENT_ID --stdin --skip-deploys
printf '%s' '<client secret>'    | railway variable set GOOGLE_CLIENT_SECRET --stdin --skip-deploys
openssl rand -hex 32 | tr -d '\n' | railway variable set RMTASKS_SECRET_KEY --stdin --skip-deploys

# 4. API keys and the rmapi token, piped in so they never land in shell history
printf '%s' "$ANTHROPIC_API_KEY"     | railway variable set ANTHROPIC_API_KEY --stdin --skip-deploys
printf '%s' "$TYPESAFE_API_KEY"      | railway variable set TYPESAFE_API_KEY --stdin --skip-deploys
base64 < .secrets/rmapi.conf | tr -d '\n' | railway variable set RMAPI_TOKEN_B64 --stdin --skip-deploys
railway variable set RMTASKS_NOTEBOOK=Tasks-sandbox --skip-deploys

# 5. Optional: bring your local data so nothing is read twice (settings, tasks, To-do slots, AI cache)
railway volume files upload data/rmtasks.db rmtasks.db
railway volume files upload cache/ai cache/ai

# 6. Deploy
railway up --detach -m "first deploy"
```

Open the URL and choose **Sign in with Google**.

**Run one copy only.** Once the hosted app is up, stop the local server (`rmtasks serve` on your laptop). Two schedulers would both pull from and write to the same notebooks.

## The rmapi token

`RMAPI_TOKEN_B64` reuses your laptop's registration. The hosted app and your laptop then share one device on my.remarkable.com, and revoking it signs both out.

To give the server its own device instead, skip `RMAPI_TOKEN_B64`, deploy, then open a shell and register:

```bash
railway ssh
rmtasks auth          # enter a fresh one-time code from my.remarkable.com
```

The token is written to the volume and kept across deploys.

## Updating

```bash
railway up --detach -m "what changed"
```

The volume (database, cache, token) is untouched by redeploys.

## Checking on it

```bash
railway logs --lines 100
railway status
```

The app logs each background check, automatic pull and push failure. The web app shows the same errors in a red banner.
