import json
import re
from typing import Any, Callable, Dict, List, Optional, Tuple


FORCE_CONTROL_BEGIN = "<!-- MEMORY_PALACE_FORCE_CONTROL_V1 -->"
FORCE_CONTROL_END = "<!-- /MEMORY_PALACE_FORCE_CONTROL_V1 -->"


def control_trailer_text_impl(content: str, *, max_lines: int = 12) -> str:
    if not isinstance(content, str):
        return ""
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    trailing_trimmed = normalized.rstrip()
    if trailing_trimmed.endswith(FORCE_CONTROL_END):
        end_index = trailing_trimmed.rfind(FORCE_CONTROL_END)
        start_index = trailing_trimmed.rfind(FORCE_CONTROL_BEGIN, 0, end_index)
        if start_index >= 0:
            block = trailing_trimmed[
                start_index + len(FORCE_CONTROL_BEGIN):end_index
            ].strip("\n")
            if block.strip():
                return block
            return ""
    lines = trailing_trimmed.split("\n")
    while lines and not lines[-1].strip():
        lines.pop()
    if len(lines) > max_lines:
        lines = lines[-max_lines:]
    return "\n".join(lines)


_LEGACY_TRAILER_RE = re.compile(
    r"\n\n---\n\n"                            # exact separator
    r"- create_after_merge_update_write_guard: true\n"
    r"- target_uri: \S+"                       # URI (no spaces)
    r"\s*$"                                    # trailing whitespace + end
)


def strip_force_control_trailer(content: str) -> str:
    """Remove force-create control trailers so they are never persisted.

    Handles two trailer forms:
    1. The structured ``<!-- MEMORY_PALACE_FORCE_CONTROL_V1 -->`` block.
    2. The legacy trailer emitted by the plugin's ``retryForcedCreate``:
       ``\\n\\n---\\n\\n- create_after_merge_update_write_guard: true\\n- target_uri: …``
       This is matched as a **suffix-anchored regex** so it cannot
       accidentally truncate legitimate body text that happens to contain
       the marker string.

    Returns the cleaned content with trailing whitespace normalised.
    """
    if not isinstance(content, str):
        return content
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")

    # Form 1: structured control block (always at the very end)
    stripped = normalized.rstrip()
    if stripped.endswith(FORCE_CONTROL_END):
        end_idx = stripped.rfind(FORCE_CONTROL_END)
        start_idx = stripped.rfind(FORCE_CONTROL_BEGIN, 0, end_idx)
        if start_idx >= 0:
            stripped = stripped[:start_idx].rstrip()

    # Form 2: legacy free-text trailer – only match if it appears as a
    # suffix with the exact separator produced by retryForcedCreate.
    stripped = _LEGACY_TRAILER_RE.sub("", stripped).rstrip()

    return stripped


def extract_literal_line_value_impl(content: str, prefix: str) -> Optional[str]:
    pattern = re.compile(rf"^{re.escape(prefix)}\s*(.+)$", re.MULTILINE)
    matched = pattern.search(content)
    if not matched:
        return None
    value = matched.group(1).strip()
    return value or None


def extract_control_line_value_impl(
    content: str, prefix: str, *, control_trailer_text: Callable[[str], str]
) -> Optional[str]:
    pattern = re.compile(rf"^{re.escape(prefix)}\s*(.+)$", re.MULTILINE)
    matched = pattern.search(control_trailer_text(content))
    if not matched:
        return None
    value = matched.group(1).strip()
    return value or None


def extract_force_create_meta_candidates_impl(
    content: str,
    *,
    control_trailer_text: Callable[[str], str],
    force_meta_pattern: re.Pattern[str],
) -> List[Dict[str, Any]]:
    if not isinstance(content, str):
        return []
    candidates: List[Dict[str, Any]] = []
    for matched in force_meta_pattern.finditer(control_trailer_text(content)):
        raw = str(matched.group(1) or "").strip()
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(parsed, dict):
            candidates.append(parsed)
    return candidates


def meta_string_impl(meta: Dict[str, Any], key: str) -> Optional[str]:
    value = meta.get(key)
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    return None


def has_force_create_meta_impl(
    content: str,
    *,
    kind: str,
    requested_uri: Optional[str],
    extract_force_create_meta_candidates: Callable[[str], List[Dict[str, Any]]],
    meta_string: Callable[[Dict[str, Any], str], Optional[str]],
    uri_keys: Tuple[str, ...] = ("requested_uri",),
    predicate: Optional[Callable[[Dict[str, Any]], bool]] = None,
) -> bool:
    if not requested_uri:
        return False
    normalized_kind = str(kind or "").strip()
    for meta in extract_force_create_meta_candidates(content):
        if meta_string(meta, "kind") != normalized_kind:
            continue
        meta_uris = {meta_string(meta, key) for key in uri_keys}
        if requested_uri not in meta_uris:
            continue
        if predicate and not predicate(meta):
            continue
        return True
    return False


