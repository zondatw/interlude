"""Interlude analysis web UI — a local browser-facing view over the JSONL logs.

A tiny stdlib HTTP server that reads .interlude/log-*.jsonl and renders:
  - overview            (/)                cross-agent comparison, per-agent stats
  - request list        (/requests)        sortable rows linking into detail pages
  - request detail      (/requests/<id>)   system/tools/messages + paired response
  - skeleton vs slots   (/skeleton/<agent>) fixed lines greyed, dynamic slots yellow
  - tools browser       (/tools/<agent>)   collapsible JSON schema per tool

Bound to 127.0.0.1 only (the logs contain full prompts; never expose them on LAN).
Pure stdlib, single file. Field extraction is delegated to analyze.py so that
script remains the source of truth for the underlying logic.
"""

import argparse
import glob
import hashlib
import html
import http.server
import json
import os
import statistics
import sys
import threading
from collections import defaultdict
from urllib.parse import parse_qs, unquote, urlparse

from analyze import skeleton, system_text, tool_names, tool_schema_key

# --- log loading (mtime-cached) ---------------------------------------------

_CACHE = {}  # path -> (mtime, [records])
_CACHE_LOCK = threading.Lock()


def load_records(glob_pattern):
    """Return (sorted file paths, flat record list). Reuse parsed records when
    a file's mtime hasn't changed. JSON decode errors are skipped silently
    because the proxy may be appending while we read."""
    paths = sorted(glob.glob(glob_pattern))
    all_recs = []
    with _CACHE_LOCK:
        for p in paths:
            try:
                mtime = os.path.getmtime(p)
            except OSError:
                continue
            cached = _CACHE.get(p)
            if cached and cached[0] == mtime:
                all_recs.extend(cached[1])
                continue
            recs = []
            try:
                with open(p, encoding="utf-8") as f:
                    for line_no, line in enumerate(f):
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            r = json.loads(line)
                        except ValueError:
                            continue
                        # Pre-Phase-4 records have no `id` — synthesize a stable
                        # one from (path, line) so they still get URLs and the
                        # skeleton view sees their distinct system samples.
                        if not r.get("id"):
                            r["id"] = hashlib.blake2s(
                                f"{p}#{line_no}".encode(), digest_size=6
                            ).hexdigest()
                        recs.append(r)
            except OSError:
                continue
            _CACHE[p] = (mtime, recs)
            all_recs.extend(recs)
    return paths, all_recs


def group_records(recs):
    """Split into (request list, response-by-id dict). Pre-Phase-4 records
    without a `kind` field are treated as requests."""
    requests = []
    responses = {}
    for r in recs:
        kind = r.get("kind", "request")
        if kind == "request":
            requests.append(r)
        elif kind == "response":
            xid = r.get("id")
            if xid:
                responses[xid] = r
    return requests, responses


# --- HTML scaffolding --------------------------------------------------------

CSS = """
* { box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
       max-width: 1180px; margin: 0 auto; padding: 1em 2em 4em; color: #222; line-height: 1.5; }
nav { padding: 0.6em 0; border-bottom: 1px solid #eee; margin-bottom: 1.5em; }
nav a { margin-right: 1.2em; text-decoration: none; color: #06c; font-weight: 500; }
nav a:hover { text-decoration: underline; }
h1 { border-bottom: 2px solid #ddd; padding-bottom: 0.3em; margin-top: 0.4em; }
h2 { margin-top: 1.8em; color: #333; }
h3 { color: #444; margin-top: 1.3em; }
table { border-collapse: collapse; width: 100%; margin: 1em 0; }
th, td { border-bottom: 1px solid #eee; padding: 0.45em 0.7em; text-align: left;
         font-size: 0.88em; vertical-align: top; }
th { background: #f7f7f9; position: sticky; top: 0; font-weight: 600; }
tr:hover td { background: #fafafa; }
code, pre { font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 0.85em; }
code { background: #f1f3f5; padding: 0.1em 0.35em; border-radius: 3px; }
pre { background: #f6f8fa; padding: 1em; border-radius: 4px; overflow-x: auto;
      white-space: pre-wrap; word-wrap: break-word; max-height: 600px;
      border: 1px solid #eaecef; }
pre code { background: none; padding: 0; }
details { margin: 0.4em 0; }
summary { cursor: pointer; padding: 0.25em 0; user-select: none; }
summary:hover { color: #06c; }
.skeleton { background: #f8f8fa; padding: 1em; border-radius: 4px; overflow-x: auto;
            white-space: pre-wrap; word-wrap: break-word; max-height: 700px;
            border: 1px solid #eaecef; font-family: ui-monospace, monospace;
            font-size: 0.85em; line-height: 1.6; }
.fixed { color: #999; }
.dyn { background: #fff3a3; padding: 0 2px; border-radius: 2px; color: #222; font-weight: 500; }
.kv { display: grid; grid-template-columns: max-content 1fr; gap: 0.4em 1.2em; margin: 0.6em 0; }
.kv dt { font-weight: 500; color: #555; }
.kv dd { margin: 0; }
.muted { color: #888; font-size: 0.88em; }
.tag { display: inline-block; padding: 0.12em 0.55em; border-radius: 3px;
       background: #eef; font-size: 0.82em; color: #339; font-weight: 500; }
.tag.claude { background: #fde7d8; color: #963; }
.tag.codex { background: #def0ff; color: #246; }
.status-ok { color: #185; font-weight: 500; }
.status-err { color: #c33; font-weight: 500; }
.json-key { color: #905; font-weight: 500; }
.json-idx { color: #999; font-size: 0.75em; margin-right: 0.4em; }
.json-tree > div { margin-left: 1.5em; padding-left: 0.3em;
                   border-left: 1px solid #eee; }
"""

