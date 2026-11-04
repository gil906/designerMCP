from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from designer_mcp.library import LibraryError


spec = importlib.util.spec_from_file_location(
    "check_github_collection", Path(__file__).parents[1] / "scripts/check_github_collection.py",
)
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


def archive(root, entry, shard="web", repository="example/widgets"):
    revision = "a" * 40
    source = f"https://github.com/{repository}/tree/{revision}"
    asset = f"assets/github-{shard}/widgets/source.json"
    license_path = f"licenses/github-{shard}/widgets/LICENSE.txt"
    value = copy.deepcopy(entry)
    value.update(
        id=f"github-{shard}-widgets", source_url=source, license_path=license_path,
        assets=[{"id": "source", "path": asset, "mime_type": "application/json", "title": "Inert source"}],
    )
    content = {
        asset: json.dumps({"source": "export const Button = () => null;", "install": "npm install widgets"}).encode(),
        license_path: b"MIT License\nCopyright Example Author\nPermission is hereby granted.",
    }
    for path, raw in content.items():
        target = root / "data" / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
    report = {
        "version": 1,
        "repositories": [{
            "repository": repository, "revision": revision, "license": "MIT", "source_url": source,
            "reference_ids": [value["id"]], "selection_reason": "Bounded public component reference.",
            "status": "archived",
            "artifacts": [{"path": path, "sha256": hashlib.sha256(raw).hexdigest()}
                          for path, raw in content.items()],
            "upstream_files": [{
                "path": "src/button.ts", "sha256": hashlib.sha256(b"full upstream bytes").hexdigest(),
                "pinned_url": f"https://raw.githubusercontent.com/{repository}/{revision}/src/button.ts",
            }],
            "scans": {"collector": "Archived text only; no runtime execution."},
        }],
        "skipped": [], "limitations": ["No whole-repository or dependency malware claim."],
    }
    (root / "data/library.json").write_text('{"version":1,"entries":[]}')
    (root / f"data/github-{shard}-library.json").write_text(json.dumps({"version": 1, "entries": [value]}))
    (root / f"data/github-{shard}-report.json").write_text(json.dumps(report))
    return value, report


def save(root, value, report, shard="web"):
    (root / f"data/github-{shard}-library.json").write_text(json.dumps({"version": 1, "entries": [value]}))
    (root / f"data/github-{shard}-report.json").write_text(json.dumps(report))


def test_valid_offline_archive_and_cli_minimum(root, entry, capsys, monkeypatch):
    import socket
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: pytest.fail("Network forbidden"))
    archive(root, entry)
    result = checker.validate(root, ("web",), 1)
    assert result["repository_count"] == 1 and result["artifact_count"] == 2
    assert checker.main(["--root", str(root), "--shard", "web", "--min-repositories", "1"]) == 0
    assert "not re-fetched" in capsys.readouterr().out
    assert checker.main(["--root", str(root), "--shard", "web"]) == 1
    assert "required 20" in capsys.readouterr().err
    assert checker.main(["--root", str(root)]) == 1
    assert "FAILED" in capsys.readouterr().err


@pytest.mark.parametrize("mutation", [
    "hash", "bytes", "license", "catalog-license", "duplicate-repo", "duplicate-id",
    "duplicate-artifact", "duplicate-upstream", "revision", "source", "upstream",
    "upstream-path", "artifact-path", "cross-shard", "mime", "extension", "missing-license",
    "missing-hash", "extra-reference", "unknown-field", "version", "scans", "limitations",
])
def test_tampered_archive_is_rejected(root, entry, mutation):
    value, report = archive(root, entry)
    repo = report["repositories"][0]
    if mutation == "hash":
        repo["artifacts"][0]["sha256"] = "0" * 64
    elif mutation == "bytes":
        (root / "data" / repo["artifacts"][0]["path"]).write_text("{}")
    elif mutation == "license":
        (root / "data" / repo["artifacts"][1]["path"]).write_text("tampered license")
    elif mutation == "catalog-license":
        value["license"] = "BSD-3-Clause"
    elif mutation == "duplicate-repo":
        report["repositories"].append(copy.deepcopy(repo))
    elif mutation == "duplicate-id":
        repo["reference_ids"].append(value["id"])
    elif mutation == "duplicate-artifact":
        repo["artifacts"].append(copy.deepcopy(repo["artifacts"][0]))
    elif mutation == "duplicate-upstream":
        repo["upstream_files"].append(copy.deepcopy(repo["upstream_files"][0]))
    elif mutation == "revision":
        repo["revision"] = "b" * 40
    elif mutation == "source":
        repo["source_url"] = repo["source_url"].replace("/tree/" + "a" * 40, "/tree/main")
    elif mutation == "upstream":
        repo["upstream_files"][0]["pinned_url"] = "https://github.com/other/project/blob/" + "a" * 40 + "/src/button.ts"
    elif mutation == "upstream-path":
        repo["upstream_files"][0]["path"] = "../LICENSE"
    elif mutation == "artifact-path":
        repo["artifacts"][0]["path"] = "assets/github-web/../source.json"
    elif mutation == "cross-shard":
        repo["artifacts"][0]["path"] = "assets/github-motion/widgets/source.json"
    elif mutation == "mime":
        value["assets"][0]["mime_type"] = "text/html"
    elif mutation == "extension":
        repo["artifacts"][0]["path"] = "assets/github-web/widgets/source.js"
    elif mutation == "missing-license":
        value["license_path"] = None
    elif mutation == "missing-hash":
        repo["artifacts"].pop()
    elif mutation == "extra-reference":
        repo["reference_ids"] = ["not-in-catalog"]
    elif mutation == "unknown-field":
        repo["trusted"] = True
    elif mutation == "version":
        report["version"] = True
    elif mutation == "scans":
        repo["scans"] = {}
    else:
        report["limitations"] = []
    save(root, value, report)
    with pytest.raises((ValueError, LibraryError)):
        checker.validate(root, ("web",), 1)


