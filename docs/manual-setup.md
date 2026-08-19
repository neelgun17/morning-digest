# Manual setup

`scripts/setup.sh` does all of this for you. These instructions are here as a fallback if you prefer running each step by hand, or if the script fails partway and you need to finish manually.

## 1. Seed your personal files

```bash
cp interests.example.md interests.md
cp feedback-log.example.md feedback-log.md
# edit interests.md and sources.yml
```

`interests.md` and `feedback-log.md` are `.gitignore`d on purpose so they don't get pushed to the template upstream and so `git merge template/main` never conflicts with your edits.

## 2. Email delivery (Resend)

1. Sign up at [resend.com](https://resend.com) (free tier: 3,000 emails/month) and create an API key.
2. Add these repo secrets (`gh secret set NAME --body "..."` or via GitHub UI):
   - `RESEND_API_KEY` — your Resend API key
   - `EMAIL_TO` — your email address
   - `EMAIL_FROM` — (optional) defaults to `Morning Digest <digest@resend.dev>`

The workflow at `.github/workflows/email-digest.yml` fires automatically when the agent pushes a new digest.

## 3. One-click feedback (Cloudflare Worker)

1. Install Wrangler if needed: `npm install -g wrangler`
2. `wrangler login`
3. Deploy and set Worker secrets:

```bash
cd worker
npx wrangler deploy
# Generate the shared secret WITHOUT a trailing newline — both sides must match byte-for-byte.
openssl rand -hex 32 | tr -d '\n' > /tmp/feedback-secret
npx wrangler secret put FEEDBACK_SECRET < /tmp/feedback-secret
npx wrangler secret put GITHUB_TOKEN    # paste a GitHub PAT with repo write scope
npx wrangler secret put GITHUB_REPO     # paste username/repo-name
```

4. Copy the Worker URL from the deploy output (e.g. `https://morning-digest-feedback.you.workers.dev`).
5. Add two more GitHub repo secrets (same value, no trailing newline):
   - `FEEDBACK_URL` — the Worker URL
   - `FEEDBACK_SECRET` — `gh secret set FEEDBACK_SECRET --body-file /tmp/feedback-secret`

### Open tracking (optional)

The Worker records email opens at `POST /webhook/resend`. Unlike every other
route, it is not authenticated by `FEEDBACK_SECRET` — Resend signs its webhooks
and the Worker verifies that signature instead, which is why the route sits
ahead of the token check.

1. In the Resend dashboard, add a webhook pointing at
   `https://<your-worker-url>/webhook/resend` for the `email.opened` event.
2. Copy the signing secret Resend shows you (it starts with `whsec_`) and set it:

```bash
cd worker
npx wrangler secret put RESEND_WEBHOOK_SECRET   # paste the whsec_... value
```

Without `RESEND_WEBHOOK_SECRET` set, the endpoint returns 500 and records
nothing — it fails closed rather than accepting unverified events. Skip this
whole section if you do not want open tracking; clicks and written feedback
work without it.

## 4. Workflow permissions

Required for the pipeline, training, eval, and judge workflows to push their
commits to `main`.

```bash
gh api -X PUT "repos/OWNER/REPO/actions/permissions/workflow" \
  -f default_workflow_permissions=write \
  -F can_approve_pull_request_reviews=false
```

Or via the UI: **Settings → Actions → General → Workflow permissions → Read and write permissions**.

## 5. Verify

```bash
./scripts/verify.sh
```

Confirms secrets are set, workflow permissions are correct, and shows recent workflow runs.
