"""NativeMemoryCoreReplica — FTS5/BM25 + temporal-decay keyword-only replica
approximating OpenClaw memory-core search semantics for benchmark use.

This is a *benchmark replica*, NOT a faithful copy of the full OpenClaw
memory-core engine.  It replicates the keyword-search path only:

  - SQLite FTS5 index with default tokenizer
  - BM25 rank → score conversion: 1.0 / (1.0 + max(-rank, 0))
  - ~400-char chunks with 80-char overlap (approximating memory-core defaults)
  - Temporal decay: score * 2^(-age_days / half_life_days)
  - No vector/embedding search (matches default memory-core without provider)
  - No MMR re-ranking (matches memory-core default mmr.enabled=false)

Differences from real memory-core that are explicitly acknowledged:
  - FTS5 tokenizer may differ (memory-core uses Node.js sqlite, we use Python)
  - buildFtsQuery() preprocessing logic not replicated (minified JS, not inspected)
  - Temporal decay half_life_days default value approximated (30 days)
  - Chunk sizing measured in chars, not tokens (approximation)

Spec: backend/tests/benchmark/E2E_BLACKBOX_SPEC.md §5.2
"""

from __future__ import annotations

import math
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple


# ---------------------------------------------------------------------------
# Constants (approximating memory-core defaults)
# ---------------------------------------------------------------------------

_CHUNK_SIZE_CHARS = 400
_CHUNK_OVERLAP_CHARS = 80
_TEMPORAL_DECAY_HALF_LIFE_DAYS = 30.0
_LN2 = math.log(2)


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------


def _chunk_text(text: str) -> List[Tuple[int, int, str]]:
    """Split text into overlapping chunks.  Returns (start, end, chunk_text)."""
    if len(text) <= _CHUNK_SIZE_CHARS:
        return [(0, len(text), text)]

    chunks = []
    start = 0
    while start < len(text):
        end = min(start + _CHUNK_SIZE_CHARS, len(text))
        chunks.append((start, end, text[start:end]))
        step = _CHUNK_SIZE_CHARS - _CHUNK_OVERLAP_CHARS
        if step <= 0:
            step = _CHUNK_SIZE_CHARS
        start += step
        if start >= len(text):
            break
    return chunks


# ---------------------------------------------------------------------------
# FTS5 query builder
# ---------------------------------------------------------------------------


def _cjk_space_separate(text: str) -> str:
    """Insert spaces around CJK characters so FTS5 unicode61 tokenizes them
    as individual tokens.  Latin/digit runs are kept together."""
    out: list[str] = []
    for ch in text:
        if "\u4e00" <= ch <= "\u9fff":
            out.append(f" {ch} ")
        else:
            out.append(ch)
    return re.sub(r" {2,}", " ", "".join(out)).strip()


def _build_fts_query(raw: str) -> str:
    """Build an FTS5 MATCH query from a natural-language search string.

    Strategy:
    - Latin tokens → quoted phrases (e.g. ``"OAuth2"``)
    - CJK runs → space-separated chars joined as phrase (e.g. ``"花 生"``)
    - All terms joined with OR

    This is an approximation — memory-core's ``buildFtsQuery`` is in minified
    JS and has not been fully inspected.
    """
    tokens: list[str] = []
    # Split on whitespace / common punctuation
    parts = re.split(
        r'[\s,?!.\uff0c\uff1f\uff01\u3002\u3001\uff1a\uff1b'
        r'\u201c\u201d\u2018\u2019\uff08\uff09\[\]()]+',
        raw,
    )
    for part in parts:
        if not part:
            continue
        for segment in re.findall(r'[\u4e00-\u9fff]+|[a-zA-Z0-9]+', part):
            if not segment:
                continue
            if "\u4e00" <= segment[0] <= "\u9fff":
                # CJK: emit individual chars + adjacent bigram phrases
                # Each char matches independently; bigrams boost adjacent pairs
                for ch in segment:
                    tokens.append(ch)
                for i in range(len(segment) - 1):
                    tokens.append(f'"{segment[i]} {segment[i + 1]}"')
            else:
                tokens.append(f'"{segment}"')

    # Deduplicate while preserving order
    seen: set[str] = set()
    unique = []
    for t in tokens:
        if t not in seen:
            seen.add(t)
            unique.append(t)

    if not unique:
        return ""
    return " OR ".join(unique)


# ---------------------------------------------------------------------------
# NativeMemoryCoreReplica
# ---------------------------------------------------------------------------


