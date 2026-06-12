"""
CLI entry point for ccburn.

Commands:
  ccburn scan                 — all projects overview
  ccburn scan --project foo   — filter to one project
  ccburn session <id>         — deep dive into one session
  ccburn chart <id>           — write burn-chart PNG
"""

from __future__ import annotations

import argparse
import copy
import shutil
import sys
from pathlib import Path

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.text import Text

import json

from .parser import discover_sessions, Session
from .analyzer import analyze_session, SessionReport
from .pricing import usage_cost, cache_read_cost
from .chart import render_burn_chart
from .live import get_live_metrics, read_live_state, compute_metrics, LiveMetrics, _save_cache, _cache_path

console = Console()


def _short_project(name: str) -> str:
    """Strip the home-dir encoding from project names."""
    prefixes = ["-Users-yasharora-Documents-", "-Users-yasharora-"]
    for p in prefixes:
        if name.startswith(p):
            return name[len(p):]
    return name


def _match_session(sessions: list, query: str) -> Session | None:
    """Find a session by prefix of session_id or by index."""
    # Try numeric index first
    try:
        idx = int(query)
        if 0 <= idx < len(sessions):
            return sessions[idx]
    except ValueError:
        pass
    # Try session_id prefix match
    q = query.lower()
    for s in sessions:
        if s.session_id.lower().startswith(q):
            return s
    return None


# ── scan ──────────────────────────────────────────────────────────────

def cmd_scan(args):
    sessions = discover_sessions()
    if args.project:
        q = args.project.lower()
        sessions = [s for s in sessions if q in _short_project(s.project).lower()]

    if not sessions:
        console.print("[red]No sessions found.[/red]")
        return

    reports = [analyze_session(s) for s in sessions]
    reports.sort(key=lambda r: r.total_cost, reverse=True)

    table = Table(
        title="ccburn scan — Claude Code session costs (API-equivalent estimates)",
        show_lines=False,
        header_style="bold cyan",
    )
    table.add_column("#", style="dim", width=3)
    table.add_column("Project", max_width=25)
    table.add_column("Turns", justify="right")
    table.add_column("Hours", justify="right")
    table.add_column("Cost", justify="right", style="bold")
    table.add_column("Cache %", justify="right")
    table.add_column("Waste", justify="right", style="bold red")
    table.add_column("Dominant pattern", max_width=30)

    for i, r in enumerate(reports):
        s = r.session
        cache_pct = ""
        if r.total_cost > 0:
            cr = sum(tc.cache_read for tc in r.per_turn_costs)
            cache_pct = f"{100 * cr / r.total_cost:.0f}%"

        dominant = _format_dominant(r)

        table.add_row(
            str(i),
            _short_project(s.project),
            str(s.num_turns),
            f"{s.duration_hours:.1f}",
            f"${r.total_cost:.2f}",
            cache_pct,
            f"${r.total_waste:.2f}" if r.total_waste > 0 else "-",
            dominant,
        )

    console.print(table)

    # Totals
    total_cost = sum(r.total_cost for r in reports)
    total_waste = sum(r.total_waste for r in reports)
    worst = reports[0] if reports else None

    console.print()
    console.print(
        Panel(
            f"[bold]TOTAL ESTIMATED COST:[/bold]  ${total_cost:.2f}\n"
            f"[bold red]TOTAL ESTIMATED WASTE:[/bold red] ${total_waste:.2f}\n"
            f"[dim]Worst offender:[/dim] {_short_project(worst.session.project)} "
            f"(${worst.total_cost:.2f}, {worst.session.num_turns} turns)"
            if worst else "",
            title="Summary",
            border_style="yellow",
        )
    )


def _format_dominant(report: SessionReport) -> str:
    labels = {
        "cache_burn": "cache-read burn",
        "model_mix": "model-mix waste",
        "redundant_reads": "redundant reads",
        "doom_loops": "doom loop",
        "none": "-",
    }
    if not report.findings:
        return "-"
    f = report.findings[0]
    label = labels.get(f.detector, f.detector)
    if f.dollars > 0:
        return f"{label} (${f.dollars:.2f})"
    return label


