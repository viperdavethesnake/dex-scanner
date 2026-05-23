# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

DEX scanner for new token launches on Base and Solana. Standalone Docker Compose stack with a single purpose: on-demand token screening.

---

## First thing every session

**Ask the user for their n8n API key before doing any workflow work.**

The n8n JWT expires periodically. The user generates a fresh one via:
`n8n UI → Settings → API → Create API Key`

Once provided, store it in memory under `reference_n8n_api_key` and use it as:
```
Header: X-N8N-API-KEY: <token>
Base URL: http://localhost:5678/api/v1  (from inside dex-n8n container)
      or: http://192.168.33.231:5678/api/v1  (LAN)
```

The `N8N_API_KEY=dex-n8n-key` env var in compose.yaml is the server-side static key — it is NOT valid for user JWT calls. The JWT is separate and must be obtained from the UI.

---

## Stack

| Service | Container | Port | Notes |
|---------|-----------|------|-------|
| llama.cpp server | `dex-llamacpp` | 8080 | Owns 192.168.33.231; GGUF, RTX 8000 |
| n8n | `dex-n8n` | 5678 | Workflow engine; webhook host |

Both services share the macvlan IP 192.168.33.231. `dex-llamacpp` owns it; n8n uses `network_mode: service:llamacpp` and communicates via localhost.

---

## GPU

RTX 8000 (GPU 1, Turing, 48 GB, compute 7.5). `CUDA_VISIBLE_DEVICES=1` is set in compose.yaml. CDI device passthrough (`nvidia.com/gpu=all`) is used — do not substitute `runtime: nvidia`.

Flash attention is disabled (`--flash-attn off`) — Turing does not support FA2.

---

## Model

**Current:** `Qwen_Qwen3.6-35B-A3B-Q6_K.gguf` (MoE 35B/3.5B active, Q6_K, 30 GB, RTX 8000)  
Thinking is enabled in the workflow (`chat_template_kwargs: {enable_thinking: true}`, `max_tokens: 4096`).

`dex-llamacpp` loads a single GGUF at container start. To change the model:

1. Drop the GGUF into `/space/docker/containers/ai-models/gguf/custom/`
2. Update `--model` in `compose.yaml`
3. Update the `model` field and `chat_template_kwargs` in the workflow's **LLM Analysis** node via API (not CLI import)
4. `docker compose up -d`

**Note:** V cache quantization (`--cache-type-v q8_0`) is incompatible with Turing (RTX 8000). Only `--cache-type-k q8_0` is used. Thinking runs need `max_tokens ≥ 4096` — the Qwen3.6 thinking block consumes ~1700 tokens before producing output.

The model is ready when `curl http://192.168.33.231:8080/health` returns `{"status":"ok"}`.

---

## Starting the stack

```bash
cd /space/docker/containers/dex-scanner
docker compose up -d

# Verify llama-server is ready
curl http://192.168.33.231:8080/health
```

n8n will not start until llama-server is healthy. Allow time for the model to load before expecting the webhook to respond.

**Never restart dex-llamacpp alone** — destroys the shared network namespace, breaks n8n DNS (symptom: `EAI_AGAIN` errors). Always restart the full stack:

```bash
docker compose restart
```

---

## DEX Scanner workflow

- **Webhook:** `http://192.168.33.231:5678/webhook/dex-scan`
- **Workflow ID:** `bZ7P0LR4SML0MUv6`
- **n8n UI:** `http://192.168.33.231:5678`
- **Backup:** `workflows/dex-scanner-workflow.json` (all workflows in `workflows/`)
- **Docs:** `docs/PIPELINE.md`

### Updating the workflow via API

Always use the n8n REST API to push workflow changes — do NOT use `n8n import:workflow` CLI while n8n is running (corrupts webhook registration).

```bash
# Fetch current workflow
curl -s http://192.168.33.231:5678/api/v1/workflows/bZ7P0LR4SML0MUv6 \
  -H "X-N8N-API-KEY: <token>" | python3 -m json.tool

# Push updated workflow (settings payload must be exactly {"executionOrder":"v1"})
curl -s -X PUT http://192.168.33.231:5678/api/v1/workflows/bZ7P0LR4SML0MUv6 \
  -H "X-N8N-API-KEY: <token>" \
  -H "Content-Type: application/json" \
  -d @updated-workflow.json
```

### Backing up after UI changes

```
n8n UI → workflow menu → Download
```

Overwrite the appropriate file in `workflows/`.

---

## n8n login

Credentials are stored locally and not committed to the repo.
Reset password directly in the SQLite database if needed.

---

## Service URLs

| Service | URL |
|---------|-----|
| DEX Scanner | http://192.168.33.231:5678/webhook/dex-scan |
| n8n UI | http://192.168.33.231:5678 |
| llama-server | http://192.168.33.231:8080 |
| llama-server health | http://192.168.33.231:8080/health |

---

## Workflow pipeline

Full pipeline details, filter thresholds, signal computation formulas, and safety API specs are in **`docs/PIPELINE.md`**. Summary:

```
Webhook GET → DexScreener profiles → Filter Base/Solana → Fetch pair data
  → Normalize → Compute signals (vol trend, micro-trend, V/L ratio, sparkline)
  → GoPlus + RugCheck (Solana) + Honeypot.is (Base) → Safety filter
  → Build prompt (pre-filters tokens >90 min old) → LLM Analysis
  → Format HTML response (SKIP hidden; INTERESTING first, then WATCH)
```

---

## Known behaviors

- **All SKIP / no cards:** Normal when no tokens under 90 minutes with good momentum exist in the current batch. The header shows a stale count.
- **RugCheck on Base tokens:** Solana-only API; returns 404 for Base. `continueOnFail: true` — handled gracefully.
- **Honeypot.is on Solana tokens:** EVM-only API; Solana tokens pass through silently.
- **Model still loading:** llama-server returns `{"status":"loading"}` from `/health` until the model is ready. n8n won't start until healthy, but verify with `curl http://192.168.33.231:8080/health` before running a scan.
- **EAI_AGAIN DNS errors in n8n:** The dex-llamacpp container was restarted alone, destroying the shared netns. Restart the full stack: `docker compose restart`.

---

## Secret handling

**Never hardcode API keys in workflow JSON files or any committed file.**

| Secret | Where it lives | How it reaches runtime |
|---|---|---|
| `BIRDEYE_API_KEY` | `.env` (gitignored) | compose `env_file: .env` → container env |
| `N8N_JWT` | `.env` (gitignored) | read by Claude Code during session only |
| Birdeye API credential | n8n encrypted credential store (SQLite, not in git) | n8n HTTP Request node credential reference |

**Rules:**
- All secrets live in `.env` (gitignored). See `.env.example` for the full contract.
- n8n workflow JSON files must use credential store references (`credentials.httpHeaderAuth.id`), never raw key values in `headerParameters`.
- A `gitleaks` pre-commit hook is installed — it will block any commit containing detected secrets. If you get a false positive, add the pattern to `.gitleaksignore`, do not disable the hook.
- When updating workflows via the n8n API, always pull the current workflow first, patch in-memory, and PUT back. Never reconstruct from scratch — credential references are stored in node data and must be preserved.
- If a key is ever found hardcoded in a committed file: rotate immediately, strip from working tree, commit the strip, then clean git history with `git filter-repo`.

---

## Do not use the advisor() tool in this project.
