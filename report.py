"""Interlude analysis web UI — a local browser-facing view over the JSONL logs.

A tiny stdlib HTTP server that reads .interlude/log-*.jsonl and renders:

  HTML                              JSON twin                       what
  /                                 /api/                           overview + per-agent stats
  /requests[?agent=…]               /api/requests[?agent=…]         list of recent exchanges
  /requests/<id>                    /api/requests/<id>              full request + paired response
  /skeleton/<agent>                 /api/skeleton/<agent>           fixed vs dynamic-slot lines
  /tools/<agent>                    /api/tools/<agent>              tools from the latest request

Bound to 127.0.0.1 only (the logs hold full prompts; never expose them on LAN).
Pure stdlib, single file, with clear section banners. Each view is split into
`model_<page>(ctx) -> dict` + `render_<page>(model) -> html`; the same model
feeds both the HTML page and its `/api/...` JSON twin, so future client-side
features (charts, search, live update) hit the JSON endpoint instead of
re-scraping HTML.
"""

import argparse
import glob
import hashlib
import html
import http.server
import json
import os
import re
import statistics
import sys
import threading
from collections import defaultdict
from urllib.parse import parse_qs, unquote, urlparse

from analyze import skeleton, system_text, tool_names, tool_schema_key


# =============================================================================
# DATA — log loading, grouping, per-agent aggregation
# =============================================================================

_CACHE = {}  # path -> (mtime, [records])
_CACHE_LOCK = threading.Lock()


def load_records(glob_pattern):
    """Return (sorted file paths, flat record list). Reuse parsed records when
    a file's mtime is unchanged. Pre-Phase-4 records get a synthesized stable
    id so they still appear in the UI. JSON decode errors are skipped silently
    because the proxy may be appending while we read.
    """
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
    requests, responses = [], {}
    for r in recs:
        kind = r.get("kind", "request")
        if kind == "request":
            requests.append(r)
        elif kind == "response":
            xid = r.get("id")
            if xid:
                responses[xid] = r
    return requests, responses


def agent_facts(requests):
    """{agent: {numbers for the overview table}}."""
    by_agent = defaultdict(list)
    for r in requests:
        if r.get("extract") is not None:
            by_agent[r.get("agent", "?")].append(r)
    out = {}
    for agent, recs in by_agent.items():
        samples = []
        for r in recs:
            t, _ = system_text(r)
            if t is not None:
                samples.append(t)
        distinct = list(dict.fromkeys(samples))
        sk = skeleton(distinct) if distinct else None
        name_lists = [nl for r in recs if (nl := tool_names(r)) is not None]
        union = set()
        for nl in name_lists:
            union.update(nl)
        out[agent] = {
            "reqs": len(recs),
            "distinct": sk["distinct"] if sk else 0,
            "sys_median": (int(statistics.median(len(t) for t in samples))
                           if samples else 0),
            "fixed": sk["fixed"] if sk else 0,
            "unique_lines": sk["unique_lines"] if sk else 0,
            "dynamic_count": len(sk["dynamic_lines"]) if sk else 0,
            "tool_count": len(union),
            "tool_key": next((tool_schema_key(r) for r in recs
                              if tool_schema_key(r) != "n/a"), "n/a"),
        }
    return out


def _usage_cells(usage):
    """Pull (input, output, cached) from either anthropic-style or openai-style usage."""
    if not isinstance(usage, dict):
        return None, None, None
    i_tok = usage.get("input_tokens") or usage.get("prompt_tokens")
    o_tok = usage.get("output_tokens") or usage.get("completion_tokens")
    cached = (
        (usage.get("input_tokens_details") or {}).get("cached_tokens")
        or usage.get("cache_read_input_tokens")
        or usage.get("cache_creation_input_tokens")
    )
    return i_tok, o_tok, cached


# =============================================================================
# MODELS — pure data prep per view. Return JSON-serializable dicts.
# A view returning None signals 404.
# =============================================================================


def model_overview(ctx):
    return {
        "files": ctx["paths"],
        "request_count": len(ctx["requests"]),
        "response_count": len(ctx["responses"]),
        "agents": agent_facts(ctx["requests"]),
    }


