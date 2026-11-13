"""Run the trial scenarios, each as its own isolated agent process, and write a run log.

Per trial: a fresh empty working folder, its own capture copy (so no cache entry is shared), the
plugin under test copied from a git revision, and the scenario's question with the real paths.
The runner (see runners/) starts the agent and returns a neutral transcript. This script never
judges an answer; trials/observer.md is the rubric for that, applied separately. It needs a model
API and is not part of CI.

Usage:
    python trials/run_trials.py --run-dir <NEW_DIR> --runner claude-code --model <MODEL>
        [--plugin-rev REV | --plugin-dir DIR] [--scenario SCN-02 ...] [--parallel 4]
        [--timeout 900] [--path-prefix DIR ...] [--runner-option key=value ...]

Choose a model in the class the skill is optimized for: a small, fast one. --path-prefix puts
directories first on the trial's PATH (a Python with DuckDB, LLVM), as an installed user has them.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import shutil
import subprocess
import sys
import tarfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

TRIALS = Path(__file__).resolve().parent
REPO = TRIALS.parent
sys.path.insert(0, str(TRIALS))

import build_captures
from runners import TrialSpec, runners

FIELD = re.compile(r"^- \*\*(\w[\w ]*):\*\*\s*(.*)$")
# Committed reference captures, as repository paths, and how a Session names them.
CAPTURES = "trials/defect_lab/captures"
IMAGES = "images"
COMMITTED = "defect_lab/"


def catalogue() -> dict[str, dict[str, str]]:
    """Scenario ID -> its fields from scenarios.md."""
    result: dict[str, dict[str, str]] = {}
    text = (TRIALS / "scenarios.md").read_text(encoding="utf-8")
    for block in text.split("\n## ")[1:]:
        title, _, body = block.partition("\n")
        match = re.match(r"(SCN-\d\d): ", title)
        if match:
            result[match.group(1)] = {
                m.group(1): m.group(2) for line in body.splitlines() if (m := FIELD.match(line))
            }
    return result


def sessions_of(fields: dict[str, str]) -> list[str]:
    session = fields.get("Session", "")
    return [] if session.startswith("none") else re.findall(r"`([^`]+)`", session)


def question(fields: dict[str, str]) -> str:
    text = fields["Question"].strip()
    return text[1:-1] if text.startswith('"') and text.endswith('"') else text


def plugin_copy(run_dir: Path, rev: str | None, directory: Path | None) -> tuple[Path, str]:
    """The plugin under test in run_dir/plugin, and where it came from."""
    target = run_dir / "plugin"
    if directory is not None:
        shutil.copytree(directory, target)
        return target, f"copy of {directory}"
    commit = subprocess.run(
        ["git", "-C", str(REPO), "rev-parse", rev], check=True, capture_output=True, text=True
    ).stdout.strip()
    archive = subprocess.run(
        ["git", "-C", str(REPO), "archive", "--format=tar", commit, "plugin"],
        check=True,
        capture_output=True,
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        try:
            tar.extractall(run_dir, filter="data")
        except TypeError:  # Python without extraction filters; the archive is our own
            tar.extractall(run_dir)
    return target, f"git archive {commit} plugin"


def git(*args: str) -> bytes:
    return subprocess.run(["git", "-C", str(REPO), *args], check=True, capture_output=True).stdout


def capture_revision(plugin_rev: str, plugin_dir: Path | None) -> str:
    """The commit the committed captures come from: the plugin's revision, else HEAD.

    Captures are never taken from the working tree; a tested plugin folder is not a revision.
    """
    return git("rev-parse", "HEAD" if plugin_dir is not None else plugin_rev).decode().strip()


def tree_names(rev: str, folder: str) -> list[str]:
    listing = git("ls-tree", "--name-only", rev, f"{folder}/").decode().split()
    return [name.rsplit("/", 1)[-1] for name in listing]


def choose_platform(platforms: list[str], name: str, rev: str) -> str:
    if not platforms:
        raise ValueError(f"no platform folder under {CAPTURES} at {rev}")
    if len(platforms) > 1:
        raise ValueError(
            f"{name}: several platforms under {CAPTURES} at {rev} ({', '.join(platforms)}); "
            f"name one as {COMMITTED}<platform>/{name}"
        )
    return platforms[0]


def committed_folder(name: str, rev: str) -> str:
    """The repository path of a committed capture named defect_lab/[<platform>/]<capture>."""
    parts = name[len(COMMITTED) :].split("/")
    platforms = tree_names(rev, CAPTURES)
    if len(parts) == 2:
        platform, capture = parts
        if platform not in platforms:
            raise ValueError(
                f"no platform {platform} under {CAPTURES} at {rev}; "
                f"available: {', '.join(platforms) or 'none'}"
            )
    else:
        capture = parts[0]
        platform = choose_platform(platforms, capture, rev)
    available = [n for n in tree_names(rev, f"{CAPTURES}/{platform}") if n != IMAGES]
    if capture not in available:
        raise ValueError(f"no committed capture {name} at {rev}; available: {', '.join(available)}")
    return f"{CAPTURES}/{platform}/{capture}"


def extract(rev: str, folder: str, target: Path) -> None:
    """Write a committed folder's files from rev into target (not from the working tree)."""
    archive = git("archive", "--format=tar", rev, folder)
    target.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        for member in tar.getmembers():
            if member.isfile():
                relative = member.name[len(folder) + 1 :]
                path = target / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(tar.extractfile(member).read())


