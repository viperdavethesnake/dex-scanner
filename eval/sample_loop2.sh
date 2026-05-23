#!/usr/bin/env bash
# Samples the DEX scanner every 5 minutes for 12 runs (1 hour).
# Saves HTML + parsed JSON summary per sample to eval/samples2/.

set -euo pipefail
OUTDIR="$(dirname "$0")/samples"
mkdir -p "$OUTDIR"
URL="http://192.168.33.231:5678/webhook/dex-scan"
SAMPLES=12
INTERVAL=300   # 5 minutes

echo "[$(date '+%H:%M:%S')] Starting 12-sample loop (5 min interval)"

for i in $(seq 1 $SAMPLES); do
  LABEL=$(printf "%02d" "$i")
  TS=$(date '+%H:%M:%S')
  echo "[${TS}] Sample ${LABEL}/${SAMPLES} ..."

  HTML_FILE="$OUTDIR/sample-${LABEL}.html"
  JSON_FILE="$OUTDIR/sample-${LABEL}.json"

  START=$(date +%s)
  curl -s "$URL" -o "$HTML_FILE"
  END=$(date +%s)
  ELAPSED=$(( END - START ))

  # Parse the HTML into a structured summary
  python3 - "$HTML_FILE" "$JSON_FILE" "$TS" "$ELAPSED" << 'PYEOF'
import sys, re, json

html_path, json_path, ts, elapsed = sys.argv[1:]

with open(html_path) as f:
    html = f.read()

# Meta line
meta_match = re.search(r'<div class="meta">(.*?)</div>', html)
meta_text = re.sub('<[^>]+>', '', meta_match.group(1)).strip() if meta_match else ''

# Counts from meta
def extract_count(pattern, text):
    m = re.search(pattern, text)
    return int(m.group(1)) if m else 0

total_launches = extract_count(r'(\d+)\s+launches', meta_text)
interesting_count = extract_count(r'(\d+)\s+INTERESTING', meta_text)
watch_count = extract_count(r'(\d+)\s+WATCH', meta_text)
skip_count = extract_count(r'(\d+)\s+SKIP', meta_text)
stale_count = extract_count(r'(\d+)\s+pre-filtered', meta_text)

# AI analysis block
analysis_match = re.search(r'<div class="analysis">(.*?)</div>', html, re.DOTALL)
analysis_text = re.sub('<[^>]+>', '', analysis_match.group(1)).strip() if analysis_match else ''

# Extract individual token ratings from analysis
tokens = []
for m in re.finditer(r'(\d+)\.\s+([A-Z0-9 ]+?)\s+[—\-–]\s+(SKIP|WATCH|INTERESTING)\s+[—\-–]\s+(.+?)(?=\d+\.\s+[A-Z]|$)', analysis_text, re.DOTALL):
    tokens.append({
        'symbol': m.group(2).strip(),
        'rating': m.group(3),
        'reasoning': m.group(4).strip()[:200],
    })

# Card symbols (INTERESTING + WATCH shown as cards)
card_symbols = re.findall(r'<div class="card[^"]*">.*?<span class="tname">[^<]+<span class="sym">\(([^)]+)\)', html, re.DOTALL)

summary = {
    'sample': int(sys.argv[3].split(':')[0]) * 3600,  # unused
    'timestamp': ts,
    'elapsed_s': int(elapsed),
    'total_launches': total_launches,
    'interesting': interesting_count,
    'watch': watch_count,
    'skip': skip_count,
    'stale': stale_count,
    'tokens': tokens,
    'analysis': analysis_text,
    'card_symbols': card_symbols,
}

with open(json_path, 'w') as f:
    json.dump(summary, f, indent=2)

print(f"  → {total_launches} launches | INTERESTING:{interesting_count} WATCH:{watch_count} SKIP:{skip_count} STALE:{stale_count} | {elapsed}s")
PYEOF

  # Sleep between samples (skip after last)
  if [ "$i" -lt "$SAMPLES" ]; then
    echo "[$(date '+%H:%M:%S')] Sleeping 5 min..."
    sleep $INTERVAL
  fi
done

echo "[$(date '+%H:%M:%S')] All $SAMPLES samples complete. Results in $OUTDIR"
