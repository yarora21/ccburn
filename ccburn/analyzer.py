"""
Waste detectors for Claude Code sessions.

Takes parsed Session objects and produces findings with dollar amounts
and evidence turn indices. All heuristics are deterministic — no LLM calls.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import List, Optional

from .parser import Session, Turn
from .pricing import usage_cost, cache_read_cost, TIERS


# ── Output types ──────────────────────────────────────────────────────

@dataclass
class Finding:
    detector: str
    dollars: float
    summary: str
    evidence_turns: List[int] = field(default_factory=list)
    detail: dict = field(default_factory=dict)


@dataclass
class TurnCost:
    index: int
    total: float
    cache_read: float
    other: float


@dataclass
class SessionReport:
    session: Session
    total_cost: float
    total_waste: float
    findings: List[Finding]
    per_turn_costs: List[TurnCost]
    dominant_waste: str


# ── Detector helpers ──────────────────────────────────────────────────

SIMPLE_TOOLS = {"Read", "Glob", "Grep", "Bash", "ToolSearch", "Write", "Edit"}


def _per_turn_costs(session: Session) -> List[TurnCost]:
    result = []
    for t in session.turns:
        u = t.usage.as_dict()
        total = usage_cost(u, t.tier)
        cr = cache_read_cost(u, t.tier)
        result.append(TurnCost(
            index=t.index,
            total=total,
            cache_read=cr,
            other=total - cr,
        ))
    return result


# ── Detector 1: Cache-read burn & split point ────────────────────────

def _detect_cache_burn(session: Session, per_turn: List[TurnCost]) -> Optional[Finding]:
    if len(session.turns) < 10:
        return None

    total_cache = sum(tc.cache_read for tc in per_turn)
    total_cost = sum(tc.total for tc in per_turn)
    if total_cost == 0:
        return None

    cache_pct = total_cache / total_cost

    # Find optimal split point: try each turn in the middle 50%,
    # estimate that post-split cache reads would be ~halved
    # (second half rebuilds context from zero).
    best_savings = 0.0
    best_turn = -1
    n = len(per_turn)
    for i in range(n // 4, 3 * n // 4):
        post_cache = sum(tc.cache_read for tc in per_turn[i + 1:])
        savings = post_cache * 0.5
        if savings > best_savings:
            best_savings = savings
            best_turn = i

    if best_savings < 0.10:
        return None

    return Finding(
        detector="cache_burn",
        dollars=best_savings,
        summary=(
            f"Cache reads are {cache_pct:.0%} of session cost (${total_cache:.2f}/${total_cost:.2f}). "
            f"Splitting at turn {best_turn} could save ~${best_savings:.2f}."
        ),
        evidence_turns=[best_turn],
        detail={
            "cache_read_cost": round(total_cache, 2),
            "cache_read_pct": round(cache_pct * 100, 1),
            "split_turn": best_turn,
            "split_savings": round(best_savings, 2),
        },
    )


# ── Detector 2: Model-mix waste ──────────────────────────────────────

def _detect_model_mix(session: Session) -> Optional[Finding]:
    waste = 0.0
    evidence = []

    for t in session.turns:
        if t.tier != "opus" or not t.tool_calls:
            continue
        names = {tc.name for tc in t.tool_calls}
        if not names <= SIMPLE_TOOLS:
            continue
        u = t.usage.as_dict()
        opus_price = usage_cost(u, "opus")
        sonnet_price = usage_cost(u, "sonnet")
        delta = opus_price - sonnet_price
        if delta > 0:
            waste += delta
            evidence.append(t.index)

    if waste < 0.10:
        return None

    return Finding(
        detector="model_mix",
        dollars=waste,
        summary=(
            f"{len(evidence)} Opus turns did only simple tool work (Read/Glob/Bash/Edit). "
            f"At Sonnet rates, that's ${waste:.2f} saved."
        ),
        evidence_turns=evidence,
        detail={
            "simple_opus_turns": len(evidence),
        },
    )


# ── Detector 3: Redundant file re-reads ──────────────────────────────

def _detect_redundant_reads(session: Session) -> Optional[Finding]:
    # Track reads and edits in order to find reads with no intervening edit
    file_reads: defaultdict[str, list] = defaultdict(list)
    file_edits: defaultdict[str, list] = defaultdict(list)

    for t in session.turns:
        for tc in t.tool_calls:
            fp = tc.input.get("file_path", "")
            if not fp:
                continue
            if tc.name == "Read":
                file_reads[fp].append(t.index)
            elif tc.name in ("Edit", "Write"):
                file_edits[fp].append(t.index)

    redundant_files = {}
    for fp, reads in file_reads.items():
        if len(reads) < 3:
            continue
        edits = set(file_edits.get(fp, []))
        # Count reads that had no edit between them and the previous read
        excess = 0
        for i in range(1, len(reads)):
            prev_read = reads[i - 1]
            curr_read = reads[i]
            had_edit = any(prev_read < e < curr_read for e in edits)
            if not had_edit:
                excess += 1
        if excess > 0:
            redundant_files[fp] = {"reads": len(reads), "excess": excess, "turns": reads}

    if not redundant_files:
        return None

    total_excess = sum(v["excess"] for v in redundant_files.values())
    evidence = []
    for v in redundant_files.values():
        evidence.extend(v["turns"])

    # Shorten paths for display
    short_files = {fp.split("/")[-1]: v for fp, v in redundant_files.items()}

    return Finding(
        detector="redundant_reads",
        dollars=0,  # hard to attribute exact cost
        summary=(
            f"{total_excess} redundant re-reads across {len(redundant_files)} files "
            f"(no edit between reads). Files: {', '.join(short_files.keys())}"
        ),
        evidence_turns=sorted(set(evidence)),
        detail={"files": {fp: v["reads"] for fp, v in redundant_files.items()}},
    )


# ── Detector 4: Doom loops ───────────────────────────────────────────

@dataclass
class _Run:
    norm_key: str
    start: int
    length: int
    errors: int


def _detect_doom_loops(session: Session) -> Optional[Finding]:
    # Find runs of >=3 consecutive identical tool calls (by norm_key)
    runs: List[_Run] = []
    prev_key: Optional[str] = None
    run_start = 0
    run_len = 1
    run_errors = 0

    all_tool_calls = []
    for t in session.turns:
        for tc in t.tool_calls:
            all_tool_calls.append((tc, t.index))

    if not all_tool_calls:
        return None

    def flush_run():
        if run_len >= 3:
            runs.append(_Run(
                norm_key=prev_key or "",
                start=run_start,
                length=run_len,
                errors=run_errors,
            ))

    for i, (tc, turn_idx) in enumerate(all_tool_calls):
        if i == 0:
            prev_key = tc.norm_key
            run_start = turn_idx
            run_len = 1
            run_errors = 0
            continue

        if tc.norm_key == prev_key:
            run_len += 1
        else:
            flush_run()
            prev_key = tc.norm_key
            run_start = turn_idx
            run_len = 1
            run_errors = 0

    flush_run()

    if not runs:
        return None

    worst = max(runs, key=lambda r: r.length)
    total_repeated = sum(r.length for r in runs)

    # Extract short label from norm_key
    label = worst.norm_key
    if "::" in label:
        tool, param = label.split("::", 1)
        param_short = param.split("/")[-1][:60] if "/" in param else param[:60]
        label = f"{tool} on {param_short}" if param_short else tool

    return Finding(
        detector="doom_loops",
        dollars=0,
        summary=(
            f"{len(runs)} repeated-call run(s), longest: {worst.length}x ({label}). "
            f"{total_repeated} total repeated calls."
        ),
        evidence_turns=[worst.start],
        detail={
            "runs": len(runs),
            "max_run": worst.length,
            "max_run_key": worst.norm_key,
            "total_repeated": total_repeated,
        },
    )


# ── Main entry point ─────────────────────────────────────────────────

def analyze_session(session: Session) -> SessionReport:
    """Run all detectors on a session and return a report."""
    per_turn = _per_turn_costs(session)
    total_cost = sum(tc.total for tc in per_turn)

    findings: List[Finding] = []

    for detector in [_detect_cache_burn, _detect_model_mix, _detect_redundant_reads, _detect_doom_loops]:
        if detector in (_detect_cache_burn,):
            result = detector(session, per_turn)
        else:
            result = detector(session)
        if result:
            findings.append(result)

    findings.sort(key=lambda f: f.dollars, reverse=True)
    total_waste = sum(f.dollars for f in findings)
    dominant = findings[0].detector if findings else "none"

    return SessionReport(
        session=session,
        total_cost=round(total_cost, 2),
        total_waste=round(total_waste, 2),
        findings=findings,
        per_turn_costs=per_turn,
        dominant_waste=dominant,
    )
