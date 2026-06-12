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
import sys

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.text import Text

from .parser import discover_sessions, Session
from .analyzer import analyze_session, SessionReport
from .pricing import usage_cost, cache_read_cost
from .chart import render_burn_chart

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

    args = parser.parse_args()
    if args.command == "scan":
        cmd_scan(args)
    elif args.command == "session":
        cmd_session(args)
    elif args.command == "chart":
        cmd_chart(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