def prepare(
    run_dir: Path, scn: str, fields: dict[str, str], rev: str = "HEAD"
) -> tuple[str, Path, list[str]]:
    """Set up the trial's captures and return its prompt with real paths, the capture root and
    the "<commit>:<path>" source of each committed capture.

    Built sessions come from build_captures; a name defect_lab/[<platform>/]<capture> is copied
    with its platform's images/ from the committed revision rev, so the trial reads it inside
    its own capture root.
    """
    names = sessions_of(fields)
    root = run_dir / "captures" / scn
    commit = git("rev-parse", rev).decode().strip()
    # Refuse an unknown committed name before anything is built.
    folders = {name: committed_folder(name, commit) for name in names if name.startswith(COMMITTED)}
    built_names = [name for name in names if name not in folders]
    built = build_captures.build(root, built_names) if built_names else {}
    # Neutral session names, so the folder names do not describe the scenario.
    labels = ["before", "after"] if len(names) == 2 else ["capture"]
    paths = {}
    sources = []
    for label, name in zip(labels, names):
        if name in folders:
            extract(commit, folders[name], root / label)
            extract(commit, f"{folders[name].rsplit('/', 1)[0]}/{IMAGES}", root / IMAGES)
            sources.append(f"{commit}:{folders[name]}")
        else:
            built[name].rename(root / label)
        paths[label] = root / label
    prompt = question(fields)
    replacements = {
        "<CAPTURE>": paths.get("capture"),
        "<BEFORE>": paths.get("before"),
        "<AFTER>": paths.get("after"),
        "<PDB_DIR>": root / "bins" / "pdbs",
        # A committed capture brings the images it was recorded with.
        "<IMAGE_DIR>": root / IMAGES if folders else root / "bins" / "timer",
    }
    for placeholder, path in replacements.items():
        if path is not None:
            prompt = prompt.replace(placeholder, str(path))
    prompt = prompt.replace("`", "")
    if re.search(r"<[A-Z_]+>", prompt):
        raise ValueError(f"{scn}: unreplaced placeholder in {prompt!r}")
    return prompt, root, sources


def context_limit(text: str | None = None) -> int:
    """The catalogue's context ceiling: a run that reaches this many tokens is defective."""
    if text is None:
        text = (TRIALS / "scenarios.md").read_text(encoding="utf-8")
    section = text.partition("\n## Pass criteria\n")[2].partition("\n## ")[0]
    match = re.search(r"fewer than ([\d,]+) tokens", section)
    if not match:
        raise ValueError("scenarios.md states no context limit")
    return int(match.group(1).replace(",", ""))


def context_verdict(tokens: int | None, limit: int) -> str:
    if tokens is None:
        return "not measured"
    return "below limit" if tokens < limit else "over limit"


