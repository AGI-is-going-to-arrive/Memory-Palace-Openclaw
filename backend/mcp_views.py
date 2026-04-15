from typing import Any, Awaitable, Callable, Dict, List, Optional


async def resolve_system_uri(
    uri: str,
    *,
    generate_boot_memory_view: Callable[[], Awaitable[str]],
    generate_memory_index_view: Callable[[], Awaitable[str]],
    generate_index_lite_memory_view: Callable[[], Awaitable[str]],
    generate_audit_memory_view: Callable[[], Awaitable[str]],
    generate_recent_memories_view: Callable[[int], Awaitable[str]],
) -> Optional[str]:
    """Resolve system:// URI values, or return None if not a system URI."""
    stripped = uri.strip()
    if stripped == "system://boot":
        return await generate_boot_memory_view()
    if stripped == "system://index":
        return await generate_memory_index_view()
    if stripped == "system://index-lite":
        return await generate_index_lite_memory_view()
    if stripped == "system://audit":
        return await generate_audit_memory_view()
    if stripped == "system://recent" or stripped.startswith("system://recent/"):
        limit = 10
        suffix = stripped[len("system://recent") :].strip("/")
        if suffix:
            try:
                limit = max(1, min(100, int(suffix)))
            except ValueError as exc:
                raise ValueError(
                    "Invalid system://recent URI. "
                    "Use system://recent or system://recent/N."
                ) from exc
        return await generate_recent_memories_view(limit)
    return None


async def generate_boot_memory_view(
    *,
    client: Any,
    core_memory_uris: List[str],
    fetch_and_format_memory: Callable[[Any, str], Awaitable[str]],
    should_expose_index_lite_in_boot: Callable[[], bool],
    generate_recent_memories_view: Callable[[int], Awaitable[str]],
) -> str:
    """Generate the system boot memory view."""
    results: List[str] = []
    loaded = 0
    failed: List[str] = []

    for uri in core_memory_uris:
        try:
            content = await fetch_and_format_memory(client, uri)
            results.append(content)
            loaded += 1
        except Exception as exc:
            failed.append(f"- {uri}: {str(exc)}")

    output_parts: List[str] = []
    output_parts.append("# Core Memories")
    output_parts.append(f"# Loaded: {loaded}/{len(core_memory_uris)} memories")
    output_parts.append("")

    if failed:
        output_parts.append("## Failed to load:")
        output_parts.extend(failed)
        output_parts.append("")

    if results:
        output_parts.append("## Contents:")
        output_parts.append("")
        output_parts.append("For full memory index, use: system://index")
        output_parts.append("For recent memories, use: system://recent")
        if should_expose_index_lite_in_boot():
            output_parts.append("For gist-backed lightweight index, use: system://index-lite")
        output_parts.extend(results)
    else:
        output_parts.append("(No core memories loaded yet.)")

    try:
        recent_view = await generate_recent_memories_view(5)
        output_parts.append("")
        output_parts.append("---")
        output_parts.append("")
        output_parts.append(recent_view)
    except Exception:
        pass

    return "\n".join(output_parts)


async def generate_memory_index_view(
    *,
    client: Any,
    generated_at: str,
    default_domain: str,
    make_uri: Callable[[str, str], str],
) -> str:
    """Generate the full memory index view."""
    try:
        paths = await client.get_all_paths()

        lines: List[str] = []
        lines.append("# Memory Index")
        lines.append(f"# Generated: {generated_at}")
        lines.append(f"# Total entries: {len(paths)}")
        lines.append(
            "# Legend: [#ID] = Memory ID (same ID = alias), [★N] = priority (lower = higher priority)"
        )
        lines.append("")

        domains: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
        for item in paths:
            domain = item.get("domain", default_domain)
            if domain not in domains:
                domains[domain] = {}

            path = item["path"]
            top_level = path.split("/")[0] if path else "(root)"
            if top_level not in domains[domain]:
                domains[domain][top_level] = []
            domains[domain][top_level].append(item)

        for domain_name in sorted(domains.keys()):
            lines.append("# ══════════════════════════════════════")
            lines.append(f"# DOMAIN: {domain_name}://")
            lines.append("# ══════════════════════════════════════")
            lines.append("")

            for group_name in sorted(domains[domain_name].keys()):
                lines.append(f"## {group_name}")
                for item in sorted(
                    domains[domain_name][group_name], key=lambda x: x["path"]
                ):
                    uri = item.get("uri", make_uri(domain_name, item["path"]))
                    priority = item.get("priority", 0)
                    memory_id = item.get("memory_id", "?")
                    imp_str = f" [★{priority}]" if priority > 0 else ""
                    lines.append(f"  - {uri} [#{memory_id}]{imp_str}")
                lines.append("")

        return "\n".join(lines)
    except Exception as exc:
        return f"Error generating index: {str(exc)}"


