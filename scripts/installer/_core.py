#!/usr/bin/env python3
from __future__ import annotations

from ._constants import *
from ._utils import *
from ._utils import (
    _backend_api_service_ready,
    _build_pid_file_record,
    _cleanup_timed_out_process,
    _dashboard_service_ready,
    _find_available_loopback_port,
    _is_process_alive,
    _metadata_key,
    _pid_file_record_matches_running_process,
    _port_open,
    _read_optional_int,
    _read_pid_file,
    _read_pid_file_record,
    _remove_file_if_exists,
    _terminate_process,
    _wait_for_port_closed,
    _write_pid_file,
)
from ._provider import *
from ._onboarding import *

def build_provider_install_checks(
    *,
    env_values: Mapping[str, str],
    setup_root_path: Path,
    requested_profile: str,
    effective_profile: str,
    provider_probe: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    probe = (
        dict(provider_probe)
        if isinstance(provider_probe, Mapping)
        else resolve_provider_probe_status(
            env_values=env_values,
            setup_root_path=setup_root_path,
            requested_profile=requested_profile,
            effective_profile=effective_profile,
        )
    )
    checks: list[dict[str, Any]] = []
    summary_status = str(probe.get("summaryStatus") or "unknown").strip().lower()
    checks.append(
        {
            "id": "provider-profile",
            "status": "PASS" if summary_status == "pass" else "WARN",
            "message": str(probe.get("summaryMessage") or "Provider status is unavailable."),
            "details": (
                f"requested={probe.get('requestedProfile') or 'b'} | "
                f"effective={probe.get('effectiveProfile') or 'b'} | "
                f"checked_at={probe.get('checkedAt') or 'n/a'}"
            ),
            **(
                {
                    "action": "Finish the missing provider fields or rerun setup after fixing provider connectivity."
                }
                if summary_status != "pass"
                else {}
            ),
        }
    )
    providers = probe.get("providers") if isinstance(probe.get("providers"), dict) else {}
    for component in ("embedding", "reranker", "llm"):
        item = providers.get(component) if isinstance(providers, dict) else None
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or "unknown").strip().lower()
        component_title = "LLM" if component == "llm" else component.capitalize()
        detail_bits: list[str] = []
        if isinstance(item.get("baseUrl"), str) and item.get("baseUrl"):
            detail_bits.append(f"base={item['baseUrl']}")
        if isinstance(item.get("model"), str) and item.get("model"):
            detail_bits.append(f"model={item['model']}")
        if component == "embedding" and isinstance(item.get("detectedDim"), str) and item.get("detectedDim"):
            detail_bits.append(f"dim={item['detectedDim']}")
        if isinstance(item.get("detail"), str) and item.get("detail"):
            detail_bits.append(str(item["detail"]))
        checks.append(
            {
                "id": f"provider-{component}",
                "status": "PASS" if status in {"pass", "not_required"} else "WARN",
                "message": (
                    f"{component_title} provider probe passed."
                    if status == "pass"
                    else (
                        f"{component_title} provider is optional for the current effective profile."
                        if status == "not_required"
                        else (
                            f"{component_title} provider fields are still missing."
                            if status == "missing"
                            else (
                                f"{component_title} provider probe failed."
                                if status == "fail"
                                else f"{component_title} provider is configured but has not been checked yet."
                            )
                        )
                    )
                ),
                "details": " | ".join(detail_bits) if detail_bits else None,
                **(
                    {
                        "action": (
                            "Fill the missing provider fields in setup first."
                            if status == "missing"
                            else "Check the provider endpoint, credentials, and model name, then rerun setup."
                        )
                    }
                    if status in {"missing", "fail", "not_checked"}
                    else {}
                ),
            }
        )
    return checks


def detect_restart_required(
    env_values: Mapping[str, str],
    *,
    current_env: Mapping[str, str] | None = None,
) -> tuple[bool, list[str]]:
    if not env_values:
        return False, []
    source_env = current_env or os.environ
    mismatch_keys: list[str] = []
    for key in RESTART_RELEVANT_ENV_KEYS:
        expected = str(env_values.get(key) or "").strip()
        current = str(source_env.get(key) or "").strip()
        if expected != current:
            mismatch_keys.append(key)
    return bool(mismatch_keys), mismatch_keys


def detect_reindex_required(
    previous_env: Mapping[str, str],
    next_env: Mapping[str, str],
) -> tuple[bool, list[str]]:
    if not previous_env or not next_env:
        return False, []
    changed_keys: list[str] = []
    for key in REINDEX_RELEVANT_ENV_KEYS:
        previous = str(previous_env.get(key) or "").strip()
        current = str(next_env.get(key) or "").strip()
        if previous != current:
            changed_keys.append(key)
    return bool(changed_keys), changed_keys


_ENV_KEY_TO_SEMANTIC_REASON: dict[str, str] = {
    "RETRIEVAL_EMBEDDING_DIM": "embedding_dim_changed",
    "RETRIEVAL_EMBEDDING_BACKEND": "embedding_backend_changed",
    "RETRIEVAL_EMBEDDING_MODEL": "embedding_model_changed",
    "RETRIEVAL_EMBEDDING_API_BASE": "embedding_provider_changed",
    "RETRIEVAL_RERANKER_ENABLED": "reranker_toggled",
    "RETRIEVAL_RERANKER_API_BASE": "reranker_provider_changed",
    "RETRIEVAL_RERANKER_MODEL": "reranker_model_changed",
    "SEARCH_DEFAULT_MODE": "search_mode_changed",
    "RETRIEVAL_VECTOR_ENGINE": "vector_engine_changed",
    "RETRIEVAL_SQLITE_VEC_ENABLED": "vector_engine_changed",
    "RETRIEVAL_SQLITE_VEC_READ_RATIO": "vector_engine_changed",
}


def _semantic_reindex_reasons(env_keys: list[str]) -> list[str]:
    """Map raw env-key change list to de-duplicated semantic reason keys."""
    seen: set[str] = set()
    reasons: list[str] = []
    for key in env_keys:
        reason = _ENV_KEY_TO_SEMANTIC_REASON.get(key, key)
        if reason not in seen:
            seen.add(reason)
            reasons.append(reason)
    return reasons


# ---------------------------------------------------------------------------
# Unified helpers consumed by CLI (command_onboarding) and REST (bootstrap_status/apply)
# ---------------------------------------------------------------------------


def build_reindex_gate(
    existing_env: Mapping[str, str] | None,
    preview_env: Mapping[str, str] | None,
) -> dict[str, Any]:
    """Build a structured reindex-gate payload from two env snapshots.

    Wraps :func:`detect_reindex_required` (raw env-key diff) and
    :func:`_semantic_reindex_reasons` (human-readable labels) into a single
    dict consumable by the frontend and CLI.

    First-time installs (*existing_env* is empty/None) are non-blocking.
    """
    if not existing_env or not preview_env:
        return {"required": False, "reasonKeys": [], "recommendedAction": None}
    changed, changed_keys = detect_reindex_required(existing_env, preview_env)
    reasons = _semantic_reindex_reasons(changed_keys) if changed else []
    return {
        "required": changed,
        "reasonKeys": reasons,
        "recommendedAction": "reindex_all" if changed else None,
    }


def build_install_guidance() -> dict[str, Any]:
    """Return the static install-guidance payload (HC-6 enforced)."""
    source_checkout = (
        f"{repo_python_command('scripts/openclaw_memory_palace.py setup')} "
        "--mode basic --profile b --transport stdio --json"
    )
    local_tgz_parts = ["openclaw", "plugins", "install"]
    if supports_dangerously_force_unsafe_install("openclaw"):
        local_tgz_parts.append("--dangerously-force-unsafe-install")
    local_tgz_parts.append("./<generated-tgz>")
    local_tgz = " ".join(local_tgz_parts)
    return {
        "supportedInstallMethods": ["source-checkout", "local-tgz"],
        "repoUrlDirectInstallSupported": False,
        "recommendedMethod": "source-checkout",
        "installCommands": {
            "source-checkout": source_checkout,
            "local-tgz": local_tgz,
        },
        "installSteps": {
            "source-checkout": [
                source_checkout,
                "openclaw memory-palace verify --json",
                "openclaw memory-palace doctor --json",
                "openclaw memory-palace smoke --json",
            ],
            "local-tgz": [
                local_tgz,
                "npm exec --yes --package ./<generated-tgz> memory-palace-openclaw -- setup --mode basic --profile b --transport stdio --json",
                "openclaw memory-palace verify --json",
                "openclaw memory-palace doctor --json",
                "openclaw memory-palace smoke --json",
            ],
        },
        "recommendedMethodNote": (
            "Run the source-checkout command from the repository root on the same machine as OpenClaw. "
            "In the current rerun, the public npm spec `@openclaw/memory-palace` returned `Package not found on npm`, "
            "and `openclaw plugins install memory-palace` resolved to a skill rather than a plugin."
        ),
    }


def _redact_seed_value(seed: Any) -> str:
    """Mask sensitive seed values (api_key) so they never leak in payloads."""
    value = str(getattr(seed, "value", "") or "")
    field = str(getattr(seed, "field", "") or "")
    if "key" in field.lower():
        if len(value) > 8:
            return value[:4] + "***" + value[-2:]
        return "***"
    return value


def build_provider_source_trace(
    existing_env: Mapping[str, str] | None = None,
    process_env: Mapping[str, str] | None = None,
    host_config_path: Path | None = None,
) -> dict[str, list[dict[str, str]]]:
    """Collect provider seeds and return a redacted trace safe for payloads.

    Secret fields (api_key) are masked; only source/confidence/field metadata
    and a redacted value are included.
    """
    try:
        seeds = collect_provider_seeds(
            runtime_env=existing_env,
            process_env=process_env or dict(os.environ),
            host_config_path=host_config_path,
        )
    except Exception:  # noqa: BLE001
        return {}
    return {
        ptype: [
            {
                "redactedValue": _redact_seed_value(s),
                "source": s.source,
                "confidence": s.confidence,
                "field": s.field,
            }
            for s in bucket
        ]
        for ptype, bucket in seeds.items()
    }


def detect_plugin_install_root(openclaw_bin: str | None = None) -> Path | None:
    if package_layout() == "repo":
        return plugin_root().resolve()
    installed = detect_installed_plugin_root(openclaw_bin=openclaw_bin)
    if installed is not None:
        return installed
    return None


def ensure_plugin_install_root(*, setup_root_path: Path | None = None, dry_run: bool) -> tuple[Path | None, list[str], list[str]]:
    actions: list[str] = []
    warnings: list[str] = []
    expected_state_plugin_root = (
        setup_root_path.parent / "state" / "extensions" / PLUGIN_ID
        if setup_root_path is not None
        else None
    )
    hinted_root = resolve_plugin_install_root_hint(setup_root_path=setup_root_path)
    if hinted_root is not None:
        actions.append(f"reused hinted plugin install root {hinted_root}")
        return hinted_root, actions, warnings
    installed_root = detect_plugin_install_root()
    if installed_root is not None:
        return installed_root, actions, warnings
    if package_layout() != "package":
        return None, actions, warnings

    openclaw_binary = resolve_openclaw_binary()
    if not openclaw_binary:
        warnings.append("openclaw is not available in PATH; cannot auto-install the current package before setup.")
        return None, actions, warnings

    package_path = plugin_root().resolve()
    if dry_run:
        actions.append(f"would install plugin from current package path {package_path}")
        return package_path, actions, warnings

    completed = subprocess.run(
        build_openclaw_plugins_install_command(
            package_path,
            openclaw_bin=openclaw_binary,
            trusted_local_package=True,
        ),
        cwd=project_root(),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
        timeout=120,
    )
    if completed.returncode != 0:
        warnings.append("Auto-installing the current package into OpenClaw did not complete cleanly.")
        return None, actions, warnings
    actions.append(f"installed plugin from current package path {package_path}")
    detected_root: Path | None = None
    for attempt in range(6):
        detected_root = detect_plugin_install_root(openclaw_bin=openclaw_binary)
        if detected_root is not None:
            break
        if expected_state_plugin_root is not None and expected_state_plugin_root.exists():
            detected_root = expected_state_plugin_root.resolve()
            actions.append(
                f"reused detected plugin install root from state dir {detected_root}"
            )
            break
        if attempt < 5:
            time.sleep(0.5)
    return detected_root, actions, warnings


