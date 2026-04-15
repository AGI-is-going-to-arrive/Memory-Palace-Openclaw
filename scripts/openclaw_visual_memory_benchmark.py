#!/usr/bin/env python3
from __future__ import annotations

import base64
import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import signal
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import openclaw_memory_palace_profile_smoke as smoke
from openclaw_json_output import extract_json_from_streams, extract_last_json_from_text


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_JSON_OUTPUT = (
    PROJECT_ROOT / ".tmp" / "benchmarks" / "openclaw_visual_memory_benchmark.json"
)
DEFAULT_MARKDOWN_OUTPUT = (
    PROJECT_ROOT / ".tmp" / "benchmarks" / "openclaw_visual_memory_benchmark.md"
)
DEFAULT_CASE_COUNT = 200
DEFAULT_RELEASE_CASE_LIMIT = 64
VALID_PROFILES = ("a", "b", "c", "d")
TRUTHY_ENV_VALUES = {"1", "true", "yes", "on", "enabled"}
DEFAULT_REQUIRED_COVERAGE_KEYS = (
    "raw_media_data_png",
    "raw_media_data_jpeg",
    "raw_media_data_webp",
    "raw_media_blob",
    "raw_media_presigned",
)
TRANSIENT_LOCK_MARKERS = (
    "database is locked",
    "sqlite3.operationalerror",
    "query-invoked autoflush",
)

ADJECTIVES = [
    "amber",
    "brisk",
    "calm",
    "delta",
    "ember",
    "frost",
    "gloss",
    "harbor",
    "ion",
    "jade",
    "keystone",
    "lumen",
    "matrix",
    "nova",
    "orbit",
    "pulse",
    "quartz",
    "radar",
    "signal",
    "tandem",
    "uplink",
    "vector",
    "wave",
    "xeno",
    "yonder",
    "zenith",
]
BOARDS = [
    "whiteboard",
    "kanban",
    "runbook",
    "tracker",
    "scorecard",
    "dashboard",
    "timeline",
    "notebook",
]
SCENES = [
    "ops war room",
    "office whiteboard",
    "migration desk",
    "incident bridge",
    "release control room",
    "rollout planning wall",
    "status huddle room",
    "support coordination hub",
]
PEOPLE = [
    "Alice",
    "Bob",
    "Carmen",
    "Diego",
    "Eve",
    "Farah",
    "Gina",
    "Hector",
    "Iris",
    "Jamal",
    "Kira",
    "Liam",
]
CHANNELS = [
    "discord",
    "telegram",
    "slack",
    "signal",
    "email",
    "teams",
    "sms",
    "whatsapp",
]
RELEASE_TOPICS = [
    "launch checklist",
    "rollback owner map",
    "shipping blockers",
    "scope freeze ledger",
    "handoff sequence",
    "migration window",
    "incident timeline",
    "verification matrix",
]


@dataclass(frozen=True)
class VisualBenchmarkCase:
    case_id: str
    family: str
    complexity: str
    description: str
    query: str
    store_args: list[str]
    expected_get_substrings: list[str]
    prime_store_args: list[str] | None = None
    forbidden_get_substrings: list[str] = field(default_factory=list)
    coverage_key: str = ""


@dataclass
class VisualBenchmarkResult:
    case_id: str
    family: str
    complexity: str
    description: str
    store_ok: bool
    search_hit_at_3: bool
    reciprocal_rank_at_3: float
    get_contains_expected: bool
    store_latency_ms: float
    search_latency_ms: float
    get_latency_ms: float
    stored_path: str | None
    stored_uri: str | None
    query: str
    notes: list[str]
    coverage_key: str = ""


@dataclass
class VisualBenchmarkProfileState:
    profile: str
    total_cases: int
    status: str = "pending"
    completed_cases: int = 0
    results: list[dict[str, Any]] = field(default_factory=list)
    runtime_probe: dict[str, Any] | None = None
    error: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    last_case_id: str | None = None
    current_case_id: str | None = None
    current_case_family: str | None = None
    current_case_started_at: str | None = None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _write_text_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    temp_path.write_text(content, encoding="utf-8")
    temp_path.replace(path)