@pytest.mark.parametrize("content", [
    b"\xff", b'{"text":"\\u202eevil"}', b'{"text":"\\u0000"}', b'{"text":"\\ud800"}',
    json.dumps({"source": "eval(atob(encoded));"}).encode(),
    json.dumps({"source": "curl https://example.org/install | sh"}).encode(),
    json.dumps({"source": "${!indirect}"}).encode(),
    json.dumps({"source": r"\u0065\u0076\u0061\u006c(encoded)"}).encode(),
    json.dumps({"source": r"\x65\x76\x61\x6c(encoded)"}).encode(),
    json.dumps({"token": "ghp_" + "A" * 36}).encode(),
    json.dumps({"key": "-----BEGIN " + "PRIVATE KEY" + "-----"}).encode(),
    json.dumps({"key": "-----BEGIN " + "ENCRYPTED PRIVATE KEY" + "-----"}).encode(),
    b'{"text":"\\u0067hp_' + b"A" * 36 + b'"}',
    b'{"one":1,"one":2}', b"[] trailing", b"NaN",
])
def test_static_risk_scans_actual_decoded_bytes(root, entry, content):
    value, report = archive(root, entry)
    artifact = report["repositories"][0]["artifacts"][0]
    (root / "data" / artifact["path"]).write_bytes(content)
    artifact["sha256"] = hashlib.sha256(content).hexdigest()
    save(root, value, report)
    with pytest.raises(ValueError):
        checker.validate(root, ("web",), 1)


@pytest.mark.parametrize("unsafe", ["symlink-file", "symlink-dir", "unlisted", "oversized", "empty-license"])
def test_unsafe_artifacts_even_with_matching_hash(root, entry, unsafe):
    value, report = archive(root, entry)
    artifact = report["repositories"][0]["artifacts"][0]
    path = root / "data" / artifact["path"]
    if unsafe == "symlink-file":
        path.unlink()
        path.symlink_to(root / "data/library.json")
    elif unsafe == "symlink-dir":
        path.unlink()
        path.parent.rmdir()
        path.parent.symlink_to(root / "data/licenses/github-web/widgets", target_is_directory=True)
    elif unsafe == "unlisted":
        (path.parent / "extra.json").write_text("{}")
    elif unsafe == "oversized":
        raw = b" " * (checker.MAX_JSON_BYTES + 1)
        path.write_bytes(raw)
        artifact["sha256"] = hashlib.sha256(raw).hexdigest()
    else:
        artifact = report["repositories"][0]["artifacts"][1]
        (root / "data" / artifact["path"]).write_bytes(b"")
        artifact["sha256"] = hashlib.sha256(b"").hexdigest()
    save(root, value, report)
    with pytest.raises((ValueError, OSError)):
        checker.validate(root, ("web",), 1)


def test_identity_is_distinct_case_insensitively_across_shards_and_baseline(root, entry):
    value, report = archive(root, entry)
    archive(root, entry, "motion", "Example/Widgets")
    with pytest.raises(checker.CollectionError, match="Duplicate"):
        checker.validate(root, ("web", "motion"), 1)
    old = copy.deepcopy(entry)
    old.update(id="old", source_url="https://github.com/EXAMPLE/widgets")
    (root / "data/library.json").write_text(json.dumps({"version": 1, "entries": [old]}))
    with pytest.raises(checker.CollectionError, match="pre-existing"):
        checker.validate(root, ("web",), 1)