class NativeMemoryCoreReplica:
    """Deterministic memory-core keyword-only search replica for benchmarking.

    All data lives in ``workspace_dir/memory/*.md`` + ``workspace_dir/MEMORY.md``.
    An in-memory SQLite database provides FTS5 indexing.
    """

    def __init__(self, workspace_dir: Path):
        self.workspace_dir = workspace_dir
        self.memory_dir = workspace_dir / "memory"
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        self.index_path = workspace_dir / "MEMORY.md"
        self.index_path.write_text("# MEMORY.md\n", encoding="utf-8")

        # Internal SQLite for FTS5
        self._db = sqlite3.connect(":memory:")
        self._db.execute(
            "CREATE TABLE files ("
            "  path TEXT PRIMARY KEY,"
            "  domain TEXT NOT NULL,"
            "  title TEXT NOT NULL,"
            "  uri TEXT NOT NULL,"
            "  mtime REAL NOT NULL"
            ")"
        )
        self._db.execute(
            "CREATE VIRTUAL TABLE chunks_fts USING fts5("
            "  text, uri UNINDEXED, path UNINDEXED,"
            "  char_start UNINDEXED, char_end UNINDEXED"
            ")"
        )
        self._db.commit()

    # ---- CRUD ----

    def create(
        self, domain: str, title: str, content: str, **_kw: Any,
    ) -> str:
        """Write a memory file, index it, return virtual URI."""
        safe_title = re.sub(r'[^\w\-.]', '_', title)
        filename = f"{domain}__{safe_title}.md"
        filepath = self.memory_dir / filename
        filepath.write_text(content, encoding="utf-8")

        uri = f"{domain}://{title}"
        now = time.time()

        # Files table
        self._db.execute(
            "INSERT OR REPLACE INTO files VALUES (?, ?, ?, ?, ?)",
            (filename, domain, title, uri, now),
        )

        # Chunk and index
        self._index_file(filename, uri, content)
        self._db.commit()

        # MEMORY.md index line
        desc = content[:80].replace("\n", " ")
        with open(self.index_path, "a", encoding="utf-8") as f:
            f.write(f"- [{title}](memory/{filename}) — {desc}\n")

        return uri

    def read(self, domain: str, title: str) -> str:
        safe_title = re.sub(r'[^\w\-.]', '_', title)
        fp = self.memory_dir / f"{domain}__{safe_title}.md"
        return fp.read_text(encoding="utf-8") if fp.exists() else ""

    def update(
        self, domain: str, title: str,
        old_string: str = "", new_string: str = "",
        content: str | None = None,
    ) -> bool:
        safe_title = re.sub(r'[^\w\-.]', '_', title)
        filename = f"{domain}__{safe_title}.md"
        fp = self.memory_dir / filename
        if not fp.exists():
            return False

        if content is not None:
            fp.write_text(content, encoding="utf-8")
        else:
            text = fp.read_text(encoding="utf-8")
            if old_string not in text:
                return False
            fp.write_text(text.replace(old_string, new_string), encoding="utf-8")

        uri = f"{domain}://{title}"
        new_content = fp.read_text(encoding="utf-8")
        # Re-index
        self._db.execute("DELETE FROM chunks_fts WHERE path = ?", (filename,))
        self._index_file(filename, uri, new_content)
        self._db.execute(
            "UPDATE files SET mtime = ? WHERE path = ?", (time.time(), filename),
        )
        self._db.commit()
        return True

    def delete(self, domain: str, title: str) -> bool:
        safe_title = re.sub(r'[^\w\-.]', '_', title)
        filename = f"{domain}__{safe_title}.md"
        fp = self.memory_dir / filename
        if fp.exists():
            fp.unlink()
        self._db.execute("DELETE FROM chunks_fts WHERE path = ?", (filename,))
        self._db.execute("DELETE FROM files WHERE path = ?", (filename,))
        self._db.commit()
        self._remove_index_entry(title)
        return True

    def add_alias(self, source_title: str, alias_title: str) -> None:
        lines = self.index_path.read_text(encoding="utf-8").splitlines()
        for line in lines:
            if f"[{source_title}]" in line:
                m = re.search(r'\[.*?\]\((.*?)\)', line)
                if m:
                    with open(self.index_path, "a", encoding="utf-8") as f:
                        f.write(f"- [{alias_title}]({m.group(1)}) — alias of {source_title}\n")
                break

    # ---- Search ----

    def search(self, query: str, max_results: int = 10) -> List[Dict]:
        """FTS5 BM25 keyword search with temporal decay."""
        fts_query = _build_fts_query(query)
        if not fts_query:
            # Pure punctuation / whitespace queries produce no tokens → empty results.
            # This is acceptable for benchmark; real memory-core likely behaves similarly.
            return []

        now = time.time()

        # FTS5 search — rank is negative BM25 (lower = better match)
        try:
            rows = self._db.execute(
                "SELECT uri, path, text, char_start, char_end, rank "
                "FROM chunks_fts WHERE chunks_fts MATCH ? "
                "ORDER BY rank LIMIT ?",
                (fts_query, max_results * 3),
            ).fetchall()
        except sqlite3.OperationalError:
            # fts5 query syntax error fallback: try LIKE
            rows = []

        if not rows:
            # Fallback: simple LIKE on each known token
            rows = self._fallback_like_search(query, max_results * 3)

        # Score: BM25 rank → score, then temporal decay, then collapse by URI
        scored: Dict[str, Dict] = {}
        for uri, path, text, _cs, _ce, rank in rows:
            # FTS5 rank: negative BM25 value (more negative = stronger match).
            # Convert to positive signal: -rank = abs(BM25).
            # Normalize to [0,1] via logistic: abs_bm25 / (1 + abs_bm25).
            #
            # NOTE: The MP production backend (sqlite_client.py) uses a different
            # formula that clamps all FTS5 hits to score=1.0.  This replica
            # targets OpenClaw memory-core's bm25RankToScore(), whose exact
            # implementation is in minified JS and has not been fully confirmed.
            # A discriminative formula is used here as a more sensible default
            # for benchmark ranking.  This is an acknowledged approximation.
            abs_bm25 = -rank if rank is not None and rank < 0 else 0.001
            bm25_score = abs_bm25 / (1.0 + abs_bm25)

            # Temporal decay from file mtime
            file_row = self._db.execute(
                "SELECT mtime FROM files WHERE path = ?", (path,),
            ).fetchone()
            if file_row:
                age_days = (now - file_row[0]) / 86400.0
                decay = 2.0 ** (-age_days / _TEMPORAL_DECAY_HALF_LIFE_DAYS)
                bm25_score *= decay

            if uri not in scored or bm25_score > scored[uri]["score"]:
                # Read full content for the result
                fp = self.memory_dir / path
                full_content = fp.read_text(encoding="utf-8") if fp.exists() else text
                scored[uri] = {
                    "uri": uri,
                    "title": self._title_from_uri(uri),
                    "content": full_content,
                    "score": bm25_score,
                    "snippet": text[:200],
                }

        results = sorted(scored.values(), key=lambda x: -x["score"])
        return results[:max_results]

    # ---- Internal helpers ----

    def _index_file(self, filename: str, uri: str, content: str) -> None:
        chunks = _chunk_text(content)
        for start, end, chunk_text in chunks:
            # Space-separate CJK chars so FTS5 tokenizes them as individual
            # character tokens.  The query builder (_build_fts_query) emits
            # individual chars + adjacent bigram phrases via OR, which matches
            # these single-char tokens.  This index/query asymmetry is intentional.
            fts_text = _cjk_space_separate(chunk_text)
            self._db.execute(
                "INSERT INTO chunks_fts (text, uri, path, char_start, char_end) "
                "VALUES (?, ?, ?, ?, ?)",
                (fts_text, uri, filename, start, end),
            )

    def _fallback_like_search(
        self, query: str, limit: int,
    ) -> List[Tuple[str, str, str, int, int, float]]:
        """LIKE fallback when FTS5 MATCH fails."""
        # Extract CJK runs and latin tokens as keywords
        keywords = re.findall(r'[\u4e00-\u9fff]+|[a-zA-Z0-9]{2,}', query)
        if not keywords:
            keywords = [query.strip()]

        # For CJK keywords, also search the space-separated form
        search_terms = []
        for kw in keywords[:8]:
            search_terms.append(kw)
            if any("\u4e00" <= c <= "\u9fff" for c in kw):
                search_terms.append(" ".join(kw))  # space-separated form

        results = []
        seen = set()
        for term in search_terms:
            rows = self._db.execute(
                "SELECT uri, path, text, char_start, char_end FROM chunks_fts "
                "WHERE text LIKE ? LIMIT ?",
                (f"%{term}%", limit),
            ).fetchall()
            for uri, path, text, cs, ce in rows:
                key = (uri, path, cs)
                if key not in seen:
                    seen.add(key)
                    results.append((uri, path, text, cs, ce, -0.5))

        return results[:limit]

    def _remove_index_entry(self, title: str) -> None:
        lines = self.index_path.read_text(encoding="utf-8").splitlines()
        filtered = [l for l in lines if f"[{title}]" not in l]
        self.index_path.write_text("\n".join(filtered) + "\n", encoding="utf-8")

    @staticmethod
    def _title_from_uri(uri: str) -> str:
        parts = uri.split("://", 1)
        return parts[1] if len(parts) == 2 else uri

    def close(self) -> None:
        self._db.close()