def model_requests(ctx):
    agent_filter = (ctx["qs"].get("agent") or [None])[0]
    rows = []
    for r in sorted(ctx["requests"], key=lambda x: x.get("ts", ""), reverse=True):
        if agent_filter and r.get("agent") != agent_filter:
            continue
        xid = r.get("id")
        if not xid:
            continue
        resp = ctx["responses"].get(xid, {})
        rc = resp.get("reconstructed") or {}
        i_tok, o_tok, cached = _usage_cells(rc.get("usage"))
        rows.append({
            "id": xid,
            "ts": r.get("ts"),
            "agent": r.get("agent"),
            "wire": r.get("wire"),
            "method": r.get("method"),
            "path": r.get("path"),
            "status": resp.get("status"),
            "model": rc.get("model") or (r.get("request") or {}).get("model"),
            "tokens_in": i_tok,
            "tokens_out": o_tok,
            "tokens_cached": cached,
        })
    return {"agent_filter": agent_filter, "rows": rows}


def model_request(ctx):
    xid = ctx["params"]["xid"]
    req = next((r for r in ctx["requests"] if r.get("id") == xid), None)
    if not req:
        return None  # → 404
    resp = ctx["responses"].get(xid)
    sys_text, _ = system_text(req)
    ex = req.get("extract") or {}
    return {
        "id": xid,
        "request": {
            "ts": req.get("ts"),
            "agent": req.get("agent"),
            "wire": req.get("wire"),
            "method": req.get("method"),
            "path": req.get("path"),
            "headers_kept": req.get("headers_kept") or {},
            "system": sys_text,
            "system_chars": len(sys_text) if sys_text else 0,
            "tools": ex.get("tools"),
            "messages": ex.get("messages"),
        },
        "response": resp,  # raw — JSON consumers and renderer share access
    }


def model_skeleton(ctx):
    agent = ctx["params"]["agent"]
    recs = [r for r in ctx["requests"]
            if r.get("agent") == agent and r.get("extract") is not None]
    samples = []
    for r in recs:
        t, _ = system_text(r)
        if t is not None:
            samples.append(t)
    distinct = list(dict.fromkeys(samples))
    if not distinct:
        return {"agent": agent, "distinct": 0, "fixed": 0, "unique_lines": 0,
                "dynamic_lines": [], "canonical": None}
    sk = skeleton(distinct)
    return {
        "agent": agent,
        "distinct": sk["distinct"],
        "fixed": sk["fixed"],
        "unique_lines": sk["unique_lines"],
        "dynamic_lines": sk["dynamic_lines"],
        "canonical": distinct[0],
    }


def model_tools(ctx):
    agent = ctx["params"]["agent"]
    recs = [r for r in ctx["requests"]
            if r.get("agent") == agent and r.get("extract") is not None]
    tools, chosen_ts = None, None
    for r in sorted(recs, key=lambda x: x.get("ts", ""), reverse=True):
        ex = r.get("extract") or {}
        t = ex.get("tools")
        if isinstance(t, list) and t:
            tools, chosen_ts = t, r.get("ts")
            break
    return {"agent": agent, "ts": chosen_ts, "tools": tools or []}


# =============================================================================
# HTML PRIMITIVES — escape, page wrapper, JSON tree, shared CSS
# =============================================================================