class VisualBenchmarkRunRecorder:
    def __init__(
        self,
        *,
        profiles: list[str],
        case_catalog: list[VisualBenchmarkCase],
        selected_cases: list[VisualBenchmarkCase],
        json_output: Path,
        markdown_output: Path,
    ) -> None:
        self._lock = threading.Lock()
        self.started_at = _utc_now_iso()
        self.updated_at = self.started_at
        self.status = "running"
        self.reason: str | None = None
        self.json_output = json_output
        self.markdown_output = markdown_output
        self.case_catalog = list(case_catalog)
        self.selected_cases = list(selected_cases)
        self.family_summary = count_cases_by_attr(self.selected_cases, "family")
        self.complexity_summary = count_cases_by_attr(self.selected_cases, "complexity")
        self.coverage_summary = count_cases_by_attr(self.selected_cases, "coverage_key")
        self.profiles: dict[str, VisualBenchmarkProfileState] = {
            profile: VisualBenchmarkProfileState(profile=profile, total_cases=len(self.selected_cases))
            for profile in profiles
        }
        self.flush()

    @classmethod
    def from_payload(
        cls,
        *,
        payload: dict[str, Any],
        profiles: list[str],
        case_catalog: list[VisualBenchmarkCase],
        selected_cases: list[VisualBenchmarkCase],
        json_output: Path,
        markdown_output: Path,
    ) -> "VisualBenchmarkRunRecorder":
        recorder = cls.__new__(cls)
        recorder._lock = threading.Lock()
        recorder.started_at = str(payload.get("started_at") or _utc_now_iso())
        recorder.updated_at = _utc_now_iso()
        recorder.status = "running"
        recorder.reason = None
        recorder.json_output = json_output
        recorder.markdown_output = markdown_output
        recorder.case_catalog = list(case_catalog)
        recorder.selected_cases = list(selected_cases)
        recorder.family_summary = count_cases_by_attr(recorder.selected_cases, "family")
        recorder.complexity_summary = count_cases_by_attr(recorder.selected_cases, "complexity")
        recorder.coverage_summary = count_cases_by_attr(recorder.selected_cases, "coverage_key")
        existing_profiles = {
            str(item.get("profile") or ""): item
            for item in payload.get("profiles", [])
            if isinstance(item, dict)
        }
        recorder.profiles = {}
        for profile in profiles:
            existing = existing_profiles.get(profile, {})
            existing_results = [
                item for item in existing.get("results", []) if isinstance(item, dict)
            ]
            recorder.profiles[profile] = VisualBenchmarkProfileState(
                profile=profile,
                total_cases=len(selected_cases),
                status="completed"
                if str(existing.get("status") or "") == "completed"
                and len(existing_results) >= len(selected_cases)
                else ("running" if existing_results else "pending"),
                completed_cases=len(existing_results),
                results=list(existing_results),
                runtime_probe=existing.get("runtime_probe") if isinstance(existing.get("runtime_probe"), dict) else None,
                error=str(existing.get("error")) if isinstance(existing.get("error"), str) and existing.get("error") else None,
                started_at=str(existing.get("started_at")) if isinstance(existing.get("started_at"), str) and existing.get("started_at") else None,
                finished_at=str(existing.get("finished_at")) if isinstance(existing.get("finished_at"), str) and existing.get("finished_at") else None,
                last_case_id=str(existing.get("last_case_id")) if isinstance(existing.get("last_case_id"), str) and existing.get("last_case_id") else None,
                current_case_id=str(existing.get("current_case_id")) if isinstance(existing.get("current_case_id"), str) and existing.get("current_case_id") else None,
                current_case_family=str(existing.get("current_case_family")) if isinstance(existing.get("current_case_family"), str) and existing.get("current_case_family") else None,
                current_case_started_at=str(existing.get("current_case_started_at")) if isinstance(existing.get("current_case_started_at"), str) and existing.get("current_case_started_at") else None,
            )
        recorder.flush()
        return recorder

    def mark_profile_started(self, profile: str) -> None:
        with self._lock:
            state = self.profiles[profile]
            if state.started_at is None:
                state.started_at = _utc_now_iso()
            state.status = "running"
            self.updated_at = _utc_now_iso()
            self.flush_locked()
        print(
            f"[progress] profile={profile} status=running total_cases={self.profiles[profile].total_cases}",
            file=sys.stderr,
            flush=True,
        )

    def record_case_result(
        self,
        profile: str,
        result: VisualBenchmarkResult,
        *,
        progress_line: str | None = None,
    ) -> None:
        with self._lock:
            state = self.profiles[profile]
            if state.started_at is None:
                state.started_at = _utc_now_iso()
            state.status = "running"
            state.results.append(asdict(result))
            state.completed_cases = len(state.results)
            state.last_case_id = result.case_id
            state.current_case_id = None
            state.current_case_family = None
            state.current_case_started_at = None
            self.updated_at = _utc_now_iso()
            overall_completed = sum(item.completed_cases for item in self.profiles.values())
            overall_total = sum(item.total_cases for item in self.profiles.values())
            self.flush_locked()
        line = progress_line or (
            f"[progress] profile={profile} case={state.completed_cases}/{state.total_cases} "
            f"overall={overall_completed}/{overall_total} case_id={result.case_id} "
            f"store={str(result.store_ok).lower()} hit3={str(result.search_hit_at_3).lower()} "
            f"get={str(result.get_contains_expected).lower()}"
        )
        print(line, file=sys.stderr, flush=True)

    def mark_profile_finished(
        self,
        profile: str,
        *,
        runtime_probe: dict[str, Any] | None,
        status: str,
        error: str | None = None,
        progress_line: str | None = None,
    ) -> None:
        with self._lock:
            state = self.profiles[profile]
            state.runtime_probe = runtime_probe
            state.status = status
            state.error = error
            state.finished_at = _utc_now_iso()
            state.current_case_id = None
            state.current_case_family = None
            state.current_case_started_at = None
            self.updated_at = state.finished_at
            self.flush_locked()
        line = progress_line or (
            f"[progress] profile={profile} status={status} "
            f"completed_cases={state.completed_cases}/{state.total_cases}"
            + (f" error={error}" if error else "")
        )
        print(line, file=sys.stderr, flush=True)

    def mark_run_status(self, status: str, *, reason: str | None = None) -> None:
        with self._lock:
            self.status = status
            self.reason = reason
            self.updated_at = _utc_now_iso()
            self.flush_locked()

    def mark_case_started(self, profile: str, case: VisualBenchmarkCase) -> None:
        with self._lock:
            state = self.profiles[profile]
            if state.started_at is None:
                state.started_at = _utc_now_iso()
            state.status = "running"
            state.current_case_id = case.case_id
            state.current_case_family = case.family
            state.current_case_started_at = _utc_now_iso()
            self.updated_at = state.current_case_started_at
            self.flush_locked()

    def completed_case_ids(self, profile: str) -> set[str]:
        with self._lock:
            return {
                str(item.get("case_id"))
                for item in self.profiles[profile].results
                if isinstance(item, dict) and isinstance(item.get("case_id"), str)
            }

    def existing_result_objects(self, profile: str) -> list[VisualBenchmarkResult]:
        with self._lock:
            return [
                VisualBenchmarkResult(**item)
                for item in self.profiles[profile].results
                if isinstance(item, dict)
            ]

    def existing_runtime_probe(self, profile: str) -> dict[str, Any] | None:
        with self._lock:
            probe = self.profiles[profile].runtime_probe
            return dict(probe) if isinstance(probe, dict) else None

    def flush(self) -> None:
        with self._lock:
            self.flush_locked()

    def flush_locked(self) -> None:
        payload = self.build_payload_locked()
        markdown = self.build_markdown_locked(payload)
        _write_text_atomic(
            self.json_output,
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        )
        _write_text_atomic(self.markdown_output, markdown)

    def build_payload_locked(self) -> dict[str, Any]:
        profile_payloads: list[dict[str, Any]] = []
        executed_case_count_total = 0
        for profile in self.profiles.values():
            results = [VisualBenchmarkResult(**item) for item in profile.results]
            metrics = compute_metrics(results, runtime_probe=profile.runtime_probe)
            executed_case_count_total += profile.completed_cases
            profile_payloads.append(
                {
                    "profile": profile.profile,
                    "status": profile.status,
                    "started_at": profile.started_at,
                    "finished_at": profile.finished_at,
                    "completed_case_count": profile.completed_cases,
                    "total_case_count": profile.total_cases,
                    "progress_ratio": round(
                        profile.completed_cases / profile.total_cases, 3
                    )
                    if profile.total_cases
                    else 0.0,
                    "last_case_id": profile.last_case_id,
                    "current_case_id": profile.current_case_id,
                    "current_case_family": profile.current_case_family,
                    "current_case_started_at": profile.current_case_started_at,
                    "metrics": metrics,
                    "runtime_probe": profile.runtime_probe,
                    "results": profile.results,
                    **({"error": profile.error} if profile.error else {}),
                }
            )
        return {
            "status": self.status,
            "partial": self.status != "completed",
            "reason": self.reason,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "case_catalog_size": len(self.case_catalog),
            "executed_case_count_per_profile": len(self.selected_cases),
            "executed_case_count_total": executed_case_count_total,
            "family_summary": self.family_summary,
            "complexity_summary": self.complexity_summary,
            "coverage_summary": self.coverage_summary,
            "profiles": profile_payloads,
        }

    def build_markdown_locked(self, payload: dict[str, Any]) -> str:
        profiles = payload.get("profiles", [])
        lines = [
            "# OpenClaw Visual Memory Benchmark Matrix",
            "",
            f"- status: `{payload.get('status')}`",
            f"- partial: `{str(bool(payload.get('partial'))).lower()}`",
            f"- started_at: `{payload.get('started_at')}`",
            f"- updated_at: `{payload.get('updated_at')}`",
            f"- case_catalog_size: `{payload.get('case_catalog_size')}`",
            f"- executed_case_count_per_profile: `{payload.get('executed_case_count_per_profile')}`",
            f"- executed_case_count_total: `{payload.get('executed_case_count_total')}`",
        ]
        if payload.get("reason"):
            lines.append(f"- reason: `{payload.get('reason')}`")
        lines.extend(
            [
                "",
                "| Profile | Status | Progress | Store | Hit@3 | MRR@3 | Get OK | Runtime Probe | Harvest OK |",
                "|---|---|---:|---:|---:|---:|---:|---|---:|",
            ]
        )
        for item in profiles:
            metrics = item.get("metrics", {}) if isinstance(item, dict) else {}
            progress = (
                f"{item.get('completed_case_count', 0)}/{item.get('total_case_count', 0)}"
                if isinstance(item, dict)
                else "0/0"
            )
            lines.append(
                f"| {item.get('profile', '-')} | {item.get('status', '-')} | {progress} | "
                f"{metrics.get('store_success_rate', '-')} | "
                f"{metrics.get('search_hit_at_3_rate', '-')} | "
                f"{metrics.get('mrr_at_3', '-')} | "
                f"{metrics.get('get_contains_expected_rate', '-')} | "
                f"{metrics.get('runtime_visual_probe', '-')} | "
                f"{metrics.get('runtime_visual_harvest_success_rate', '-')} |"
            )
        lines.extend(_render_distribution_lines("Family Coverage", payload.get("family_summary", {})))
        lines.extend(_render_distribution_lines("Complexity Coverage", payload.get("complexity_summary", {})))
        lines.extend(_render_distribution_lines("Raw Media Coverage", payload.get("coverage_summary", {})))
        return "\n".join(lines) + "\n"


def _normalize_profiles(raw: str | None, fallback: str | None = None) -> list[str]:
    source = raw if raw and raw.strip() else fallback or "a"
    profiles: list[str] = []
    seen: set[str] = set()
    for token in source.split(","):
        profile = token.strip().lower()
        if not profile:
            continue
        if profile not in VALID_PROFILES:
            raise ValueError(f"Unsupported profile '{profile}'. Expected one of: {', '.join(VALID_PROFILES)}")
        if profile in seen:
            continue
        seen.add(profile)
        profiles.append(profile)
    if not profiles:
        raise ValueError("At least one profile is required.")
    return profiles


def parse_profiles(profile: str | None, profiles: str | None) -> list[str]:
    return _normalize_profiles(profiles, profile)


def _case_token(family: str, ordinal: int) -> str:
    return f"bench-{family.replace('_', '-')}-{ordinal:03d}"


def _media_ref(case_id: str) -> str:
    return f"file:/tmp/{case_id}.png"


def _data_url_media_ref(token: str, *, mime: str) -> str:
    raw_blob = base64.b64encode(((token + "|") * 10).encode("utf-8")).decode("ascii")
    return f"data:{mime};base64,{raw_blob}"


def _blob_media_ref(token: str) -> str:
    return f"blob:https://openclaw.local/{token}"


def _presigned_media_ref(token: str) -> str:
    signature = (token * 24)[:720]
    return (
        "https://cdn.openclaw.local/visuals/"
        f"{token}.png"
        "?X-Amz-Algorithm=AWS4-HMAC-SHA256"
        f"&X-Amz-Credential={token}%2F20260311%2Fus-east-1%2Fs3%2Faws4_request"
        "&X-Amz-Date=20260311T000000Z"
        "&X-Amz-Expires=900"
        f"&X-Amz-Security-Token={signature}"
        f"&X-Amz-Signature={signature}"
    )


def _expected_sanitized_media_ref(media_ref: str) -> str:
    if re.match(r"^data:", media_ref, flags=re.IGNORECASE):
        mime_match = re.search(r"^data:([^;,]+)", media_ref, flags=re.IGNORECASE)
        mime_value = mime_match.group(1) if mime_match else "application/octet-stream"
        digest = hashlib.sha256(media_ref.encode("utf-8")).hexdigest()[:12]
        return f"data:{mime_value};sha256-{digest}"
    if len(media_ref) > 512:
        digest = hashlib.sha256(media_ref.encode("utf-8")).hexdigest()[:12]
        return f"sha256-{digest}"
    return media_ref


def _choice(values: list[str], ordinal: int, offset: int = 0) -> str:
    return values[(ordinal + offset) % len(values)]


