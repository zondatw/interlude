"""Interlude proxy — intercepts agent <-> API traffic and logs it as JSONL.

One process, one listener per agent. Each listener forwards to a fixed HTTPS
upstream. The agent talks to the listener over plain HTTP (set its base URL via
env var); we relay to the real API over HTTPS.

Each exchange produces two JSONL lines sharing an `id`:
  - kind="request"  : the parsed request body (system / tools / messages).
  - kind="response" : status + reassembled SSE events (or JSON body).

Responses are streamed through unbuffered (so SSE keeps working) while a copy is
teed into a buffer and reassembled after the stream ends. We strip the request's
accept-encoding so the captured bytes are always plaintext (no gzip/br to decode).
"""

import argparse
import http.client
import http.server
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from collections import Counter
from datetime import UTC, datetime

# (port, upstream_host, agent_label) — add a row to support another agent.
LISTENERS = [
    (8788, "api.anthropic.com", "claude"),
    (8789, "api.openai.com", "codex"),  # Codex via OpenAI API key (custom provider)
    (8790, "chatgpt.com", "codex"),  # Codex via ChatGPT login (chatgpt_base_url)
]

# `.interlude/` lives under the *user's cwd*, not next to the installed module —
# otherwise an `interlude` invoked from any directory would dump logs deep inside
# site-packages. Same behavior as before for users running from the repo root.
LOG_DIR = os.path.join(os.getcwd(), ".interlude")
LOG_PATH = None  # set in main()
LOG_LOCK = threading.Lock()

# Headers stripped before forwarding the request upstream. accept-encoding is
# dropped so responses come back uncompressed and are always parseable.
REQ_DROP = {
    "host",
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "accept-encoding",
}
# Headers stripped from the upstream response (we re-frame the body as chunked).
RESP_DROP = REQ_DROP | {"content-length"}
# Only these request headers are persisted — auth headers are never logged.
KEEP_HEADERS = {
    "content-type",
    "anthropic-version",
    "anthropic-beta",
    "openai-beta",
    "openai-organization",
    "user-agent",
}

MAX_TEXT = 20000  # cap on a reconstructed text field
MAX_BODY_JSON = 65536  # cap on a stored non-stream JSON body (serialized)
TEE_CAP = 16 * 1024 * 1024  # cap on the in-memory response copy


# --- request-side parsing ---------------------------------------------------


def detect_wire(path):
    p = path.split("?", 1)[0]
    if p.endswith("/v1/messages") or p.endswith("/messages"):
        return "claude-messages"
    if p.endswith("/responses"):
        return "codex-responses"
    if p.endswith("/chat/completions"):
        return "codex-chat"
    return "unknown"


def extract(wire, body):
    """Normalize system/tools/messages per wire format for eyeball verification."""
    if not isinstance(body, dict):
        return None
    if wire == "claude-messages":
        return {
            "system": body.get("system"),
            "tools": body.get("tools"),
            "messages": body.get("messages"),
        }
    if wire == "codex-responses":
        return {
            "system": body.get("instructions"),
            "tools": body.get("tools"),
            "messages": body.get("input"),
        }
    if wire == "codex-chat":
        msgs = body.get("messages")
        system = None
        if isinstance(msgs, list):
            system = [m for m in msgs if isinstance(m, dict) and m.get("role") == "system"] or None
        return {"system": system, "tools": body.get("tools"), "messages": msgs}
    return None


def kept_headers(headers):
    return {k: v for k, v in headers.items() if k.lower() in KEEP_HEADERS}


# --- response-side parsing (SSE reassembly) ---------------------------------


def parse_sse(text):
    """Split an SSE stream into [{event, data}] with data JSON-parsed when possible."""
    events = []
    for block in text.replace("\r\n", "\n").split("\n\n"):
        block = block.strip("\n")
        if not block:
            continue
        event, data_lines = None, []
        for line in block.split("\n"):
            if line.startswith("event:"):
                event = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:") :].lstrip())
        data = "\n".join(data_lines)
        try:
            parsed = json.loads(data) if data else None
        except ValueError:
            parsed = data
        events.append({"event": event, "data": parsed})
    return events