def build_plugin_entry(
    *,
    profile: str = "b",
    transport: str,
    sse_url: str | None,
    api_key_env: str | None,
    database_url: str | None,
    timeout_ms: int,
    connect_retries: int,
    connect_backoff_ms: int,
    runtime_env_file: Path | None = None,
    runtime_python_path: Path | None = None,
    runtime_root: Path | None = None,
    transport_diagnostics_path: Path | None = None,
    host_platform: str | None = None,
    env_values: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    normalized_profile = str(profile or "b").strip().lower() or "b"
    effective_timeout_ms = (
        max(int(timeout_ms), 60_000)
        if transport == "stdio" and normalized_profile in {"c", "d"}
        else int(timeout_ms)
    )
    llm_suite_enabled = False
    if normalized_profile == "d":
        llm_suite_enabled = True
    elif normalized_profile == "c" and isinstance(env_values, Mapping):
        llm_suite_enabled = all(
            str(env_values.get(key) or "").strip().lower() == "true"
            for key in (
                "WRITE_GUARD_LLM_ENABLED",
                "COMPACT_GIST_LLM_ENABLED",
                "INTENT_LLM_ENABLED",
            )
        )
    config: dict[str, Any] = {
        "transport": transport,
        "timeoutMs": effective_timeout_ms,
        "connection": {
            "connectRetries": connect_retries,
            "connectBackoffMs": connect_backoff_ms,
            "connectBackoffMaxMs": max(connect_backoff_ms, 1000),
            "requestRetries": max(connect_retries + 1, 2),
            "healthcheckTool": "index_status",
            "healthcheckTtlMs": 5000,
        },
        "observability": {
            "enabled": True,
            "transportDiagnosticsPath": str(
                transport_diagnostics_path or default_transport_diagnostics_path(default_setup_root())
            ),
            "maxRecentTransportEvents": 12,
        },
        "profileMemory": {
            "enabled": True,
            "injectBeforeAgentStart": True,
            "maxCharsPerBlock": 1200,
            "blocks": ["identity", "preferences", "workflow"],
        },
        "hostBridge": {
            "enabled": True,
            "importUserMd": True,
            "importMemoryMd": True,
            "importDailyMemory": True,
            "writeBackSummary": False,
        },
        "smartExtraction": {
            "enabled": llm_suite_enabled,
            "mode": "auto" if llm_suite_enabled else "disabled",
            "minConversationMessages": 2,
            "maxTranscriptChars": 8000,
            "timeoutMs": 8000,
            "retryAttempts": 2,
            "circuitBreakerFailures": 3,
            "circuitBreakerCooldownMs": 300000,
            "categories": [
                "profile",
                "preferences",
                "workflow",
                "entities",
                "events",
                "cases",
                "patterns",
                "reminders",
            ],
        },
        "reconcile": {
            "enabled": llm_suite_enabled,
            "profileMergePolicy": "always_merge",
            "eventMergePolicy": "append_only",
            "similarityThreshold": 0.70,
            "actions": ["ADD", "UPDATE", "NONE"],
        },
        "capturePipeline": {
            "mode": "v2",
            "captureAssistantDerived": normalized_profile != "a",
            "maxAssistantDerivedPerRun": 2,
            "pendingOnFailure": True,
        },
    }
    if transport == "sse":
        if not sse_url:
            raise SystemExit("--sse-url is required when --transport=sse")
        config["sse"] = {
            "url": sse_url,
            **({"apiKeyEnv": api_key_env} if api_key_env else {}),
        }
    else:
        env_block: dict[str, str] = {}
        if database_url:
            env_block["DATABASE_URL"] = database_url
        env_block.setdefault("PYTHONIOENCODING", "utf-8")
        env_block.setdefault("PYTHONUTF8", "1")
        if runtime_env_file is not None:
            env_block["OPENCLAW_MEMORY_PALACE_ENV_FILE"] = portable_path_string(runtime_env_file) or str(runtime_env_file)
        if runtime_python_path is not None:
            env_block["OPENCLAW_MEMORY_PALACE_RUNTIME_PYTHON"] = portable_path_string(runtime_python_path) or str(runtime_python_path)
        if runtime_root is not None:
            env_block["OPENCLAW_MEMORY_PALACE_RUNTIME_ROOT"] = portable_path_string(runtime_root) or str(runtime_root)
        if transport_diagnostics_path is not None:
            env_block["OPENCLAW_TRANSPORT_DIAGNOSTICS_PATH"] = portable_path_string(transport_diagnostics_path) or str(transport_diagnostics_path)
        stdio_command, stdio_args, stdio_cwd = build_default_stdio_launch(
            runtime_python_path=runtime_python_path,
            host_platform=host_platform,
        )
        config["stdio"] = {
            "command": stdio_command,
            "args": stdio_args,
            "cwd": stdio_cwd,
            **({"env": env_block} if env_block else {}),
        }
    return {
        "enabled": True,
        "config": config,
    }


def merge_openclaw_config(
    payload: dict[str, Any],
    *,
    entry_payload: dict[str, Any],
    activate: bool,
    plugin_install_root: Path | None = None,
) -> tuple[dict[str, Any], list[str]]:
    result = dict(payload)
    hooks = result.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise SystemExit("`hooks` must be a JSON object.")
    internal_hooks = hooks.setdefault("internal", {})
    if not isinstance(internal_hooks, dict):
        raise SystemExit("`hooks.internal` must be a JSON object.")
    internal_hooks["enabled"] = True
    plugins = result.setdefault("plugins", {})
    if not isinstance(plugins, dict):
        raise SystemExit("`plugins` must be a JSON object.")

    allow = plugins.get("allow")
    allow_list = allow if isinstance(allow, list) else []
    plugins["allow"] = dedupe_keep_order(
        [
            *(item for item in allow_list if isinstance(item, str)),
            PLUGIN_ID,
            MEMORY_CORE_COMPAT_PLUGIN_ID,
        ]
    )

    load = plugins.get("load")
    load_obj = load if isinstance(load, dict) else {}
    load_paths = load_obj.get("paths")
    install_root = str((plugin_install_root or plugin_root()).resolve())
    load_obj["paths"] = dedupe_keep_order(
        [*(item for item in load_paths if isinstance(item, str))] + [install_root]
        if isinstance(load_paths, list)
        else [install_root]
    )
    plugins["load"] = load_obj

    slots = plugins.get("slots")
    slots_obj = slots if isinstance(slots, dict) else {}
    if activate:
        slots_obj["memory"] = PLUGIN_ID
    plugins["slots"] = slots_obj

    entries = plugins.get("entries")
    entries_obj = entries if isinstance(entries, dict) else {}
    existing_entry = entries_obj.get(PLUGIN_ID)
    if existing_entry is not None and not isinstance(existing_entry, dict):
        raise SystemExit(f"`plugins.entries.{PLUGIN_ID}` must be a JSON object when present.")
    entries_obj[PLUGIN_ID] = deep_merge(existing_entry or {}, entry_payload)

    memory_core_entry = entries_obj.get(MEMORY_CORE_COMPAT_PLUGIN_ID)
    if memory_core_entry is not None and not isinstance(memory_core_entry, dict):
        raise SystemExit(
            f"`plugins.entries.{MEMORY_CORE_COMPAT_PLUGIN_ID}` must be a JSON object when present."
        )
    entries_obj[MEMORY_CORE_COMPAT_PLUGIN_ID] = deep_merge(
        memory_core_entry or {},
        {"enabled": True},
    )
    plugins["entries"] = entries_obj

    actions = [
        "enabled hooks.internal for plugin-managed internal events",
        "ensured plugins.allow contains memory-palace",
        "ensured plugins.allow contains memory-core for host facade compatibility",
        "ensured plugins.load.paths contains plugin install root",
        "ensured plugins.entries.memory-palace exists",
        "enabled plugins.entries.memory-core for host facade compatibility",
    ]
    if activate:
        actions.append("set plugins.slots.memory to memory-palace")

    return result, actions


def build_report_snapshot(payload: dict[str, Any]) -> dict[str, Any]:
    plugins = payload.get("plugins")
    plugins_obj = plugins if isinstance(plugins, dict) else {}
    load = plugins_obj.get("load")
    load_obj = load if isinstance(load, dict) else {}
    slots = plugins_obj.get("slots")
    slots_obj = slots if isinstance(slots, dict) else {}
    entries = plugins_obj.get("entries")
    entries_obj = entries if isinstance(entries, dict) else {}
    plugin_entry = entries_obj.get(PLUGIN_ID)
    return {
        "plugins": {
            "allow": plugins_obj.get("allow"),
            "load": {
                "paths": load_obj.get("paths"),
            },
            "slots": {
                "memory": slots_obj.get("memory"),
            },
            "entries": {
                PLUGIN_ID: plugin_entry,
            },
        }
    }


def _config_path_matches_plugin_root(path_value: str, expected_plugin_root: Path) -> bool:
    rendered = str(path_value or "").strip()
    if not rendered:
        return False
    normalized_expected = str(expected_plugin_root).replace("\\", "/").rstrip("/")
    try:
        candidate = Path(rendered).expanduser().resolve()
        normalized_candidate = str(candidate).replace("\\", "/").rstrip("/")
        if normalized_candidate == normalized_expected:
            return True
    except Exception:
        normalized_candidate = rendered.replace("\\", "/").rstrip("/")
        if normalized_candidate == normalized_expected:
            return True
    return rendered.replace("\\", "/").rstrip("/").endswith(f"/{PLUGIN_ID}")


def inspect_host_wiring_state(
    *,
    config_path: Path,
    plugin_install_root: Path | None = None,
) -> dict[str, Any]:
    payload = read_json_file(config_path)
    plugins = payload.get("plugins") if isinstance(payload.get("plugins"), dict) else {}
    allow_list = plugins.get("allow") if isinstance(plugins.get("allow"), list) else []
    load_obj = plugins.get("load") if isinstance(plugins.get("load"), dict) else {}
    load_paths = load_obj.get("paths") if isinstance(load_obj.get("paths"), list) else []
    slots_obj = plugins.get("slots") if isinstance(plugins.get("slots"), dict) else {}
    entries_obj = plugins.get("entries") if isinstance(plugins.get("entries"), dict) else {}
    expected_plugin_root = (plugin_install_root or detect_plugin_install_root() or plugin_root()).resolve()

    allow_contains_plugin = any(str(item or "").strip() == PLUGIN_ID for item in allow_list)
    load_path_present = any(
        isinstance(item, str) and _config_path_matches_plugin_root(item, expected_plugin_root)
        for item in load_paths
    )
    entry_present = isinstance(entries_obj.get(PLUGIN_ID), dict)
    memory_slot_active = str(slots_obj.get("memory") or "").strip() == PLUGIN_ID
    host_wiring_ready = (
        allow_contains_plugin and load_path_present and entry_present and memory_slot_active
    )
    return {
        "allowContainsPlugin": allow_contains_plugin,
        "loadPathPresent": load_path_present,
        "entryPresent": entry_present,
        "memorySlotActive": memory_slot_active,
        "hostWiringReady": host_wiring_ready,
    }


def collect_install_checks(
    *,
    config_path: Path,
    config_path_source: str,
    transport: str,
    sse_url: str | None,
    api_key_env: str | None,
    database_url: str | None,
    plugin_path: Path | None = None,
    runtime_python_path: Path | None = None,
    backend_python_path: Path | None = None,
    runtime_env_file: Path | None = None,
    openclaw_bin: str | None = None,
    host_platform: str | None = None,
    env_values: Mapping[str, str] | None = None,
    requested_profile: str | None = None,
    effective_profile: str | None = None,
    provider_probe: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    platform_name = host_platform_name(host_platform)
    resolved_env_values = dict(env_values or {})
    resolved_plugin_path = plugin_path or detect_plugin_install_root(openclaw_bin=openclaw_bin) or plugin_root()
    resolved_runtime_python = (
        runtime_python_path
        or backend_python_path
        or default_runtime_python_path(default_setup_root(), host_platform=platform_name)
    )
    resolved_runtime_env = runtime_env_file or default_runtime_env_path(default_setup_root())
    resolved_openclaw_bin = resolve_openclaw_binary(openclaw_bin)
    resolved_openclaw_version = detect_openclaw_version(resolved_openclaw_bin)
    stdio_launcher_path = windows_stdio_wrapper() if platform_name == "windows" else stdio_wrapper()
    stdio_launcher_label = "Windows MCP wrapper" if platform_name == "windows" else "stdio shell wrapper"
    skill_root = resolved_plugin_path.parent / "skills" if resolved_plugin_path.name == "dist" else resolved_plugin_path / "skills"
    bundled_skill_files = [
        skill_root / "memory-palace-openclaw" / "SKILL.md",
        skill_root / "memory-palace-openclaw-onboarding" / "SKILL.md",
    ]
    bundled_skill_present = all(path.is_file() for path in bundled_skill_files)

    checks: list[dict[str, Any]] = [
        {
            "id": "config-path",
            "status": "PASS",
            "message": f"Using OpenClaw config path from {config_path_source}.",
            "details": {
                "path": str(config_path),
                "exists": config_path.exists(),
            },
        },
        {
            "id": "plugin-load-path",
            "status": "PASS",
            "message": "plugins.load.paths will store the resolved plugin install root.",
            "details": str(resolved_plugin_path),
        },
        {
            "id": "bundled-skill",
            "status": "PASS" if bundled_skill_present else "WARN",
            "message": (
                "Plugin-bundled OpenClaw skills are present."
                if bundled_skill_present
                else "One or more plugin-bundled OpenClaw skills are missing."
            ),
            **(
                {}
                if bundled_skill_present
                else {"action": "Repack or reinstall the plugin so `skills/memory-palace-openclaw/` and `skills/memory-palace-openclaw-onboarding/` are shipped."}
            ),
        },
        {
            "id": "openclaw-bin",
            "status": "PASS" if resolved_openclaw_bin else "WARN",
            "message": "Detected `openclaw` in PATH." if resolved_openclaw_bin else "`openclaw` was not found in PATH.",
            **(
                {}
                if resolved_openclaw_bin
                else {"action": "Install OpenClaw or ensure the `openclaw` binary is in PATH before running verify/doctor/smoke."}
            ),
        },
        {
            "id": "openclaw-version",
            "status": (
                "PASS"
                if resolved_openclaw_version and resolved_openclaw_version.get("meets_minimum")
                else "WARN"
            ),
            "message": (
                f"Detected OpenClaw {resolved_openclaw_version['version']}; hook-capable host requirement is >= {MIN_OPENCLAW_VERSION_TEXT}."
                if resolved_openclaw_version and resolved_openclaw_version.get("version")
                else f"Could not determine the installed OpenClaw version; automatic recall/capture expects a hook-capable host >= {MIN_OPENCLAW_VERSION_TEXT}."
            ),
            **(
                {}
                if resolved_openclaw_version and resolved_openclaw_version.get("meets_minimum")
                else {
                    "action": f"Upgrade OpenClaw to >= {MIN_OPENCLAW_VERSION_TEXT} before relying on automatic recall/capture/visual-harvest hooks."
                }
            ),
            **(
                {
                    "details": {
                        "required": MIN_OPENCLAW_VERSION_TEXT,
                        "detected": resolved_openclaw_version.get("version") or None,
                        "raw": resolved_openclaw_version.get("raw") or None,
                    }
                }
                if resolved_openclaw_version
                else {"details": {"required": MIN_OPENCLAW_VERSION_TEXT, "detected": None}}
            ),
        },
    ]

    if transport == "stdio":
        checks.extend(
            [
                {
                    "id": "stdio-wrapper",
                    "status": "PASS" if stdio_launcher_path.exists() else "WARN",
                    "message": (
                        f"The installer will wire stdio through the {stdio_launcher_label}."
                        if stdio_launcher_path.exists()
                        else f"The {stdio_launcher_label} is missing."
                    ),
                    **(
                        {}
                        if stdio_launcher_path.exists()
                        else {"action": f"Ensure {stdio_launcher_path} exists before using stdio transport."}
                    ),
                },
                {
                    "id": "runtime-env-file",
                    "status": "PASS" if resolved_runtime_env.exists() else "WARN",
                    "message": (
                        "Runtime env file already exists."
                        if resolved_runtime_env.exists()
                        else "Runtime env file will be created on setup."
                    ),
                    "details": str(resolved_runtime_env),
                },
                {
                    "id": "backend-venv",
                    "status": "PASS" if resolved_runtime_python.exists() else "WARN",
                    "message": (
                        "User-state runtime python is available."
                        if resolved_runtime_python.exists()
                        else "User-state runtime python is not bootstrapped yet."
                    ),
                    **(
                        {}
                        if resolved_runtime_python.exists()
                        else {"action": "Run setup without --dry-run to create the dedicated runtime venv."}
                    ),
                },
                {
                    "id": "database-url",
                    "status": "PASS" if database_url else "WARN",
                    "message": (
                        "DATABASE_URL will be injected via the runtime env file."
                        if database_url
                        else "DATABASE_URL was not provided; setup will use the user-state default database path."
                    ),
                },
            ]
        )
    else:
        checks.extend(
            [
                {
                    "id": "sse-url",
                    "status": "PASS" if sse_url else "WARN",
                    "message": "SSE endpoint will be written into the plugin config." if sse_url else "SSE endpoint is missing.",
                    **({} if sse_url else {"action": "Provide --sse-url before using sse transport."}),
                },
                {
                    "id": "sse-api-key-env",
                    "status": "PASS" if api_key_env and os.getenv(api_key_env) else "WARN",
                    "message": (
                        f"{api_key_env} is available in the current shell."
                        if api_key_env and os.getenv(api_key_env)
                        else f"{api_key_env or 'MCP_API_KEY'} is not set in the current shell."
                    ),
                    **(
                        {}
                        if api_key_env and os.getenv(api_key_env)
                        else {"action": f"Export {api_key_env or 'MCP_API_KEY'} before running verify/doctor/smoke against SSE."}
                    ),
                },
            ]
        )

    checks.extend(
        build_provider_install_checks(
            env_values=resolved_env_values,
            setup_root_path=default_setup_root() if runtime_env_file is None else runtime_env_file.parent,
            requested_profile=str(
                requested_profile
                or resolved_env_values.get(_metadata_key("PROFILE_REQUESTED"))
                or resolved_env_values.get(_metadata_key("PROFILE_EFFECTIVE"))
                or "b"
            ),
            effective_profile=str(
                effective_profile
                or resolved_env_values.get(_metadata_key("PROFILE_EFFECTIVE"))
                or requested_profile
                or "b"
            ),
            provider_probe=provider_probe,
        )
    )

    return checks


def build_next_steps(
    *,
    config_path: Path,
    transport: str,
    dry_run: bool,
    host_platform: str | None = None,
) -> list[str]:
    installer_command = (
        repo_python_command("scripts/openclaw_memory_palace_installer.py --dry-run --json", host_platform=host_platform)
        if dry_run
        else "openclaw memory-palace status --json"
    )
    return [
        render_env_prefixed_command(
            "openclaw plugins inspect memory-palace --json",
            config_path=config_path,
            host_platform=host_platform,
        ),
        installer_command,
        "openclaw memory-palace verify --json",
        "openclaw memory-palace doctor --json",
        "openclaw memory-palace smoke --json",
        *(
            ["Export your SSE API key env before running the commands above."]
            if transport == "sse"
            else []
        ),
    ]


def dashboard_cli_command(*, host_platform: str | None = None) -> str:
    if package_layout() == "package":
        return "memory-palace-openclaw dashboard"
    return repo_python_command("scripts/openclaw_memory_palace.py dashboard", host_platform=host_platform)


def packaged_dashboard_bundle_dir(frontend_dir: Path) -> Path | None:
    candidate = frontend_dir / "dist"
    if package_layout() == "package" and (candidate / "index.html").is_file():
        return candidate
    return None


def build_frontend_release_bundle() -> list[str]:
    frontend_dir = frontend_root()
    package_json = frontend_dir / "package.json"
    dist_index = frontend_dir / "dist" / "index.html"
    if not frontend_dir.is_dir() or not package_json.is_file():
        return []
    if dist_index.is_file():
        return [f"reused existing frontend static bundle at {dist_index.parent}"]

    if (frontend_dir / "pnpm-lock.yaml").is_file() and shutil.which("pnpm"):
        build_command = [shutil.which("pnpm") or "pnpm", "build"]
    elif shutil.which("npm"):
        build_command = [shutil.which("npm") or "npm", "run", "build"]
    else:
        raise RuntimeError(
            "Could not build the frontend release bundle because neither pnpm nor npm is available in PATH."
        )

    completed = subprocess.run(
        build_command,
        cwd=frontend_dir,
        text=True,
        capture_output=True,
        check=False,
        timeout=900,
    )
    if completed.returncode != 0 or not dist_index.is_file():
        raise RuntimeError(
            "Frontend release bundle build failed.\n"
            f"COMMAND: {' '.join(build_command)}\n"
            f"STDOUT:\n{completed.stdout}\n"
            f"STDERR:\n{completed.stderr}"
        )
    return [f"built frontend static bundle via `{' '.join(build_command)}`"]


def ensure_frontend_dashboard(
    *,
    setup_root_path: Path,
    env_values: Mapping[str, str] | None = None,
    runtime_python_path: Path | None = None,
    dry_run: bool,
) -> tuple[dict[str, Any], list[str], list[str]]:
    settings = resolve_stack_runtime_settings(setup_root_path=setup_root_path, env_values=env_values)
    dashboard_settings = settings["dashboard"]
    backend_settings = settings["backendApi"]
    dashboard = inspect_dashboard_state(setup_root_path=setup_root_path, env_values=env_values)
    actions: list[str] = []
    warnings: list[str] = []
    frontend_dir = Path(dashboard["frontendRoot"])

    dashboard["enabled"] = True
    dashboard["dependenciesInstalled"] = False
    dashboard["deliveryMode"] = "vite_dev_server"
    dashboard["installsDependenciesAtRuntime"] = True

    if not frontend_dir.is_dir():
        dashboard["status"] = "missing"
        warnings.append("当前安装模式请求 dashboard，但发布包中未找到 frontend 目录。")
        return dashboard, actions, warnings

    static_bundle_dir = frontend_dir / "dist"
    static_server_script = scripts_root() / "serve_memory_palace_dashboard.py"
    static_python = str(runtime_python_path or shutil.which("python3") or sys.executable)
    use_static_bundle = (
        package_layout() == "package"
        and static_bundle_dir.is_dir()
        and (static_bundle_dir / "index.html").is_file()
        and static_server_script.is_file()
    )

    if use_static_bundle:
        start_command = [
            static_python,
            str(static_server_script),
            "--host",
            str(dashboard_settings["host"]),
            "--port",
            str(dashboard_settings["port"]),
            "--root",
            str(static_bundle_dir),
            "--api-target",
            str(backend_settings["url"]),
            "--sse-target",
            str(backend_settings["url"]),
        ]
        dashboard["deliveryMode"] = "static_bundle"
        dashboard["installsDependenciesAtRuntime"] = False
        dashboard["dependenciesInstalled"] = True
        dashboard["command"] = " ".join(shlex.quote(part) for part in start_command)
    else:
        npm_bin = shutil.which("npm")
        if not npm_bin:
            dashboard["status"] = "missing_npm"
            warnings.append("当前安装模式请求 dashboard，但 PATH 中未找到 npm。")
            return dashboard, actions, warnings

        node_modules_dir = frontend_dir / "node_modules"
        install_command = [
            npm_bin,
            "ci" if (frontend_dir / "package-lock.json").is_file() else "install",
            "--no-audit",
            "--no-fund",
            "--prefer-offline",
            "--progress=false",
        ]
        dashboard["command"] = (
            f"cd {shlex.quote(str(frontend_dir))} && npm run dev -- --host {dashboard_settings['host']} --port {dashboard_settings['port']} --strictPort"
        )
    if dry_run:
        if use_static_bundle:
            actions.append(f"would start packaged static dashboard at {dashboard['url']}")
        else:
            actions.append(f"would install dashboard dependencies via `{' '.join(install_command)}`")
            actions.append(f"would start dashboard Vite dev server at {dashboard['url']}")
            dashboard["dependenciesInstalled"] = node_modules_dir.is_dir()
        dashboard["status"] = "dry_run"
        dashboard["running"] = False
        return dashboard, actions, warnings

    if not use_static_bundle:
        if not node_modules_dir.is_dir():
            install_env = dict(os.environ)
            install_env.setdefault("npm_config_audit", "false")
            install_env.setdefault("npm_config_fund", "false")
            install_env.setdefault("npm_config_progress", "false")
            install_env.setdefault("npm_config_fetch_retries", "5")
            install_env.setdefault("npm_config_fetch_retry_mintimeout", "1000")
            install_env.setdefault("npm_config_fetch_retry_maxtimeout", "120000")
            install_env.setdefault("npm_config_prefer_offline", "true")

            completed: subprocess.CompletedProcess[str] | None = None
            for attempt in range(1, DASHBOARD_DEPENDENCY_INSTALL_RETRIES + 1):
                completed = subprocess.run(
                    install_command,
                    cwd=frontend_dir,
                    env=install_env,
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=DASHBOARD_DEPENDENCY_INSTALL_TIMEOUT_SECONDS,
                )
                if completed.returncode == 0:
                    break
                if attempt < DASHBOARD_DEPENDENCY_INSTALL_RETRIES:
                    time.sleep(min(2 ** (attempt - 1), 5))

            if completed is None or completed.returncode != 0:
                dashboard["status"] = "install_failed"
                warnings.append(
                    "dashboard 依赖安装失败；基础安装已完成，但 full/dev 页面暂不可用。"
                )
                dashboard["installError"] = (completed.stderr or completed.stdout or "").strip()[-2000:]
                return dashboard, actions, warnings
            dashboard["dependenciesInstalled"] = True
            actions.append(f"installed dashboard dependencies via `{' '.join(install_command)}`")
        else:
            dashboard["dependenciesInstalled"] = True
            actions.append("reused existing dashboard dependencies")

        vite_dir = frontend_dir / "node_modules" / ".bin"
        vite_binary = next(
            (
                candidate
                for candidate in (
                    vite_dir / ("vite.cmd" if os.name == "nt" else "vite"),
                    vite_dir / "vite",
                    vite_dir / "vite.cmd",
                )
                if candidate.is_file()
            ),
            None,
        )
        if vite_binary is not None:
            start_command = [
                str(vite_binary),
                "--host",
                dashboard_settings["host"],
                "--port",
                str(dashboard_settings["port"]),
                "--strictPort",
            ]
        else:
            start_command = [
                npm_bin,
                "run",
                "dev",
                "--",
                "--host",
                dashboard_settings["host"],
                "--port",
                str(dashboard_settings["port"]),
                "--strictPort",
            ]

    if dashboard["running"]:
        dashboard["status"] = "running"
        actions.append(f"dashboard already reachable at {dashboard['url']}")
        return dashboard, actions, warnings

    if _port_open(dashboard_settings["host"], dashboard_settings["port"]) and _dashboard_service_ready(dashboard["url"]):
        dashboard["running"] = True
        dashboard["reachable"] = True
        dashboard["serviceReady"] = True
        dashboard["status"] = "running_external"
        actions.append(f"dashboard already reachable at {dashboard['url']}")
        return dashboard, actions, warnings
    if _port_open(dashboard_settings["host"], dashboard_settings["port"]):
        dashboard["status"] = "port_in_use"
        suggested_port = (
            _find_available_loopback_port(int(dashboard_settings["port"]))
            if is_loopback_host(str(dashboard_settings["host"]))
            else None
        )
        warnings.append(
            (
                f"dashboard 端口 {dashboard_settings['port']} 已被其他服务占用，未自动启动新的 dashboard 进程。"
                + (
                    f" 可改用 `--dashboard-port {suggested_port}`。"
                    if suggested_port is not None
                    else ""
                )
            )
        )
        return dashboard, actions, warnings

    log_path = Path(dashboard["logFile"])
    log_path.parent.mkdir(parents=True, exist_ok=True)
    launch_env = dict(os.environ)
    launch_env["MEMORY_PALACE_API_PROXY_TARGET"] = backend_settings["url"]
    launch_env["MEMORY_PALACE_SSE_PROXY_TARGET"] = backend_settings["url"]
    with log_path.open("ab") as log_stream:
        process = subprocess.Popen(
            start_command,
            cwd=frontend_dir,
            env=launch_env,
            stdout=log_stream,
            stderr=log_stream,
            **background_process_popen_kwargs(),
        )
    pid_path = default_dashboard_pid_path(setup_root_path)
    _write_pid_file(
        pid_path,
        process.pid,
        dry_run=False,
        record=_build_pid_file_record(
            pid=process.pid,
            component="dashboard",
            command=start_command,
            cwd=frontend_dir,
        ),
    )
    if use_static_bundle:
        actions.append(f"started packaged static dashboard at {dashboard['url']}")
    else:
        actions.append(f"started dashboard Vite dev server at {dashboard['url']}")

    if wait_for_dashboard_ready(
        dashboard_settings["host"],
        dashboard_settings["port"],
        dashboard["url"],
        timeout_seconds=DASHBOARD_START_TIMEOUT_SECONDS,
    ):
        dashboard["running"] = True
        dashboard["reachable"] = True
        dashboard["serviceReady"] = True
        dashboard["status"] = "running"
        dashboard["pid"] = process.pid
        return dashboard, actions, warnings

    dashboard["running"] = False
    dashboard["status"] = "start_timeout"
    _cleanup_timed_out_process(
        pid=process.pid,
        pid_path=pid_path,
        component_label="dashboard",
        actions=actions,
        warnings=warnings,
    )
    warnings.append(
        "dashboard 依赖已安装，但启动超时；基础安装已完成，可稍后用 next step 手动检查前端日志。"
    )
    return dashboard, actions, warnings


def ensure_backend_http_api(
    *,
    setup_root_path: Path,
    runtime_python_path: Path,
    runtime_env_file: Path,
    env_values: Mapping[str, str] | None = None,
    dry_run: bool,
) -> tuple[dict[str, Any], list[str], list[str]]:
    settings = resolve_stack_runtime_settings(setup_root_path=setup_root_path, env_values=env_values)
    backend_settings = settings["backendApi"]
    backend_api = inspect_backend_api_state(setup_root_path=setup_root_path, env_values=env_values)
    actions: list[str] = []
    warnings: list[str] = []
    backend_dir = backend_root()

    backend_api["enabled"] = True
    backend_api["command"] = " ".join(
        shlex.quote(part)
        for part in build_backend_api_command(
            runtime_python_path=runtime_python_path,
            host=backend_settings["host"],
            port=backend_settings["port"],
        )
    )

    if not backend_dir.is_dir():
        backend_api["status"] = "missing"
        warnings.append("当前安装模式请求 dashboard/full stack，但发布包中未找到 backend 目录。")
        return backend_api, actions, warnings

    if dry_run:
        actions.append(f"would start backend HTTP API at {backend_api['url']}")
        backend_api["status"] = "dry_run"
        return backend_api, actions, warnings

    if not runtime_python_path.exists():
        backend_api["status"] = "missing_runtime"
        warnings.append("当前安装模式请求 dashboard/full stack，但 runtime Python 还未就绪。")
        return backend_api, actions, warnings

    if not runtime_env_file.is_file():
        backend_api["status"] = "missing_env"
        warnings.append("当前安装模式请求 dashboard/full stack，但 bootstrap env 文件还不存在；请先完成 setup。")
        return backend_api, actions, warnings

    if backend_api["running"]:
        actions.append(f"backend HTTP API already reachable at {backend_api['url']}")
        return backend_api, actions, warnings

    if _port_open(backend_settings["host"], backend_settings["port"]) and _backend_api_service_ready(backend_api["url"]):
        backend_api["running"] = True
        backend_api["reachable"] = True
        backend_api["serviceReady"] = True
        backend_api["status"] = "running_external"
        actions.append(f"backend HTTP API already reachable at {backend_api['url']}")
        return backend_api, actions, warnings
    if _port_open(backend_settings["host"], backend_settings["port"]):
        backend_api["status"] = "port_in_use"
        suggested_port = (
            _find_available_loopback_port(int(backend_settings["port"]))
            if is_loopback_host(str(backend_settings["host"]))
            else None
        )
        warnings.append(
            (
                f"backend HTTP API 端口 {backend_settings['port']} 已被其他服务占用，未自动启动新的 backend API 进程。"
                + (
                    f" 可改用 `--backend-api-port {suggested_port}`。"
                    if suggested_port is not None
                    else ""
                )
            )
        )
        return backend_api, actions, warnings

    launch_env = dict(os.environ)
    launch_env.update(load_env_file(runtime_env_file))
    launch_env["OPENCLAW_MEMORY_PALACE_ENV_FILE"] = str(runtime_env_file)
    configured_config_path = str(
        (env_values or {}).get(_metadata_key("CONFIG_PATH"))
        or launch_env.get(_metadata_key("CONFIG_PATH"))
        or ""
    ).strip()
    if configured_config_path and not str(launch_env.get("OPENCLAW_CONFIG_PATH") or "").strip():
        launch_env["OPENCLAW_CONFIG_PATH"] = configured_config_path
    log_path = Path(backend_api["logFile"])
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as log_stream:
        process = subprocess.Popen(
            build_backend_api_command(
                runtime_python_path=runtime_python_path,
                host=backend_settings["host"],
                port=backend_settings["port"],
            ),
            cwd=backend_dir,
            env=launch_env,
            stdout=log_stream,
            stderr=log_stream,
            **background_process_popen_kwargs(),
        )
    pid_path = default_backend_api_pid_path(setup_root_path)
    _write_pid_file(
        pid_path,
        process.pid,
        dry_run=False,
        record=_build_pid_file_record(
            pid=process.pid,
            component="backend_api",
            command=build_backend_api_command(
                runtime_python_path=runtime_python_path,
                host=backend_settings["host"],
                port=backend_settings["port"],
            ),
            cwd=backend_dir,
        ),
    )
    actions.append(f"started backend HTTP API at {backend_api['url']}")

    if wait_for_backend_api_ready(
        backend_settings["host"],
        backend_settings["port"],
        backend_api["url"],
        timeout_seconds=BACKEND_API_START_TIMEOUT_SECONDS,
    ):
        backend_api["running"] = True
        backend_api["reachable"] = True
        backend_api["serviceReady"] = True
        backend_api["status"] = "running"
        backend_api["pid"] = process.pid
        return backend_api, actions, warnings

    backend_api["running"] = False
    backend_api["status"] = "start_timeout"
    _cleanup_timed_out_process(
        pid=process.pid,
        pid_path=pid_path,
        component_label="backend HTTP API",
        actions=actions,
        warnings=warnings,
    )
    warnings.append(
        "backend HTTP API 启动超时；full/dev 只完成了基础安装，dashboard stack 尚未完全可用。"
    )
    return backend_api, actions, warnings


def dashboard_status(*, setup_root_value: str | None = None) -> dict[str, Any]:
    setup_root_path = Path(setup_root_value).expanduser().resolve() if setup_root_value else default_setup_root()
    dashboard = inspect_dashboard_state(setup_root_path=setup_root_path)
    backend_api = inspect_backend_api_state(setup_root_path=setup_root_path)
    return {
        "ok": True,
        "summary": f"Dashboard status: {dashboard['status']} / backend API: {backend_api['status']}.",
        "setup_root": str(setup_root_path),
        "dashboard": dashboard,
        "backendApi": backend_api,
    }


def dashboard_start(
    *,
    setup_root_value: str | None = None,
    dashboard_host: str | None = None,
    dashboard_port: str | int | None = None,
    backend_api_host: str | None = None,
    backend_api_port: str | int | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    setup_root_path = Path(setup_root_value).expanduser().resolve() if setup_root_value else default_setup_root()
    runtime_env_file = default_runtime_env_path(setup_root_path)
    if not runtime_env_file.exists():
        raise RuntimeError("Bootstrap runtime env is missing. Run `memory-palace-openclaw setup` first.")
    env_values = load_env_file(runtime_env_file)
    apply_stack_runtime_overrides(
        env_values,
        dashboard_host=dashboard_host,
        dashboard_port=dashboard_port,
        backend_api_host=backend_api_host,
        backend_api_port=backend_api_port,
    )
    write_env_file(runtime_env_file, env_values, dry_run=dry_run)
    runtime_python_path, runtime_actions = ensure_runtime_venv(
        setup_root_path=setup_root_path,
        dry_run=dry_run,
    )
    backend_api, backend_actions, warnings = ensure_backend_http_api(
        setup_root_path=setup_root_path,
        runtime_python_path=runtime_python_path,
        runtime_env_file=runtime_env_file,
        env_values=env_values,
        dry_run=dry_run,
    )
    dashboard_actions: list[str] = []
    dashboard_warnings: list[str] = []
    dashboard = inspect_dashboard_state(setup_root_path=setup_root_path, env_values=env_values)
    if backend_api.get("status") in {"running", "running_external", "dry_run"}:
        dashboard, dashboard_actions, dashboard_warnings = ensure_frontend_dashboard(
            setup_root_path=setup_root_path,
            env_values=env_values,
            runtime_python_path=runtime_python_path,
            dry_run=dry_run,
        )
    else:
        dashboard["enabled"] = True
        dashboard["status"] = "blocked_by_backend_api"
        dashboard_warnings.append("backend HTTP API 未就绪，已跳过 dashboard 启动。")
    return {
        "ok": True,
        "summary": f"Dashboard start handled with status={dashboard.get('status', 'unknown')}.",
        "setup_root": str(setup_root_path),
        "dashboard": dashboard,
        "backendApi": backend_api,
        "actions": [*runtime_actions, *backend_actions, *dashboard_actions],
        "warnings": [*warnings, *dashboard_warnings],
        "dry_run": dry_run,
    }


def dashboard_stop(
    *,
    setup_root_value: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    setup_root_path = Path(setup_root_value).expanduser().resolve() if setup_root_value else default_setup_root()
    env_values = load_env_file(default_runtime_env_path(setup_root_path))
    dashboard = inspect_dashboard_state(setup_root_path=setup_root_path, env_values=env_values)
    backend_api = inspect_backend_api_state(setup_root_path=setup_root_path, env_values=env_values)
    actions: list[str] = []
    warnings: list[str] = []
    dashboard_pid_path = default_dashboard_pid_path(setup_root_path)
    dashboard_record = _read_pid_file_record(dashboard_pid_path)
    pid = int(dashboard_record.get("pid") or 0) if dashboard_record else None
    if pid and _is_process_alive(pid):
        if not _pid_file_record_matches_running_process(dashboard_record):
            warnings.append(
                "dashboard pid 文件指向的进程与当前受管 dashboard 不匹配；已跳过停止，避免误杀其他进程。"
            )
            _remove_file_if_exists(dashboard_pid_path, dry_run=dry_run)
        elif dry_run:
            actions.append(f"would stop dashboard process pid={pid}")
        elif _terminate_process(
            pid,
            process_group_id=_read_optional_int(dashboard_record.get("process_group_id")),
        ):
            actions.append(f"stopped dashboard process pid={pid}")
            if not _wait_for_port_closed(str(dashboard.get("host") or ""), int(dashboard.get("port") or 0)):
                warnings.append("dashboard 进程已发送停止信号，但端口尚未释放；请稍后再查状态。")
            _remove_file_if_exists(dashboard_pid_path, dry_run=dry_run)
        else:
            warnings.append(f"dashboard pid={pid} could not be terminated cleanly.")
            _remove_file_if_exists(dashboard_pid_path, dry_run=dry_run)
    elif dashboard_record:
        actions.append("cleared stale dashboard pid file")
        _remove_file_if_exists(dashboard_pid_path, dry_run=dry_run)
    elif dashboard.get("reachable"):
        warnings.append("dashboard 当前可访问，但不是由本工具管理的进程；未执行停止。")
    else:
        actions.append("dashboard was already stopped")

    backend_pid_path = default_backend_api_pid_path(setup_root_path)
    backend_record = _read_pid_file_record(backend_pid_path)
    backend_pid = int(backend_record.get("pid") or 0) if backend_record else None
    if backend_pid and _is_process_alive(backend_pid):
        if not _pid_file_record_matches_running_process(backend_record):
            warnings.append(
                "backend API pid 文件指向的进程与当前受管 backend 不匹配；已跳过停止，避免误杀其他进程。"
            )
            _remove_file_if_exists(backend_pid_path, dry_run=dry_run)
        elif dry_run:
            actions.append(f"would stop backend API process pid={backend_pid}")
        elif _terminate_process(
            backend_pid,
            process_group_id=_read_optional_int(backend_record.get("process_group_id")),
        ):
            actions.append(f"stopped backend API process pid={backend_pid}")
            if not _wait_for_port_closed(str(backend_api.get("host") or ""), int(backend_api.get("port") or 0)):
                warnings.append("backend API 进程已发送停止信号，但端口尚未释放；请稍后再查状态。")
            _remove_file_if_exists(backend_pid_path, dry_run=dry_run)
        else:
            warnings.append(f"backend API pid={backend_pid} could not be terminated cleanly.")
            _remove_file_if_exists(backend_pid_path, dry_run=dry_run)
    elif backend_record:
        actions.append("cleared stale backend API pid file")
        _remove_file_if_exists(backend_pid_path, dry_run=dry_run)
    elif backend_api.get("reachable"):
        warnings.append("backend HTTP API 当前可访问，但不是由本工具管理的进程；未执行停止。")
    else:
        actions.append("backend API was already stopped")

    return {
        "ok": True,
        "summary": "Dashboard stop handled.",
        "setup_root": str(setup_root_path),
        "dashboard": inspect_dashboard_state(setup_root_path=setup_root_path, env_values=env_values),
        "backendApi": inspect_backend_api_state(setup_root_path=setup_root_path, env_values=env_values),
        "actions": actions,
        "warnings": warnings,
        "dry_run": dry_run,
    }


def remove_plugin_from_openclaw_config(
    payload: dict[str, Any],
    *,
    plugin_install_root: Path | None = None,
    restore_memory_slot: str | None = None,
) -> tuple[dict[str, Any], list[str]]:
    result = dict(payload)
    actions: list[str] = []

    plugins = result.get("plugins")
    if not isinstance(plugins, dict):
        return result, actions

    allow = plugins.get("allow")
    if isinstance(allow, list):
        new_allow = [
            item
            for item in allow
            if item not in {PLUGIN_ID, MEMORY_CORE_COMPAT_PLUGIN_ID}
        ]
        if new_allow != allow:
            plugins["allow"] = new_allow
            actions.append("removed memory-palace from plugins.allow")
            if MEMORY_CORE_COMPAT_PLUGIN_ID in allow:
                actions.append("removed memory-core from plugins.allow")

    load = plugins.get("load")
    if isinstance(load, dict):
        load_paths = load.get("paths")
        install_root_candidates = path_match_candidates(plugin_install_root or plugin_root())
        if isinstance(load_paths, list):
            new_paths = [
                item
                for item in load_paths
                if normalize_path_text(item) not in install_root_candidates
            ]
            if new_paths != load_paths:
                load["paths"] = new_paths
                actions.append("removed plugin install root from plugins.load.paths")

    slots = plugins.get("slots")
    if isinstance(slots, dict) and slots.get("memory") == PLUGIN_ID:
        if restore_memory_slot:
            slots["memory"] = restore_memory_slot
            actions.append(f"restored plugins.slots.memory to {restore_memory_slot}")
        else:
            slots.pop("memory", None)
            actions.append("removed plugins.slots.memory binding for memory-palace")

    entries = plugins.get("entries")
    if isinstance(entries, dict):
        if PLUGIN_ID in entries:
            entries.pop(PLUGIN_ID, None)
            actions.append("removed plugins.entries.memory-palace")
        if MEMORY_CORE_COMPAT_PLUGIN_ID in entries:
            entries.pop(MEMORY_CORE_COMPAT_PLUGIN_ID, None)
            actions.append("removed plugins.entries.memory-core")

    return result, actions


def perform_uninstall(
    *,
    config: str | None = None,
    setup_root_value: str | None = None,
    openclaw_bin: str | None = None,
    keep_files: bool = False,
    remove_runtime: bool = False,
    force: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    config_path, config_path_source = detect_config_path_with_source(config)
    setup_root_path = Path(setup_root_value).expanduser().resolve() if setup_root_value else default_setup_root()
    env_values = load_env_file(default_runtime_env_path(setup_root_path))
    warnings: list[str] = []
    actions: list[str] = []
    plugin_install_root = detect_plugin_install_root(openclaw_bin=openclaw_bin)
    restore_memory_slot = str(env_values.get(_metadata_key("PREVIOUS_MEMORY_SLOT")) or "").strip() or None
    config_backup_path = backup_config_file(config_path, label="memory-palace-uninstall", dry_run=dry_run)

    resolved_openclaw = openclaw_bin or shutil.which("openclaw")
    if resolved_openclaw:
        command = [resolved_openclaw, "plugins", "uninstall", PLUGIN_ID]
        if keep_files:
            command.append("--keep-files")
        if force:
            command.append("--force")
        if dry_run:
            command.append("--dry-run")
        if dry_run:
            actions.append("would execute `openclaw plugins uninstall memory-palace`")
        else:
            try:
                completed = subprocess.run(
                    command,
                    cwd=project_root(),
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=30,
                    env={
                        **os.environ,
                        "OPENCLAW_CONFIG_PATH": str(config_path),
                    },
                )
            except Exception:
                completed = None
            if completed is not None and completed.returncode == 0:
                actions.append("executed `openclaw plugins uninstall memory-palace`")
            else:
                warnings.append(
                    "OpenClaw plugin uninstall command did not complete cleanly; continuing with local cleanup."
                )
    else:
        warnings.append("未检测到 openclaw，可继续做本地配置和 runtime 清理，但不会卸载插件文件。")

    current = read_json_file(config_path)
    current_plugins = current.get("plugins") if isinstance(current.get("plugins"), dict) else {}
    current_slots = current_plugins.get("slots") if isinstance(current_plugins.get("slots"), dict) else {}
    current_memory_slot = str(current_slots.get("memory") or "").strip()
    merged, config_actions = remove_plugin_from_openclaw_config(
        current,
        plugin_install_root=plugin_install_root,
        restore_memory_slot=restore_memory_slot,
    )
    if current_memory_slot == PLUGIN_ID and not restore_memory_slot:
        warnings.append(
            "未找到之前的 memory slot 记录；卸载后将直接移除 plugins.slots.memory。"
        )
    write_json_file(config_path, merged, dry_run=dry_run)
    if config_backup_path is not None:
        actions.append(f"backed up existing config to {config_backup_path}")
    actions.extend(config_actions)

    pid = _read_pid_file(default_dashboard_pid_path(setup_root_path))
    if pid:
        if dry_run:
            actions.append("would stop dashboard dev server")
        elif _terminate_process(pid):
            actions.append("stopped dashboard dev server")
    _remove_file_if_exists(default_dashboard_pid_path(setup_root_path), dry_run=dry_run)

    backend_pid = _read_pid_file(default_backend_api_pid_path(setup_root_path))
    if backend_pid:
        if dry_run:
            actions.append("would stop backend HTTP API")
        elif _terminate_process(backend_pid):
            actions.append("stopped backend HTTP API")
    _remove_file_if_exists(default_backend_api_pid_path(setup_root_path), dry_run=dry_run)

    if remove_runtime and setup_root_path.exists():
        if dry_run:
            actions.append(f"would remove runtime directory {setup_root_path}")
        else:
            shutil.rmtree(setup_root_path, ignore_errors=True)
            actions.append(f"removed runtime directory {setup_root_path}")
    elif setup_root_path.exists():
        actions.append(f"kept runtime directory {setup_root_path}")

    return {
        "ok": True,
        "summary": "Memory Palace uninstall completed.",
        "config_path": str(config_path),
        "config_path_source": config_path_source,
        "plugin_root": str(plugin_install_root or plugin_root()),
        "setup_root": str(setup_root_path),
        "keep_files": keep_files,
        "remove_runtime": remove_runtime,
        "dry_run": dry_run,
        "actions": actions,
        "warnings": warnings,
        "config_backup_path": str(config_backup_path) if config_backup_path else None,
        "config_preview": build_report_snapshot(merged),
    }


def build_install_report(
    *,
    config_path: Path,
    config_path_source: str,
    transport: str,
    activate_slot: bool,
    dry_run: bool,
    actions: list[str],
    merged_payload: dict[str, Any],
    sse_url: str | None,
    api_key_env: str | None,
    database_url: str | None,
    plugin_path: Path | None = None,
    runtime_python_path: Path | None = None,
    runtime_env_file: Path | None = None,
    openclaw_bin: str | None = None,
    host_platform: str | None = None,
    env_values: Mapping[str, str] | None = None,
    requested_profile: str | None = None,
    effective_profile: str | None = None,
    provider_probe: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    checks = collect_install_checks(
        config_path=config_path,
        config_path_source=config_path_source,
        transport=transport,
        sse_url=sse_url,
        api_key_env=api_key_env,
        database_url=database_url,
        plugin_path=plugin_path,
        runtime_python_path=runtime_python_path,
        runtime_env_file=runtime_env_file,
        openclaw_bin=openclaw_bin,
        host_platform=host_platform,
        env_values=env_values,
        requested_profile=requested_profile,
        effective_profile=effective_profile,
        provider_probe=provider_probe,
    )
    warning_count = sum(1 for item in checks if item["status"] == "WARN")
    return {
        "ok": True,
        "summary": f"Installer completed with {len(actions)} config action(s) and {warning_count} warning(s).",
        "config_path": str(config_path),
        "config_path_source": config_path_source,
        "plugin_root": str(plugin_path or plugin_root()),
        "transport": transport,
        "activate_slot": activate_slot,
        "dry_run": dry_run,
        "actions": actions,
        "checks": checks,
        "next_steps": build_next_steps(
            config_path=config_path,
            transport=transport,
            dry_run=dry_run,
            host_platform=host_platform,
        ),
        "config_preview": build_report_snapshot(merged_payload),
    }


def ensure_runtime_venv(
    *,
    setup_root_path: Path,
    dry_run: bool,
) -> tuple[Path, list[str]]:
    if (
        sys.version_info < (3, SUPPORTED_PYTHON_MIN_MINOR)
        or sys.version_info >= (3, SUPPORTED_PYTHON_MAX_MINOR + 1)
    ):
        raise RuntimeError(
            "Memory Palace OpenClaw setup currently requires "
            f"Python 3.{SUPPORTED_PYTHON_MIN_MINOR}-3.{SUPPORTED_PYTHON_MAX_MINOR}."
        )
    runtime_python = default_runtime_python_path(setup_root_path)
    actions: list[str] = []
    if runtime_python.exists():
        return runtime_python, actions
    if dry_run:
        actions.append(f"would create runtime venv at {runtime_python.parent.parent}")
        return runtime_python, actions
    runtime_python.parent.parent.mkdir(parents=True, exist_ok=True)
    print(
        f"[setup] creating runtime venv at {runtime_python.parent.parent}",
        file=sys.stderr,
    )
    builder = venv.EnvBuilder(with_pip=True, clear=False)
    builder.create(runtime_python.parent.parent)
    if not runtime_python.exists():
        subprocess.run(
            [sys.executable, "-m", "venv", str(runtime_python.parent.parent)],
            cwd=project_root(),
            check=True,
        )
    if not runtime_python.exists():
        raise RuntimeError(f"Runtime venv creation did not produce {runtime_python}")
    actions.append(f"created runtime venv at {runtime_python.parent.parent}")
    requirements_file = runtime_requirements_path()
    if requirements_file is not None and requirements_file.is_file():
        pip_env = {
            **os.environ,
            "PIP_DEFAULT_TIMEOUT": os.getenv("PIP_DEFAULT_TIMEOUT", "300"),
            "PIP_RETRIES": os.getenv("PIP_RETRIES", "8"),
            "PIP_DISABLE_PIP_VERSION_CHECK": os.getenv("PIP_DISABLE_PIP_VERSION_CHECK", "1"),
        }
        install_command = [str(runtime_python), "-m", "pip", "install", "-r", str(requirements_file)]
        print(
            f"[setup] installing runtime requirements from {requirements_file}",
            file=sys.stderr,
        )
        last_completed: subprocess.CompletedProcess[str] | None = None
        for attempt in range(1, RUNTIME_REQUIREMENTS_INSTALL_RETRIES + 1):
            completed = subprocess.run(
                install_command,
                cwd=project_root(),
                env=pip_env,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                check=False,
            )
            last_completed = completed
            if completed.returncode == 0:
                if completed.stdout:
                    print(completed.stdout, file=sys.stderr, end="")
                if completed.stderr:
                    print(completed.stderr, file=sys.stderr, end="")
                print("[setup] runtime requirements install completed", file=sys.stderr)
                actions.append(f"installed backend requirements from {requirements_file.name} into runtime venv")
                break
            if completed.stdout:
                print(completed.stdout, file=sys.stderr, end="")
            if completed.stderr:
                print(completed.stderr, file=sys.stderr, end="")
            if attempt < RUNTIME_REQUIREMENTS_INSTALL_RETRIES:
                time.sleep(min(2 ** (attempt - 1), 5))
                continue
            raise subprocess.CalledProcessError(
                completed.returncode,
                install_command,
                output=completed.stdout,
                stderr=completed.stderr,
            )
        if last_completed is None or last_completed.returncode != 0:
            raise RuntimeError(f"Failed to install backend requirements from {requirements_file}")
    return runtime_python, actions


def build_setup_state(
    *,
    setup_root_path: Path,
    config_path: Path,
    config_path_source: str,
    env_values: Mapping[str, str],
    mode: str,
    requested_profile: str,
    effective_profile: str,
    transport: str,
    dashboard: dict[str, Any] | None = None,
    backend_api: dict[str, Any] | None = None,
    warnings: list[str] | None = None,
    restart_required: bool = False,
    provider_probe: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    state = configured_runtime_state(env_values, setup_root_path=setup_root_path)
    last_provider_probe = load_provider_probe_status(setup_root_path)
    host_wiring = inspect_host_wiring_state(config_path=config_path)
    state["requiresOnboarding"] = (
        not default_runtime_env_path(setup_root_path).exists()
        or not bool(host_wiring.get("hostWiringReady"))
    )
    state["restartRequired"] = restart_required
    state["envFile"] = str(default_runtime_env_path(setup_root_path))
    state["configPath"] = str(config_path)
    state["mode"] = mode
    state["requestedProfile"] = requested_profile
    state["effectiveProfile"] = effective_profile
    state["transport"] = transport
    state["frontendAvailable"] = frontend_root().is_dir()
    state["dashboard"] = dashboard or inspect_dashboard_state(setup_root_path=setup_root_path, env_values=env_values)
    state["backendApi"] = backend_api or inspect_backend_api_state(setup_root_path=setup_root_path, env_values=env_values)
    state["hostWiring"] = host_wiring
    state["providerProbe"] = (
        dict(provider_probe)
        if isinstance(provider_probe, Mapping)
        else resolve_provider_probe_status(
            env_values=env_values,
            setup_root_path=setup_root_path,
            requested_profile=requested_profile,
            effective_profile=effective_profile,
        )
    )
    state["lastProviderProbe"] = (
        dict(last_provider_probe)
        if isinstance(last_provider_probe, Mapping)
        else None
    )
    state["warnings"] = dedupe_keep_order([*(state.get("warnings") or []), *((warnings or []))])
    return state


def perform_setup(
    *,
    config: str | None = None,
    setup_root_value: str | None = None,
    env_file_value: str | None = None,
    transport: str = "stdio",
    mode: str = "basic",
    profile: str = "b",
    sse_url: str | None = None,
    api_key_env: str = "MCP_API_KEY",
    timeout_ms: int = 20_000,
    connect_retries: int = 1,
    connect_backoff_ms: int = 250,
    no_activate: bool = False,
    dry_run: bool = False,
    json_output: bool = False,
    reconfigure: bool = False,
    strict_profile: bool = False,
    database_path: str | None = None,
    mcp_api_key: str | None = None,
    allow_insecure_local: bool | None = None,
    allow_generate_remote_api_key: bool = False,
    dashboard_host: str | None = None,
    dashboard_port: str | int | None = None,
    backend_api_host: str | None = None,
    backend_api_port: str | int | None = None,
    embedding_api_base: str | None = None,
    embedding_api_key: str | None = None,
    embedding_model: str | None = None,
    embedding_dim: str | int | None = None,
    reranker_api_base: str | None = None,
    reranker_api_key: str | None = None,
    reranker_model: str | None = None,
    llm_api_base: str | None = None,
    llm_api_key: str | None = None,
    llm_model: str | None = None,
    write_guard_llm_api_base: str | None = None,
    write_guard_llm_api_key: str | None = None,
    write_guard_llm_model: str | None = None,
    compact_gist_llm_api_base: str | None = None,
    compact_gist_llm_api_key: str | None = None,
    compact_gist_llm_model: str | None = None,
) -> dict[str, Any]:
    platform_name = host_platform_name()
    normalized_mode = str(mode or "basic").strip().lower() or "basic"
    if normalized_mode not in MODE_VALUES:
        raise ValueError(f"Unsupported mode: {mode}")
    normalized_transport = str(transport or "stdio").strip().lower() or "stdio"
    if normalized_transport not in TRANSPORT_VALUES:
        raise ValueError(f"Unsupported transport: {transport}")
    config_path, config_path_source = detect_config_path_with_source(config)
    setup_root_path = Path(setup_root_value).expanduser().resolve() if setup_root_value else default_setup_root()
    env_file_path = Path(env_file_value).expanduser().resolve() if env_file_value else default_runtime_env_path(setup_root_path)
    existing_env = load_env_file(env_file_path)
    requested_profile = str(profile).strip().lower() or "b"
    prompt_supported = supports_interactive_profile_prompt(
        profile=requested_profile,
        dry_run=dry_run,
        json_output=json_output,
    )
    interactive_actions: list[str] = []
    prompted_env: dict[str, str] | None = None
    prompted_env_source_path: Path | None = None
    profile_probe_failures: list[dict[str, str]] = []
    provider_probe_payload: dict[str, Any] | None = None

    def compute_setup_defaults(*, strict_override: bool) -> tuple[dict[str, str], str, list[str], bool, list[str]]:
        return apply_setup_defaults(
            profile=profile,
            mode=normalized_mode,
            transport=normalized_transport,
            config_path=config_path if platform_name == "windows" else None,
            setup_root_path=setup_root_path,
            existing_env=existing_env if reconfigure or existing_env else existing_env,
            database_path=database_path,
            sse_url=sse_url,
            mcp_api_key=mcp_api_key,
            allow_insecure_local=allow_insecure_local,
            embedding_api_base=embedding_api_base,
            embedding_api_key=embedding_api_key,
            embedding_model=embedding_model,
            embedding_dim=embedding_dim,
            reranker_api_base=reranker_api_base,
            reranker_api_key=reranker_api_key,
            reranker_model=reranker_model,
            llm_api_base=llm_api_base,
            llm_api_key=llm_api_key,
            llm_model=llm_model,
            write_guard_llm_api_base=write_guard_llm_api_base,
            write_guard_llm_api_key=write_guard_llm_api_key,
            write_guard_llm_model=write_guard_llm_model,
            compact_gist_llm_api_base=compact_gist_llm_api_base,
            compact_gist_llm_api_key=compact_gist_llm_api_key,
            compact_gist_llm_model=compact_gist_llm_model,
            prompted_env=prompted_env,
            strict_profile=strict_override,
            allow_generate_remote_api_key=allow_generate_remote_api_key,
            host_platform=platform_name,
        )

    env_values, effective_profile, warnings, fallback_applied, missing_profile_fields = compute_setup_defaults(
        strict_override=strict_profile and not prompt_supported
    )
    env_values[_metadata_key("CONFIG_PATH")] = str(config_path)
    if prompt_supported and missing_profile_fields:
        while missing_profile_fields:
            choice = prompt_profile_input_method(
                profile=requested_profile,
                missing_fields=missing_profile_fields,
            )
            if choice == "fallback":
                break
            if choice == "env":
                loaded_env, prompt_actions, loaded_env_path = prompt_for_profile_env_file(
                    profile=requested_profile,
                    missing_fields=missing_profile_fields,
                )
                if loaded_env is None:
                    break
                merge_payload = loaded_env
                prompted_env_source_path = loaded_env_path
            else:
                merge_payload, prompt_actions = prompt_for_profile_manual_values(
                    profile=requested_profile,
                    missing_fields=profile_required_input_fields(requested_profile),
                )
            prompted_env = {**(prompted_env or {}), **merge_payload}
            interactive_actions.extend(prompt_actions)
            env_values, effective_profile, warnings, fallback_applied, missing_profile_fields = compute_setup_defaults(
                strict_override=False
            )
            if missing_profile_fields:
                print(
                    cli_text(
                        "profile_env_prompt_still_missing",
                        fields=", ".join(missing_profile_fields),
                    )
                )
        if missing_profile_fields and strict_profile:
            raise ValueError(
                f"Profile {requested_profile.upper()} requires: {', '.join(missing_profile_fields)}"
            )
    if prompt_supported and requested_profile == "c":
        llm_suite_already_configured = all(
            not is_placeholder_profile_value(env_values.get(key))
            for key in (
                "WRITE_GUARD_LLM_API_BASE",
                "WRITE_GUARD_LLM_API_KEY",
                "WRITE_GUARD_LLM_MODEL",
                "COMPACT_GIST_LLM_API_BASE",
                "COMPACT_GIST_LLM_API_KEY",
                "COMPACT_GIST_LLM_MODEL",
                "INTENT_LLM_API_BASE",
                "INTENT_LLM_API_KEY",
                "INTENT_LLM_MODEL",
            )
        )
        if not llm_suite_already_configured:
            enable_optional_llm = prompt_for_profile_c_optional_llm_choice()
            if enable_optional_llm:
                llm_payload, llm_actions = prompt_for_shared_llm_values(
                    features=["write_guard", "compact_gist", "intent_llm"]
                )
                if llm_payload:
                    prompted_env = {**(prompted_env or {}), **llm_payload}
                    interactive_actions.extend(llm_actions)
                    env_values, effective_profile, warnings, fallback_applied, missing_profile_fields = compute_setup_defaults(
                        strict_override=False
                    )
    if effective_profile in {"c", "d"}:
        probe_env_values: dict[str, str] | None = None
        probe_profile = effective_profile
        configured_embedding_dim = str(env_values.get("RETRIEVAL_EMBEDDING_DIM") or "").strip()
        if prompt_supported:
            print(cli_text("profile_probe_intro", profile=effective_profile.upper()))
        blocking_probe_failures = probe_profile_model_connectivity(
            env_values,
            profile=effective_profile,
        )
        reported_probe_failures = list(blocking_probe_failures)
        if effective_profile == "c":
            required_probe_failures = [
                item for item in blocking_probe_failures if str(item.get("component") or "").strip() != "llm"
            ]
            optional_llm_failures = [
                item for item in blocking_probe_failures if str(item.get("component") or "").strip() == "llm"
            ]
            if optional_llm_failures and not required_probe_failures:
                warnings = [
                    *warnings,
                    cli_text("profile_c_llm_probe_failed"),
                    *[
                        cli_text("profile_c_llm_probe_failed_detail", detail=str(item.get("detail") or "").strip() or "unknown_error")
                        for item in optional_llm_failures
                    ],
                ]
                env_values["WRITE_GUARD_LLM_ENABLED"] = "false"
                env_values["COMPACT_GIST_LLM_ENABLED"] = "false"
                env_values["INTENT_LLM_ENABLED"] = "false"
                blocking_probe_failures = []
                reported_probe_failures = list(optional_llm_failures)
        profile_probe_failures = list(blocking_probe_failures)
        detected_embedding_dim = str(env_values.get("RETRIEVAL_EMBEDDING_DIM") or "").strip()
        if (
            not blocking_probe_failures
            and detected_embedding_dim
            and detected_embedding_dim != configured_embedding_dim
        ):
            warnings = [
                *warnings,
                cli_text(
                    "profile_probe_embedding_dim_aligned",
                    profile=effective_profile.upper(),
                    configured=configured_embedding_dim or "unset",
                    detected=detected_embedding_dim,
                ),
            ]
        probe_env_values = dict(env_values)
        if blocking_probe_failures:
            component_names = ", ".join(item["component"] for item in blocking_probe_failures)
            component_details = "; ".join(
                f"{item['component']}: {item['detail']}"
                for item in blocking_probe_failures
                if str(item.get("detail") or "").strip()
            )
            if strict_profile:
                detail_suffix = f" Details: {component_details}" if component_details else ""
                strict_error_message = cli_text(
                    "profile_probe_failed_strict",
                    profile=effective_profile.upper(),
                    components=component_names,
                )
                raise ValueError(
                    f"{strict_error_message}{detail_suffix}"
                )
            warnings = [
                *warnings,
                cli_text(
                    "profile_probe_failed",
                    profile=effective_profile.upper(),
                    components=component_names,
                ),
                *[
                    cli_text(
                        "profile_probe_component_detail",
                        component=item["component"],
                        detail=item["detail"],
                    )
                    for item in blocking_probe_failures
                ],
                cli_text(
                    "profile_probe_retry_env",
                    profile=requested_profile.upper(),
                    path=str(
                        prompted_env_source_path
                        or persist_prompted_profile_env(
                            prompted_env=prompted_env,
                            setup_root_path=setup_root_path,
                            profile=requested_profile,
                            dry_run=dry_run,
                        )
                        or env_file_path
                    ),
                ),
                cli_text("profile_probe_retry_flags"),
            ]
            env_values, effective_profile, _probe_warnings, _probe_fallback, _probe_missing = apply_setup_defaults(
                profile="b",
                mode=normalized_mode,
                transport=normalized_transport,
                config_path=config_path if platform_name == "windows" else None,
                setup_root_path=setup_root_path,
                existing_env=existing_env if reconfigure or existing_env else existing_env,
                database_path=database_path,
                sse_url=sse_url,
                mcp_api_key=mcp_api_key,
                allow_insecure_local=allow_insecure_local,
                embedding_api_base=embedding_api_base,
                embedding_api_key=embedding_api_key,
                embedding_model=embedding_model,
                embedding_dim=embedding_dim,
                reranker_api_base=reranker_api_base,
                reranker_api_key=reranker_api_key,
                reranker_model=reranker_model,
                llm_api_base=llm_api_base,
                llm_api_key=llm_api_key,
                llm_model=llm_model,
                write_guard_llm_api_base=write_guard_llm_api_base,
                write_guard_llm_api_key=write_guard_llm_api_key,
                write_guard_llm_model=write_guard_llm_model,
                compact_gist_llm_api_base=compact_gist_llm_api_base,
                compact_gist_llm_api_key=compact_gist_llm_api_key,
                compact_gist_llm_model=compact_gist_llm_model,
                prompted_env=prompted_env,
                strict_profile=False,
                allow_generate_remote_api_key=allow_generate_remote_api_key,
                host_platform=platform_name,
            )
            env_values[_metadata_key("PROFILE_REQUESTED")] = requested_profile
            env_values[_metadata_key("PROFILE_EFFECTIVE")] = "b"
            fallback_applied = True
        provider_probe_payload = build_provider_probe_status(
            env_values=probe_env_values or dict(env_values),
            requested_profile=requested_profile,
            effective_profile=effective_profile,
            fallback_applied=fallback_applied,
            profile_probe_failures=reported_probe_failures,
            missing_profile_fields=missing_profile_fields,
            probed_profile=probe_profile,
        )
    else:
        provider_probe_payload = build_provider_probe_status(
            env_values=env_values,
            requested_profile=requested_profile,
            effective_profile=effective_profile,
            fallback_applied=fallback_applied,
            profile_probe_failures=[],
            missing_profile_fields=missing_profile_fields,
        )
    apply_stack_runtime_overrides(
        env_values,
        dashboard_host=dashboard_host,
        dashboard_port=dashboard_port,
        backend_api_host=backend_api_host,
        backend_api_port=backend_api_port,
    )
    transport_diagnostics_path = default_transport_diagnostics_path(setup_root_path)
    runtime_python, runtime_actions = ensure_runtime_venv(setup_root_path=setup_root_path, dry_run=dry_run)

    plugin_install_root, plugin_install_actions, plugin_install_warnings = ensure_plugin_install_root(
        setup_root_path=setup_root_path,
        dry_run=dry_run,
    )
    warnings = [*warnings, *plugin_install_warnings]
    if package_layout() == "package" and plugin_install_root is None:
        raise RuntimeError(
            "Could not resolve the installed plugin root. Run `openclaw plugins install <package>` before `npx ... setup` (for a local trusted package on OpenClaw 2026.4.5+, add `--dangerously-force-unsafe-install`)."
        )

    current = read_json_file(config_path)
    config_backup_path = backup_config_file(config_path, label="memory-palace-setup", dry_run=dry_run)
    current_plugins = current.get("plugins") if isinstance(current.get("plugins"), dict) else {}
    current_slots = current_plugins.get("slots") if isinstance(current_plugins.get("slots"), dict) else {}
    previous_memory_slot = str(current_slots.get("memory") or "").strip()
    if previous_memory_slot and previous_memory_slot != PLUGIN_ID:
        env_values[_metadata_key("PREVIOUS_MEMORY_SLOT")] = previous_memory_slot
    else:
        env_values.pop(_metadata_key("PREVIOUS_MEMORY_SLOT"), None)
    write_env_file(env_file_path, env_values, dry_run=dry_run)
    persist_provider_probe_status(
        setup_root_path=setup_root_path,
        payload=provider_probe_payload,
        dry_run=dry_run,
    )
    restart_required, restart_mismatch_keys = detect_restart_required(env_values)
    reindex_required, reindex_reason_keys = detect_reindex_required(existing_env, env_values)
    if restart_required:
        warnings = [
            *warnings,
            "当前运行中的后端环境与刚写入的 bootstrap 配置不一致；需要重启相关进程后才会完全按新配置运行。",
        ]
    if reindex_required:
        warnings = [
            *warnings,
            "检测到 embedding / reranker 检索配置发生变化；重启后请运行 `openclaw memory-palace index --wait --json` 重建索引，避免旧向量维度或 reranker 配置残留继续影响检索。",
        ]
    entry_payload = build_plugin_entry(
        profile=effective_profile,
        transport=normalized_transport,
        sse_url=sse_url or str(env_values.get(_metadata_key("SSE_URL")) or "").strip() or None,
        api_key_env=api_key_env,
        database_url=str(env_values.get("DATABASE_URL") or "").strip() or None,
        timeout_ms=timeout_ms,
        connect_retries=connect_retries,
        connect_backoff_ms=connect_backoff_ms,
        runtime_env_file=env_file_path,
        runtime_python_path=runtime_python,
        runtime_root=setup_root_path,
        transport_diagnostics_path=transport_diagnostics_path,
        host_platform=platform_name,
        env_values=env_values,
    )
    merged, config_actions = merge_openclaw_config(
        current,
        entry_payload=entry_payload,
        activate=not no_activate,
        plugin_install_root=plugin_install_root,
    )
    write_json_file(config_path, merged, dry_run=dry_run)
    backend_api: dict[str, Any] | None = None
    backend_api_actions: list[str] = []
    dashboard: dict[str, Any] | None = None
    dashboard_actions: list[str] = []
    resolved_openclaw_version = detect_openclaw_version()
    if resolved_openclaw_version and not resolved_openclaw_version.get("meets_minimum"):
        warnings = [
            *warnings,
            f"检测到 OpenClaw {resolved_openclaw_version.get('version') or 'unknown'}；自动 recall/capture/visual-harvest 依赖 >= {MIN_OPENCLAW_VERSION_TEXT} 的 hook-capable 宿主版本。",
        ]
    if normalized_mode in {"full", "dev"}:
        if package_layout() == "repo":
            warnings = [
                *warnings,
                "full/dev 当前通过本地 Vite dev server 提供 dashboard，并且首轮可能执行 npm 依赖安装；这是开发者/运维本地栈，不是自包含前端发布包。",
            ]
        backend_api, backend_api_actions, backend_api_warnings = ensure_backend_http_api(
            setup_root_path=setup_root_path,
            runtime_python_path=runtime_python,
            runtime_env_file=env_file_path,
            env_values=env_values,
            dry_run=dry_run,
        )
        warnings = [*warnings, *backend_api_warnings]
        if backend_api.get("status") in {"running", "running_external", "dry_run"}:
            dashboard, dashboard_actions, dashboard_warnings = ensure_frontend_dashboard(
                setup_root_path=setup_root_path,
                env_values=env_values,
                runtime_python_path=runtime_python,
                dry_run=dry_run,
            )
            warnings = [*warnings, *dashboard_warnings]
        else:
            dashboard = inspect_dashboard_state(setup_root_path=setup_root_path, env_values=env_values)
            dashboard["enabled"] = True
            dashboard["status"] = "blocked_by_backend_api"
            warnings = [*warnings, "backend HTTP API 未就绪，已跳过 dashboard 启动。"]
    setup_state = build_setup_state(
        setup_root_path=setup_root_path,
        config_path=config_path,
        config_path_source=config_path_source,
        env_values=env_values,
        mode=normalized_mode,
        requested_profile=str(profile).strip().lower() or "b",
        effective_profile=effective_profile,
        transport=normalized_transport,
        dashboard=dashboard,
        backend_api=backend_api,
        warnings=warnings,
        restart_required=restart_required,
        provider_probe=provider_probe_payload,
    )
    next_steps = build_next_steps(
        config_path=config_path,
        transport=normalized_transport,
        dry_run=dry_run,
        host_platform=platform_name,
    )
    if normalized_mode in {"full", "dev"}:
        if (
            dashboard
            and dashboard.get("status") in {"running", "running_external"}
            and backend_api
            and backend_api.get("status") in {"running", "running_external"}
        ):
            next_steps.append(f"Open dashboard: {dashboard['url']}")
        else:
            next_steps.append(f"{dashboard_cli_command(host_platform=platform_name)} status --json")
            next_steps.append(f"{dashboard_cli_command(host_platform=platform_name)} start --json")
            if backend_api and backend_api.get("status") == "port_in_use" and is_loopback_host(str(backend_api.get("host") or "")):
                suggested_backend_port = _find_available_loopback_port(int(backend_api.get("port") or BACKEND_API_PORT))
                if suggested_backend_port is not None:
                    extra_parts = [f"--backend-api-port {suggested_backend_port}"]
                    if dashboard and dashboard.get("status") == "port_in_use" and is_loopback_host(str(dashboard.get("host") or "")):
                        suggested_dashboard_port = _find_available_loopback_port(int(dashboard.get("port") or DASHBOARD_PORT))
                        if suggested_dashboard_port is not None:
                            extra_parts.append(f"--dashboard-port {suggested_dashboard_port}")
                    next_steps.append(
                        f"{dashboard_cli_command(host_platform=platform_name)} start {' '.join(extra_parts)} --json"
                    )
    if missing_profile_fields and not strict_profile:
        next_steps.append(
            "To switch back to the requested C/D profile, re-run setup with the missing model fields populated."
        )
    if reindex_required:
        next_steps.append("openclaw memory-palace index --wait --json")

    return {
        "ok": True,
        "summary": (
            f"Setup completed for mode={normalized_mode}, requested profile={profile.upper()}, "
            f"effective profile={effective_profile.upper()}."
        ),
        "config_path": str(config_path),
        "config_path_source": config_path_source,
        "env_file": str(env_file_path),
        "plugin_root": str(plugin_install_root or plugin_root()),
        "mode": normalized_mode,
        "requested_profile": str(profile).strip().lower() or "b",
        "effective_profile": effective_profile,
        "transport": normalized_transport,
        "activate_slot": not no_activate,
        "dry_run": dry_run,
        "config_backup_path": str(config_backup_path) if config_backup_path else None,
        "fallback_applied": fallback_applied,
        "profile_missing_fields": missing_profile_fields,
        "warnings": warnings,
        "actions": [
            *interactive_actions,
            *runtime_actions,
            *plugin_install_actions,
            *( [f"backed up existing config to {config_backup_path}"] if config_backup_path else [] ),
            *config_actions,
            *backend_api_actions,
            *dashboard_actions,
        ],
        "next_steps": next_steps,
        "config_preview": build_report_snapshot(merged),
        "setup": setup_state,
        "dashboard": dashboard,
        "backend_api": backend_api,
        "restart_required": restart_required,
        "restart_mismatch_keys": restart_mismatch_keys,
        "reindex_required": reindex_required,
        "reindex_reason_keys": reindex_reason_keys,
        "reindexGate": {
            "required": reindex_required,
            "reasonKeys": _semantic_reindex_reasons(reindex_reason_keys),
            "recommendedAction": "reindex_all" if reindex_required else None,
        },
        "profile_probe_failures": profile_probe_failures,
        "checks": collect_install_checks(
            config_path=config_path,
            config_path_source=config_path_source,
            transport=normalized_transport,
            sse_url=sse_url,
            api_key_env=api_key_env,
            database_url=str(env_values.get("DATABASE_URL") or "").strip() or None,
            plugin_path=plugin_install_root,
            runtime_python_path=runtime_python,
            runtime_env_file=env_file_path,
            env_values=env_values,
            requested_profile=requested_profile,
            effective_profile=effective_profile,
            provider_probe=provider_probe_payload,
        ),
    }


def bootstrap_status(
    *,
    config: str | None = None,
    setup_root_value: str | None = None,
    env_file_value: str | None = None,
) -> dict[str, Any]:
    config_path, config_path_source = detect_config_path_with_source(config)
    setup_root_path = Path(setup_root_value).expanduser().resolve() if setup_root_value else default_setup_root()
    env_file_path = Path(env_file_value).expanduser().resolve() if env_file_value else default_runtime_env_path(setup_root_path)
    env_values = load_env_file(env_file_path)
    state = configured_runtime_state(env_values, setup_root_path=setup_root_path)
    host_wiring = inspect_host_wiring_state(config_path=config_path)
    state["requiresOnboarding"] = (
        not env_file_path.exists()
        or not bool(host_wiring.get("hostWiringReady"))
    )
    restart_required, restart_mismatch_keys = detect_restart_required(env_values)
    if restart_required:
        state["warnings"] = dedupe_keep_order([
            *(state.get("warnings") or []),
            "当前进程尚未加载 bootstrap env 文件中的最新运行参数。",
        ])
    state["restartRequired"] = restart_required
    state["restartMismatchKeys"] = restart_mismatch_keys
    # IMP-9 / Fix D: Compute reindexGate so it persists across page refreshes.
    process_env_snapshot = {
        k: v for k, v in os.environ.items()
        if k.startswith("RETRIEVAL_") or k.startswith("SEARCH_")
    }
    state["reindexGate"] = build_reindex_gate(process_env_snapshot, env_values)
    state["envFile"] = str(env_file_path)
    state["configPath"] = str(config_path)
    state["frontendAvailable"] = frontend_root().is_dir()
    state["dashboard"] = inspect_dashboard_state(setup_root_path=setup_root_path)
    state["hostWiring"] = host_wiring
    transport = str(state.get("transport") or "stdio").strip().lower() or "stdio"
    last_provider_probe = load_provider_probe_status(setup_root_path)
    provider_probe_payload = resolve_provider_probe_status(
        env_values=env_values,
        setup_root_path=setup_root_path,
        requested_profile=str(state.get("requestedProfile") or "b"),
        effective_profile=str(state.get("effectiveProfile") or "b"),
    )
    state["providerProbe"] = provider_probe_payload
    state["lastProviderProbe"] = (
        dict(last_provider_probe)
        if isinstance(last_provider_probe, Mapping)
        else None
    )
    checks = collect_install_checks(
        config_path=config_path,
        config_path_source=config_path_source,
        transport=transport,
        sse_url=str(env_values.get(_metadata_key("SSE_URL")) or "").strip() or None,
        api_key_env="MCP_API_KEY",
        database_url=str(env_values.get("DATABASE_URL") or "").strip() or None,
        runtime_python_path=default_runtime_python_path(setup_root_path),
        runtime_env_file=env_file_path,
        env_values=env_values,
        requested_profile=str(state.get("requestedProfile") or "b"),
        effective_profile=str(state.get("effectiveProfile") or "b"),
        provider_probe=provider_probe_payload,
    )
    payload = {
        "ok": True,
        "summary": (
            "Bootstrap configuration is ready."
            if env_file_path.exists() and bool(host_wiring.get("hostWiringReady"))
            else (
                "Bootstrap configuration is not fully wired into OpenClaw yet."
                if env_file_path.exists()
                else "Bootstrap configuration is not initialized yet."
            )
        ),
        "setup": state,
        "installGuidance": build_install_guidance(),
        "checks": checks,
        "profileOptions": list(PROFILE_VALUES),
        "modeOptions": list(MODE_VALUES),
        "transportOptions": list(TRANSPORT_VALUES),
        "config_path_source": config_path_source,
    }
    return payload


def resolve_bootstrap_runtime(
    *,
    config: str | None = None,
    setup_root_value: str | None = None,
    env_file_value: str | None = None,
) -> tuple[Path, str, Path, Path, dict[str, str], str, str, str]:
    config_path, config_path_source = detect_config_path_with_source(config)
    setup_root_path = (
        Path(setup_root_value).expanduser().resolve()
        if setup_root_value
        else default_setup_root()
    )
    env_file_path = (
        Path(env_file_value).expanduser().resolve()
        if env_file_value
        else default_runtime_env_path(setup_root_path)
    )
    if not env_file_path.is_file():
        raise RuntimeError(
            "Bootstrap runtime env is missing. Run `setup` before `upgrade` or `migrate`."
        )
    env_values = load_env_file(env_file_path)
    if not env_values:
        raise RuntimeError(
            f"Bootstrap runtime env is empty or unreadable: {env_file_path}"
        )
    mode = str(env_values.get(_metadata_key("MODE")) or "basic").strip().lower() or "basic"
    requested_profile = (
        str(env_values.get(_metadata_key("PROFILE_REQUESTED")) or "b").strip().lower()
        or "b"
    )
    transport = (
        str(env_values.get(_metadata_key("TRANSPORT")) or "stdio").strip().lower()
        or "stdio"
    )
    return (
        config_path,
        config_path_source,
        setup_root_path,
        env_file_path,
        env_values,
        mode,
        requested_profile,
        transport,
    )


def _run_runtime_migration_task(
    *,
    runtime_python: Path,
    database_url: str,
    migrations_dir: Path | None,
    lock_file_path: Path | None,
    lock_timeout_seconds: float,
    dry_run: bool,
) -> dict[str, Any]:
    backend_dir = backend_root().resolve()
    if not backend_dir.is_dir():
        raise RuntimeError(f"Backend directory is missing: {backend_dir}")

    helper = """
import asyncio
import json
import sqlite3
import sys
from pathlib import Path

database_url = sys.argv[1]
backend_root = sys.argv[2]
migrations_dir_raw = sys.argv[3]
lock_file_raw = sys.argv[4]
lock_timeout_seconds = float(sys.argv[5])
dry_run = sys.argv[6] == "1"

sys.path.insert(0, backend_root)

from db.migration_runner import MigrationRunner

runner = MigrationRunner(
    database_url=database_url,
    migrations_dir=Path(migrations_dir_raw) if migrations_dir_raw else None,
    lock_file_path=Path(lock_file_raw) if lock_file_raw else None,
    lock_timeout_seconds=lock_timeout_seconds,
)


def collect_versions():
    discovered = runner._discover_migrations()
    applied_map = {}
    if runner.database_file is not None and runner.database_file.exists():
        with sqlite3.connect(runner.database_file) as conn:
            conn.row_factory = sqlite3.Row
            runner._ensure_schema_table(conn)
            applied_map = runner._load_applied_checksums(conn)
    current_versions = sorted(applied_map.keys())
    pending_versions = [item.version for item in discovered if item.version not in applied_map]
    return current_versions, pending_versions


async def main():
    current_versions_before, pending_versions_before = collect_versions()
    applied_versions = []
    if not dry_run:
        applied_versions = await runner.apply_pending()
    current_versions, pending_versions_after = collect_versions()
    if dry_run:
        summary = (
            f"Migration dry-run found {len(pending_versions_before)} pending version(s)."
            if pending_versions_before
            else "Migration dry-run found no pending versions."
        )
    else:
        summary = (
            f"Applied {len(applied_versions)} migration(s)."
            if applied_versions
            else "No pending migrations."
        )
    print(
        json.dumps(
            {
                "ok": True,
                "summary": summary,
                "dry_run": dry_run,
                "database_file": str(runner.database_file) if runner.database_file is not None else None,
                "migrations_dir": str(runner.migrations_dir),
                "lock_file": str(runner.lock_file_path) if runner.lock_file_path is not None else None,
                "current_versions_before": current_versions_before,
                "pending_versions_before": pending_versions_before,
                "applied_versions": applied_versions,
                "current_versions": current_versions,
                "pending_versions_after": pending_versions_after,
            },
            ensure_ascii=False,
        )
    )


asyncio.run(main())
""".strip()

    completed = subprocess.run(
        [
            str(runtime_python),
            "-c",
            helper,
            database_url,
            str(backend_dir),
            str(migrations_dir.resolve()) if migrations_dir is not None else "",
            str(lock_file_path.resolve()) if lock_file_path is not None else "",
            str(max(0.0, lock_timeout_seconds)),
            "1" if dry_run else "0",
        ],
        cwd=str(project_root()),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
        timeout=300,
    )
    if completed.returncode != 0:
        stdout = str(completed.stdout or "").strip()
        stderr = str(completed.stderr or "").strip()
        details = [f"Migration helper failed with exit code {completed.returncode}."]
        if stdout:
            details.append(f"stdout: {stdout[-4000:]}")
        if stderr:
            details.append(f"stderr: {stderr[-4000:]}")
        raise RuntimeError("\n".join(details))
    text = str(completed.stdout or "").strip()
    if not text:
        raise RuntimeError("Migration helper returned empty stdout.")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Migration helper returned invalid JSON: {text[-4000:]}"
        ) from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Migration helper returned an unexpected payload shape.")
    return payload


def perform_migrate(
    *,
    config: str | None = None,
    setup_root_value: str | None = None,
    env_file_value: str | None = None,
    database_url: str | None = None,
    migrations_dir_value: str | None = None,
    lock_file_value: str | None = None,
    lock_timeout_seconds: float = 10.0,
    dry_run: bool = False,
) -> dict[str, Any]:
    (
        config_path,
        _config_path_source,
        setup_root_path,
        env_file_path,
        env_values,
        mode,
        requested_profile,
        transport,
    ) = resolve_bootstrap_runtime(
        config=config,
        setup_root_value=setup_root_value,
        env_file_value=env_file_value,
    )
    runtime_python, runtime_actions = ensure_runtime_venv(
        setup_root_path=setup_root_path,
        dry_run=dry_run,
    )
    if dry_run:
        runtime_python = (
            default_runtime_python_path(setup_root_path, host_platform=host_platform_name())
        )
    database_url_value = (
        str(database_url or env_values.get("DATABASE_URL") or "").strip()
    )
    if not database_url_value:
        raise RuntimeError(
            "Current bootstrap env does not define DATABASE_URL. `migrate` only supports local runtime databases."
        )
    migrations_dir = (
        Path(migrations_dir_value).expanduser().resolve()
        if migrations_dir_value
        else None
    )
    lock_file_path = (
        Path(lock_file_value).expanduser().resolve()
        if lock_file_value
        else None
    )
    warnings: list[str] = []
    backend_api = inspect_backend_api_state(
        setup_root_path=setup_root_path,
        env_values=env_values,
    )
    if backend_api.get("running"):
        warnings.append(
            "backend HTTP API 当前正在运行；如果你在高写入负载下执行 migrate，建议先停掉 full/dev stack 再重试。"
        )
    payload = _run_runtime_migration_task(
        runtime_python=runtime_python,
        database_url=database_url_value,
        migrations_dir=migrations_dir,
        lock_file_path=lock_file_path,
        lock_timeout_seconds=lock_timeout_seconds,
        dry_run=dry_run,
    )
    next_steps = ["openclaw memory-palace verify --json"]
    if str(mode or "").strip().lower() in {"full", "dev"}:
        next_steps.append(dashboard_cli_command(host_platform=host_platform_name()) + " status --json")
    return {
        "ok": True,
        "summary": str(payload.get("summary") or "Migration finished."),
        "config_path": str(config_path),
        "setup_root": str(setup_root_path),
        "env_file": str(env_file_path),
        "mode": mode,
        "requested_profile": requested_profile,
        "transport": transport,
        "database_url": database_url_value,
        "database_file": payload.get("database_file"),
        "migrations_dir": payload.get("migrations_dir"),
        "lock_file": payload.get("lock_file"),
        "runtime_python": str(runtime_python),
        "dry_run": bool(payload.get("dry_run", dry_run)),
        "current_versions_before": payload.get("current_versions_before") or [],
        "pending_versions_before": payload.get("pending_versions_before") or [],
        "applied_versions": payload.get("applied_versions") or [],
        "current_versions": payload.get("current_versions") or [],
        "pending_versions_after": payload.get("pending_versions_after") or [],
        "actions": runtime_actions,
        "warnings": warnings,
        "next_steps": next_steps,
        "backend_api": backend_api,
    }


def perform_upgrade(
    *,
    config: str | None = None,
    setup_root_value: str | None = None,
    env_file_value: str | None = None,
    strict_profile: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    (
        config_path,
        _config_path_source,
        setup_root_path,
        env_file_path,
        env_values,
        mode,
        requested_profile,
        transport,
    ) = resolve_bootstrap_runtime(
        config=config,
        setup_root_value=setup_root_value,
        env_file_value=env_file_value,
    )
    setup_report = perform_setup(
        config=str(config_path),
        setup_root_value=str(setup_root_path),
        env_file_value=str(env_file_path),
        transport=transport,
        mode=mode,
        profile=requested_profile,
        reconfigure=True,
        strict_profile=strict_profile,
        dry_run=dry_run,
        json_output=True,
    )
    migrate_report = perform_migrate(
        config=str(config_path),
        setup_root_value=str(setup_root_path),
        env_file_value=str(Path(str(setup_report.get("env_file") or env_file_path)).resolve()),
        dry_run=dry_run,
    )
    warnings = dedupe_keep_order(
        [
            *((setup_report.get("warnings") or [])),
            *((migrate_report.get("warnings") or [])),
        ]
    )
    actions = dedupe_keep_order(
        [
            *((setup_report.get("actions") or [])),
            *((migrate_report.get("actions") or [])),
        ]
    )
    next_steps = dedupe_keep_order(
        [
            *((setup_report.get("next_steps") or [])),
            *((migrate_report.get("next_steps") or [])),
        ]
    )
    return {
        "ok": True,
        "summary": "Upgrade completed." if not dry_run else "Upgrade dry-run completed.",
        "config_path": str(config_path),
        "setup_root": str(setup_root_path),
        "env_file": str(env_file_path),
        "mode": setup_report.get("mode") or mode,
        "requested_profile": setup_report.get("requested_profile") or requested_profile,
        "effective_profile": setup_report.get("effective_profile") or requested_profile,
        "transport": setup_report.get("transport") or transport,
        "dry_run": dry_run,
        "setup": setup_report,
        "migrate": migrate_report,
        "actions": actions,
        "warnings": warnings,
        "next_steps": next_steps,
    }


def copy_release_tree(source: Path, destination: Path, *, ignore: shutil.IgnorePattern | None = None) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination, ignore=ignore, dirs_exist_ok=True)


def build_frontend_release_bundle(frontend_source: Path) -> list[str]:
    package_json = frontend_source / "package.json"
    dist_root = frontend_source / "dist"
    if dist_root.is_dir() and (dist_root / "index.html").is_file():
        return [f"reused existing frontend static bundle at {dist_root}"]
    if not package_json.is_file():
        return []

    if (frontend_source / "pnpm-lock.yaml").is_file() and shutil.which("pnpm"):
        command = ["pnpm", "build"]
    else:
        command = [shutil.which("npm") or "npm", "run", "build"]

    completed = subprocess.run(
        command,
        cwd=frontend_source,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
        timeout=1800,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "Failed to build frontend release bundle.\n"
            f"COMMAND: {' '.join(command)}\n"
            f"STDOUT:\n{completed.stdout}\n"
            f"STDERR:\n{completed.stderr}"
        )
    if not dist_root.is_dir() or not (dist_root / "index.html").is_file():
        raise RuntimeError("Frontend build completed, but dist/index.html is missing.")
    return [f"built frontend static bundle via `{' '.join(command)}`"]


def stage_release_package() -> dict[str, Any]:
    if package_layout() != "repo":
        raise RuntimeError("Package staging is only available from the repo layout.")
    release_root = plugin_root() / "release"
    staging_parent = plugin_root()
    staging_dir = Path(
        tempfile.mkdtemp(
            prefix="release-stage-",
            dir=str(staging_parent),
        )
    )
    release_root = release_root.resolve()

    backend_ignore = shutil.ignore_patterns(
        ".venv",
        "__pycache__",
        ".pytest_cache",
        "tests",
        "docs",
        ".tmp",
        "AUDIT_REPORT.md",
        "AUDIT_REPORT*.md",
        "AUDIT-REPORT*.md",
        "CODE_REVIEW_REPORT*.md",
        "CODE-REVIEW*.md",
        "*REVIEW_REPORT*.md",
        "*REVIEW-REPORT*.md",
        "*REVIEW-FINDINGS*.md",
        "*REVIEW_FINDINGS*.md",
        "COMPREHENSIVE_*.md",
        "CONTEXT_*ANALYSIS*.md",
        "PROFILE_TEST_REPORT*.md",
        ".audit-report.md",
        "CLAUDE.md",
        "*pytest-of-*",
        "*test_snapshot_manager_*",
        "*\\private\\var\\folders\\*",
        "*.db",
        "*.sqlite",
        "*.log",
        "*.pyc",
        "*.pyo",
        ".DS_Store",
    )
    frontend_ignore = shutil.ignore_patterns(
        "node_modules",
        "e2e",
        "design-system",
        "playwright-report",
        "test-results",
        ".tmp",
        ".playwright-cli",
        "coverage",
        "AUDIT-REPORT.md",
        "AUDIT_REPORT*.md",
        "AUDIT-REPORT*.md",
        "*REVIEW_REPORT*.md",
        "*REVIEW-REPORT*.md",
        "*REVIEW-FINDINGS*.md",
        "*REVIEW_FINDINGS*.md",
        "COMPREHENSIVE_*.md",
        "CONTEXT_*ANALYSIS*.md",
        "PROFILE_TEST_REPORT*.md",
        "CLAUDE.md",
        "*.test.js",
        "*.test.jsx",
        "*.spec.ts",
        "*.tmp.test.jsx",
        ".DS_Store",
    )
    copy_release_tree(backend_root(), staging_dir / "backend", ignore=backend_ignore)
    if frontend_root().is_dir():
        frontend_actions = build_frontend_release_bundle(frontend_root())
        copy_release_tree(frontend_root(), staging_dir / "frontend", ignore=frontend_ignore)
    else:
        frontend_actions = []
    profiles_root = deploy_root() / "profiles"
    if profiles_root.is_dir():
        copy_release_tree(deploy_root(), staging_dir / "deploy", ignore=shutil.ignore_patterns(".DS_Store"))
    scripts_destination = staging_dir / "scripts"
    scripts_destination.mkdir(parents=True, exist_ok=True)
    for file_name in (
        "openclaw_json_output.py",
        "openclaw_memory_palace.py",
        "openclaw_memory_palace_launcher.mjs",
        "openclaw_memory_palace_installer.py",
        "openclaw_memory_palace_windows_smoke.ps1",
        "run_memory_palace_mcp_stdio.sh",
        "serve_memory_palace_dashboard.py",
    ):
        shutil.copy2(scripts_root() / file_name, scripts_destination / file_name)
    installer_package_source = scripts_root() / "installer"
    if installer_package_source.is_dir():
        copy_release_tree(
            installer_package_source,
            scripts_destination / "installer",
            ignore=shutil.ignore_patterns(
                "__pycache__",
                ".pytest_cache",
                "*.pyc",
                "*.pyo",
                ".DS_Store",
            ),
        )

    if env_example_path().is_file():
        shutil.copy2(env_example_path(), staging_dir / ".env.example")

    backup_root = release_root.with_name(f"{release_root.name}.bak")
    if backup_root.exists():
        shutil.rmtree(backup_root, ignore_errors=True)

    replaced_atomically = False
    if release_root.exists():
        try:
            release_root.replace(backup_root)
            replaced_atomically = True
        except PermissionError:
            # Windows can keep handles open on the existing release tree. Fall back to
            # refreshing the known staged entries in place instead of failing package staging.
            replaced_atomically = False

    if replaced_atomically or not release_root.exists():
        staging_dir.replace(release_root)
    else:
        release_root.mkdir(parents=True, exist_ok=True)
        staged_entries = {entry.name for entry in staging_dir.iterdir()}
        for child_path in list(release_root.iterdir()):
            if child_path.name in staged_entries:
                continue
            if child_path.is_dir():
                shutil.rmtree(child_path, ignore_errors=True)
            else:
                child_path.unlink(missing_ok=True)
        for entry in staging_dir.iterdir():
            destination = release_root / entry.name
            if entry.is_dir():
                copy_release_tree(entry, destination)
            else:
                shutil.copy2(entry, destination)
        shutil.rmtree(staging_dir, ignore_errors=True)

    if backup_root.exists():
        shutil.rmtree(backup_root, ignore_errors=True)

    return {
        "ok": True,
        "summary": "Release package resources staged.",
        "release_root": str(release_root),
        "actions": frontend_actions,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Install or update Memory Palace OpenClaw plugin config.")
    parser.add_argument("--config", help="Explicit OpenClaw config path.")
    parser.add_argument("--transport", choices=TRANSPORT_VALUES, default="stdio")
    parser.add_argument("--sse-url", help="SSE endpoint when --transport=sse.")
    parser.add_argument("--api-key-env", default="MCP_API_KEY", help="SSE API key env name.")
    parser.add_argument("--database-url", help="DATABASE_URL injected into stdio env.")
    parser.add_argument("--timeout-ms", type=int, default=20_000)
    parser.add_argument("--connect-retries", type=int, default=1)
    parser.add_argument("--connect-backoff-ms", type=int, default=250)
    parser.add_argument("--no-activate", action="store_true", help="Do not switch plugins.slots.memory.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--print-config-path", action="store_true")
    parser.add_argument("--json", action="store_true", help="Print JSON report.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    platform_name = host_platform_name()
    config_path, config_path_source = detect_config_path_with_source(args.config)
    if args.print_config_path:
        print(config_path)
        return 0

    current = read_json_file(config_path)
    entry_payload = build_plugin_entry(
        transport=args.transport,
        sse_url=args.sse_url,
        api_key_env=args.api_key_env,
        database_url=args.database_url,
        timeout_ms=args.timeout_ms,
        connect_retries=args.connect_retries,
        connect_backoff_ms=args.connect_backoff_ms,
        host_platform=platform_name,
    )
    merged, actions = merge_openclaw_config(
        current,
        entry_payload=entry_payload,
        activate=not args.no_activate,
        plugin_install_root=detect_plugin_install_root(),
    )
    write_json_file(config_path, merged, dry_run=args.dry_run)

    report = build_install_report(
        config_path=config_path,
        config_path_source=config_path_source,
        transport=args.transport,
        activate_slot=not args.no_activate,
        dry_run=args.dry_run,
        actions=actions,
        merged_payload=merged,
        sse_url=args.sse_url,
        api_key_env=args.api_key_env,
        database_url=args.database_url,
        plugin_path=detect_plugin_install_root(),
        host_platform=platform_name,
    )

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(report["summary"])
        print(f"config_path: {config_path}")
        print(f"config_path_source: {config_path_source}")
        print(f"plugin_root: {report['plugin_root']}")
        print(f"transport: {args.transport}")
        for action in actions:
            print(f"- {action}")
        if report["checks"]:
            print("checks:")
            for item in report["checks"]:
                line = f"- [{item['status']}] {item['id']}: {item['message']}"
                print(line)
                if item.get("action"):
                    print(f"  action: {item['action']}")
        if args.dry_run:
            print("dry_run: true")
        print("next_steps:")
        for step in report["next_steps"]:
            print(f"- {step}")
    return 0