def _iso_observed_at(ordinal: int) -> str:
    day = 1 + (ordinal % 27)
    hour = 8 + (ordinal % 10)
    minute = 7 + (ordinal % 43)
    second = 11 + (ordinal % 37)
    return f"2026-03-{day:02d}T{hour:02d}:{minute:02d}:{second:02d}.000Z"


def _family_distribution(total_cases: int) -> dict[str, int]:
    families = list(FAMILY_BUILDERS.keys())
    if total_cases < len(families):
        raise ValueError(
            f"case-count must be >= {len(families)} so every family is represented."
        )
    base, remainder = divmod(total_cases, len(families))
    return {
        family: base + (1 if index < remainder else 0)
        for index, family in enumerate(families)
    }


def _build_ocr_exact_case(ordinal: int) -> VisualBenchmarkCase:
    token = _case_token("ocr_exact", ordinal)
    board = _choice(BOARDS, ordinal)
    topic = _choice(RELEASE_TOPICS, ordinal)
    summary = f"{_choice(ADJECTIVES, ordinal)} {board} capture for {token}"
    ocr = f"{token} {topic}"
    return VisualBenchmarkCase(
        case_id=f"ocr_exact_{ordinal:03d}",
        family="ocr_exact",
        complexity="basic",
        description="Exact OCR token retrieval over real OpenClaw CLI",
        query=f"{token} {topic.split()[0]}",
        store_args=[
            "--media-ref",
            _media_ref(f"ocr-exact-{ordinal:03d}"),
            "--summary",
            summary,
            "--ocr",
            ocr,
            "--scene",
            _choice(SCENES, ordinal),
        ],
        expected_get_substrings=[ocr, "provenance_ocr_source: direct"],
    )


def _build_summary_overlap_case(ordinal: int) -> VisualBenchmarkCase:
    token = _case_token("summary_overlap", ordinal)
    board = _choice(BOARDS, ordinal, 1)
    summary = f"{_choice(ADJECTIVES, ordinal, 2)} {board} for {token} shipping blockers"
    return VisualBenchmarkCase(
        case_id=f"summary_overlap_{ordinal:03d}",
        family="summary_overlap",
        complexity="basic",
        description="Summary-heavy retrieval with reordered query phrasing",
        query=f"shipping blockers {token} {board}",
        store_args=[
            "--media-ref",
            _media_ref(f"summary-overlap-{ordinal:03d}"),
            "--summary",
            summary,
            "--scene",
            _choice(SCENES, ordinal, 1),
        ],
        expected_get_substrings=[summary, "provenance_summary_source: direct"],
    )


def _build_scene_entity_case(ordinal: int) -> VisualBenchmarkCase:
    token = _case_token("scene_entity", ordinal)
    person_a = _choice(PEOPLE, ordinal)
    person_b = _choice(PEOPLE, ordinal, 3)
    scene = f"{_choice(SCENES, ordinal, 2)} {token}"
    return VisualBenchmarkCase(
        case_id=f"scene_entity_{ordinal:03d}",
        family="scene_entity",
        complexity="medium",
        description="Scene plus entity lookup over a real visual record",
        query=f"{person_a} {token} {scene.split()[0]}",
        store_args=[
            "--media-ref",
            _media_ref(f"scene-entity-{ordinal:03d}"),
            "--summary",
            f"team board snapshot for {token}",
            "--scene",
            scene,
            "--entities",
            f"{person_a},{person_b}",
        ],
        expected_get_substrings=[f"{person_a}, {person_b}", scene],
    )


def _build_visual_context_only_case(ordinal: int) -> VisualBenchmarkCase:
    token = _case_token("visual_context_only", ordinal)
    summary = f"context planning wall {token}"
    visual_context = json.dumps(
        {
            "summary": summary,
            "ocr": f"{token} scope freeze checklist",
            "scene": f"{_choice(SCENES, ordinal, 3)}",
            "entities": [_choice(PEOPLE, ordinal), _choice(PEOPLE, ordinal, 4)],
            "whyRelevant": f"release planning for {token}",
        },
        ensure_ascii=False,
    )
    return VisualBenchmarkCase(
        case_id=f"visual_context_only_{ordinal:03d}",
        family="visual_context_only",
        complexity="medium",
        description="Visual-context-only write should preserve context provenance",
        query=f"{token} planning wall",
        store_args=[
            "--media-ref",
            _media_ref(f"visual-context-only-{ordinal:03d}"),
            "--visual-context",
            visual_context,
        ],
        expected_get_substrings=[summary, "provenance_summary_source: context"],
    )


def _build_duplicate_new_case(ordinal: int) -> VisualBenchmarkCase:
    token = _case_token("duplicate_new", ordinal)
    media_ref = _media_ref(f"duplicate-new-{ordinal:03d}")
    summary = f"variant board {token}"
    prime_args = [
        "--media-ref",
        media_ref,
        "--summary",
        summary,
        "--scene",
        _choice(SCENES, ordinal, 4),
    ]
    return VisualBenchmarkCase(
        case_id=f"duplicate_new_{ordinal:03d}",
        family="duplicate_new",
        complexity="complex",
        description="duplicatePolicy=new should create a distinct visual variant",
        query=f"{token} duplicate variant new-01",
        prime_store_args=prime_args,
        store_args=[
            *prime_args,
            "--duplicate-policy",
            "new",
        ],
        expected_get_substrings=["duplicate_variant: new-", "provenance_variant_uri:"],
    )


def _build_redaction_guard_case(ordinal: int) -> VisualBenchmarkCase:
    token = _case_token("redaction_guard", ordinal)
    phone = f"+1 (555) 010-{1000 + ordinal:04d}"
    email = f"owner-{ordinal:03d}@example.com"
    summary = f"contact escalation board {token}"
    return VisualBenchmarkCase(
        case_id=f"redaction_guard_{ordinal:03d}",
        family="redaction_guard",
        complexity="complex",
        description="Sensitive OCR and rationale values should be redacted on readback",
        query=f"{token} escalation board",
        store_args=[
            "--media-ref",
            _media_ref(f"redaction-guard-{ordinal:03d}"),
            "--summary",
            summary,
            "--ocr",
            f"{token} call {phone}",
            "--why-relevant",
            f"Contact {email} before release freeze for {token}",
            "--scene",
            _choice(SCENES, ordinal, 5),
        ],
        expected_get_substrings=[summary, "[REDACTED_PHONE]", "[REDACTED_EMAIL]"],
    )


def _build_source_observed_case(ordinal: int) -> VisualBenchmarkCase:
    token = _case_token("source_observed", ordinal)
    channel = _choice(CHANNELS, ordinal)
    observed_at = _iso_observed_at(ordinal)
    return VisualBenchmarkCase(
        case_id=f"source_observed_{ordinal:03d}",
        family="source_observed",
        complexity="medium",
        description="Source channel and observed timestamp should persist in the visual record",
        query=f"{token} dispatch lane",
        store_args=[
            "--media-ref",
            _media_ref(f"source-observed-{ordinal:03d}"),
            "--summary",
            f"dispatch lane snapshot {token}",
            "--source-channel",
            channel,
            "--observed-at",
            observed_at,
            "--scene",
            _choice(SCENES, ordinal, 6),
        ],
        expected_get_substrings=[f"- source_channel: {channel}", f"- observed_at: {observed_at}"],
    )


def _build_raw_media_mixed_case(ordinal: int) -> VisualBenchmarkCase:
    token = _case_token("raw_media_mixed", ordinal)
    variant = ordinal % 4
    if variant == 1:
        media_ref = _data_url_media_ref(token, mime="image/jpeg")
        description = "JPEG data URLs should be sanitized into MIME-scoped hashes"
        forbidden = [media_ref, "data:image/jpeg;base64,"]
        coverage_key = "raw_media_data_jpeg"
    elif variant == 2:
        media_ref = _data_url_media_ref(token, mime="image/webp")
        description = "WebP data URLs should be sanitized into MIME-scoped hashes"
        forbidden = [media_ref, "data:image/webp;base64,"]
        coverage_key = "raw_media_data_webp"
    elif variant == 3:
        media_ref = _blob_media_ref(token)
        description = "Blob media refs should remain readable and searchable"
        forbidden = []
        coverage_key = "raw_media_blob"
    else:
        media_ref = _data_url_media_ref(token, mime="image/png")
        description = "PNG data URLs should be sanitized into MIME-scoped hashes"
        forbidden = [media_ref, "data:image/png;base64,"]
        coverage_key = "raw_media_data_png"

    return VisualBenchmarkCase(
        case_id=f"raw_media_mixed_{ordinal:03d}",
        family="raw_media_mixed",
        complexity="medium",
        description=description,
        query=f"{token} raw media lane",
        store_args=[
            "--media-ref",
            media_ref,
            "--summary",
            f"raw media lane {token}",
            "--ocr",
            f"{token} raw media ref coverage",
            "--scene",
            f"raw media regression wall {token}",
        ],
        expected_get_substrings=[f"- media_ref: {_expected_sanitized_media_ref(media_ref)}"],
        forbidden_get_substrings=forbidden,
        coverage_key=coverage_key,
    )


