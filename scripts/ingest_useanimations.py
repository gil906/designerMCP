#!/usr/bin/env python3
"""Opt-in, bounded import of a pinned MIT source tree. Never imported by MCP."""
import argparse
import concurrent.futures
import hashlib
import http.client
import ipaddress
import json
import mimetypes
import pathlib
import queue
import socket
import ssl
import threading
import time
import urllib.parse
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
REVISION = "f7af6f53f6f1a97486ad95756b281a34e9f7c670"
REPO = "UseAnimations/react-useanimations"
RAW = f"https://raw.githubusercontent.com/{REPO}/{REVISION}/"
HOSTS = frozenset({"api.github.com", "raw.githubusercontent.com"})
MAX_FILE = 2 * 1024 * 1024
MAX_TOTAL = 64 * 1024 * 1024
MAX_ASSETS = 1000
MAX_SECONDS = 600
REQUEST_SECONDS = 20


def safe_path(relative):
    """Reject traversal and symlinks even in existing parent directories."""
    path = pathlib.PurePosixPath(relative)
    if (path.is_absolute() or "\\" in relative or
            any(part in {"", ".", ".."} for part in relative.split("/"))):
        raise ValueError("unsafe asset path")
    dest = DATA.joinpath(*path.parts)
    for parent in [DATA, *dest.relative_to(DATA).parents]:
        candidate = parent if parent.is_absolute() else DATA / parent
        if candidate.is_symlink():
            raise ValueError("symlink in asset path")
    if dest.is_symlink():
        raise ValueError("symlink asset")
    return dest


def save(relative, body):
    dest = safe_path(relative)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(body)


def json_save(relative, value):
    save(relative, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode())


def public_addresses(host, timeout):
    result = queue.Queue()

    def resolve():
        try:
            result.put(socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM))
        except Exception as error:
            result.put(error)

    threading.Thread(target=resolve, daemon=True).start()
    addresses = result.get(timeout=max(0.01, timeout))
    if isinstance(addresses, Exception):
        raise addresses
    ips = list(dict.fromkeys(row[4][0] for row in addresses))
    if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):
        raise ValueError("DNS resolved to non-public IP")
    return ips


class PinnedHTTPS(http.client.HTTPSConnection):
    """Connect to the already checked IP, while verifying TLS against the host."""
    def __init__(self, host, ip, timeout):
        super().__init__(host, timeout=timeout, context=ssl.create_default_context())
        self.ip = ip

    def connect(self):
        sock = socket.create_connection((self.ip, 443), self.timeout)
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except Exception:
            sock.close()
            raise


class Fetcher:
    def __init__(self):
        self.deadline = time.monotonic() + MAX_SECONDS
        self.total = 0
        self.attempts = []
        self.lock = threading.Lock()

    def remaining(self, request_deadline):
        left = min(self.deadline, request_deadline) - time.monotonic()
        if left <= 0:
            raise TimeoutError("import/request deadline exceeded")
        return left

    def fetch(self, url):
        request_deadline = time.monotonic() + REQUEST_SECONDS
        for redirect in range(6):
            record = {"url": url, "status": "pending", "bytes": 0}
            with self.lock:
                self.attempts.append(record)
            connection = None
            try:
                parsed = urllib.parse.urlsplit(url)
                if (parsed.scheme != "https" or parsed.hostname not in HOSTS or
                        parsed.username or parsed.password or parsed.port not in {None, 443} or
                        parsed.fragment):
                    raise ValueError("URL must use allowlisted HTTPS host without credentials")
                ips = public_addresses(parsed.hostname, self.remaining(request_deadline))
                connection = PinnedHTTPS(parsed.hostname, ips[0], self.remaining(request_deadline))
                target = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
                connection.request("GET", target, headers={
                    "User-Agent": "MCP-designer-offline-import/1",
                    "Accept-Encoding": "identity",
                })
                connection.sock.settimeout(self.remaining(request_deadline))
                response = connection.getresponse()
                record["http_status"] = response.status
                if response.status in {301, 302, 303, 307, 308}:
                    if redirect == 5 or not response.getheader("Location"):
                        raise ValueError("redirect limit or missing location")
                    record["status"] = "redirect"
                    url = urllib.parse.urljoin(url, response.getheader("Location"))
                    continue
                if response.status != 200:
                    raise ValueError(f"HTTP {response.status}")
                if response.getheader("Content-Encoding", "identity") != "identity":
                    raise ValueError("compressed response not permitted")
                length = response.getheader("Content-Length")
                if length and int(length) > MAX_FILE:
                    raise ValueError("file exceeds byte cap")
                chunks = []
                size = 0
                while True:
                    if connection.sock:
                        connection.sock.settimeout(self.remaining(request_deadline))
                    else:
                        self.remaining(request_deadline)
                    chunk = response.read1(min(65536, MAX_FILE + 1 - size))
                    if not chunk:
                        break
                    size += len(chunk)
                    with self.lock:
                        self.total += len(chunk)
                        if self.total > MAX_TOTAL:
                            raise ValueError("total byte cap exceeded")
                    if size > MAX_FILE:
                        raise ValueError("file byte cap exceeded")
                    chunks.append(chunk)
                record.update(status="downloaded", bytes=size)
                return b"".join(chunks)
            except Exception as error:
                record.update(status="failed", error=str(error)[:300])
                raise
            finally:
                if connection:
                    connection.close()
        raise ValueError("redirect limit exceeded")


