# Semi Equipment Daily

A daily Gmail digest of semiconductor capital-equipment news, with an editorial
focus on optics, metrology, and lithography — the core technologies of equipment
makers like **ASML, KLA, Onto Innovation, Applied Materials,** and **Lam
Research**.

A two-pass LLM pipeline scores each day's articles with Claude Haiku, then
clusters the top stories into 4–6 short briefs — written for a technical reader
new to semiconductor business jargon — with Claude Sonnet. It covers tech,
policy/geopolitics, and business angles, and lands in your inbox each weekday
morning.

---

## Prerequisites

- **Python 3.10+**
- An **Anthropic API key** — the pipeline scores and writes with Claude Haiku
  and Sonnet (~$1–3/month at this volume)
- A **Gmail account with an app password** — used for SMTP delivery

## Setup

**1. Clone and install into a virtual environment.**

```powershell
git clone https://github.com/junehoy98/tech-news.git
cd tech-news
python -m venv .venv
.\.venv\Scripts\Activate.ps1        # macOS/Linux: source .venv/bin/activate
pip install -e ".[dev]"
```

> If your working copy lives on a synced drive (Google Drive, Dropbox, …),
> create the venv somewhere outside it — file syncing can corrupt a venv.

**2. Get an Anthropic API key.** At https://console.anthropic.com/ → Settings →
API Keys, create a key (starts with `sk-ant-...`) and add a few dollars of
credit under Billing.

**3. Generate a Gmail app password.** Gmail SMTP won't accept your normal
password. Enable 2-factor auth, then visit
https://myaccount.google.com/apppasswords and create a 16-character password
(Google shows it once).

**4. Fill in your secrets.** Copy the template and set the four values:

```powershell
Copy-Item .env.example .env          # macOS/Linux: cp .env.example .env
```

`.env` needs `ANTHROPIC_API_KEY`, `GMAIL_FROM_ADDRESS`, `GMAIL_APP_PASSWORD`,
and `DIGEST_TO_ADDRESS`.

## Usage

```powershell
python -m tech_news.main --check      # validate config + secrets, then exit
python -m tech_news.main --dry-run    # build the digest to out/digest.html, no email
python -m tech_news.main              # fetch -> rank -> synthesize -> email
```

`--check` reports each secret as `[OK]` or `[MISSING]`. `--dry-run` writes the
HTML to `out/digest.html` and sends nothing — and doesn't mark articles as seen,
so you can iterate freely. Run `python -m tech_news.main --help` for all flags.

---

## Deploying to GitHub Actions (automated daily runs)

When you're happy with the digest content, push to GitHub so it runs
automatically every morning without your laptop needing to be on.

1. Fork this repo (or push your own copy) to your GitHub account
2. In your repo on GitHub: **Settings → Secrets and variables → Actions**
3. Add four repository secrets (same values as your `.env`):
   - `ANTHROPIC_API_KEY`
   - `GMAIL_FROM_ADDRESS`
   - `GMAIL_APP_PASSWORD`
   - `DIGEST_TO_ADDRESS`
4. Go to the **Actions** tab → **Daily Digest** → **Run workflow**
   (manual trigger to test it works in the cloud)
5. The workflow is already scheduled for weekday mornings. Because GitHub's
   cron can lag by hours, it starts a runner early and the job waits until
   ~9 AM ET to send, so the delivery time stays fixed regardless of scheduler
   lag. See the comments in `.github/workflows/daily.yml` for the details.

---

## Tuning the digest

The two files you'll edit when you want to change what shows up:

### `config/sources.toml`

Add or remove RSS feeds. Each entry has a `category` (company / tech / policy
/ business) and a `priority` (1 high, 3 low). No code changes needed.

### `config/criteria.md`

The relevance rubric sent to Claude as the system prompt. Edit this when:
- A type of story is getting too much weight (lower its score in the rubric)
- You want to surface a new theme (add it to the high-score examples)
- The briefs aren't reading how you want (rewrite the Voice section)

---

## Architecture

```
fetch (RSS)  →  dedupe (SQLite)  →  score (Claude Haiku 4.5)
                                            ↓
                        synthesize 4–6 briefs (Claude Sonnet 4.6)
                                            ↓
                           render HTML (Jinja2)  →  Gmail SMTP
```

- `src/tech_news/` — pipeline code, one module per stage
- `config/` — editorial tuning files
- `.github/workflows/daily.yml` — GitHub Actions cron + state caching
- `data/seen.sqlite` — local dedupe DB (gitignored; cached in Actions)
- `tests/` — pytest covering dedupe, ranking prompt, template rendering

## Cost

~$1–3/month in Anthropic API charges. GitHub Actions and Gmail SMTP are free
at this scale.

## Roadmap

- **V1.5** — Enable cron, expand source list after tuning the rubric
- **V2** — Click-tracking redirect to mark articles "saved"; weekly saved-items digest
- **V3** — Chat-with-corpus: ask "what's been happening with ASML High-NA?"
  over your last 90 days of articles
