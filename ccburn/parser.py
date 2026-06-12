"""
Tolerant JSONL parser for Claude Code session logs.

Reads ~/.claude/projects/**/*.jsonl and produces typed dataclasses
suitable for analysis. Malformed lines are silently skipped.
"""

from __future__ import annotations

import glob
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional

from .pricing import tier_for_model


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0

    @classmethod
    def from_dict(cls, d: dict) -> Usage:
        return cls(
            input_tokens=d.get("input_tokens", 0),
            output_tokens=d.get("output_tokens", 0),
            cache_write_tokens=d.get("cache_creation_input_tokens", 0),
            cache_read_tokens=d.get("cache_read_input_tokens", 0),
        )

    def __iadd__(self, other: Usage) -> Usage:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_write_tokens += other.cache_write_tokens
        self.cache_read_tokens += other.cache_read_tokens
        return self

    def as_dict(self) -> dict:
        """Return in the raw JSONL key format (for pricing functions)."""
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_creation_input_tokens": self.cache_write_tokens,
            "cache_read_input_tokens": self.cache_read_tokens,
        }


@dataclass
class ToolCall:
    name: str
    input: dict
    turn_index: int
    norm_key: str  # name::primary_param (for dedup detection)


@dataclass
class Turn:
    index: int
    timestamp: Optional[datetime]
    model: str
    tier: str
    usage: Usage
    tool_calls: List[ToolCall]


@dataclass
class Session:
    path: str
    session_id: str
    project: str
    cwd: Optional[str]
    cc_version: Optional[str]
    git_branch: Optional[str]
    turns: List[Turn]
    tool_errors: int
    total_usage: Usage = field(default_factory=Usage)
    first_ts: Optional[datetime] = None
    last_ts: Optional[datetime] = None

    @property
    def duration_hours(self) -> float:
        if self.first_ts and self.last_ts:
            return (self.last_ts - self.first_ts).total_seconds() / 3600
        return 0.0

    @property
    def num_turns(self) -> int:
        return len(self.turns)


# ── Helpers ───────────────────────────────────────────────────────────

def _parse_ts(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _norm_key(name: str, inp: dict) -> str:
    key_param = inp.get("file_path") or inp.get("command") or inp.get("pattern") or ""
    if isinstance(key_param, str):
        key_param = key_param[:300]
    else:
        key_param = str(key_param)[:300]
    return f"{name}::{key_param}"


# ── Public API ────────────────────────────────────────────────────────

def parse_session(path: str) -> Session:
    """Parse a single JSONL file into a Session."""
    turns: List[Turn] = []
    tool_errors = 0
    session_id: Optional[str] = None
    cwd: Optional[str] = None
    cc_version: Optional[str] = None
    git_branch: Optional[str] = None
    project = os.path.basename(os.path.dirname(path))

    with open(path, errors="replace") as fh:
        for line in fh:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            if not session_id:
                session_id = obj.get("sessionId")
            if not cwd:
                cwd = obj.get("cwd")
            if not cc_version:
                cc_version = obj.get("version")
            if not git_branch:
                git_branch = obj.get("gitBranch")

            line_type = obj.get("type")

            if line_type == "assistant":
                msg = obj.get("message", {})
                usage_raw = msg.get("usage")
                if not usage_raw:
                    continue

                usage = Usage.from_dict(usage_raw)
                model = msg.get("model", "")
                tier = tier_for_model(model)
                ts = _parse_ts(obj.get("timestamp"))

                tool_calls: List[ToolCall] = []
                content = msg.get("content", [])
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "tool_use":
                            tc_name = block.get("name", "")
                            tc_input = block.get("input", {})
                            tool_calls.append(ToolCall(
                                name=tc_name,
                                input=tc_input,
                                turn_index=len(turns),
                                norm_key=_norm_key(tc_name, tc_input),
                            ))

                turns.append(Turn(
                    index=len(turns),
                    timestamp=ts,
                    model=model,
                    tier=tier,
                    usage=usage,
                    tool_calls=tool_calls,
                ))

            elif line_type == "user":
                content = obj.get("message", {}).get("content", [])
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("is_error"):
                            tool_errors += 1

    # Compute aggregates
    total_usage = Usage()
    for turn in turns:
        total_usage += turn.usage

    first_ts = None
    last_ts = None
    for turn in turns:
        if turn.timestamp:
            if first_ts is None:
                first_ts = turn.timestamp
            last_ts = turn.timestamp

    return Session(
        path=path,
        session_id=session_id or os.path.splitext(os.path.basename(path))[0],
        project=project,
        cwd=cwd,
        cc_version=cc_version,
        git_branch=git_branch,
        turns=turns,
        tool_errors=tool_errors,
        total_usage=total_usage,
        first_ts=first_ts,
        last_ts=last_ts,
    )


def discover_sessions(root: Optional[str] = None) -> List[Session]:
    """Find and parse all JSONL session files under root."""
    if root is None:
        root = os.path.expanduser("~/.claude/projects")
    paths = sorted(glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True))
    sessions = []
    for p in paths:
        session = parse_session(p)
        if session.turns:  # skip empty / metadata-only files
            sessions.append(session)
    return sessions