def _build_raw_media_presigned_case(ordinal: int) -> VisualBenchmarkCase:
    token = _case_token("raw_media_presigned", ordinal)
    media_ref = _presigned_media_ref(token)
    return VisualBenchmarkCase(
        case_id=f"raw_media_presigned_{ordinal:03d}",
        family="raw_media_presigned",
        complexity="complex",
        description="Long presigned media refs should be reduced to a stable hash without leaking query tokens",
        query=f"{token} presigned lane",
        store_args=[
            "--media-ref",
            media_ref,
            "--summary",
            f"presigned media lane {token}",
            "--ocr",
            f"{token} presigned raw media coverage",
            "--scene",
            f"presigned regression wall {token}",
        ],
        expected_get_substrings=[f"- media_ref: {_expected_sanitized_media_ref(media_ref)}"],
        forbidden_get_substrings=[media_ref, "X-Amz-Security-Token=", "X-Amz-Signature="],
        coverage_key="raw_media_presigned",
    )


def _build_mixed_dense_case(ordinal: int) -> VisualBenchmarkCase:
    token = _case_token("mixed_dense", ordinal)
    person_a = _choice(PEOPLE, ordinal, 1)
    person_b = _choice(PEOPLE, ordinal, 5)
    topic = _choice(RELEASE_TOPICS, ordinal, 2)
    scene = f"{_choice(SCENES, ordinal, 7)} {token}"
    why_relevant = f"Used to brief {person_a} and {person_b} before {topic} for {token}"
    return VisualBenchmarkCase(
        case_id=f"mixed_dense_{ordinal:03d}",
        family="mixed_dense",
        complexity="complex",
        description="Dense multi-field record with summary, OCR, scene, entities, and rationale",
        query=f"{person_a} {token} {topic.split()[0]}",
        store_args=[
            "--media-ref",
            _media_ref(f"mixed-dense-{ordinal:03d}"),
            "--summary",
            f"dense rollout board {token}",
            "--ocr",
            f"{token} {topic} owner matrix",
            "--scene",
            scene,
            "--entities",
            f"{person_a},{person_b}",
            "--why-relevant",
            why_relevant,
            "--source-channel",
            _choice(CHANNELS, ordinal, 2),
            "--observed-at",
            _iso_observed_at(ordinal + 30),
        ],
        expected_get_substrings=[scene, f"{person_a}, {person_b}", why_relevant],
    )


FAMILY_BUILDERS: dict[str, Callable[[int], VisualBenchmarkCase]] = {
    "ocr_exact": _build_ocr_exact_case,
    "summary_overlap": _build_summary_overlap_case,
    "scene_entity": _build_scene_entity_case,
    "visual_context_only": _build_visual_context_only_case,
    "duplicate_new": _build_duplicate_new_case,
    "redaction_guard": _build_redaction_guard_case,
    "source_observed": _build_source_observed_case,
    "raw_media_mixed": _build_raw_media_mixed_case,
    "raw_media_presigned": _build_raw_media_presigned_case,
    "mixed_dense": _build_mixed_dense_case,
}


def default_cases(total_cases: int = DEFAULT_CASE_COUNT) -> list[VisualBenchmarkCase]:
    distribution = _family_distribution(total_cases)
    cases: list[VisualBenchmarkCase] = []
    for family, count in distribution.items():
        builder = FAMILY_BUILDERS[family]
        for ordinal in range(1, count + 1):
            cases.append(builder(ordinal))
    return cases


def select_cases(cases: list[VisualBenchmarkCase], case_limit: int | None) -> list[VisualBenchmarkCase]:
    if case_limit is None or case_limit <= 0 or case_limit >= len(cases):
        return list(cases)
    by_family: dict[str, list[VisualBenchmarkCase]] = defaultdict(list)
    family_order: list[str] = []
    for case in cases:
        if case.family not in by_family:
            family_order.append(case.family)
        by_family[case.family].append(case)
    offsets = {family: 0 for family in family_order}
    selected: list[VisualBenchmarkCase] = []
    while len(selected) < case_limit:
        progressed = False
        for family in family_order:
            idx = offsets[family]
            if idx >= len(by_family[family]):
                continue
            selected.append(by_family[family][idx])
            offsets[family] += 1
            progressed = True
            if len(selected) >= case_limit:
                break
        if not progressed:
            break
    return selected


def count_cases_by_attr(cases: list[VisualBenchmarkCase], attr: str) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for case in cases:
        value = str(getattr(case, attr) or "").strip()
        if attr == "coverage_key" and not value:
            continue
        if not value:
            continue
        counts[value] += 1
    return dict(sorted(counts.items()))


def summarize_case_catalog(cases: list[VisualBenchmarkCase]) -> dict[str, Any]:
    return {
        "total_cases": len(cases),
        "family_counts": count_cases_by_attr(cases, "family"),
        "complexity_counts": count_cases_by_attr(cases, "complexity"),
        "coverage_counts": count_cases_by_attr(cases, "coverage_key"),
    }


def percentile_ms(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(round((len(ordered) - 1) * p))))
    return round(ordered[index], 3)


def _timed_run(
    cmd: list[str], *, env: dict[str, str], cwd: Path, timeout: int = 300
) -> tuple[Any, float]:
    started_at = time.perf_counter()
    proc = smoke.run(cmd, env=env, cwd=cwd, timeout=timeout)
    elapsed_ms = round((time.perf_counter() - started_at) * 1000, 3)
    return proc, elapsed_ms


def is_transient_lock_output(stdout: str, stderr: str) -> bool:
    text = f"{stdout}\n{stderr}".lower()
    return any(marker in text for marker in TRANSIENT_LOCK_MARKERS)


def _timed_run_with_lock_retry(
    cmd: list[str],
    *,
    env: dict[str, str],
    cwd: Path,
    timeout: int = 300,
    max_attempts: int = 4,
    base_sleep_seconds: float = 0.4,
) -> tuple[subprocess.CompletedProcess[str], float]:
    total_elapsed_ms = 0.0
    last_proc: subprocess.CompletedProcess[str] | None = None
    for attempt in range(1, max_attempts + 1):
        proc, elapsed_ms = _timed_run(cmd, env=env, cwd=cwd, timeout=timeout)
        total_elapsed_ms += elapsed_ms
        last_proc = proc
        if not is_transient_lock_output(proc.stdout or "", proc.stderr or ""):
            return proc, round(total_elapsed_ms, 3)
        if attempt < max_attempts:
            time.sleep(base_sleep_seconds * attempt)
    if last_proc is None:
        raise RuntimeError("lock-retry runner exited without executing a command")
    return last_proc, round(total_elapsed_ms, 3)


def _parse_json_stdout(stdout: str, stderr: str = "") -> dict[str, Any]:
    payload = extract_json_from_streams(stdout, stderr)
    return payload if isinstance(payload, dict) else {"value": payload}


def _extract_path_or_uri(payload: dict[str, Any]) -> tuple[str | None, str | None]:
    path = payload.get("path")
    uri = payload.get("uri")
    return (
        str(path) if isinstance(path, str) and path.strip() else None,
        str(uri) if isinstance(uri, str) and uri.strip() else None,
    )


def _env_enabled(env: dict[str, str], key: str) -> bool:
    return str(env.get(key) or "").strip().lower() in TRUTHY_ENV_VALUES


def _resolve_duplicate_variant_label(
    stored_uri: str | None,
    stored_path: str | None,
) -> str | None:
    for value in (stored_uri, stored_path):
        if not value:
            continue
        matched = re.search(r"new-\d{2}", value)
        if matched:
            return matched.group(0)
    return None


def _rank_for_path(search_payload: dict[str, Any], expected_path: str | None) -> float:
    if not expected_path:
        return 0.0
    results = search_payload.get("results")
    if not isinstance(results, list):
        return 0.0
    for index, item in enumerate(results[:3], start=1):
        if not isinstance(item, dict):
            continue
        path = item.get("path")
        if isinstance(path, str) and path == expected_path:
            return round(1.0 / index, 3)
    return 0.0


def _get_contains_expected_content(
    case: VisualBenchmarkCase,
    payload: dict[str, Any],
) -> bool:
    text = str(payload.get("text") or "")
    return all(
        needle in text for needle in case.expected_get_substrings
    ) and all(
        needle not in text for needle in case.forbidden_get_substrings
    )


def _result_is_successful(result: VisualBenchmarkResult) -> bool:
    return result.store_ok and result.search_hit_at_3 and result.get_contains_expected


def _result_quality_tuple(result: VisualBenchmarkResult) -> tuple[int, int, int, float]:
    return (
        1 if result.store_ok else 0,
        1 if result.search_hit_at_3 else 0,
        1 if result.get_contains_expected else 0,
        result.reciprocal_rank_at_3,
    )