NAV = ('<nav>'
       '<a href="/">Overview</a>'
       '<a href="/requests">Requests</a>'
       '</nav>')


def esc(value):
    return html.escape(str(value)) if value is not None else ""


def page(title, body_html):
    return ("<!doctype html><html lang='en'><head>"
            "<meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{esc(title)} · Interlude</title>"
            f"<style>{CSS}</style></head><body>{NAV}{body_html}</body></html>")


def render_json(obj, depth=0):
    """Recursive collapsible JSON tree. Top level dicts/lists default to open."""
    if isinstance(obj, dict):
        if not obj:
            return "<code>{}</code>"
        open_attr = " open" if depth < 1 else ""
        out = [f"<details{open_attr}>"
               f"<summary><code>{{</code> {len(obj)} key"
               f"{'s' if len(obj) != 1 else ''} <code>}}</code></summary>"
               "<div class='json-tree'>"]
        for k, v in obj.items():
            out.append(f"<div><span class='json-key'>{esc(k)}</span>: "
                       f"{render_json(v, depth + 1)}</div>")
        out.append("</div></details>")
        return "".join(out)
    if isinstance(obj, list):
        if not obj:
            return "<code>[]</code>"
        open_attr = " open" if depth < 1 else ""
        out = [f"<details{open_attr}>"
               f"<summary><code>[</code> {len(obj)} item"
               f"{'s' if len(obj) != 1 else ''} <code>]</code></summary>"
               "<div class='json-tree'>"]
        for i, v in enumerate(obj):
            out.append(f"<div><span class='json-idx'>{i}</span>"
                       f"{render_json(v, depth + 1)}</div>")
        out.append("</div></details>")
        return "".join(out)
    if isinstance(obj, str):
        if len(obj) > 200:
            return (f"<details><summary>str ({len(obj)} chars)</summary>"
                    f"<pre>{esc(obj)}</pre></details>")
        return f"<code>{esc(json.dumps(obj))}</code>"
    return f"<code>{esc(json.dumps(obj))}</code>"


# --- per-agent fact aggregation ---------------------------------------------


def agent_facts(requests):
    """Return {agent: {...numbers for the overview table...}}."""
    by_agent = defaultdict(list)
    for r in requests:
        if r.get("extract") is not None:
            by_agent[r.get("agent", "?")].append(r)
    out = {}
    for agent, recs in by_agent.items():
        sys_samples = []
        for r in recs:
            t, _ = system_text(r)
            if t is not None:
                sys_samples.append(t)
        distinct = list(dict.fromkeys(sys_samples))
        sk = skeleton(distinct) if distinct else None
        name_lists = [nl for r in recs if (nl := tool_names(r)) is not None]
        union = set()
        for nl in name_lists:
            union.update(nl)
        key = next((tool_schema_key(r) for r in recs
                    if tool_schema_key(r) != "n/a"), "n/a")
        out[agent] = {
            "reqs": len(recs),
            "distinct": sk["distinct"] if sk else 0,
            "sys_median": int(statistics.median(len(t) for t in sys_samples))
                          if sys_samples else 0,
            "fixed": sk["fixed"] if sk else 0,
            "unique_lines": sk["unique_lines"] if sk else 0,
            "dynamic_count": len(sk["dynamic_lines"]) if sk else 0,
            "tool_count": len(union),
            "tool_key": key,
        }
    return out