def requested_create_uri_impl(
    domain: str,
    parent_path: str,
    title: Optional[str],
    *,
    make_uri: Callable[[str, str], str],
) -> Optional[str]:
    if not isinstance(title, str) or not title.strip():
        return None
    path = f"{parent_path}/{title}" if parent_path else title
    return make_uri(domain, path)


def is_forced_visual_variant_create_impl(
    content: str,
    requested_uri: Optional[str],
    *,
    control_trailer_text: Callable[[str], str],
    has_force_create_meta: Callable[..., bool],
    meta_string: Callable[[Dict[str, Any], str], Optional[str]],
    extract_control_line_value: Callable[[str, str], Optional[str]],
) -> bool:
    if not isinstance(content, str) or not requested_uri:
        return False
    trailer = control_trailer_text(content)
    if has_force_create_meta(
        content,
        kind="visual_duplicate_variant",
        requested_uri=requested_uri,
        uri_keys=("requested_uri", "variant_uri"),
        predicate=lambda meta: meta_string(meta, "duplicate_policy") == "new",
    ):
        return True
    return (
        "- kind: visual-memory" in content
        and "- duplicate_policy: new" in content
        and "- duplicate_variant: new-" in content
        and "VISUAL_DUP_FORCE_RULE=RETAIN_DISTINCT_VARIANT_RECORD" in trailer
        and extract_control_line_value(content, "VISUAL_DUP_FORCE_VARIANT_URI=")
        == requested_uri
    )


def is_forced_visual_distinct_create_impl(
    content: str,
    requested_uri: Optional[str],
    *,
    has_force_create_meta: Callable[..., bool],
    extract_control_line_value: Callable[[str, str], Optional[str]],
) -> bool:
    if not isinstance(content, str) or not requested_uri:
        return False
    if has_force_create_meta(
        content,
        kind="visual_distinct_force_create",
        requested_uri=requested_uri,
        uri_keys=("requested_uri", "target_uri"),
    ):
        return True
    return (
        "- kind: visual-memory" in content
        and "- visual_force_create_reason: disambiguate non-duplicate visual record after write_guard collision"
        in content
        and extract_control_line_value(content, "- visual_force_create_uri:")
        == requested_uri
    )


def is_forced_visual_namespace_create_impl(
    content: str,
    requested_uri: Optional[str],
    *,
    control_trailer_text: Callable[[str], str],
    has_force_create_meta: Callable[..., bool],
    extract_control_line_value: Callable[[str, str], Optional[str]],
) -> bool:
    if not isinstance(content, str) or not requested_uri:
        return False
    trailer = control_trailer_text(content)
    if has_force_create_meta(
        content,
        kind="visual_namespace_force_create",
        requested_uri=requested_uri,
        uri_keys=("requested_uri", "target_uri", "namespace_uri"),
    ):
        return True
    return (
        "visual_namespace_container: true" in content
        and "VISUAL_NS_FORCE_RULE=NO_DEDUP_WITH_PARENT_OR_SIBLING" in trailer
        and "- visual_force_create_reason: disambiguate non-duplicate visual record after write_guard collision"
        in trailer
        and extract_control_line_value(content, "- visual_force_create_uri:")
        == requested_uri
        and extract_control_line_value(content, "VISUAL_NS_FORCE_URI=") == requested_uri
    )


def is_forced_memory_palace_namespace_create_impl(
    content: str,
    requested_uri: Optional[str],
    *,
    control_trailer_text: Callable[[str], str],
    has_force_create_meta: Callable[..., bool],
    meta_string: Callable[[Dict[str, Any], str], Optional[str]],
    extract_literal_line_value: Callable[[str, str], Optional[str]],
    extract_control_line_value: Callable[[str, str], Optional[str]],
) -> bool:
    if not isinstance(content, str) or not requested_uri:
        return False
    trailer = control_trailer_text(content)
    if not requested_uri.startswith("core://agents"):
        return False
    if has_force_create_meta(
        content,
        kind="memory_palace_namespace_force_create",
        requested_uri=requested_uri,
        uri_keys=("requested_uri", "target_uri"),
        predicate=lambda meta: meta_string(meta, "lane")
        in {"capture", "profile", "reflection"},
    ):
        return True
    lane = (
        extract_literal_line_value(content, "- lane:")
        or extract_literal_line_value(content, "namespace_lane:")
        or extract_control_line_value(content, "MP_NS_FORCE_CREATE_LANE=")
    )
    namespace_uri = (
        extract_literal_line_value(content, "- namespace_uri:")
        or extract_literal_line_value(content, "namespace_uri:")
        or extract_control_line_value(content, "MP_NS_FORCE_URI=")
        or extract_control_line_value(content, "MP_NS_FORCE_CREATE_URI=")
    )
    if lane not in {"capture", "profile", "reflection"}:
        return False
    return (
        "# Memory Palace Namespace" in content
        and (
            "MP_NS_FORCE_URI=" in trailer
            or "MP_NS_FORCE_CREATE_URI=" in trailer
        )
        and namespace_uri == requested_uri
    )