def _cap(s):
    return s if len(s) <= MAX_TEXT else s[:MAX_TEXT] + f"…(+{len(s) - MAX_TEXT} chars)"


def reconstruct_claude(events):
    text, tools, stop, usage, model, mid = [], {}, None, {}, None, None
    for ev in events:
        d = ev.get("data")
        if not isinstance(d, dict):
            continue
        t = d.get("type") or ev.get("event")
        if t == "message_start":
            m = d.get("message", {}) or {}
            model, mid = m.get("model"), m.get("id")
            usage.update(m.get("usage") or {})
        elif t == "content_block_start":
            cb = d.get("content_block", {}) or {}
            if cb.get("type") == "tool_use":
                tools[d.get("index")] = {"name": cb.get("name"), "json": []}
        elif t == "content_block_delta":
            delta = d.get("delta", {}) or {}
            if delta.get("type") == "text_delta":
                text.append(delta.get("text", ""))
            elif delta.get("type") == "input_json_delta":
                tools.get(d.get("index"), {}).setdefault("json", []).append(
                    delta.get("partial_json", "")
                )
        elif t == "message_delta":
            if "stop_reason" in (d.get("delta") or {}):
                stop = d["delta"]["stop_reason"]
            usage.update(d.get("usage") or {})
    tool_uses = []
    for idx in sorted(tools, key=lambda i: (i is None, i)):
        raw = "".join(tools[idx].get("json", []))
        try:
            inp = json.loads(raw) if raw else {}
        except ValueError:
            inp = {"_raw": raw[:500]}
        tool_uses.append({"name": tools[idx].get("name"), "input": inp})
    return {
        "model": model,
        "id": mid,
        "stop_reason": stop,
        "usage": usage,
        "text": _cap("".join(text)),
        "tool_uses": tool_uses,
    }


def reconstruct_codex_responses(events):
    text, usage, status, model, rid = [], {}, None, None, None
    for ev in events:
        d = ev.get("data")
        if not isinstance(d, dict):
            continue
        t = d.get("type") or ev.get("event")
        if t == "response.output_text.delta":
            text.append(d.get("delta", ""))
        elif isinstance(d.get("response"), dict):
            r = d["response"]
            model, rid = r.get("model", model), r.get("id", rid)
            usage = r.get("usage", usage)
            status = r.get("status", status)
    return {
        "model": model,
        "id": rid,
        "status": status,
        "usage": usage,
        "text": _cap("".join(text)),
    }


def reconstruct_codex_chat(events):
    text, model, finish, usage = [], None, None, {}
    for ev in events:
        d = ev.get("data")
        if not isinstance(d, dict):
            continue
        model = d.get("model", model)
        for ch in d.get("choices") or []:
            delta = ch.get("delta", {}) or {}
            if isinstance(delta.get("content"), str):
                text.append(delta["content"])
            if ch.get("finish_reason"):
                finish = ch["finish_reason"]
        usage = d.get("usage") or usage
    return {"model": model, "finish_reason": finish, "usage": usage, "text": _cap("".join(text))}


def reconstruct(wire, events):
    if wire == "claude-messages":
        return reconstruct_claude(events)
    if wire == "codex-responses":
        return reconstruct_codex_responses(events)
    if wire == "codex-chat":
        return reconstruct_codex_chat(events)
    return {"note": "no reconstructor for wire"}


# --- logging ----------------------------------------------------------------


def _now():
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _append(record):
    line = json.dumps(record, ensure_ascii=False)
    with LOG_LOCK:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def log_request(xid, agent, wire, method, path, headers, body):
    try:
        raw = body.decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = None
        _append(
            {
                "id": xid,
                "kind": "request",
                "ts": _now(),
                "agent": agent,
                "wire": wire,
                "method": method,
                "path": path,
                "headers_kept": kept_headers(headers),
                "request": parsed if parsed is not None else raw,
                "extract": extract(wire, parsed),
            }
        )
    except Exception as e:  # logging must never break the proxy
        print(f"[interlude] log_request error: {e}", flush=True)


