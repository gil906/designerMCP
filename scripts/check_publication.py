#!/usr/bin/env python3
"""Check publication content without printing matched sensitive values."""

from __future__ import annotations

import argparse
import ipaddress
import json
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath


SECRET_PATTERNS = {
    "private-key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    "github-token": re.compile(rb"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b"),
    "cloud-access-key": re.compile(rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "google-api-key": re.compile(rb"\bAIza[A-Za-z0-9_-]{30,}\b"),
    "slack-token": re.compile(rb"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    "credential-url": re.compile(rb"https?://[^/\s:@]+:[^/\s@]+@"),
    "credential-literal": re.compile(
        rb"""(?i)\b(?:password|passwd|api[_-]?key|access[_-]?token|client[_-]?secret)"""
        rb"""["']?\s*[:=]\s*["']([^"'\r\n]{16,})["']"""
    ),
    "private-home-path": re.compile(rb"/(?:root|home|Users)/(?:[A-Za-z0-9_.-]+/)?[A-Za-z0-9_.-]+"),
    "private-workspace-path": re.compile(rb"/mnt/(?:media|workspace)/[A-Za-z0-9_.-]+"),
    "windows-home-path": re.compile(rb"[A-Za-z]:\\Users\\[^\\\s]+\\"),
}
IPV4 = re.compile(rb"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")
PRIVATE_NETWORKS = tuple(
    ipaddress.ip_network(address)
    for address in ((0x0A000000, 8), (0xAC100000, 12), (0xC0A80000, 16))
)
FORBIDDEN_PARTS = {".ssh", ".copilot", ".venv", "__pycache__", ".pytest_cache"}
FORBIDDEN_NAMES = {
    ".mcp.json", ".publish-private.json", "tokens.json", "credentials.json",
    "id_rsa", "id_ed25519",
}
PLACEHOLDERS = re.compile(rb"(?i)example|placeholder|redacted|your[_ -]|test[_ -]|<[^>]+>")
MAX_FILE_BYTES = 8 * 1024 * 1024


def git(*args: str) -> bytes:
    result = subprocess.run(
        ["git", *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False
    )
    if result.returncode:
        raise RuntimeError("Git publication inspection failed.")
    return result.stdout


def inspect_content(path: str, content: bytes, denylist: tuple[bytes, ...] = ()) -> list[str]:
    parts = PurePosixPath(path).parts
    private_path = any(value.lower() in path.encode().lower() for value in denylist)
    if private_path:
        path = "[redacted-path]"
    name = parts[-1] if parts else ""
    findings: list[str] = [f"{path}: local-private-denylist"] if private_path else []
    if (
        any(part in FORBIDDEN_PARTS for part in parts)
        or name in FORBIDDEN_NAMES
        or (name.startswith(".env") and not name.endswith((".example", ".sample")))
        or name.endswith((".pem", ".key", ".p12", ".pfx"))
    ):
        findings.append(f"{path}: private-file")
    if len(content) > MAX_FILE_BYTES:
        return findings + [f"{path}: exceeds-publication-inspection-limit"]
    for label, pattern in SECRET_PATTERNS.items():
        for match in pattern.finditer(content):
            if label == "credential-literal" and PLACEHOLDERS.search(match.group(1)):
                continue
            line = content.count(b"\n", 0, match.start()) + 1
            findings.append(f"{path}:{line}: {label}")
    for match in IPV4.finditer(content):
        try:
            address = ipaddress.ip_address(match.group().decode("ascii"))
        except ValueError:
            continue
        if any(address in network for network in PRIVATE_NETWORKS):
            line = content.count(b"\n", 0, match.start()) + 1
            findings.append(f"{path}:{line}: private-network-address")
    for value in denylist:
        if value.lower() in content.lower() or value.lower() in path.encode().lower():
            findings.append(f"{path}: local-private-denylist")
    return sorted(set(findings))


def load_denylist() -> tuple[bytes, ...]:
    location = git("rev-parse", "--git-path", "info/publication-private.json").decode().strip()
    path = Path(location)
    if not path.exists():
        return ()
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 16 * 1024:
        raise RuntimeError("Local publication denylist is unsafe.")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list) or not all(isinstance(item, str) and len(item) >= 3 for item in data):
        raise RuntimeError("Local publication denylist must contain nonempty private strings.")
    return tuple(item.encode("utf-8") for item in data)


def tree_files(ref: str) -> list[tuple[str, bytes]]:
    files: list[tuple[str, bytes]] = []
    for record in git("ls-tree", "-r", "-z", ref).split(b"\0"):
        if not record:
            continue
        metadata, raw_path = record.split(b"\t", 1)
        mode, kind, oid = metadata.decode("ascii").split()
        path = raw_path.decode("utf-8")
        if kind != "blob" or mode == "120000":
            raise RuntimeError("Submodules and symlinks require manual publication review.")
        files.append((path, git("cat-file", "blob", oid)))
    return files


def staged_files() -> list[tuple[str, bytes]]:
    files: list[tuple[str, bytes]] = []
    for record in git("ls-files", "--stage", "-z").split(b"\0"):
        if not record:
            continue
        metadata, raw_path = record.split(b"\t", 1)
        mode, oid, stage = metadata.decode("ascii").split()
        if stage != "0" or mode in {"120000", "160000"}:
            raise RuntimeError("Conflicts, symlinks and submodules require manual review.")
        files.append((raw_path.decode("utf-8"), git("cat-file", "blob", oid)))
    return files


def outgoing_files(stream: str) -> list[tuple[str, bytes]]:
    files: list[tuple[str, bytes]] = []
    seen: set[tuple[str, bytes]] = set()
    for line in stream.splitlines():
        fields = line.split()
        if len(fields) != 4 or not all(re.fullmatch(r"[0-9a-f]{40,64}", fields[i]) for i in (1, 3)):
            raise RuntimeError("Invalid pre-push input.")
        local_oid = fields[1]
        if set(local_oid) == {"0"}:
            continue
        try:
            commit_oid = git("rev-parse", "--verify", f"{local_oid}^{{commit}}").decode("ascii").strip()
        except RuntimeError as error:
            raise RuntimeError("Outgoing refs must resolve to commits; noncommit tags are blocked.") from error
        # Inspect every reachable commit, including data removed by a later commit.
        for commit in git("rev-list", commit_oid).decode("ascii").splitlines():
            for path, content in tree_files(commit):
                item = (path, content)
                if item not in seen:
                    seen.add(item)
                    files.append(item)
    return files


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--outgoing", action="store_true", help="Inspect complete outgoing history.")
    parser.add_argument("--staged", action="store_true", help="Inspect all index blobs (default).")
    args = parser.parse_args()
    try:
        denylist = load_denylist()
        files = outgoing_files(sys.stdin.read()) if args.outgoing else staged_files()
        findings = [
            finding
            for path, content in files
            for finding in inspect_content(path, content, denylist)
        ]
        if findings:
            print("Publication blocked; sensitive values are deliberately omitted:", file=sys.stderr)
            for finding in sorted(set(findings)):
                print(finding, file=sys.stderr)
            return 1
        print(f"Publication check passed: {len(files)} file versions inspected.")
        return 0
    except (RuntimeError, OSError, ValueError, UnicodeError) as error:
        print(f"Publication blocked: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
