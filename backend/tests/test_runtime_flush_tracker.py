from __future__ import annotations

import pytest

from runtime_state import SessionFlushTracker


def _padded_event(prefix: str, *, fill: int) -> str:
    return f"{prefix} {'x' * fill}"


@pytest.mark.asyncio
async def test_session_flush_tracker_ignores_blank_messages_and_does_not_create_session() -> None:
    tracker = SessionFlushTracker()

    await tracker.record_event(session_id="blank", message="")
    await tracker.record_event(session_id="blank", message="   \n\t   ")

    assert "blank" not in tracker._events
    assert await tracker.pending_session_ids() == []


@pytest.mark.asyncio
async def test_session_flush_tracker_requires_both_event_and_char_thresholds() -> None:
    tracker = SessionFlushTracker()
    tracker._min_events = 3
    tracker._trigger_chars = 20

    await tracker.record_event(session_id="threshold", message="1234567890")
    await tracker.record_event(session_id="threshold", message="abcdefghij")

    # chars threshold hit, events threshold not hit
    assert await tracker.should_flush(session_id="threshold") is False

    await tracker.record_event(session_id="threshold", message="xyz")

    # both thresholds hit
    assert await tracker.should_flush(session_id="threshold") is True


@pytest.mark.asyncio
async def test_session_flush_tracker_reclaims_oldest_sessions_when_capacity_is_reached() -> None:
    tracker = SessionFlushTracker()
    tracker._max_sessions = 2
    tracker._max_events = 10

    await tracker.record_event(session_id="session-a", message="alpha")
    await tracker.record_event(session_id="session-b", message="beta")
    await tracker.record_event(session_id="session-a", message="alpha-again")
    await tracker.record_event(session_id="session-c", message="gamma")

    assert list(tracker._events.keys()) == ["session-a", "session-c"]
    assert list(tracker._events["session-a"]) == ["alpha", "alpha-again"]
    assert list(tracker._events["session-c"]) == ["gamma"]


@pytest.mark.asyncio
async def test_session_flush_tracker_should_flush_refreshes_lru_position() -> None:
    tracker = SessionFlushTracker()
    tracker._max_sessions = 2
    tracker._max_events = 10
    tracker._min_events = 1
    tracker._trigger_chars = 1

    await tracker.record_event(session_id="session-a", message="alpha")
    await tracker.record_event(session_id="session-b", message="beta")

    assert await tracker.should_flush(session_id="session-a") is True

    await tracker.record_event(session_id="session-c", message="gamma")

    assert "session-a" in tracker._events
    assert "session-b" not in tracker._events
    assert "session-c" in tracker._events


@pytest.mark.asyncio
async def test_session_flush_tracker_audits_overflow_and_clears_after_flush() -> None:
    """P3-1: overflow now compresses into rolling summary instead of dropping."""
    tracker = SessionFlushTracker()
    tracker._max_events = 2

    await tracker.record_event(session_id="session-a", message="alpha")
    await tracker.record_event(session_id="session-a", message="beta")
    await tracker.record_event(session_id="session-a", message="gamma")

    summary = await tracker.build_summary(session_id="session-a", limit=5)
    stats = await tracker.summary()

    # P3-1: "alpha" was compressed into rolling summary, not dropped
    assert "compressed into rolling summary" in summary
    assert "alpha" in summary  # preserved in rolling summary section
    assert "- beta" in summary
    assert "- gamma" in summary
    assert stats["rolled_events"] == 1
    assert stats["top_sessions"][0]["rolled_events"] == 1
    assert stats["dropped_events"] == 0  # no longer dropped, compressed instead

    await tracker.mark_flushed(session_id="session-a")

    assert await tracker.build_summary(session_id="session-a", limit=5) == ""
    post_flush = await tracker.summary()
    assert post_flush["dropped_events"] == 0
    assert post_flush["rolled_events"] == 0
    assert post_flush["pending_events"] == 0


