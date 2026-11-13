"""Runner for the Claude Code CLI in print mode (`claude -p`).

Isolation: no user, project or local settings or memory (`--setting-sources ""`), no MCP servers,
only the plugin under test (`--plugin-dir`), and generated permission settings that allow Read,
Grep and Glob only under the trial's read roots, Edit and Write only in its cwd, and deny the
deny roots, subagents and web access. Anything not allowed is denied without a prompt
(`dontAsk`). Bash and PowerShell stay allowed, because analysis needs them; their writes outside
the cwd cannot be blocked without the CLI sandbox, so temp variables point into the cwd.

Options (`--runner-option key=value`): `executable` (default `claude` on PATH).
"""

from __future__ import annotations

import json
from pathlib import Path, PureWindowsPath

from runners import TrialResult, TrialSpec, run_process, trial_env

# Host session variables that would tie the trial to the session that started it.
DROP_PREFIXES = ("CLAUDE",)
DROP = ("AI_AGENT", "BAGGAGE", "SENTRY_TRACE")


def rule_path(path: Path) -> str:
    """The absolute-path form of a permission rule: //c/dir on Windows, //dir on POSIX."""
    windows = PureWindowsPath(path)
    if windows.drive and len(windows.drive) == 2:
        rest = windows.as_posix()[2:]
        return "//" + windows.drive[0].lower() + rest
    return "/" + Path(path).as_posix()


def settings(spec: TrialSpec) -> dict:
    allow = ["Bash", "PowerShell"]
    allow += [f"{tool}({rule_path(spec.cwd)}/**)" for tool in ("Edit", "Write")]
    allow += [f"{tool}({rule_path(root)}/**)" for root in spec.read_roots
              for tool in ("Read", "Grep", "Glob")]
    deny = [f"{tool}({rule_path(root)}/**)" for root in spec.deny_roots
            for tool in ("Read", "Grep", "Glob", "Edit", "Write")]
    for root in spec.deny_roots:
        for spelling in dict.fromkeys([str(root), Path(root).as_posix()]):
            deny += [f"Bash(*{spelling}*)", f"PowerShell(*{spelling}*)"]
    deny += ["Agent", "WebFetch", "WebSearch"]
    return {"permissions": {"allow": allow, "deny": deny, "defaultMode": "dontAsk"}}


def command(executable: str, spec: TrialSpec, settings_file: Path) -> list[str]:
    return [executable, "-p", spec.prompt, "--model", spec.model,
            "--output-format", "stream-json", "--verbose",
            "--setting-sources", "", "--settings", str(settings_file),
            "--plugin-dir", str(spec.plugin_dir),
            "--permission-mode", "dontAsk", "--permission-prompts", "none",
            "--no-session-persistence", "--strict-mcp-config"]


def _text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(part.get("text", "") for part in content if isinstance(part, dict))
    return ""


def convert(lines: list[str]) -> tuple[list[dict], dict[str, object]]:
    """Neutral events and isolation evidence from a stream-json transcript."""
    events: list[dict] = []
    evidence: dict[str, object] = {}
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "init":
            evidence.update({
                "model": event.get("model"), "cwd": event.get("cwd"),
                "permission_mode": event.get("permissionMode"),
                "plugins": [f"{p.get('name')} {p.get('path')}" for p in event.get("plugins", [])],
                "mcp_servers": len(event.get("mcp_servers", [])),
                "skills": event.get("skills")})
        elif kind == "assistant":
            nested = event.get("parent_tool_use_id") is not None
            for block in event.get("message", {}).get("content", []):
                if block.get("type") == "text" and not nested:
                    events.append({"type": "assistant_text", "text": block.get("text", "")})
                elif block.get("type") == "tool_use":
                    call = {"type": "tool_call", "id": block.get("id"),
                            "tool": block.get("name"), "input": block.get("input")}
                    if nested:
                        call["nested"] = True
                    events.append(call)
        elif kind == "user":
            content = event.get("message", {}).get("content", [])
            for block in content if isinstance(content, list) else []:
                if block.get("type") == "tool_result":
                    events.append({"type": "tool_result", "id": block.get("tool_use_id"),
                                   "is_error": bool(block.get("is_error")),
                                   "output": _text(block.get("content"))})
        elif kind == "result":
            events.append({"type": "final", "answer": event.get("result") or ""})
            evidence.update({
                "result": event.get("subtype"), "is_error": event.get("is_error"),
                "num_turns": event.get("num_turns"), "cost_usd": event.get("total_cost_usd"),
                "permission_denials": [d.get("tool_name")
                                       for d in event.get("permission_denials", [])]})
    return events, evidence


CONTEXT_FIELDS = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


def final_context_tokens(lines: list[str]) -> int | None:
    """The main agent's input at its last turn: uncached, cache-read and cache-written tokens.

    The result event's usage sums all turns, and a subagent's turns have their own context, so
    the count comes from the last top-level assistant message that reports usage.
    """
    tokens = None
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") != "assistant" or event.get("parent_tool_use_id") is not None:
            continue
        usage = event.get("message", {}).get("usage")
        if isinstance(usage, dict) and any(name in usage for name in CONTEXT_FIELDS):
            tokens = sum(int(usage.get(name) or 0) for name in CONTEXT_FIELDS)
    return tokens


class ClaudeCodeRunner:
    name = "claude-code"

    def __init__(self, executable: str = "claude") -> None:
        self.executable = executable

    def run(self, spec: TrialSpec) -> TrialResult:
        settings_file = spec.out_dir / "settings.json"
        settings_file.write_text(json.dumps(settings(spec), indent=1), encoding="utf-8")
        raw, stderr = spec.out_dir / "raw.stream.jsonl", spec.out_dir / "stderr.txt"
        status, wall = run_process(command(self.executable, spec, settings_file), spec.cwd,
                                   trial_env(spec, DROP_PREFIXES, DROP), raw, stderr,
                                   spec.timeout_s)
        lines = raw.read_text(encoding="utf-8", errors="replace").splitlines()
        events, evidence = convert(lines)
        evidence["setting_sources"] = "none"
        return TrialResult(events, status, wall, evidence,
                           context_tokens=final_context_tokens(lines))
