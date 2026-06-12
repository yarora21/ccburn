"""Tests for ccburn.live — incremental reader + metrics."""

import json
import os
import tempfile
import time
from pathlib import Path

from ccburn.live import (
    LiveState,
    TurnStats,
    _cache_path,
    compute_metrics,
    read_live_state,
    CACHE_DIR,
)


# ── Helpers ──────────────────────────────────────────────────────────

def _make_assistant_line(
    turn_index: int,
    input_tokens: int = 100,
    output_tokens: int = 200,
    cache_write: int = 0,
    cache_read: int = 0,
    model: str = "claude-opus-4-6",
    is_sidechain: bool = False,
) -> str:
    obj = {
        "type": "assistant",
        "isSidechain": is_sidechain,
        "sessionId": "test-session-001",
        "message": {
            "model": model,
            "role": "assistant",
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_creation_input_tokens": cache_write,
                "cache_read_input_tokens": cache_read,
            },
            "content": [{"type": "text", "text": f"Turn {turn_index}"}],
        },
    }
    return json.dumps(obj) + "\n"


def _make_user_line(content: str = "hello") -> str:
    obj = {
        "type": "user",
        "sessionId": "test-session-001",
        "message": {"role": "user", "content": content},
    }
    return json.dumps(obj) + "\n"


# ── Tests ────────────────────────────────────────────────────────────

def test_empty_file():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        f.write("")
        path = f.name
    try:
        state = read_live_state(path)
        assert state.num_turns == 0
        assert len(state.turns) == 0
    finally:
        os.unlink(path)
        cp = _cache_path(path)
        cp.unlink(missing_ok=True)


def test_basic_parse():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        f.write(_make_user_line())
        f.write(_make_assistant_line(0, input_tokens=50, output_tokens=100))
        f.write(_make_user_line())
        f.write(_make_assistant_line(1, input_tokens=50, output_tokens=150,
                                     cache_read=1000))
        path = f.name
    try:
        state = read_live_state(path)
        assert len(state.turns) == 2
        assert state.turns[0].input_tokens == 50
        assert state.turns[1].cache_read_tokens == 1000
        assert state.session_id == "test-session-001"
    finally:
        os.unlink(path)
        _cache_path(path).unlink(missing_ok=True)