@pytest.mark.asyncio
async def test_runtime_flush_tracker_multiline_reason_survives_rollup() -> None:
    tracker = SessionFlushTracker()
    tracker._max_events = 1
    tracker._rolling_summary_max_chars = 400

    await tracker.record_event(
        session_id="multiline-reason",
        message=(
            "workflow changed for reflection lane\n"
            "reason: provider switched to local fallback\n"
            "uri: core://reflection/agent-alpha/session-1"
        ),
    )
    await tracker.record_event(session_id="multiline-reason", message="recent tail event")

    summary = await tracker.build_summary(session_id="multiline-reason", limit=1)

    assert summary.startswith("Session compaction notes:\n")
    assert "- [meta] summary_version: v2-progressive" in summary
    assert "## Older Events (rolling summary)" in summary
    assert "## Recent Events" in summary
    assert (
        "* workflow changed for reflection lane | reason: provider switched to local fallback"
        in summary
    )
    assert "- recent tail event" in summary


@pytest.mark.asyncio
async def test_runtime_flush_tracker_multiline_uri_survives_rollup_when_no_reason_available() -> None:
    tracker = SessionFlushTracker()
    tracker._max_events = 1
    tracker._rolling_summary_max_chars = 400

    await tracker.record_event(
        session_id="multiline-uri",
        message=(
            "write guard updated flush target\n"
            "uri: notes://agent/preferences/default\n"
            "category: workflow"
        ),
    )
    await tracker.record_event(session_id="multiline-uri", message="recent tail event")

    summary = await tracker.build_summary(session_id="multiline-uri", limit=1)

    assert (
        "* write guard updated flush target | uri: notes://agent/preferences/default"
        in summary
    )
    assert "## Older Events (rolling summary)" in summary
    assert "## Recent Events" in summary


@pytest.mark.asyncio
async def test_runtime_flush_tracker_high_value_early_flush_disabled_is_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUNTIME_FLUSH_HIGH_VALUE_EARLY_ENABLED", "false")
    monkeypatch.setenv("RUNTIME_FLUSH_HIGH_VALUE_MIN_EVENTS", "2")
    monkeypatch.setenv("RUNTIME_FLUSH_HIGH_VALUE_MIN_CHARS", "400")
    tracker = SessionFlushTracker()
    tracker._min_events = 6
    tracker._trigger_chars = 6000

    await tracker.record_event(
        session_id="high-value-disabled",
        message=_padded_event("preference updated for reflection workflow", fill=220),
    )
    await tracker.record_event(
        session_id="high-value-disabled",
        message=_padded_event("remember default workflow for compact context", fill=220),
    )

    assert await tracker.should_flush(session_id="high-value-disabled") is False


@pytest.mark.asyncio
async def test_runtime_flush_tracker_high_value_early_flush_triggers_only_under_guardrails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUNTIME_FLUSH_HIGH_VALUE_EARLY_ENABLED", "true")
    monkeypatch.setenv("RUNTIME_FLUSH_HIGH_VALUE_MIN_EVENTS", "2")
    monkeypatch.setenv("RUNTIME_FLUSH_HIGH_VALUE_MIN_CHARS", "400")
    tracker = SessionFlushTracker()
    tracker._min_events = 6
    tracker._trigger_chars = 6000

    await tracker.record_event(
        session_id="high-value-guardrails",
        message=_padded_event("workflow preference updated for reflection lane", fill=170),
    )
    assert await tracker.should_flush(session_id="high-value-guardrails") is False

    await tracker.record_event(
        session_id="high-value-guardrails",
        message=_padded_event("routine checkpoint captured", fill=120),
    )
    assert await tracker.should_flush(session_id="high-value-guardrails") is False

    await tracker.record_event(
        session_id="high-value-guardrails",
        message=_padded_event("follow-up checkpoint stored", fill=120),
    )

    assert await tracker.should_flush(session_id="high-value-guardrails") is True


