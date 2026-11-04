#!/usr/bin/env python3
"""Bounded offline integrity and static-risk checks for the three fixed GitHub shards.

Reports use version 1 and the collector contract; upstream hashes are provenance
assertions, not independently re-fetched evidence. No code or URLs are executed.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import stat
import sys
import unicodedata
from pathlib import Path
from urllib.parse import quote, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from designer_mcp.library import (
    Catalog, GITHUB_SHARDS, Library, LibraryError, MANIFESTS, MAX_JSON_BYTES,
    MAX_LIBRARY_BYTES, github_source, path_parts, strict_json,
)
from check_publication import inspect_content

MAX_LICENSE_BYTES = 512 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_FILES = 4000
LIMITATIONS = (
    "Offline static checks cover only archived catalog/report text and listed artifacts. "
    "They do not establish repository publicity independently, license validity, "
    "or absence of malware in whole repositories, dependencies, binaries, or runtime behavior. "
    "Upstream full-byte hashes are checked for format and pinned provenance, not re-fetched; "
    "optional expected/verified Git blob SHA1 pairs are collector assertions unless full bytes "
    "are archived (no independent offline blob verification is claimed). "
    "Transformed archive hashes cover actual local bytes. Install snippets are inert documentation."
)
REPOSITORY_KEYS = {
    "repository", "revision", "license", "source_url", "reference_ids",
    "selection_reason", "status", "artifacts", "upstream_files", "scans",
}
EXEC_PATTERNS = {
    "dynamic-execution": re.compile(r"(?<![\w.])(?:eval|exec|(?:new\s+)?Function)\s*\("),
    "process-execution": re.compile(
        r"\b(?:os\.system|subprocess\.(?:run|Popen|call)|child_process|"
        r"Runtime\.getRuntime|ProcessBuilder)\b", re.I,
    ),
    "download-and-execute": re.compile(
        r"\b(?:curl|wget)\b[^\n]{0,500}\|\s*(?:ba|z|k)?sh\b", re.I,
    ),
    "obfuscated-execution": re.compile(
        r"(?:\$\{[^}\n]+@P\}|\$\{![^}\n]+\}|"
        r"(?:atob|fromCharCode|b64decode|base64\s+-d)[\s\S]{0,200}"
        r"(?:eval|exec|Function|(?:ba|z)?sh)\b)", re.I,
    ),
}
CODE_ESCAPES = re.compile(r"\\(?:x([0-9a-fA-F]{2})|u([0-9a-fA-F]{4})|u\{([0-9a-fA-F]{1,6})\})")
EXTRA_PRIVATE_KEYS = re.compile(
    r"-----BEGIN (?:ENCRYPTED PRIVATE KEY|PGP PRIVATE KEY BLOCK)-----|"
    r"PuTTY-User-Key-File-[23]:", re.I,
)


class CollectionError(ValueError):
    """Sanitized integrity failure, never containing copied source or secrets."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise CollectionError(message)


def text(value, maximum: int = 2000) -> bool:
    return isinstance(value, str) and 0 < len(value) <= maximum and bool(value.strip())


def digest(value) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def sequence(value, maximum: int, *, nonempty: bool = False) -> bool:
    return isinstance(value, list) and int(nonempty) <= len(value) <= maximum


def scan_text(raw: bytes) -> str:
    value = raw.decode("utf-8")
    require(
        not any(
            (unicodedata.category(c) in {"Cc", "Cf", "Cs"} and c not in "\n\r\t")
            or (ord(c) & 0xFFFF) in {0xFFFE, 0xFFFF}
            or 0xFDD0 <= ord(c) <= 0xFDEF for c in value
        ), "Unsafe control, invisible, surrogate, or noncharacter codepoint.",
    )
    findings = inspect_content("archived-reference.txt", raw)
    require(not findings, "Publication/credential signature in archived text: " +
            ", ".join(sorted({finding.rsplit(': ', 1)[-1] for finding in findings})))
    require(EXTRA_PRIVATE_KEYS.search(value) is None, "Private-key signature in archived text.")
    return value


