# Deploying rmtasks to Railway

One service, built from the `Dockerfile`, with a volume at `/data`. The image contains the web app, the background scheduler and `rmapi`. `railway.json` sets the health check (`/healthz`), a single replica (the scheduler must run exactly once) and restart on failure.

## What lives where

| Thing | Where | Why |
| --- | --- | --- |
| Code, rmapi binary | The image | Rebuilt on each deploy |
| Database, AI cache, downloads, run reports, rmapi token | Volume at `/data` | Survives redeploys; rmapi refreshes its token in place |
| Password, API keys, first rmapi token | Railway variables | Never in the repo or the image |

Paths are set in `config.railway.toml`; the image points `RMTASKS_CONFIG` at it.

## Variables

| Variable | Required | Value |
| --- | --- | --- |
| `RMTASKS_PASSWORD` | Yes | The web app's login password. The server refuses to start on a public address without it |
| `ANTHROPIC_API_KEY` | Yes | Handwriting recognition |
| `TYPESAFE_API_KEY` | Yes | Jev |
| `RMAPI_TOKEN_B64` | First boot | Your rmapi token file, base64-encoded. Copied to the volume once; after that the volume's copy is used |
| `RMTASKS_NOTEBOOK` | No | The Tasks notebook's name, e.g. `Tasks-sandbox` (default: `notebook.name`, `Tasks`) |
| `RMTASKS_SECRET_KEY` | No | Signs login cookies. Defaults to a key derived from the password, so changing the password signs everyone out |

## First deploy

Run these from the repository root. Each step is one command.

```bash
# 1. Project and service
railway init --name rmtasks
railway add --service rmtasks
railway link --service rmtasks   # or pass --service rmtasks to each command below

# 2. The volume (must exist before the first deploy; the container refuses to start without it)
railway volume add --mount-path /data

# 3. Secrets, piped in so they never land in shell history
printf '%s' 'choose-a-long-password' | railway variable set RMTASKS_PASSWORD --stdin --skip-deploys
printf '%s' "$ANTHROPIC_API_KEY"     | railway variable set ANTHROPIC_API_KEY --stdin --skip-deploys
printf '%s' "$TYPESAFE_API_KEY"      | railway variable set TYPESAFE_API_KEY --stdin --skip-deploys
base64 < .secrets/rmapi.conf | tr -d '\n' | railway variable set RMAPI_TOKEN_B64 --stdin --skip-deploys
railway variable set RMTASKS_NOTEBOOK=Tasks-sandbox --skip-deploys

# 4. Optional: bring your local data so nothing is read twice (settings, tasks, To-do slots, AI cache)
railway volume files upload data/rmtasks.db rmtasks.db
railway volume files upload cache/ai cache/ai

# 5. Deploy and get a URL
railway up --detach -m "first deploy"
railway domain
```

Open the URL and sign in with `RMTASKS_PASSWORD`.

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