@pytest.mark.asyncio
async def test_runtime_flush_tracker_high_value_early_flush_requires_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUNTIME_FLUSH_HIGH_VALUE_EARLY_ENABLED", "true")
    monkeypatch.setenv("RUNTIME_FLUSH_HIGH_VALUE_MIN_EVENTS", "2")
    monkeypatch.setenv("RUNTIME_FLUSH_HIGH_VALUE_MIN_CHARS", "400")
    tracker = SessionFlushTracker()
    tracker._min_events = 6
    tracker._trigger_chars = 6000

    await tracker.record_event(
        session_id="high-value-no-signal",
        message=_padded_event("routine checkpoint captured", fill=220),
    )
    await tracker.record_event(
        session_id="high-value-no-signal",
        message=_padded_event("follow-up checkpoint stored", fill=220),
    )

    assert await tracker.should_flush(session_id="high-value-no-signal") is False


@pytest.mark.asyncio
async def test_runtime_flush_tracker_high_value_early_flush_ignores_duplicate_noise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUNTIME_FLUSH_HIGH_VALUE_EARLY_ENABLED", "true")
    monkeypatch.setenv("RUNTIME_FLUSH_HIGH_VALUE_MIN_EVENTS", "2")
    monkeypatch.setenv("RUNTIME_FLUSH_HIGH_VALUE_MIN_CHARS", "200")
    tracker = SessionFlushTracker()
    tracker._min_events = 6
    tracker._trigger_chars = 6000

    duplicate = _padded_event("remember preferred workflow for recall", fill=120)
    await tracker.record_event(session_id="high-value-duplicate", message=duplicate)
    await tracker.record_event(session_id="high-value-duplicate", message=duplicate)

    assert await tracker.should_flush(session_id="high-value-duplicate") is False


@pytest.mark.asyncio
async def test_runtime_flush_tracker_high_value_early_flush_uses_lower_cjk_char_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUNTIME_FLUSH_HIGH_VALUE_EARLY_ENABLED", "true")
    monkeypatch.setenv("RUNTIME_FLUSH_HIGH_VALUE_MIN_EVENTS", "2")
    monkeypatch.setenv("RUNTIME_FLUSH_HIGH_VALUE_MIN_CHARS", "120")
    monkeypatch.setenv("RUNTIME_FLUSH_HIGH_VALUE_MIN_CHARS_CJK", "100")
    tracker = SessionFlushTracker()
    tracker._min_events = 6
    tracker._trigger_chars = 6000

    await tracker.record_event(
        session_id="high-value-cjk",
        message="请记住这个长期偏好：默认工作流代号是 sim-zh-med，以后没有额外说明就按这个工作流协作，并保留这个偏好。",
    )
    await tracker.record_event(
        session_id="high-value-cjk",
        message="再记住一次：默认工作流代号仍然是 sim-zh-med，这个偏好需要在后续会话里继续生效。",
    )

    assert await tracker.should_flush(session_id="high-value-cjk") is True


@pytest.mark.asyncio
async def test_runtime_flush_tracker_high_value_early_flush_resets_after_mark_flushed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUNTIME_FLUSH_HIGH_VALUE_EARLY_ENABLED", "true")
    monkeypatch.setenv("RUNTIME_FLUSH_HIGH_VALUE_MIN_EVENTS", "2")
    monkeypatch.setenv("RUNTIME_FLUSH_HIGH_VALUE_MIN_CHARS", "200")
    tracker = SessionFlushTracker()
    tracker._min_events = 6
    tracker._trigger_chars = 6000

    await tracker.record_event(
        session_id="high-value-reset",
        message=_padded_event("remember preferred workflow for recall", fill=120),
    )
    await tracker.record_event(
        session_id="high-value-reset",
        message=_padded_event("routine checkpoint captured", fill=120),
    )

    assert await tracker.should_flush(session_id="high-value-reset") is True

    await tracker.mark_flushed(session_id="high-value-reset")
    await tracker.record_event(
        session_id="high-value-reset",
        message=_padded_event("remember preferred workflow for recall", fill=120),
    )

    assert await tracker.should_flush(session_id="high-value-reset") is False