# ── session ───────────────────────────────────────────────────────────

def cmd_session(args):
    sessions = discover_sessions()
    reports = [analyze_session(s) for s in sessions]
    reports.sort(key=lambda r: r.total_cost, reverse=True)

    target = _match_session([r.session for r in reports], args.id)
    if not target:
        console.print(f"[red]No session matching '{args.id}'. Run 'ccburn scan' to list sessions.[/red]")
        return

    report = next(r for r in reports if r.session is target)
    s = report.session

    # Header
    console.print(Panel(
        f"[bold]{_short_project(s.project)}[/bold]\n"
        f"Session: {s.session_id}\n"
        f"Turns: {s.num_turns}  |  Duration: {s.duration_hours:.1f}h  |  "
        f"Version: {s.cc_version or '?'}  |  Branch: {s.git_branch or '?'}\n"
        f"[bold]Total cost:[/bold] ${report.total_cost:.2f}  |  "
        f"[bold red]Addressable waste:[/bold red] ${report.total_waste:.2f}",
        title="Session Detail",
        border_style="cyan",
    ))

    # Token breakdown
    u = s.total_usage
    token_table = Table(title="Token breakdown", show_lines=False, header_style="bold")
    token_table.add_column("Category", style="dim")
    token_table.add_column("Tokens", justify="right")
    token_table.add_row("Input", f"{u.input_tokens:,}")
    token_table.add_row("Output", f"{u.output_tokens:,}")
    token_table.add_row("Cache writes", f"{u.cache_write_tokens:,}")
    token_table.add_row("Cache reads", f"{u.cache_read_tokens:,}")
    console.print(token_table)

    # Findings
    if report.findings:
        console.print()
        for f in report.findings:
            style = "bold red" if f.dollars > 0 else "yellow"
            console.print(f"  [{style}][{f.detector}][/{style}]  {f.summary}")
            if f.evidence_turns:
                turns_str = ", ".join(str(t) for t in f.evidence_turns[:10])
                if len(f.evidence_turns) > 10:
                    turns_str += f" ... (+{len(f.evidence_turns) - 10} more)"
                console.print(f"    [dim]Evidence turns: {turns_str}[/dim]")

            # Detector-specific details
            if f.detector == "redundant_reads" and "files" in f.detail:
                for fp, count in f.detail["files"].items():
                    short = fp.split("/")[-1]
                    console.print(f"    [dim]  {short}: {count} reads[/dim]")

            if f.detector == "doom_loops" and "max_run_key" in f.detail:
                key = f.detail["max_run_key"]
                if "::" in key:
                    tool, param = key.split("::", 1)
                    param_short = param.split("/")[-1][:80]
                    console.print(f"    [dim]  Longest run: {tool} on {param_short}[/dim]")

        console.print()
    else:
        console.print("\n  [green]No waste patterns detected.[/green]\n")

    console.print(f"[dim]Tip: run 'ccburn chart {args.id}' to generate a burn chart PNG.[/dim]")


# ── chart ─────────────────────────────────────────────────────────────

def cmd_chart(args):
    sessions = discover_sessions()
    reports = [analyze_session(s) for s in sessions]
    reports.sort(key=lambda r: r.total_cost, reverse=True)

    target = _match_session([r.session for r in reports], args.id)
    if not target:
        console.print(f"[red]No session matching '{args.id}'. Run 'ccburn scan' to list sessions.[/red]")
        return

    report = next(r for r in reports if r.session is target)
    path = render_burn_chart(report, output_path=args.output)
    console.print(f"[green]Burn chart written to:[/green] {path}")


# ── statusline ────────────────────────────────────────────────────────

# ANSI color helpers
_RESET = "\033[0m"
_YELLOW = "\033[33m"
_RED = "\033[31m"
_DIM = "\033[2m"