def scan_code(value: str) -> None:
    def character(match):
        codepoint = int(next(group for group in match.groups() if group is not None), 16)
        require(codepoint <= 0x10FFFF, "Invalid escaped source codepoint.")
        return chr(codepoint)

    variants = [value]
    if CODE_ESCAPES.search(value):
        decoded = CODE_ESCAPES.sub(character, value)
        scan_text(decoded.encode("utf-8"))
        variants.append(decoded)
    for variant in variants:
        for label, pattern in EXEC_PATTERNS.items():
            require(pattern.search(variant) is None, f"Suspicious copied text: {label}.")


def scan_json(raw: bytes, *, execution: bool = True):
    scan_text(raw)
    value = strict_json(raw)
    pending = [value]
    nodes = 0
    while pending:
        item = pending.pop()
        nodes += 1
        require(nodes <= 100_000, "JSON exceeds static inspection node limit.")
        if isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
        elif isinstance(item, str):
            decoded = scan_text(item.encode("utf-8"))
            if execution:
                scan_code(decoded)
    return value


def artifact_parts(value: str, shard: str) -> tuple[tuple[str, ...], int]:
    require(isinstance(value, str), "Artifact path must be text.")
    area = value.split("/", 1)[0]
    require(area in {"assets", "licenses"}, "Artifact must be data-relative beneath its shard.")
    parts = path_parts(value, area)
    require(len(parts) >= 4 and parts[2] == f"github-{shard}", "Artifact crosses shard boundary.")
    if area == "assets":
        require(parts[-1].endswith(".json"), "Only inert JSON assets are allowed.")
        maximum = MAX_JSON_BYTES
    else:
        require(
            re.fullmatch(r"(?:LICENSE|NOTICE)(?:\.(?:txt|md))?", parts[-1]) is not None,
            "Only UTF-8 LICENSE/NOTICE text artifacts are allowed.",
        )
        maximum = MAX_LICENSE_BYTES
    return parts, maximum


def inventory(library: Library, shard: str, area: str) -> set[str]:
    """Bounded descriptor-relative inventory also rejects unlisted unsafe files."""
    found = set()
    visited = 0
    root_fd = os.open(library.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)

    def visit(fd: int, prefix: tuple[str, ...], depth: int) -> None:
        nonlocal visited
        visited += 1
        require(visited <= MAX_FILES, "Artifact directory count exceeds limit.")
        require(depth <= 8, "Artifact directory nesting exceeds limit.")
        names = os.listdir(fd)
        require(len(names) <= MAX_FILES, "Artifact directory exceeds file limit.")
        for name in names:
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                try:
                    visit(child, (*prefix, name), depth + 1)
                finally:
                    os.close(child)
            else:
                require(stat.S_ISREG(info.st_mode), "Symlink or nonregular artifact forbidden.")
                path = "/".join((*prefix, name))
                artifact_parts(path, shard)
                found.add(path)
                require(len(found) <= MAX_FILES, "Artifact inventory exceeds file limit.")

    fd = root_fd
    try:
        for component in ("data", area, f"github-{shard}"):
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        visit(fd, (area, f"github-{shard}"), 0)
    finally:
        os.close(fd)
    return found


