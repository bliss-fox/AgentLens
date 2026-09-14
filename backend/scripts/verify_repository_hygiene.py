from __future__ import annotations

import re
import subprocess
from pathlib import Path, PurePosixPath
from typing import NamedTuple

PATTERNS = {
    "absolute_path": re.compile(
        r"(?<![A-Za-z0-9])(?:[A-Za-z]:[\\/]|/(?:Users|home)/[^/\s]+/)",
        re.IGNORECASE,
    ),
    "private_key": re.compile(r"BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY"),
    "api_token": re.compile(
        r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16})\b"
    ),
}


class Finding(NamedTuple):
    category: str
    path: str
    line: int

    def render(self) -> str:
        return f"{self.category}:{self.path}:{self.line}"


def candidate_paths(root: Path) -> list[str]:
    result = subprocess.run(
        [
            "git",
            "-c",
            f"safe.directory={root.as_posix()}",
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
        ],
        cwd=root,
        check=True,
        capture_output=True,
    )
    return sorted(item for item in result.stdout.decode("utf-8").split("\0") if item)


def is_secret_env_file(relative_path: str) -> bool:
    name = PurePosixPath(relative_path).name
    return name == ".env" or (name.startswith(".env.") and name != ".env.example")


def scan_text(relative_path: str, text: str) -> list[Finding]:
    findings = []
    for line_number, line in enumerate(text.splitlines(), 1):
        for category, pattern in PATTERNS.items():
            if pattern.search(line):
                findings.append(Finding(category, relative_path, line_number))
    return findings


def scan_repository(root: Path) -> tuple[int, list[Finding]]:
    findings = []
    paths = candidate_paths(root)
    for relative_path in paths:
        if is_secret_env_file(relative_path):
            findings.append(Finding("secret_env_file", relative_path, 1))
            continue
        path = root / relative_path
        if path.is_symlink() or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        findings.extend(scan_text(relative_path, text))
    return len(paths), findings


def main() -> None:
    root = Path(__file__).resolve().parents[2]
    scanned, findings = scan_repository(root)
    if findings:
        for finding in findings:
            print(finding.render())
        raise SystemExit(f"repository hygiene failed with {len(findings)} finding(s)")
    print(f"repository hygiene passed: {scanned} version-control candidate file(s) scanned")


if __name__ == "__main__":
    main()
