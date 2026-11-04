#!/usr/bin/env python3
"""Build bounded offline dossiers from local catalogs and two fixed local reports.

No downloads, rendering, source execution or repository traversal. The source
and preview collectors must finish before this command is run. --check validates
the combined inventory, not merely the generated files.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import stat
import sys
from pathlib import Path
from typing import Annotated, Literal

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pydantic import Field, ValidationError, field_validator
from PIL import Image

from designer_mcp.library import (
    ArchiveItem, Asset, BundleItem, Catalog, Entry, Identifier, Label, Library,
    LibraryError, MANIFESTS, MAX_ASSET_BYTES, MAX_JSON_BYTES, MAX_LIBRARY_BYTES,
    MAX_OUTPUT_BYTES, OfflineBundle, StrictModel, bounded, path_parts, strict_json,
)
from check_github_collection import CollectionError, require, scan_code, scan_json, scan_text
from check_publication import inspect_content

REPORTS = ("offline-preview-report.json", "offline-source-report.json")
GENERATED = "assets/offline-library"
CC0 = "licenses/offline/CC0.txt"
MAX_FILES = 16000
MAX_TOTAL_BYTES = 256 * 1024 * 1024
RASTERS = {"image/png", "image/jpeg", "image/gif", "image/webp"}
CODE_FORMATS = {
    "html", "css", "scss", "sass", "less", "javascript", "typescript", "jsx",
    "tsx", "vue", "svelte", "dart", "swift", "kotlin", "java", "python", "qml",
}
FORMATS = {
    ".md": "markdown", ".mdx": "markdown", ".txt": "text", ".yml": "yaml",
    ".yaml": "yaml", ".xml": "xml", ".ui": "qt-xml", ".svg": "svg",
    ".js": "javascript", ".mjs": "javascript", ".ts": "typescript",
    ".py": "python", ".kt": "kotlin", ".toml": "toml",
}
BASE_LIMITATIONS = [
    "Selected reference content only; not complete repositories, applications, dependencies or runnable environments.",
    "All source/code/HTML/SVG is inert reference text. No network access, code execution or SVG rendering is performed.",
    "Original concepts illustrate guidance; they are not copies or screenshots of unavailable upstream galleries.",
]
# Exact existing tooling payloads retained for audit, never served as code.
# This allowlist is byte-pinned, not an asset-supplied origin/risk exemption.
EXCLUDED_TOOLING = {
    "assets/unslop/skills/unslop/scripts/story.mjs.json": "e09c8878e0655d3e08b549b089982246bf7819f03c741aa4b04c98abdf11f80b",
    "assets/useanimations/source/scripts/pack.ts": "98ee15ba4af02241fdd6d7846ee7d59c9a5615591d3c72ff2c15dcc015c72a3e",
    "assets/useanimations/source/scripts/release.ts": "e11f0ff0a82e6c111768737a3948ae007bc19f524b88b2cfa5d0dab409d74893",
    "assets/useanimations/source/website/src/lib/git.ts": "322e9311980ec84286f663a8b49f07b712383d65feeec3ebc41d0cec20adff50",
}


def excluded_path(original: str) -> str:
    return "archives/offline-tooling/" + original.removeprefix("assets/")

Hash = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Count = Annotated[int, Field(ge=0, le=MAX_FILES)]
Finite = Annotated[float, Field(allow_inf_nan=False)]


class MediaValidation(StrictModel):
    bytes: Annotated[int, Field(ge=0, le=MAX_ASSET_BYTES)]
    frames: Annotated[int, Field(ge=1, le=48)]
    distinct_frames: Annotated[int, Field(ge=1, le=48)]
    dimensions: Annotated[list[Annotated[int, Field(ge=1, le=16000)]], Field(min_length=2, max_length=2)]
    decoded_frame_sha256: Annotated[list[Hash], Field(max_length=48)]
    durations_ms: Annotated[list[Annotated[int, Field(ge=0, le=1000000)]], Field(max_length=48)]


class MediaStats(StrictModel):
    references: Count
    artifacts: Count
    kinds: dict[Literal["original-concept", "rendered-animation", "unsupported-animation", "upstream-preview"], Count]
    animated_previews: Count | None = None


class RenderDependencies(StrictModel):
    optional_build_only: Annotated[list[Annotated[str, Field(min_length=1, max_length=500)]], Field(max_length=20)]
    runtime_dependencies_added: Literal[False]


class SourceCollection(StrictModel):
    repositories: Count
    references: Count
    full_selected_files: Count
    wrappers: Count
    fetched_files: Count
    verified_git_blobs: Count
    archived_verified_git_blobs: Count
    transport: Annotated[str, Field(min_length=1, max_length=1000)]


class ReportReference(StrictModel):
    assets: Annotated[list[Asset], Field(max_length=32)]
    kind: Label | None = None
    animation_source_asset_id: Identifier | None = None
    limitations: Annotated[list[Annotated[str, Field(min_length=1, max_length=2000)]], Field(max_length=64)]
    source_path: Annotated[str | None, Field(max_length=512)] = None
    upstream_source_path: Annotated[str | None, Field(max_length=512)] = None
    source_sha256: Hash | None = None
    sample_frames: Annotated[list[Annotated[int, Field(ge=0, le=1000000)]], Field(max_length=48)] | None = None
    source_sample_times_seconds: Annotated[list[Finite], Field(max_length=48)] | None = None
    frame_sha256: Annotated[list[Hash], Field(max_length=48)] | None = None
    still_source_frame: Annotated[int | None, Field(ge=0, le=1000000)] = None
    source_frame_count: Annotated[int | None, Field(ge=0, le=1000000)] = None
    source_frame_rate: Finite | None = None
    source_in_point: Finite | None = None
    source_out_point: Finite | None = None
    source_keyframed: bool | None = None
    source_expressions_ignored: bool | None = None
    render_method: Annotated[str | None, Field(max_length=1000)] = None
    validation: dict[Literal["webp", "gif"], MediaValidation] | None = None
    diagram_category: Literal["archive", "navigation", "typography", "forms", "motion", "tokens", "data", "structure"] | None = None

    _assets = field_validator("assets")(Entry.unique_assets.__func__)

    @field_validator("source_path", "upstream_source_path")
    @classmethod
    def source_path_safe(cls, value):
        if value is not None:
            path_parts(value, "assets")
        return value


class InputReport(StrictModel):
    version: Literal[1]
    references: dict[Identifier, ReportReference]
    artifact_hashes: dict[Annotated[str, Field(min_length=1, max_length=512)], Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]]
    limitations: Annotated[list[Annotated[str, Field(min_length=1, max_length=2000)]], Field(max_length=100)]
    stats: MediaStats | None = None
    render_dependencies: RenderDependencies | None = None
    collection: SourceCollection | None = None

    _version = field_validator("version", mode="before")(Catalog.exact_version.__func__)


def encoded(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def relative(value: str) -> str:
    area = value.removeprefix("data/").split("/", 1)[0]
    require(area in {"assets", "licenses", "archives"}, "Offline artifact must be beneath assets, licenses or archives.")
    return "/".join(path_parts(value, area)[1:])


def read_artifact(library: Library, value: str, maximum=MAX_LIBRARY_BYTES) -> bytes:
    rel = relative(value)
    return library.read(("data", *rel.split("/")), maximum)


def report_inputs(library: Library, ids: set[str]) -> list[InputReport]:
    reports = []
    for name in REPORTS:
        raw = library.read(("data", name), MAX_LIBRARY_BYTES)
        report = InputReport.model_validate(scan_json(raw, execution=False))
        require(len(report.references) <= 1000 and set(report.references) <= ids, "Report has unknown or too many references.")
        require(len(report.artifact_hashes) <= MAX_FILES, "Report artifact count exceeds limit.")
        normalized = {}
        total = 0
        for path, expected in report.artifact_hashes.items():
            rel = relative(path)
            require(path == rel and rel not in normalized, "Report paths must be unique and DATA-relative.")
            raw = read_artifact(library, rel)
            total += len(raw)
            require(total <= MAX_TOTAL_BYTES, "Report inspection exceeds aggregate byte limit.")
            require(digest(raw) == expected, "Collector artifact hash mismatch.")
            normalized[rel] = expected
        for reference in report.references.values():
            for asset in reference.assets:
                require(asset.path == relative(asset.path), "Report asset path must be DATA-relative.")
                require(asset.path in normalized, "Report asset missing from artifact hashes.")
                require(all(getattr(asset, key) is not None for key in ("origin", "format", "license", "license_path", "source_url")),
                        "Collector assets require explicit provenance and format.")
                # Collector maps cover their owned outputs. Pre-existing license
                # archives are checked here and hashed in the combined ledger.
                require(bool(scan_text(read_artifact(library, asset.license_path)).strip()),
                        "Collector asset lacks retained local licensing.")
            source_path = reference.source_path or reference.upstream_source_path
            if source_path is not None:
                require(reference.source_sha256 is not None and
                        digest(read_artifact(library, source_path)) == reference.source_sha256,
                        "Media source hash mismatch.")
            if reference.validation:
                for fmt, evidence in reference.validation.items():
                    matching = [a for a in reference.assets if a.mime_type == "image/" + fmt]
                    require(len(matching) == 1, "Renderer evidence requires matching catalog media.")
                    require(evidence.bytes == len(read_artifact(library, matching[0].path)), "Renderer byte evidence mismatch.")
                    require(evidence.distinct_frames <= evidence.frames and
                            len(evidence.decoded_frame_sha256) == evidence.frames,
                            "Renderer frame evidence is inconsistent.")
        reports.append(report)
    require(set(reports[0].references) == ids, "Preview report must cover every reference.")
    return reports


def inventory(library: Library) -> set[str]:
    """Descriptor-relative walk; symlinks and special files are always errors."""
    found = set()
    count = 0

    def walk(fd: int, parts: tuple[str, ...], depth: int):
        nonlocal count
        require(depth <= 32, "Offline inventory nesting exceeds limit.")
        names = os.listdir(fd)
        require(len(names) <= MAX_FILES, "Offline directory exceeds limit.")
        for name in names:
            count += 1
            require(count <= MAX_FILES, "Offline inventory exceeds file/directory limit.")
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            require(not stat.S_ISLNK(info.st_mode), "Offline inventory contains a symlink.")
            if stat.S_ISDIR(info.st_mode):
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                try:
                    walk(child, (*parts, name), depth + 1)
                finally:
                    os.close(child)
            else:
                require(stat.S_ISREG(info.st_mode), "Offline inventory contains a special file.")
                rel = "/".join((*parts, name))
                relative(rel)
                found.add(rel)

    rootfd = os.open(library.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        datafd = os.open("data", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=rootfd)
        try:
            for area in ("assets", "licenses", "archives"):
                try:
                    fd = os.open(area, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=datafd)
                except FileNotFoundError:
                    require(area == "archives", "Missing asset/license inventory directory.")
                    continue
                try:
                    walk(fd, (area,), 0)
                finally:
                    os.close(fd)
        finally:
            os.close(datafd)
    finally:
        os.close(rootfd)
    return found


def scan_artifact(path: str, raw: bytes):
    require(not inspect_content(path, raw), "Publication/credential signature in offline artifact.")
    if path.startswith("archives/"):
        original = next((p for p in EXCLUDED_TOOLING if excluded_path(p) == path), None)
        require(original is not None and digest(raw) == EXCLUDED_TOOLING[original],
                "Unknown or changed unserved tooling archive.")
        # Still run the same detectors and require the recorded finding. No
        # caller-controlled labels can suppress a finding or add an archive.
        try:
            if original.endswith(".json"):
                scan_json(raw, execution=False)
                scan_json(raw)
            else:
                scan_code(scan_text(raw))
        except CollectionError as exc:
            require(str(exc) == "Suspicious copied text: process-execution.",
                    "Unserved tooling archive has an unexpected static-risk finding.")
            return
        raise CollectionError("Unserved tooling archive no longer matches its recorded finding.")
    if path.endswith(".json"):
        scan_json(raw)
    elif Path(path).suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
        expected = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".gif": "GIF", ".webp": "WEBP"}[Path(path).suffix.lower()]
        try:
            with Image.open(io.BytesIO(raw)) as image:
                require(image.format == expected and image.width * image.height <= 16_000_000,
                        "Invalid or oversized offline raster archive.")
                image.verify()
        except (OSError, ValueError, Image.DecompressionBombError):
            raise CollectionError("Invalid offline raster archive.") from None
    else:
        scan_code(scan_text(raw))


def format_of(asset: Asset, value=None) -> str:
    if asset.format:
        return asset.format
    if asset.mime_type in RASTERS:
        return asset.mime_type.split("/")[1]
    if isinstance(value, dict):
        if "v" in value and isinstance(value.get("layers"), list):
            return "lottie"
        if isinstance(value.get("language"), str):
            return value["language"][:100]
        original = value.get("original_path") or value.get("path")
        if isinstance(original, str):
            suffix = Path(original).suffix.lower()
            return FORMATS.get(suffix, suffix.lstrip(".") or "text")
    suffix = Path(asset.path).suffix.lower()
    return FORMATS.get(suffix, suffix.lstrip(".") or "text")


def role_of(asset: Asset, value, fmt: str) -> str:
    if asset.mime_type in RASTERS:
        return "visual"
    if fmt == "lottie":
        require(isinstance(value, dict) and isinstance(value.get("v"), str) and isinstance(value.get("layers"), list),
                "Lottie format marker requires actual animation JSON.")
        return "animation-source"
    if fmt == "license":
        return "license"
    if isinstance(value, dict):
        if value.get("kind") in {"offline-examples", "integration-example"}:
            return "example"
        if value.get("kind") == "offline-guidance":
            return "document"
        if isinstance(value.get("content"), str) and value.get("classification") in {"documentation", "implementation"}:
            return "code" if value["classification"] == "implementation" else "document"
        if value.get("kind") == "offline-text" or "text" in value or "code" in value:
            return "code" if fmt in CODE_FORMATS else "document"
    return "reference"


def animated_frames(raw: bytes) -> int:
    """Bounded raster decoding only; source expressions are never executed."""
    with Image.open(io.BytesIO(raw)) as image:
        count = getattr(image, "n_frames", 1)
        require(count <= 48 and image.width * image.height <= 16_000_000,
                "Animation preview exceeds frame/pixel inspection limits.")
        hashes = set()
        for frame in range(count):
            image.seek(frame)
            hashes.add(digest(image.convert("RGBA").tobytes()))
        return len(hashes)


def unlink_original(root: Path, path: str, expected: str):
        library = Library(root)
        raw = library.read(("data", *path.split("/")), MAX_LIBRARY_BYTES, optional=True)
        if raw is None:
            return
        require(digest(raw) == expected, "Excluded original changed before archive handoff.")
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            parts = ("data", *path.split("/"))
            for part in parts[:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
            os.unlink(parts[-1], dir_fd=fd)
        finally:
            os.close(fd)


def atomic_write(root: Path, rel: str, raw: bytes):
    """Replace a single file atomically without traversing symlink parents."""
    require(rel.startswith("data/") and all(p not in {"", ".", ".."} for p in rel.split("/")), "Unsafe output path.")
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    stage = None
    try:
        parts = rel.split("/")
        for part in parts[:-1]:
            try:
                os.mkdir(part, dir_fd=fd)
            except FileExistsError:
                pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        stage = f".{parts[-1]}.offline-stage-{os.getpid()}"
        out = os.open(stage, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=fd)
        with os.fdopen(out, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(stage, parts[-1], src_dir_fd=fd, dst_dir_fd=fd)
        stage = None
        os.fsync(fd)
    finally:
        if stage is not None:
            os.unlink(stage, dir_fd=fd)
        os.close(fd)


def build(root: Path) -> dict:
    library = Library(root)
    entries = library.catalog().entries
    reports = report_inputs(library, {e.id for e in entries})
    require(read_artifact(library, CC0).strip(), "Original content requires retained CC0 license.")
    writes: dict[str, bytes] = {}
    excluded_originals = set()
    new_entries = {}

    def generated(entry: Entry, asset_id: str, value, fmt: str, title: str, provenance=None) -> Asset:
        path = f"{GENERATED}/{entry.id}/{asset_id}.json"
        raw = encoded(value)
        require(len(raw) <= MAX_JSON_BYTES, "Generated per-file JSON exceeds limit; no silent truncation.")
        writes["data/" + path] = raw
        return Asset(id=asset_id, path=path, mime_type="application/json", title=title,
                     format=fmt, **(provenance or {
                         "origin": "original", "source_url": f"original://designerMCP/offline/{entry.id}",
                         "license": "CC0-1.0", "license_path": CC0,
                     }))

    for entry in entries:
        assets = {}
        archives: list[ArchiveItem] = []
        limitations = list(BASE_LIMITATIONS)
        for asset in entry.assets:
            if relative(asset.path).startswith(GENERATED + "/"):
                if asset.id == "offline-bundle" or asset.id in {"offline-license", "offline-guidance", "offline-examples"}:
                    continue
                value = strict_json(read_artifact(library, asset.path, MAX_JSON_BYTES))
                require(isinstance(value, dict) and value.get("kind") in {"offline-text", "offline-excluded-source"} and "original_asset" in value,
                        "Existing generated wrapper is invalid.")
                asset = Asset.model_validate(value["original_asset"])
                require(not relative(asset.path).startswith(GENERATED + "/"), "Recursive generated wrapper.")
            assets[asset.id] = asset
        previous_bundle = next((a for a in entry.assets if a.id == "offline-bundle"), None)
        if previous_bundle:
            previous = OfflineBundle.model_validate(strict_json(read_artifact(library, previous_bundle.path, MAX_OUTPUT_BYTES)))
            require(previous.reference_id == entry.id, "Previous archive index reference mismatch.")
            for archive in previous.archives:
                if archive.original_asset and archive.original_asset.id not in assets:
                    assets[archive.original_asset.id] = archive.original_asset
        for report in reports:
            reference = report.references.get(entry.id)
            if reference:
                limitations.extend(reference.limitations)
                for asset in reference.assets:
                    existing = assets.get(asset.id)
                    require(existing is None or relative(existing.path) == relative(asset.path), "Collector asset ID collision.")
                    assets[asset.id] = asset
        normalized = []
        values = {}
        for asset in assets.values():
            original_path = relative(asset.path)
            if original_path in EXCLUDED_TOOLING:
                archive_path = excluded_path(original_path)
                raw = library.read(("data", *original_path.split("/")), MAX_LIBRARY_BYTES, optional=True)
                if raw is None:
                    raw = read_artifact(library, archive_path)
                require(digest(raw) == EXCLUDED_TOOLING[original_path], "Excluded tooling original hash mismatch.")
                scan_artifact(archive_path, raw)
                writes["data/" + archive_path] = raw
                excluded_originals.add(original_path)
                value = strict_json(raw) if asset.mime_type == "application/json" else None
                fmt = format_of(asset, value)
                archives.append(ArchiveItem(path=archive_path, format=fmt, sha256=digest(raw),
                                            reason="process-execution detected in executable tooling; byte-pinned unserved archive, not complete served source.",
                                            original_asset=asset))
                limitations.append("Executable process tooling is excluded from serving; original bytes remain in a hash-pinned unserved archive with process-execution finding.")
                provenance = library.asset_provenance(entry, asset)
                value = {"version": 1, "kind": "offline-excluded-source", "filename": Path(original_path).name,
                         "format": fmt, "archive_path": archive_path, "sha256": digest(raw),
                         "original_asset": asset.model_dump(exclude_none=True),
                         "complete": False, "content_omitted": True, "static_risk": "process-execution",
                         "purpose": "Original tooling is not returned or executed. This is an omission index, not source implementation."}
                asset = generated(entry, asset.id, value, fmt, "Tooling omission index (not complete source)",
                                  {k: provenance[k] for k in ("origin", "source_url", "license", "license_path")})
                normalized.append(asset)
                values[asset.id] = value
                continue
            raw = read_artifact(library, asset.path)
            scan_artifact(asset.path, raw)
            value = strict_json(raw) if asset.mime_type == "application/json" else None
            fmt = format_of(asset, value)
            supported = asset.mime_type in RASTERS or asset.mime_type == "application/json"
            maximum = MAX_JSON_BYTES if asset.mime_type == "application/json" else MAX_ASSET_BYTES
            if supported and len(raw) > maximum:
                archives.append(ArchiveItem(path=relative(asset.path), format=fmt, sha256=digest(raw),
                                            reason="Original exceeds runtime size limit; retained archive-only. Use bounded preview.",
                                            original_asset=asset))
                limitations.append("An oversized original is archive-only, not retrievable through get_asset.")
                continue
            if not supported:
                text = scan_text(raw)
                provenance = library.asset_provenance(entry, asset)
                wrapper = {
                    "version": 1, "kind": "offline-text", "filename": Path(asset.path).name,
                    "format": fmt, "text": text, "archive_path": relative(asset.path),
                    "sha256": digest(raw), "original_asset": asset.model_dump(exclude_none=True),
                    "provenance": {k: provenance[k] for k in ("origin", "source_url", "license", "license_path")},
                    "notice": "Full original text retained verbatim; inert reference, not executable or rendered.",
                }
                if len(encoded(wrapper)) > MAX_JSON_BYTES:
                    archives.append(ArchiveItem(path=relative(asset.path), format=fmt, sha256=digest(raw),
                                                reason="Full text wrapper exceeds runtime JSON bound; original retained archive-only.",
                                                original_asset=asset))
                    limitations.append("Oversized source text is archive-only; no truncated file is substituted.")
                    continue
                asset = generated(entry, asset.id, wrapper, fmt, asset.title,
                                  {k: provenance[k] for k in ("origin", "source_url", "license", "license_path")})
                value = wrapper
            elif asset.format is None:
                asset = asset.model_copy(update={"format": fmt})
            normalized.append(asset)
            values[asset.id] = value
        roles = [role_of(a, values[a.id], a.format) for a in normalized]
        if not any(role in {"animation-source", "document", "code", "example"} for role in roles):
            if entry.examples:
                original = entry.source_url.startswith("original://") or all(
                    example.description.lower().startswith("original") for example in entry.examples
                )
                provenance = None if original else {
                    "origin": "upstream", "source_url": entry.source_url,
                    "license": entry.license, "license_path": entry.license_path,
                }
                value = {"version": 1, "kind": "offline-examples",
                         "examples": [e.model_dump() for e in entry.examples],
                         "purpose": "Existing illustrative fragments, not complete upstream implementation."}
                asset = generated(entry, "offline-examples", value, "examples", "Retained inert illustrative examples", provenance)
            else:
                value = {"version": 1, "kind": "offline-guidance", "text": entry.guidance,
                         "purpose": "Original adaptation guide; no upstream implementation or runnable example is claimed."}
                asset = generated(entry, "offline-guidance", value, "text", "Original offline adaptation guide")
                limitations.append("Upstream source/documents are unavailable; the local document is original adaptation guidance.")
            normalized.append(asset)
            values[asset.id] = value
        license_paths = {library.asset_provenance(entry, a)["license_path"] for a in normalized}
        if entry.license_path:
            license_paths.add(entry.license_path)
        license_values = []
        for license_path in sorted(p for p in license_paths if p):
            raw = read_artifact(library, license_path)
            text = scan_text(raw)
            license_values.append({"path": relative(license_path), "sha256": digest(raw), "text": text})
        require(bool(license_values), "Reference lacks retained local licensing.")
        value = {"version": 1, "kind": "offline-license", "licenses": license_values,
                 "notice": "Verbatim retained licenses; rights apply individually, not blanket permission."}
        license_available = len(encoded(value)) <= MAX_JSON_BYTES
        if license_available:
            asset = generated(entry, "offline-license", value, "license", "Full retained local license texts")
            normalized.append(asset)
            values[asset.id] = value
        else:
            for item in license_values:
                archives.append(ArchiveItem(path=item["path"], format="license", sha256=item["sha256"],
                                            reason="Full license wrapper exceeds JSON limit; license retained archive-only."))
        require(len(normalized) <= 31, "Reference exceeds 32-asset cap including bundle.")
        items = []
        for asset in normalized:
            raw = writes.get("data/" + relative(asset.path))
            if raw is None:
                raw = read_artifact(library, asset.path)
            provenance = library.asset_provenance(entry, asset)
            items.append(BundleItem(
                asset_id=asset.id, role=role_of(asset, values[asset.id], asset.format),
                format=asset.format, mime_type=asset.mime_type, sha256=digest(raw),
                **{k: provenance[k] for k in ("origin", "source_url", "license", "license_path")},
            ))
        visuals = [i for i in items if i.role == "visual"]
        animation = [i for i in items if i.role == "animation-source"]
        require(bool(visuals), "Every reference requires a local raster visual.")
        gifs = [a for a in normalized if a.mime_type == "image/gif"]
        has_animated_preview = any(animated_frames(read_artifact(library, a.path)) > 1 for a in gifs)
        unsupported_animation = reports[0].references[entry.id].kind == "unsupported-animation"
        if animation and not has_animated_preview:
            require(unsupported_animation and bool(reports[0].references[entry.id].limitations),
                    "True animated reference requires a local animated GIF or explicit unsupported-animation limitation.")
        report_kind = reports[0].references[entry.id].kind
        declared_animation = reports[0].references[entry.id].animation_source_asset_id
        if declared_animation:
            require(declared_animation in {i.asset_id for i in animation}, "Reported animation source is not actual Lottie.")
        has_source = any(i.role in {"code", "document"} and i.origin == "upstream" for i in items)
        has_original_guide = any(i.asset_id == "offline-guidance" for i in items)
        has_code = any(i.role == "code" for i in items)
        has_examples = bool(entry.examples) or any(i.role == "example" for i in items)
        if not has_source and not animation:
            limitations.append("No complete upstream implementation is claimed; any examples are illustrative only.")
        coverage = {
            "visual": "original-concept" if all(i.origin == "original" for i in visuals) else "available",
            "animation": "available" if animation else "not-applicable",
            "animated_preview": "available" if animation and has_animated_preview else "unavailable" if animation else "not-applicable",
            "source": "selected-files" if has_source else "available" if animation or has_code else "original-guidance" if has_original_guide else "examples-only",
            "implementation": "selected-files" if has_code else "examples-only" if has_examples else "unavailable",
            "license": "available" if license_available else "archive-only",
        }
        bundle = OfflineBundle(version=1, reference_id=entry.id,
                               scope="selected-reference-content-not-complete-repository",
                               kind="unsupported-animation" if animation and unsupported_animation else "animated-icon" if animation else report_kind or "design-reference",
                               assets=items, archives=archives, coverage=coverage,
                               limitations=list(dict.fromkeys(limitations)))
        # Leave room for trusted envelope, full entry guidance and provenance.
        bounded({"bundle": bundle.model_dump(), "guidance": entry.guidance,
                 "provenance": library.provenance(entry), "notice": " " * 300})
        asset = generated(entry, "offline-bundle", bundle.model_dump(), "offline-bundle", "Offline reference dossier index")
        normalized.append(asset)
        new_entries[entry.id] = Entry.model_validate({**entry.model_dump(), "assets": [a.model_dump(exclude_none=True) for a in normalized]})
    for name in MANIFESTS:
        raw = library.read(("data", name), MAX_LIBRARY_BYTES, optional=name != "library.json")
        if raw is not None:
            original = Catalog.model_validate(strict_json(raw))
            catalog = Catalog(version=1, entries=[new_entries[e.id] for e in original.entries])
            raw = encoded(catalog.model_dump(exclude_none=True))
            require(len(raw) <= MAX_LIBRARY_BYTES, "Updated manifest exceeds 4MiB limit.")
            scan_json(raw)
            writes["data/" + name] = raw
    hashes = {}
    total = 0
    planned_paths = (inventory(library) - excluded_originals) | {
        p.removeprefix("data/") for p in writes if p.startswith(("data/assets/", "data/archives/"))
    }
    for path in sorted(planned_paths):
        raw = writes.get("data/" + path)
        if raw is None:
            raw = read_artifact(library, path)
        total += len(raw)
        require(total <= MAX_TOTAL_BYTES, "Combined inventory exceeds aggregate byte limit.")
        scan_artifact(path, raw)
        hashes[path] = digest(raw)
    output = {"version": 1, "reference_ids": sorted(new_entries),
              "artifact_hashes": hashes, "manifest_hashes": {
                  name: digest(writes["data/" + name]) for name in MANIFESTS if "data/" + name in writes
              }, "input_hashes": {name: digest(library.read(("data", name), MAX_LIBRARY_BYTES)) for name in REPORTS},
              "limitations": BASE_LIMITATIONS,
              "unserved_tooling_findings": {
                  excluded_path(path): {"sha256": EXCLUDED_TOOLING[path], "finding": "process-execution", "served": False}
                  for path in sorted(excluded_originals)
              }}
    raw = encoded(output)
    require(len(raw) <= MAX_LIBRARY_BYTES, "Offline integrity report exceeds limit.")
    # Validate all inputs and stage all bytes before replacing any catalog.
    for rel, raw in sorted(writes.items(), key=lambda item: (item[0].split("/")[-1] in MANIFESTS, item[0])):
        atomic_write(root, rel, raw)
    for path in excluded_originals:
        unlink_original(root, path, EXCLUDED_TOOLING[path])
    atomic_write(root, "data/offline-library-report.json", encoded(output))
    return validate(root, min_references=0, min_animations=0, min_source_references=0)


def validate(root: Path, *, min_references=426, min_animations=300, min_source_references=24, min_animation_sources=None) -> dict:
    if min_animation_sources is None:
        min_animation_sources = min_animations
    require(all(type(n) is int and n >= 0 for n in (min_references, min_animations, min_source_references, min_animation_sources)),
            "Invalid offline aggregate minima.")
    library = Library(root)
    entries = library.catalog().entries
    report_inputs(library, {e.id for e in entries})
    report = scan_json(library.read(("data", "offline-library-report.json"), MAX_LIBRARY_BYTES), execution=False)
    require(isinstance(report, dict) and set(report) == {
        "version", "reference_ids", "artifact_hashes", "manifest_hashes", "input_hashes", "limitations", "unserved_tooling_findings",
    } and type(report["version"]) is int and report["version"] == 1, "Invalid combined integrity report.")
    require(report["reference_ids"] == sorted(e.id for e in entries), "Combined report reference mismatch.")
    require(isinstance(report["artifact_hashes"], dict), "Invalid combined inventory hash map.")
    found = inventory(library)
    require(set(report["artifact_hashes"]) == found, "Unlisted or missing combined offline artifact.")
    require(report["unserved_tooling_findings"] == {
        path: {"sha256": EXCLUDED_TOOLING[original], "finding": "process-execution", "served": False}
        for path in found if path.startswith("archives/")
        for original in EXCLUDED_TOOLING if excluded_path(original) == path
    }, "Unserved tooling findings differ from fixed byte-pinned archive policy.")
    total = 0
    for path, expected in report["artifact_hashes"].items():
        require(isinstance(expected, str) and len(expected) == 64, "Invalid artifact digest.")
        raw = read_artifact(library, path)
        total += len(raw)
        require(total <= MAX_TOTAL_BYTES, "Combined inventory inspection exceeds aggregate bound.")
        require(digest(raw) == expected, "Combined artifact hash mismatch.")
        scan_artifact(path, raw)
    require(isinstance(report["manifest_hashes"], dict) and set(report["manifest_hashes"]) == {
        name for name in MANIFESTS if library.read(("data", name), MAX_LIBRARY_BYTES, optional=True) is not None
    }, "Combined manifest inventory mismatch.")
    for name, expected in report["manifest_hashes"].items():
        require(digest(library.read(("data", name), MAX_LIBRARY_BYTES)) == expected, "Combined manifest hash mismatch.")
    require(isinstance(report["input_hashes"], dict) and set(report["input_hashes"]) == set(REPORTS), "Combined input report inventory mismatch.")
    for name, expected in report["input_hashes"].items():
        require(digest(library.read(("data", name), MAX_LIBRARY_BYTES)) == expected, "Combined collector report hash mismatch.")
    animations = animation_sources = source_references = unsupported_previews = 0
    for entry in entries:
        dossier = library.bundle(entry.id)["bundle"]
        require(bool(entry.guidance.strip()), "Reference requires guidance.")
        for item in dossier["assets"]:
            library.asset(entry.id, item["asset_id"])
            asset = next(a for a in entry.assets if a.id == item["asset_id"])
            raw = read_artifact(library, asset.path)
            require(digest(raw) == item["sha256"], "Bundle item hash mismatch.")
            value = strict_json(raw) if asset.mime_type == "application/json" else None
            require(role_of(asset, value, asset.format) == item["role"], "Bundle asset role mismatch.")
            if isinstance(value, dict) and value.get("kind") == "offline-text":
                require(value.get("version") == 1 and isinstance(value.get("text"), str), "Invalid offline text wrapper.")
                original = Asset.model_validate(value.get("original_asset"))
                original_raw = read_artifact(library, original.path)
                require(value.get("archive_path") == relative(original.path) and value.get("sha256") == digest(original_raw),
                        "Offline text original archive/hash mismatch.")
                require(value["text"].encode("utf-8") == original_raw and value.get("filename") == Path(original.path).name
                        and value.get("format") == asset.format, "Offline wrapper does not retain full original file.")
                expected = library.asset_provenance(entry, original)
                require(value.get("provenance") == {k: expected[k] for k in ("origin", "source_url", "license", "license_path")},
                        "Offline wrapper provenance mismatch.")
            if isinstance(value, dict) and value.get("kind") == "offline-excluded-source":
                original = Asset.model_validate(value.get("original_asset"))
                original_path = relative(original.path)
                require(original_path in EXCLUDED_TOOLING and value.get("archive_path") == excluded_path(original_path)
                        and value.get("sha256") == EXCLUDED_TOOLING[original_path]
                        and value.get("complete") is False and value.get("content_omitted") is True
                        and value.get("static_risk") == "process-execution"
                        and not any(key in value for key in ("code", "text", "content")),
                        "Unserved tooling index falsely claims or contains complete source.")
                require(digest(read_artifact(library, excluded_path(original_path))) == EXCLUDED_TOOLING[original_path],
                        "Unserved tooling original hash mismatch.")
            if isinstance(value, dict) and value.get("kind") == "offline-license":
                require(isinstance(value.get("licenses"), list) and bool(value["licenses"]), "Empty license wrapper.")
                for license in value["licenses"]:
                    raw_license = read_artifact(library, license["path"])
                    require(license["sha256"] == digest(raw_license) and license["text"].encode("utf-8") == raw_license,
                            "License wrapper differs from retained license.")
            require(item["license_path"] is not None and bool(read_artifact(library, item["license_path"]).strip()),
                    "Asset lacks retained license.")
        for archive in dossier["archives"]:
            require(digest(read_artifact(library, archive["path"])) == archive["sha256"], "Archive index hash mismatch.")
        items = dossier["assets"]
        visuals = [i for i in items if i["role"] == "visual"]
        animation = [i for i in items if i["role"] == "animation-source"]
        require(visuals and any(i["role"] in {"animation-source", "code", "document", "example"} for i in items),
                "Reference lacks visual or accessible source/document/example.")
        require(any(i["role"] == "license" for i in items) or dossier["coverage"]["license"] == "archive-only",
                "Reference lacks accessible licensing.")
        if animation:
            animation_sources += 1
            gifs = [a for a in entry.assets if a.mime_type == "image/gif"]
            has_preview = any(animated_frames(read_artifact(library, a.path)) > 1 for a in gifs)
            if has_preview:
                animations += 1
                require(dossier["coverage"]["animated_preview"] == "available" and dossier["kind"] == "animated-icon",
                        "Incorrect animated preview coverage.")
            else:
                unsupported_previews += 1
                require(dossier["coverage"]["animated_preview"] == "unavailable" and dossier["kind"] == "unsupported-animation"
                        and bool(dossier["limitations"]), "Missing preview must be honestly unsupported, never a static GIF success.")
            require(dossier["coverage"]["animation"] == "available", "Incorrect animation source coverage.")
        else:
            require(dossier["coverage"]["animation"] == "not-applicable"
                    and dossier["coverage"]["animated_preview"] == "not-applicable", "False animation coverage.")
        upstream = any(i["origin"] == "upstream" and i["role"] in {"code", "document"} for i in items)
        source_references += upstream
        require((dossier["coverage"]["source"] == "selected-files") == upstream, "False upstream source coverage.")
        require((dossier["coverage"]["visual"] == "original-concept") == all(i["origin"] == "original" for i in visuals),
                "False original/upstream visual coverage.")
        if not any(i["role"] == "code" for i in items):
            require(dossier["coverage"]["implementation"] != "selected-files", "False implementation completeness.")
    require(len(entries) >= min_references and animations >= min_animations and source_references >= min_source_references
            and animation_sources >= min_animation_sources,
            "Offline minimum reference/animation/selected-source coverage not met.")
    return {"reference_count": len(entries), "animated_reference_count": animations,
            "animation_source_reference_count": animation_sources,
            "unsupported_animation_preview_count": unsupported_previews,
            "selected_upstream_source_reference_count": source_references,
            "artifact_count": len(found), "bytes_inspected": total, "offline": True,
            "unserved_tooling_archive_count": len(report["unserved_tooling_findings"]),
            "limitations": BASE_LIMITATIONS}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--min-animated-previews", type=int, default=300)
    parser.add_argument("--min-animation-sources", type=int, default=300)
    args = parser.parse_args(argv)
    try:
        result = validate(args.root, min_animations=args.min_animated_previews,
                          min_animation_sources=args.min_animation_sources) if args.check else build(args.root)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (LibraryError, CollectionError) as exc:
        print("Offline library check/build failed: " + str(exc), file=sys.stderr)
        return 1
    except (ValidationError, OSError, ValueError, RecursionError, UnicodeError):
        print("Offline library check/build failed: missing, unsafe, oversized, corrupt or incomplete local data.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
