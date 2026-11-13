"""Record system-wide timer captures of defect_lab under background load, one per class.

Usage: python trials/defect_lab/record.py <bin_dir> <out_dir> [CLASS ...]

<bin_dir> holds the images build.py wrote. For each class this starts 24 copies of
lab_busy.exe under neutral names (busy_01.exe ... busy_24.exe) as the load, starts
defect_lab.exe --class <class>, records a 3 s system-wide timer capture at a 10 ms interval
with AMDuProfCLI, stops the processes it started and translates the capture with uprof.py.
The raw and translated captures hold host details; commit only what export_capture.py writes.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
UPROF = REPO / "plugin" / "skills" / "amd-uprof" / "tools" / "uprof.py"
CLASSES = ("memory", "lock", "branch", "os", "vendor", "wait", "chain", "efficient")
STRESSORS = 24
SECONDS = 3
INTERVAL_MS = 10


def stressor_images(bin_dir: Path) -> list[Path]:
    images = []
    for index in range(1, STRESSORS + 1):
        image = bin_dir / f"busy_{index:02d}.exe"
        if not image.exists():
            shutil.copyfile(bin_dir / "lab_busy.exe", image)
        images.append(image)
    return images


def record(bin_dir: Path, out_dir: Path, name: str) -> Path:
    started = [subprocess.Popen([str(image)], cwd=bin_dir) for image in stressor_images(bin_dir)]
    # AMDuProfCLI takes several seconds to start sampling, so the workload runs well past the
    # capture and is stopped afterwards.
    started.append(
        subprocess.Popen(
            [str(bin_dir / "defect_lab.exe"), "--class", name, "--seconds", "30"],
            cwd=bin_dir,
            stdout=subprocess.DEVNULL,
        )
    )
    raw = out_dir / f"{name}.raw"
    try:
        time.sleep(1)
        subprocess.run(
            [
                "AMDuProfCLI",
                "collect",
                "-e",
                f"event=timer,interval={INTERVAL_MS}",
                "-a",
                "-d",
                str(SECONDS),
                "-o",
                str(raw),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
        )
    finally:
        for process in started:
            process.kill()
            process.wait()
    translated = out_dir / f"{name}.translated"
    subprocess.run(
        [
            sys.executable,
            str(UPROF),
            "translate",
            "--session",
            str(raw),
            "--out-dir",
            str(translated),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    return translated


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) < 2:
        print(__doc__.split("\n\n")[1], file=sys.stderr)
        return 2
    bin_dir, out_dir = Path(args[0]).resolve(), Path(args[1]).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    for name in args[2:] or CLASSES:
        print(record(bin_dir, out_dir, name))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
