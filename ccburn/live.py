"""
Live session math for ccburn statusline and hook.

Incrementally parses the CURRENT session's JSONL, caching byte offsets
for <50ms warm reads. Computes marginal cost of next turn, baseline
ratio, session cost, and compact savings delta.
"""

from __future__ import annotations

import json
import os
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from .pricing import TIERS, tier_for_model, token_cost

# ── Constants ────────────────────────────────────────────────────────

POST_COMPACT_TOKENS = 15_000
DEFAULT_HORIZON_TURNS = 20
BASELINE_TURN_COUNT = 5
CACHE_DIR = Path.home() / ".cache" / "ccburn" / "live"


# ── Data types ───────────────────────────────────────────────────────

@dataclass
class TurnStats:
    """Extracted stats from a single assistant turn."""
    index: int
    model: str
    tier: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    is_sidechain: bool = False

    @property
    def context_tokens(self) -> int:
        return self.cache_read_tokens + self.cache_write_tokens + self.input_tokens

    @property
    def cost(self) -> float:
        return token_cost(
            self.input_tokens, self.output_tokens,
            self.cache_write_tokens, self.cache_read_tokens,
            tier=self.tier,
        )


@dataclass
class LiveState:
    """Running aggregates for a live session, persisted to cache."""
    byte_offset: int = 0
    turns: List[TurnStats] = field(default_factory=list)
    session_id: str = ""
    warned_tiers: List[int] = field(default_factory=list)  # e.g. [4, 8]

    @property
    def mainchain_turns(self) -> List[TurnStats]:
        return [t for t in self.turns if not t.is_sidechain]

    @property
    def num_turns(self) -> int:
        return len(self.mainchain_turns)

    def to_cache_dict(self) -> dict:
        return {
            "byte_offset": self.byte_offset,
            "session_id": self.session_id,
            "warned_tiers": self.warned_tiers,
            "turns": [
                {
                    "index": t.index,
                    "model": t.model,
                    "tier": t.tier,
                    "input_tokens": t.input_tokens,
                    "output_tokens": t.output_tokens,
                    "cache_write_tokens": t.cache_write_tokens,
                    "cache_read_tokens": t.cache_read_tokens,
                    "is_sidechain": t.is_sidechain,
                }
                for t in self.turns
            ],
        }

    @classmethod
    def from_cache_dict(cls, d: dict) -> LiveState:
        state = cls(
            byte_offset=d.get("byte_offset", 0),
            session_id=d.get("session_id", ""),
            warned_tiers=d.get("warned_tiers", []),
        )
        for t in d.get("turns", []):
            state.turns.append(TurnStats(
                index=t["index"],
                model=t["model"],
                tier=t["tier"],
                input_tokens=t.get("input_tokens", 0),
                output_tokens=t.get("output_tokens", 0),
                cache_write_tokens=t.get("cache_write_tokens", 0),
                cache_read_tokens=t.get("cache_read_tokens", 0),
                is_sidechain=t.get("is_sidechain", False),
            ))
        return state


@dataclass
class LiveMetrics:
    """Computed metrics for the current session state."""
    session_cost: float
    marginal_cost: Optional[float]  # None if <1 turn
    baseline_marginal: Optional[float]  # None if <5 turns
    ratio: Optional[float]  # marginal / baseline, None if no baseline
    compact_delta: Optional[float]  # estimated savings from compacting now
    context_tokens: int  # current context size
    num_turns: int
    last_model: str
    last_tier: str


# ── Cache I/O ────────────────────────────────────────────────────────

def _cache_path(transcript_path: str) -> Path:
    session_id = Path(transcript_path).stem
    return CACHE_DIR / f"{session_id}.json"


def _load_cache(transcript_path: str) -> Optional[LiveState]:
    cp = _cache_path(transcript_path)
    try:
        with open(cp) as f:
            return LiveState.from_cache_dict(json.load(f))
    except (OSError, json.JSONDecodeError, KeyError):
        return None


def _save_cache(transcript_path: str, state: LiveState) -> None:
    cp = _cache_path(transcript_path)
    try:
        cp.parent.mkdir(parents=True, exist_ok=True)
        tmp = cp.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(state.to_cache_dict(), f)
        tmp.rename(cp)
    except OSError:
        pass  # non-fatal


# ── Incremental parser ───────────────────────────────────────────────

def _parse_new_lines(fh, state: LiveState) -> None:
    """Read from current file position, appending new turns to state."""
    for raw_line in fh:
        try:
            obj = json.loads(raw_line)
        except json.JSONDecodeError:
            continue

        if not state.session_id:
            state.session_id = obj.get("sessionId", "")

        if obj.get("type") != "assistant":
            continue

        msg = obj.get("message", {})
        usage = msg.get("usage")
        if not usage:
            continue

        model = msg.get("model", "")
        tier = tier_for_model(model)
        is_sidechain = obj.get("isSidechain", False)

        state.turns.append(TurnStats(
            index=len(state.turns),
            model=model,
            tier=tier,
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            cache_write_tokens=usage.get("cache_creation_input_tokens", 0),
            cache_read_tokens=usage.get("cache_read_input_tokens", 0),
            is_sidechain=is_sidechain,
        ))