def _ratio_color(ratio: float | None) -> str:
    """Return ANSI color code based on cost ratio thresholds."""
    if ratio is None or ratio < 2.0:
        return ""
    if ratio < 5.0:
        return _YELLOW
    return _RED


def cmd_statusline(args):
    """Print one-line cost meter for Claude Code statusLine integration."""
    # Read stdin JSON from Claude Code
    stdin_data = {}
    try:
        raw = sys.stdin.read()
        if raw.strip():
            stdin_data = json.loads(raw)
    except (json.JSONDecodeError, OSError):
        pass

    transcript_path = stdin_data.get("transcript_path", "")
    if not transcript_path:
        # Can't do anything without a transcript
        return

    model_id = None
    model_info = stdin_data.get("model")
    if isinstance(model_info, dict):
        model_id = model_info.get("id")

    try:
        m = get_live_metrics(transcript_path, model_override=model_id)
    except Exception:
        return  # fail silent — statusline must never error

    if args.json:
        out = {
            "session_cost": m.session_cost,
            "marginal_cost": m.marginal_cost,
            "baseline_marginal": m.baseline_marginal,
            "ratio": m.ratio,
            "compact_delta": m.compact_delta,
            "context_tokens": m.context_tokens,
            "num_turns": m.num_turns,
            "model": m.last_model,
            "tier": m.last_tier,
        }
        print(json.dumps(out))
        return

    _print_statusline(m)


def _print_statusline(m: LiveMetrics) -> None:
    """Format and print the one-line statusline output."""
    parts = [f"$={m.session_cost:.2f}"]

    if m.marginal_cost is not None:
        color = _ratio_color(m.ratio)
        reset = _RESET if color else ""
        ratio_str = f" ({m.ratio:.1f}x)" if m.ratio is not None else ""
        parts.append(f"{color}next {chr(0x2248)} ${m.marginal_cost:.2f}/turn{ratio_str}{reset}")

        if m.compact_delta is not None and m.ratio is not None and m.ratio >= 2.0:
            # Show post-compact marginal estimate
            post_compact_marginal = m.marginal_cost - (m.compact_delta / 20.0) if m.compact_delta else m.marginal_cost
            if post_compact_marginal < 0:
                post_compact_marginal = 0.01
            parts.append(f"compact {chr(0x2192)} {chr(0x2248)}${post_compact_marginal:.2f}/turn")

    print(" | ".join(parts))


# ── hook ──────────────────────────────────────────────────────────────

# Firing policy thresholds (configurable via future config file)
WARN_TIERS = [4, 8]       # ratio thresholds
WARN_FLOOR = 0.15         # minimum marginal cost to warn ($)
WARN_COMPACT_MIN = 2.0    # minimum compact_delta to warn ($)


def cmd_hook(args):
    """UserPromptSubmit hook: emit cost warning via systemMessage."""
    # All failures -> exit 0, no output (fail open)
    try:
        _run_hook(args)
    except Exception:
        pass