def evaluate_case(
    case: VisualBenchmarkCase,
    *,
    env: dict[str, str],
    cwd: Path,
) -> VisualBenchmarkResult:
    notes: list[str] = []
    if case.prime_store_args:
        prime_cmd = [
            smoke.DEFAULT_OPENCLAW_BIN,
            "memory-palace",
            "store-visual",
            *case.prime_store_args,
            "--json",
        ]
        prime_proc, _ = _timed_run_with_lock_retry(prime_cmd, env=env, cwd=cwd)
        if prime_proc.returncode != 0:
            notes.append("prime_store_failed")

    store_cmd = [
        smoke.DEFAULT_OPENCLAW_BIN,
        "memory-palace",
        "store-visual",
        *case.store_args,
        "--json",
    ]
    store_proc, store_latency_ms = _timed_run_with_lock_retry(store_cmd, env=env, cwd=cwd)
    store_ok = store_proc.returncode == 0
    store_payload = (
        _parse_json_stdout(store_proc.stdout, store_proc.stderr)
        if (store_proc.stdout.strip() or store_proc.stderr.strip())
        else {}
    )
    stored_path, stored_uri = _extract_path_or_uri(store_payload)
    if not store_ok:
        notes.append(f"store_failed:{store_payload.get('error') or store_proc.stderr.strip()}")
    effective_query = case.query
    if case.family == "duplicate_new":
        variant_label = _resolve_duplicate_variant_label(stored_uri, stored_path)
        if variant_label:
            effective_query = re.sub(
                r"new-\d{2}(?!.*new-\d{2})",
                variant_label,
                effective_query,
            )

    search_cmd = [
        smoke.DEFAULT_OPENCLAW_BIN,
        "memory-palace",
        "search",
        effective_query,
        "--json",
    ]
    search_proc, search_latency_ms = _timed_run_with_lock_retry(search_cmd, env=env, cwd=cwd)
    search_payload = (
        _parse_json_stdout(search_proc.stdout, search_proc.stderr)
        if (search_proc.stdout.strip() or search_proc.stderr.strip())
        else {}
    )
    search_rr = _rank_for_path(search_payload, stored_path)
    search_hit = search_rr > 0
    if not search_hit:
        notes.append("search_miss_at_3")

    get_cmd_target = stored_path or stored_uri or ""
    get_latency_ms = 0.0
    get_contains_expected = False
    get_payload: dict[str, Any] = {}
    if get_cmd_target:
        get_cmd = [
            smoke.DEFAULT_OPENCLAW_BIN,
            "memory-palace",
            "get",
            get_cmd_target,
            "--json",
        ]
        get_proc, get_latency_ms = _timed_run_with_lock_retry(get_cmd, env=env, cwd=cwd)
        if get_proc.returncode == 0 and get_proc.stdout.strip():
            get_payload = _parse_json_stdout(get_proc.stdout, get_proc.stderr)
            get_contains_expected = _get_contains_expected_content(case, get_payload)
        if not get_contains_expected:
            notes.append("get_missing_expected_content")
    else:
        notes.append("missing_store_path")

    should_retry_after_index = (
        bool(get_cmd_target)
        and _env_enabled(env, "RUNTIME_INDEX_DEFER_ON_WRITE")
        and (not store_ok or not search_hit or not get_contains_expected)
    )
    if should_retry_after_index:
        index_cmd = [
            smoke.DEFAULT_OPENCLAW_BIN,
            "memory-palace",
            "index",
            "--wait",
            "--json",
        ]
        for attempt in range(1, 3):
            index_proc, index_latency_ms = _timed_run_with_lock_retry(index_cmd, env=env, cwd=cwd)
            if index_proc.returncode != 0:
                break

            if attempt > 1:
                time.sleep(0.35 * attempt)

            search_proc_retry, retry_search_latency_ms = _timed_run_with_lock_retry(
                search_cmd,
                env=env,
                cwd=cwd,
            )
            search_payload_retry = (
                _parse_json_stdout(search_proc_retry.stdout, search_proc_retry.stderr)
                if search_proc_retry.stdout.strip()
                else {}
            )
            search_rr_retry = _rank_for_path(search_payload_retry, stored_path)
            if search_rr_retry > 0:
                search_payload = search_payload_retry
                search_rr = search_rr_retry
                search_hit = True
                search_latency_ms = round(
                    search_latency_ms + index_latency_ms + retry_search_latency_ms,
                    3,
                )
                notes = [note for note in notes if note != "search_miss_at_3"]
                if "search_recovered_after_index_wait" not in notes:
                    notes.append("search_recovered_after_index_wait")

            if get_cmd_target:
                get_proc_retry, retry_get_latency_ms = _timed_run_with_lock_retry(
                    get_cmd,
                    env=env,
                    cwd=cwd,
                )
                if get_proc_retry.returncode == 0 and get_proc_retry.stdout.strip():
                    get_payload = _parse_json_stdout(
                        get_proc_retry.stdout,
                        get_proc_retry.stderr,
                    )
                    if _get_contains_expected_content(case, get_payload):
                        get_contains_expected = True
                        get_latency_ms = round(get_latency_ms + retry_get_latency_ms, 3)
                        notes = [note for note in notes if note != "get_missing_expected_content"]
                        if "get_recovered_after_index_wait" not in notes:
                            notes.append("get_recovered_after_index_wait")
                        if not store_ok:
                            store_ok = True
                            notes = [
                                note
                                for note in notes
                                if not note.startswith("store_failed:")
                            ]
                            if "store_recovered_after_index_wait" not in notes:
                                notes.append("store_recovered_after_index_wait")

            if store_ok and search_hit and get_contains_expected:
                break

    return VisualBenchmarkResult(
        case_id=case.case_id,
        family=case.family,
        complexity=case.complexity,
        description=case.description,
        store_ok=store_ok,
        search_hit_at_3=search_hit,
        reciprocal_rank_at_3=search_rr,
        get_contains_expected=get_contains_expected,
        store_latency_ms=store_latency_ms,
        search_latency_ms=search_latency_ms,
        get_latency_ms=get_latency_ms,
        stored_path=stored_path,
        stored_uri=stored_uri,
        query=effective_query,
        notes=notes,
        coverage_key=case.coverage_key,
    )


def probe_runtime_visual_harvest(*, cwd: Path) -> dict[str, Any]:
    bun_bin = shutil.which("bun")
    if not bun_bin:
        return {
            "runtime_visual_probe": "none",
            "runtime_visual_harvest_success_rate": 0.0,
            "runtime_visual_harvest_cases": [
                {"case_id": "probe_runtime_visual_harvest", "ok": False, "reason": "bun_unavailable"}
            ],
        }
    cases: list[dict[str, Any]] = []
    for hook in ("message:preprocessed", "before_prompt_build", "agent_end"):
        proc = smoke.run(
            [bun_bin, "scripts/openclaw_runtime_visual_probe.ts", "--hook", hook],
            cwd=cwd,
            timeout=120,
        )
        if proc.returncode != 0:
            cases.append(
                {
                    "case_id": hook.replace(":", "_"),
                    "ok": False,
                    "probe": "none",
                    "reason": (proc.stderr or proc.stdout).strip() or "bun_probe_failed",
                }
            )
            continue
        payload = _parse_json_stdout(proc.stdout.strip() or "{}", proc.stderr)
        cases.append(
            {
                "case_id": hook.replace(":", "_"),
                "ok": bool(payload.get("ok")),
                "probe": str(payload.get("runtime_visual_probe") or "none"),
                "hook": hook,
            }
        )
    success_rate = round(sum(1 for entry in cases if entry.get("ok")) / len(cases), 3) if cases else 0.0
    preferred = next(
        (
            entry["probe"]
            for entry in cases
            if entry.get("ok")
            and entry.get("probe") in {"message_preprocessed", "tool_context_only"}
        ),
        None,
    )
    fallback_probe = next(
        (str(entry.get("probe") or "none") for entry in cases if entry.get("ok")),
        "none",
    )
    return {
        "runtime_visual_probe": preferred or fallback_probe,
        "runtime_visual_harvest_success_rate": success_rate,
        "runtime_visual_harvest_cases": cases,
    }


def _summarize_by_key(
    results: list[VisualBenchmarkResult], key: str, *, skip_empty: bool = False
) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[VisualBenchmarkResult]] = defaultdict(list)
    for item in results:
        group_key = getattr(item, key)
        if skip_empty and not group_key:
            continue
        grouped[group_key].append(item)
    summary: dict[str, dict[str, Any]] = {}
    for group, items in sorted(grouped.items()):
        total = len(items)
        summary[group] = {
            "cases": total,
            "store_success_rate": round(sum(1 for item in items if item.store_ok) / total, 3),
            "search_hit_at_3_rate": round(sum(1 for item in items if item.search_hit_at_3) / total, 3),
            "get_contains_expected_rate": round(
                sum(1 for item in items if item.get_contains_expected) / total, 3
            ),
        }
    return summary


def parse_required_coverage(raw: str | None) -> list[str]:
    if not raw:
        return []
    required: list[str] = []
    seen: set[str] = set()
    for token in raw.split(","):
        normalized = token.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        required.append(normalized)
    return required


