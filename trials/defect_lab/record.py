"""Record the defect_lab capture set: every class under load, as timer, IBS and PMC captures.

Usage: python trials/defect_lab/record.py <bin_dir> <raw_dir> [--modes timer,ibs,pmc]
           [--classes memory,lock,...] [--anonymize <out_dir>]

<bin_dir> holds the images build.py wrote. For each class and mode this starts 24 copies of
lab_busy.exe under neutral names (busy_01.exe ... busy_24.exe) as the load, starts
defect_lab.exe --class <class>, records a 3 s system-wide capture, stops the processes it
started, and translates the capture with uprof.py. Modes:
  timer  AMDuProfCLI collect -e event=timer,interval=10 -a -d 3 (any x86 CPU)
  ibs    uprof.py collect --event ibs-op --seconds 3 (AMD CPU with IBS)
  pmc    uprof.py collect --event cycles --seconds 3 (CPU with core PMC access)
A mode the CPU or driver cannot record is reported and skipped. Two plain captures (ibs and pmc,
no workload) show the session.uprof key names. Each capture also gets <name>.keys.txt: the
session.uprof key paths with value types and no values, safe to hand over as they are.

--anonymize <out_dir> runs trials/anonymize_capture.py on every translated capture into
<out_dir>/<name>, keeping defect_lab and the stressors. The raw and translated captures hold
host details; hand over only the anonymized folders and the keys.txt files, after reading each
review.txt.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
UPROF = REPO / "plugin" / "skills" / "amd-uprof" / "tools" / "uprof.py"
ANONYMIZE = REPO / "trials" / "anonymize_capture.py"
CLASSES = ("memory", "lock", "branch", "os", "vendor", "wait", "chain", "efficient")
MODES = ("timer", "ibs", "pmc")
STRESSORS = 24
SECONDS = 3
INTERVAL_MS = 10
KEEP = ["defect_lab.exe"] + [f"busy_{index:02d}.exe" for index in range(1, STRESSORS + 1)]


def stressor_images(bin_dir: Path) -> list[Path]:
    images = []
    for index in range(1, STRESSORS + 1):
        image = bin_dir / f"busy_{index:02d}.exe"
        if not image.exists():
            shutil.copyfile(bin_dir / "lab_busy.exe", image)
        images.append(image)
    return images


def collect(mode: str, raw: Path) -> Path:
    """Record one capture; return the session directory."""
    if mode == "timer":
        subprocess.run(["AMDuProfCLI", "collect", "-e", f"event=timer,interval={INTERVAL_MS}",
                        "-a", "-d", str(SECONDS), "-o", str(raw)], check=True,
                       stdout=subprocess.DEVNULL)
        return raw
    event = {"ibs": "ibs-op", "pmc": "cycles"}[mode]
    done = subprocess.run([sys.executable, str(UPROF), "collect", "--out-dir", str(raw),
                           "--seconds", str(SECONDS), "--event", event],
                          capture_output=True, text=True)
    sys.stdout.write(done.stdout[-2000:])
    if done.returncode != 0:
        raise RuntimeError(done.stderr.strip()[-2000:] or f"collect exited {done.returncode}")
    match = re.search(r"^SESSION=(.+)$", done.stdout, re.M)
    return Path(match.group(1).strip()) if match else raw


def key_paths(value, path=""):
    if isinstance(value, dict):
        for key, item in value.items():
            yield from key_paths(item, f"{path}.{key}" if path else key)
    elif isinstance(value, list):
        for item in value[:3]:
            yield from key_paths(item, f"{path}[]")
    else:
        yield f"{path}: {type(value).__name__}"


def write_keys(session: Path, target: Path) -> None:
    meta = json.loads((session / "session.uprof").read_text(encoding="utf-8-sig"))
    target.write_text("\n".join(sorted(set(key_paths(meta)))) + "\n", encoding="utf-8")


def translate(session: Path, translated: Path) -> None:
    subprocess.run([sys.executable, str(UPROF), "translate", "--session", str(session),
                    "--out-dir", str(translated)], check=True, stdout=subprocess.DEVNULL)


def record(bin_dir: Path, raw_dir: Path, name: str, mode: str) -> Path | None:
    started = [subprocess.Popen([str(image)], cwd=bin_dir)
               for image in stressor_images(bin_dir)]
    # AMDuProfCLI takes several seconds to start sampling, so the workload runs well past the
    # capture and is stopped afterwards.
    started.append(subprocess.Popen([str(bin_dir / "defect_lab.exe"), "--class", name,
                                     "--seconds", "40"], cwd=bin_dir,
                                    stdout=subprocess.DEVNULL))
    raw = raw_dir / f"{name}.{mode}.raw"
    try:
        time.sleep(1)
        session = collect(mode, raw)
    except (subprocess.CalledProcessError, RuntimeError) as exc:
        print(f"skipped {name} {mode}: {exc}", file=sys.stderr)
        return None
    finally:
        for process in started:
            process.kill()
            process.wait()
    write_keys(session, raw_dir / f"{name}.{mode}.keys.txt")
    translated = raw_dir / f"{name}.{mode}.translated"
    translate(session, translated)
    return translated


def plain(raw_dir: Path, mode: str) -> None:
    """A capture without workload, for the session.uprof key names of the mode."""
    try:
        session = collect(mode, raw_dir / f"plain.{mode}.raw")
    except (subprocess.CalledProcessError, RuntimeError) as exc:
        print(f"skipped plain {mode}: {exc}", file=sys.stderr)
        return
    write_keys(session, raw_dir / f"plain.{mode}.keys.txt")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("bin_dir", type=Path)
    parser.add_argument("raw_dir", type=Path)
    parser.add_argument("--modes", default=",".join(MODES))
    parser.add_argument("--classes", default=",".join(CLASSES))
    parser.add_argument("--anonymize", type=Path, metavar="OUT_DIR")
    args = parser.parse_args(argv)
    bin_dir, raw_dir = args.bin_dir.resolve(), args.raw_dir.resolve()
    raw_dir.mkdir(parents=True, exist_ok=True)
    modes = [mode for mode in args.modes.split(",") if mode]
    translated = []
    for mode in modes:
        if mode in ("ibs", "pmc"):
            plain(raw_dir, mode)
        for name in args.classes.split(","):
            result = record(bin_dir, raw_dir, name, mode)
            if result:
                print(result)
                translated.append((f"{name}.{mode}", result))
    if args.anonymize:
        for label, session in translated:
            code = subprocess.run([sys.executable, str(ANONYMIZE), str(session),
                                   "--keep-process", *KEEP, "--out",
                                   str(args.anonymize / label)]).returncode
            print(f"anonymize {label}: exit {code}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