def git_hash(body):
    return hashlib.sha1(b"blob " + str(len(body)).encode() + b"\0" + body).hexdigest()


def lottie_safe(value):
    if not isinstance(value, dict) or not all(k in value for k in ("v", "fr", "ip", "op", "layers")):
        return False
    for asset in value.get("assets", []):
        if asset.get("u") or (asset.get("p") and not str(asset["p"]).startswith("data:")):
            raise ValueError("Lottie references external media")
    return True


def animation_entry(item):
    name = item["name"]
    title = item.get("title", name)
    category = item.get("category", "icons").lower()
    return {
        "id": "useanimations-" + item.get("id", name),
        "title": title + " animated icon",
        "summary": f"Offline vector motion for {title.lower()} feedback and controls.",
        "categories": ["animation", "icons", category],
        "tags": sorted(set(["lottie", "microinteraction", "useanimations",
                            item.get("interaction", "interaction"), *item.get("keywords", [])])),
        "platforms": ["web", "react", "shadcn", "flutter"],
        "guidance": (
            f"Original integration guidance: use {title.lower()} with a visible label or accessible "
            "button name. Preserve a still state for prefers-reduced-motion; do not communicate "
            "status using motion alone. Import the local JSON into your chosen Lottie renderer. "
            "JSON is renderer-neutral; Flutter may differ in supported Lottie features. "
            "The upstream icon and React source are MIT, not authored by this project."
        ),
        "source_url": RAW + f"icons/{name}.json",
        "license": "MIT",
        "license_path": "licenses/useanimations-MIT.txt",
        "assets": [{"id": name, "path": f"assets/useanimations/{name}.json",
                    "mime_type": "application/json", "title": title + " Lottie JSON"}],
        "examples": [
            {"language": "tsx", "description": "Original local-data React integration; supply a local Lottie renderer.",
             "code": f'import animationData from "./data/assets/useanimations/{name}.json";\n'
                     '// Lottie is your locally installed renderer component.\n'
                     f'<span aria-hidden="true"><Lottie animationData={{animationData}} '
                     'autoplay={!reducedMotion} loop={false} /></span>'},
            {"language": "dart", "description": "Original Flutter usage; declare the bundled JSON in pubspec.yaml.",
             "code": f'Lottie.asset("assets/useanimations/{name}.json",\n'
                     '  animate: !MediaQuery.of(context).disableAnimations,\n'
                     '  repeat: false, width: 24, height: 24);'},
        ],
    }