@pytest.mark.asyncio
async def test_runtime_flush_tracker_summary_reports_flush_result_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUNTIME_FLUSH_HIGH_VALUE_EARLY_ENABLED", "true")
    monkeypatch.setenv("RUNTIME_FLUSH_HIGH_VALUE_MIN_EVENTS", "2")
    monkeypatch.setenv("RUNTIME_FLUSH_HIGH_VALUE_MIN_CHARS", "200")
    tracker = SessionFlushTracker()
    tracker._min_events = 6
    tracker._trigger_chars = 6000

    await tracker.record_event(
        session_id="metrics",
        message=_padded_event("remember preferred workflow for recall", fill=120),
    )
    await tracker.record_event(
        session_id="metrics",
        message=_padded_event("routine checkpoint captured", fill=120),
    )

    assert await tracker.should_flush(session_id="metrics") is True

    await tracker.note_flush_result(
        session_id="metrics",
        source="compact_context",
        flushed=True,
        data_persisted=True,
        result_reason="stored",
        source_hash="hash-one",
    )
    await tracker.note_flush_result(
        session_id="metrics",
        source="compact_context",
        trigger_reason="high_value_early",
        flushed=True,
        data_persisted=False,
        result_reason="write_guard_deduped",
        source_hash="hash-two",
    )

    summary = await tracker.summary()

    assert summary["flush_results_total"] == 2
    assert summary["completed_flushes"] == 2
    assert summary["persisted_flushes"] == 1
    assert summary["early_flush_count"] == 2
    assert summary["trigger_breakdown"]["high_value_early"] == 2
    assert summary["result_reason_breakdown"]["stored"] == 1
    assert summary["result_reason_breakdown"]["write_guard_deduped"] == 1
    assert summary["write_guard_deduped_ratio"] == pytest.approx(0.5)
    assert summary["last_source_hash"] == "hash-two"
    assert summary["source_hash_observations"] == 2
    assert summary["source_hash_changes"] == 1


@pytest.mark.asyncio
async def test_session_flush_tracker_uses_configurable_event_truncation_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUNTIME_FLUSH_EVENT_MAX_CHARS", "64")
    tracker = SessionFlushTracker()

    await tracker.record_event(
        session_id="session-a",
        message="0123456789abcdef" * 5,
    )

    summary = await tracker.build_summary(session_id="session-a", limit=5)
    stats = await tracker.summary()

    assert "truncated to 64 chars before flush" in summary
    assert f"- {('0123456789abcdef' * 5)[:64]}" in summary
    assert stats["event_max_chars"] == 64
    assert stats["truncated_events"] == 1
    assert stats["top_sessions"][0]["truncated_events"] == 1


@pytest.mark.asyncio
async def test_session_eviction_cleans_up_audit_counters() -> None:
    tracker = SessionFlushTracker()
    tracker._max_sessions = 2
    tracker._max_events = 2

    await tracker.record_event(session_id="session-a", message="a1")
    await tracker.record_event(session_id="session-a", message="a2")
    await tracker.record_event(session_id="session-a", message="a3")

    # P3-1: overflow is now rolled, not dropped
    assert tracker._rolled_events.get("session-a", 0) == 1

    await tracker.record_event(session_id="session-b", message="b1")
    await tracker.record_event(session_id="session-c", message="c1")

    assert "session-a" not in tracker._events
    assert "session-a" not in tracker._dropped_events
    assert "session-a" not in tracker._truncated_events
    assert "session-a" not in tracker._rolling_summaries
    assert "session-a" not in tracker._rolled_events
    assert "session-a" not in tracker._overflow_compactions


@pytest.mark.asyncio
async def test_pending_session_ids_cleared_after_mark_flushed() -> None:
    tracker = SessionFlushTracker()

    await tracker.record_event(session_id="pending-a", message="alpha")
    await tracker.record_event(session_id="pending-b", message="beta")

    pending_before = await tracker.pending_session_ids()
    assert pending_before == ["pending-a", "pending-b"]

    await tracker.mark_flushed(session_id="pending-a")

    pending_after = await tracker.pending_session_ids()
    assert pending_after == ["pending-b"]


