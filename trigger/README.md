# External trigger (Cloudflare Worker)

A tiny Cloudflare Worker that fires the digest's `workflow_dispatch` on a
reliable cron, because GitHub's own scheduled-cron queue can lag by hours.

- `worker.js` — the `scheduled()` handler that POSTs the dispatch to GitHub.
- `wrangler.toml` — the cron (`30 12 * * 1-5` = 12:30 UTC weekdays) and config.

The GitHub `schedule:` crons in `.github/workflows/daily.yml` stay as a fallback;
this Worker just makes delivery punctual on a normal day.

## One-time setup

**1. Create the GitHub token.** GitHub → **Settings → Developer settings →
Personal access tokens → Fine-grained tokens → Generate new token**:

- **Repository access:** Only select repositories → `tech-news`
- **Permissions:** Repository permissions → **Actions: Read and write**
  (Metadata: Read is added automatically; nothing else is needed)
- **Expiration:** pick a date and set a calendar reminder to rotate it

Copy the token (`github_pat_...`); GitHub shows it once.

**2. Confirm the token works** before involving Cloudflare. In PowerShell:

```powershell
$pat = "github_pat_PASTE_HERE"
Invoke-RestMethod -Method Post `
  -Uri "https://api.github.com/repos/junehoy98/tech-news/actions/workflows/daily.yml/dispatches" `
  -Headers @{ Authorization = "Bearer $pat"; Accept = "application/vnd.github+json"; "User-Agent" = "tech-news-trigger" } `
  -Body '{"ref":"main"}'
```

Success returns nothing (HTTP 204) and you'll see a new run appear in the
Actions tab. (That run will build a digest; if one already went out today it
no-ops via the already-sent marker.)

**3. Deploy the Worker.** Install Wrangler and log in once:

```powershell
npm install -g wrangler
wrangler login
```

Then from this `trigger/` folder:

```powershell
wrangler deploy
wrangler secret put GH_DISPATCH_TOKEN   # paste the same PAT when prompted
```

`wrangler deploy` registers the cron; the secret is stored encrypted in your
Cloudflare account (never in this repo).

## Verifying

- **Cron registered:** Cloudflare dashboard → Workers & Pages →
  `tech-news-trigger` → **Settings → Triggers** shows `30 12 * * 1-5`.
- **Live logs:** `wrangler tail` streams invocations; a successful fire logs an
  "Ok" scheduled event with no error.
- **End-to-end test on demand:** `wrangler dev --test-scheduled`, then in another
  terminal `curl "http://localhost:8787/__scheduled?cron=30+12+*+*+1-5"`. This
  runs the real dispatch (so it really does kick off a GitHub run).

## Rotating the token

When the PAT nears expiry, generate a new one (step 1) and re-run
`wrangler secret put GH_DISPATCH_TOKEN`. No redeploy needed.