def build_coverage_gate_status(
    metrics: dict[str, Any],
    required_keys: list[str],
) -> dict[str, Any]:
    coverage_summary = metrics.get("coverage_summary")
    coverage_summary = coverage_summary if isinstance(coverage_summary, dict) else {}
    missing_keys: list[str] = []
    failing_keys: dict[str, dict[str, Any]] = {}
    for coverage_key in required_keys:
        entry = coverage_summary.get(coverage_key)
        if not isinstance(entry, dict):
            missing_keys.append(coverage_key)
            continue
        if not all(
            entry.get(metric_key) == 1.0
            for metric_key in (
                "store_success_rate",
                "search_hit_at_3_rate",
                "get_contains_expected_rate",
            )
        ):
            failing_keys[coverage_key] = {
                "store_success_rate": entry.get("store_success_rate"),
                "search_hit_at_3_rate": entry.get("search_hit_at_3_rate"),
                "get_contains_expected_rate": entry.get("get_contains_expected_rate"),
                "cases": entry.get("cases"),
            }
    return {
        "required_keys": list(required_keys),
        "missing_keys": missing_keys,
        "failing_keys": failing_keys,
        "passed": not missing_keys and not failing_keys,
    }


def compute_metrics(
    results: list[VisualBenchmarkResult], runtime_probe: dict[str, Any] | None = None
) -> dict[str, Any]:
    total = len(results)
    store_successes = sum(1 for item in results if item.store_ok)
    search_hits = sum(1 for item in results if item.search_hit_at_3)
    get_hits = sum(1 for item in results if item.get_contains_expected)
    duplicate_new = [item for item in results if item.family == "duplicate_new"]
    visual_context = [item for item in results if item.family == "visual_context_only"]
    metrics = {
        "total_cases": total,
        "store_success_rate": round(store_successes / total, 3) if total else 0.0,
        "search_hit_at_3_rate": round(search_hits / total, 3) if total else 0.0,
        "mrr_at_3": round(
            sum(item.reciprocal_rank_at_3 for item in results) / total, 3
        )
        if total
        else 0.0,
        "get_contains_expected_rate": round(get_hits / total, 3) if total else 0.0,
        "duplicate_new_success_rate": round(
            sum(1 for item in duplicate_new if item.store_ok) / len(duplicate_new), 3
        )
        if duplicate_new
        else None,
        "visual_context_reuse_success_rate": round(
            sum(1 for item in visual_context if item.get_contains_expected)
            / len(visual_context),
            3,
        )
        if visual_context
        else None,
        "store_p95_ms": percentile_ms([item.store_latency_ms for item in results], 0.95),
        "search_p95_ms": percentile_ms(
            [item.search_latency_ms for item in results], 0.95
        ),
        "get_p95_ms": percentile_ms([item.get_latency_ms for item in results], 0.95),
        "runtime_visual_probe": "cli_store_visual_only",
        "runtime_visual_harvest_success_rate": 0.0,
        "runtime_visual_harvest_cases": [],
        "family_summary": _summarize_by_key(results, "family"),
        "complexity_summary": _summarize_by_key(results, "complexity"),
        "coverage_summary": _summarize_by_key(results, "coverage_key", skip_empty=True),
    }
    if runtime_probe:
        metrics.update(runtime_probe)
    return metrics


def _render_distribution_lines(title: str, payload: dict[str, Any]) -> list[str]:
    lines = ["", f"## {title}", ""]
    if not payload:
        lines.append("- none")
        return lines
    for key, value in payload.items():
        if isinstance(value, dict):
            lines.append(
                f"- {key}: cases={value.get('cases')} store={value.get('store_success_rate')} "
                f"hit@3={value.get('search_hit_at_3_rate')} get={value.get('get_contains_expected_rate')}"
            )
        else:
            lines.append(f"- {key}: {value}")
    return lines


def _render_family_gate(metrics: dict[str, Any], family: str) -> str:
    family_summary = metrics.get("family_summary")
    if not isinstance(family_summary, dict):
        return "missing"
    family_metrics = family_summary.get(family)
    if not isinstance(family_metrics, dict) or int(family_metrics.get("cases") or 0) <= 0:
        return "missing"
    if _family_metric_is_full_success(metrics, family):
        return "pass"
    return (
        f"fail(store={family_metrics.get('store_success_rate')}, "
        f"hit@3={family_metrics.get('search_hit_at_3_rate')}, "
        f"get={family_metrics.get('get_contains_expected_rate')})"
    )


def _family_metric_is_full_success(metrics: dict[str, Any], family: str) -> bool:
    family_summary = metrics.get("family_summary")
    if not isinstance(family_summary, dict):
        return False
    family_metrics = family_summary.get(family)
    if not isinstance(family_metrics, dict) or int(family_metrics.get("cases") or 0) <= 0:
        return False
    return all(
        family_metrics.get(metric_key) == 1.0
        for metric_key in (
            "store_success_rate",
            "search_hit_at_3_rate",
            "get_contains_expected_rate",
        )
    )


def build_markdown_report(
    *,
    profile: str,
    results: list[VisualBenchmarkResult],
    metrics: dict[str, Any],
    case_catalog_size: int | None = None,
    executed_case_count: int | None = None,
    coverage_gate: dict[str, Any] | None = None,
) -> str:
    lines = [
        "# OpenClaw Visual Memory Benchmark",
        "",
        f"- profile: `{profile}`",
        f"- case_catalog_size: `{case_catalog_size if case_catalog_size is not None else len(results)}`",
        f"- executed_case_count: `{executed_case_count if executed_case_count is not None else len(results)}`",
        f"- total_cases: `{metrics['total_cases']}`",
        f"- store_success_rate: `{metrics['store_success_rate']}`",
        f"- search_hit_at_3_rate: `{metrics['search_hit_at_3_rate']}`",
        f"- mrr_at_3: `{metrics['mrr_at_3']}`",
        f"- get_contains_expected_rate: `{metrics['get_contains_expected_rate']}`",
        f"- duplicate_new_success_rate: `{metrics['duplicate_new_success_rate']}`",
        f"- visual_context_reuse_success_rate: `{metrics['visual_context_reuse_success_rate']}`",
        f"- store_p95_ms: `{metrics['store_p95_ms']}`",
        f"- search_p95_ms: `{metrics['search_p95_ms']}`",
        f"- get_p95_ms: `{metrics['get_p95_ms']}`",
        f"- runtime_visual_probe: `{metrics['runtime_visual_probe']}`",
        f"- runtime_visual_harvest_success_rate: `{metrics['runtime_visual_harvest_success_rate']}`",
        f"- raw_media_mixed_gate: `{_render_family_gate(metrics, 'raw_media_mixed')}`",
        f"- raw_media_presigned_gate: `{_render_family_gate(metrics, 'raw_media_presigned')}`",
    ]
    lines.extend(_render_distribution_lines("Family Coverage", metrics.get("family_summary", {})))
    lines.extend(_render_distribution_lines("Complexity Coverage", metrics.get("complexity_summary", {})))
    lines.extend(_render_distribution_lines("Raw Media Coverage", metrics.get("coverage_summary", {})))
    if coverage_gate and coverage_gate.get("required_keys"):
        lines.extend(
            [
                "",
                "## Required Coverage",
                "",
                f"- passed: `{str(bool(coverage_gate.get('passed'))).lower()}`",
                f"- required_keys: `{', '.join(coverage_gate.get('required_keys', []))}`",
                f"- missing_keys: `{', '.join(coverage_gate.get('missing_keys', [])) or '-'}`",
            ]
        )
        failing_keys = coverage_gate.get("failing_keys", {})
        if failing_keys:
            lines.extend(["", "| Coverage Key | Cases | Store | Hit@3 | Get OK |", "|---|---:|---:|---:|---:|"])
            for coverage_key, entry in sorted(failing_keys.items()):
                lines.append(
                    f"| {coverage_key} | {entry.get('cases', '-')} | "
                    f"{entry.get('store_success_rate', '-')} | "
                    f"{entry.get('search_hit_at_3_rate', '-')} | "
                    f"{entry.get('get_contains_expected_rate', '-')} |"
                )
    lines.extend(
        [
            "",
            "| Case | Family | Complexity | Store | Hit@3 | RR@3 | Get OK | Notes |",
            "|---|---|---|---:|---:|---:|---:|---|",
        ]
    )
    for item in results:
        lines.append(
            f"| {item.case_id} | {item.family} | {item.complexity} | "
            f"{str(item.store_ok).lower()} | {str(item.search_hit_at_3).lower()} | "
            f"{item.reciprocal_rank_at_3:.3f} | {str(item.get_contains_expected).lower()} | "
            f"{', '.join(item.notes) or '-'} |"
        )
    if metrics.get("runtime_visual_harvest_cases"):
        lines.extend(
            [
                "",
                "## Runtime Harvest Probe",
                "",
                "| Hook | Probe | OK |",
                "|---|---|---:|",
            ]
        )
        for entry in metrics["runtime_visual_harvest_cases"]:
            lines.append(
                f"| {entry.get('hook') or entry.get('case_id') or '-'} | "
                f"{entry.get('probe') or '-'} | {str(bool(entry.get('ok'))).lower()} |"
            )
    return "\n".join(lines) + "\n"