# --- route renderers --------------------------------------------------------


def render_overview(paths, requests, responses):
    facts = agent_facts(requests)
    body = ["<h1>Interlude</h1>",
            "<p class='muted'>"
            f"{len(paths)} log file(s) · "
            f"{len(requests)} request record(s) · "
            f"{len(responses)} response record(s)"
            "</p>"]
    if not facts:
        body.append("<p>No analyzable records yet. Start the proxy "
                    "(<code>uv run proxy.py</code>) and capture some traffic.</p>")
    else:
        body.append("<h2>Per agent</h2><table>"
                    "<tr><th>agent</th><th>requests</th><th>distinct system</th>"
                    "<th>system median (chars)</th><th>fixed/total lines</th>"
                    "<th>dynamic slots</th><th>tools</th><th>schema key</th>"
                    "<th></th></tr>")
        for agent, f in sorted(facts.items()):
            tag = f"<span class='tag {esc(agent)}'>{esc(agent)}</span>"
            ratio = (f"{f['fixed']}/{f['unique_lines']}"
                     if f['unique_lines'] else "—")
            body.append(
                f"<tr><td>{tag}</td><td>{f['reqs']}</td>"
                f"<td>{f['distinct']}</td><td>{f['sys_median']}</td>"
                f"<td>{ratio}</td><td>{f['dynamic_count']}</td>"
                f"<td>{f['tool_count']}</td>"
                f"<td><code>{esc(f['tool_key'])}</code></td>"
                f"<td><a href='/skeleton/{esc(agent)}'>skeleton</a> · "
                f"<a href='/tools/{esc(agent)}'>tools</a></td></tr>"
            )
        body.append("</table>")
    body.append("<h2>Source files</h2><ul>")
    for p in paths:
        body.append(f"<li><code>{esc(p)}</code></li>")
    body.append("</ul>")
    return page("Overview", "".join(body))


def _usage_cells(usage):
    if not isinstance(usage, dict):
        return "", "", ""
    i_tok = usage.get("input_tokens") or usage.get("prompt_tokens") or ""
    o_tok = usage.get("output_tokens") or usage.get("completion_tokens") or ""
    details = usage.get("input_tokens_details") or {}
    cached = (details.get("cached_tokens")
              or usage.get("cache_read_input_tokens")
              or usage.get("cache_creation_input_tokens")
              or "")
    return i_tok, o_tok, cached


def render_requests(requests, responses, agent_filter=None):
    body = ["<h1>Requests</h1>",
            "<p class='muted'>"
            "<a href='/requests'>all</a> · "
            "<a href='/requests?agent=claude'>claude</a> · "
            "<a href='/requests?agent=codex'>codex</a>"
            "</p>",
            "<table><tr><th>ts (UTC)</th><th>agent</th><th>wire</th>"
            "<th>path</th><th>status</th><th>model</th>"
            "<th>tokens in / out / cached</th></tr>"]
    rows = 0
    for r in sorted(requests, key=lambda x: x.get("ts", ""), reverse=True):
        if agent_filter and r.get("agent") != agent_filter:
            continue
        xid = r.get("id")
        if not xid:
            continue  # pre-Phase-4 records have no id
        resp = responses.get(xid, {})
        rc = resp.get("reconstructed") or {}
        i_tok, o_tok, cached = _usage_cells(rc.get("usage"))
        model = rc.get("model") or (r.get("request") or {}).get("model") or ""
        status = resp.get("status")
        s_class = ("status-ok"
                   if isinstance(status, int) and status < 400
                   else "status-err")
        body.append(
            f"<tr><td>{esc(r.get('ts', ''))}</td>"
            f"<td><span class='tag {esc(r.get('agent', ''))}'>{esc(r.get('agent', ''))}</span></td>"
            f"<td>{esc(r.get('wire', ''))}</td>"
            f"<td><a href='/requests/{esc(xid)}'><code>{esc(r.get('path', ''))}</code></a></td>"
            f"<td class='{s_class}'>{esc(status) if status is not None else '—'}</td>"
            f"<td><code>{esc(model)}</code></td>"
            f"<td>{esc(i_tok)} / {esc(o_tok)} / {esc(cached)}</td></tr>"
        )
        rows += 1
    body.append("</table>")
    if rows == 0:
        body.append("<p class='muted'>(no matching requests)</p>")
    return page("Requests", "".join(body))


