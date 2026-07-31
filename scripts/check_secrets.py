"""Fail when source-controlled project files look like they contain secrets."""

from __future__ import annotations

import re
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
IGNORED_PARTS = {
    ".git",
    ".idea",
    ".pytest_cache",
    ".venv",
    ".vscode",
    "__pycache__",
    "build",
    "dist",
}
SCANNED_SUFFIXES = {
    ".cfg",
    ".html",
    ".ini",
    ".jinja",
    ".jinja2",
    ".js",
    ".json",
    ".md",
    ".py",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}
SENSITIVE_ASSIGNMENT = re.compile(
    r"""(?ix)
    \b(
        api[_-]?key
        |client[_-]?secret
        |access[_-]?token
        |refresh[_-]?token
        |session[_-]?secret
    )\b
    \s*(?:=|:)\s*
    ["']
    (?!<|\$\{|\{)
    [^"'\r\n]{12,}
    ["']
    """
)
HEX_API_KEY = re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{32}(?![0-9a-fA-F])")


def iter_project_files(root: Path = PROJECT_ROOT):
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in IGNORED_PARTS for part in path.relative_to(root).parts):
            continue
        if path.name == ".env.example" or path.suffix.lower() in SCANNED_SUFFIXES:
            yield path


def find_suspected_secrets(root: Path = PROJECT_ROOT) -> list[str]:
    findings: list[str] = []
    scanner_path = Path(__file__).resolve()

    for path in iter_project_files(root):
        text = path.read_text(encoding="utf-8", errors="replace")
        for line_number, line in enumerate(text.splitlines(), start=1):
            # The scanner necessarily contains the detection expressions.
            if path.resolve() == scanner_path:
                continue
            if SENSITIVE_ASSIGNMENT.search(line) or HEX_API_KEY.search(line):
                relative_path = path.relative_to(root)
                findings.append(f"{relative_path}:{line_number}")

    return findings


def main() -> int:
    findings = find_suspected_secrets()
    if findings:
        print("Possible credentials found:")
        for finding in findings:
            print(f"  {finding}")
        return 1

    print("Credential scan passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
