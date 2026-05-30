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
import math
import os
import re
import statistics
import sys
import threading
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

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


def _parse_ts(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _parse_filter_ts(s):
    """Parse a UTC timestamp string from a query param or a datetime-local
    input. Accepts 'YYYY-MM-DDTHH:MM[:SS][.sss][Z]'. Returns a tz-aware UTC
    datetime, or None on any failure."""
    if not s:
        return None
    try:
        norm = s.strip().replace("Z", "")
        # datetime-local input gives 16 chars without seconds; pad them.
        if len(norm) == 16:
            norm += ":00"
        return datetime.fromisoformat(norm).replace(tzinfo=timezone.utc)
    except (ValueError, AttributeError):
        return None


_DURATION_RE = re.compile(r"^(\d+)\s*([smhd])$")


def _parse_duration(s):
    """Parse '5m' / '1h' / '24h' / '7d' / '30s' into a timedelta. None on miss."""
    if not s:
        return None
    m = _DURATION_RE.match(s.strip().lower())
    if not m:
        return None
    n = int(m.group(1))
    unit = m.group(2)
    return {
        "s": timedelta(seconds=n),
        "m": timedelta(minutes=n),
        "h": timedelta(hours=n),
        "d": timedelta(days=n),
    }.get(unit)


def _parse_int(value, default=None):
    """Read a single int from a query-string list, falling back to `default`."""
    if isinstance(value, list):
        value = value[0] if value else None
    try:
        return int(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _rtt_width_pct(seconds):
    """Map a round-trip duration to a 0–100 percent bar width on a log scale.
    10ms barely registers, 1s sits around the midpoint, 60s pins the bar."""
    if seconds is None or seconds <= 0:
        return 0
    val = math.log10(max(seconds, 0.001))
    # log10(0.01)=-2 → 0%, log10(100)=2 → 100%; clamp + give very small RTTs
    # a 2% floor so the bar is at least visible.
    pct = (val + 2) * 25
    return max(2.0, min(100.0, pct))


def _compute_time_range(qs, requests):
    """Decide (from_dt, to_dt) given the query params and the captured data.
    `from`/`to` take precedence; `since=Nm|Nh|Nd` is relative to the latest
    request ts in the data set (so 'last hour' still makes sense for old
    captures). Anything missing leaves that bound open."""
    from_dt = _parse_filter_ts((qs.get("from") or [None])[0])
    to_dt   = _parse_filter_ts((qs.get("to")   or [None])[0])
    since   = (qs.get("since") or [None])[0]
    delta = _parse_duration(since) if since else None
    if delta and not from_dt:
        latest = None
        for r in requests:
            t = _parse_ts(r.get("ts"))
            if t and (latest is None or t > latest):
                latest = t
        if latest:
            from_dt = latest - delta
    return from_dt, to_dt


def _fmt_gap(seconds):
    """Format a time delta for the timeline. Negative inputs would only occur
    on clock skew between log files; render as a leading minus and keep going."""
    if seconds is None:
        return ""
    sign = "+"
    if seconds < 0:
        sign = "−"
        seconds = -seconds
    if seconds < 1:
        return f"{sign}{seconds * 1000:.0f}ms"
    if seconds < 60:
        return f"{sign}{seconds:.1f}s"
    if seconds < 3600:
        return f"{sign}{int(seconds // 60)}m {int(seconds % 60)}s"
    if seconds < 86400:
        return f"{sign}{int(seconds // 3600)}h {int((seconds % 3600) // 60)}m"
    return f"{sign}{int(seconds // 86400)}d"


def _flatten_messages_text(msgs):
    """Concatenate every textual fragment in a messages/input array so a
    substring search can hit user prompts, assistant replies, tool inputs,
    tool results — anything readable that the model saw or wrote."""
    if not isinstance(msgs, list):
        return ""
    parts = []
    for m in msgs:
        if not isinstance(m, dict):
            continue
        c = m.get("content")
        if isinstance(c, str):
            parts.append(c)
        elif isinstance(c, list):
            for cb in c:
                if not isinstance(cb, dict):
                    continue
                for k in ("text", "input_text", "output_text"):
                    v = cb.get(k)
                    if isinstance(v, str):
                        parts.append(v)
                inp = cb.get("input")
                if isinstance(inp, (dict, list)):
                    parts.append(json.dumps(inp, ensure_ascii=False))
                elif isinstance(inp, str):
                    parts.append(inp)
                content_field = cb.get("content")
                if isinstance(content_field, str):
                    parts.append(content_field)
    return "\n\n".join(parts)


def _make_snippet(text, query, context=40):
    """Return a {before, match, after} snippet around the first case-insensitive
    occurrence of `query` in `text`, or None if no hit. Whitespace is collapsed
    so the snippet reads on a single visual line. The structured shape lets the
    HTML renderer wrap the match in <mark> while JSON consumers do their own
    highlighting."""
    if not text or not query:
        return None
    norm = re.sub(r"\s+", " ", text).strip()
    if not norm:
        return None
    idx = norm.lower().find(query.lower())
    if idx < 0:
        return None
    start = max(0, idx - context)
    end = min(len(norm), idx + len(query) + context)
    return {
        "before": ("…" if start > 0 else "") + norm[start:idx],
        "match":  norm[idx:idx + len(query)],
        "after":  norm[idx + len(query):end] + ("…" if end < len(norm) else ""),
    }


def _build_request_detail(r):
    """Project a raw request record into the dict shape used by the request
    detail view and the timeline expansion."""
    ex = r.get("extract") or {}
    sys_text, _ = system_text(r)
    return {
        "ts": r.get("ts"),
        "agent": r.get("agent"),
        "wire": r.get("wire"),
        "method": r.get("method"),
        "path": r.get("path"),
        "headers_kept": r.get("headers_kept") or {},
        "system": sys_text,
        "system_chars": len(sys_text) if sys_text else 0,
        "tools": ex.get("tools"),
        "messages": ex.get("messages"),
    }


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
    return {
        "id": xid,
        "request": _build_request_detail(req),
        "response": ctx["responses"].get(xid),
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


def _upstream_label(wire, path):
    """Derive the upstream-host label for the API lane based on wire format
    and path. The proxy listener mapping is static (see proxy.py LISTENERS),
    so we can reverse-engineer it from what we logged."""
    p = (path or "")
    if wire == "claude-messages":
        return "Anthropic"
    if wire == "codex-responses":
        if p.startswith("/backend-api"):
            return "ChatGPT backend"
        if p.startswith("/v1"):
            return "OpenAI"
        return "OpenAI"
    if wire == "codex-chat":
        return "OpenAI"
    return "API"


def model_timeline(ctx):
    """Chronological flow of exchanges as a sequence-diagram event list.
    Each captured exchange becomes TWO events: an outbound 'out' (the request
    going agent → API) and an inbound 'in' (the response coming back). The
    JSON twin returns just summary events; the HTML renderer pulls full
    request/response detail straight from ctx for inline expansion so
    /api/timeline stays compact even with large logs."""
    agent_filter = (ctx["qs"].get("agent") or [None])[0]
    from_dt, to_dt = _compute_time_range(ctx["qs"], ctx["requests"])
    q_raw = (ctx["qs"].get("q") or [""])[0]
    q = q_raw.strip()
    events = []
    prev_ts = None
    exchanges = 0
    for r in sorted(ctx["requests"], key=lambda x: x.get("ts", "")):
        if agent_filter and r.get("agent") != agent_filter:
            continue
        xid = r.get("id")
        if not xid:
            continue
        ts_filter = _parse_ts(r.get("ts"))
        if from_dt and (ts_filter is None or ts_filter < from_dt):
            continue
        if to_dt and (ts_filter is None or ts_filter >= to_dt):
            continue

        # --- full-text search filter -------------------------------------
        # Search across system text, every messages turn, and the response
        # reassembled text. Snippets are stashed per-event so the OUT card
        # shows system/messages hits and the IN card shows response hits.
        out_snippets, in_snippets = [], []
        if q:
            sys_t, _sys_blocks = system_text(r)
            sys_t = sys_t or ""
            msgs_t = _flatten_messages_text(
                (r.get("extract") or {}).get("messages"))
            paired_resp = ctx["responses"].get(xid) or {}
            resp_t = ((paired_resp.get("reconstructed") or {}).get("text") or "")
            blob = (sys_t + "\n" + msgs_t + "\n" + resp_t).lower()
            if q.lower() not in blob:
                continue
            for src, body_text, bucket in (("system", sys_t, out_snippets),
                                           ("messages", msgs_t, out_snippets),
                                           ("response", resp_t, in_snippets)):
                snip = _make_snippet(body_text, q)
                if snip:
                    snip["source"] = src
                    bucket.append(snip)
        resp = ctx["responses"].get(xid, {})
        rc = resp.get("reconstructed") or {}
        i_tok, o_tok, cached = _usage_cells(rc.get("usage"))
        ex = r.get("extract") or {}
        msg_count = len(ex.get("messages") or []) if isinstance(ex.get("messages"), list) else 0
        tool_count = len(ex.get("tools") or []) if isinstance(ex.get("tools"), list) else 0
        upstream = _upstream_label(r.get("wire"), r.get("path"))
        sys_t, _ = system_text(r)
        sys_chars = len(sys_t) if sys_t else 0

        # OUT event: the request
        ts_out = r.get("ts")
        ts_out_p = _parse_ts(ts_out)
        gap_out = None
        if prev_ts and ts_out_p:
            gap_out = (ts_out_p - prev_ts).total_seconds()
        events.append({
            "id": xid, "dir": "out", "ts": ts_out, "gap_s": gap_out,
            "agent": r.get("agent"), "wire": r.get("wire"),
            "upstream": upstream,
            "method": r.get("method"), "path": r.get("path"),
            "msg_count": msg_count, "tool_count": tool_count,
            "system_chars": sys_chars,
            "match_snippets": out_snippets,
        })
        if ts_out_p:
            prev_ts = ts_out_p

        # IN event: the response (ts == log_response time → out→in gap = RTT)
        ts_in = resp.get("ts") if resp else None
        ts_in_p = _parse_ts(ts_in) if ts_in else None
        gap_in = None
        if ts_in_p and prev_ts:
            gap_in = (ts_in_p - prev_ts).total_seconds()
        text = rc.get("text") if isinstance(rc, dict) else None
        events.append({
            "id": xid, "dir": "in", "ts": ts_in, "gap_s": gap_in,
            "agent": r.get("agent"), "wire": r.get("wire"),
            "upstream": upstream,
            "status": resp.get("status") if resp else None,
            "stream": resp.get("stream") if resp else None,
            "model": rc.get("model") or (r.get("request") or {}).get("model"),
            "tokens_in": i_tok, "tokens_out": o_tok, "tokens_cached": cached,
            "event_count": resp.get("event_count") if resp else None,
            "text_preview": (text[:80] + ("…" if len(text) > 80 else ""))
                            if isinstance(text, str) else None,
            "match_snippets": in_snippets,
        })
        if ts_in_p:
            prev_ts = ts_in_p
        exchanges += 1
    # --- Session auto-grouping -----------------------------------------------
    # Walk the events in order and start a new session whenever the gap from
    # the previous response to the next request exceeds `session_gap_s`.
    session_gap_s = _parse_int(ctx["qs"].get("session_gap"), default=30)
    sessions = []
    current_id = -1
    last_in_dt = None
    for ev in events:
        if ev["dir"] == "out":
            out_dt = _parse_ts(ev["ts"])
            should_split = (
                current_id < 0
                or (last_in_dt is not None and out_dt is not None
                    and (out_dt - last_in_dt).total_seconds() > session_gap_s)
            )
            if should_split:
                current_id += 1
                sessions.append({
                    "id": current_id,
                    "start_ts": ev["ts"],
                    "end_ts": ev["ts"],
                    "exchange_count": 0,
                    "_agents_set": set(),
                    "tokens_in": 0, "tokens_out": 0, "tokens_cached": 0,
                })
            s = sessions[-1]
            s["exchange_count"] += 1
            if ev.get("agent"):
                s["_agents_set"].add(ev["agent"])
        else:  # "in"
            if sessions:
                s = sessions[-1]
                s["end_ts"] = ev["ts"] or s["end_ts"]
                for k in ("tokens_in", "tokens_out", "tokens_cached"):
                    v = ev.get(k)
                    if isinstance(v, int):
                        s[k] += v
            in_dt = _parse_ts(ev["ts"])
            if in_dt:
                last_in_dt = in_dt
        ev["session_id"] = current_id
    for s in sessions:
        s["agents"] = sorted(s.pop("_agents_set"))

    # --- Hour density histogram ---------------------------------------------
    # Count exchanges (out events) per UTC hour. Used to render a quick visual
    # at the top of the page that doubles as a one-click filter.
    bucket_counts = defaultdict(int)
    for ev in events:
        if ev["dir"] != "out":
            continue
        t = _parse_ts(ev["ts"])
        if t:
            hour = t.replace(minute=0, second=0, microsecond=0)
            bucket_counts[hour.isoformat()] += 1
    hour_buckets = [{"hour": h, "count": c}
                    for h, c in sorted(bucket_counts.items())]

    # --- Token usage series (per-call + cumulative) -------------------------
    # Build the time series from "in" events (tokens are only known once the
    # response was reassembled). Each point carries the per-call breakdown
    # AND the running total so the chart can show both at once.
    tokens_series = []
    running = 0
    for ev in events:
        if ev["dir"] != "in":
            continue
        in_t = ev.get("tokens_in") or 0
        out_t = ev.get("tokens_out") or 0
        cached_t = ev.get("tokens_cached") or 0
        if not isinstance(in_t, int):
            in_t = 0
        if not isinstance(out_t, int):
            out_t = 0
        if not isinstance(cached_t, int):
            cached_t = 0
        total = in_t + out_t
        running += total
        tokens_series.append({
            "id": ev["id"], "ts": ev["ts"],
            "in": in_t, "out": out_t, "cached": cached_t,
            "total": total, "cumulative": running,
            "session_id": ev.get("session_id"),
            "agent": ev.get("agent"),
        })
    tokens_total = running

    return {
        "agent_filter": agent_filter,
        "search": q,
        "form": {
            "q":     q_raw,
            "agent": agent_filter or "",
            "since": (ctx["qs"].get("since") or [""])[0],
            "from":  (ctx["qs"].get("from")  or [""])[0],
            "to":    (ctx["qs"].get("to")    or [""])[0],
            "session_gap": str(session_gap_s),
        },
        "effective_from": from_dt.isoformat() if from_dt else None,
        "effective_to":   to_dt.isoformat()   if to_dt   else None,
        "session_gap_s": session_gap_s,
        "sessions": sessions,
        "hour_buckets": hour_buckets,
        "tokens_series": tokens_series,
        "tokens_total": tokens_total,
        "exchanges": exchanges,
        "event_count": len(events),
        "events": events,
    }


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

/* === timeline (sequence-diagram view) === */
.seq-controls { display: flex; justify-content: space-between; align-items: center;
                margin: 1em 0; flex-wrap: wrap; gap: 0.5em; }
.seq-controls a { color: #06c; text-decoration: none; }
.seq-controls a:hover { text-decoration: underline; }

.seq-filter {
  display: flex; flex-wrap: wrap; align-items: flex-end;
  gap: 0.6em 0.9em; margin: 0.7em 0 0.4em;
  padding: 0.7em 0.9em; background: #f7f8fa;
  border: 1px solid #eef0f3; border-radius: 5px;
}
.seq-filter label {
  display: flex; flex-direction: column; gap: 0.2em;
  font-size: 0.76em; color: #555; font-weight: 500;
}
.seq-filter input, .seq-filter select {
  font-size: 0.88em; padding: 0.3em 0.45em;
  border: 1px solid #d0d6dd; border-radius: 3px;
  background: #fff; font-family: inherit;
}
.seq-filter input[type="datetime-local"] { min-width: 14em; }
.seq-filter button {
  padding: 0.4em 1em; background: #06c; color: #fff;
  border: 0; border-radius: 3px; font-weight: 500;
  cursor: pointer; font-size: 0.9em;
}
.seq-filter button:hover { background: #048; }
.seq-filter a.reset {
  font-size: 0.85em; color: #888; text-decoration: none;
  padding: 0.4em 0.2em; align-self: center;
}
.seq-filter a.reset:hover { color: #555; text-decoration: underline; }
.seq-active-range { margin: 0.2em 0 0.4em; }
.seq-active-range code { font-size: 0.82em; }
.seq-filter-q input { min-width: 20em; width: 100%; }
.seq-filter-q { flex: 1 1 22em; }

/* --- search-hit snippets shown inside an event summary --- */
.seq-event-snippets {
  margin: 0.35em 0 0.1em;
  padding: 0.35em 0.55em;
  background: #fff9d6;
  border: 1px solid #f0e3a3;
  border-radius: 3px;
  font-size: 0.8em;
  line-height: 1.45;
}
.seq-snippet + .seq-snippet { margin-top: 0.25em; padding-top: 0.25em;
                              border-top: 1px dashed #ecdf9a; }
.seq-snippet-src {
  display: inline-block; min-width: 5em;
  font-weight: 600; color: #886a00;
  font-size: 0.78em; text-transform: uppercase; letter-spacing: 0.04em;
}
.seq-event-snippets mark {
  background: #ffd23f; padding: 0 0.18em; border-radius: 2px;
  font-weight: 600; color: #222;
}

/* --- per-hour density histogram --- */
.seq-histogram {
  margin: 0.6em 0 1em;
  padding: 0.6em 0.8em;
  background: #fafbfc;
  border: 1px solid #eef0f3;
  border-radius: 5px;
}
.seq-hist-title { font-size: 0.78em; margin-bottom: 0.4em; }
.seq-hist-row {
  display: grid;
  grid-template-columns: 110px 1fr 40px;
  gap: 0.5em; align-items: center;
  text-decoration: none; color: inherit;
  padding: 0.12em 0.3em; border-radius: 3px;
}
.seq-hist-row:hover { background: #eef3fa; }
.seq-hist-label { font-family: ui-monospace, monospace; font-size: 0.74em; color: #666; }
.seq-hist-bar {
  display: inline-block; height: 0.85em;
  background: linear-gradient(to right, #b8d4ed, #3a78c8);
  border-radius: 2px; min-width: 2px;
}
.seq-hist-count { font-size: 0.78em; color: #555; text-align: right; }

/* --- session group --- */
.seq-session { margin: 0.6em 0 1em; border-top: 1px solid #e3e6ea; }
.seq-session[open] > .seq-session-header { background: #eef3fa; }
.seq-session-header {
  display: grid;
  grid-template-columns: auto 1fr auto auto;
  gap: 0 0.9em; align-items: baseline;
  padding: 0.5em 0.7em;
  background: #f4f6f9;
  border-radius: 4px;
  cursor: pointer;
  list-style: none;
  font-size: 0.86em;
}
.seq-session-header::-webkit-details-marker { display: none; }
.seq-session-header::marker { content: ''; }
.seq-session-id { font-weight: 600; color: #333; }
.seq-session-range { color: #555; }
.seq-session-range code { font-size: 0.78em; background: transparent; padding: 0; }
.seq-session-meta { color: #666; font-size: 0.82em; white-space: nowrap; }
.seq-session-link {
  font-size: 0.8em; color: #06c; text-decoration: none;
  padding: 0.15em 0.5em; border-radius: 3px;
}
.seq-session-link:hover { background: #d8e8f8; text-decoration: underline; }

/* --- RTT bar inside an "in" arrow's summary --- */
.seq-rtt-row {
  display: flex; align-items: center; gap: 0.5em;
  padding: 0.05em 0.4em;
}
.seq-rtt-bar {
  display: inline-block; height: 0.45em;
  background: linear-gradient(to right, #5cb85c, #f0ad4e, #d9534f);
  border-radius: 1px;
  min-width: 2px;
}
.seq-rtt-label { font-size: 0.75em; font-family: ui-monospace, monospace; }

/* --- token usage chart --- */
.seq-tokens {
  margin: 0.8em 0 1em;
  padding: 0.7em 0.9em;
  background: #fafbfc;
  border: 1px solid #eef0f3;
  border-radius: 5px;
}
.seq-tokens-title {
  display: flex; justify-content: space-between; align-items: center;
  flex-wrap: wrap; gap: 0.3em 0.6em;
  font-size: 0.78em; margin: 0.3em 0 0.2em;
}
.seq-tokens-title:first-child { margin-top: 0; }
.seq-tokens-legend {
  display: inline-flex; align-items: center; gap: 0.3em 0.7em;
  flex-wrap: wrap; font-size: 0.78em; color: #555;
}
.seq-tokens-legend .dot {
  display: inline-block; width: 0.7em; height: 0.7em; border-radius: 2px;
  vertical-align: -1px; margin-right: 0.1em;
}
.seq-tokens-legend .dot-in     { background: #5a9c4f; }
.seq-tokens-legend .dot-cached { background: #b8b8c0; }
.seq-tokens-legend .dot-out    { background: #3a78c8; }
.seq-tokens-svg {
  width: 100%; height: 90px;
  background: #fff;
  border: 1px solid #eef0f3;
  border-radius: 3px;
  display: block;
}
.seq-tokens-axis {
  display: flex; justify-content: space-between;
  font-family: ui-monospace, monospace; font-size: 0.72em;
  margin-top: 0.3em;
}

.seq { margin: 1em 0; }

/* The lanes header and every event row share the same 5-column template so
   the agent / API "lanes" align vertically across rows like lifelines. */
.seq-lanes, .seq-event {
  display: grid;
  grid-template-columns: 110px 55px 95px 1fr 110px;
  gap: 0 0.5em;
  align-items: start;
}

/* Top lane labels */
.seq-lanes { padding: 0.4em 0 0.7em; margin-bottom: 0.4em;
             border-bottom: 1px solid #eee; }
.seq-lane-l, .seq-lane-r {
  padding: 0.45em 0.8em; background: #f0f1f4; border-radius: 4px;
  text-align: center; font-weight: 600; font-size: 0.88em; color: #444;
}
.seq-lane-l { grid-column: 3; justify-self: stretch; }
.seq-lane-r { grid-column: 5; justify-self: stretch; }
.seq-lane-spacer { grid-column: 4; }

/* Events list */
.seq-events { list-style: none; padding: 0; margin: 0; }

.seq-event { padding: 0.35em 0; border-bottom: 1px dashed transparent; }
.seq-event:hover { background: rgba(6, 100, 204, 0.02); }

.seq-time {
  grid-column: 1;
  font-family: ui-monospace, monospace; font-size: 0.74em; color: #666;
  text-align: right; padding-top: 0.45em; word-break: break-all;
}
.seq-gap {
  grid-column: 2;
  font-size: 0.74em; color: #999;
  text-align: right; padding-top: 0.45em;
}
.seq-lane-l-cell {
  grid-column: 3;
  padding-top: 0.35em; text-align: right; padding-right: 0.3em;
  white-space: nowrap;
}
.seq-msg {
  grid-column: 4;
  min-width: 0;  /* let inner flex children shrink instead of overflowing */
}
.seq-lane-r-cell {
  grid-column: 5;
  padding-top: 0.45em; text-align: left; padding-left: 0.3em;
  white-space: nowrap;
}
.seq-api-label { font-size: 0.8em; color: #666; }

/* Clickable summary: arrow line on top, label + meta below */
.seq-msg > summary {
  cursor: pointer;
  padding: 0.25em 0.4em;
  list-style: none;
  border-radius: 3px;
  display: flex; flex-direction: column; gap: 0.2em;
}
.seq-msg > summary::-webkit-details-marker { display: none; }
.seq-msg > summary::marker { content: ''; }
.seq-msg > summary:hover { background: #f7f8fa; }

.seq-arrow-track {
  display: flex; align-items: center;
  height: 0.95em; min-width: 0;
}
.seq-arrow-line { flex: 1; height: 2px; background: #888; border-radius: 1px; }
.seq-arrow-head { font-size: 0.85em; line-height: 1;
                  padding: 0 0.15em; color: #888; }

.seq-event[data-dir="out"] .seq-arrow-head { order: 2; }
.seq-event[data-dir="in"]  .seq-arrow-head { order: -1; }

.seq-event[data-agent="claude"] .seq-arrow-line { background: #c8743a; }
.seq-event[data-agent="claude"] .seq-arrow-head { color: #c8743a; }
.seq-event[data-agent="codex"] .seq-arrow-line { background: #3a78c8; }
.seq-event[data-agent="codex"] .seq-arrow-head { color: #3a78c8; }

.seq-label-row {
  display: flex; justify-content: space-between; align-items: baseline;
  gap: 0.6em; font-size: 0.86em; min-width: 0;
}
.seq-label {
  font-weight: 500; min-width: 0;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.seq-meta {
  font-size: 0.78em; color: #777; white-space: nowrap; flex-shrink: 0;
}

.seq-msg-body {
  margin: 0.5em 0 0.8em;
  padding: 0.7em 0.8em;
  border: 1px solid #eef0f3; border-radius: 4px;
  background: #fdfdfe;
}
.seq-msg-body h4 { margin-top: 0.9em; margin-bottom: 0.2em; }
.seq-msg-body h4:first-child { margin-top: 0; }

/* === overview CTA === */
.ov-cta { display: inline-block; padding: 0.4em 0.9em; margin: 0.4em 0;
          border-radius: 4px; background: #06c; color: #fff;
          text-decoration: none; font-weight: 500; }
.ov-cta:hover { background: #048; }
"""


def esc(value):
    return html.escape(str(value)) if value is not None else ""


def _nav(json_url=None):
    api = (f'<span class="api">JSON: '
           f'<a href="{esc(json_url)}">{esc(json_url)}</a></span>') if json_url else ""
    return (f'<nav class="nav">'
            f'<a href="/">Overview</a>'
            f'<a href="/timeline">Timeline</a>'
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


def _render_tokens_chart(tokens_series):
    """SVG chart of token usage over time: stacked per-call bars on top
    (input - cached / cached / output) + cumulative running total below.
    Both panels are time-positioned across the same x range so a tall bar
    lines up with the matching step in the cumulative curve."""
    if not tokens_series:
        return ""
    parsed = [(p, _parse_ts(p["ts"])) for p in tokens_series]
    parsed = [(p, t) for p, t in parsed if t is not None]
    if not parsed:
        return ""

    if len(parsed) == 1:
        only = parsed[0][0]
        return (
            f"<div class='seq-tokens'>"
            f"<div class='seq-tokens-title muted'>tokens</div>"
            f"<p class='muted'>only one data point: "
            f"{only['in']:,} in / {only['out']:,} out / "
            f"{only['cached']:,} cached</p>"
            f"</div>"
        )

    min_ts = min(t for _, t in parsed)
    max_ts = max(t for _, t in parsed)
    span = (max_ts - min_ts).total_seconds() or 1.0
    max_call = max(p["total"] for p, _ in parsed) or 1
    max_cum = parsed[-1][0]["cumulative"] or 1
    cum_total = parsed[-1][0]["cumulative"]

    W, H_BAR, H_CUM = 1000, 90, 90
    bar_w = max(2, min(10, W / max(1, len(parsed))))

    # Per-call stacked bars: in_only on top, cached in middle, output at bottom
    # (visually clearer than billing-order; reads as "billed prompt / cached /
    # generation" since they live in a single bar). Order chosen so cached
    # sits between input and output, matching how a reader skims the cost.
    bars = []
    for p, t in parsed:
        x = (t - min_ts).total_seconds() / span * (W - bar_w)
        total = p["total"]
        if total <= 0:
            # Mark zero-token events with a tiny tick so retries are visible
            bars.append(f"<line x1='{x:.1f}' y1='{H_BAR-1}' x2='{x + bar_w:.1f}' "
                        f"y2='{H_BAR-1}' stroke='#c33' stroke-width='1.5' "
                        f"opacity='0.5'/>")
            continue
        h_total = (total / max_call) * H_BAR
        # Stack (top → bottom): in_only, cached, out
        in_only = max(0, p["in"] - p["cached"])
        cached = p["cached"]
        out = p["out"]
        denom = in_only + cached + out or 1
        h_in     = (in_only / denom) * h_total
        h_cached = (cached  / denom) * h_total
        h_out    = (out     / denom) * h_total
        y0 = H_BAR - h_total
        bars.append(f"<rect x='{x:.1f}' y='{y0:.2f}' width='{bar_w:.1f}' "
                    f"height='{h_in:.2f}' fill='#5a9c4f'/>")
        bars.append(f"<rect x='{x:.1f}' y='{(y0 + h_in):.2f}' width='{bar_w:.1f}' "
                    f"height='{h_cached:.2f}' fill='#b8b8c0'/>")
        bars.append(f"<rect x='{x:.1f}' y='{(y0 + h_in + h_cached):.2f}' "
                    f"width='{bar_w:.1f}' height='{h_out:.2f}' fill='#3a78c8'/>")

    # Cumulative line as a step path (tokens jump at the moment they land)
    pts = ["0," + f"{H_CUM}"]
    last_y = H_CUM
    for p, t in parsed:
        x = (t - min_ts).total_seconds() / span * W
        y = H_CUM - (p["cumulative"] / max_cum) * H_CUM
        pts.append(f"{x:.1f},{last_y:.2f}")
        pts.append(f"{x:.1f},{y:.2f}")
        last_y = y
    pts.append(f"{W},{last_y:.2f}")
    poly = " ".join(pts)

    start_lbl = min_ts.strftime("%Y-%m-%d %H:%M:%S")
    end_lbl   = max_ts.strftime("%Y-%m-%d %H:%M:%S")

    return (
        "<div class='seq-tokens'>"
        f"<div class='seq-tokens-title'>"
        f"<span class='muted'>tokens per call</span> "
        f"<span class='seq-tokens-legend'>"
        f"<span class='dot dot-in'></span> input(billable) "
        f"<span class='dot dot-cached'></span> cached "
        f"<span class='dot dot-out'></span> output "
        f"<span class='muted'>· max {max_call:,}</span>"
        f"</span>"
        f"</div>"
        f"<svg viewBox='0 0 {W} {H_BAR + 2}' preserveAspectRatio='none' "
        f"class='seq-tokens-svg'>"
        f"{''.join(bars)}"
        f"</svg>"
        f"<div class='seq-tokens-title'>"
        f"<span class='muted'>cumulative</span> "
        f"<span class='muted'>· total {cum_total:,} tokens</span>"
        f"</div>"
        f"<svg viewBox='0 0 {W} {H_CUM + 2}' preserveAspectRatio='none' "
        f"class='seq-tokens-svg'>"
        f"<polygon points='{poly}' fill='#3a78c8' fill-opacity='0.12' "
        f"stroke='none'/>"
        f"<polyline points='{poly}' fill='none' stroke='#3a78c8' "
        f"stroke-width='2'/>"
        f"</svg>"
        f"<div class='seq-tokens-axis muted'>"
        f"<span>{esc(start_lbl)}</span><span>{esc(end_lbl)}</span>"
        f"</div>"
        f"</div>"
    )


def _render_request_detail(req, resp, *, top_h="h2", messages_open=False,
                           text_open=True, parts="both"):
    """Inner HTML for a captured exchange. `parts` controls which half renders:
    - 'both' (default): headers + system + tools + messages, then response —
      used by /requests/<id>.
    - 'req' : headers + system + tools + messages only — used by the request
      arrow's inline expansion in the sequence-diagram view.
    - 'resp': response only — used by the response arrow's expansion.
    `top_h` lets the caller pick the heading depth, and `messages_open` /
    `text_open` decide which sub-sections start expanded (for behavioral
    analysis the messages and the model reply are the focal points)."""
    sub_h = "h3" if top_h == "h2" else "h4"
    out = []
    show_req = parts in ("both", "req")
    show_resp = parts in ("both", "resp")
    if show_req:
        if req.get("headers_kept"):
            out.append(f"<{sub_h}>headers (kept)</{sub_h}>")
            out.append(render_json(req["headers_kept"]))
        if req.get("system") is not None:
            out.append(f"<{top_h}>system <span class='muted'>"
                       f"({req.get('system_chars', 0)} chars)</span></{top_h}>"
                       f"<details><summary>full text</summary>"
                       f"<pre>{esc(req['system'])}</pre></details>")
        tools = req.get("tools")
        if isinstance(tools, list):
            out.append(f"<{top_h}>tools <span class='muted'>({len(tools)})</span></{top_h}>")
            for t in tools:
                if not isinstance(t, dict):
                    out.append(f"<details><summary><code>{esc(repr(t))}</code></summary></details>")
                    continue
                n = (t.get("name") or (t.get("function") or {}).get("name")
                     or t.get("type") or "<unknown>")
                out.append(f"<details><summary><code>{esc(n)}</code></summary>"
                           f"{render_json(t)}</details>")
        msgs = req.get("messages")
        if isinstance(msgs, list):
            out.append(f"<{top_h}>messages <span class='muted'>({len(msgs)})</span></{top_h}>")
            for i, msg in enumerate(msgs):
                role = (msg.get("role") if isinstance(msg, dict) else None) or "?"
                open_attr = " open" if messages_open else ""
                out.append(f"<details{open_attr}><summary>[{i}] role=<code>{esc(role)}</code></summary>"
                           f"{render_json(msg)}</details>")
    if show_resp:
        if parts == "both":
            out.append(f"<{top_h}>response</{top_h}>")
        if not resp:
            out.append("<p class='muted'>(no paired response — the proxy may still "
                       "be streaming, or this record predates Phase 4)</p>")
        else:
            status = resp.get("status")
            s_class = ("status-ok" if isinstance(status, int) and status < 400
                       else "status-err")
            out.append("<dl class='kv'>"
                       f"<dt>status</dt><dd class='{s_class}'>{esc(status)}</dd>"
                       f"<dt>stream</dt><dd>{esc(resp.get('stream'))}</dd>"
                       f"<dt>content-type</dt>"
                       f"<dd><code>{esc(resp.get('content_type'))}</code></dd>"
                       "</dl>")
            rc = resp.get("reconstructed")
            if rc:
                out.append(f"<{sub_h}>reassembled</{sub_h}>")
                if rc.get("model"):
                    out.append(f"<p>model: <code>{esc(rc.get('model'))}</code></p>")
                if rc.get("text"):
                    text_attr = " open" if text_open else ""
                    out.append(f"<details{text_attr}><summary>text</summary>"
                               f"<pre>{esc(rc.get('text'))}</pre></details>")
                if rc.get("tool_uses"):
                    out.append(f"<h4>tool uses</h4>{render_json(rc.get('tool_uses'))}")
                if rc.get("usage"):
                    out.append(f"<h4>usage</h4>{render_json(rc.get('usage'))}")
                if resp.get("event_types"):
                    out.append(f"<h4>event_types</h4>{render_json(resp.get('event_types'))}")
            else:
                b = resp.get("body")
                if b is not None:
                    out.append(f"<{sub_h}>body</{sub_h}>")
                    if isinstance(b, str):
                        out.append(f"<pre>{esc(b)}</pre>")
                    else:
                        out.append(render_json(b))
    return "".join(out)


# =============================================================================
# RENDERERS — pure model dict -> HTML string
# =============================================================================


def render_overview(m, ctx=None):
    body = ["<h1>Interlude</h1>",
            "<p class='muted'>"
            f"{len(m['files'])} log file(s) · "
            f"{m['request_count']} request record(s) · "
            f"{m['response_count']} response record(s)"
            "</p>",
            "<p><a class='ov-cta' href='/timeline'>▶ Open timeline</a> "
            "<span class='muted'>— chronological exchanges, click any card to "
            "expand its system / tools / messages / response inline</span></p>"]
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


def render_requests(m, ctx=None):
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


def render_request(m, ctx=None):
    req = m["request"]
    resp = m["response"]
    body = [f"<h1>Request <code>{esc(m['id'])}</code></h1>",
            "<dl class='kv'>"]
    for k in ("ts", "agent", "wire", "method", "path"):
        body.append(f"<dt>{esc(k)}</dt><dd><code>{esc(req.get(k, ''))}</code></dd>")
    body.append("</dl>")
    body.append(_render_request_detail(req, resp, top_h="h2"))
    return page(f"Request {m['id']}", "".join(body),
                json_url=f"/api/requests/{m['id']}")


def render_skeleton(m, ctx=None):
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


def render_tools(m, ctx=None):
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


def render_timeline(m, ctx=None):
    """Sequence-diagram-style flow with two lanes (agent ↔ API). Each captured
    exchange becomes two arrows: out (request) and in (response). Clicking an
    arrow expands only the matching half — req shows headers/system/tools/
    messages; resp shows reassembled text/usage/event_types."""
    form = m["form"]
    # Reconstruct the JSON URL with the same query params so the link in the
    # nav matches what is currently filtered. quote_plus handles arbitrary
    # text values (the search box can contain spaces, slashes, etc.).
    qs_bits = []
    for k in ("q", "agent", "since", "from", "to", "session_gap"):
        v = form.get(k)
        if v and (k != "session_gap" or v != "30"):
            qs_bits.append(f"{k}={quote_plus(v)}")
    json_url = "/api/timeline" + ("?" + "&".join(qs_bits) if qs_bits else "")

    def opt(name, value, current, label=None):
        sel = " selected" if value == current else ""
        return f"<option value='{esc(value)}'{sel}>{esc(label or value or '—')}</option>"

    agent_opts = "".join(opt("agent", v, form["agent"], lbl)
                         for v, lbl in (("", "all"), ("claude", "claude"),
                                        ("codex", "codex")))
    since_opts = "".join(opt("since", v, form["since"], lbl)
                         for v, lbl in (("", "—"), ("5m", "last 5m"),
                                        ("1h", "last 1h"), ("6h", "last 6h"),
                                        ("24h", "last 24h"), ("7d", "last 7d")))

    active = ""
    if m["effective_from"] or m["effective_to"]:
        from_lbl = esc(m["effective_from"] or "(start)")
        to_lbl = esc(m["effective_to"] or "(end)")
        active = (f"<p class='seq-active-range muted'>"
                  f"active range (UTC): <code>{from_lbl}</code> → "
                  f"<code>{to_lbl}</code></p>")

    body = [
        "<h1>Timeline</h1>",
        f"<form class='seq-filter' method='get' action='/timeline'>"
        f"<label class='seq-filter-q'>search<input type='search' name='q' "
        f"value='{esc(form['q'])}' placeholder='system / messages / response text'>"
        f"</label>"
        f"<label>agent<select name='agent'>{agent_opts}</select></label>"
        f"<label>since<select name='since'>{since_opts}</select></label>"
        f"<label>from (UTC)<input type='datetime-local' name='from' "
        f"value='{esc(form['from'])}'></label>"
        f"<label>to (UTC)<input type='datetime-local' name='to' "
        f"value='{esc(form['to'])}'></label>"
        f"<label>session gap (s)<input type='number' name='session_gap' "
        f"min='0' step='1' value='{esc(form['session_gap'])}' "
        f"style='width: 5em'></label>"
        f"<button type='submit'>Apply</button>"
        f"<a class='reset' href='/timeline'>reset</a>"
        f"</form>",
        active,
        f"<p class='muted'>{m['exchanges']} exchange(s) · "
        f"{m['event_count']} arrows · {len(m['sessions'])} session(s) · "
        f"gap threshold {m['session_gap_s']}s</p>",
    ]

    # --- Per-hour density histogram (clickable to filter that hour) ---------
    if m["hour_buckets"]:
        max_count = max(b["count"] for b in m["hour_buckets"])
        body.append("<div class='seq-histogram'>"
                    "<div class='seq-hist-title muted'>exchanges per hour "
                    "(click a bar to filter)</div>")
        agent_q = (f"&agent={esc(form['agent'])}" if form["agent"] else "")
        for b in m["hour_buckets"]:
            try:
                hour_dt = datetime.fromisoformat(b["hour"])
            except ValueError:
                continue
            pct = (b["count"] / max_count) * 100 if max_count else 0
            from_str = hour_dt.strftime("%Y-%m-%dT%H:%M")
            to_str = (hour_dt + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M")
            href = f"/timeline?from={from_str}&to={to_str}{agent_q}"
            label = hour_dt.strftime("%m-%d %H:00")
            body.append(
                f"<a class='seq-hist-row' href='{esc(href)}' "
                f"title='{esc(b['hour'])} UTC'>"
                f"<span class='seq-hist-label'>{esc(label)}</span>"
                f"<span class='seq-hist-bar' style='width: {pct:.1f}%'></span>"
                f"<span class='seq-hist-count'>{b['count']}</span>"
                f"</a>"
            )
        body.append("</div>")

    # --- Token usage chart (per-call stacked bars + cumulative line) --------
    body.append(_render_tokens_chart(m["tokens_series"]))

    if not m["events"]:
        body.append("<p class='muted'>(no exchanges matched — capture some "
                    "traffic, or relax the filter)</p>")
        return page("Timeline", "".join(body), json_url=json_url)

    body.append(
        "<div class='seq'>"
        "<div class='seq-lanes'>"
        "<div class='seq-lane-l'>agent</div>"
        "<div class='seq-lane-spacer'></div>"
        "<div class='seq-lane-r'>API</div>"
        "</div>"
    )
    # Build a session_id → summary index for quick lookup while iterating events
    sessions_by_id = {s["id"]: s for s in m["sessions"]}
    current_session = -1

    by_id = {r.get("id"): r for r in (ctx or {}).get("requests", [])}
    responses = (ctx or {}).get("responses", {})

    for ev in m["events"]:
        agent = ev.get("agent") or "?"
        direction = ev["dir"]
        gap = _fmt_gap(ev.get("gap_s"))
        ts = ev.get("ts") or ""
        raw = by_id.get(ev["id"])
        resp = responses.get(ev["id"])

        # Open / close session group as we cross boundaries
        if ev.get("session_id") != current_session:
            if current_session >= 0:
                body.append("</ol></details>")
            current_session = ev["session_id"]
            s = sessions_by_id.get(current_session)
            if s:
                # Compute duration label
                start_dt = _parse_ts(s["start_ts"])
                end_dt = _parse_ts(s["end_ts"])
                if start_dt and end_dt and end_dt >= start_dt:
                    dur = _fmt_gap((end_dt - start_dt).total_seconds()).lstrip("+")
                else:
                    dur = "—"
                # Per-session filter link (snaps to second precision; +1s on
                # the end so the boundary event stays inside).
                s_from = (start_dt.strftime("%Y-%m-%dT%H:%M:%S")
                          if start_dt else (s["start_ts"] or "")[:19])
                s_to = ((end_dt + timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%S")
                        if end_dt else (s["end_ts"] or "")[:19])
                agent_q = f"&agent={esc(form['agent'])}" if form["agent"] else ""
                filter_href = f"/timeline?from={s_from}&to={s_to}{agent_q}"
                agents_lbl = ", ".join(s["agents"]) if s["agents"] else "?"
                body.append(
                    f"<details class='seq-session' open>"
                    f"<summary class='seq-session-header'>"
                    f"<span class='seq-session-id'>Session #{s['id'] + 1}</span>"
                    f"<span class='seq-session-range'>"
                    f"<code>{esc(s['start_ts'])}</code> → "
                    f"<code>{esc(s['end_ts'])}</code> "
                    f"<span class='muted'>({esc(dur)})</span>"
                    f"</span>"
                    f"<span class='seq-session-meta'>"
                    f"{s['exchange_count']} exchange(s) · "
                    f"agents: {esc(agents_lbl)} · "
                    f"{s['tokens_in']:,} in / {s['tokens_out']:,} out / "
                    f"{s['tokens_cached']:,} cached"
                    f"</span>"
                    f"<a class='seq-session-link' href='{esc(filter_href)}'>"
                    f"filter to this session →</a>"
                    f"</summary>"
                    f"<ol class='seq-events'>"
                )

        if direction == "out":
            # Outbound: agent → API
            label = (f"<code>{esc(ev.get('method') or '')} "
                     f"{esc(ev.get('path') or '')}</code>")
            meta = (f"<span class='muted'>msgs:{ev['msg_count']} · "
                    f"tools:{ev['tool_count']} · "
                    f"system {ev['system_chars']} chars</span>")
            detail = (_render_request_detail(_build_request_detail(raw) if raw else {},
                                             None, top_h="h4",
                                             messages_open=True, parts="req")
                      if raw else
                      "<p class='muted'>(no request record)</p>")
        else:
            # Inbound: API → agent
            status = ev.get("status")
            s_class = ("status-ok" if isinstance(status, int) and status < 400
                       else "status-err")
            tokens_bits = []
            for k, lbl in (("tokens_in", "in"), ("tokens_out", "out"),
                           ("tokens_cached", "cached")):
                if ev.get(k) is not None:
                    tokens_bits.append(f"{esc(ev[k])} {lbl}")
            tokens_str = (" · " + " · ".join(tokens_bits)) if tokens_bits else ""
            preview = ev.get("text_preview")
            text_part = (f" · text=<code>{esc(repr(preview))}</code>"
                         if preview else "")
            label = (f"<span class='{s_class}'>"
                     f"{esc(status) if status is not None else '—'}</span>"
                     f"{text_part}")
            meta = (f"<span class='muted'>"
                    f"events:{ev.get('event_count') if ev.get('event_count') is not None else '—'}"
                    f"{tokens_str}</span>")
            detail = _render_request_detail({}, resp, top_h="h4",
                                            messages_open=False, text_open=True,
                                            parts="resp")

        agent_chip = (f"<span class='tag {esc(agent)}'>{esc(agent)}</span>"
                      if direction == "out" else "")
        api_chip = (f"<span class='muted seq-api-label'>{esc(ev.get('upstream') or 'API')}</span>"
                    if direction == "out" else "")
        # On 'in' arrows show the API origin on the left side
        in_origin = (f"<span class='muted seq-api-label'>{esc(ev.get('upstream') or 'API')}</span>"
                     if direction == "in" else "")
        in_target = (f"<span class='tag {esc(agent)}'>{esc(agent)}</span>"
                     if direction == "in" else "")

        # Optional RTT bar for "in" arrows (gap_s is the response time relative
        # to the matching "out").
        rtt_row = ""
        if direction == "in" and ev.get("gap_s") is not None and ev["gap_s"] >= 0:
            pct = _rtt_width_pct(ev["gap_s"])
            rtt_row = (
                f"<div class='seq-rtt-row'>"
                f"<span class='seq-rtt-bar' style='width: {pct:.1f}%'></span>"
                f"<span class='seq-rtt-label muted'>RTT "
                f"{esc(_fmt_gap(ev['gap_s']).lstrip('+'))}</span>"
                f"</div>"
            )

        # Search-hit snippets (stays visible even when the card is collapsed,
        # so the match is the first thing the user sees).
        snippets_html = ""
        if ev.get("match_snippets"):
            parts = ["<div class='seq-event-snippets'>"]
            for snip in ev["match_snippets"]:
                parts.append(
                    f"<div class='seq-snippet'>"
                    f"<span class='seq-snippet-src'>{esc(snip.get('source', '?'))}:</span> "
                    f"{esc(snip['before'])}"
                    f"<mark>{esc(snip['match'])}</mark>"
                    f"{esc(snip['after'])}"
                    f"</div>"
                )
            parts.append("</div>")
            snippets_html = "".join(parts)

        body.append(
            f"<li class='seq-event' data-dir='{esc(direction)}' "
            f"data-agent='{esc(agent)}' id='ex-{esc(ev['id'])}-{esc(direction)}'>"
            f"<span class='seq-time'>{esc(ts)}</span>"
            f"<span class='seq-gap'>{esc(gap)}</span>"
            f"<span class='seq-lane-l-cell'>{agent_chip}{in_origin}</span>"
            f"<details class='seq-msg'>"
            f"<summary>"
            f"<span class='seq-arrow-track'>"
            f"<span class='seq-arrow-line'></span>"
            f"<span class='seq-arrow-head'>{('▶' if direction == 'out' else '◀')}</span>"
            f"</span>"
            f"<span class='seq-label-row'>"
            f"<span class='seq-label'>{label}</span>"
            f"<span class='seq-meta'>{meta}</span>"
            f"</span>"
            f"{rtt_row}"
            f"{snippets_html}"
            f"</summary>"
            f"<div class='seq-msg-body'>"
            f"<p class='muted'><a href='/requests/{esc(ev['id'])}'>open full page →</a></p>"
            f"{detail}"
            f"</div>"
            f"</details>"
            f"<span class='seq-lane-r-cell'>{api_chip}{in_target}</span>"
            f"</li>"
        )
    # Close the last open session group
    if current_session >= 0:
        body.append("</ol></details>")
    body.append("</div>")
    return page("Timeline", "".join(body), json_url=json_url)


# =============================================================================
# ROUTING — one table, twin /api JSON via prefix strip
# =============================================================================


ROUTES = [
    # (regex,                                                model,          renderer)
    (re.compile(r"^/$"),                                    model_overview,  render_overview),
    (re.compile(r"^/timeline$"),                            model_timeline,  render_timeline),
    (re.compile(r"^/requests$"),                            model_requests,  render_requests),
    (re.compile(r"^/requests/(?P<xid>[0-9a-f]+)$"),         model_request,   render_request),
    (re.compile(r"^/skeleton/(?P<agent>[a-zA-Z0-9_-]+)$"),  model_skeleton,  render_skeleton),
    (re.compile(r"^/tools/(?P<agent>[a-zA-Z0-9_-]+)$"),     model_tools,     render_tools),
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
        return 200, "text/html; charset=utf-8", render_fn(model, ctx).encode("utf-8")
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
