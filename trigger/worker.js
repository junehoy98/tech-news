/**
 * Cloudflare Worker: punctually triggers the tech-news GitHub Actions digest.
 *
 * Why this exists: GitHub's own scheduled-cron queue can lag by hours, which
 * regularly pushed delivery from the intended 8:58 AM ET to mid-morning. A
 * workflow_dispatch, by contrast, gets a runner allocated almost immediately.
 * This Worker fires that dispatch on a reliable cron (see wrangler.toml). The
 * GitHub `schedule:` crons stay in place as a fallback — if this Worker ever
 * fails to fire, a late GitHub run still sends, and if this one already sent,
 * the digest's "already sent today" marker makes the backups no-op.
 *
 * The trigger only needs to *start* the runner with margin before 8:58 ET; the
 * digest job's `--send-at 08:58:37 America/New_York` gate pins the exact,
 * DST-aware delivery time. So second-level cron precision doesn't matter here.
 *
 * Secret (set with `wrangler secret put GH_DISPATCH_TOKEN`):
 *   GH_DISPATCH_TOKEN — fine-grained PAT, tech-news repo only, Actions: R/W.
 */

async function dispatch(env) {
  const url = `https://api.github.com/repos/${env.GH_OWNER}/${env.GH_REPO}/actions/workflows/${env.GH_WORKFLOW}/dispatches`;
  const resp = await fetch(url, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${env.GH_DISPATCH_TOKEN}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": `${env.GH_REPO}-trigger`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ ref: env.GH_REF }),
  });

  // The dispatch endpoint returns 204 No Content on success. Anything else is
  // an error worth surfacing — throwing marks the cron invocation as failed in
  // the Cloudflare dashboard so you can see it in the Worker's logs.
  if (resp.status !== 204) {
    const detail = await resp.text();
    throw new Error(`GitHub dispatch failed: ${resp.status} ${detail}`);
  }
}

export default {
  async scheduled(event, env, ctx) {
    await dispatch(env);
  },

  // Visiting the Worker's URL is a no-op health check. It deliberately does NOT
  // trigger a dispatch, so the public URL can't be used to fire the digest.
  async fetch(request, env) {
    return new Response(
      `tech-news trigger is live. It dispatches ${env.GH_OWNER}/${env.GH_REPO} on its cron schedule.\n`,
      { headers: { "Content-Type": "text/plain" } }
    );
  },
};
