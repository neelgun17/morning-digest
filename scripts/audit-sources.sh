#!/bin/bash
# Audit source diversity across recent digests
# Usage: ./scripts/audit-sources.sh [days=7]

DAYS=${1:-7}
DAILY_DIR="daily"

echo "=== Source Diversity Audit (last $DAYS days) ==="
echo ""

# Extract all URLs from recent digests
find "$DAILY_DIR" -name "*.md" -not -name "sample*" -mtime -"$DAYS" | sort -r | while read -r file; do
  echo "--- $(basename "$file" .md) ---"
  grep -oP 'https?://[^\s\)]+' "$file" | sed 's|https\?://||;s|/.*||;s|www\.||' | sort | uniq -c | sort -rn
  echo ""
done

echo "=== Domain frequency (all $DAYS days) ==="
find "$DAILY_DIR" -name "*.md" -not -name "sample*" -mtime -"$DAYS" -exec grep -oP 'https?://[^\s\)]+' {} \; \
  | sed 's|https\?://||;s|/.*||;s|www\.||' \
  | sort | uniq -c | sort -rn | head -20

echo ""
echo "=== Red flags ==="
REPEATED=$(find "$DAILY_DIR" -name "*.md" -not -name "sample*" -mtime -"$DAYS" -exec grep -oP 'https?://[^\s\)]+' {} \; \
  | sed 's|https\?://||;s|/.*||;s|www\.||' \
  | sort | uniq -c | sort -rn | awk "\$1 >= $DAYS {print}")

if [ -n "$REPEATED" ]; then
  echo "Domains appearing every single day (possible over-reliance):"
  echo "$REPEATED"
else
  echo "No domain appears every day. Good diversity."
fi
