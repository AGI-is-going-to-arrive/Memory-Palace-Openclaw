import re
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple


def parse_range_spec(range_value: Optional[str]) -> Optional[Tuple[int, int]]:
    """Parse `start:end` or `start-end` range spec."""
    if range_value is None:
        return None
    text = str(range_value).strip()
    if not text:
        return None
    match = re.match(r"^(\d+)\s*[:,-]\s*(\d+)$", text)
    if not match:
        raise ValueError(
            "Invalid range format. Use `start:end` (e.g., `0:500`) or `start-end`."
        )
    start = int(match.group(1))
    end = int(match.group(2))
    if end <= start:
        raise ValueError("Invalid range: end must be greater than start.")
    return start, end


def slice_text_content(
    content: str,
    chunk_id: Optional[int],
    range_spec: Optional[Tuple[int, int]],
    max_chars: Optional[int],
    *,
    read_chunk_size: int,
    read_chunk_overlap: int,
) -> Tuple[str, Dict[str, Any]]:
    """Slice content by chunk/range/max_chars."""
    total_chars = len(content)
    start = 0
    end = total_chars
    mode = "full"

    if chunk_id is not None:
        stride = max(1, read_chunk_size - read_chunk_overlap)
        start = chunk_id * stride
        if start >= total_chars:
            raise ValueError(
                f"chunk_id={chunk_id} is out of range for content length {total_chars}."
            )
        end = min(total_chars, start + read_chunk_size)
        mode = "chunk"
    elif range_spec is not None:
        start, end = range_spec
        if start >= total_chars:
            raise ValueError(
                f"range start {start} is out of range for content length {total_chars}."
            )
        end = min(end, total_chars)
        mode = "range"

    selected = content[start:end]
    truncated = False
    if max_chars is not None and len(selected) > max_chars:
        selected = selected[:max_chars]
        end = start + len(selected)
        truncated = True

    return selected, {
        "mode": mode,
        "start": start,
        "end": end,
        "selected_chars": len(selected),
        "total_chars": total_chars,
        "truncated_by_max_chars": truncated,
    }


async def collect_ancestor_memories(
    client: Any,
    *,
    domain: str,
    path: str,
    make_uri: Callable[[str, str], str],
    event_preview: Callable[[str, int], str],
    max_hops: int = 64,
) -> List[Dict[str, Any]]:
    """Collect ancestor memories from parent path to root, deduplicated."""
    path_value = (path or "").strip().strip("/")
    if not path_value:
        return []

    segments = [segment for segment in path_value.split("/") if segment]
    if len(segments) <= 1:
        return []

    ancestors: List[Dict[str, Any]] = []
    seen_keys: set[Tuple[Any, str]] = set()
    for depth in range(len(segments) - 1, 0, -1):
        if len(ancestors) >= max_hops:
            break
        candidate_path = "/".join(segments[:depth])
        memory = await client.get_memory_by_path(candidate_path, domain)
        if not memory:
            continue
        ancestor_uri = make_uri(domain, candidate_path)
        key = (memory.get("id"), ancestor_uri)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        ancestors.append(
            {
                "uri": ancestor_uri,
                "memory_id": memory.get("id"),
                "priority": memory.get("priority", 0),
                "disclosure": memory.get("disclosure"),
                "content_snippet": event_preview(str(memory.get("content", "")), 160),
            }
        )
    return ancestors


async def fetch_and_format_memory(
    client: Any,
    uri: str,
    *,
    parse_uri: Callable[[str], tuple[str, str]],
    make_uri: Callable[[str, str], str],
    default_domain: str,
    collect_ancestor_memories_fn: Callable[..., Awaitable[List[Dict[str, Any]]]],
    event_preview: Callable[[str, int], str],
    include_ancestors: bool = False,
) -> str:
    """Fetch memory data and return the legacy formatted string response."""
    domain, path = parse_uri(uri)
    memory = await client.get_memory_by_path(path, domain)

    if not memory:
        raise ValueError(f"URI '{make_uri(domain, path)}' not found.")

    disp_domain = memory.get("domain", default_domain)
    disp_path = memory.get("path", "unknown")
    disp_uri = make_uri(disp_domain, disp_path)
    children = await client.get_children(memory["id"])

    ancestors: List[Dict[str, Any]] = []
    ancestors_lookup_failed = False
    if include_ancestors:
        try:
            ancestors = await collect_ancestor_memories_fn(
                client,
                domain=disp_domain,
                path=disp_path,
            )
        except Exception:
            ancestors = []
            ancestors_lookup_failed = True

    lines: List[str] = []
    lines.append("=" * 60)
    lines.append("")
    lines.append(f"MEMORY: {disp_uri}")
    lines.append(f"Memory ID: {memory.get('id')}")
    lines.append(f"Priority: {memory.get('priority', 0)}")

    disclosure = memory.get("disclosure")
    if disclosure:
        lines.append(f"Disclosure: {disclosure}")
    else:
        lines.append("Disclosure: (not set)")

    lines.append("")
    lines.append("=" * 60)
    lines.append("")
    lines.append(memory.get("content", "(empty)"))
    lines.append("")

    if include_ancestors:
        lines.append("=" * 60)
        lines.append("")
        lines.append("ANCESTOR MEMORIES (Nearest Parent -> Root)")
        lines.append("")
        lines.append("=" * 60)
        lines.append("")
        if ancestors_lookup_failed:
            lines.append("(Ancestor lookup degraded: include_ancestors_lookup_failed.)")
            lines.append("")
        elif ancestors:
            for ancestor in ancestors:
                lines.append(f"- URI: {ancestor.get('uri')} [#{ancestor.get('memory_id')}]")
                lines.append(f"  Priority: {ancestor.get('priority', 0)}")
                ancestor_disclosure = ancestor.get("disclosure")
                if ancestor_disclosure:
                    lines.append(f"  When to recall: {ancestor_disclosure}")
                else:
                    lines.append("  When to recall: (not set)")
                lines.append(f"  Snippet: {ancestor.get('content_snippet') or '(empty)'}")
                lines.append("")
        else:
            lines.append("(No ancestor memories found.)")
            lines.append("")

    if children:
        lines.append("=" * 60)
        lines.append("")
        lines.append("CHILD MEMORIES (Use 'read_memory' with URI to access)")
        lines.append("")
        lines.append("=" * 60)
        lines.append("")

        for child in children:
            child_domain = child.get("domain", disp_domain)
            child_path = child.get("path", "")
            child_uri = make_uri(child_domain, child_path)
            child_disclosure = child.get("disclosure")
            content_preview = event_preview(str(child.get("content", "")), 120)

            lines.append(f"- URI: {child_uri}  ")
            lines.append(f"  Priority: {child.get('priority', 0)}  ")

            if child_disclosure:
                lines.append(f"  When to recall: {child_disclosure}  ")
            else:
                lines.append("  When to recall: (not set)  ")
            lines.append(f"  Snippet: {content_preview or '(empty)'}")

            lines.append("")

    return "\n".join(lines)