CSS = """
/* === reset / shared layout === */
* { box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
       max-width: 1180px; margin: 0 auto; padding: 1em 2em 4em; color: #222; line-height: 1.5; }
h1 { border-bottom: 2px solid #ddd; padding-bottom: 0.3em; margin-top: 0.4em; }
h2 { margin-top: 1.8em; color: #333; }
h3, h4 { color: #444; margin-top: 1.3em; }
code, pre { font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 0.85em; }
code { background: #f1f3f5; padding: 0.1em 0.35em; border-radius: 3px; }
pre { background: #f6f8fa; padding: 1em; border-radius: 4px; overflow-x: auto;
      white-space: pre-wrap; word-wrap: break-word; max-height: 600px;
      border: 1px solid #eaecef; }
pre code { background: none; padding: 0; }
details { margin: 0.4em 0; }
summary { cursor: pointer; padding: 0.25em 0; user-select: none; }
summary:hover { color: #06c; }
table { border-collapse: collapse; width: 100%; margin: 1em 0; }
th, td { border-bottom: 1px solid #eee; padding: 0.45em 0.7em; text-align: left;
         font-size: 0.88em; vertical-align: top; }
th { background: #f7f7f9; position: sticky; top: 0; font-weight: 600; }
tr:hover td { background: #fafafa; }

/* === shared atoms === */
.tag { display: inline-block; padding: 0.12em 0.55em; border-radius: 3px;
       background: #eef; font-size: 0.82em; color: #339; font-weight: 500; }
.tag.claude { background: #fde7d8; color: #963; }
.tag.codex { background: #def0ff; color: #246; }
.muted { color: #888; font-size: 0.88em; }
.status-ok { color: #185; font-weight: 500; }
.status-err { color: #c33; font-weight: 500; }
.kv { display: grid; grid-template-columns: max-content 1fr; gap: 0.4em 1.2em; margin: 0.6em 0; }
.kv dt { font-weight: 500; color: #555; }
.kv dd { margin: 0; }

/* === nav === */
.nav { padding: 0.6em 0; border-bottom: 1px solid #eee; margin-bottom: 1.5em; }
.nav a { margin-right: 1.2em; text-decoration: none; color: #06c; font-weight: 500; }
.nav a:hover { text-decoration: underline; }
.nav .api { float: right; font-size: 0.85em; color: #888; }
.nav .api a { color: #888; font-weight: 400; }

/* === json tree (shared) === */
.json-tree > div { margin-left: 1.5em; padding-left: 0.3em;
                   border-left: 1px solid #eee; }
.json-key { color: #905; font-weight: 500; }
.json-idx { color: #999; font-size: 0.75em; margin-right: 0.4em; }

/* === skeleton view === */
.skel-canvas { background: #f8f8fa; padding: 1em; border-radius: 4px; overflow-x: auto;
               white-space: pre-wrap; word-wrap: break-word; max-height: 700px;
               border: 1px solid #eaecef; font-family: ui-monospace, monospace;
               font-size: 0.85em; line-height: 1.6; }
.skel-fixed { color: #999; }
.skel-dyn { background: #fff3a3; padding: 0 2px; border-radius: 2px; color: #222; font-weight: 500; }
"""


def esc(value):
    return html.escape(str(value)) if value is not None else ""


def _nav(json_url=None):
    api = (f'<span class="api">JSON: '
           f'<a href="{esc(json_url)}">{esc(json_url)}</a></span>') if json_url else ""
    return (f'<nav class="nav">'
            f'<a href="/">Overview</a>'
            f'<a href="/requests">Requests</a>'
            f'{api}'
            f'</nav>')


def page(title, body_html, json_url=None):
    return ("<!doctype html><html lang='en'><head>"
            "<meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{esc(title)} · Interlude</title>"
            f"<style>{CSS}</style></head><body>"
            f"{_nav(json_url)}{body_html}</body></html>")


def render_json(obj, depth=0):
    """Recursive collapsible JSON tree. Top-level dicts/lists default to open."""
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


# =============================================================================
# RENDERERS — pure model dict -> HTML string
# =============================================================================


def render_overview(m):
    body = ["<h1>Interlude</h1>",
            "<p class='muted'>"
            f"{len(m['files'])} log file(s) · "
            f"{m['request_count']} request record(s) · "
            f"{m['response_count']} response record(s)"
            "</p>"]
    facts = m["agents"]
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
            ratio = (f"{f['fixed']}/{f['unique_lines']}"
                     if f['unique_lines'] else "—")
            body.append(
                f"<tr><td><span class='tag {esc(agent)}'>{esc(agent)}</span></td>"
                f"<td>{f['reqs']}</td>"
                f"<td>{f['distinct']}</td><td>{f['sys_median']}</td>"
                f"<td>{ratio}</td><td>{f['dynamic_count']}</td>"
                f"<td>{f['tool_count']}</td>"
                f"<td><code>{esc(f['tool_key'])}</code></td>"
                f"<td><a href='/skeleton/{esc(agent)}'>skeleton</a> · "
                f"<a href='/tools/{esc(agent)}'>tools</a></td></tr>"
            )
        body.append("</table>")
    body.append("<h2>Source files</h2><ul>")
    for p in m["files"]:
        body.append(f"<li><code>{esc(p)}</code></li>")
    body.append("</ul>")
    return page("Overview", "".join(body), json_url="/api/")


