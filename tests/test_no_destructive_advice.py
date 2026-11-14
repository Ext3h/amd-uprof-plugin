"""Shipped text never tells the caller to delete or remove anything.

An error, warning, NEXT or remedy line names the finding (an artifact is possibly incompatible
or possibly defective) and a non-destructive next step: a fresh copy, another output directory,
another cache root. A caller who follows the text never loses a dump, a cache, a run or a path.

The check reads every string literal the tools can print, the SQL the tools run and the shipped
Markdown, and flags an imperative delete or remove: a verb at the start of a clause, or a shell
command that deletes.
"""

import ast
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "plugin" / "skills" / "amd-uprof"
TOOLS = SKILL / "tools"

# A delete/remove verb that opens a clause ("...; delete the dump", "or remove it") is an
# instruction; "could not remove", "is removed" or "--force removes" report what happened.
INSTRUCTION = re.compile(
    r"(?:^|[;:.,(]\s*|\b(?:and|or|then|to)\s+)"
    r"(?:delete|remove|erase|wipe|purge|Delete|Remove|Erase|Wipe|Purge)\b"
    r"|\b(?:rm\s+-|rmdir\b|Remove-Item\b|del\s+/)"
)


def _docstrings(tree: ast.AST) -> set[int]:
    """ids of the docstring nodes, which document code and are never printed."""
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                found.add(id(body[0].value))
    return found


def _python_texts(path: Path) -> list[tuple[int, str]]:
    """Line and text of every string literal in path; an f-string's fields become {}."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    skip = _docstrings(tree)
    texts = []
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            skip.update(id(part) for part in node.values)
            text = "".join(
                part.value if isinstance(part, ast.Constant) else "{}" for part in node.values
            )
            texts.append((node.lineno, text))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in skip:
                texts.append((node.lineno, node.value))
    return texts


def _sql_texts(path: Path) -> list[tuple[int, str]]:
    """Line and text of every single-quoted SQL string literal in path."""
    text = path.read_text(encoding="utf-8")
    return [
        (text.count("\n", 0, match.start()) + 1, match.group(1))
        for match in re.finditer(r"'((?:[^']|'')*)'", text)
    ]


def _markdown_texts(path: Path) -> list[tuple[int, str]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [(number, line) for number, line in enumerate(lines, 1)]


def destructive_instructions() -> list[str]:
    sources = (
        [(path, _python_texts) for path in sorted(TOOLS.glob("*.py"))]
        + [(path, _sql_texts) for path in sorted(TOOLS.glob("*.sql"))]
        + [(SKILL / "SKILL.md", _markdown_texts)]
        + [(path, _markdown_texts) for path in sorted((ROOT / "docs").glob("*.md"))]
    )
    found = []
    for path, texts in sources:
        for line, text in texts(path):
            if INSTRUCTION.search(text):
                found.append(f"{path.relative_to(ROOT)}:{line}: {text.strip()[:160]}")
    return found


class NoDestructiveAdviceTest(unittest.TestCase):
    def test_the_pattern_tells_an_instruction_from_a_report(self):
        for instruction in (
            "x; delete the dump and re-run",
            "replace the link, or remove it, and re-run",
            "remove the path or fix its permissions",
            "run rm -rf <dir>",
        ):
            self.assertRegex(instruction, INSTRUCTION)
        for report in (
            "WARNING: could not remove <path>",
            "cannot remove <path> for --force",
            "every .db is removed before",
            "unless --force removes them",
            "DELETE FROM cache_meta",
        ):
            self.assertNotRegex(report, INSTRUCTION)

    def test_no_shipped_text_instructs_a_delete_or_remove(self):
        self.assertEqual(destructive_instructions(), [])


if __name__ == "__main__":
    unittest.main()
