"""Record the defect_lab capture set: every class under load, as timer, IBS and PMC captures.

Usage: python trials/defect_lab/record.py <bin_dir> <raw_dir> [--modes timer,ibs,pmc]
           [--classes memory,lock,...] [--seconds S] [--ibs-interval OPS] [--timer-ms MS]
           [--pmc-interval TIME] [--stressors N] [--anonymize <out_dir>]

<bin_dir> holds the images build.py wrote. For each class and mode this starts N copies of
lab_busy.exe under neutral names (busy_01.exe ...) as the load, starts defect_lab.exe --class
<class>, records a short system-wide capture, stops the processes it started, and translates
the capture with uprof.py (an IBS capture with its ASCII dump). Modes:
  timer  AMDuProfCLI collect -e event=timer,interval=<ms> -a -d <S> (any x86 CPU)
  ibs    uprof.py collect --event ibs-op --sample-interval <ops> --seconds <S> (AMD with IBS)
  pmc    uprof.py collect --event cycles --sample-interval <time> --seconds <S>
A mode the CPU or driver cannot record is reported and skipped. Two plain captures (ibs and pmc,
no workload) show the session.uprof key names. Each capture also gets <name>.keys.txt: the
session.uprof key paths with value types and no values, safe to hand over as they are.

The defaults keep each capture small (about 1-2 MB with its dump, estimated) while the class's
own module still gets a few hundred samples: 2 s, IBS every 16,000,000 ops, timer and PMC every
4 ms, 15 stressors.

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
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
UPROF = REPO / "plugin" / "skills" / "amd-uprof" / "tools" / "uprof.py"
ANONYMIZE = REPO / "trials" / "anonymize_capture.py"
CLASSES = ("memory", "lock", "branch", "os", "vendor", "wait", "chain", "efficient")
MODES = ("timer", "ibs", "pmc")


@dataclass(frozen=True)
class Settings:
    seconds: int = 2
    ibs_interval: int = 16_000_000
    timer_ms: int = 4
    pmc_interval: str = "4ms"
    stressors: int = 15

    def keep(self) -> list[str]:
        return ["defect_lab.exe"] + [f"busy_{n:02d}.exe" for n in range(1, self.stressors + 1)]


def stressor_images(bin_dir: Path, count: int) -> list[Path]:
    images = []
    for index in range(1, count + 1):
        image = bin_dir / f"busy_{index:02d}.exe"
        if not image.exists():
            shutil.copyfile(bin_dir / "lab_busy.exe", image)
        images.append(image)
    return images


def collect(mode: str, raw: Path, settings: Settings) -> Path:
    """Record one capture; return the session directory."""
    if mode == "timer":
        subprocess.run(["AMDuProfCLI", "collect", "-e",
                        f"event=timer,interval={settings.timer_ms}", "-a", "-d",
                        str(settings.seconds), "-o", str(raw)], check=True,
                       stdout=subprocess.DEVNULL)
        return raw
    event, interval = {"ibs": ("ibs-op", str(settings.ibs_interval)),
                       "pmc": ("cycles", settings.pmc_interval)}[mode]
    done = subprocess.run([sys.executable, str(UPROF), "collect", "--out-dir", str(raw),
                           "--seconds", str(settings.seconds), "--event", event,
                           "--sample-interval", interval],
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


def translate(session: Path, translated: Path, mode: str) -> None:
    """Translate a capture; an IBS capture also gets its ASCII dump beside the session.

    The dump is generated here, where the raw files are; the handover carries only the
    translated tables and the anonymized dump, from which profile cannot regenerate it.
    """
    command = [sys.executable, str(UPROF), "translate", "--session", str(session),
               "--out-dir", str(translated)]
    if mode == "ibs":
        command += ["--ascii", "ibsop-event-dump"]
    subprocess.run(command, check=True, stdout=subprocess.DEVNULL)


def record(bin_dir: Path, raw_dir: Path, name: str, mode: str,
           settings: Settings) -> Path | None:
    started = [subprocess.Popen([str(image)], cwd=bin_dir)
               for image in stressor_images(bin_dir, settings.stressors)]
    # AMDuProfCLI takes several seconds to start sampling, so the workload runs well past the
    # capture and is stopped afterwards.
    started.append(subprocess.Popen([str(bin_dir / "defect_lab.exe"), "--class", name,
                                     "--seconds", "40"], cwd=bin_dir,
                                    stdout=subprocess.DEVNULL))
    raw = raw_dir / f"{name}.{mode}.raw"
    try:
        time.sleep(1)
        session = collect(mode, raw, settings)
    except (subprocess.CalledProcessError, RuntimeError) as exc:
        print(f"skipped {name} {mode}: {exc}", file=sys.stderr)
        return None
    finally:
        for process in started:
            process.kill()
            process.wait()
    write_keys(session, raw_dir / f"{name}.{mode}.keys.txt")
    translated = raw_dir / f"{name}.{mode}.translated"
    translate(session, translated, mode)
    return translated


def plain(raw_dir: Path, mode: str, settings: Settings) -> None:
    """A capture without workload, for the session.uprof key names of the mode."""
    try:
        session = collect(mode, raw_dir / f"plain.{mode}.raw", settings)
    except (subprocess.CalledProcessError, RuntimeError) as exc:
        print(f"skipped plain {mode}: {exc}", file=sys.stderr)
        return
    write_keys(session, raw_dir / f"plain.{mode}.keys.txt")


def parser() -> argparse.ArgumentParser:
    defaults = Settings()
    result = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    result.add_argument("bin_dir", type=Path)
    result.add_argument("raw_dir", type=Path)
    result.add_argument("--modes", default=",".join(MODES))
    result.add_argument("--classes", default=",".join(CLASSES))
    result.add_argument("--seconds", type=int, default=defaults.seconds)
    result.add_argument("--ibs-interval", type=int, default=defaults.ibs_interval,
                        metavar="OPS", help="IBS op-count interval (dispatched ops per sample)")
    result.add_argument("--timer-ms", type=int, default=defaults.timer_ms, metavar="MS")
    result.add_argument("--pmc-interval", default=defaults.pmc_interval, metavar="TIME")
    result.add_argument("--stressors", type=int, default=defaults.stressors, metavar="N")
    result.add_argument("--anonymize", type=Path, metavar="OUT_DIR")
    return result


def settings_from(args: argparse.Namespace) -> Settings:
    return Settings(seconds=args.seconds, ibs_interval=args.ibs_interval,
                    timer_ms=args.timer_ms, pmc_interval=args.pmc_interval,
                    stressors=args.stressors)


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    settings = settings_from(args)
    bin_dir, raw_dir = args.bin_dir.resolve(), args.raw_dir.resolve()
    raw_dir.mkdir(parents=True, exist_ok=True)
    modes = [mode for mode in args.modes.split(",") if mode]
    translated = []
    for mode in modes:
        if mode in ("ibs", "pmc"):
            plain(raw_dir, mode, settings)
        for name in args.classes.split(","):
            result = record(bin_dir, raw_dir, name, mode, settings)
            if result:
                print(result)
                translated.append((f"{name}.{mode}", result))
    if args.anonymize:
        failed = []
        for label, session in translated:
            code = subprocess.run([sys.executable, str(ANONYMIZE), str(session),
                                   "--keep-process", *settings.keep(), "--out",
                                   str(args.anonymize / label)]).returncode
            print(f"anonymize {label}: exit {code}")
            if code != 0:
                failed.append(label)
        if failed:
            print(f"error: anonymization failed for {', '.join(failed)}; do not hand those "
                  "folders over", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