def is_forced_host_bridge_create_impl(
    content: str,
    requested_uri: Optional[str],
    *,
    has_force_create_meta: Callable[..., bool],
    extract_control_line_value: Callable[[str, str], Optional[str]],
) -> bool:
    if not isinstance(content, str) or not requested_uri:
        return False
    if not requested_uri.startswith("core://agents/"):
        return False
    if has_force_create_meta(
        content,
        kind="host_bridge_force_create",
        requested_uri=requested_uri,
        uri_keys=("requested_uri", "target_uri"),
    ):
        return True
    return (
        "# Host Workspace Import" in content
        and "- capture_layer: host_bridge" in content
        and "- source_mode: host_workspace_import" in content
        and extract_control_line_value(content, "- host_bridge_force_create_uri:")
        == requested_uri
    )


def is_forced_explicit_memory_create_impl(
    content: str,
    requested_uri: Optional[str],
    *,
    control_trailer_text: Callable[[str], str],
    extract_control_line_value: Callable[[str, str], Optional[str]],
) -> bool:
    if not isinstance(content, str) or not requested_uri:
        return False
    if not requested_uri.startswith("core://agents/"):
        return False
    if "/captured/" not in requested_uri:
        return False
    trailer = control_trailer_text(content)
    allowed_titles = (
        "# Memory Palace Durable Fact" in content
        or "# Auto Captured Memory" in content
    )
    return (
        allowed_titles
        and "## Content" in content
        and "- create_after_merge_update_write_guard: true" in trailer
        and extract_control_line_value(content, "- target_uri:") == requested_uri
    )


def is_forced_durable_synthesis_current_create_impl(
    content: str,
    requested_uri: Optional[str],
    *,
    control_trailer_text: Callable[[str], str],
    has_force_create_meta: Callable[..., bool],
    meta_string: Callable[[Dict[str, Any], str], Optional[str]],
    extract_control_line_value: Callable[[str, str], Optional[str]],
) -> bool:
    if not isinstance(content, str) or not requested_uri:
        return False
    trailer = control_trailer_text(content)
    if not requested_uri.startswith("core://agents/"):
        return False
    if has_force_create_meta(
        content,
        kind="durable_synthesis_force_current",
        requested_uri=requested_uri,
        uri_keys=("requested_uri", "target_uri"),
        predicate=lambda meta: meta_string(meta, "source_mode") == "llm_extracted"
        and meta_string(meta, "capture_layer") == "smart_extraction",
    ):
        return True
    return (
        "# Memory Palace Durable Fact" in content
        and "- source_mode: llm_extracted" in content
        and "- capture_layer: smart_extraction" in content
        and "- durable_synthesis_force_current: true" in trailer
        and "/llm-extracted/" in requested_uri
        and requested_uri.endswith("/current")
        and extract_control_line_value(content, "- target_uri:") == requested_uri
    )


def is_forced_durable_synthesis_variant_create_impl(
    content: str,
    requested_uri: Optional[str],
    *,
    control_trailer_text: Callable[[str], str],
    has_force_create_meta: Callable[..., bool],
    meta_string: Callable[[Dict[str, Any], str], Optional[str]],
    extract_control_line_value: Callable[[str, str], Optional[str]],
) -> bool:
    if not isinstance(content, str) or not requested_uri:
        return False
    trailer = control_trailer_text(content)
    if not requested_uri.startswith("core://agents/"):
        return False
    if has_force_create_meta(
        content,
        kind="durable_synthesis_force_variant",
        requested_uri=requested_uri,
        uri_keys=("requested_uri", "variant_uri"),
        predicate=lambda meta: (
            isinstance(meta_string(meta, "target_uri"), str)
            and meta_string(meta, "target_uri") != requested_uri
        ),
    ):
        return True
    target_uri = extract_control_line_value(content, "- target_uri:")
    return (
        "# Memory Palace Durable Fact" in content
        and "- source_mode: llm_extracted" in content
        and "- capture_layer: smart_extraction" in content
        and "- durable_synthesis_force_variant: true" in trailer
        and "/current--force-" in requested_uri
        and isinstance(target_uri, str)
        and target_uri != requested_uri
        and target_uri.startswith("core://agents/")
        and "/llm-extracted/" in target_uri
        and target_uri.endswith("/current")
    )


def build_visual_namespace_chain_content_impl(domain: str, segments: List[str]) -> str:
    current_uri = f"{domain}://{'/'.join(segments)}"
    hierarchy = " > ".join(segments)
    if len(segments) == 1:
        purpose = "group image-derived memories by capture date."
    elif len(segments) == 2:
        purpose = "group image-derived memories by month."
    elif len(segments) == 3:
        purpose = "group image-derived memories by day."
    else:
        purpose = "group image-derived memories by namespace depth."
    return "\n".join(
        [
            "# Visual Namespace Container",
            f"Namespace URI: {current_uri}",
            f"Hierarchy: {hierarchy}",
            f"Purpose: {purpose}",
            "Kind: internal namespace container",
        ]
    )
