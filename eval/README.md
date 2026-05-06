# Ranker eval

Offline evaluation for the digest ranker. Two artifacts live here:

- `baseline.json` — the latest leave-one-day-out result on `main`. Updated by
  the `refresh-baseline` job in `.github/workflows/eval.yml` after every push.
  PRs are gated against this file.
- `metrics-YYYY-MM-DD.json` — per-run snapshot from each eval workflow run.

## What it measures

For each labeled day in the impression+reaction history, the harness trains
a fresh logistic-regression ranker on every *other* day, then ranks that
day's items. It reports four metrics, macro-averaged across days:

| Metric | What it tells you |
|--------|-------------------|
| `ndcg_at_5` | Are the top 5 ranked items the ones the user actually engaged with? Higher = better. |
| `recall_at_5` | What fraction of the day's positive items landed in the top 5? |
| `category_entropy_top5` | Diversity of categories in the top 5 — a sanity check that the ranker isn't collapsing to a single topic. |
| `source_entropy_top5` | Same idea but for sources. Catches "every slot is Hacker News" failure modes. |

## CI gate

PRs that touch `pipeline/**` run `pipeline.eval --metrics --check`. The job
fails if `ndcg_at_5` drops more than 10% (`REGRESSION_TOLERANCE` in
`pipeline/eval.py`) versus `baseline.json`. When the eval can't run yet
(< 3 labeled days), the gate auto-passes.

## Running locally

```bash
python -m pipeline.eval --metrics
# inspect the output:
cat eval/metrics-$(date -u +%F).json | jq .macro
```

To regenerate the baseline manually (useful right after the first weeks of
data accumulate):

```bash
python -m pipeline.eval --metrics --update-baseline
git add eval/baseline.json && git commit -m "Set initial eval baseline"
```
