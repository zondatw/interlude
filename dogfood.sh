#!/usr/bin/env bash
# Interlude end-to-end dogfood.
#
# Starts the proxy, drives Claude Code + Codex through it, then verifies the
# captured JSONL (correct agent/wire tags, system/tools/messages present, and
# zero credential leaks). Stops the proxy on exit.
#
# Codex uses a custom provider pointed at the proxy's chatgpt.com listener
# (port 8790, /backend-api/codex), reusing the ChatGPT login — so the Codex
# round-trip succeeds and its response is reassembled (no OPENAI_API_KEY needed).

set -uo pipefail
cd "$(dirname "$0")"

CLAUDE_PORT=8788
CODEX_PORT=8790            # Codex via ChatGPT-login backend (chatgpt.com)
PROXY_PORTS="8788 8789 8790"

port_busy() { lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1; }
all_bound() { for p in $PROXY_PORTS; do port_busy "$p" || return 1; done; }

# --- preflight ---
command -v uv     >/dev/null || { echo "FAIL: 'uv' not on PATH";     exit 1; }
command -v claude >/dev/null || { echo "FAIL: 'claude' not on PATH"; exit 1; }
command -v codex  >/dev/null || { echo "FAIL: 'codex' not on PATH";  exit 1; }
for p in $PROXY_PORTS; do
  if port_busy "$p"; then
    echo "FAIL: port $p already in use (proxy already running?). Stop it and retry."
    exit 1
  fi
done

mkdir -p .interlude

# --- start proxy ---
echo "==> starting proxy"
# --no-ui keeps dogfood headless — no need to bind port 8000 just to make
# one round-trip call.
uv run interlude --no-ui > .interlude/_dogfood-proxy.log 2>&1 &
PROXY_PID=$!
cleanup() { kill "$PROXY_PID" 2>/dev/null; }
trap cleanup EXIT

for _ in $(seq 1 25); do
  all_bound && break
  sleep 0.2
done
if ! all_bound; then
  echo "FAIL: proxy did not bind all ports ($PROXY_PORTS). Output:"; cat .interlude/_dogfood-proxy.log
  exit 1
fi

# Read the exact log path the proxy announced (its file is created lazily on the
# first request, so we must not guess via `ls -t`, which could pick a stale run).
LOG=""
for _ in $(seq 1 25); do
  LOG=$(sed -n 's/.*logging to \(.*\)$/\1/p' .interlude/_dogfood-proxy.log | tail -1)
  [ -n "$LOG" ] && break
  sleep 0.2
done
if [ -z "$LOG" ]; then
  echo "FAIL: proxy did not announce a log path. Output:"; cat .interlude/_dogfood-proxy.log
  exit 1
fi
echo "==> capturing to: $LOG"

# --- drive Claude Code (full round-trip should succeed) ---
echo "==> Claude Code through proxy"
CLAUDE_OUT=$(ANTHROPIC_BASE_URL="http://localhost:$CLAUDE_PORT" \
  claude -p "Reply with exactly the word PONG and nothing else." 2>&1)
echo "    claude replied: ${CLAUDE_OUT}"

# --- drive Codex via a custom provider (env-var shortcut is ignored by the
#     built-in openai provider, so we must define our own) ---
echo "==> Codex through proxy (ChatGPT-login backend)"
( cd /tmp && codex exec -s read-only --skip-git-repo-check \
    -c model_provider=interlude \
    -c 'model_providers.interlude.name="Interlude"' \
    -c "model_providers.interlude.base_url=\"http://localhost:$CODEX_PORT/backend-api/codex\"" \
    -c 'model_providers.interlude.wire_api="responses"' \
    -m gpt-5.5 \
    "Reply with exactly the word PONG and nothing else." >/dev/null 2>&1 )
echo "    codex exit: $?"

sleep 0.3  # let the proxy flush the final append

# --- verify ---
echo "==> verifying captured JSONL"
uv run python - "$LOG" <<'PY'
import json, re, sys
path = sys.argv[1]
recs = [json.loads(l) for l in open(path, encoding="utf-8")]
reqs  = [r for r in recs if r.get("kind", "request") == "request"]
resps = [r for r in recs if r.get("kind") == "response"]

def captured(agent, wire):
    for r in reqs:
        if r["agent"] == agent and r["wire"] == wire:
            ex = r.get("extract") or {}
            if all(ex.get(k) is not None for k in ("system", "tools", "messages")):
                return ex
    return None

def response(agent, pred):
    return next((r for r in resps if r["agent"] == agent and pred(r)), None)

# Deterministic guarantee: no auth header survives the whitelist (response
# records carry no headers_kept, hence the .get default).
header_leaks = sum(
    1 for r in recs
    if {k.lower() for k in r.get("headers_kept", {})} & {"authorization", "x-api-key", "cookie"}
)
# Defense in depth: no real token/key/JWT shapes anywhere in the records.
patterns = [
    r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}",  # JWT
    r"sk-ant-[A-Za-z0-9-]{20,}",                        # Anthropic key
    r"sk-(?:proj-)?[A-Za-z0-9]{32,}",                   # OpenAI key
    r"Bearer\s+[A-Za-z0-9._-]{20,}",                    # bearer value
]
value_leaks = sum(len(re.findall(p, json.dumps(r))) for r in recs for p in patterns)

claude_req  = captured("claude", "claude-messages")
codex_req   = captured("codex",  "codex-responses")
# Claude succeeds → streamed, reassembled text non-empty.
claude_resp = response("claude", lambda r: r.get("stream") and (r.get("reconstructed") or {}).get("text"))
# Codex round-trips via the ChatGPT backend → streamed, reassembled (status 200).
codex_resp  = response("codex",  lambda r: r.get("status") == 200 and (r.get("reconstructed") or {}).get("text"))

def shape(ex):
    def d(v): return f"{type(v).__name__}(len={len(v)})" if isinstance(v, (list, str)) else type(v).__name__
    return f"system={d(ex['system'])} tools={d(ex['tools'])} messages={d(ex['messages'])}"

ctext = (claude_resp.get("reconstructed") or {}).get("text") if claude_resp else None
xtext = (codex_resp.get("reconstructed") or {}).get("text") if codex_resp else None
print(f"   records                  : {len(recs)} ({len(reqs)} req / {len(resps)} resp)")
print(f"   claude req captured      : {'YES ' + shape(claude_req) if claude_req else 'NO'}")
print(f"   codex  req captured      : {'YES ' + shape(codex_req)  if codex_req  else 'NO'}")
print(f"   claude resp reassembled  : {'YES text=' + repr(ctext) if claude_resp else 'NO'}")
print(f"   codex  resp reassembled  : {'YES text=' + repr(xtext) if codex_resp else 'NO'}")
print(f"   header credential leaks  : {header_leaks}")
print(f"   value credential leaks   : {value_leaks}")

ok = all([claude_req, codex_req, claude_resp, codex_resp]) and header_leaks == 0 and value_leaks == 0
print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
PY
RESULT=$?

echo
echo "Full capture (sensitive — gitignored, do not commit/share): $LOG"
exit $RESULT