def render_request(xid, requests, responses):
    req = next((r for r in requests if r.get("id") == xid), None)
    if not req:
        return None
    resp = responses.get(xid)
    body = [f"<h1>Request <code>{esc(xid)}</code></h1>",
            "<dl class='kv'>"]
    for k in ("ts", "agent", "wire", "method", "path"):
        body.append(f"<dt>{esc(k)}</dt><dd><code>{esc(req.get(k, ''))}</code></dd>")
    body.append("</dl>")

    kept = req.get("headers_kept") or {}
    if kept:
        body.append("<h3>headers (kept)</h3>")
        body.append(render_json(kept))

    ex = req.get("extract") or {}

    sys_blocks = ex.get("system")
    if sys_blocks is not None:
        text, _ = system_text(req)
        n = len(text) if text else 0
        body.append(f"<h2>system <span class='muted'>({n} chars)</span></h2>")
        body.append(f"<details><summary>full text</summary>"
                    f"<pre>{esc(text)}</pre></details>")

    tools = ex.get("tools")
    if isinstance(tools, list):
        body.append(f"<h2>tools <span class='muted'>({len(tools)})</span></h2>")
        for t in tools:
            if not isinstance(t, dict):
                body.append(f"<details><summary><code>{esc(repr(t))}</code></summary></details>")
                continue
            n = (t.get("name")
                 or (t.get("function") or {}).get("name")
                 or t.get("type")
                 or "<unknown>")
            body.append(f"<details><summary><code>{esc(n)}</code></summary>"
                        f"{render_json(t)}</details>")

    msgs = ex.get("messages")
    if isinstance(msgs, list):
        body.append(f"<h2>messages <span class='muted'>({len(msgs)})</span></h2>")
        for i, m in enumerate(msgs):
            role = (m.get("role") if isinstance(m, dict) else None) or "?"
            body.append(f"<details><summary>[{i}] role=<code>{esc(role)}</code></summary>"
                        f"{render_json(m)}</details>")

    body.append("<h2>response</h2>")
    if not resp:
        body.append("<p class='muted'>(no paired response — the proxy may still "
                    "be streaming, or this record predates Phase 4)</p>")
    else:
        status = resp.get("status")
        s_class = ("status-ok"
                   if isinstance(status, int) and status < 400
                   else "status-err")
        body.append("<dl class='kv'>"
                    f"<dt>status</dt><dd class='{s_class}'>{esc(status)}</dd>"
                    f"<dt>stream</dt><dd>{esc(resp.get('stream'))}</dd>"
                    f"<dt>content-type</dt>"
                    f"<dd><code>{esc(resp.get('content_type'))}</code></dd>"
                    "</dl>")
        rc = resp.get("reconstructed")
        if rc:
            body.append("<h3>reassembled</h3>")
            if rc.get("model"):
                body.append(f"<p>model: <code>{esc(rc.get('model'))}</code></p>")
            if rc.get("text"):
                body.append("<details open><summary>text</summary>"
                            f"<pre>{esc(rc.get('text'))}</pre></details>")
            if rc.get("tool_uses"):
                body.append(f"<h4>tool uses</h4>{render_json(rc.get('tool_uses'))}")
            if rc.get("usage"):
                body.append(f"<h4>usage</h4>{render_json(rc.get('usage'))}")
            if resp.get("event_types"):
                body.append(f"<h4>event_types</h4>{render_json(resp.get('event_types'))}")
        else:
            b = resp.get("body")
            if b is not None:
                body.append("<h3>body</h3>")
                if isinstance(b, str):
                    body.append(f"<pre>{esc(b)}</pre>")
                else:
                    body.append(render_json(b))
    return page(f"Request {xid}", "".join(body))


