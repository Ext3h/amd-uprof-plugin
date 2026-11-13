"""Build defect_lab.exe, lab_core.dll, lab_vendor.dll and lab_busy.exe from this folder's source.

Usage: python trials/defect_lab/build.py <out_dir>

The recipe is the timer fixture's: freestanding C for x86-64 Windows, no C runtime, kernel32
imported through a .def file, CodeView PDBs without host paths. It needs clang, lld-link and
llvm-dlltool from LLVM, found on PATH or in LLVM's default Windows install folder.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

SOURCE = Path(__file__).resolve().parent
TOOLS = ("clang", "lld-link", "llvm-dlltool", "llvm-objdump")
CFLAGS = [
    "--target=x86_64-pc-windows-msvc",
    "-O2",
    "-g",
    "-gcodeview",
    "-fdebug-compilation-dir=.",
    "-fcoverage-compilation-dir=.",
    "-nostdinc",
    "-ffreestanding",
    "-fno-builtin",
    "-fno-stack-protector",
    # uProf walks call stacks by frame pointer or by unwind information (.pdata); without both
    # the -cg captures held only the sampled frame.
    "-fno-omit-frame-pointer",
    "-fasynchronous-unwind-tables",
]
LFLAGS = ["/nodefaultlib", "/debug", "/pdbaltpath:%_PDB%", "/pdbsourcepath:.", "/release"]
SOURCES = ("lab_vendor", "lab_core", "lab_busy", "defect_lab")
# (image, linker kind, entry point, objects, import libraries)
IMAGES = (
    ("lab_vendor.dll", "/dll", "lab_vendor_entry", ["lab_vendor.obj"], []),
    (
        "lab_core.dll",
        "/dll",
        "lab_core_entry",
        ["lab_core.obj"],
        ["kernel32.lib", "lab_vendor.lib"],
    ),
    ("lab_busy.exe", "/subsystem:console", "lab_busy_entry", ["lab_busy.obj"], ["kernel32.lib"]),
    (
        "defect_lab.exe",
        "/subsystem:console",
        "defect_lab_entry",
        ["defect_lab.obj"],
        ["kernel32.lib", "lab_core.lib"],
    ),
)


def tool(name: str) -> Path | None:
    """The LLVM tool on PATH, else in LLVM's default Windows install folder."""
    found = shutil.which(name)
    if found:
        return Path(found)
    default = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "LLVM" / "bin"
    candidate = default / f"{name}.exe"
    return candidate if candidate.is_file() else None


def missing_tools() -> str | None:
    """A skip reason naming the tools that are missing, or None."""
    missing = [name for name in TOOLS if tool(name) is None]
    if missing:
        return f"{', '.join(missing)} not found; install LLVM"
    return None


def build(out_dir: Path) -> list[Path]:
    """Compile and link every image into out_dir; return the image paths."""
    out_dir.mkdir(parents=True, exist_ok=True)
    clang, lld_link, dlltool = tool("clang"), tool("lld-link"), tool("llvm-dlltool")
    # The sources are compiled under relative names inside out_dir, so that neither the source
    # folder nor out_dir ends up in a PDB or debug directory.
    for name in ("lab.h", "kernel32.def", *(f"{source}.c" for source in SOURCES)):
        shutil.copyfile(SOURCE / name, out_dir / name)
    for source in SOURCES:
        run([clang, *CFLAGS, "-c", f"{source}.c", "-o", f"{source}.obj"], out_dir)
    run([dlltool, "-m", "i386:x86-64", "-d", "kernel32.def", "-l", "kernel32.lib"], out_dir)
    images = []
    for image, kind, entry, objects, libraries in IMAGES:
        run(
            [lld_link, *LFLAGS, kind, f"/entry:{entry}", f"/out:{image}", *objects, *libraries],
            out_dir,
        )
        images.append(out_dir / image)
    return images


def run(command: list, cwd: Path) -> None:
    completed = subprocess.run(
        [str(part) for part in command], cwd=cwd, capture_output=True, text=True
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"{Path(str(command[0])).name} failed:\n{completed.stdout}{completed.stderr}"
        )


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print(__doc__.split("\n\n")[1], file=sys.stderr)
        return 2
    reason = missing_tools()
    if reason:
        print(f"error: {reason}", file=sys.stderr)
        return 1
    for image in build(Path(args[0])):
        print(image)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
