from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest


spec = importlib.util.spec_from_file_location(
    "check_publication", Path(__file__).parents[1] / "scripts" / "check_publication.py"
)
assert spec is not None and spec.loader is not None
publication = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publication)


def test_tokens_are_blocked_without_disclosing_value():
    token = b"ghp_" + b"A" * 36
    findings = publication.inspect_content("example.txt", token)
    assert findings == ["example.txt:1: github-token"]
    assert token.decode() not in str(findings)


def test_private_paths_and_network_addresses_are_blocked():
    address = b".".join((b"192", b"168", b"23", b"7"))
    path = b"/" + b"root" + b"/private/settings"
    findings = publication.inspect_content("settings.txt", address + b"\n" + path)
    assert "settings.txt:1: private-network-address" in findings
    assert "settings.txt:2: private-home-path" in findings


def test_local_personal_denylist_is_case_insensitive():
    assert publication.inspect_content(
        "notes.txt", b"Private Person", (b"private person",)
    ) == ["notes.txt: local-private-denylist"]


def test_private_files_are_blocked_even_when_empty():
    assert publication.inspect_content(".env", b"") == [".env: private-file"]
    assert publication.inspect_content(".mcp.json", b"") == [".mcp.json: private-file"]


def test_quoted_json_and_yaml_credential_keys_are_blocked():
    value = "S" * 32
    assert "credential-literal" in str(publication.inspect_content(
        "config.json", json.dumps({"api_key": value}).encode()
    ))
    assert "credential-literal" in str(publication.inspect_content(
        "config.yml", ('"password": "' + value + '"').encode()
    ))


def test_public_attribution_and_placeholders_remain_allowed():
    assert publication.inspect_content(
        "data/licenses/example.txt", b"Copyright Example Author <author@example.org>"
    ) == []


def test_private_file_names_are_not_echoed():
    findings = publication.inspect_content(
        "private-person.txt", b"", (b"private-person",)
    )
    assert findings == ["[redacted-path]: local-private-denylist"]
    findings = publication.inspect_content(
        "private-person.txt", b"private-person", (b"private-person",)
    )
    assert findings == ["[redacted-path]: local-private-denylist"]


def test_outgoing_history_checks_content_removed_by_later_commit(tmp_path, monkeypatch):
    def git(*args):
        return subprocess.run(
            ["git", "-C", str(tmp_path), *args],
            check=True, capture_output=True, text=True,
        ).stdout.strip()

    git("init", "-b", "main", "--quiet")
    git("config", "user.name", "Test contributor")
    git("config", "user.email", "contributor@example.org")
    leak = tmp_path / "leak.txt"
    leak.write_bytes(b"ghp_" + b"B" * 36)
    git("add", "leak.txt")
    git("commit", "--quiet", "-m", "Add fixture")
    leak.unlink()
    git("add", "-u")
    git("commit", "--quiet", "-m", "Remove fixture")
    commit = git("rev-parse", "HEAD")
    monkeypatch.chdir(tmp_path)
    files = publication.outgoing_files(f"refs/heads/main {commit} refs/heads/main {'0' * 40}\n")
    findings = [finding for path, content in files for finding in publication.inspect_content(path, content)]
    assert any("github-token" in finding for finding in findings)


def test_outgoing_noncommit_object_is_blocked(tmp_path, monkeypatch):
    subprocess.run(
        ["git", "-C", str(tmp_path), "init", "--quiet"],
        check=True, capture_output=True,
    )
    result = subprocess.run(
        ["git", "-C", str(tmp_path), "hash-object", "-w", "--stdin"],
        input=b"ghp_" + b"C" * 36, check=True, capture_output=True,
    )
    blob = result.stdout.decode("ascii").strip()
    monkeypatch.chdir(tmp_path)
    with pytest.raises(RuntimeError, match="noncommit tags are blocked"):
        publication.outgoing_files(f"refs/tags/blob {blob} refs/tags/blob {'0' * 40}\n")
    assert publication.inspect_content(
        ".env.example", b'api_key = "YOUR_PLACEHOLDER_TOKEN"'
    ) == []