def build_matrix_markdown_report(payload: dict[str, Any]) -> str:
    profiles = payload.get("profiles", [])
    coverage_gate = payload.get("coverage_gate") if isinstance(payload, dict) else None
    lines = [
        "# OpenClaw Visual Memory Benchmark Matrix",
        "",
        f"- profiles: `{', '.join(item['profile'] for item in profiles)}`",
        f"- case_catalog_size: `{payload.get('case_catalog_size')}`",
        f"- executed_case_count_per_profile: `{payload.get('executed_case_count_per_profile')}`",
        f"- executed_case_count_total: `{payload.get('executed_case_count_total')}`",
        "",
        "| Profile | Store | Hit@3 | MRR@3 | Get OK | Raw Mixed | Raw Presigned | Runtime Probe | Harvest OK |",
        "|---|---:|---:|---:|---:|---|---|---|---:|",
    ]
    for item in profiles:
        metrics = item["metrics"]
        lines.append(
            f"| {item['profile']} | {metrics['store_success_rate']} | {metrics['search_hit_at_3_rate']} | "
            f"{metrics['mrr_at_3']} | {metrics['get_contains_expected_rate']} | "
            f"{_render_family_gate(metrics, 'raw_media_mixed')} | "
            f"{_render_family_gate(metrics, 'raw_media_presigned')} | "
            f"{metrics['runtime_visual_probe']} | {metrics['runtime_visual_harvest_success_rate']} |"
        )
    lines.extend(_render_distribution_lines("Family Coverage", payload.get("family_summary", {})))
    lines.extend(_render_distribution_lines("Complexity Coverage", payload.get("complexity_summary", {})))
    lines.extend(_render_distribution_lines("Raw Media Coverage", payload.get("coverage_summary", {})))
    if coverage_gate and coverage_gate.get("required_keys"):
        lines.extend(
            [
                "",
                "## Required Coverage",
                "",
                f"- passed: `{str(bool(coverage_gate.get('passed'))).lower()}`",
                f"- required_keys: `{', '.join(coverage_gate.get('required_keys', []))}`",
            ]
        )
        profile_status = coverage_gate.get("profiles", {})
        if isinstance(profile_status, dict) and profile_status:
            lines.extend(
                [
                    "",
                    "| Profile | Passed | Missing | Failing |",
                    "|---|---:|---|---|",
                ]
            )
            for profile, entry in sorted(profile_status.items()):
                missing_keys = ", ".join(entry.get("missing_keys", [])) or "-"
                failing_keys = ", ".join(sorted(entry.get("failing_keys", {}).keys())) or "-"
                lines.append(
                    f"| {profile} | {str(bool(entry.get('passed'))).lower()} | "
                    f"{missing_keys} | {failing_keys} |"
                )
    return "\n".join(lines) + "\n"


def run_profile_benchmark(
    profile: str,
    model_env: dict[str, str],
    cases: list[VisualBenchmarkCase],
    *,
    required_coverage: list[str] | None = None,
    recorder: VisualBenchmarkRunRecorder | None = None,
    stop_requested: threading.Event | None = None,
) -> dict[str, Any]:
    existing_results = recorder.existing_result_objects(profile) if recorder else []
    existing_runtime_probe = recorder.existing_runtime_probe(profile) if recorder else None
    completed_case_ids = recorder.completed_case_ids(profile) if recorder else set()
    remaining_cases = [case for case in cases if case.case_id not in completed_case_ids]

    if recorder:
        recorder.mark_profile_started(profile)

    if not remaining_cases:
        metrics = compute_metrics(existing_results, runtime_probe=existing_runtime_probe)
        coverage_gate = build_coverage_gate_status(metrics, required_coverage or [])
        payload = {
            "profile": profile,
            "case_catalog_size": len(cases),
            "executed_case_count": len(existing_results),
            "status": "completed",
            "interrupted": False,
            "metrics": metrics,
            "runtime_probe": existing_runtime_probe,
            "coverage_gate": coverage_gate,
            "results": [asdict(item) for item in existing_results],
        }
        if recorder:
            recorder.mark_profile_finished(
                profile,
                runtime_probe=existing_runtime_probe,
                status="completed",
                progress_line=(
                    f"[progress] profile={profile} status=completed completed_cases={len(existing_results)}/{len(cases)} "
                    "resume=skipped"
                ),
            )
        return payload

    try:
        payload = run_local_benchmark(
            profile,
            model_env,
            remaining_cases,
            required_coverage=required_coverage,
            progress_callback=(
                (lambda current_profile, result: recorder.record_case_result(current_profile, result))
                if recorder
                else None
            ),
            case_started_callback=(
                (lambda current_profile, case: recorder.mark_case_started(current_profile, case))
                if recorder
                else None
            ),
            stop_requested=stop_requested,
        )
    except Exception as exc:
        if recorder:
            recorder.mark_profile_finished(
                profile,
                runtime_probe=None,
                status="failed",
                error=str(exc),
            )
        raise

    combined_results = existing_results + [
        VisualBenchmarkResult(**item) for item in payload.get("results", [])
    ]
    combined_runtime_probe = payload.get("runtime_probe") or existing_runtime_probe
    combined_metrics = compute_metrics(combined_results, runtime_probe=combined_runtime_probe)
    combined_coverage_gate = build_coverage_gate_status(combined_metrics, required_coverage or [])
    combined_payload = {
        "profile": profile,
        "case_catalog_size": len(cases),
        "executed_case_count": len(combined_results),
        "status": payload.get("status") or "completed",
        "interrupted": bool(payload.get("interrupted")),
        "metrics": combined_metrics,
        "runtime_probe": combined_runtime_probe,
        "coverage_gate": combined_coverage_gate,
        "results": [asdict(item) for item in combined_results],
    }

    if recorder:
        recorder.mark_profile_finished(
            profile,
            runtime_probe=combined_runtime_probe,
            status=str(combined_payload.get("status") or "completed"),
            error=str(combined_payload.get("error") or "") or None,
        )
    return combined_payload


def run_local_benchmark(
    profile: str,
    model_env: dict[str, str],
    cases: list[VisualBenchmarkCase],
    *,
    required_coverage: list[str] | None = None,
    progress_callback: Callable[[str, VisualBenchmarkResult], None] | None = None,
    case_started_callback: Callable[[str, VisualBenchmarkCase], None] | None = None,
    stop_requested: threading.Event | None = None,
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix=f"mp-openclaw-visual-bench-{profile}-") as tmp:
        tmp_path = Path(tmp)
        env_file = tmp_path / f"profile-{profile}.env"
        config_path = tmp_path / f"openclaw-{profile}.json"
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        env_values = smoke.build_profile_env(smoke.local_native_platform_name(), profile, env_file, model_env)
        smoke.seed_local_memory(env_values["DATABASE_URL"])
        smoke.build_openclaw_config(
            config_path,
            transport="stdio",
            workspace_dir=tmp_path / "workspace",
            stdio_env=env_values,
        )

        env = dict(os.environ)
        env["OPENCLAW_CONFIG_PATH"] = str(config_path)
        env["OPENCLAW_STATE_DIR"] = str(state_dir)

        results: list[VisualBenchmarkResult] = []
        interrupted = False
        for case in cases:
            if stop_requested and stop_requested.is_set():
                interrupted = True
                break
            if case_started_callback:
                case_started_callback(profile, case)
            result = evaluate_case(case, env=env, cwd=PROJECT_ROOT)
            if not _result_is_successful(result):
                best_result = result
                for attempt in range(1, 3):
                    time.sleep(0.35 * attempt)
                    retried = evaluate_case(case, env=env, cwd=PROJECT_ROOT)
                    if _result_quality_tuple(retried) >= _result_quality_tuple(best_result):
                        retried.notes = list(retried.notes)
                        retried.notes.append("case_retried_after_failure")
                        if _result_is_successful(retried):
                            retried.notes.append("case_recovered_after_full_retry")
                        best_result = retried
                    if _result_is_successful(best_result):
                        break
                result = best_result
            results.append(result)
            if progress_callback:
                progress_callback(profile, result)
            if stop_requested and stop_requested.is_set():
                interrupted = True
                break

        runtime_probe = (
            None
            if interrupted or (stop_requested and stop_requested.is_set())
            else probe_runtime_visual_harvest(cwd=PROJECT_ROOT)
        )
        metrics = compute_metrics(results, runtime_probe=runtime_probe)
        coverage_gate = build_coverage_gate_status(metrics, required_coverage or [])
        return {
            "profile": profile,
            "case_catalog_size": len(cases),
            "executed_case_count": len(results),
            "status": "interrupted" if interrupted else "completed",
            "interrupted": interrupted,
            "metrics": metrics,
            "runtime_probe": runtime_probe,
            "coverage_gate": coverage_gate,
            "results": [asdict(item) for item in results],
        }