async def generate_recent_memories_view(
    *,
    client: Any,
    generated_at: str,
    limit: int = 10,
) -> str:
    """Generate a view of recently modified memories."""
    try:
        results = await client.get_recent_memories(limit=limit)

        lines: List[str] = []
        lines.append("# Recently Modified Memories")
        lines.append(f"# Generated: {generated_at}")
        lines.append(
            f"# Showing: {len(results)} most recent entries (requested: {limit})"
        )
        lines.append("")

        if not results:
            lines.append("(No memories found.)")
            return "\n".join(lines)

        for i, item in enumerate(results, 1):
            uri = item["uri"]
            priority = item.get("priority", 0)
            disclosure = item.get("disclosure")
            raw_ts = item.get("created_at", "")

            if raw_ts and len(raw_ts) >= 16:
                modified = raw_ts[:10] + " " + raw_ts[11:16]
            else:
                modified = raw_ts or "unknown"

            imp_str = f"★{priority}"
            lines.append(f"{i}. {uri}  [{imp_str}]  modified: {modified}")
            if disclosure:
                lines.append(f"   disclosure: {disclosure}")
            else:
                lines.append("   disclosure: (NOT SET — consider adding one)")
            lines.append("")

        return "\n".join(lines)
    except Exception as exc:
        return f"Error generating recent memories view: {str(exc)}"