def log_response(xid, agent, wire, status, ctype, body, truncated, cenc="", error=None):
    try:
        rec = {
            "id": xid,
            "kind": "response",
            "ts": _now(),
            "agent": agent,
            "wire": wire,
            "status": status,
        }
        if error is not None:
            rec["error"] = error
        elif cenc and cenc.lower() not in ("", "identity"):
            rec["note"] = f"unparsed content-encoding: {cenc}"
            rec["bytes"] = len(body)
        else:
            text = body.decode("utf-8", "replace")
            sniff = text[:256].lstrip()
            # Detect SSE by content-type OR by sniffing the body, since some
            # backends (e.g. the ChatGPT backend) mislabel the stream.
            if "text/event-stream" in (ctype or "").lower() or sniff.startswith(
                ("event:", "data:")
            ):
                events = parse_sse(text)
                types = Counter(
                    ev.get("event")
                    or (ev["data"].get("type") if isinstance(ev.get("data"), dict) else None)
                    for ev in events
                )
                rec.update(
                    stream=True,
                    event_count=len(events),
                    event_types=dict(types),
                    reconstructed=reconstruct(wire, events),
                )
            else:
                rec["stream"] = False
                try:
                    obj = json.loads(text) if text.strip() else None
                except ValueError:
                    rec["body"] = _cap(text)
                else:
                    if obj is not None and len(json.dumps(obj)) > MAX_BODY_JSON:
                        rec["body"] = {
                            "_truncated": True,
                            "_keys": list(obj) if isinstance(obj, dict) else None,
                        }
                    else:
                        rec["body"] = obj
        rec["content_type"] = ctype
        if truncated:
            rec["truncated"] = True
        _append(rec)
    except Exception as e:
        print(f"[interlude] log_response error: {e}", flush=True)


# --- proxy handler ----------------------------------------------------------


def make_handler(upstream_host, agent_label):
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        UPSTREAM = upstream_host
        AGENT = agent_label

        def log_message(self, *args):
            pass  # silence default access logging; we print our own line

        def handle(self):
            # Backends keep side connections open (analytics, long-polls); when the
            # agent drops them we get resets — swallow them quietly.
            try:
                super().handle()
            except (ConnectionResetError, BrokenPipeError):
                pass

        def _handle(self):
            xid = uuid.uuid4().hex[:12]
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            wire = detect_wire(self.path)
            print(f"[{self.AGENT}] {self.command} {self.path}", flush=True)
            log_request(xid, self.AGENT, wire, self.command, self.path, self.headers, body)

            try:
                fwd = {k: v for k, v in self.headers.items() if k.lower() not in REQ_DROP}
                conn = http.client.HTTPSConnection(self.UPSTREAM, timeout=600)
                conn.request(self.command, self.path, body=body or None, headers=fwd)
                resp = conn.getresponse()
            except Exception as e:
                self.send_error(502, f"Interlude upstream error: {e}")
                log_response(xid, self.AGENT, wire, 502, "", b"", False, error=str(e))
                return

            # Footgun guard: a ChatGPT-login token 401s against api.openai.com.
            if self.AGENT == "codex" and resp.status == 401 and self.UPSTREAM == "api.openai.com":
                print(
                    "[interlude] hint: Codex got 401 from api.openai.com. If you log in "
                    "with ChatGPT (no API key), use route A — base_url "
                    "http://localhost:8790/backend-api/codex",
                    flush=True,
                )

            ctype = resp.getheader("Content-Type", "")
            cenc = resp.getheader("Content-Encoding", "")
            buf, truncated = bytearray(), False
            try:
                self.send_response_only(resp.status, resp.reason)
                for k, v in resp.getheaders():
                    if k.lower() not in RESP_DROP:
                        self.send_header(k, v)
                if self.command == "HEAD" or resp.status in (204, 304):
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                else:
                    self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()
                    while True:
                        # read1() returns whatever is available now; plain read(n)
                        # blocks until n bytes accumulate or the stream ends, which
                        # would buffer the whole SSE response and stall the agent.
                        chunk = resp.read1(65536)
                        if not chunk:
                            break
                        self.wfile.write(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
                        self.wfile.flush()
                        if not truncated:
                            buf.extend(chunk)
                            if len(buf) > TEE_CAP:
                                truncated = True
                                del buf[TEE_CAP:]
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
            except Exception as e:
                print(f"[interlude] relay interrupted: {e}", flush=True)
            finally:
                conn.close()
                log_response(
                    xid, self.AGENT, wire, resp.status, ctype, bytes(buf), truncated, cenc=cenc
                )

        do_GET = _handle
        do_POST = _handle
        do_PUT = _handle
        do_PATCH = _handle
        do_DELETE = _handle
        do_HEAD = _handle
        do_OPTIONS = _handle

    return Handler


def _spawn_ui(port, logs_glob):
    """Spawn `interlude.report serve` as a child process so the user gets
    capture + web UI from one command. The child runs in its own Python
    process, so its own auto-reload watcher can re-exec on report.py edits
    WITHOUT touching the proxy — keeping live SSE streams intact.

    Returns the Popen handle. Child stdout is piped through a daemon thread
    that re-prints each line so proxy and UI logs interleave readably in one
    terminal.

    Module-form invocation (`-m interlude.report`) works both when running
    from a wheel install (site-packages) and from a source checkout via
    `uv run interlude`. Path-based invocation broke under the wheel."""
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "interlude.report",
            "serve",
            "--port",
            str(port),
            "--logs",
            logs_glob,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
        text=True,
    )

    def _pump():
        # report.py prints its own `[interlude-report] ...` banner; just
        # re-stream those lines verbatim. If the child dies, the loop exits
        # naturally and the supervisor in main() notices via poll().
        assert proc.stdout is not None
        for line in proc.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()

    threading.Thread(target=_pump, daemon=True, name="ui-stdout-pump").start()
    return proc