def render_skeleton(agent, requests):
    recs = [r for r in requests
            if r.get("agent") == agent and r.get("extract") is not None]
    sys_samples = []
    for r in recs:
        t, _ = system_text(r)
        if t is not None:
            sys_samples.append(t)
    distinct = list(dict.fromkeys(sys_samples))
    body = [f"<h1>Skeleton vs slots · "
            f"<span class='tag {esc(agent)}'>{esc(agent)}</span></h1>"]
    if not distinct:
        body.append("<p>No system samples captured for this agent.</p>")
        return page(f"Skeleton {agent}", "".join(body))
    sk = skeleton(distinct)
    body.append(f"<p class='muted'>{sk['distinct']} distinct sample(s) · "
                f"{sk['fixed']}/{sk['unique_lines']} lines fixed · "
                f"{len(sk['dynamic_lines'])} dynamic slot line(s)</p>")
    if sk["distinct"] < 2:
        body.append("<p>Only 1 distinct system seen — capture more varied "
                    "sessions to surface dynamic slots.</p>"
                    "<h2>system text</h2>"
                    f"<pre>{esc(distinct[0])}</pre>")
    else:
        dyn_set = set(sk["dynamic_lines"])
        body.append("<h2>canonical sample "
                    "<span class='muted'>(first distinct, dynamic lines highlighted)</span></h2>")
        body.append("<div class='skeleton'>")
        for ln in distinct[0].split("\n"):
            cls = "dyn" if ln in dyn_set else "fixed"
            body.append(f"<span class='{cls}'>{esc(ln)}</span>\n")
        body.append("</div>")
        body.append(f"<h2>all dynamic-slot lines "
                    f"<span class='muted'>({len(sk['dynamic_lines'])})</span></h2>"
                    "<ol>")
        for ln in sk["dynamic_lines"]:
            if ln.strip():
                body.append(f"<li><code>{esc(ln[:400])}</code></li>")
        body.append("</ol>")
    return page(f"Skeleton {agent}", "".join(body))


def render_tools(agent, requests):
    recs = [r for r in requests
            if r.get("agent") == agent and r.get("extract") is not None]
    tools = None
    chosen_ts = None
    for r in sorted(recs, key=lambda x: x.get("ts", ""), reverse=True):
        ex = r.get("extract") or {}
        t = ex.get("tools")
        if isinstance(t, list) and t:
            tools = t
            chosen_ts = r.get("ts")
            break
    body = [f"<h1>Tools · <span class='tag {esc(agent)}'>{esc(agent)}</span></h1>"]
    if not tools:
        body.append("<p>No tools captured for this agent.</p>")
        return page(f"Tools {agent}", "".join(body))
    body.append(f"<p class='muted'>{len(tools)} tool(s) "
                f"from the most recent request ({esc(chosen_ts)})</p>")
    for t in tools:
        if not isinstance(t, dict):
            continue
        n = (t.get("name")
             or (t.get("function") or {}).get("name")
             or t.get("type")
             or "<unknown>")
        body.append(f"<details><summary><code>{esc(n)}</code></summary>"
                    f"{render_json(t)}</details>")
    return page(f"Tools {agent}", "".join(body))


# --- HTTP handler -----------------------------------------------------------


def make_handler(logs_glob):
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # silence default access log

        def _send(self, status, body, ctype="text/html; charset=utf-8"):
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            url = urlparse(self.path)
            path = url.path
            qs = parse_qs(url.query)

            if path == "/favicon.ico":
                self.send_response(204)
                self.end_headers()
                return

            paths, recs = load_records(logs_glob)
            requests, responses = group_records(recs)

            if path in ("/", "/overview"):
                self._send(200, render_overview(paths, requests, responses))
            elif path == "/requests":
                self._send(200, render_requests(requests, responses,
                                                (qs.get("agent") or [None])[0]))
            elif path.startswith("/requests/"):
                xid = unquote(path[len("/requests/"):])
                page_html = render_request(xid, requests, responses)
                if page_html is None:
                    self._send(404, page("Not found",
                                         f"<h1>404</h1>"
                                         f"<p>No request with id "
                                         f"<code>{esc(xid)}</code></p>"))
                else:
                    self._send(200, page_html)
            elif path.startswith("/skeleton/"):
                agent = unquote(path[len("/skeleton/"):])
                self._send(200, render_skeleton(agent, requests))
            elif path.startswith("/tools/"):
                agent = unquote(path[len("/tools/"):])
                self._send(200, render_tools(agent, requests))
            else:
                self._send(404, page("Not found", "<h1>404</h1>"))

    return Handler


def main():
    ap = argparse.ArgumentParser(description="Interlude analysis web UI.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sv = sub.add_parser("serve", help="run the local web UI")
    sv.add_argument("--port", type=int, default=8000)
    sv.add_argument("--logs", default=".interlude/log-*.jsonl",
                    help="glob for JSONL files (default: .interlude/log-*.jsonl)")
    args = ap.parse_args()

    if args.cmd == "serve":
        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", args.port),
                                                make_handler(args.logs))
        print(f"[interlude-report] http://127.0.0.1:{args.port}", flush=True)
        print(f"[interlude-report] watching {args.logs}", flush=True)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n[interlude-report] shutting down", flush=True)
            httpd.shutdown()


if __name__ == "__main__":
    main()