async def generate_index_lite_memory_view(
    *,
    client: Any,
    generated_at: str,
    trim_sentence: Callable[[str, int], str],
    limit: int = 20,
) -> str:
    """Generate a lightweight gist-backed index summary."""
    target_limit = max(1, min(100, int(limit)))
    degrade_reasons: List[str] = []

    gist_stats: Dict[str, Any] = {}
    gist_stats_getter = getattr(client, "get_gist_stats", None)
    if callable(gist_stats_getter):
        try:
            raw_stats = await gist_stats_getter()
            if isinstance(raw_stats, dict):
                gist_stats = raw_stats
            else:
                degrade_reasons.append("invalid_gist_stats_payload")
        except Exception:
            degrade_reasons.append("gist_stats_error")
    else:
        degrade_reasons.append("gist_stats_unavailable")

    recent_entries: List[Dict[str, Any]] = []
    recent_getter = getattr(client, "get_recent_memories", None)
    if callable(recent_getter):
        try:
            raw_recent = await recent_getter(limit=max(target_limit * 3, 20))
            if isinstance(raw_recent, list):
                recent_entries = [row for row in raw_recent if isinstance(row, dict)]
            else:
                degrade_reasons.append("invalid_recent_memories_payload")
        except Exception:
            degrade_reasons.append("recent_memories_error")
    else:
        degrade_reasons.append("recent_memories_unavailable")

    gist_lookup = getattr(client, "get_latest_memory_gist", None)
    index_items: List[Dict[str, Any]] = []
    if callable(gist_lookup):
        for row in recent_entries:
            memory_id = int(row.get("memory_id") or -1)
            if memory_id <= 0:
                continue
            uri = str(row.get("uri") or "").strip()
            if not uri:
                continue
            try:
                gist_row = await gist_lookup(memory_id)
            except Exception:
                gist_row = None
                if "latest_gist_lookup_error" not in degrade_reasons:
                    degrade_reasons.append("latest_gist_lookup_error")
            if not isinstance(gist_row, dict):
                continue
            gist_text = str(gist_row.get("gist_text") or "").strip()
            if not gist_text:
                continue
            quality_raw = gist_row.get("quality_score")
            try:
                quality_value = round(float(quality_raw), 3)
            except (TypeError, ValueError):
                quality_value = None
            index_items.append(
                {
                    "uri": uri,
                    "memory_id": memory_id,
                    "gist_method": str(gist_row.get("gist_method") or "unknown"),
                    "quality_score": quality_value,
                    "gist_preview": trim_sentence(gist_text, limit=96),
                    "updated_at": row.get("created_at"),
                }
            )
            if len(index_items) >= target_limit:
                break
    else:
        degrade_reasons.append("latest_gist_lookup_unavailable")

    lines: List[str] = []
    lines.append("# Memory Index Lite")
    lines.append(f"# Generated: {generated_at}")
    lines.append(f"# Entry count: {len(index_items)}")
    if isinstance(gist_stats.get("total_rows"), int):
        lines.append(f"# Gist rows: {gist_stats.get('total_rows')}")
    if isinstance(gist_stats.get("active_coverage"), (int, float)):
        lines.append(f"# Gist active coverage: {gist_stats.get('active_coverage')}")
    if degrade_reasons:
        lines.append("# Status: degraded")
        lines.append(f"# degrade_reason: {', '.join(sorted(set(degrade_reasons)))}")
    else:
        lines.append("# Status: ok")
    lines.append("")

    if not index_items:
        lines.append("(No gist-backed entries found.)")
    else:
        for idx, item in enumerate(index_items, 1):
            quality_value = item.get("quality_score")
            quality_text = (
                "n/a" if quality_value is None else f"{float(quality_value):.3f}"
            )
            lines.append(
                f"{idx}. {item['uri']} [#{item['memory_id']}] "
                f"(method={item['gist_method']}, quality={quality_text})"
            )
            lines.append(f"   gist: {item['gist_preview']}")
            if item.get("updated_at"):
                lines.append(f"   updated_at: {item['updated_at']}")
            lines.append("")

    return "\n".join(lines)