def import_corpus():
    fetcher = Fetcher()
    report = {
        "version": 1, "started_at": datetime.now(timezone.utc).isoformat(),
        "source": f"https://github.com/{REPO}", "revision": REVISION,
        "primary_site": "https://useanimations.com",
        "advertised_animation_count": 300,
        "coverage": "Pinned licensed source tree, not a complete website mirror. Branding media excluded; no unlicensed articles or media fetched.",
        "limits": {"file_bytes": MAX_FILE, "total_bytes": MAX_TOTAL,
                   "assets": MAX_ASSETS, "deadline_seconds": MAX_SECONDS,
                   "request_seconds": REQUEST_SECONDS, "redirects": 5, "workers": 8},
        "assets": [], "skipped": [], "failures": [], "previous_imports": [],
    }
    previous_path = safe_path("import-report.json")
    if previous_path.is_file():
        previous = json.loads(previous_path.read_text())
        report["previous_imports"] = previous.get("previous_imports", [])[-9:] + [{
            "started_at": previous.get("started_at"), "finished_at": previous.get("finished_at"),
            "downloaded_files": previous.get("downloaded_files"), "failures": previous.get("failures", []),
            "request_count": len(previous.get("requests", [])),
        }]
    entries = []
    try:
        license_body = fetcher.fetch(RAW + "LICENSE")
        if b"MIT License" not in license_body or b"2018-2026 useAnimations" not in license_body:
            raise ValueError("expected MIT license not found")
        save("licenses/useanimations-MIT.txt", license_body)
        report["license_sha256"] = hashlib.sha256(license_body).hexdigest()
        tree = json.loads(fetcher.fetch(
            f"https://api.github.com/repos/{REPO}/git/trees/{REVISION}?recursive=1"))
        if tree.get("truncated"):
            raise ValueError("truncated upstream tree")
        files = [row for row in tree["tree"] if row["type"] == "blob"]
        report["tree_sha"] = tree["sha"]
        report["upstream_file_count"] = len(files)
        selected = []
        excluded = {".github/assets/preview.webp", "website/public/apple-touch-icon.png",
                    "website/public/favicon.ico", "website/public/favicon.svg",
                    "website/public/logo.svg", "website/public/og.png"}
        for row in files:
            if row["path"] in excluded or row.get("mode") == "120000":
                report["skipped"].append({"path": row["path"], "reason": "branding media or symlink"})
            else:
                selected.append(row)
        if len(selected) > MAX_ASSETS:
            raise ValueError("source exceeds asset cap")
        flag_license = fetcher.fetch(RAW + "website/public/flags/LICENSE")
        if b"Permission is hereby granted" not in flag_license:
            raise ValueError("flag license missing")
        save("licenses/flags-MIT.txt", flag_license)

        def download(row):
            path = row["path"]
            body = fetcher.fetch(RAW + urllib.parse.quote(path, safe="/"))
            if git_hash(body) != row["sha"]:
                raise ValueError("Git blob checksum mismatch")
            local = "assets/useanimations/source/" + path
            if path.startswith("icons/") and path.endswith(".json"):
                local = "assets/useanimations/" + path.split("/")[-1]
                value = json.loads(body)
                if path not in {"icons/catalog.json", "icons/svg.json"} and not lottie_safe(value):
                    raise ValueError("invalid Lottie asset")
            save(local, body)
            return {"upstream_path": path, "path": local, "bytes": len(body),
                    "git_blob_sha1": row["sha"], "sha256": hashlib.sha256(body).hexdigest(),
                    "source_url": RAW + urllib.parse.quote(path, safe="/"),
                    "license_path": "licenses/flags-MIT.txt" if path.startswith("website/public/flags/")
                    else "licenses/useanimations-MIT.txt"}

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            futures = {pool.submit(download, row): row for row in selected}
            for future in concurrent.futures.as_completed(futures):
                row = futures[future]
                try:
                    report["assets"].append(future.result())
                except Exception as error:
                    report["failures"].append({"path": row["path"], "error": str(error)[:300]})
        successful = {item["upstream_path"] for item in report["assets"]}
        if "icons/catalog.json" in successful:
            catalog = json.loads(safe_path("assets/useanimations/catalog.json").read_text())
            for item in catalog:
                if f'icons/{item["name"]}.json' in successful:
                    entries.append(animation_entry(item))
            report["catalog_animation_count"] = len(catalog)
        metadata_assets = [
            {"id": "catalog", "path": "assets/useanimations/catalog.json",
             "mime_type": "application/json", "title": "Upstream icon catalog and interaction metadata"},
            {"id": "svg-reference", "path": "assets/useanimations/svg.json",
             "mime_type": "application/json", "title": "Upstream static SVG strings, reference only"},
        ]
        entries.append({
            "id": "useanimations-offline-metadata", "title": "useAnimations icon catalog and SVG reference",
            "summary": "Names, categories, keywords, interaction modes and static SVG reference data for the pinned icon library.",
            "categories": ["icons", "documentation", "design-tokens"],
            "tags": ["useanimations", "catalog", "svg", "offline", "metadata"],
            "platforms": ["web", "react", "shadcn"],
            "guidance": "Sourced MIT catalog metadata. SVG strings are reference data only, never automatically executed or rendered. Use local Lottie JSON for animation. Branded icon availability does not grant trademark rights.",
            "source_url": RAW + "icons/catalog.json",
            "license": "MIT", "license_path": "licenses/useanimations-MIT.txt",
            "assets": [asset for asset in metadata_assets
                       if "icons/" + asset["path"].split("/")[-1] in successful],
            "examples": [],
        })
        flags = [row for row in report["assets"]
                 if row["upstream_path"].startswith("website/public/flags/")]
        if flags:
            entries.append({
                "id": "useanimations-offline-flags", "title": "Licensed language flag references",
                "summary": "Static SVG language-selector flag references and their distinct MIT attribution.",
                "categories": ["icons", "navigation", "localization"],
                "tags": ["svg", "language", "offline", "reference"],
                "platforms": ["web", "react", "shadcn"],
                "guidance": "Sourced flag assets carry their separate MIT notice from Panayiotis Lipiridis. Flags are imperfect language identifiers; always include the language name. SVG files are reference-only, not inline MCP rendered.",
                "source_url": RAW + "website/public/flags/LICENSE",
                "license": "MIT", "license_path": "licenses/flags-MIT.txt",
                "assets": [{"id": "flag-" + str(index), "path": row["path"],
                            "mime_type": "image/svg+xml" if row["path"].endswith(".svg") else "text/plain",
                            "title": row["upstream_path"].split("/")[-1]}
                           for index, row in enumerate(sorted(flags, key=lambda row: row["path"]))],
                "examples": [],
            })
        source_assets = [
            {"id": "source-" + str(index), "path": row["path"],
             "mime_type": mimetypes.guess_type(row["path"])[0] or "text/plain",
             "title": row["upstream_path"]}
            for index, row in enumerate(sorted(report["assets"], key=lambda row: row["path"]))
            if not row["upstream_path"].startswith("icons/") and
            not row["upstream_path"].startswith("website/public/flags/")
        ]
        for offset in range(0, len(source_assets), 24):
            entries.append({
            "id": "useanimations-offline-source-" + str(offset // 24 + 1), "title": "useAnimations MIT source and documentation " + str(offset // 24 + 1),
            "summary": "Pinned React component, vector player, tests, catalog, build configuration, and licensed website implementation.",
            "categories": ["animation", "documentation", "components"],
            "tags": ["react", "shadcn", "lottie", "source-code", "offline", "MIT"],
            "platforms": ["web", "react", "shadcn"],
            "guidance": "Upstream MIT code is retained verbatim as reference data. Nothing is executed during ingestion or MCP requests. Source may require dependencies to build; offline storage does not imply those dependencies are installed. Read licenses before reuse. Report retains hashes for every file.",
            "source_url": f"https://github.com/{REPO}/tree/{REVISION}",
            "license": "MIT", "license_path": "licenses/useanimations-MIT.txt",
            "assets": source_assets[offset:offset + 24], "examples": [],
        })
    except Exception as error:
        report["failures"].append({"source": report["source"], "error": str(error)[:300]})
    finally:
        from original_patterns import original_entries
        entries.extend(original_entries())
        json_save("library.json", {"version": 1, "entries": entries})
        report["assets"].sort(key=lambda row: row["path"])
        report["requests"] = fetcher.attempts
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        report["downloaded_bytes"] = sum(row["bytes"] for row in report["assets"])
        report["request_bytes"] = fetcher.total
        report["downloaded_files"] = len(report["assets"])
        report["animation_count"] = sum(entry["id"].startswith("useanimations-") and
                                        not entry["id"].startswith("useanimations-offline-") for entry in entries)
        report["entry_count"] = len(entries)
        report["original_entry_count"] = sum(entry["id"].startswith("original-") for entry in entries)
        report["original_assets"] = [
            {"path": asset["path"],
             "sha256": hashlib.sha256(safe_path(asset["path"]).read_bytes()).hexdigest(),
             "license_path": "licenses/original-CC0.txt"}
            for entry in entries if entry["id"].startswith("original-")
            for asset in entry["assets"]
        ]
        report["local_asset_count"] = len(report["assets"]) + len(report["original_assets"])
        report["complete_licensed_selection"] = not report["failures"]
        json_save("import-report.json", report)
    print(json.dumps({key: report[key] for key in
                      ("downloaded_files", "downloaded_bytes", "animation_count", "entry_count",
                       "complete_licensed_selection")}))
    return 1 if report["failures"] else 0


def validate():
    # Lazy imports keep the opt-in network importer independent of MCP startup.
    from designer_mcp.library import (
        Asset, Catalog, Library, MAX_LIBRARY_BYTES, MAX_OUTPUT_BYTES,
        OfflineBundle, path_parts, strict_json,
    )

    local = Library(DATA.parent)
    catalog = Catalog.model_validate(strict_json(local.read(("data", "library.json"), MAX_LIBRARY_BYTES)))
    report = strict_json(local.read(("data", "import-report.json"), MAX_LIBRARY_BYTES))
    relocated = {}
    for entry in catalog.entries:
        bundle_asset = next((a for a in entry.assets if a.id == "offline-bundle"), None)
        if bundle_asset is None:
            continue
        if bundle_asset.mime_type != "application/json" or bundle_asset.format != "offline-bundle":
            raise ValueError("invalid offline bundle asset")
        bundle = OfflineBundle.model_validate(strict_json(local.read(path_parts(bundle_asset.path, "assets"), MAX_OUTPUT_BYTES)))
        if bundle.reference_id != entry.id:
            raise ValueError("offline bundle reference mismatch")
        for archive in bundle.archives:
            if not archive.path.startswith("archives/"):
                continue
            if archive.original_asset is None:
                raise ValueError("archived source lacks original asset metadata")
            original = archive.original_asset
            original_path = "/".join(path_parts(original.path, "assets")[1:])
            expected_path = "archives/offline-tooling/" + original_path.removeprefix("assets/")
            if archive.path != expected_path or original_path in relocated:
                raise ValueError("invalid or duplicate archived source mapping")
            served = next((a for a in entry.assets if a.id == original.id), None)
            if served is None or served.mime_type != "application/json":
                raise ValueError("archived source lacks catalog omission index")
            index = strict_json(local.read(path_parts(served.path, "assets"), MAX_OUTPUT_BYTES))
            if (
                not isinstance(index, dict) or index.get("kind") != "offline-excluded-source"
                or index.get("archive_path") != archive.path or index.get("sha256") != archive.sha256
                or index.get("complete") is not False or index.get("content_omitted") is not True
                or Asset.model_validate(index.get("original_asset")) != original
            ):
                raise ValueError("archived source omission index mismatch")
            relocated[original_path] = (path_parts(archive.path, "archives"), archive.sha256)

    def original_bytes(path, expected_sha256):
        # Historical reports still authenticate the ORIGINAL bytes with both
        # SHA256 and Git blob SHA1. Only an explicit catalog omission/archive
        # mapping can replace a missing old path; missing files are never skipped.
        safe_path(path)
        parts = path_parts(path, "assets")
        body = local.read(parts, MAX_FILE, optional=True)
        canonical = "/".join(parts[1:])
        if canonical in relocated and body is not None:
            raise ValueError("archived tooling source restored beneath serving assets")
        if body is None:
            if canonical not in relocated:
                raise ValueError("missing original asset without archive mapping")
            archive_parts, archive_sha256 = relocated[canonical]
            if archive_sha256 != expected_sha256:
                raise ValueError("historical/archive checksum metadata mismatch")
            body = local.read(archive_parts, MAX_FILE)
        return body

    ids = set()
    assets = set()
    for model in catalog.entries:
        entry = model.model_dump(exclude_none=True)
        if entry["id"] in ids:
            raise ValueError("duplicate entry id")
        ids.add(entry["id"])
        for field in ("title", "summary", "categories", "tags", "platforms", "guidance",
                      "source_url", "license", "assets", "examples"):
            if field not in entry:
                raise ValueError("missing entry field: " + field)
        if entry.get("license_path"):
            local.read(path_parts(entry["license_path"], "licenses"), MAX_FILE)
        for asset in entry["assets"]:
            local.read(path_parts(asset["path"], "assets"), MAX_FILE)
            assets.add(asset["path"])
    for asset in report["assets"]:
        body = original_bytes(asset["path"], asset["sha256"])
        if hashlib.sha256(body).hexdigest() != asset["sha256"] or git_hash(body) != asset["git_blob_sha1"]:
            raise ValueError("checksum mismatch: " + asset["path"])
        if asset["upstream_path"].startswith("icons/") and asset["upstream_path"] not in {"icons/catalog.json", "icons/svg.json"}:
            if not lottie_safe(json.loads(body)):
                raise ValueError("invalid Lottie")
    for asset in report.get("original_assets", []):
        if hashlib.sha256(original_bytes(asset["path"], asset["sha256"])).hexdigest() != asset["sha256"]:
            raise ValueError("original asset checksum mismatch")
    print(json.dumps({"valid": True, "entries": len(ids), "referenced_assets": len(assets),
                      "downloaded_files": report["downloaded_files"],
                      "animations": report["animation_count"],
                      "bytes": report["downloaded_bytes"]}))
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--allow-network", action="store_true", help="explicitly import pinned upstream files")
    mode.add_argument("--validate-only", action="store_true", help="validate local corpus; no network")
    mode.add_argument("--stats", action="store_true", help="validated local statistics; no network")
    args = parser.parse_args()
    return import_corpus() if args.allow_network else validate()


if __name__ == "__main__":
    raise SystemExit(main())