@pytest.mark.asyncio
async def test_progressive_compression_preserves_early_facts() -> None:
    """P3-1 KEY TEST: early critical facts survive in rolling summary."""
    tracker = SessionFlushTracker()
    tracker._max_events = 5

    # Events 1-3: critical facts
    await tracker.record_event(session_id="s1", message="CRITICAL_FACT_ALPHA: baseline established")
    await tracker.record_event(session_id="s1", message="CRITICAL_FACT_BETA: dependency confirmed")
    await tracker.record_event(session_id="s1", message="CRITICAL_FACT_GAMMA: constraint verified")

    # Events 4-20: filler that pushes critical facts into rolling summary
    for i in range(4, 21):
        await tracker.record_event(session_id="s1", message=f"filler event {i}: routine operation")

    summary = await tracker.build_summary(session_id="s1", limit=5)
    stats = await tracker.summary()

    # Rolling summary should preserve early critical facts
    assert "CRITICAL_FACT_ALPHA" in summary
    assert "CRITICAL_FACT_BETA" in summary
    assert "CRITICAL_FACT_GAMMA" in summary

    # Recent tail should have the last 5 events
    assert "filler event 20" in summary
    assert "filler event 19" in summary
    assert "filler event 18" in summary
    assert "filler event 17" in summary
    assert "filler event 16" in summary

    # Structural sections
    assert "## Older Events (rolling summary)" in summary
    assert "## Recent Events" in summary

    # At least 15 events rolled (20 total - 5 in queue)
    assert stats["rolled_events"] >= 15
    assert stats["top_sessions"][0]["rolled_events"] >= 15


@pytest.mark.asyncio
async def test_progressive_compression_100_events_long_session() -> None:
    """Long session: early, mid, and recent events each have correct placement."""
    tracker = SessionFlushTracker()
    tracker._max_events = 20
    # Increase rolling summary capacity so early facts survive 80 compactions
    tracker._rolling_summary_max_chars = 6000

    for i in range(1, 101):
        if 1 <= i <= 5:
            msg = f"EARLY_FACT_{i}: important baseline info"
        elif 50 <= i <= 55:
            msg = f"MID_SESSION_UPDATE_{i}: changed assumption"
        elif 95 <= i <= 100:
            msg = f"RECENT_EXCEPTION_{i}: edge case found"
        else:
            msg = f"routine event {i}: standard operation"

        await tracker.record_event(session_id="long", message=msg)

    summary = await tracker.build_summary(session_id="long", limit=12)
    stats = await tracker.summary()

    # Rolling summary should mention early facts (preserved because capacity is large enough)
    assert "EARLY_FACT_1" in summary
    assert "EARLY_FACT_5" in summary

    # Mid-session updates should also be in rolling summary
    assert "MID_SESSION_UPDATE_50" in summary

    # Recent tail should have the last 12 events (89-100)
    assert "RECENT_EXCEPTION_100" in summary
    assert "RECENT_EXCEPTION_95" in summary

    # Version tag
    assert "v2-progressive" in summary

    # At least 80 events rolled (100 - 20 in queue)
    assert stats["rolled_events"] >= 80
    assert stats["top_sessions"][0]["overflow_compactions"] >= 80


