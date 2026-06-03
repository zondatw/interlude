"""Interlude analysis layer (Phase 5).

Reads the JSONL captured by proxy.py and reports the prompt architecture of each
agent: how the system prompt / tools / messages are shaped, which parts of the
system prompt are a FIXED SKELETON (present in every request) versus DYNAMIC
SLOTS (vary between requests), and how Claude Code and Codex differ structurally.

Decoupled from interception — it only reads files. Pure stdlib.

Usage:
    uv run analyze.py                       # all .interlude/log-*.jsonl
    uv run analyze.py path/to/log.jsonl ... # specific files/globs
    uv run analyze.py --agent claude        # filter to one agent
    uv run analyze.py --max-slots 30        # show more dynamic-slot lines
"""

import argparse
import glob
import json
import statistics
import sys
from collections import Counter, defaultdict

# --- field extraction (tolerant of the different wire shapes) ---------------


def system_text(rec):
    """Return (concatenated system text, block_count) or (None, 0)."""
    ex = rec.get("extract") or {}
    s = ex.get("system")
    if s is None:
        return None, 0
    if isinstance(s, str):  # codex-responses `instructions`
        return s, 1
    if isinstance(s, list):  # claude blocks, or codex-chat system messages
        parts = []
        for b in s:
            if isinstance(b, str):
                parts.append(b)
            elif isinstance(b, dict):
                if isinstance(b.get("text"), str):
                    parts.append(b["text"])
                elif "content" in b:
                    c = b["content"]
                    if isinstance(c, str):
                        parts.append(c)
                    elif isinstance(c, list):
                        for cb in c:
                            if isinstance(cb, dict) and isinstance(cb.get("text"), str):
                                parts.append(cb["text"])
        return "\n".join(parts), len(s)
    return None, 0


def tool_names(rec):
    ex = rec.get("extract") or {}
    tools = ex.get("tools")
    if not isinstance(tools, list):
        return None
    names = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        n = t.get("name")
        if not n and isinstance(t.get("function"), dict):
            n = t["function"].get("name")
        names.append(n or t.get("type") or "<unknown>")
    return names


def tool_schema_key(rec):
    ex = rec.get("extract") or {}
    for t in ex.get("tools") or []:
        if isinstance(t, dict):
            if "input_schema" in t:
                return "input_schema"
            if "parameters" in t:
                return "parameters"
            if isinstance(t.get("function"), dict) and "parameters" in t["function"]:
                return "function.parameters"
    return "n/a"


def message_summary(rec):
    ex = rec.get("extract") or {}
    msgs = ex.get("messages")
    if not isinstance(msgs, list):
        return None
    roles, ctypes = Counter(), Counter()
    for m in msgs:
        if not isinstance(m, dict):
            continue
        roles[m.get("role") or m.get("type") or "?"] += 1
        c = m.get("content")
        if isinstance(c, str):
            ctypes["text"] += 1
        elif isinstance(c, list):
            for cb in c:
                if isinstance(cb, dict):
                    ctypes[cb.get("type", "?")] += 1
    return {"count": len(msgs), "roles": roles, "ctypes": ctypes}


# --- skeleton vs slot detection ---------------------------------------------


def skeleton(distinct_texts):
    """Line-frequency split across DISTINCT system samples.

    A line present in every distinct sample is fixed skeleton; a line present in
    some-but-not-all is a dynamic slot.
    """
    k = len(distinct_texts)
    freq = Counter()
    for t in distinct_texts:
        for ln in set(t.split("\n")):
            freq[ln] += 1
    fixed = [ln for ln, c in freq.items() if c == k]
    dynamic = [ln for ln, c in freq.items() if 0 < c < k]
    return {"distinct": k, "unique_lines": len(freq), "fixed": len(fixed), "dynamic_lines": dynamic}


# --- reporting ---------------------------------------------------------------


