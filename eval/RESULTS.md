# Model Eval Results

**Date:** 2026-05-02  
**Fixtures:** `fixture-01.json` (4 tokens, ~1016 tok input), `fixture-02.json` (4 tokens, ~1026 tok input)  
Both fixtures captured from live scans against the production webhook.

---

## Raw Results

| Run | Model | Thinking | Wall F1 | Wall F2 | Tok/s F1 | Tok/s F2 | Hard F1 | Hard F2 |
|-----|-------|----------|---------|---------|----------|----------|---------|---------|
| A | Llama-3.3-70B Q4_K_M | off | ~14s* | ~22s* | ~9 | ~10 | ✓ | **FAIL** |
| B | Qwen3.6-27B Q6_K | off | 22s | 12s | 6.6 | 11.7 | ✓ | ✓ |
| C | Qwen3.6-27B Q6_K | on | 278s | 336s | 11.1 | 9.1 | ✓ | ✓ |
| D | Qwen3.6-35B-A3B Q6_K | off | 18s | 3s | 8.6 | 61.7 | ✓ | ✓ |
| E | Qwen3.6-35B-A3B Q6_K | on | 30s | 34s | 87.7 | 78.2 | ✓ | ✓ |

*Run A timing was broken in the script (fixed after Run A); values estimated from log timestamps.  
†Run C's tok/s includes thinking tokens (3060–3087 total completion); visible output tokens were ~180.

---

## Hard Criteria Assessment

### Run A — Llama-3.3-70B — ELIMINATED

**fixture-02 HARD FAIL:** Model appended trading scenarios to PUMPPERPS which it rated SKIP. 

```
4. PUMPPERPS — SKIP — ...
Scenarios if INTERESTING — Aggressive: entry $0.0000300...
```

Scenarios must only appear on INTERESTING tokens. This is a structural instruction failure.

### Runs B, C, D, E — PASS both fixtures

All start with `1.`, all tokens rated, no INTERESTING tokens generated (correct — none were eligible given the fixture data), NOT ELIGIBLE (2+ failures) correctly rated SKIP, NOT ELIGIBLE (1 failure) correctly rated WATCH or SKIP (both are valid), no trailing summaries.

---

## Soft Quality Scores

Scored per criterion per fixture, averaged. Scale 1–5.

### Run B — Qwen3.6-27B, no thinking

| Token | Reasoning | Conciseness | Differentiation |
|-------|-----------|-------------|-----------------|
| F1: "sharp 5-minute drop after a large 6-hour gain indicates distribution" | 5 | 5 | — |
| F1: "Low liquidity under $15k creates high slippage risk...exit signals" | 5 | 5 | — |
| F1: overall | 4.5 | 5 | 3 (all SKIP except 1 WATCH) |
| F2: overall | 4 | 4.5 | 3 (all SKIP except 1 WATCH) |

**Soft total: 22/30**

### Run C — Qwen3.6-27B, thinking on

Marginally better reasoning depth vs B (e.g., "potential scalp if the micro dip stabilizes"), but same ratings and minimal visible improvement.

**Soft total: 23/30**  
**Wall clock: disqualifying for production** — 278–336s makes the full scan 5+ minutes.

### Run D — Qwen3.6-35B-A3B, no thinking

Good specific reasoning. F2 correctly identifies SAM MOGMAN's buy pressure as a reason for WATCH. Clean signal-based language ("severe 1-hour drawdown", "Sub-$15k liquidity").

**Soft total: 24/30**

### Run E — Qwen3.6-35B-A3B, thinking on

Best quality across both fixtures. Reasoning is precise and uses exact signal thresholds from the prompt ("buy pressure above 52%", "Sub-$15k liquidity", "active distribution"). F2 correctly rates 2 tokens WATCH rather than defaulting to SKIP — better differentiation.

Example from F1: *"Sub-$15k liquidity creates dangerous slippage risk while negative 5m price action and a down micro confirm ongoing distribution."* — references three independent signals in one tight sentence.

**Soft total: 26/30**

---

## Summary Table

| Run | Hard pass | Soft (/30) | Wall clock | Viable? |
|-----|-----------|------------|------------|---------|
| A — Llama-3.3-70B | **NO** | — | ~14–22s | No |
| B — Qwen3.6-27B, off | Yes | 22 | 12–22s | Yes |
| C — Qwen3.6-27B, on | Yes | 23 | 278–336s | **No** (too slow) |
| D — Qwen3.6-35B-A3B, off | Yes | 24 | 3–18s | Yes |
| E — Qwen3.6-35B-A3B, on | Yes | **26** | 30–34s | **Yes** |

---

## Decision

**Winner: Run E — Qwen3.6-35B-A3B Q6_K, thinking on**

- Best soft score (26/30) — most precise, most differentiated
- 30–34s wall clock adds ~15s vs no-thinking but is within acceptable scan latency (~45–50s total)
- 78–87 tok/s is exceptionally fast for a thinking run — MoE architecture absorbs the thinking overhead well
- Passes all hard criteria on both fixtures

**Runner-up: Run D (no thinking)** — if scan speed becomes the priority (e.g., polling every 2–3 minutes), D's 3–18s LLM time keeps total scan under 30s.

---

## Known issues found during eval

1. **`--cache-type-v q8_0` incompatible with Turing GPU** — V cache quantization requires flash attention, which the RTX 8000 (Turing/compute 7.5) does not support. Removed from `compose.yaml`. K cache quant (`--cache-type-k q8_0`) is unaffected.

2. **Thinking runs need `max_tokens ≥ 4096`** — Qwen3.6 thinking blocks run 1700–2000 tokens before producing output. Setting `max_tokens: 1024` or `2048` silently truncates the response to empty.

3. **Llama-3.3-70B format reliability** — Single hard failure observed (scenarios on SKIP token). May be a one-off prompt sensitivity, but enough to eliminate it given the other options.

---

## Next steps

1. Update `compose.yaml` — set `--model` to `Qwen_Qwen3.6-35B-A3B-Q6_K.gguf`
2. Update workflow LLM Analysis node — set model name + `chat_template_kwargs: {enable_thinking: true}` + `max_tokens: 4096`
3. Back up workflow JSON
4. Update `DEX-SCANNER.md` and `CLAUDE.md` with final model config