def _run_hook(args):
    event = args.event or "prompt_submit"

    stdin_data = {}
    try:
        raw = sys.stdin.read()
        if raw.strip():
            stdin_data = json.loads(raw)
    except (json.JSONDecodeError, OSError):
        return

    transcript_path = stdin_data.get("transcript_path", "")
    if not transcript_path:
        return

    # PreCompact event: reset warning tiers
    if event == "precompact":
        state = read_live_state(transcript_path)
        state.warned_tiers = []
        _save_cache(transcript_path, state)
        return

    # UserPromptSubmit: check if we should warn
    model_id = None
    model_info = stdin_data.get("model")
    if isinstance(model_info, dict):
        model_id = model_info.get("id")

    state = read_live_state(transcript_path)
    m = compute_metrics(state, model_override=model_id)

    # Check firing policy
    if m.ratio is None or m.marginal_cost is None:
        return
    if m.marginal_cost < WARN_FLOOR:
        return
    if m.compact_delta is None or m.compact_delta < WARN_COMPACT_MIN:
        return

    # Determine which tier to fire
    fire_tier = None
    for tier_threshold in WARN_TIERS:
        if m.ratio >= tier_threshold and tier_threshold not in state.warned_tiers:
            fire_tier = tier_threshold

    if fire_tier is None:
        return

    # Record that we warned at this tier
    state.warned_tiers.append(fire_tier)
    _save_cache(transcript_path, state)

    # Build the warning message
    ratio_str = f"{m.ratio:.0f}x" if m.ratio >= 10 else f"{m.ratio:.1f}x"
    msg = (
        f"ccburn: next turn \u2248 ${m.marginal_cost:.2f} "
        f"({ratio_str} session start). "
        f"/compact now \u2248 saves ${m.compact_delta:.0f} at your current pace."
    )

    # Emit structured JSON so the message goes to the user
    # without entering model context
    output = {
        "suppressOutput": True,
        "systemMessage": msg,
    }
    print(json.dumps(output))


# ── install / uninstall ───────────────────────────────────────────────

SETTINGS_PATH = Path.home() / ".claude" / "settings.json"

CCBURN_STATUSLINE = {
    "type": "command",
    "command": "ccburn statusline",
}

CCBURN_HOOK_PROMPT = {
    "type": "command",
    "command": "ccburn hook",
    "timeout": 10,
}

CCBURN_HOOK_PRECOMPACT = {
    "type": "command",
    "command": "ccburn hook --event precompact",
    "timeout": 10,
}

# Marker to identify ccburn-managed entries
_CCBURN_MARKER = "ccburn"


def _load_settings() -> dict:
    if SETTINGS_PATH.exists():
        with open(SETTINGS_PATH) as f:
            return json.load(f)
    return {}


def _save_settings(settings: dict, backup: bool = True) -> None:
    if backup and SETTINGS_PATH.exists():
        bak = SETTINGS_PATH.with_suffix(".json.bak")
        shutil.copy2(SETTINGS_PATH, bak)
        console.print(f"  [dim]Backed up to {bak}[/dim]")

    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(SETTINGS_PATH, "w") as f:
        json.dump(settings, f, indent=2)
        f.write("\n")


def _is_ccburn_entry(entry: dict) -> bool:
    cmd = entry.get("command", "")
    return isinstance(cmd, str) and _CCBURN_MARKER in cmd


def cmd_install(args):
    """Idempotently register ccburn statusline + hooks in settings.json."""
    settings = _load_settings()
    original = json.dumps(settings, indent=2)
    changed = False

    # --- StatusLine ---
    existing_sl = settings.get("statusLine")
    if existing_sl and not _is_ccburn_entry(existing_sl):
        console.print(
            "[yellow]statusLine already configured.[/yellow] "
            "Consider using [bold]ccburn statusline --json[/bold] as a segment "
            "in your existing statusline tool."
        )
    elif existing_sl != CCBURN_STATUSLINE:
        settings["statusLine"] = CCBURN_STATUSLINE
        changed = True

    # --- Hooks ---
    hooks = settings.setdefault("hooks", {})

    # UserPromptSubmit
    prompt_hooks = hooks.setdefault("UserPromptSubmit", [])
    if not any(_is_ccburn_entry(h) for h in prompt_hooks):
        prompt_hooks.append(CCBURN_HOOK_PROMPT)
        changed = True

    # PreCompact
    precompact_hooks = hooks.setdefault("PreCompact", [])
    if not any(_is_ccburn_entry(h) for h in precompact_hooks):
        precompact_hooks.append(CCBURN_HOOK_PRECOMPACT)
        changed = True

    if not changed:
        console.print("[green]ccburn is already installed.[/green]")
        return

    _save_settings(settings)

    # Print diff
    updated = json.dumps(settings, indent=2)
    console.print("\n[bold]Changes to ~/.claude/settings.json:[/bold]")
    _print_diff(original, updated)
    console.print("\n[green]ccburn installed.[/green] Restart Claude Code to activate.")