@pytest.mark.asyncio
async def test_progressive_compression_100_events_trims_oldest_when_capacity_limited() -> None:
    """With default capacity, oldest entries are trimmed from rolling summary."""
    tracker = SessionFlushTracker()
    tracker._max_events = 20
    # Default rolling_summary_max_chars=2000 -- not enough for 80 entries

    for i in range(1, 101):
        if 1 <= i <= 5:
            msg = f"EARLY_FACT_{i}: important baseline info"
        elif 95 <= i <= 100:
            msg = f"RECENT_EXCEPTION_{i}: edge case found"
        else:
            msg = f"routine event {i}: standard operation"

        await tracker.record_event(session_id="long", message=msg)

    summary = await tracker.build_summary(session_id="long", limit=12)
    stats = await tracker.summary()

    # With limited capacity, early facts may be trimmed -- that's correct behavior
    # But recent events in tail are always preserved
    assert "RECENT_EXCEPTION_100" in summary
    assert "RECENT_EXCEPTION_95" in summary
    assert "v2-progressive" in summary
    assert stats["rolled_events"] >= 80

    # Rolling summary should be within capacity
    assert len(tracker._rolling_summaries.get("long", "")) <= 2000


@pytest.mark.asyncio
async def test_rolling_summary_max_chars_respected() -> None:
    """Rolling summary text is trimmed when it exceeds max chars."""
    tracker = SessionFlushTracker()
    tracker._max_events = 3
    tracker._rolling_summary_max_chars = 200

    # Record many events to overflow repeatedly
    for i in range(50):
        await tracker.record_event(
            session_id="trim",
            message=f"event_{i}_with_some_padding_text_to_fill_up_space",
        )

    summary = await tracker.build_summary(session_id="trim", limit=3)

    # Extract the rolling summary section
    rolling_section = ""
    in_rolling = False
    for line in summary.split("\n"):
        if line.strip() == "## Older Events (rolling summary)":
            in_rolling = True
            continue
        if line.strip().startswith("## Recent Events"):
            break
        if in_rolling:
            rolling_section += line + "\n"

    # Rolling summary text (stored internally) must not exceed max chars
    assert len(tracker._rolling_summaries.get("trim", "")) <= 200

    # Oldest compressed entries were dropped to stay under limit
    stats = await tracker.summary()
    assert stats["rolled_events"] >= 47  # 50 - 3 in queue


@pytest.mark.asyncio
async def test_build_summary_v2_format() -> None:
    """Verify the v2-progressive output format structure."""
    tracker = SessionFlushTracker()
    tracker._max_events = 3

    await tracker.record_event(session_id="fmt", message="first")
    await tracker.record_event(session_id="fmt", message="second")
    await tracker.record_event(session_id="fmt", message="third")
    await tracker.record_event(session_id="fmt", message="fourth")  # triggers overflow

    summary = await tracker.build_summary(session_id="fmt", limit=3)

    assert "[meta] summary_version: v2-progressive" in summary
    assert "[audit]" in summary
    assert "compressed into rolling summary" in summary
    assert "## Older Events (rolling summary)" in summary
    assert "## Recent Events" in summary
    assert "- second" in summary
    assert "- third" in summary
    assert "- fourth" in summary
    assert "first" in summary  # in rolling summary section


@pytest.mark.asyncio
async def test_rolling_summary_tail_keep_overrides_small_limit():
    """RUNTIME_FLUSH_RECENT_TAIL_KEEP acts as a floor for the recent tail window."""
    tracker = SessionFlushTracker()
    tracker._max_events = 20
    tracker._rolling_summary_tail_keep = 8  # config: keep at least 8 recent

    for i in range(25):
        await tracker.record_event(session_id="s", message=f"event_{i}")

    # build_summary with limit=3, but config says keep at least 8
    summary = await tracker.build_summary(session_id="s", limit=3)

    # Recent Events section should have 8 events (config floor), not 3
    recent_section = ""
    in_recent = False
    for line in summary.split("\n"):
        if "## Recent Events" in line:
            in_recent = True
            continue
        if in_recent and line.startswith("- "):
            recent_section += line + "\n"

    recent_events = [l for l in recent_section.strip().split("\n") if l.startswith("- ")]
    assert len(recent_events) == 8, (
        f"Expected 8 recent events (config floor), got {len(recent_events)}: {recent_events}"
    )

    # Most recent event should be present
    assert "event_24" in summary
    # 8th from end should be present
    assert "event_17" in summary  # 24-7=17