def summarize(events: list[dict]) -> dict[str, object]:
    calls = [e for e in events if e["type"] == "tool_call"]
    tools: dict[str, int] = {}
    for call in calls:
        tools[call["tool"]] = tools.get(call["tool"], 0) + 1
    return {
        "tool_calls": len(calls),
        "tools": tools,
        "tool_errors": sum(1 for e in events if e["type"] == "tool_result" and e.get("is_error")),
        "answered": any(e["type"] == "final" and e.get("answer") for e in events),
    }


def outside_references(events: list[dict], denied: list[Path]) -> list[str]:
    """Tool inputs that name a denied root or another trial's folders (isolation evidence)."""
    found = []
    spellings = [s for root in denied for s in {str(root).lower(), root.as_posix().lower()}]
    for event in events:
        if event["type"] != "tool_call":
            continue
        text = json.dumps(event.get("input")).lower().replace("\\\\", "\\")
        for spelling in spellings:
            if spelling in text or spelling in text.replace("\\", "/"):
                found.append(f"{event['tool']}: {spelling}")
    return found


def run_one(
    runner, run_dir: Path, scn: str, fields, plugin: Path, args, others: list[str]
) -> dict[str, object]:
    prompt, capture_root, sources = prepare(run_dir, scn, fields, args.capture_rev)
    cwd = run_dir / "trials" / scn
    (cwd / "_temp").mkdir(parents=True)
    out = run_dir / "out" / scn
    out.mkdir(parents=True)
    # The repository, every run's output and the other trials' folders and captures.
    denied = [REPO, run_dir / "out"]
    denied += [
        run_dir / folder / other
        for other in others
        if other != scn
        for folder in ("captures", "trials")
    ]
    spec = TrialSpec(
        trial_id=scn,
        prompt=prompt,
        model=args.model,
        plugin_dir=plugin,
        capture_dir=capture_root,
        cwd=cwd,
        out_dir=out,
        read_roots=(cwd, plugin, capture_root),
        deny_roots=tuple(denied),
        timeout_s=args.timeout,
        path_prefix=tuple(args.path_prefix),
    )
    result = runner.run(spec)
    with open(out / "transcript.jsonl", "w", encoding="utf-8", newline="\n") as handle:
        handle.writelines(json.dumps(event, ensure_ascii=False) + "\n" for event in result.events)
    meta = {
        "scenario": scn,
        "prompt": prompt,
        "runner": runner.name,
        "model": args.model,
        "exit": result.exit_status,
        "wall_s": result.wall_s,
        "cwd": str(cwd),
        "capture": str(capture_root),
        "capture_source": sources,
        "evidence": result.evidence,
        "context_tokens": result.context_tokens,
        "context": context_verdict(result.context_tokens, context_limit()),
        **summarize(result.events),
        "outside_references": outside_references(result.events, denied),
    }
    (out / "meta.json").write_text(json.dumps(meta, indent=1, default=str), encoding="utf-8")
    print(
        f"{scn}: exit {result.exit_status}, {result.wall_s} s, {meta['tool_calls']} tool calls",
        flush=True,
    )
    return meta