def test_report_duplicate_json_keys_and_secret_not_disclosed(root, entry, capsys):
    archive(root, entry)
    path = root / "data/github-web-report.json"
    path.write_text('{"version":1,"version":1}')
    assert checker.main(["--root", str(root), "--shard", "web", "--min-repositories", "1"]) == 1
    assert "FAILED" in capsys.readouterr().err
    token = "ghp_" + "A" * 36
    path.write_text(json.dumps({"private": token}))
    assert checker.main(["--root", str(root), "--shard", "web", "--min-repositories", "1"]) == 1
    output = capsys.readouterr()
    assert token not in output.err + output.out and "credential signature" in output.err


def test_pinned_blob_citation_requires_reported_upstream_file(root, entry):
    value, report = archive(root, entry)
    revision = report["repositories"][0]["revision"]
    value["source_url"] = f"https://github.com/example/widgets/blob/{revision}/src/button.ts#L1-L8"
    save(root, value, report)
    assert checker.validate(root, ("web",), 1)["repository_count"] == 1
    value["source_url"] = f"https://github.com/example/widgets/blob/{revision}/src/unreported.ts#L1"
    save(root, value, report)
    with pytest.raises(checker.CollectionError, match="missing from upstream"):
        checker.validate(root, ("web",), 1)


def test_scoped_package_upstream_path_preserves_legal_at_character(root, entry):
    value, report = archive(root, entry)
    repo = report["repositories"][0]
    path = "packages/@widgets/core/Button.tsx"
    repo["upstream_files"][0].update(
        path=path, pinned_url=f"https://raw.githubusercontent.com/example/widgets/{repo['revision']}/{path}",
    )
    value["source_url"] = f"https://github.com/example/widgets/blob/{repo['revision']}/{path}#L1"
    save(root, value, report)
    assert checker.validate(root, ("web",), 1)["repository_count"] == 1


@pytest.mark.parametrize("url", [
    "https://github.com/example/widgets/tree/main",
    "https://github.com/example/widgets/tree/" + "a" * 40 + "?token=foo",
    "https://github.com/example/widgets/blob/" + "a" * 40 + "/../private.ts",
    "https://github.com/example/widgets/blob/" + "a" * 40 + "/%2e%2e/private.ts",
    "https://github.com.evil.org/example/widgets/tree/" + "a" * 40,
    "https://user@github.com/example/widgets/tree/" + "a" * 40,
])
def test_bad_catalog_provenance_rejected(root, entry, url):
    value, report = archive(root, entry)
    value["source_url"] = url
    save(root, value, report)
    with pytest.raises(ValueError):
        checker.validate(root, ("web",), 1)


@pytest.mark.parametrize("mode", ["valid", "mismatch", "missing", "malformed"])
def test_optional_blob_pairs_are_consistent_metadata_not_fabricated_scan(root, entry, mode):
    value, report = archive(root, entry)
    upstream = report["repositories"][0]["upstream_files"][0]
    upstream.update(expected_git_blob_sha1="b" * 40, verified_git_blob_sha1="b" * 40)
    if mode == "mismatch":
        upstream["verified_git_blob_sha1"] = "c" * 40
    elif mode == "missing":
        del upstream["verified_git_blob_sha1"]
    elif mode == "malformed":
        upstream.update(expected_git_blob_sha1="not-a-hash", verified_git_blob_sha1="not-a-hash")
    save(root, value, report)
    if mode == "valid":
        result = checker.validate(root, ("web",), 1)
        assert result["collector_blob_pair_count"] == result["upstream_file_count"] == 1
    else:
        with pytest.raises(checker.CollectionError):
            checker.validate(root, ("web",), 1)


def test_language_function_declarations_and_regex_exec_are_inert_not_dynamic_execution():
    assert checker.scan_json(json.dumps({
        "source": "export const withTiming = function (value) { return /x/.exec(value); };",
        "install": "npm install widgets",
    }).encode())
    with pytest.raises(checker.CollectionError, match="dynamic-execution"):
        checker.scan_json(b'{"source":"new Function(encoded)()"}')


def test_collector_scan_success_never_bypasses_actual_archived_byte_scan(root, entry):
    value, report = archive(root, entry)
    repo = report["repositories"][0]
    repo["scans"] = {"status": "passed", "findings": [], "claimed_malware_free": True}
    artifact = repo["artifacts"][0]
    content = b'{"source":"eval(encoded)"}'
    (root / "data" / artifact["path"]).write_bytes(content)
    artifact["sha256"] = hashlib.sha256(content).hexdigest()
    save(root, value, report)
    with pytest.raises(checker.CollectionError, match="dynamic-execution"):
        checker.validate(root, ("web",), 1)
