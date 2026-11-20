# Deploying lukidown on Railway

## Prerequisites
- A [Railway](https://railway.app) account
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- Telegram API credentials from [my.telegram.org](https://my.telegram.org)

---

## 1. Create a new Railway project

```
railway login
railway init
```

Or create one at [railway.app/new](https://railway.app/new) and link it:

```
railway link
```

---

## 2. Add Postgres and Redis plugins

In the Railway dashboard for your project:

1. Click **+ New** → **Database** → **Add PostgreSQL**
2. Click **+ New** → **Database** → **Add Redis**

Railway will inject `DATABASE_URL` and `REDIS_URL` as environment variables automatically, but their format needs adjusting (see step 4).

---

## 3. Push the code

```
git init          # if not already a git repo
git add .
git commit -m "initial"
railway up
```

Or connect your GitHub repo in the Railway dashboard for automatic deploys.

---

## 4. Set environment variables

In **Railway dashboard → your bot service → Variables**, add:

| Variable | Value |
|---|---|
| `API_ID` | Your Telegram API ID (from my.telegram.org) |
| `API_HASH` | Your Telegram API hash |
| `BOT_TOKEN` | Your bot token from @BotFather |
| `DATABASE_URL` | See note below |
| `REDIS_URL` | See note below |
| `DOWNLOAD_DIR` | `downloads` |
| `MAX_FILE_SIZE_MB` | `2000` |
| `DOWNLOAD_WORKERS` | `2` |
| `CANCEL_TTL_SECONDS` | `60` |
| `HISTORY_LIMIT` | `10` |

### DATABASE_URL format

Railway's Postgres plugin injects `DATABASE_URL` as `postgresql://...`.
SQLAlchemy with asyncpg requires `postgresql+asyncpg://...`.

Set the variable manually using the value Railway shows, but change the scheme:

```
# Railway gives you:
postgresql://postgres:password@hostname.railway.internal:5432/railway

# Set it as:
postgresql+asyncpg://postgres:password@hostname.railway.internal:5432/railway
```

You can reference Railway's auto-injected variable with a string substitution:

In the **Variables** tab, add:
```
DATABASE_URL=postgresql+asyncpg://${{Postgres.PGUSER}}:${{Postgres.PGPASSWORD}}@${{Postgres.PGHOST}}:${{Postgres.PGPORT}}/${{Postgres.PGDATABASE}}
```

### REDIS_URL

Railway injects `REDIS_URL` as `redis://...` which is already the correct format.
If Railway names the variable differently in your project, reference it directly:
```
REDIS_URL=${{Redis.REDIS_URL}}
```

---

## 5. Optional: proxy variables

If you need to reach Russian platforms (Rutube, VK, Yandex, etc.) from outside Russia:

```
RU_PROXY=socks5://user:password@your-ru-proxy-host:port
```

For all other platforms:
```
SOCKS5_PROXY=socks5://user:password@your-proxy-host:port
```

---

## 6. Health check

The bot exposes a health endpoint at port `8080` on paths `/` and `/health`.
Railway's health check is configured in `railway.toml` to probe `/health`.

---

## 7. Persistent storage (downloads directory)

Railway's ephemeral filesystem is wiped on redeploy. Downloaded files are only
held temporarily for upload to Telegram, then deleted — this is fine.

If you want session persistence across restarts (avoids re-auth), add a
Railway Volume mounted at `/app/sessions`.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `missing env vars: DATABASE_URL` | Check the variable uses the `+asyncpg` scheme |
| Bot starts but cannot download | Verify `ffmpeg` is available — it is included in the Dockerfile |
| Health check fails → container restarts | Check `/health` logs; usually a Redis/Postgres connection issue |
| `MAX_FILE_SIZE_MB` ignored | Railway has a 100 GB egress limit per month on free tier; Telegram's bot API limit is 50 MB per file without a local server |
