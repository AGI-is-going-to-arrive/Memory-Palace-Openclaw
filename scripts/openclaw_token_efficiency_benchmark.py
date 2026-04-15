#!/usr/bin/env python3
"""Token efficiency benchmark: Memory Palace auto-recall vs native MEMORY.md.

Measures REAL data from your local environment:
- Native side: actual MEMORY.md + USER.md sizes (injected every turn)
- Palace side: actual auto-recall results for test prompts via MCP

Usage:
    python3 scripts/openclaw_token_efficiency_benchmark.py --json
    python3 scripts/openclaw_token_efficiency_benchmark.py --report .tmp/token_efficiency.md

Requires a running Memory Palace stdio backend (the smoke test must pass first).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

SCENARIOS = [
    {
        "id": "greeting",
        "prompt": "Hello, how are you today?",
        "description": "Casual greeting",
    },
    {
        "id": "preference",
        "prompt": "What are my coding preferences and workflow habits?",
        "description": "Preference recall",
    },
    {
        "id": "workflow",
        "prompt": "What was I working on in my last session?",
        "description": "Workflow continuity",
    },
    {
        "id": "technical",
        "prompt": "How is the embedding provider configured?",
        "description": "Technical recall",
    },
    {
        "id": "creative",
        "prompt": "Write a haiku about memory.",
        "description": "Creative (low recall)",
    },
]

CHARS_PER_TOKEN = 4.0


def measure_native_memory_size() -> dict:
    """Measure actual native OpenClaw memory file sizes."""
    home = Path.home()
    candidates = [
        home / ".openclaw" / "workspace" / "MEMORY.md",
        home / ".openclaw" / "workspace" / "USER.md",
    ]
    # Also check for daily memory files
    memory_dir = home / ".openclaw" / "memory"
    if memory_dir.is_dir():
        for f in memory_dir.iterdir():
            if f.suffix in (".md", ".txt") and f.is_file():
                candidates.append(f)

    total_chars = 0
    files = []
    for p in candidates:
        if p.is_file():
            size = len(p.read_text(errors="replace"))
            total_chars += size
            files.append({"path": str(p), "chars": size})

    return {
        "total_chars": total_chars,
        "total_tokens": int(total_chars / CHARS_PER_TOKEN),
        "files": files,
        "file_count": len(files),
    }


def measure_palace_recall(prompt: str, openclaw_bin: str = "openclaw") -> dict:
    """Call actual search_memory via openclaw CLI to measure real recall size."""
    try:
        result = subprocess.run(
            [
                openclaw_bin, "memory-palace", "search",
                "--query", prompt,
                "--max-results", "10",
                "--json",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            results = data.get("results", [])
            total_chars = sum(
                len(str(r.get("content", "") or r.get("snippet", "")))
                for r in results
            )
            return {
                "chars": total_chars,
                "tokens": int(total_chars / CHARS_PER_TOKEN),
                "hits": len(results),
                "method": "live_mcp",
            }
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError, KeyError):
        pass

    # Fallback: use verify's index_status to estimate
    return measure_palace_recall_via_db(prompt)


def measure_palace_recall_via_db(prompt: str) -> dict:
    """Fallback: estimate recall from the database directly."""
    try:
        import sqlite3
        db_path = Path.home() / ".openclaw" / "memory-palace" / "data" / "memory-palace.db"
        if not db_path.exists():
            return {"chars": 0, "tokens": 0, "hits": 0, "method": "no_db"}

        conn = sqlite3.connect(str(db_path))
        # Use FTS to search
        words = prompt.split()[:5]
        fts_query = " OR ".join(w for w in words if len(w) > 2)
        if not fts_query:
            conn.close()
            return {"chars": 0, "tokens": 0, "hits": 0, "method": "no_query"}

        rows = conn.execute(
            "SELECT chunk_text FROM memory_chunks_fts WHERE memory_chunks_fts MATCH ? LIMIT 10",
            (fts_query,),
        ).fetchall()
        conn.close()

        total_chars = sum(len(r[0]) for r in rows)
        return {
            "chars": total_chars,
            "tokens": int(total_chars / CHARS_PER_TOKEN),
            "hits": len(rows),
            "method": "fts_direct",
        }
    except Exception as e:
        return {"chars": 0, "tokens": 0, "hits": 0, "method": f"error:{e}"}


def run_benchmark(openclaw_bin: str = "openclaw") -> dict:
    native = measure_native_memory_size()
    scenarios = []

    for s in SCENARIOS:
        recall = measure_palace_recall(s["prompt"], openclaw_bin)

        if native["total_chars"] > 0:
            savings_pct = round((1 - recall["chars"] / native["total_chars"]) * 100, 1)
        else:
            savings_pct = None  # Can't calculate if native is empty

        scenarios.append({
            "id": s["id"],
            "description": s["description"],
            "prompt": s["prompt"],
            "native_chars": native["total_chars"],
            "native_tokens": native["total_tokens"],
            "palace_chars": recall["chars"],
            "palace_tokens": recall["tokens"],
            "palace_hits": recall["hits"],
            "savings_pct": savings_pct,
            "method": recall["method"],
        })

    total_native = sum(s["native_chars"] for s in scenarios)
    total_palace = sum(s["palace_chars"] for s in scenarios)
    avg_savings = round((1 - total_palace / total_native) * 100, 1) if total_native > 0 else None

    return {
        "benchmark": "token_efficiency",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "native": native,
        "scenarios": scenarios,
        "summary": {
            "native_total_chars": native["total_chars"],
            "native_total_tokens": native["total_tokens"],
            "avg_savings_pct": avg_savings,
            "scenario_count": len(scenarios),
            "note": (
                "Savings are meaningful only when native MEMORY.md has substantial content. "
                "A near-empty MEMORY.md means both approaches use minimal tokens."
            ) if (native["total_chars"] < 1000) else None,
        },
    }


def format_report(data: dict) -> str:
    native = data["native"]
    summary = data["summary"]

    lines = [
        "# Token Efficiency: Memory Palace vs Native MEMORY.md",
        "",
        "## Native OpenClaw memory files",
        "",
    ]
    if native["files"]:
        for f in native["files"]:
            lines.append(f"- `{f['path']}`: {f['chars']:,} chars")
        lines.append(f"- **Total: {native['total_chars']:,} chars (~{native['total_tokens']:,} tokens injected every turn)**")
    else:
        lines.append("- No native memory files found.")

    if native["total_chars"] < 1000:
        lines.extend([
            "",
            "> **Note:** Your native MEMORY.md is very small ({} chars). ".format(native["total_chars"]),
            "> Token savings comparison is only meaningful with a larger MEMORY.md.",
            "> As your memory grows, the difference becomes significant because native",
            "> injects the FULL file every turn, while Palace retrieves only relevant entries.",
        ])

    lines.extend([
        "",
        "## Per-scenario recall measurement",
        "",
        "| Scenario | Native (tokens) | Palace (tokens) | Hits | Method | Savings |",
        "|----------|----------------:|----------------:|-----:|--------|--------:|",
    ])
    for s in data["scenarios"]:
        sav = f"{s['savings_pct']}%" if s["savings_pct"] is not None else "N/A"
        lines.append(
            f"| {s['description']:<25} | {s['native_tokens']:>6,} | {s['palace_tokens']:>6,} "
            f"| {s['palace_hits']:>3} | {s['method']:<10} | {sav:>6} |"
        )

    if summary["avg_savings_pct"] is not None:
        lines.append(f"\n**Average savings: {summary['avg_savings_pct']}%**")

    if summary.get("note"):
        lines.append(f"\n*{summary['note']}*")

    lines.extend([
        "",
        "## How it works",
        "",
        "- **Native MEMORY.md**: the entire file is loaded into the system prompt every turn,",
        "  regardless of whether the conversation needs any of that context.",
        "- **Memory Palace auto-recall**: only memories matching the current prompt are retrieved",
        "  (typically 0-5 entries), so most turns inject far less context.",
        "",
        "## Reproduce",
        "",
        "```bash",
        "python3 scripts/openclaw_token_efficiency_benchmark.py --json",
        "```",
    ])
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Token efficiency benchmark (real measurements)")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--report", type=str, help="Write markdown report to path")
    parser.add_argument("--openclaw-bin", default="openclaw")
    args = parser.parse_args()

    data = run_benchmark(args.openclaw_bin)

    if args.json:
        print(json.dumps(data, indent=2))
    elif args.report:
        p = Path(args.report)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(format_report(data))
        print(f"Report written to {p}")
    else:
        print(format_report(data))


if __name__ == "__main__":
    main()
