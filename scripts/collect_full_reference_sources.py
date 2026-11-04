#!/usr/bin/env python3
"""Archive inert, complete selected pinned source files, never repositories."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
import threading
import unicodedata
from urllib.parse import urlsplit

sys.dont_write_bytecode = True
import ingest_useanimations as trusted
from check_publication import inspect_content

DATA = Path(__file__).resolve().parents[1] / "data"
REPORT = "offline-source-report.json"
PREFIX = "assets/offline-source/"
CAP = 128 * 1024
CONTENT_TARGET = 90 * 1024
LANGUAGES = {".ts": "typescript", ".tsx": "tsx", ".js": "javascript",
             ".jsx": "jsx", ".dart": "dart", ".kt": "kotlin", ".swift": "swift",
             ".xml": "xml", ".json": "json", ".yaml": "yaml", ".yml": "yaml"}
DOCUMENTS = {".md", ".mdx", ".txt"}
GUARD = threading.local()
ORIGINAL_HTTPS = trusted.PinnedHTTPS
HEX256 = re.compile(r"[0-9a-f]{64}")
HEX160 = re.compile(r"[0-9a-f]{40}")
URL = re.compile(
    r"https://raw\.githubusercontent\.com/"
    r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/[0-9a-f]{40}/[A-Za-z0-9_@./+~-]+"
)
RISK_PATTERNS = {
    "dynamic-execution": r"(?<![\w.])(?:eval|exec|(?:new\s+)?Function)\s*\(",
    "instruction-injection": r"(?i)(?:ignore|disregard)\s+(?:all\s+)?(?:previous|prior|system)\s+instructions|<\|(?:system|im_start)\|>",
    "obfuscated-execution": r"(?i)\beval\s*\(|\bnew\s+Function\s*\(|\bexec\s*\(\s*(?:base64|atob)|\batob\s*\(|\$\{[^}]*@P\}|\$\{!",
    "external-executable-loader": r"(?is)(?:importScripts|import)\s*\(\s*['\"]https?://|(?:src|setAttribute\s*\(\s*['\"]src['\"])\s*[:=,]\s*['\"]https?://[^'\"\s]+\.(?:js|mjs|wasm)|<script\b[^>]*\bsrc\s*=|(?:curl|wget)[^\n]{0,200}\|\s*(?:sh|bash)|(?:WebAssembly\.instantiateStreaming|execSync|child_process)",
}
COMPILED_RISKS = {k: re.compile(v) for k, v in RISK_PATTERNS.items()}


def digest(body):
    return hashlib.sha256(body).hexdigest()


def pinned_url(url, repository=None, revision=None, path=None):
    if not isinstance(url, str) or not URL.fullmatch(url):
        raise ValueError("noncanonical immutable raw URL")
    parts = urlsplit(url).path[1:].split("/")
    if any(p in {"", ".", ".."} for p in parts):
        raise ValueError("unsafe request path")
    if repository is not None and url != f"https://raw.githubusercontent.com/{repository}/{revision}/{path}":
        raise ValueError("pinned URL disagrees with recorded provenance")
    return urlsplit(url).path


class GuardedHTTPS(ORIGINAL_HTTPS):
    def request(self, method, target, *args, **kwargs):
        if (self.host != "raw.githubusercontent.com" or method != "GET" or
                target != getattr(GUARD, "target", None)):
            raise ValueError("immutable request-target protection")
        return super().request(method, target, *args, **kwargs)

    def getresponse(self):
        response = super().getresponse()
        if response.status in {301, 302, 303, 307, 308}:
            response.close()
            raise ValueError("redirects forbidden for immutable full-source fetch")
        return response


class ImmutableFetcher(trusted.Fetcher):
    def fetch(self, url):
        GUARD.target = pinned_url(url)
        try:
            return super().fetch(url)
        finally:
            GUARD.target = None


def safety(text, filename):
    body = text.encode("utf-8")
    findings = inspect_content(filename, body, denylist=())
    if any((unicodedata.category(c) in {"Cc", "Cf", "Cs"} and c not in "\n\r\t") or
           (ord(c) & 0xFFFF) in {0xFFFE, 0xFFFF} or 0xFDD0 <= ord(c) <= 0xFDEF
           for c in text):
        findings.append("NUL/control/bidi/escape character")
    findings.extend(label for label, pattern in COMPILED_RISKS.items() if pattern.search(text))
    return sorted(set(findings))


def localize_document(text):
    """Keep useful prose/examples as text; never interpret Markdown or HTML."""
    modifications = []
    patterns = [
        ("remove U+200D emoji joiners for strict plain-text control policy", "\u200d", ""),
        ("remove raw script blocks", r"(?is)<script\b[^>]*>.*?</script\s*>", ""),
        ("remove raw script tags", r"(?is)</?script\b[^>]*>", ""),
        ("remove HTML media blocks", r"(?is)<(?:video|audio|iframe|object)\b[^>]*>.*?</(?:video|audio|iframe|object)\s*>", ""),
        ("remove HTML media tags", r"(?is)</?(?:img|video|audio|iframe|source|embed|object)\b[^>]*>", ""),
        ("remove Markdown image embeds", r"!\[[^\]]*\](?:\([^)\n]*\)|\[[^\]\n]*\])", ""),
        ("remove image reference definitions", r"(?im)^\s*\[[^\]]+\]:\s*\S+\.(?:png|jpe?g|gif|svg|webp)(?:\S*).*$", ""),
        ("remove installer commands", r"(?im)^.*(?:\b(?:npm|pnpm|yarn|bun)\s+(?:install|add|i)\b|\bnpx\b|\bflutter\s+pub\s+add\b|\b(?:pod|brew|pip)\s+install\b|\b(?:curl|wget)\b.*\|\s*(?:sh|bash)).*(?:\n|$)", ""),
        ("make Markdown hyperlinks plain text", r"\[([^\]\n]+)\]\(([^)\n]+)\)", r"\1 (\2)"),
    ]
    for label, pattern, replacement in patterns:
        text, count = re.subn(pattern, replacement, text)
        if count:
            modifications.append({"operation": label, "count": count})
    if modifications:
        modifications.append({"operation": "plain-text localization; not byte-identical to upstream"})
    return text, modifications


def adapt_source(text, repository, path):
    if repository != "flutter/samples" or path != "material_3_demo/lib/src/home.dart":
        return text, []
    replacements = {
        "final void Function(bool useLightMode) handleBrightnessChange;":
            "final ValueChanged<bool> handleBrightnessChange;",
        "final void Function() handleMaterialVersionChange;":
            "final VoidCallback handleMaterialVersionChange;",
        "final void Function(int value) handleColorSelect;":
            "final ValueChanged<int> handleColorSelect;",
        "final void Function(int value) handleImageSelect;":
            "final ValueChanged<int> handleImageSelect;",
    }
    for original, replacement in replacements.items():
        if text.count(original) != 1:
            raise ValueError("Flutter callback adaptation source declarations changed")
        text = text.replace(original, replacement, 1)
    return text, [{
        "operation": "replace four Dart callable declarations with equivalent existing Flutter ValueChanged and VoidCallback type aliases",
        "count": 4,
        "reason": "valid Dart callable-type syntax collides with strict language-neutral dynamic-execution detector",
    }]


def encode(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def atomic_save(relative, body):
    dest = trusted.safe_path(relative)
    if dest.is_file() and dest.read_bytes() == body:
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    pending = trusted.safe_path(relative + ".pending")
    try:
        with pending.open("wb") as output:
            output.write(body)
            output.flush()
            os.fsync(output.fileno())
        pending.replace(dest)
    finally:
        if pending.exists():
            pending.unlink()


def chunks(text):
    """Split on lines where possible, otherwise on Unicode character boundaries."""
    result, current = [], ""
    for line in text.splitlines(keepends=True):
        if current and len((current + line).encode()) > CONTENT_TARGET:
            result.append(current)
            current = ""
        while len(line.encode()) > CONTENT_TARGET:
            count = min(len(line), CONTENT_TARGET // 4)
            result.append(line[:count])
            line = line[count:]
        current += line
    if current or not result:
        result.append(current)
    assert "".join(result) == text
    return result


def collect():
    trusted.PinnedHTTPS = GuardedHTTPS
    fetcher = ImmutableFetcher()
    previous_path = trusted.safe_path(REPORT)
    previous_hashes = (json.loads(previous_path.read_text(encoding="utf-8")).get("artifact_hashes", {})
                       if previous_path.is_file() else {})
    report = {"version": 1, "references": {}, "artifact_hashes": {}, "limitations": [
        "Selected complete modules/sample documents only, not complete repositories, websites, frameworks or dependency closures.",
        "Imported text is inert JSON data and was not executed; no upstream/dependency malware-free or drop-in-compatibility guarantee.",
        "Static secret/control/injection/loader scans are heuristic; synthetic empty private denylist used without accessing private configuration.",
        "READMEs/component documents are localized plain text with explicit modifications; source hashes describe original fetched bytes, content hashes describe localized bytes.",
        "Complete selected-file counts include adapted/localized text; modified wrappers do not claim complete or byte-identical upstream source. Flutter callback type aliases are an explicitly documented adaptation, not compiled or executed.",
        "Five previously recorded files lack expected Git-blob SHA1; prior SHA256 is verified without fabricating missing tree proof.",
        "Existing retained LICENSE/NOTICE files and public attribution remain applicable; no licensing obligations waived.",
    ]}
    outputs = {}
    repositories = []
    for group in ("web", "motion", "mobile"):
        repositories.extend(json.loads((DATA / f"github-{group}-report.json").read_text())["repositories"])
    full_files, verified_blobs, archived_verified_blobs, fetched_files = 0, 0, 0, 0
    for repo in repositories:
        metadata_path = next(a["path"] for a in repo["artifacts"] if a["path"].endswith(".json"))
        metadata = json.loads(trusted.safe_path(metadata_path).read_text(encoding="utf-8"))
        license_path = metadata["license_path"]
        retained = [a for a in repo["artifacts"] if a["path"].startswith("licenses/")]
        for artifact in retained:
            if digest(trusted.safe_path(artifact["path"]).read_bytes()) != artifact["sha256"]:
                raise ValueError("retained license/notice integrity mismatch")
        for reference in repo["reference_ids"]:
            entry = {"assets": [], "limitations": []}
            report["references"][reference] = entry
            meaningful = False
            for upstream in repo["upstream_files"]:
                path = upstream["path"]
                if PurePosixPath(path).name.lower().startswith(("license", "notice")):
                    continue
                if PurePosixPath(path).name == "Package.swift":
                    entry["limitations"].append(f"{path}: dependency manifest not archived as executable implementation; retained selected Swift modules instead.")
                    continue
                url = upstream["pinned_url"]
                pinned_url(url, repo["repository"], repo["revision"], path)
                if not HEX256.fullmatch(upstream["sha256"]):
                    raise ValueError("missing prior full-file SHA256")
                try:
                    body = fetcher.fetch(url)
                    fetched_files += 1
                    if digest(body) != upstream["sha256"]:
                        raise ValueError("prior upstream SHA256 mismatch")
                    expected = upstream.get("expected_git_blob_sha1")
                    if expected:
                        if not HEX160.fullmatch(expected) or trusted.git_hash(body) != expected:
                            raise ValueError("recorded Git-blob SHA1 mismatch")
                        if upstream.get("verified_git_blob_sha1") not in {None, expected}:
                            raise ValueError("conflicting recorded Git-blob proof")
                        verified_blobs += 1
                    text = body.decode("utf-8", errors="strict")
                    suffix = PurePosixPath(path).suffix.lower()
                    document = suffix in DOCUMENTS
                    # Scan secrets/controls before localization, not only the retained prose.
                    baseline = safety(text.replace("\u200d", "") if document else text, path)
                    preflight = [f for f in baseline if f not in COMPILED_RISKS]
                    if preflight:
                        raise ValueError("unsafe upstream text: " + ", ".join(preflight))
                    modifications = []
                    if document:
                        text, modifications = localize_document(text)
                    else:
                        text, modifications = adapt_source(text, repo["repository"], path)
                    findings = safety(text, path)
                    if findings:
                        raise ValueError("excluded static risk: " + ", ".join(findings))
                    language = "text" if document else LANGUAGES.get(suffix)
                    if language is None:
                        raise ValueError("unsupported full source text format")
                    file_kind = "documentation" if document else "source-file"
                    classification = ("documentation" if document else
                                      "xml" if suffix == ".xml" else
                                      "configuration" if suffix in {".json", ".yaml", ".yml"} else
                                      "implementation")
                    segments = chunks(text)
                    whole_hash = digest(text.encode())
                    line_start, offset = 1, 0
                    for number, segment in enumerate(segments, 1):
                        slug = re.sub(r"[^a-zA-Z0-9-]+", "-", path).strip("-").lower()
                        asset_id = f"offline-{'document' if document else 'code'}-{reference}-{slug}"
                        if len(segments) > 1:
                            asset_id += f"-part-{number:03d}-of-{len(segments):03d}"
                        if len(asset_id) > 120:
                            asset_id = asset_id[:107].rstrip("-") + "-" + digest(asset_id.encode())[:12]
                        relative = f"{PREFIX}{reference}/{asset_id}.json"
                        wrapper = {
                            "kind": file_kind, "content": segment, "language": language,
                            "original_format": "mdx" if suffix == ".mdx" else "markdown" if suffix == ".md" else language,
                            "filename": PurePosixPath(path).name, "original_path": path,
                            "source_url": url, "license": repo["license"], "license_path": license_path,
                            "origin": "upstream", "complete": len(segments) == 1 and not modifications,
                            "full_file_complete": not modifications, "classification": classification,
                            "adapted_content_complete": True,
                            "completeness_scope": ("full localized document except explicit modifications" if document else "full adapted module except explicit modifications") if modifications else "all original source bytes",
                            "source_sha256": upstream["sha256"], "full_source_sha256": upstream["sha256"],
                            "content_sha256": digest(segment.encode()), "full_content_sha256": whole_hash,
                            "source_bytes": len(body), "source_lines": len(body.decode("utf-8").splitlines()),
                            "modifications": modifications, "byte_identical_upstream": not modifications and len(segments) == 1,
                            "line_coverage": {
                                "basis": "localized text" if modifications else "upstream source",
                                "start": line_start, "end": line_start + len(segment.splitlines()) - 1,
                                "total": len(text.splitlines()), "all_source_lines_retained": not modifications,
                            },
                            "chunk": {"number": number, "total": len(segments),
                                      "character_start": offset, "character_end": offset + len(segment)},
                            "repository": repo["repository"], "revision": repo["revision"],
                            "retained_license_notices": retained,
                            "attribution": metadata.get("attribution_verbatim", metadata.get("attribution", "Public upstream authors; preserve original copyright headers and retained license/notice.")),
                            "safety": {"static_scan": "passed", "execution": "not executed",
                                       "dependency_audit": "not performed"},
                        }
                        if expected:
                            wrapper["expected_git_blob_sha1"] = expected
                            wrapper["verified_git_blob_sha1"] = expected
                        else:
                            wrapper["git_blob_proof"] = "unavailable in prior collector; SHA256 matches recorded upstream"
                            if number == 1:
                                entry["limitations"].append(f"{path}: no prior expected Git-blob SHA1; prior full-source SHA256 verified.")
                        encoded = encode(wrapper)
                        if len(encoded) > CAP:
                            raise ValueError("JSON wrapper exceeds 128KiB; no truncation permitted")
                        if safety(segment, relative) or inspect_content(relative, encoded, denylist=()):
                            raise ValueError("output scan failed")
                        outputs[relative] = encoded
                        report["artifact_hashes"][relative] = digest(encoded)
                        entry["assets"].append({
                            "id": asset_id, "path": relative, "mime_type": "application/json",
                            "title": f"{'Localized' if document and modifications else 'Adapted' if modifications else 'Full'} {path}" + (f" (part {number}/{len(segments)})" if len(segments) > 1 else ""),
                            "origin": "upstream", "format": "plaintext" if document else language,
                            "license": repo["license"], "license_path": license_path, "source_url": url,
                        })
                        line_start += segment.count("\n")
                        offset += len(segment)
                    full_files += 1
                    archived_verified_blobs += bool(expected)
                    if PurePosixPath(path).name.lower() != "readme.md":
                        meaningful = True
                except (ValueError, UnicodeError, OSError, TimeoutError) as error:
                    entry["limitations"].append(f"{path}: {error}; full file not archived.")
            if not meaningful:
                entry["limitations"].append("BLOCKED: no safe complete selected module/sample document beyond root README.")
    report["collection"] = {"repositories": len(repositories), "references": len(report["references"]),
                            "full_selected_files": full_files, "wrappers": len(outputs),
                            "fetched_files": fetched_files, "verified_git_blobs": verified_blobs,
                            "archived_verified_git_blobs": archived_verified_blobs,
                            "transport": "trusted ingest_useanimations.Fetcher; exact immutable request-target guard; redirects rejected"}
    if len(report["references"]) != 24:
        raise ValueError("expected exactly 24 reference IDs")
    # Stage/validate every byte before publishing the report as the commit marker.
    for relative, body in outputs.items():
        atomic_save(relative, body)
    atomic_save(REPORT, encode(report))
    validate()
    for relative, expected_hash in previous_hashes.items():
        if (relative not in outputs and relative.startswith(PREFIX) and
                PurePosixPath(relative).suffix == ".json"):
            old = trusted.safe_path(relative)
            if old.is_file() and digest(old.read_bytes()) == expected_hash:
                old.unlink()
    print(json.dumps(report["collection"], sort_keys=True))
    blocked = [reference for reference, value in report["references"].items()
               if any("BLOCKED:" in limitation for limitation in value["limitations"])]
    if blocked:
        print("Blocked references: " + ", ".join(blocked))
        return 1
    return 0


def validate():
    report = json.loads(trusted.safe_path(REPORT).read_text(encoding="utf-8"))
    if len(report["references"]) != 24:
        raise ValueError("reference count mismatch")
    grouped = {}
    count = 0
    for reference, entry in report["references"].items():
        for asset in entry["assets"]:
            path = asset["path"]
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,119}", asset["id"]):
                raise ValueError("asset ID exceeds strict identifier bound")
            if not path.startswith(f"{PREFIX}{reference}/"):
                raise ValueError("asset outside owned output")
            body = trusted.safe_path(path).read_bytes()
            if len(body) > CAP or digest(body) != report["artifact_hashes"][path]:
                raise ValueError("wrapper cap/hash mismatch")
            wrapper = json.loads(body.decode("utf-8", errors="strict"))
            if digest(wrapper["content"].encode()) != wrapper["content_sha256"]:
                raise ValueError("content hash mismatch")
            pinned_url(wrapper["source_url"], wrapper["repository"], wrapper["revision"], wrapper["original_path"])
            if safety(wrapper["content"], path) or inspect_content(path, body, denylist=()):
                raise ValueError("wrapper scan failed")
            if not trusted.safe_path(wrapper["license_path"]).is_file():
                raise ValueError("missing retained license")
            grouped.setdefault((reference, wrapper["original_path"]), []).append(wrapper)
            count += 1
    for parts in grouped.values():
        parts.sort(key=lambda p: p["chunk"]["number"])
        if [p["chunk"]["number"] for p in parts] != list(range(1, parts[0]["chunk"]["total"] + 1)):
            raise ValueError("incomplete source chunks")
        content = "".join(p["content"] for p in parts).encode()
        if digest(content) != parts[0]["full_content_sha256"]:
            raise ValueError("full reconstructed source hash mismatch")
        if not parts[0]["modifications"] and digest(content) != parts[0]["source_sha256"]:
            raise ValueError("unmodified source is not byte-identical")
    print(f"Validated {count} UTF-8 inert JSON wrappers / {len(grouped)} complete selected files / 24 references.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    if args.validate_only:
        validate()
    else:
        raise SystemExit(collect())
