"""
Burn chart: per-turn cost over session lifetime.

Stacked area chart (cache-read vs other cost), with a vertical line
at the suggested split point and an annotation showing savings.
"""

from __future__ import annotations

import os
from typing import Optional

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker

from .analyzer import SessionReport, Finding


def render_burn_chart(
    report: SessionReport,
    output_path: Optional[str] = None,
) -> str:
    """Write a burn-chart PNG and return the file path."""
    if output_path is None:
        sid = report.session.session_id[:12]
        output_path = f"ccburn-chart-{sid}.png"

    turns = report.per_turn_costs
    xs = [t.index for t in turns]
    cache_reads = [t.cache_read for t in turns]
    others = [t.other for t in turns]

    # Cumulative cost for the secondary y-axis
    cumulative = []
    running = 0.0
    for t in turns:
        running += t.total
        cumulative.append(running)

    fig, ax1 = plt.subplots(figsize=(14, 5))
    fig.patch.set_facecolor("#1a1a2e")
    ax1.set_facecolor("#1a1a2e")

    # Stacked area: cache-read (red/orange) on bottom, other (blue) on top
    ax1.stackplot(
        xs, cache_reads, others,
        labels=["Cache-read cost", "Other cost"],
        colors=["#e74c3c", "#3498db"],
        alpha=0.85,
    )

    ax1.set_xlabel("Turn", color="#cccccc", fontsize=11)
    ax1.set_ylabel("Cost per turn ($)", color="#cccccc", fontsize=11)
    ax1.tick_params(colors="#999999")
    ax1.yaxis.set_major_formatter(ticker.FormatStrFormatter("$%.2f"))

    # Cumulative line on secondary axis
    ax2 = ax1.twinx()
    ax2.plot(xs, cumulative, color="#2ecc71", linewidth=1.5, alpha=0.8, label="Cumulative cost")
    ax2.set_ylabel("Cumulative cost ($)", color="#2ecc71", fontsize=11)
    ax2.tick_params(axis="y", colors="#2ecc71")
    ax2.yaxis.set_major_formatter(ticker.FormatStrFormatter("$%.0f"))

    # Split point annotation
    cache_finding = _find_cache_burn(report)
    if cache_finding and cache_finding.detail.get("split_turn", -1) >= 0:
        split = cache_finding.detail["split_turn"]
        savings = cache_finding.detail["split_savings"]
        ax1.axvline(x=split, color="#f1c40f", linewidth=2, linestyle="--", alpha=0.9)
        # Place annotation in upper portion of chart
        y_max = ax1.get_ylim()[1]
        ax1.annotate(
            f"Split here → save ~${savings:.2f}",
            xy=(split, y_max * 0.85),
            fontsize=10,
            color="#f1c40f",
            fontweight="bold",
            ha="left" if split < len(xs) * 0.6 else "right",
            va="top",
        )

    # Title
    project = report.session.project.replace("-Users-yasharora-Documents-", "")
    title = (
        f"{project}  |  {len(turns)} turns  |  "
        f"\\${report.total_cost:.2f} total  |  "
        f"\\${report.total_waste:.2f} addressable waste"
    )
    ax1.set_title(title, color="#eeeeee", fontsize=12, pad=12)

    # Legend
    lines1, labels1 = ax1.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    ax1.legend(
        lines1 + lines2, labels1 + labels2,
        loc="upper left", fontsize=9,
        facecolor="#2a2a3e", edgecolor="#444444", labelcolor="#cccccc",
    )

    fig.tight_layout()
    fig.savefig(output_path, dpi=150, facecolor=fig.get_facecolor())
    plt.close(fig)
    return os.path.abspath(output_path)


def _find_cache_burn(report: SessionReport) -> Optional[Finding]:
    for f in report.findings:
        if f.detector == "cache_burn":
            return f
    return None
