"""End-to-end pipeline orchestrator: ingest → fetch → embed → rank.

Run by .github/workflows/pipeline.yml each morning, then by the Claude Code
scheduled trigger which reads the candidates JSON.
"""
from __future__ import annotations

import argparse
import json
import sys

from . import db, embed, fetch, health, ingest_events, rank


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--date", default=None, help="YYYY-MM-DD; defaults to today UTC")
    p.add_argument("--skip-fetch", action="store_true", help="don't fetch new items")
    args = p.parse_args(argv)

    print("→ ingest events (legacy events.jsonl + data/events/ shards)")
    print(json.dumps(ingest_events.ingest(), indent=2))

    conn = db.connect()
    db.init_schema(conn)

    if not args.skip_fetch:
        print("→ fetch sources")
        n = fetch.run(conn)
        print(f"  inserted {n} new items")

    print("→ embed + dedupe")
    print(json.dumps(embed.run(conn), indent=2))

    print("→ rank")
    rank_summary = rank.run(conn, today=args.date)
    print(json.dumps(rank_summary, indent=2))

    print("→ health")
    health_record = health.record(rank_summary, today=args.date)
    health.persist(health_record)
    print(json.dumps(health_record, indent=2))

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