def read_live_state(transcript_path: str) -> LiveState:
    """Incrementally parse a live JSONL transcript, using cache for speed."""
    try:
        file_size = os.path.getsize(transcript_path)
    except OSError:
        return LiveState()

    state = _load_cache(transcript_path)
    if state is None or state.byte_offset > file_size:
        state = LiveState()

    try:
        with open(transcript_path, errors="replace") as fh:
            fh.seek(state.byte_offset)
            _parse_new_lines(fh, state)
            state.byte_offset = fh.tell()
    except OSError:
        return state

    _save_cache(transcript_path, state)
    return state


# ── Metrics computation ──────────────────────────────────────────────

def _marginal_cost_for_turn(turn: TurnStats, median_output: int) -> float:
    """Estimate marginal cost of a hypothetical next turn like this one."""
    pricing = TIERS.get(turn.tier, TIERS["sonnet"])
    return (
        turn.context_tokens * pricing.cache_read
        + median_output * pricing.output
    )


def compute_metrics(
    state: LiveState,
    model_override: Optional[str] = None,
    post_compact_tokens: int = POST_COMPACT_TOKENS,
    horizon_turns: int = DEFAULT_HORIZON_TURNS,
) -> LiveMetrics:
    """Compute all live metrics from current session state."""
    mainchain = state.mainchain_turns
    num_turns = len(mainchain)

    # Session cost: sum over ALL turns (including sidechain)
    session_cost = sum(t.cost for t in state.turns)

    if num_turns == 0:
        return LiveMetrics(
            session_cost=session_cost,
            marginal_cost=None,
            baseline_marginal=None,
            ratio=None,
            compact_delta=None,
            context_tokens=0,
            num_turns=0,
            last_model="",
            last_tier="sonnet",
        )

    last = mainchain[-1]
    tier = tier_for_model(model_override) if model_override else last.tier
    pricing = TIERS.get(tier, TIERS["sonnet"])

    # Median output tokens this session
    output_values = [t.output_tokens for t in mainchain if t.output_tokens > 0]
    median_output = int(statistics.median(output_values)) if output_values else 500

    # Marginal cost of next turn
    context_tokens = last.context_tokens
    marginal_cost = context_tokens * pricing.cache_read + median_output * pricing.output

    # Baseline: median marginal of first 5 mainchain turns
    baseline_marginal = None
    ratio = None
    if num_turns >= BASELINE_TURN_COUNT:
        baseline_turns = mainchain[:BASELINE_TURN_COUNT]
        baseline_marginals = [_marginal_cost_for_turn(t, median_output) for t in baseline_turns]
        baseline_marginal = statistics.median(baseline_marginals)
        if baseline_marginal > 0:
            ratio = marginal_cost / baseline_marginal

    # Compact delta
    compact_delta = None
    if context_tokens > post_compact_tokens:
        per_turn_savings = (context_tokens - post_compact_tokens) * pricing.cache_read
        compact_delta = per_turn_savings * horizon_turns

    return LiveMetrics(
        session_cost=round(session_cost, 4),
        marginal_cost=round(marginal_cost, 4) if marginal_cost is not None else None,
        baseline_marginal=round(baseline_marginal, 4) if baseline_marginal is not None else None,
        ratio=round(ratio, 2) if ratio is not None else None,
        compact_delta=round(compact_delta, 2) if compact_delta is not None else None,
        context_tokens=context_tokens,
        num_turns=num_turns,
        last_model=last.model,
        last_tier=tier,
    )


# ── Horizon estimation from historical corpus ────────────────────────

def estimate_horizon_turns(
    current_turn_count: int,
    projects_root: Optional[str] = None,
) -> int:
    """Estimate remaining turns from user's historical sessions.

    Looks at completed sessions in ~/.claude/projects and computes the
    median remaining turns for sessions that reached current_turn_count.
    Falls back to DEFAULT_HORIZON_TURNS.
    """
    if projects_root is None:
        projects_root = os.path.expanduser("~/.claude/projects")

    if not os.path.isdir(projects_root):
        return DEFAULT_HORIZON_TURNS

    remaining_counts: List[int] = []

    import glob as glob_mod
    paths = glob_mod.glob(os.path.join(projects_root, "**", "*.jsonl"), recursive=True)

    for path in paths:
        try:
            turn_count = 0
            with open(path, errors="replace") as fh:
                for line in fh:
                    if '"type":"assistant"' in line or '"type": "assistant"' in line:
                        turn_count += 1
            if turn_count >= current_turn_count:
                remaining_counts.append(turn_count - current_turn_count)
        except OSError:
            continue

    if len(remaining_counts) < 3:
        return DEFAULT_HORIZON_TURNS

    return max(1, int(statistics.median(remaining_counts)))


# ── Convenience: one-call API for statusline/hook ────────────────────

def get_live_metrics(
    transcript_path: str,
    model_override: Optional[str] = None,
    post_compact_tokens: int = POST_COMPACT_TOKENS,
) -> LiveMetrics:
    """Read live state and compute metrics in one call."""
    state = read_live_state(transcript_path)
    mainchain = state.mainchain_turns
    horizon = DEFAULT_HORIZON_TURNS
    if len(mainchain) >= 10:
        horizon = estimate_horizon_turns(len(mainchain))
    return compute_metrics(state, model_override, post_compact_tokens, horizon)