def render_requests(m):
    body = ["<h1>Requests</h1>",
            "<p class='muted'>"
            "<a href='/requests'>all</a> · "
            "<a href='/requests?agent=claude'>claude</a> · "
            "<a href='/requests?agent=codex'>codex</a>"
            "</p>",
            "<table><tr><th>ts (UTC)</th><th>agent</th><th>wire</th>"
            "<th>path</th><th>status</th><th>model</th>"
            "<th>tokens in / out / cached</th></tr>"]
    for r in m["rows"]:
        status = r["status"]
        s_class = ("status-ok"
                   if isinstance(status, int) and status < 400
                   else "status-err")
        body.append(
            f"<tr><td>{esc(r['ts'])}</td>"
            f"<td><span class='tag {esc(r['agent'])}'>{esc(r['agent'])}</span></td>"
            f"<td>{esc(r['wire'])}</td>"
            f"<td><a href='/requests/{esc(r['id'])}'><code>{esc(r['path'])}</code></a></td>"
            f"<td class='{s_class}'>{esc(status) if status is not None else '—'}</td>"
            f"<td><code>{esc(r['model'] or '')}</code></td>"
            f"<td>{esc(r['tokens_in'] or '')} / "
            f"{esc(r['tokens_out'] or '')} / "
            f"{esc(r['tokens_cached'] or '')}</td></tr>"
        )
    body.append("</table>")
    if not m["rows"]:
        body.append("<p class='muted'>(no matching requests)</p>")
    json_url = ("/api/requests?agent=" + m["agent_filter"]
                if m["agent_filter"] else "/api/requests")
    return page("Requests", "".join(body), json_url=json_url)


def render_request(m):
    req = m["request"]
    resp = m["response"]
    body = [f"<h1>Request <code>{esc(m['id'])}</code></h1>",
            "<dl class='kv'>"]
    for k in ("ts", "agent", "wire", "method", "path"):
        body.append(f"<dt>{esc(k)}</dt><dd><code>{esc(req.get(k, ''))}</code></dd>")
    body.append("</dl>")

    if req["headers_kept"]:
        body.append("<h3>headers (kept)</h3>")
        body.append(render_json(req["headers_kept"]))

    if req["system"] is not None:
        body.append(f"<h2>system <span class='muted'>"
                    f"({req['system_chars']} chars)</span></h2>"
                    f"<details><summary>full text</summary>"
                    f"<pre>{esc(req['system'])}</pre></details>")

    tools = req["tools"]
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

    msgs = req["messages"]
    if isinstance(msgs, list):
        body.append(f"<h2>messages <span class='muted'>({len(msgs)})</span></h2>")
        for i, msg in enumerate(msgs):
            role = (msg.get("role") if isinstance(msg, dict) else None) or "?"
            body.append(f"<details><summary>[{i}] role=<code>{esc(role)}</code></summary>"
                        f"{render_json(msg)}</details>")

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
    return page(f"Request {m['id']}", "".join(body),
                json_url=f"/api/requests/{m['id']}")


def render_skeleton(m):
    agent = m["agent"]
    body = [f"<h1>Skeleton vs slots · "
            f"<span class='tag {esc(agent)}'>{esc(agent)}</span></h1>"]
    if m["distinct"] == 0:
        body.append("<p>No system samples captured for this agent.</p>")
        return page(f"Skeleton {agent}", "".join(body),
                    json_url=f"/api/skeleton/{agent}")
    body.append(f"<p class='muted'>{m['distinct']} distinct sample(s) · "
                f"{m['fixed']}/{m['unique_lines']} lines fixed · "
                f"{len(m['dynamic_lines'])} dynamic slot line(s)</p>")
    if m["distinct"] < 2:
        body.append("<p>Only 1 distinct system seen — capture more varied "
                    "sessions to surface dynamic slots.</p>"
                    "<h2>system text</h2>"
                    f"<pre>{esc(m['canonical'])}</pre>")
    else:
        dyn_set = set(m["dynamic_lines"])
        body.append("<h2>canonical sample "
                    "<span class='muted'>(first distinct, dynamic lines highlighted)</span></h2>"
                    "<div class='skel-canvas'>")
        for ln in m["canonical"].split("\n"):
            cls = "skel-dyn" if ln in dyn_set else "skel-fixed"
            body.append(f"<span class='{cls}'>{esc(ln)}</span>\n")
        body.append("</div>")
        body.append(f"<h2>all dynamic-slot lines "
                    f"<span class='muted'>({len(m['dynamic_lines'])})</span></h2>"
                    "<ol>")
        for ln in m["dynamic_lines"]:
            if ln.strip():
                body.append(f"<li><code>{esc(ln[:400])}</code></li>")
        body.append("</ol>")
    return page(f"Skeleton {agent}", "".join(body),
                json_url=f"/api/skeleton/{agent}")