def test_incremental_read():
    """Simulate a growing file — second read should only parse new lines."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        f.write(_make_assistant_line(0, input_tokens=50, output_tokens=100))
        path = f.name

    try:
        state1 = read_live_state(path)
        assert len(state1.turns) == 1
        offset1 = state1.byte_offset

        # Append more data
        with open(path, "a") as f:
            f.write(_make_assistant_line(1, input_tokens=80, output_tokens=200,
                                         cache_read=5000))

        state2 = read_live_state(path)
        assert len(state2.turns) == 2
        assert state2.byte_offset > offset1
        assert state2.turns[1].cache_read_tokens == 5000
    finally:
        os.unlink(path)
        _cache_path(path).unlink(missing_ok=True)


def test_sidechain_excluded_from_mainchain():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        f.write(_make_assistant_line(0))
        f.write(_make_assistant_line(1, is_sidechain=True))
        f.write(_make_assistant_line(2))
        path = f.name

    try:
        state = read_live_state(path)
        assert len(state.turns) == 3
        assert len(state.mainchain_turns) == 2
        assert state.num_turns == 2
    finally:
        os.unlink(path)
        _cache_path(path).unlink(missing_ok=True)


def test_malformed_line_skipped():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        f.write(_make_assistant_line(0))
        f.write("this is not json\n")
        f.write("{incomplete json\n")
        f.write(_make_assistant_line(1))
        path = f.name

    try:
        state = read_live_state(path)
        assert len(state.turns) == 2
    finally:
        os.unlink(path)
        _cache_path(path).unlink(missing_ok=True)


def test_metrics_no_turns():
    state = LiveState()
    m = compute_metrics(state)
    assert m.session_cost == 0
    assert m.marginal_cost is None
    assert m.num_turns == 0


def test_metrics_few_turns():
    """<5 turns: marginal computed but no baseline/ratio."""
    state = LiveState()
    for i in range(3):
        state.turns.append(TurnStats(
            index=i, model="claude-opus-4-6", tier="opus",
            input_tokens=100, output_tokens=200,
            cache_read_tokens=1000 * (i + 1),
        ))
    m = compute_metrics(state)
    assert m.marginal_cost is not None
    assert m.baseline_marginal is None
    assert m.ratio is None
    assert m.num_turns == 3


def test_metrics_with_baseline():
    """>=5 turns: baseline and ratio computed."""
    state = LiveState()
    for i in range(8):
        state.turns.append(TurnStats(
            index=i, model="claude-opus-4-6", tier="opus",
            input_tokens=100, output_tokens=200,
            cache_read_tokens=1000 * (i + 1),
        ))
    m = compute_metrics(state)
    assert m.baseline_marginal is not None
    assert m.ratio is not None
    assert m.ratio > 1.0  # later turns have more cache reads


def test_metrics_compact_delta():
    """Compact delta computed when context > POST_COMPACT_TOKENS."""
    state = LiveState()
    for i in range(6):
        state.turns.append(TurnStats(
            index=i, model="claude-opus-4-6", tier="opus",
            input_tokens=100, output_tokens=200,
            cache_read_tokens=20_000,  # context = 20100 > 15000
        ))
    m = compute_metrics(state, horizon_turns=20)
    assert m.compact_delta is not None
    assert m.compact_delta > 0


def test_metrics_no_compact_delta_small_context():
    """No compact delta when context < POST_COMPACT_TOKENS."""
    state = LiveState()
    for i in range(6):
        state.turns.append(TurnStats(
            index=i, model="claude-opus-4-6", tier="opus",
            input_tokens=100, output_tokens=200,
            cache_read_tokens=5000,  # context = 5100 < 15000
        ))
    m = compute_metrics(state, horizon_turns=20)
    assert m.compact_delta is None


def test_real_session_fixture():
    """Run against the real 1585-line session if available."""
    fixture = os.path.expanduser(
        "~/.claude/projects/-Users-yasharora-Documents-football-trvia/"
        "d12eb1c7-02d4-4cf8-a1a0-e785653145b4.jsonl"
    )
    if not os.path.exists(fixture):
        return  # skip if not present

    # Clear any existing cache
    cp = _cache_path(fixture)
    cp.unlink(missing_ok=True)

    # Cold read
    t0 = time.perf_counter()
    state = read_live_state(fixture)
    cold_ms = (time.perf_counter() - t0) * 1000

    assert state.num_turns > 0, "Should find turns in fixture"
    print(f"\nFixture: {len(state.turns)} turns, {state.num_turns} mainchain")

    # Warm read (from cache, no new data)
    t0 = time.perf_counter()
    state2 = read_live_state(fixture)
    warm_ms = (time.perf_counter() - t0) * 1000

    assert len(state2.turns) == len(state.turns)
    print(f"Cold: {cold_ms:.1f}ms, Warm: {warm_ms:.1f}ms")
    assert warm_ms < 50, f"Warm read too slow: {warm_ms:.1f}ms (target <50ms)"

    # Compute metrics
    m = compute_metrics(state)
    print(f"Session cost: ${m.session_cost:.2f}")
    print(f"Marginal cost: ${m.marginal_cost:.4f}" if m.marginal_cost else "No marginal")
    print(f"Ratio: {m.ratio:.1f}x" if m.ratio else "No ratio")
    print(f"Compact delta: ${m.compact_delta:.2f}" if m.compact_delta else "No compact delta")
    print(f"Context tokens: {m.context_tokens:,}")

    # Cleanup
    cp.unlink(missing_ok=True)
