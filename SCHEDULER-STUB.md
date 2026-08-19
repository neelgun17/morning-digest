# Scheduler prompt fix — manual step

**What's wrong:** the scheduled trigger at [claude.ai/code/scheduled](https://claude.ai/code/scheduled)
holds a full copy of `.github/prompts/digest-agent.txt`'s instructions,
pasted in at some point in the past. Editing that file in the repo since
then hasn't changed what the agent actually does each morning — it's still
running on whatever was frozen into the trigger's stored prompt. This is
the root cause of at least one multi-week bug already (a fix landed in the
repo, but the running agent kept doing the old thing).

`scripts/setup.sh` already generates the *right* kind of prompt for new
setups — a short pointer, not an inline copy — but the currently-live
trigger predates that and needs to be fixed by hand.

**The fix:** open the existing "morning-digest" trigger at
[claude.ai/code/scheduled](https://claude.ai/code/scheduled) and replace
its entire stored prompt with the block below. This collapses the whole
class of drift — from now on the agent re-reads the instructions file fresh
on every run, so editing `.github/prompts/digest-agent.txt` in the repo is
enough on its own.

Nothing else about the trigger (schedule time, repo, tools) needs to
change — only the prompt text.

---

## Paste this as the trigger's prompt

```
Read .github/prompts/digest-agent.txt in this repository and follow its
instructions exactly, in full, as your task for today's run. Read it fresh
every time — do not rely on a cached or remembered version, and do not
substitute your own judgment for what it specifies. The file in the repo is
the single source of truth and may have changed since your last run.

Repo: https://github.com/YOUR-USERNAME/YOUR-REPO
Tools needed: Bash, Read, Write, Edit, Glob, Grep, WebSearch
```

---

## Why this is safe

`.github/prompts/digest-agent.txt` already contains the complete, current
instructions (duplicate detection, candidate reading, absence detection,
writing the digest + manifest, committing). The stub above adds nothing new
to what the agent does — it only removes the staleness risk by making the
agent re-read that file instead of running on a frozen snapshot of it.
