# ccburn

**ccusage tells you the bill. ccburn tells you why.**

A local-only CLI that reads your Claude Code session logs and explains where the money went — cache-read burn from marathon sessions, Opus spend on Sonnet-grade work, doom loops, redundant file re-reads. Every dollar of claimed waste points at specific turns you can audit.

![burn chart](assets/burn-chart.png)

*A real 617-turn session: $93.82 at API-equivalent rates. $60 of that was cache reads — the cost of Claude re-reading an ever-growing conversation every turn. Splitting at turn 154 would have saved ~$29.*

## The problem

Existing tools ([ccusage](https://github.com/ryoppippi/ccusage), ccost, ccflare, etc.) answer **"how much did I spend?"** — tables of tokens and dollars by day or session.

Nobody answers **"why was it expensive and what should I change?"**

ccburn does. It runs four detectors on your local session logs:

| Detector | What it finds | Example |
|----------|--------------|---------|
| **Cache-read burn** | Long sessions where cache reads dominate cost; suggests where to split | "64% of this session's cost was cache reads. Split at turn 154 → save ~$29" |
| **Model-mix waste** | Opus turns that only did simple tool work (Read, Glob, Bash, Edit) | "214 turns used Opus for file reads. At Sonnet rates, that's $31 saved" |
| **Redundant reads** | Same file read 3+ times with no edit in between | "handler.py read 4x — Claude lost track of context" |
| **Doom loops** | Runs of 3+ near-identical tool calls | "Edit on compute-construct.ts repeated 11x" |

All heuristics. All deterministic. Milliseconds to run. No LLM calls. Nothing leaves your machine.

## Install

```bash
pip install ccburn
```

Or from source:

```bash
git clone https://github.com/yasharora/ccburn.git
cd ccburn
pip install .
```

## Usage

### Scan all sessions

```
$ ccburn scan
```

Shows every session ranked by cost, with cache-read percentage, addressable waste, and the dominant waste pattern. Ends with a summary:

```
TOTAL ESTIMATED COST:  $305.67
TOTAL ESTIMATED WASTE: $177.15
Worst offender: football-trvia ($93.82, 617 turns)
```

Filter to a project:

```
$ ccburn scan --project football
```

### Deep dive into a session

```
$ ccburn session 0
```

Use the index from `scan` output or a session ID prefix. Shows token breakdown, all detector findings with evidence turn numbers, and per-file details.

### Generate a burn chart

```
$ ccburn chart 0
```

Writes a PNG with:
- **Stacked area**: cache-read cost (red) vs other cost (blue) per turn
- **Cumulative line**: total spend over the session
- **Split point**: dashed line where splitting the session would save the most

## Live mode: catch cost bloat before it happens

ccburn can also run *during* your session — a statusline meter and a hook that warns you when compacting would save real money.

### Setup (one command)

```bash
ccburn install
```

This registers two things in `~/.claude/settings.json`:
- A **statusline** showing live session cost and marginal cost per turn
- A **UserPromptSubmit hook** that warns when your context has grown expensive

To remove: `ccburn uninstall`

### Statusline

Once installed, your Claude Code status bar shows:

```
$=14.20 | next ≈ $0.31/turn (6.2x) | compact → ≈$0.06/turn
```

- **$=14.20** — session cost so far (API-equivalent estimate)
- **next ≈ $0.31/turn** — what your next message will cost, dominated by cache reads of the grown context
- **(6.2x)** — ratio vs the first 5 turns (your "baseline" cost)
- **compact → ≈$0.06/turn** — estimated per-turn cost after compacting

Color thresholds: default below 2x, yellow 2–5x, red above 5x.

Early/cheap sessions just show: `$=0.84 | next ≈ $0.05/turn`

For embedding in other statusline tools (ccstatusline, claude-powerline), use `ccburn statusline --json` to get raw values.

### Cost warning hook

The hook fires a `systemMessage` (shown to you, not injected into Claude's context) when ALL of:
- Cost ratio ≥ 4x your session baseline
- Marginal cost ≥ $0.15/turn (silences cheap Haiku/Sonnet sessions)
- Compacting would save ≥ $2

```
ccburn: next turn ≈ $0.32 (6x session start). /compact now ≈ saves $9 at your current pace.
```

Fires at most twice per session (4x and 8x tiers). Resets if you `/compact`. Never blocks your prompt — all failures exit silently.

### The key metric: marginal cost of next turn

This is what makes ccburn's live mode different from other statusline tools. ccusage shows session cost and $/hr burn rate. cc-safe-setup counts tool calls. Neither answers: **"what will my next message cost?"**

On Opus, a 120k-token context costs ≈$0.18 just in cache reads *per turn* — before Claude writes a single word. Auto-compact triggers near the context *limit* (a correctness threshold); cost-optimal compaction arrives much earlier.

**Auto-compact protects your context window. ccburn protects your wallet.**

### Assumptions and honesty

All dollar figures are API-equivalent **estimates** (marked with ≈). The compact savings estimate assumes:
- Post-compact context ≈ 15,000 tokens (configurable)
- Remaining turns estimated from your historical session lengths (fallback: 20 turns)
- Compaction has real costs — lost detail, possible file re-reads

ccburn tells you when compacting likely pays for itself. It never tells you that you must compact.

## How it works

Claude Code writes a JSONL log for every session to `~/.claude/projects/`. Each assistant turn includes token usage (input, output, cache writes, cache reads) and tool calls. ccburn:

1. **Parses** every JSONL file into typed session/turn objects
2. **Prices** each turn at API-equivalent rates (Opus $15/$75 per MTok in/out, Sonnet $3/$15, with cache pricing)
3. **Runs detectors** that flag waste patterns and attribute dollar amounts to specific turns
4. **Renders** tables (Rich) and charts (matplotlib)

Costs are estimates. Pro/Max plan users pay a flat rate — ccburn shows what the same usage would cost at API rates, so you can see relative waste even without a real bill.

## Why these detectors?

Built from a survey of 21 real sessions ($305 total). The data picked the features:

| Detector | Dollars found | Sessions hit |
|----------|--------------|-------------|
| Model-mix waste | $98 | 15 |
| Cache-read burn | $78 | 13 |
| Doom loops | — | 10 |
| Redundant reads | — | 3 |

Cache-read burn and model-mix waste account for essentially all quantifiable waste. The others are diagnostic signals — they tell you something went wrong even if the dollar amount is hard to pin down.

## Prior art

| Tool | What it does | Live? | Waste analysis? | Marginal cost? |
|------|-------------|-------|----------------|----------------|
| [ccusage](https://github.com/ryoppippi/ccusage) | Cost tables by day/session | Statusline (cost + $/hr) | No | No |
| [cc-safe-setup](https://github.com/ATheorell/cc-safe-setup) | Hook: warn on tool call count | Yes (hook) | No | No |
| [claude-token-analyzer](https://github.com/anthropics/claude-token-analyzer) | Statistical anomaly detection | No | Flags anomalies | No |
| ccost, ccflare, CCTracker | Various UIs on JSONL data | Varies | No | No |
| **ccburn** | Waste detectors + live cost warnings | Statusline + hook | Yes, with $ amounts | **Yes** |

Context warnings exist (tool-call counting). Cost warnings don't — except here.

## License

MIT