def validate(root: Path, shards=GITHUB_SHARDS, min_repositories: int = 20) -> dict:
    require(type(min_repositories) is int and min_repositories >= 0, "Invalid repository minimum.")
    require(sequence(list(shards), 3, nonempty=True) and len(set(shards)) == len(shards)
            and set(shards) <= set(GITHUB_SHARDS), "Invalid shard selection.")
    library = Library(root)
    library.catalog()
    baseline_repos = set()
    for name in MANIFESTS:
        if name.startswith("github-"):
            continue
        raw = library.read(("data", name), MAX_LIBRARY_BYTES, optional=name != "library.json")
        if raw is not None:
            for entry in Catalog.model_validate(strict_json(raw)).entries:
                url = urlsplit(entry.source_url)
                pieces = url.path.strip("/").split("/")
                if url.hostname == "github.com" and len(pieces) >= 2:
                    baseline_repos.add("/".join(pieces[:2]).casefold())
    repos, reference_ids, artifacts = set(), set(), set()
    total_bytes = 0
    counts = {}
    upstream_count = blob_pair_count = 0
    for shard in shards:
        raw = library.read(("data", f"github-{shard}-library.json"), MAX_LIBRARY_BYTES)
        shard_catalog = Catalog.model_validate(scan_json(raw))
        raw = library.read(("data", f"github-{shard}-report.json"), MAX_LIBRARY_BYTES)
        report = scan_json(raw, execution=False)
        require(isinstance(report, dict) and set(report) == {"version", "repositories", "skipped", "limitations"},
                "Invalid report fields.")
        require(type(report["version"]) is int and report["version"] == 1, "Invalid report version.")
        require(sequence(report["repositories"], 1000, nonempty=True), "Invalid report repositories.")
        require(sequence(report["skipped"], 1000) and all(isinstance(s, dict) for s in report["skipped"]),
                "Invalid skipped records.")
        require(sequence(report["limitations"], 100, nonempty=True) and
                all(text(s, 4000) for s in report["limitations"]), "Invalid report limitations.")
        entries = {entry.id: entry for entry in shard_catalog.entries}
        shard_ids, shard_artifacts = set(), set()
        for repository in report["repositories"]:
            require(isinstance(repository, dict) and set(repository) == REPOSITORY_KEYS,
                    "Invalid repository report fields.")
            source = github_source(repository["source_url"]) if text(repository["source_url"]) else None
            require(source is not None and source == (repository["repository"], repository["revision"])
                    and repository["source_url"] == f"https://github.com/{source[0]}/tree/{source[1]}",
                    "Repository identity/revision must match exact pinned GitHub tree URL.")
            identity, revision = source
            require(identity.casefold() not in repos | baseline_repos, "Duplicate or pre-existing repository identity.")
            repos.add(identity.casefold())
            require(repository["status"] == "archived" and text(repository["license"], 1000)
                    and text(repository["selection_reason"], 4000), "Invalid archive/license/selection metadata.")
            require(isinstance(repository["scans"], (dict, list)) and bool(repository["scans"]),
                    "Missing collector scan evidence (not trusted as a scan result).")
            ids = repository["reference_ids"]
            require(sequence(ids, 1000, nonempty=True) and all(text(i, 120) for i in ids)
                    and len(set(ids)) == len(ids), "Invalid or duplicate repository reference IDs.")
            require(not set(ids) & reference_ids and set(ids) <= set(entries), "Duplicate or unknown report reference IDs.")
            reference_ids.update(ids)
            shard_ids.update(ids)
            required_paths = set()
            for reference_id in ids:
                entry = entries[reference_id]
                require(github_source(entry.source_url) == source and entry.license == repository["license"],
                        "Catalog/report provenance or license mismatch.")
                require(entry.license_path is not None and bool(entry.assets), "Archived reference requires license and assets.")
                license_parts = path_parts(entry.license_path, "licenses")
                required_paths.add("/".join(license_parts[1:]))
                for asset in entry.assets:
                    require(asset.mime_type == "application/json", "Shard asset MIME must be application/json.")
                    required_paths.add("/".join(path_parts(asset.path, "assets")[1:]))
            records = repository["artifacts"]
            require(sequence(records, 100, nonempty=True), "Invalid artifact list.")
            local_paths = set()
            for artifact in records:
                require(isinstance(artifact, dict) and set(artifact) == {"path", "sha256"}
                        and digest(artifact["sha256"]), "Invalid artifact hash record.")
                parts, maximum = artifact_parts(artifact["path"], shard)
                path = "/".join(parts[1:])
                require(path not in artifacts | local_paths, "Duplicate artifact path.")
                local_paths.add(path)
                require(len(artifacts | local_paths) <= MAX_FILES, "Total artifact count exceeds limit.")
                content = library.read(parts, maximum)
                total_bytes += len(content)
                require(total_bytes <= MAX_TOTAL_BYTES, "Total archive inspection byte limit exceeded.")
                require(hashlib.sha256(content).hexdigest() == artifact["sha256"], "Archived artifact hash mismatch.")
                if parts[1] == "assets":
                    require(isinstance(scan_json(content), (dict, list)), "Inert asset must be a JSON object/array.")
                else:
                    require(bool(scan_text(content).strip()), "Empty license/notice.")
            require(required_paths <= local_paths, "Catalog assets/license missing from artifact hashes.")
            artifacts |= local_paths
            shard_artifacts |= local_paths
            upstream = repository["upstream_files"]
            require(sequence(upstream, 100, nonempty=True), "Invalid upstream file provenance.")
            upstream_paths = set()
            upstream_urls = set()
            for item in upstream:
                base_keys = {"path", "sha256", "pinned_url"}
                blob_keys = {"expected_git_blob_sha1", "verified_git_blob_sha1"}
                require(isinstance(item, dict) and set(item) in (base_keys, base_keys | blob_keys)
                        and digest(item["sha256"]) and text(item["path"], 512), "Invalid upstream hash record.")
                upstream_count += 1
                if blob_keys <= set(item):
                    require(isinstance(item["expected_git_blob_sha1"], str) and
                            re.fullmatch(r"[0-9a-f]{40}", item["expected_git_blob_sha1"]) is not None
                            and item["expected_git_blob_sha1"] == item["verified_git_blob_sha1"],
                            "Invalid or mismatched collector Git blob SHA1 evidence.")
                    blob_pair_count += 1
                path = item["path"]
                require(not path.startswith("/") and "\\" not in path and
                        all(p not in {"", ".", ".."} for p in path.split("/")) and
                        not any(c.isspace() or ord(c) < 32 for c in path), "Unsafe upstream path.")
                require(path not in upstream_paths, "Duplicate upstream path.")
                upstream_paths.add(path)
                encoded = quote(path, safe="/@")
                allowed_urls = {
                    f"https://github.com/{identity}/blob/{revision}/{encoded}",
                    f"https://raw.githubusercontent.com/{identity}/{revision}/{encoded}",
                }
                require(item["pinned_url"] in allowed_urls, "Upstream URL is not pinned to reported identity/revision/path.")
                upstream_urls |= allowed_urls
            for reference_id in ids:
                entry_url = entries[reference_id].source_url.split("#", 1)[0]
                if f"/blob/{revision}/" in entry_url:
                    require(entry_url in upstream_urls, "Catalog source file missing from upstream provenance.")
        require(shard_ids == set(entries), "Catalog/report reference coverage mismatch.")
        found = inventory(library, shard, "assets") | inventory(library, shard, "licenses")
        require(found == shard_artifacts, "Unlisted or missing shard artifacts.")
        counts[shard] = {"repository_count": len(report["repositories"]), "entry_count": len(entries),
                         "artifact_count": len(shard_artifacts)}
    require(len(repos) >= min_repositories,
            f"Repository minimum not met: {len(repos)} distinct archived repositories; required {min_repositories}.")
    return {"repository_count": len(repos), "entry_count": len(reference_ids),
            "artifact_count": len(artifacts), "bytes_inspected": total_bytes, "shards": counts,
            "upstream_file_count": upstream_count, "collector_blob_pair_count": blob_pair_count}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shard", choices=GITHUB_SHARDS, help="Inspect one fixed shard (default: all three).")
    parser.add_argument("--min-repositories", type=int, default=20,
                        help="Minimum distinct archived repositories in selected shards (default: 20).")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1],
                        help="Portable project root, containing data/.")
    args = parser.parse_args(argv)
    print("Scan limitations: " + LIMITATIONS)
    try:
        result = validate(args.root, (args.shard,) if args.shard else GITHUB_SHARDS, args.min_repositories)
    except (LibraryError, CollectionError) as error:
        print(f"GitHub collection check FAILED: {error}", file=sys.stderr)
        return 1
    except (ValueError, UnicodeError, RecursionError, OSError):
        # Error content can include Pydantic's untrusted input; never echo it.
        print("GitHub collection check FAILED: missing, unsafe, inconsistent, suspicious, or insufficient archive. "
              "Use validate() in focused tests for sanitized integrity details.", file=sys.stderr)
        return 1
    print(f"GitHub collection check passed: {result['repository_count']} repositories, "
          f"{result['entry_count']} references, {result['artifact_count']} artifacts; "
          f"{result['bytes_inspected']} archived bytes inspected.")
    print(f"Upstream metadata: {result['collector_blob_pair_count']} of {result['upstream_file_count']} "
          "files carry consistent collector expected/verified Git blob SHA1 pairs; "
          "remaining files do not claim that check. None were re-fetched by this validator.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
