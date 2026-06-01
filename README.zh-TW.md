# Interlude

**[English](README.md)** · 繁體中文

攔截 AI coding agent（**Claude Code**、**Codex**）與其 API 之間的流量，把每筆
請求/回應的 prompt 架構（`system` / `tools` / `messages`）落地成 JSONL，用來分析
固定骨幹 vs 動態插槽，並做跨 agent 比較。

## 原理

兩個 agent 都能用環境變數覆寫 API base URL，所以不需要透明 MITM 或憑證偽造。
Interlude 是一個**明確的 reverse proxy**：

```
Claude Code ──(A) 純 HTTP 明文──▶ Interlude proxy ──(B) 正常 HTTPS──▶ api.anthropic.com
              localhost:8788                       (proxy 當 client 重新加密)
```

(A) 段沒有 TLS，proxy 直接從 socket 讀到明文 body——這就是攔截點，全程不碰憑證。
回應沿著 streaming relay 邊轉發邊複製，串流結束後重組 SSE 事件存檔。agent 完全無感。

## 需求

- [`uv`](https://docs.astral.sh/uv/)（本專案 Python 一律走 uv）
- `claude`（Claude Code CLI）、`codex`（Codex CLI）

## 快速上手

```bash
# 1. 啟動 proxy（三個 listener）
uv run proxy.py

# 2. 另開終端，把 Claude Code 指過來
ANTHROPIC_BASE_URL=http://localhost:8788 claude

# 3. 分析錄到的東西
uv run analyze.py
```

proxy 啟動後會印：

```
[interlude] claude: http://127.0.0.1:8788 -> https://api.anthropic.com
[interlude] codex:  http://127.0.0.1:8789 -> https://api.openai.com   (Codex + API key)
[interlude] codex:  http://127.0.0.1:8790 -> https://chatgpt.com      (Codex + ChatGPT 登入)
[interlude] logging to .interlude/log-<時間戳>.jsonl
```

每次啟動開一個新的 log 檔；每筆請求印一行 `[claude] POST /v1/messages`；`Ctrl-C` 停止。

## 把 agent 指過來

### Claude Code

環境變數即可（Claude Code 會自行在 base URL 後接 `/v1/messages`）：

```bash
ANTHROPIC_BASE_URL=http://localhost:8788 claude
# 非互動：
ANTHROPIC_BASE_URL=http://localhost:8788 claude -p "say hi"
```

### Codex

Codex 內建的 `openai` provider **不吃** base URL 覆寫（`OPENAI_BASE_URL` 會被忽略），
必須定義自訂 provider。依登入方式選一條。

#### A. ChatGPT 登入（建議,免 API key）

自訂 provider 指向 proxy 的 **chatgpt.com** listener（port 8790，路徑 `/backend-api/codex`）。
Codex 會帶著你的 ChatGPT token，proxy 轉發到真正的
`https://chatgpt.com/backend-api/codex/responses`，回應也錄得到：

```bash
codex exec -s read-only \
  -c model_provider=interlude \
  -c 'model_providers.interlude.base_url="http://localhost:8790/backend-api/codex"' \
  -c 'model_providers.interlude.wire_api="responses"' \
  "say hi"
```

長期穩定——寫進 `~/.codex/config.toml`：

```toml
[model_providers.interlude]
name = "Interlude"
base_url = "http://localhost:8790/backend-api/codex"
wire_api = "responses"
```

然後每次用 `-c model_provider=interlude` 切過去（**別**設頂層 `model_provider`，
否則 proxy 沒開時 Codex 會壞）：

```bash
codex -c model_provider=interlude exec -s read-only "say hi"
```

#### B. OpenAI API key

若你有一把帶 `api.responses.write` scope 的 `OPENAI_API_KEY`，改指向 proxy 的
**api.openai.com** listener（port 8789，路徑 `/v1`）：

```bash
codex exec -s read-only \
  -c model_provider=interlude \
  -c 'model_providers.interlude.base_url="http://localhost:8789/v1"' \
  -c 'model_providers.interlude.wire_api="responses"' \
  "say hi"
```

> **Note** — 用 ChatGPT 登入卻指到 `api.openai.com`（route B 但沒金鑰）會回 **401**
> （ChatGPT token 缺 `api.responses.write` scope）。請求仍會完整錄到，只是收不到回應——
> 改用 route A 即可。

## 桌面 App 支援

### 支援矩陣

| Agent / 介面 | 攔截機制 | 狀態 |
|---|---|---|
| **Claude Code**（CLI） | `ANTHROPIC_BASE_URL` 環境變數 | ✅ 已驗證，見上方 |
| **Codex**（CLI） | `~/.codex/config.toml` 裡的 `model_providers.*.base_url` | ✅ 已驗證，見上方 |
| **Codex IDE 擴充**（VS Code / JetBrains） | 與 CLI 共用 `~/.codex/config.toml`，依 [OpenAI 官方說明](https://developers.openai.com/codex/ide/settings) | ✅ 同設定即可（本機未直接驗證） |
| **Codex 桌面版**（`codex app`） | 包裝同一個 CLI binary | ✅ 同設定即可（本機未直接驗證） |
| **Claude Desktop**（GUI Electron app） | — | ❌ 不支援（見下方） |
| Claude Desktop MCP 流量 | 本地 MCP stdio shim，包成 `.mcpb` / `.dxt` | 🔬 訊號不同——只能錄到 **tool 呼叫**，不是 model prompt。屬另一個 scope，目前未實作 |

### Codex IDE / Codex 桌面版

依 OpenAI 的 [Codex IDE settings 文件](https://developers.openai.com/codex/ide/settings)，IDE 擴充會讀同一份 `~/.codex/config.toml`，獨立的 `codex app` 也是包裝同一個 CLI binary。所以只要 `codex` 在 CLI 上已經能透過 interlude 跑通，IDE / 桌面版只要在它們的設定裡切換到 `model_provider=interlude`（或你在 config 中取的名字）就好——proxy 端 8789 / 8790 那組 listener 全部都接得到，不需要任何額外設定。

### 為什麼 Claude Desktop 不支援

interlude 的整體設計是 **顯式 reverse proxy、不做 TLS MITM**——A 段（agent ↔ proxy）跑明文 HTTP，B 段（proxy ↔ upstream）跑正常 HTTPS。這只能用在 agent 有開 base-URL 旋鈕的情況。

Claude Desktop 沒有。我們查過：

- `ANTHROPIC_BASE_URL` 只是 **Claude Code** 的功能，不是桌面版（見 [`code.claude.com/docs/en/env-vars`](https://code.claude.com/docs/en/env-vars)）。
- macOS GUI app 連 shell 環境變數都不會繼承，要用 `launchctl setenv` 才能塞進去（[anthropics/claude-code#45994](https://github.com/anthropics/claude-code/issues/45994)）。
- `HTTPS_PROXY` 透過 Electron / Chromium runtime 部分有效，但有被回報過會被 upstream 擋 403——TLS pinning 狀態無公開資料證實或反證（[anthropics/claude-code#53869](https://github.com/anthropics/claude-code/issues/53869)）。
- 公開文件、設定面板、開發者選單裡都找不到 base-URL 設定。

如果硬要錄 Claude Desktop 流量，剩兩條路，但**都不在本專案 scope 內**：

1. **System-wide HTTPS proxy + 本地受信任 CA**。解密 Anthropic 的每條連線，但信任足跡變大、會牴觸 interlude「不碰 credential」的承諾，而且可能被 upstream pinning 擋掉。真的需要的話，請改用 mitmproxy / Charles Proxy——它們本來就明說自己在做什麼，interlude 不打算跟它們競爭。
2. **MCP server shim**，包成 Desktop Extension（`.mcpb` / `.dxt`）。Claude Desktop 會讀 `~/Library/Application Support/Claude/claude_desktop_config.json`，把每個 MCP server 當 subprocess 用 stdio JSON-RPC 跑起來（[官方說明](https://support.claude.com/en/articles/10949351-getting-started-with-local-mcp-servers-on-claude-desktop)）。interlude 可以做一個 shim 錄下「Claude Desktop ↔ 其他 MCP server 之間的 tool 呼叫」——但 model 真正的 prompt / completion 走的是 host 自己的 HTTPS 連線，跟 MCP 是分開的。訊號不同、scope 也不同，若有需要可另開 issue。

**今天就想完整錄 Anthropic 端 prompt 的話，請用 Claude Code CLI 透過 interlude**——同一個模型、同一個 provider、有完整的 base-URL 支援。

## 錄到什麼

log 在 `.interlude/log-<時間戳>.jsonl`，每筆交換**兩行**、用 `id` 配對：

```jsonc
// kind="request"
{"id":"ab12…","kind":"request","agent":"claude","wire":"claude-messages",
 "headers_kept":{…},                  // 已濾掉 authorization / x-api-key
 "request":{…完整解析後 body…},
 "extract":{"system":…,"tools":…,"messages":…}}

// kind="response"（同一個 id）
{"id":"ab12…","kind":"response","agent":"claude","status":200,
 "stream":true,"event_count":7,"event_types":{…},
 "reconstructed":{"model":"…","text":"…","usage":{…},"tool_uses":[…]}}
```

非串流回應（例如 Codex 的 401）則是 `"stream":false,"body":{…}`。

支援的 wire 格式：`claude-messages`（`/v1/messages`）、`codex-responses`（`/responses`）、
`codex-chat`（`/chat/completions`）。

## 分析

```bash
uv run analyze.py                    # 讀 .interlude 全部 log
uv run analyze.py --agent claude     # 只看單一 agent
uv run analyze.py --max-slots 30     # 多印幾條動態插槽
uv run analyze.py path/to/log.jsonl  # 指定檔 / glob
```

報告內容：

- 每個 agent 的 system 大小、**固定骨幹 vs 動態插槽**（例：Claude 注入的 `git status`、
  日期會被標成動態插槽）。
- tools 清單、數量、schema key（Claude=`input_schema` / Codex=`parameters`）。
- 跨 agent 架構比較表。

> 想讓 Codex 的動態插槽浮現，需跑幾個**不同 prompt / 不同時間**的 session（同一個 prompt
> 的多次 retry 都是同一份 system，只會算成 1 個 distinct 樣本）。

## Web UI

想用瀏覽器逐筆瀏覽、看「動態插槽」黃底高亮在原文裡的位置、展開 tool schema——
起本地 web UI：

```bash
uv run report.py serve                  # http://127.0.0.1:8000（預設）
uv run report.py serve --port 9000
uv run report.py serve --logs "other/path/log-*.jsonl"
```

路由：

| 路徑 | 內容 |
|---|---|
| `/` | 跨 agent 比較表 + 每個 agent 關鍵數字 |
| `/timeline[?agent=…&since=…&from=…&to=…&session_gap=…]` | Sequence-diagram 風格:agent ↔ API 兩條 lane,每個 exchange 拆 req/resp 兩支箭頭,自動依 gap 分 session(可調),頂端有每小時 histogram,每支 resp 箭頭附 RTT bar。點任一箭頭只展開那一半(req 看 system/tools/messages、resp 看 reassembled/usage/event_types)。 |
| `/requests[?agent=…]` | 全部 exchange 列表，可依 agent 過濾，含 model / token 欄位 |
| `/requests/<id>` | 單筆完整：system / tools / messages 摺疊 + 配對好的重組回應 |
| `/skeleton/<agent>` | canonical sample，**固定行灰、動態插槽行黃底**；底下列所有動態行 |
| `/tools/<agent>` | 每個 tool 的 JSON schema 可展開 |

每個 HTML 頁面都有對應的 `/api/<同路徑>` 端點，回相同資料的 JSON——一開始就內建好，
之後做 token usage 圖表 / 搜尋過濾 / live update 直接吃這個 endpoint，不必回頭剝
HTML。每頁 nav 都有 JSON 連結方便發現。

**只綁 `127.0.0.1`**（log 含完整 prompt，絕不能 LAN 可達）。loader 用 mtime cache，
proxy 邊 append 邊讀也不會慢；F5 reload 就看到新資料。

## 一鍵端到端驗證

```bash
./dogfood.sh
```

起 proxy → 各打一次 Claude/Codex → 驗請求+回應都錄到、零憑證外洩 → 收掉 proxy，
最後印 `RESULT: PASS`。

## 手動驗證（逐步）

想親手確認每個環節（而不是只跑 `dogfood.sh`）：

**Terminal 1** — 起 proxy：

```bash
uv run proxy.py
```

**Terminal 2** — 打一句 Claude Code，應正常回 `PONG`（證明 relay + streaming 沒壞）：

```bash
ANTHROPIC_BASE_URL=http://localhost:8788 claude -p "Reply with exactly the word PONG and nothing else."
```

回到 Terminal 1，proxy console 應出現一行 `[claude] POST /v1/messages`。

**檢查 log 落地**（結構摘要，不傾印 prompt 內容）：

```bash
LOG=$(ls -t .interlude/log-*.jsonl | head -1)
uv run python - "$LOG" <<'PY'
import json, re, sys
recs = [json.loads(l) for l in open(sys.argv[1], encoding="utf-8")]
for r in recs:
    if r.get("kind", "request") == "request":
        ex = r.get("extract") or {}
        present = [k for k in ("system", "tools", "messages") if ex.get(k) is not None]
        print(f"REQ  {r['agent']:<7} {r['wire']:<16} extract={present}")
    else:
        txt = (r.get("reconstructed") or {}).get("text")
        info = f"text={txt!r}" if r.get("stream") else f"body={type(r.get('body')).__name__}"
        print(f"RESP {r['agent']:<7} status={r['status']:<3} {info[:70]}")
blob = "\n".join(json.dumps(r) for r in recs)
leaks = re.findall(r"Bearer\s+\S{20,}|sk-ant-\S{20,}|eyJ[\w-]{10,}\.eyJ[\w-]{10,}", blob)
print("\ncredential leaks:", len(leaks))
PY
```

預期看到至少一組：

```
REQ  claude  claude-messages  extract=['system', 'tools', 'messages']
RESP claude  status=200 text='PONG'

credential leaks: 0
```

（開頭可能有一筆 `REQ claude unknown` → `RESP claude status=404`，那是 Claude Code 的
連線預檢 `HEAD /`，可忽略。）

**看架構分析**：

```bash
uv run analyze.py
```

驗完在 Terminal 1 按 `Ctrl-C` 收掉 proxy。

## 安全須知

- `.interlude/` 含**完整 prompt**（程式碼、可能的機密）→ 已 gitignore，**別 commit、別分享**。
- auth header（`authorization` / `x-api-key` / `cookie`）只轉送、**不寫 log**；`headers_kept`
  只保留白名單欄位。
- proxy 剝掉請求的 `accept-encoding`，所以錄到的 bytes 永遠是明文（免處理 gzip/br）。
- 自查惡意連線：`lsof -nP -iTCP -sTCP:ESTABLISHED | grep Python`，確認 proxy 只連
  `api.anthropic.com` / `api.openai.com` / `chatgpt.com`。
- **絕不做 TLS 攔截。** interlude 只攔截有開 base-URL 旋鈕的 agent（Claude Code、Codex
  CLI / IDE / 桌面版）。**不會**安裝 CA、終結 upstream TLS、或透明代理其他 app 的連線。
  這是刻意的信任邊界——若要錄 Claude Desktop GUI，請改用講清楚自己威脅模型的工具
  （mitmproxy、Charles Proxy）。見上方「桌面 App 支援」。

## 擴充新 agent

編 `proxy.py` 頂端的 `LISTENERS`，加一行 `(port, upstream_host, label)`。wire 偵測在
`detect_wire()`、欄位正規化在 `extract()`（請求）與 `reconstruct()`（回應）。

## 疑難排解

| 症狀 | 原因 / 解法 |
|---|---|
| `port 8788 already in use` | 上一個 proxy 還在。`lsof -nP -iTCP:8788 -sTCP:LISTEN` 找 PID → `kill <PID>` |
| Codex 沒被錄到,啟動印 `provider: openai` | 用了 `OPENAI_BASE_URL` 捷徑，被內建 provider 忽略。改用自訂 provider |
| Codex 回 401（缺 `api.responses.write` scope） | 你用 ChatGPT 登入卻指到 `api.openai.com`（8789）。改用 route A（8790 + `/backend-api/codex`），見「把 agent 指過來 › Codex」 |
| agent 拒收 `http://` | 退回 TLS：proxy 用自簽 CA 終結 TLS，Claude Code 設 `NODE_EXTRA_CA_CERTS` 信任（目前用不到） |

## 檔案

| 檔 | 用途 |
|---|---|
| `proxy.py` | 三 listener reverse proxy，streaming relay + SSE tee/重組 |
| `analyze.py` | 跨請求 diff、固定骨幹 vs 動態插槽、跨 agent 比較（純文字報告） |
| `report.py` | 本地 web UI（HTML + JSON），對應同樣的分析 |
| `dogfood.sh` | 一鍵端到端驗證 |
| `.interlude/` | JSONL 輸出（gitignored，敏感） |