async def generate_audit_memory_view(
    *,
    client: Any,
    runtime_state: Any,
    get_sqlite_client: Callable[[], Any],
    utc_iso_now: Callable[[], str],
    build_index_status_payload: Callable[[Any], Awaitable[Dict[str, Any]]],
    load_persisted_import_learn_summary: Callable[[Any], Awaitable[Optional[Dict[str, Any]]]],
    merge_import_learn_summaries: Callable[..., Dict[str, Any]],
    safe_non_negative_int: Callable[[Any], int],
    build_sm_lite_stats: Callable[[], Awaitable[Dict[str, Any]]],
    audit_verbose: bool,
    to_json: Callable[[Dict[str, Any]], str],
) -> str:
    """Generate a consolidated audit view for index/guard/gist/vitality and SM-Lite."""
    await runtime_state.ensure_started(get_sqlite_client)

    generated_at = utc_iso_now()
    degrade_reasons: List[str] = []

    try:
        index_status = await build_index_status_payload(client)
    except Exception as exc:
        index_status = {"degraded": True, "reason": str(exc), "index_available": False}
        degrade_reasons.append("index_status_error")
    if bool(index_status.get("degraded")):
        degrade_reasons.append(f"index:{str(index_status.get('reason') or 'degraded')}")

    try:
        guard_stats = await runtime_state.guard_tracker.summary()
    except Exception as exc:
        guard_stats = {"degraded": True, "reason": str(exc)}
        degrade_reasons.append("guard_stats_error")

    try:
        import_learn_stats = await runtime_state.import_learn_tracker.summary()
    except Exception as exc:
        import_learn_stats = {"degraded": True, "reason": str(exc)}
        degrade_reasons.append("import_learn:error")
    if bool(import_learn_stats.get("degraded")):
        degrade_reasons.append(
            f"import_learn:{str(import_learn_stats.get('reason') or 'degraded')}"
        )
    persisted_import_learn_stats = await load_persisted_import_learn_summary(client)
    if isinstance(import_learn_stats, dict) and isinstance(persisted_import_learn_stats, dict):
        runtime_total = safe_non_negative_int(import_learn_stats.get("total_events"))
        if runtime_total <= 0:
            import_learn_stats = merge_import_learn_summaries(
                runtime_summary=import_learn_stats,
                persisted_summary=persisted_import_learn_stats,
            )

    gist_stats_getter = getattr(client, "get_gist_stats", None)
    if callable(gist_stats_getter):
        try:
            gist_stats = await gist_stats_getter()
            if not isinstance(gist_stats, dict):
                gist_stats = {"degraded": True, "reason": "invalid_gist_stats_payload"}
                degrade_reasons.append("gist:invalid_payload")
        except Exception as exc:
            gist_stats = {"degraded": True, "reason": str(exc)}
            degrade_reasons.append("gist:error")
    else:
        gist_stats = {"degraded": True, "reason": "gist_stats_unavailable"}
        degrade_reasons.append("gist:unavailable")
    if bool(gist_stats.get("degraded")):
        degrade_reasons.append(f"gist:{str(gist_stats.get('reason') or 'degraded')}")

    vitality_stats_getter = getattr(client, "get_vitality_stats", None)
    if callable(vitality_stats_getter):
        try:
            vitality_stats = await vitality_stats_getter()
            if not isinstance(vitality_stats, dict):
                vitality_stats = {"degraded": True, "reason": "invalid_vitality_stats_payload"}
                degrade_reasons.append("vitality:invalid_payload")
        except Exception as exc:
            vitality_stats = {"degraded": True, "reason": str(exc)}
            degrade_reasons.append("vitality:error")
    else:
        vitality_stats = {"degraded": True, "reason": "vitality_stats_unavailable"}
        degrade_reasons.append("vitality:unavailable")
    if bool(vitality_stats.get("degraded")):
        degrade_reasons.append(f"vitality:{str(vitality_stats.get('reason') or 'degraded')}")

    try:
        sm_lite = await build_sm_lite_stats()
    except Exception as exc:
        sm_lite = {"degraded": True, "reason": str(exc)}
        degrade_reasons.append("sm_lite:error")
    if bool(sm_lite.get("degraded")):
        degrade_reasons.append(f"sm_lite:{str(sm_lite.get('reason') or 'degraded')}")

    lines: List[str] = []
    lines.append("# System Audit")
    lines.append(f"# Generated: {generated_at}")
    lines.append(f"# Status: {'degraded' if degrade_reasons else 'ok'}")
    if degrade_reasons:
        lines.append(f"# degrade_reason: {', '.join(sorted(set(degrade_reasons)))}")
    lines.append("")

    lines.append("## Index")
    lines.append(f"- index_available: {bool(index_status.get('index_available', False))}")
    lines.append(f"- degraded: {bool(index_status.get('degraded', False))}")
    if index_status.get("reason"):
        lines.append(f"- reason: {index_status.get('reason')}")
    lines.append(f"- source: {index_status.get('source', 'unknown')}")
    lines.append("")

    lines.append("## Guard")
    lines.append(f"- total_events: {guard_stats.get('total_events', 0)}")
    lines.append(f"- blocked_events: {guard_stats.get('blocked_events', 0)}")
    lines.append(f"- degraded_events: {guard_stats.get('degraded_events', 0)}")
    lines.append(f"- last_event_at: {guard_stats.get('last_event_at') or 'n/a'}")
    lines.append("")

    lines.append("## Import/Learn")
    lines.append(f"- total_events: {import_learn_stats.get('total_events', 0)}")
    lines.append(f"- rejected_events: {import_learn_stats.get('rejected_events', 0)}")
    lines.append(f"- rollback_events: {import_learn_stats.get('rollback_events', 0)}")
    lines.append(
        f"- learn_events: {import_learn_stats.get('event_type_breakdown', {}).get('learn', 0)}"
    )
    lines.append(
        f"- import_events: {import_learn_stats.get('event_type_breakdown', {}).get('import', 0)}"
    )
    lines.append(f"- last_event_at: {import_learn_stats.get('last_event_at') or 'n/a'}")
    if isinstance(import_learn_stats.get("persisted_snapshot"), dict):
        persisted = import_learn_stats["persisted_snapshot"]
        lines.append("- persisted_snapshot: true")
        lines.append(f"- persisted_total_events: {safe_non_negative_int(persisted.get('total_events'))}")
        lines.append(f"- persisted_last_event_at: {persisted.get('last_event_at') or 'n/a'}")
    else:
        lines.append("- persisted_snapshot: false")
    lines.append("")

    lines.append("## Gist")
    lines.append(f"- degraded: {bool(gist_stats.get('degraded', False))}")
    if gist_stats.get("reason"):
        lines.append(f"- reason: {gist_stats.get('reason')}")
    if gist_stats.get("total_rows") is not None:
        lines.append(f"- total_rows: {gist_stats.get('total_rows')}")
    if gist_stats.get("active_coverage") is not None:
        lines.append(f"- active_coverage: {gist_stats.get('active_coverage')}")
    lines.append("")

    lines.append("## Vitality")
    lines.append(f"- degraded: {bool(vitality_stats.get('degraded', False))}")
    if vitality_stats.get("reason"):
        lines.append(f"- reason: {vitality_stats.get('reason')}")
    if vitality_stats.get("total_memories") is not None:
        lines.append(f"- total_memories: {vitality_stats.get('total_memories')}")
    if vitality_stats.get("low_vitality_count") is not None:
        lines.append(f"- low_vitality_count: {vitality_stats.get('low_vitality_count')}")
    lines.append("")

    lines.append("## SM-Lite (Runtime Working Set)")
    session_cache = sm_lite.get("session_cache", {}) if isinstance(sm_lite, dict) else {}
    flush_tracker = sm_lite.get("flush_tracker", {}) if isinstance(sm_lite, dict) else {}
    promotion = sm_lite.get("promotion", {}) if isinstance(sm_lite, dict) else {}
    lines.append(f"- storage: {sm_lite.get('storage', 'runtime_ephemeral')}")
    lines.append(f"- promotion_path: {sm_lite.get('promotion_path', 'compact_context + auto_flush')}")
    lines.append(f"- session_cache.session_count: {session_cache.get('session_count', 0)}")
    lines.append(f"- session_cache.total_hits: {session_cache.get('total_hits', 0)}")
    lines.append(f"- flush_tracker.session_count: {flush_tracker.get('session_count', 0)}")
    lines.append(f"- flush_tracker.pending_events: {flush_tracker.get('pending_events', 0)}")
    lines.append(f"- promotion.total_promotions: {promotion.get('total_promotions', 0)}")
    lines.append(f"- promotion.degraded_promotions: {promotion.get('degraded_promotions', 0)}")
    lines.append(f"- promotion.avg_quality: {promotion.get('avg_quality', 0.0)}")
    lines.append("")

    if audit_verbose:
        lines.append("## Verbose Payloads")
        lines.append("")
        lines.append("### index_status")
        lines.append(to_json(index_status))
        lines.append("")
        lines.append("### guard_stats")
        lines.append(to_json(guard_stats))
        lines.append("")
        lines.append("### import_learn_audit_stats")
        lines.append(to_json(import_learn_stats))
        lines.append("")
        lines.append("### gist_stats")
        lines.append(to_json(gist_stats))
        lines.append("")
        lines.append("### vitality_stats")
        lines.append(to_json(vitality_stats))
        lines.append("")
        lines.append("### sm_lite")
        lines.append(to_json(sm_lite))
        lines.append("")

    return "\n".join(lines)