def main():
    global LOG_PATH

    ap = argparse.ArgumentParser(description="Interlude — agent <-> API capture proxy + web UI.")
    ap.add_argument(
        "--no-ui",
        action="store_true",
        help="don't auto-start the report.py web UI (proxy-only mode)",
    )
    ap.add_argument(
        "--ui-port",
        type=int,
        default=8000,
        help="port for the bundled web UI (default: 8000)",
    )
    ap.add_argument(
        "--logs-glob",
        default=None,
        help=f"JSONL glob passed to the web UI (default: {LOG_DIR}/log-*.jsonl)",
    )
    args = ap.parse_args()

    os.makedirs(LOG_DIR, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    LOG_PATH = os.path.join(LOG_DIR, f"log-{stamp}.jsonl")

    servers = []
    for port, host, label in LISTENERS:
        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), make_handler(host, label))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        servers.append(httpd)
        print(f"[interlude] {label}: http://127.0.0.1:{port} -> https://{host}", flush=True)
    print(f"[interlude] logging to {LOG_PATH}", flush=True)

    ui_proc = None
    if not args.no_ui:
        logs_glob = args.logs_glob or f"{LOG_DIR}/log-*.jsonl"
        ui_proc = _spawn_ui(args.ui_port, logs_glob)
        print(
            f"[interlude] web UI: http://127.0.0.1:{args.ui_port}/timeline "
            f"(auto-started; disable with --no-ui)",
            flush=True,
        )

    try:
        while True:
            time.sleep(1)
            if ui_proc is not None and ui_proc.poll() is not None:
                # Child died on its own (crash, manual kill, port collision).
                # Surface the exit code and stop supervising; proxy keeps going.
                print(
                    f"[interlude] web UI exited with code {ui_proc.returncode}",
                    flush=True,
                )
                ui_proc = None
    except KeyboardInterrupt:
        print("\n[interlude] shutting down", flush=True)
        if ui_proc is not None and ui_proc.poll() is None:
            ui_proc.terminate()
            try:
                ui_proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                ui_proc.kill()
        for s in servers:
            s.shutdown()


if __name__ == "__main__":
    main()