def trunc(s, n=100):
    s = s.replace("\t", " ").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def analyze_agent(agent, recs, max_slots):
    print(f"\n## Agent: {agent}")
    wires = Counter(r["wire"] for r in recs)
    print(
        f"requests analyzed: {len(recs)}  | wire(s): "
        + ", ".join(f"{w}×{c}" for w, c in wires.items())
    )

    # system ---------------------------------------------------------------
    sys_samples, block_counts = [], []
    for r in recs:
        t, bc = system_text(r)
        if t is not None:
            sys_samples.append(t)
            block_counts.append(bc)
    facts = {"agent": agent}
    print("\n### system")
    if sys_samples:
        sizes = [len(t) for t in sys_samples]
        distinct = list(dict.fromkeys(sys_samples))  # preserve order, dedup
        sk = skeleton(distinct)
        bc_note = (
            f"{statistics.median(block_counts):.0f} block(s)/req"
            if max(block_counts) > 1
            else "single string"
        )
        print(f"- representation : {bc_note}")
        print(
            f"- size (chars)   : min={min(sizes)} median={statistics.median(sizes):.0f} max={max(sizes)}"
        )
        print(f"- distinct samples: {sk['distinct']} (of {len(sys_samples)} requests)")
        if sk["distinct"] < 2:
            print(
                "- skeleton/slots : only 1 distinct system seen — capture more "
                "varied sessions to surface dynamic slots"
            )
        else:
            pct = 100 * sk["fixed"] / sk["unique_lines"] if sk["unique_lines"] else 0
            print(f"- skeleton       : {sk['fixed']}/{sk['unique_lines']} lines fixed ({pct:.0f}%)")
            print(f"- dynamic slots  : {len(sk['dynamic_lines'])} line(s) vary across samples")
            shown = [trunc(ln) for ln in sk["dynamic_lines"] if ln.strip()][:max_slots]
            for ln in shown:
                print(f"    • {ln}")
            if len(sk["dynamic_lines"]) > len(shown):
                print(f"    … +{len(sk['dynamic_lines']) - len(shown)} more")
        facts.update(
            sys_repr=bc_note, sys_median=int(statistics.median(sizes)), sys_distinct=sk["distinct"]
        )
    else:
        print("- (no system field captured)")
        facts.update(sys_repr="n/a", sys_median=0, sys_distinct=0)

    # tools ----------------------------------------------------------------
    print("\n### tools")
    name_lists = [tn for r in recs if (tn := tool_names(r)) is not None]
    if name_lists:
        sets = [frozenset(nl) for nl in name_lists]
        union = set().union(*sets)
        always = set.intersection(*[set(s) for s in sets]) if sets else set()
        sometimes = union - always
        key = next((tool_schema_key(r) for r in recs if tool_schema_key(r) != "n/a"), "n/a")
        print(f"- count          : {statistics.median(len(nl) for nl in name_lists):.0f} (median)")
        print(f"- schema key     : {key}")
        print(f"- always present : {len(always)} | sometimes: {len(sometimes)}")
        print(f"- names          : {', '.join(sorted(union))}")
        if sometimes:
            print(f"- varying tools  : {', '.join(sorted(sometimes))}")
        facts.update(
            tool_count=int(statistics.median(len(nl) for nl in name_lists)),
            tool_key=key,
            tool_container="top-level `tools`",
        )
    else:
        print("- (no tools captured)")
        facts.update(tool_count=0, tool_key="n/a", tool_container="n/a")

    # messages -------------------------------------------------------------
    print("\n### messages")
    summaries = [ms for r in recs if (ms := message_summary(r)) is not None]
    if summaries:
        counts = [s["count"] for s in summaries]
        roles, ctypes = Counter(), Counter()
        for s in summaries:
            roles.update(s["roles"])
            ctypes.update(s["ctypes"])
        print(f"- per-request    : min={min(counts)} max={max(counts)} items")
        print(f"- roles/types    : {dict(roles)}")
        print(f"- content blocks : {dict(ctypes)}")
    else:
        print("- (no messages captured)")
    return facts


def cross_agent(all_facts):
    if len(all_facts) < 2:
        return
    print("\n## Cross-agent comparison")
    rows = [
        ("system representation", "sys_repr"),
        ("system size (median chars)", "sys_median"),
        ("distinct system samples", "sys_distinct"),
        ("tool count (median)", "tool_count"),
        ("tool schema key", "tool_key"),
        ("tool container", "tool_container"),
    ]
    agents = [f["agent"] for f in all_facts]
    w = max(28, *(len(r[0]) for r in rows)) + 1
    print(f"{'dimension':<{w}}" + "".join(f"{a:<22}" for a in agents))
    print("-" * (w + 22 * len(agents)))
    for label, key in rows:
        print(f"{label:<{w}}" + "".join(f"{str(f.get(key, '')):<22}" for f in all_facts))


def main():
    ap = argparse.ArgumentParser(description="Analyze Interlude JSONL captures.")
    ap.add_argument(
        "paths",
        nargs="*",
        default=[".interlude/log-*.jsonl"],
        help="JSONL files or globs (default: .interlude/log-*.jsonl)",
    )
    ap.add_argument("--agent", help="only analyze this agent label")
    ap.add_argument(
        "--max-slots", type=int, default=15, help="max dynamic-slot lines to print per agent"
    )
    args = ap.parse_args()

    files = sorted({f for p in args.paths for f in glob.glob(p)})
    if not files:
        print(f"No log files matched: {args.paths}", file=sys.stderr)
        sys.exit(1)

    recs = []
    for f in files:
        with open(f, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    recs.append(json.loads(line))
                except ValueError:
                    continue

    by_agent = defaultdict(list)
    for r in recs:
        if r.get("extract") is not None and (not args.agent or r.get("agent") == args.agent):
            by_agent[r.get("agent", "?")].append(r)

    print("# Interlude prompt-architecture report")
    print(f"sources: {len(files)} file(s), {len(recs)} record(s)")
    print(
        "agents : "
        + ", ".join(f"{a} ({len(v)} analyzable req)" for a, v in sorted(by_agent.items()))
    )

    facts = [analyze_agent(a, v, args.max_slots) for a, v in sorted(by_agent.items())]
    cross_agent(facts)


if __name__ == "__main__":
    main()