def runlog(run_dir: Path, header: dict[str, str], metas: list[dict], skipped: dict[str, str]):
    lines = ["# Trial run log", ""]
    lines += ["| Item | Value |", "|---|---|"]
    lines += [f"| {key} | {value} |" for key, value in header.items()]
    lines += [
        "",
        "Answers are not judged here; apply `trials/observer.md` to the transcripts.",
        f"Context is the final-turn context in tokens; a run passes only below {context_limit()}.",
        "",
        (
            "| Scenario | Exit | Wall s | Tool calls | Tools | Tool errors | Answered | "
            "Outside references | Context |"
        ),
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for meta in sorted(metas, key=lambda m: m["scenario"]):
        tools = ", ".join(f"{k} {v}" for k, v in sorted(meta["tools"].items()))
        tokens = meta["context_tokens"]
        context = meta["context"] if tokens is None else f"{tokens} {meta['context']}"
        lines.append(
            f"| {meta['scenario']} | {meta['exit']} | {meta['wall_s']} | "
            f"{meta['tool_calls']} | {tools} | {meta['tool_errors']} | "
            f"{'yes' if meta['answered'] else 'no'} | "
            f"{'; '.join(meta['outside_references']) or 'none'} | {context} |"
        )
    lines += ["", "## Isolation evidence reported by the runner", ""]
    for meta in sorted(metas, key=lambda m: m["scenario"]):
        facts = "; ".join(f"{k}: {v}" for k, v in sorted(meta["evidence"].items()) if k != "skills")
        lines.append(f"- **{meta['scenario']}:** {facts}")
    if skipped:
        lines += ["", "## Skipped", "", "| Scenario | Reason |", "|---|---|"]
        lines += [f"| {scn} | {reason} |" for scn, reason in sorted(skipped.items())]
    (run_dir / "runlog.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args(argv: list[str] | None, available: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--run-dir", type=Path, required=True, help="new folder for this run")
    parser.add_argument("--runner", required=True, choices=available)
    parser.add_argument("--model", required=True, help="the model the runner passes on")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--plugin-rev", default="HEAD", help="git revision of plugin/")
    source.add_argument("--plugin-dir", type=Path, help="test this plugin folder instead")
    parser.add_argument("--scenario", action="append", default=[], metavar="SCN-nn")
    parser.add_argument("--parallel", type=int, default=4)
    parser.add_argument("--timeout", type=int, default=900, help="seconds per trial")
    parser.add_argument("--path-prefix", action="append", default=[], metavar="DIR")
    parser.add_argument("--runner-option", action="append", default=[], metavar="KEY=VALUE")
    args = parser.parse_args(argv)
    if args.run_dir.exists():
        parser.error(f"{args.run_dir} exists; choose a new folder")
    args.run_dir = args.run_dir.resolve()
    if args.run_dir == REPO or REPO in args.run_dir.parents:
        parser.error("--run-dir must be outside the repository, which the trials may not read")
    return args


def main(argv: list[str] | None = None, registry: dict[str, type] | None = None) -> int:
    registry = registry or runners()
    args = parse_args(argv, sorted(registry))
    options = dict(option.split("=", 1) for option in args.runner_option)
    runner = registry[args.runner](**options)
    scenarios = catalogue()
    chosen = args.scenario or list(scenarios)
    unknown = sorted(set(chosen) - set(scenarios))
    if unknown:
        raise SystemExit(f"unknown scenario(s): {', '.join(unknown)}")
    skipped = {scn: scenarios[scn]["Session"] for scn in chosen if not sessions_of(scenarios[scn])}
    runnable = [scn for scn in chosen if scn not in skipped]
    args.capture_rev = capture_revision(args.plugin_rev, args.plugin_dir)
    # Every committed capture must exist before any trial starts: trials run in parallel, so a
    # name checked only when its trial starts would let the others spend model calls first.
    for scn in runnable:
        for name in sessions_of(scenarios[scn]):
            if name.startswith(COMMITTED):
                try:
                    committed_folder(name, args.capture_rev)
                except ValueError as exc:
                    raise SystemExit(f"{scn}: {exc}") from exc
    args.run_dir.mkdir(parents=True)
    plugin, source = plugin_copy(
        args.run_dir, None if args.plugin_dir else args.plugin_rev, args.plugin_dir
    )
    (args.run_dir / "captures").mkdir()
    with ThreadPoolExecutor(max(1, args.parallel)) as pool:
        metas = list(
            pool.map(
                lambda scn: run_one(
                    runner, args.run_dir, scn, scenarios[scn], plugin, args, runnable
                ),
                runnable,
            )
        )
    header = {
        "Started": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "Runner": runner.name,
        "Model": args.model,
        "Plugin": source,
        "Committed captures": f"from commit {args.capture_rev}",
        "Timeout per trial": f"{args.timeout} s",
        "Parallel": str(args.parallel),
    }
    runlog(args.run_dir, header, metas, skipped)
    print(args.run_dir / "runlog.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
