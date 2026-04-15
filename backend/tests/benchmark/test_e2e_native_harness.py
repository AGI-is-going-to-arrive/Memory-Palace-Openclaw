"""Filesystem Keyword Baseline Harness  (fs_keyword_baseline)

Historical reference baseline — kept for backwards compatibility.
Simulates a file-based memory system (MEMORY.md index + memory/*.md files)
using the same scenarios as the black-box MCP benchmark, with identical scoring.

IMPORTANT: This is NOT the actual OpenClaw native memory implementation.
It is a "filesystem keyword baseline" that models flat-file memory with
keyword grep search.  It does NOT replicate OpenClaw memory-core's
FTS5/BM25 search engine, embedding support, or temporal-decay scoring.

For a closer approximation of memory-core search semantics, see
test_e2e_native_memory_core.py (NativeMemoryCoreReplica).

Model:
  - MEMORY.md: index file with `- [Title](memory/file.md) — description`
  - memory/*.md: individual memory files
  - Search: keyword grep + CJK bigram tokenization, ranked by match count + mtime
  - No FTS5, no BM25, no semantic search, no write guard, no compact context

N/A categories (no filesystem equivalent):
  - SC07 (conflict_guard): no write guard
  - SC09 (compact_recall): no compact/gist

Spec: backend/tests/benchmark/E2E_BLACKBOX_SPEC.md §5
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from helpers.e2e_eval import (
    ScenarioResult,
    aggregate_results,
    eval_delete as _eval_delete_shared,
    eval_read as _eval_read_shared,
    eval_search as _eval_search_shared,
)

_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
_BENCH = Path(__file__).resolve().parent
_SCENARIOS_FILE = "e2e_blackbox_scenarios.jsonl"

# Categories with no native equivalent — reported as MP advantage
_NA_CATEGORIES = {"conflict_guard", "compact_recall"}


# ScenarioResult imported from helpers.e2e_eval


# ---------------------------------------------------------------------------
# Native Memory Simulation
# ---------------------------------------------------------------------------


class NativeMemory:
    """Simulates OpenClaw native memory: MEMORY.md + memory/*.md files."""

    def __init__(self, root: Path):
        self.root = root
        self.memory_dir = root / "memory"
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        self.index_path = root / "MEMORY.md"
        self.index_path.write_text("", encoding="utf-8")

    def create(self, domain: str, title: str, content: str, **_kwargs: Any) -> str:
        """Write memory file + index entry. Returns virtual URI."""
        safe_title = re.sub(r'[^\w\-.]', '_', title)
        filename = f"{domain}__{safe_title}.md"
        filepath = self.memory_dir / filename
        filepath.write_text(content, encoding="utf-8")

        uri = f"{domain}://{title}"
        description = content[:80].replace("\n", " ")
        index_line = f"- [{title}]({filepath.relative_to(self.root)}) — {description}\n"

        with open(self.index_path, "a", encoding="utf-8") as f:
            f.write(index_line)

        return uri

    def read(self, domain: str, title: str) -> str:
        """Read memory file by domain + title."""
        safe_title = re.sub(r'[^\w\-.]', '_', title)
        filename = f"{domain}__{safe_title}.md"
        filepath = self.memory_dir / filename
        if filepath.exists():
            return filepath.read_text(encoding="utf-8")
        return ""

    def update(self, domain: str, title: str, old_string: str, new_string: str) -> bool:
        """Edit memory file content (find & replace)."""
        safe_title = re.sub(r'[^\w\-.]', '_', title)
        filename = f"{domain}__{safe_title}.md"
        filepath = self.memory_dir / filename
        if not filepath.exists():
            return False
        content = filepath.read_text(encoding="utf-8")
        if old_string not in content:
            return False
        filepath.write_text(content.replace(old_string, new_string), encoding="utf-8")
        # Also update MEMORY.md index description
        self._update_index_description(title, new_string[:80])
        return True

    def delete(self, domain: str, title: str) -> bool:
        """Delete memory file + index entry."""
        safe_title = re.sub(r'[^\w\-.]', '_', title)
        filename = f"{domain}__{safe_title}.md"
        filepath = self.memory_dir / filename
        if filepath.exists():
            filepath.unlink()
        self._remove_index_entry(title)
        return True

    def _update_index_description(self, title: str, new_desc: str) -> None:
        """Update the description portion of an index line."""
        lines = self.index_path.read_text(encoding="utf-8").splitlines()
        updated = []
        for line in lines:
            if f"[{title}]" in line:
                match = re.search(r'(- \[.*?\]\(.*?\)) — .*', line)
                if match:
                    line = f"{match.group(1)} — {new_desc}"
            updated.append(line)
        self.index_path.write_text("\n".join(updated) + "\n", encoding="utf-8")

    def _remove_index_entry(self, title: str) -> None:
        """Remove index line containing the given title."""
        lines = self.index_path.read_text(encoding="utf-8").splitlines()
        filtered = [l for l in lines if f"[{title}]" not in l]
        self.index_path.write_text("\n".join(filtered) + "\n", encoding="utf-8")

    def add_alias(self, title: str, alias_title: str) -> None:
        """Add a second index entry pointing to the same file."""
        # Find the original file path from index
        lines = self.index_path.read_text(encoding="utf-8").splitlines()
        for line in lines:
            if f"[{title}]" in line:
                # Extract file path from markdown link
                match = re.search(r'\[.*?\]\((.*?)\)', line)
                if match:
                    filepath = match.group(1)
                    index_line = f"- [{alias_title}]({filepath}) — alias of {title}\n"
                    with open(self.index_path, "a", encoding="utf-8") as f:
                        f.write(index_line)
                break

    def search(self, query: str, max_results: int = 10) -> List[Dict]:
        """Keyword search across MEMORY.md + memory/*.md files.

        Ranking: match_count (primary) + file mtime recency (secondary).
        This simulates what a real agent would do with grep + glob.
        """
        query_lower = query.lower()
        # Tokenize: split on whitespace/punctuation for Latin,
        # plus CJK character bigrams (simulates what grep -i would catch).
        # This models a smart agent extracting key terms before grepping.
        raw_tokens = [w for w in re.split(r'[\s,?!.\uff0c\uff1f\uff01\u3002\u3001\uff1a\uff1b\u201c\u201d\u2018\u2019\uff08\uff09\[\]]+', query_lower) if w]
        keywords = []
        for tok in raw_tokens:
            # Check if token contains CJK characters
            cjk_chars = [c for c in tok if "\u4e00" <= c <= "\u9fff"]
            if len(cjk_chars) >= 2:
                # Add CJK bigrams as keywords (simulate partial matching)
                for i in range(len(cjk_chars) - 1):
                    keywords.append(cjk_chars[i] + cjk_chars[i + 1])
                # Also add individual CJK characters as weaker signals
                keywords.extend(cjk_chars)
            elif len(tok) >= 2:
                keywords.append(tok)
        # Deduplicate while preserving order
        seen = set()
        keywords = [k for k in keywords if k not in seen and not seen.add(k)]

        candidates: List[Tuple[float, str, str, str]] = []  # (score, uri, title, content)

        # Search index first
        index_lines = self.index_path.read_text(encoding="utf-8").splitlines()
        index_titles: Dict[str, str] = {}
        for line in index_lines:
            match = re.search(r'\[(.*?)\]\((.*?)\)', line)
            if match:
                index_titles[match.group(1)] = match.group(2)

        # Search each memory file
        for filepath in sorted(self.memory_dir.glob("*.md")):
            content = filepath.read_text(encoding="utf-8")
            filename = filepath.name
            # Extract domain and title from filename
            parts = filename.rsplit(".", 1)[0].split("__", 1)
            domain = parts[0] if len(parts) == 2 else "unknown"
            title_raw = parts[1] if len(parts) == 2 else parts[0]
            title = title_raw.replace("_", " ")  # approximate reverse

            # Find the real title from index
            real_title = title_raw
            for idx_title, idx_path in index_titles.items():
                if filename in idx_path:
                    real_title = idx_title
                    break

            uri = f"{domain}://{real_title}"

            # Score: keyword match count in content + title + index description
            search_text = (content + " " + real_title).lower()
            match_count = sum(1 for kw in keywords if kw in search_text)

            if match_count == 0:
                # Try substring match on full query
                if query_lower in search_text:
                    match_count = 1

            if match_count > 0:
                # Add recency bonus (0-0.5 based on mtime)
                mtime = filepath.stat().st_mtime
                recency = min(0.5, (time.time() - mtime) / 3600 * -0.01 + 0.5)
                score = match_count + max(0, recency)
                candidates.append((score, uri, real_title, content))

        # Sort by score descending
        candidates.sort(key=lambda x: -x[0])

        results = []
        seen_uris = set()
        for score, uri, title, content in candidates[:max_results]:
            if uri in seen_uris:
                continue
            seen_uris.add(uri)
            results.append({
                "uri": uri,
                "title": title,
                "content": content,
                "score": score,
                "snippet": content[:200],
            })

        return results


# ---------------------------------------------------------------------------
# Scenario execution
# ---------------------------------------------------------------------------


def _load_scenarios() -> List[Dict]:
    path = _FIXTURES / _SCENARIOS_FILE
    assert path.exists(), f"Scenarios not found: {path}"
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def _parse_uri(uri: str) -> Tuple[str, str]:
    """Parse 'domain://title' into (domain, title)."""
    parts = uri.split("://", 1)
    return (parts[0], parts[1]) if len(parts) == 2 else ("unknown", uri)


def _execute_native_tool(mem: NativeMemory, tool: str, args: Dict) -> Any:
    """Map MCP tool calls to native memory operations."""
    if tool == "create_memory":
        parent_uri = args.get("parent_uri", "")
        domain = parent_uri.split("://")[0] if "://" in parent_uri else "unknown"
        title = args.get("title", "untitled")
        content = args.get("content", "")
        uri = mem.create(domain, title, content)
        return {"created": True, "uri": uri}

    elif tool == "search_memory":
        query = args.get("query", "")
        results = mem.search(query)
        return {"ok": True, "results": results}

    elif tool == "read_memory":
        uri = args.get("uri", "")
        domain, title = _parse_uri(uri)
        content = mem.read(domain, title)
        return content if content else {"error": "not found"}

    elif tool == "update_memory":
        uri = args.get("uri", "")
        domain, title = _parse_uri(uri)
        old_s = args.get("old_string", "")
        new_s = args.get("new_string", "")
        if args.get("content"):
            # Full content replace
            safe_title = re.sub(r'[^\w\-.]', '_', title)
            filepath = mem.memory_dir / f"{domain}__{safe_title}.md"
            if filepath.exists():
                filepath.write_text(args["content"], encoding="utf-8")
                return {"ok": True}
        ok = mem.update(domain, title, old_s, new_s)
        return {"ok": ok}

    elif tool == "delete_memory":
        uri = args.get("uri", "")
        domain, title = _parse_uri(uri)
        mem.delete(domain, title)
        return {"ok": True}

    elif tool == "add_alias":
        source_uri = args.get("source_uri", "")
        alias_uri = args.get("alias_uri", "")
        _, source_title = _parse_uri(source_uri)
        _, alias_title = _parse_uri(alias_uri)
        mem.add_alias(source_title, alias_title)
        return {"ok": True}

    elif tool == "compact_context":
        return {"ok": True, "native_na": True}

    return {"error": f"Unknown tool: {tool}"}


def _run_setup(mem: NativeMemory, steps: List[Dict]) -> None:
    for step in steps:
        _execute_native_tool(mem, step["tool"], step["args"])


def _evaluate_scenario(mem: NativeMemory, scenario: Dict) -> ScenarioResult:
    sr = ScenarioResult(
        scenario_id=scenario["scenario_id"],
        category=scenario["category"],
        profile="native",
    )

    # Skip non-comparable categories
    if scenario["category"] in _NA_CATEGORIES:
        sr.comparable = False
        sr.details["reason"] = f"No native equivalent for {scenario['category']}"
        return sr

    try:
        _run_setup(mem, scenario.get("setup", []))
        expected = scenario.get("expected", {})

        if "action_sequence" in scenario:
            actions = scenario["action_sequence"]
            result = None
            t0 = time.perf_counter()
            for act in actions:
                result = _execute_native_tool(mem, act["tool"], act["args"])
            sr.latency_ms = (time.perf_counter() - t0) * 1000
        else:
            action = scenario["action"]
            t0 = time.perf_counter()
            result = _execute_native_tool(mem, action["tool"], action["args"])
            sr.latency_ms = (time.perf_counter() - t0) * 1000

        if scenario["category"] == "delete_verify":
            _eval_delete_shared(sr, result, expected)
        elif scenario["category"] == "namespace_read":
            _eval_read_shared(sr, result, expected)
        else:
            _eval_search_shared(sr, result, expected)

    except Exception as exc:
        sr.error = str(exc)

    return sr


# eval_search, eval_delete, eval_read, aggregate_results
# are all imported from helpers.e2e_eval (see top of file)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_e2e_native_baseline(tmp_path):
    """Run all 18 scenarios against filesystem keyword baseline (fs_keyword_baseline).

    This is a historical reference baseline using flat-file grep search.
    For a memory-core-approximating baseline, see test_e2e_native_memory_core.py.
    """
    scenarios = _load_scenarios()
    mem = NativeMemory(tmp_path)
    results: List[ScenarioResult] = []

    for sc in scenarios:
        sr = _evaluate_scenario(mem, sc)
        results.append(sr)

    agg = aggregate_results(results, na_categories=_NA_CATEGORIES)
    _write_report(results, agg, tmp_path)

    # Assertions
    assert agg["n_comparable"] > 0
    assert agg["n_na"] == 2, f"Expected 2 N/A scenarios, got {agg['n_na']}"


def test_e2e_native_vs_mp_comparison(tmp_path):
    """Generate comparison report: fs_keyword_baseline vs MP black-box.

    Historical comparison — kept for backwards compatibility.
    For memory-core replica comparison, see test_e2e_native_memory_core.py.
    """
    # Run native
    scenarios = _load_scenarios()
    mem = NativeMemory(tmp_path)
    native_results: List[ScenarioResult] = []

    for sc in scenarios:
        sr = _evaluate_scenario(mem, sc)
        native_results.append(sr)

    native_agg = aggregate_results(native_results)

    # Load MP black-box report
    mp_report_path = _BENCH / "e2e_blackbox_report.json"
    if not mp_report_path.exists():
        pytest.skip("MP black-box report not found, run test_e2e_blackbox_harness.py first")

    with open(mp_report_path) as f:
        mp_report = json.load(f)

    comparison = _build_comparison(native_results, native_agg, mp_report)
    _write_comparison_report(comparison, tmp_path)


def _write_report(
    results: List[ScenarioResult], agg: Dict, tmp_path: Path,
) -> None:
    report = {
        "benchmark": "e2e_filesystem_keyword_baseline",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "profile": "native",
        "overall": agg,
        "per_scenario": [
            {
                "scenario_id": r.scenario_id,
                "category": r.category,
                "comparable": r.comparable,
                "hit": r.hit,
                "rank": r.rank,
                "mrr": round(r.mrr, 4),
                "content_match": r.content_match,
                "latency_ms": round(r.latency_ms, 1),
                "error": r.error,
                "details": r.details,
            }
            for r in results
        ],
    }

    for path in (_BENCH / "e2e_native_report.json", tmp_path / "e2e_native_report.json"):
        with open(path, "w") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)


def _build_comparison(
    native_results: List[ScenarioResult],
    native_agg: Dict,
    mp_report: Dict,
) -> Dict:
    """Build per-scenario and aggregate comparison."""
    native_by_id = {r.scenario_id: r for r in native_results}

    per_scenario = []
    for profile_key in ("B", "C", "D"):
        mp_profile = mp_report.get("profiles", {}).get(profile_key)
        if not mp_profile:
            continue
        for mp_sc in mp_profile["per_scenario"]:
            sid = mp_sc["scenario_id"]
            nr = native_by_id.get(sid)
            if not nr:
                continue
            per_scenario.append({
                "scenario_id": sid,
                "category": mp_sc["category"],
                "comparable": nr.comparable,
                "mp_profile": profile_key,
                "mp_hit": mp_sc["hit"],
                "mp_mrr": mp_sc["mrr"],
                "mp_latency_ms": mp_sc["latency_ms"],
                "native_hit": nr.hit if nr.comparable else None,
                "native_mrr": round(nr.mrr, 4) if nr.comparable else None,
                "native_latency_ms": round(nr.latency_ms, 1) if nr.comparable else None,
                "winner": _determine_winner(mp_sc, nr, profile_key),
            })

    # Aggregate comparison
    agg_comparison = {}
    for profile_key in ("B", "C", "D"):
        mp_profile = mp_report.get("profiles", {}).get(profile_key)
        if not mp_profile:
            continue
        mp_overall = mp_profile["overall"]
        agg_comparison[profile_key] = {
            "mp_hr": mp_overall["hr"],
            "mp_mrr": mp_overall["mrr"],
            "native_hr": native_agg["hr"],
            "native_mrr": native_agg["mrr"],
            "mp_advantage_categories": list(_NA_CATEGORIES),
        }

    return {
        "benchmark": "e2e_mp_vs_filesystem_keyword_comparison",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "native_overall": native_agg,
        "comparison_aggregate": agg_comparison,
        "per_scenario": per_scenario,
        "conclusion_boundary": (
            "This comparison measures Memory Palace MCP e2e vs a filesystem "
            "keyword baseline (MEMORY.md + memory/*.md with grep search). "
            "This is NOT the actual OpenClaw native memory implementation. "
            "MP advantages in compact_context and write_guard have no "
            "filesystem equivalent and are excluded from comparable metrics. "
            "Results do not support claims of general superiority — they "
            "measure specific retrieval scenarios on a small corpus only. "
            "At 18 scenarios with direct keyword overlap, grep is expected "
            "to perform well; larger corpus with interference and paraphrase "
            "queries are needed for meaningful differentiation."
        ),
    }


def _determine_winner(mp_sc: Dict, nr: ScenarioResult, profile: str) -> str:
    if not nr.comparable:
        return "mp_advantage_no_native_equivalent"
    mp_hit = mp_sc.get("hit", False)
    native_hit = nr.hit
    if mp_hit and not native_hit:
        return f"mp_{profile}"
    elif native_hit and not mp_hit:
        return "native"
    elif mp_hit and native_hit:
        mp_mrr = mp_sc.get("mrr", 0)
        native_mrr = nr.mrr
        if abs(mp_mrr - native_mrr) < 0.01:
            return "tie"
        return f"mp_{profile}" if mp_mrr > native_mrr else "native"
    else:
        return "both_miss"


def _write_comparison_report(comparison: Dict, tmp_path: Path) -> None:
    for path in (_BENCH / "e2e_comparison_report.json", tmp_path / "e2e_comparison_report.json"):
        with open(path, "w") as f:
            json.dump(comparison, f, indent=2, ensure_ascii=False)