def run_benchmark_matrix(
    profiles: list[str],
    model_env: dict[str, str],
    cases: list[VisualBenchmarkCase],
    max_workers: int = 1,
    *,
    required_coverage: list[str] | None = None,
    recorder: VisualBenchmarkRunRecorder | None = None,
    stop_requested: threading.Event | None = None,
) -> dict[str, Any]:
    effective_workers = max(1, min(max_workers, len(profiles)))
    if effective_workers == 1:
        results = [
            run_profile_benchmark(
                profile,
                model_env,
                cases,
                required_coverage=required_coverage,
                recorder=recorder,
                stop_requested=stop_requested,
            )
            for profile in profiles
        ]
    else:
        indexed_results: dict[int, dict[str, Any]] = {}
        errors: list[str] = []
        with ThreadPoolExecutor(max_workers=effective_workers) as executor:
            future_map = {
                executor.submit(
                    run_profile_benchmark,
                    profile,
                    model_env,
                    cases,
                    required_coverage=required_coverage,
                    recorder=recorder,
                    stop_requested=stop_requested,
                ): index
                for index, profile in enumerate(profiles)
            }
            for future in as_completed(future_map):
                try:
                    indexed_results[future_map[future]] = future.result()
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{profiles[future_map[future]]}: {exc}")
                    if stop_requested:
                        stop_requested.set()
        if errors:
            raise RuntimeError("; ".join(errors))
        results = [indexed_results[index] for index in range(len(profiles))]
    return {
        "profiles": results,
        "case_catalog_size": len(cases),
        "executed_case_count_per_profile": len(cases),
        "executed_case_count_total": len(cases) * len(results),
        "family_summary": count_cases_by_attr(cases, "family"),
        "complexity_summary": count_cases_by_attr(cases, "complexity"),
        "coverage_summary": count_cases_by_attr(cases, "coverage_key"),
    }


def load_resume_payload(
    *,
    json_output: Path,
    profiles: list[str],
    selected_cases: list[VisualBenchmarkCase],
) -> dict[str, Any] | None:
    if not json_output.is_file():
        return None
    payload = json.loads(json_output.read_text(encoding="utf-8"))
    existing_profiles = [
        str(item.get("profile") or "")
        for item in payload.get("profiles", [])
        if isinstance(item, dict)
    ]
    if existing_profiles != profiles:
        raise ValueError(
            f"Resume artifact profile set mismatch: expected {profiles}, got {existing_profiles}."
        )
    expected_case_count = len(selected_cases)
    for item in payload.get("profiles", []):
        if not isinstance(item, dict):
            continue
        results = item.get("results", [])
        if not isinstance(results, list):
            raise ValueError("Resume artifact has invalid results payload.")
        completed_ids = [
            str(result.get("case_id") or "")
            for result in results
            if isinstance(result, dict) and isinstance(result.get("case_id"), str)
        ]
        if len(set(completed_ids)) != len(completed_ids):
            raise ValueError("Resume artifact contains duplicate case_ids.")
        if len(completed_ids) > expected_case_count:
            raise ValueError("Resume artifact reports more cases than selected for this run.")
    return payload


def _metric_is_full_success(metrics: dict[str, Any], key: str) -> bool:
    value = metrics.get(key)
    return value is None or value == 1.0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run a real OpenClaw visual-memory benchmark over store/search/get."
    )
    parser.add_argument("--profile", default="a")
    parser.add_argument("--profiles", default="")
    parser.add_argument("--model-env", default="")
    parser.add_argument("--case-count", type=int, default=DEFAULT_CASE_COUNT)
    parser.add_argument("--case-limit", type=int, default=0)
    parser.add_argument("--max-workers", type=int, default=1)
    parser.add_argument("--json-output", default=str(DEFAULT_JSON_OUTPUT))
    parser.add_argument("--markdown-output", default=str(DEFAULT_MARKDOWN_OUTPUT))
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--required-coverage",
        default="",
        help=(
            "Comma-separated coverage keys that must be present and fully green. "
            f"Release gate typically uses: {', '.join(DEFAULT_REQUIRED_COVERAGE_KEYS)}"
        ),
    )
    args = parser.parse_args()

    profiles = parse_profiles(args.profile, args.profiles)
    case_catalog = default_cases(args.case_count)
    selected_cases = select_cases(case_catalog, args.case_limit if args.case_limit > 0 else None)
    model_env = smoke.load_env_file(smoke.normalize_host_cli_path(args.model_env)) if args.model_env else {}
    json_output = smoke.normalize_host_cli_path(args.json_output)
    markdown_output = smoke.normalize_host_cli_path(args.markdown_output)
    required_coverage = parse_required_coverage(args.required_coverage)
    json_output.parent.mkdir(parents=True, exist_ok=True)
    markdown_output.parent.mkdir(parents=True, exist_ok=True)
    resume_payload = load_resume_payload(
        json_output=json_output,
        profiles=profiles,
        selected_cases=selected_cases,
    ) if args.resume else None
    recorder = (
        VisualBenchmarkRunRecorder.from_payload(
            payload=resume_payload,
            profiles=profiles,
            case_catalog=case_catalog,
            selected_cases=selected_cases,
            json_output=json_output,
            markdown_output=markdown_output,
        )
        if resume_payload
        else VisualBenchmarkRunRecorder(
            profiles=profiles,
            case_catalog=case_catalog,
            selected_cases=selected_cases,
            json_output=json_output,
            markdown_output=markdown_output,
        )
    )
    stop_requested = threading.Event()
    previous_signal_handlers: dict[int, Any] = {}

    def _request_stop(signum: int, _frame: Any) -> None:
        if stop_requested.is_set():
            return
        stop_requested.set()
        recorder.mark_run_status(
            "interrupted",
            reason=f"signal:{signum}",
        )
        print(
            f"[interrupt] received signal {signum}; finishing in-flight case(s) and writing partial artifacts.",
            file=sys.stderr,
            flush=True,
        )

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            previous_signal_handlers[sig] = signal.getsignal(sig)
            signal.signal(sig, _request_stop)
        except Exception:  # noqa: BLE001
            continue

    try:
        if len(profiles) == 1:
            payload = run_profile_benchmark(
                profiles[0],
                model_env,
                selected_cases,
                required_coverage=required_coverage,
                recorder=recorder,
                stop_requested=stop_requested,
            )
            payload["status"] = payload.get("status") or "completed"
            payload["partial"] = bool(payload.get("interrupted"))
            payload["started_at"] = recorder.started_at
            payload["updated_at"] = _utc_now_iso()
            payload["case_catalog_size"] = len(case_catalog)
            payload["executed_case_count"] = payload.get("executed_case_count", len(selected_cases))
            markdown = build_markdown_report(
                profile=payload["profile"],
                results=[VisualBenchmarkResult(**item) for item in payload["results"]],
                metrics=payload["metrics"],
                case_catalog_size=len(case_catalog),
                executed_case_count=payload["executed_case_count"],
                coverage_gate=payload["coverage_gate"],
            )
            success = (
                payload["status"] == "completed"
                and payload["metrics"]["store_success_rate"] == 1.0
                and payload["metrics"]["search_hit_at_3_rate"] == 1.0
                and payload["metrics"]["get_contains_expected_rate"] == 1.0
                and _metric_is_full_success(payload["metrics"], "duplicate_new_success_rate")
                and _metric_is_full_success(payload["metrics"], "visual_context_reuse_success_rate")
                and payload["metrics"]["runtime_visual_harvest_success_rate"] == 1.0
                and payload["coverage_gate"]["passed"]
            )
        else:
            payload = run_benchmark_matrix(
                profiles,
                model_env,
                selected_cases,
                max_workers=max(1, args.max_workers),
                required_coverage=required_coverage,
                recorder=recorder,
                stop_requested=stop_requested,
            )
            payload["status"] = "interrupted" if stop_requested.is_set() else "completed"
            payload["partial"] = stop_requested.is_set()
            payload["started_at"] = recorder.started_at
            payload["updated_at"] = _utc_now_iso()
            payload["case_catalog_size"] = len(case_catalog)
            payload["executed_case_count_per_profile"] = len(selected_cases)
            payload["executed_case_count_total"] = sum(
                int(item.get("executed_case_count") or 0) for item in payload["profiles"]
            )
            payload["coverage_gate"] = {
                "required_keys": required_coverage,
                "profiles": {
                    str(item.get("profile") or "-"): item.get("coverage_gate", {})
                    for item in payload["profiles"]
                },
            }
            payload["coverage_gate"]["passed"] = all(
                entry.get("passed") is True
                for entry in payload["coverage_gate"]["profiles"].values()
            )
            markdown = build_matrix_markdown_report(payload)
            success = (
                payload["status"] == "completed"
                and all(
                    item["metrics"]["store_success_rate"] == 1.0
                    and item["metrics"]["search_hit_at_3_rate"] == 1.0
                    and item["metrics"]["get_contains_expected_rate"] == 1.0
                    and _metric_is_full_success(item["metrics"], "duplicate_new_success_rate")
                    and _metric_is_full_success(item["metrics"], "visual_context_reuse_success_rate")
                    and item["metrics"]["runtime_visual_harvest_success_rate"] == 1.0
                    and item["coverage_gate"]["passed"]
                    for item in payload["profiles"]
                )
                and payload["coverage_gate"]["passed"]
            )

        recorder.mark_run_status("completed" if success else payload.get("status", "failed"))
        _write_text_atomic(
            json_output,
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        )
        _write_text_atomic(markdown_output, markdown)
        print(json_output)
        print(markdown_output)
        if stop_requested.is_set():
            return 130
        return 0 if success else 1
    except Exception as exc:  # noqa: BLE001
        recorder.mark_run_status("failed", reason=str(exc))
        print(json_output)
        print(markdown_output)
        raise
    finally:
        for sig, previous in previous_signal_handlers.items():
            try:
                signal.signal(sig, previous)
            except Exception:  # noqa: BLE001
                continue


if __name__ == "__main__":
    raise SystemExit(main())
