#!/usr/bin/env python3
"""Reject credentials, runtime state, and production-specific data from Git."""

from __future__ import annotations

import argparse
import ipaddress
import re
import subprocess
from pathlib import Path


FORBIDDEN_COMPONENTS = {
    "artifacts",
    "backups",
    "inventory",
    "logs",
    "private",
    "rendered",
    "secrets",
    "state",
}
FORBIDDEN_SUFFIXES = {
    ".key",
    ".log",
    ".p12",
    ".pem",
    ".pfx",
    ".sql",
    ".sqlite",
    ".sqlite3",
    ".wal",
}
FORBIDDEN_NAMES = {
    ".env",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "armagetronad-dedicated",
    "firebase-service-account.json",
    "id_ed25519",
    "id_rsa",
    "resend-api-key",
}
SECRET_PATTERNS = {
    "private key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "GitHub token": re.compile(rb"\b(?:ghp|gho|ghu|ghs|github_pat)_[A-Za-z0-9_]{20,}\b"),
    "Google API key": re.compile(rb"\bAIza[0-9A-Za-z_-]{30,}\b"),
    "Slack token": re.compile(rb"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    "service-account private key": re.compile(rb'"private_key"\s*:\s*"-----BEGIN'),
    "service-account document": re.compile(rb'"type"\s*:\s*"service_account"'),
    "non-example Firebase database": re.compile(
        rb"https://(?!example\.firebaseio\.com)[A-Za-z0-9._-]+\.firebaseio\.com"
    ),
}
IPV4 = re.compile(r"(?<![0-9])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![0-9])")
PRODUCTION_PATH = re.compile(r"/(?:home|Users)/[^/\s]+/|/" + r"root/")


def tracked_files(root: Path) -> list[Path]:
    try:
        output = subprocess.check_output(
            [
                "git",
                "-C",
                str(root),
                "ls-files",
                "--cached",
                "--others",
                "--exclude-standard",
                "-z",
            ],
        )
    except (OSError, subprocess.CalledProcessError):
        return [path for path in root.rglob("*") if path.is_file() and ".git" not in path.parts]
    return [
        root / item.decode("utf-8", "surrogateescape")
        for item in output.split(b"\0")
        if item
    ]


def allowed_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if address.is_loopback or address.is_unspecified:
        return True
    # One private documentation overlay plus RFC 5737 documentation networks.
    return any(
        address in network
        for network in (
            ipaddress.ip_network("10.77.0.0/24"),
            ipaddress.ip_network("192.0.2.0/24"),
            ipaddress.ip_network("198.51.100.0/24"),
            ipaddress.ip_network("203.0.113.0/24"),
        )
    )


def scan(root: Path) -> list[str]:
    failures: list[str] = []
    for path in tracked_files(root):
        relative = path.relative_to(root)
        if path.is_symlink():
            failures.append(f"symbolic links are not allowed: {relative}")
            continue
        lowered_parts = {part.casefold() for part in relative.parts}
        if lowered_parts & FORBIDDEN_COMPONENTS:
            failures.append(f"forbidden runtime directory: {relative}")
        if path.name.casefold() in FORBIDDEN_NAMES:
            failures.append(f"forbidden generated/credential file: {relative}")
        if path.suffix.casefold() in FORBIDDEN_SUFFIXES:
            failures.append(f"forbidden runtime/credential extension: {relative}")
        try:
            data = path.read_bytes()
        except OSError as exc:
            failures.append(f"unable to scan {relative}: {exc}")
            continue
        for label, pattern in SECRET_PATTERNS.items():
            if pattern.search(data):
                failures.append(f"probable {label}: {relative}")
        if b"\0" in data:
            continue
        text = data.decode("utf-8", "replace")
        if PRODUCTION_PATH.search(text):
            failures.append(f"operator home path: {relative}")
        for candidate in IPV4.findall(text):
            if not allowed_address(candidate):
                failures.append(f"public IPv4 literal {candidate}: {relative}")
    return sorted(set(failures))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, nargs="?", default=Path.cwd())
    return parser.parse_args()


def main() -> int:
    root = parse_args().root.resolve()
    failures = scan(root)
    if failures:
        print("public-tree validation failed:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print(f"public-tree validation passed ({len(tracked_files(root))} candidate files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