def cmd_uninstall(args):
    """Remove ccburn entries from settings.json."""
    if not SETTINGS_PATH.exists():
        console.print("[dim]No settings.json found — nothing to uninstall.[/dim]")
        return

    settings = _load_settings()
    original = json.dumps(settings, indent=2)
    changed = False

    # Remove statusLine if it's ours
    if _is_ccburn_entry(settings.get("statusLine", {})):
        del settings["statusLine"]
        changed = True

    # Remove hooks
    hooks = settings.get("hooks", {})
    for event_name in ["UserPromptSubmit", "PreCompact"]:
        if event_name in hooks:
            before = len(hooks[event_name])
            hooks[event_name] = [h for h in hooks[event_name] if not _is_ccburn_entry(h)]
            if len(hooks[event_name]) < before:
                changed = True
            if not hooks[event_name]:
                del hooks[event_name]

    if not hooks:
        settings.pop("hooks", None)

    if not changed:
        console.print("[dim]No ccburn entries found — nothing to uninstall.[/dim]")
        return

    _save_settings(settings)

    updated = json.dumps(settings, indent=2)
    console.print("\n[bold]Changes to ~/.claude/settings.json:[/bold]")
    _print_diff(original, updated)
    console.print("\n[green]ccburn uninstalled.[/green]")


def _print_diff(before: str, after: str) -> None:
    """Print a simple line-by-line diff."""
    before_lines = before.splitlines()
    after_lines = after.splitlines()

    import difflib
    diff = difflib.unified_diff(before_lines, after_lines, lineterm="", n=2)
    for line in diff:
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("@@"):
            console.print(f"  [dim]{line}[/dim]")
        elif line.startswith("+"):
            console.print(f"  [green]{line}[/green]")
        elif line.startswith("-"):
            console.print(f"  [red]{line}[/red]")
        else:
            console.print(f"  {line}")


# ── main ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        prog="ccburn",
        description="Explain why your Claude Code sessions are expensive.",
    )
    sub = parser.add_subparsers(dest="command")

    # scan
    p_scan = sub.add_parser("scan", help="Overview of all sessions and costs")
    p_scan.add_argument("--project", "-p", help="Filter to project name (substring match)")

    # session
    p_sess = sub.add_parser("session", help="Deep dive into a single session")
    p_sess.add_argument("id", help="Session ID prefix or index from scan output")

    # chart
    p_chart = sub.add_parser("chart", help="Generate burn chart PNG")
    p_chart.add_argument("id", help="Session ID prefix or index from scan output")
    p_chart.add_argument("--output", "-o", help="Output file path (default: ccburn-chart-<id>.png)")

    # statusline
    p_status = sub.add_parser("statusline", help="Live cost meter for Claude Code statusLine")
    p_status.add_argument("--json", action="store_true", dest="json",
                          help="Emit raw JSON for embedding in other statusline tools")

    # hook
    p_hook = sub.add_parser("hook", help="UserPromptSubmit hook for cost warnings")
    p_hook.add_argument("--event", default=None,
                        help="Hook event type (default: prompt_submit, or precompact)")

    # install / uninstall
    sub.add_parser("install", help="Register ccburn statusline + hooks in Claude Code settings")
    sub.add_parser("uninstall", help="Remove ccburn from Claude Code settings")

    args = parser.parse_args()
    if args.command == "scan":
        cmd_scan(args)
    elif args.command == "session":
        cmd_session(args)
    elif args.command == "chart":
        cmd_chart(args)
    elif args.command == "statusline":
        cmd_statusline(args)
    elif args.command == "hook":
        cmd_hook(args)
    elif args.command == "install":
        cmd_install(args)
    elif args.command == "uninstall":
        cmd_uninstall(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
