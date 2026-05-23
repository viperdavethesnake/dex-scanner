#!/usr/bin/env bash
# Usage: ./run_eval.sh <run-label> [thinking_budget]
# Example: ./run_eval.sh A
#          ./run_eval.sh C 1024   (thinking on, budget 1024 tokens)
#
# Sends fixture-01 and fixture-02 to llama-server and saves results.
# Model is whatever is currently loaded — change compose.yaml before running.

set -euo pipefail
LABEL="${1:-}"
THINKING_BUDGET="${2:-0}"
BASE="$(dirname "$0")"
LLAMA_URL="http://192.168.33.231:8080/v1/chat/completions"

if [ -z "$LABEL" ]; then
  echo "Usage: $0 <run-label> [thinking_budget]"
  exit 1
fi

# Check server health
HEALTH=$(curl -s http://192.168.33.231:8080/health)
if [ "$HEALTH" != '{"status":"ok"}' ]; then
  echo "ERROR: llama-server not healthy: $HEALTH"
  exit 1
fi

for FIXTURE in fixture-01 fixture-02; do
  OUTFILE="$BASE/run-${LABEL}-${FIXTURE}.json"
  echo "--- Run $LABEL | $FIXTURE ---"

  # Build request: swap PLACEHOLDER, add thinking params if needed
  REQUEST=$(python3 - << PYEOF
import json, sys
with open('$BASE/$FIXTURE.json') as f:
    req = json.load(f)
req['model'] = 'local'
req['stream'] = False
thinking = $THINKING_BUDGET
if thinking > 0:
    req['chat_template_kwargs'] = {'enable_thinking': True}
    req['thinking'] = {'type': 'enabled', 'budget_tokens': thinking}
    req['max_tokens'] = 4096  # thinking budget + output headroom
else:
    req['chat_template_kwargs'] = {'enable_thinking': False}
print(json.dumps(req))
PYEOF
)

  START=$(date +%s)
  RESPONSE=$(curl -s -X POST "$LLAMA_URL" \
    -H "Content-Type: application/json" \
    -d "$REQUEST")
  END=$(date +%s)
  ELAPSED=$(( (END - START) * 1000 ))

  echo "$RESPONSE" > "$OUTFILE"

  # Extract and display key metrics
  python3 - << PYEOF
import json
with open('$OUTFILE') as f:
    r = json.load(f)

if 'error' in r:
    print(f"ERROR: {r['error']}")
else:
    usage = r.get('usage', {})
    content = r['choices'][0]['message']['content']
    prompt_tokens = usage.get('prompt_tokens', '?')
    completion_tokens = usage.get('completion_tokens', '?')
    elapsed_s = $ELAPSED / 1000
    tps = round(int(completion_tokens) / elapsed_s, 1) if isinstance(completion_tokens, int) and elapsed_s > 0 else '?'
    print(f"Wall clock: {elapsed_s:.1f}s | Prompt: {prompt_tokens} tok | Completion: {completion_tokens} tok | {tps} tok/s")
    print()
    print("--- OUTPUT ---")
    print(content[:2000])
    print("--- END ---")
PYEOF

  echo
done