def render_tools(m):
    agent = m["agent"]
    body = [f"<h1>Tools · <span class='tag {esc(agent)}'>{esc(agent)}</span></h1>"]
    if not m["tools"]:
        body.append("<p>No tools captured for this agent.</p>")
        return page(f"Tools {agent}", "".join(body),
                    json_url=f"/api/tools/{agent}")
    body.append(f"<p class='muted'>{len(m['tools'])} tool(s) "
                f"from the most recent request ({esc(m['ts'])})</p>")
    for t in m["tools"]:
        if not isinstance(t, dict):
            continue
        n = (t.get("name")
             or (t.get("function") or {}).get("name")
             or t.get("type")
             or "<unknown>")
        body.append(f"<details><summary><code>{esc(n)}</code></summary>"
                    f"{render_json(t)}</details>")
    return page(f"Tools {agent}", "".join(body),
                json_url=f"/api/tools/{agent}")


# =============================================================================
# ROUTING — one table, twin /api JSON via prefix strip
# =============================================================================


ROUTES = [
    # (regex,                                                model,         renderer)
    (re.compile(r"^/$"),                                    model_overview, render_overview),
    (re.compile(r"^/requests$"),                            model_requests, render_requests),
    (re.compile(r"^/requests/(?P<xid>[0-9a-f]+)$"),         model_request,  render_request),
    (re.compile(r"^/skeleton/(?P<agent>[a-zA-Z0-9_-]+)$"),  model_skeleton, render_skeleton),
    (re.compile(r"^/tools/(?P<agent>[a-zA-Z0-9_-]+)$"),     model_tools,    render_tools),
]


def dispatch(url_path, qs, ctx_base):
    """Match against ROUTES and return (status, content_type, body_bytes).
    `/api/...` prefix selects JSON output sharing the same model functions.
    Returns None when no route matches.
    """
    # Strip /api/ prefix → JSON twin. /api alone (no slash) and /api/ both map
    # to overview-as-JSON.
    json_mode = url_path == "/api" or url_path.startswith("/api/")
    inner = url_path
    if json_mode:
        inner = url_path[4:] or "/"  # /api → "" → "/"
        if not inner.startswith("/"):
            inner = "/" + inner

    for pattern, model_fn, render_fn in ROUTES:
        match = pattern.match(inner)
        if not match:
            continue
        ctx = {**ctx_base, "qs": qs, "params": match.groupdict()}
        model = model_fn(ctx)
        if model is None:  # explicit not-found
            if json_mode:
                return (404, "application/json",
                        b'{"error":"not found"}')
            return (404, "text/html; charset=utf-8",
                    page("Not found", "<h1>404</h1>").encode("utf-8"))
        if json_mode:
            data = json.dumps(model, ensure_ascii=False, default=str).encode("utf-8")
            return 200, "application/json", data
        return 200, "text/html; charset=utf-8", render_fn(model).encode("utf-8")
    return None


# =============================================================================
# SERVER — HTTP handler + main entry point
# =============================================================================


def make_handler(logs_glob):
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            url = urlparse(self.path)
            path = unquote(url.path)
            if path == "/favicon.ico":
                self.send_response(204)
                self.end_headers()
                return

            paths, recs = load_records(logs_glob)
            requests, responses = group_records(recs)
            ctx_base = {"paths": paths, "requests": requests, "responses": responses}
            qs = parse_qs(url.query)

            result = dispatch(path, qs, ctx_base)
            if result is None:
                body = page("Not found", "<h1>404</h1>").encode("utf-8")
                self._send(404, "text/html; charset=utf-8", body)
                return
            status, ctype, body = result
            self._send(status, ctype, body)

        def _send(self, status, ctype, body):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

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
