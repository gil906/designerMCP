"""Local importer safety tests; network and disk mutations are mocked."""
import io
import json
import pathlib
import unittest
from unittest.mock import patch

import ingest_useanimations as ingest


class FakeResponse:
    def __init__(self, status=200, headers=None, body=b"{}"):
        self.status = status
        self.headers = headers or {}
        self.body = io.BytesIO(body)

    def getheader(self, name, default=None):
        return self.headers.get(name, default)

    def read1(self, size):
        return self.body.read(size)


class FakeConnection:
    response = FakeResponse()
    sock = None

    def __init__(self, *args):
        self.sock = self

    def request(self, *args, **kwargs):
        pass

    def settimeout(self, timeout):
        pass

    def getresponse(self):
        return self.response

    def close(self):
        pass


class ImportSafetyTests(unittest.TestCase):
    def test_rejects_paths_and_existing_symlinks(self):
        for path in ("../secret", "/etc/passwd", "assets/../x", "assets\\x", "assets//x"):
            with self.assertRaises(ValueError):
                ingest.safe_path(path)
        with patch.object(pathlib.Path, "is_symlink", return_value=True):
            with self.assertRaises(ValueError):
                ingest.safe_path("assets/test.json")

    def test_rejects_unapproved_urls_before_dns(self):
        with patch.object(ingest, "public_addresses") as dns:
            for url in ("http://raw.githubusercontent.com/x", "https://localhost/x",
                        "https://api.github.com:8443/x", "https://user@api.github.com/x"):
                with self.assertRaises(ValueError):
                    ingest.Fetcher().fetch(url)
            dns.assert_not_called()

    def test_rejects_private_dns_including_mixed_answers(self):
        private = [(2, 1, 6, "", ("127.0.0.1", 443)),
                   (2, 1, 6, "", ("8.8.8.8", 443))]
        with patch.object(ingest.socket, "getaddrinfo", return_value=private):
            with self.assertRaisesRegex(ValueError, "non-public"):
                ingest.public_addresses("api.github.com", 1)

    def test_redirect_target_is_revalidated(self):
        FakeConnection.response = FakeResponse(302, {"Location": "http://127.0.0.1/private"})
        with patch.object(ingest, "PinnedHTTPS", FakeConnection), \
                patch.object(ingest, "public_addresses", return_value=["8.8.8.8"]) as dns:
            with self.assertRaises(ValueError):
                ingest.Fetcher().fetch("https://api.github.com/test")
            self.assertEqual(dns.call_count, 1)

    def test_same_host_redirect_rechecks_dns(self):
        responses = [FakeResponse(302, {"Location": "/next"}), FakeResponse()]
        with patch.object(ingest, "PinnedHTTPS", FakeConnection), \
                patch.object(FakeConnection, "getresponse", side_effect=responses), \
                patch.object(ingest, "public_addresses", return_value=["8.8.8.8"]) as dns:
            self.assertEqual(ingest.Fetcher().fetch("https://api.github.com/test"), b"{}")
            self.assertEqual(dns.call_count, 2)

    def test_byte_cap_and_deadline(self):
        FakeConnection.response = FakeResponse(body=b"123456789")
        with patch.object(ingest, "PinnedHTTPS", FakeConnection), \
                patch.object(ingest, "public_addresses", return_value=["8.8.8.8"]), \
                patch.object(ingest, "MAX_FILE", 4):
            with self.assertRaisesRegex(ValueError, "byte cap"):
                ingest.Fetcher().fetch("https://api.github.com/test")
        fetcher = ingest.Fetcher()
        fetcher.deadline = 0
        with self.assertRaises(TimeoutError):
            fetcher.fetch("https://api.github.com/test")

    def test_rejects_external_lottie_media(self):
        value = {"v": "5", "fr": 24, "ip": 0, "op": 30, "layers": [],
                 "assets": [{"p": "image.png", "u": "https://example.com/"}]}
        with self.assertRaisesRegex(ValueError, "external media"):
            ingest.lottie_safe(value)

    def test_validation_is_entirely_offline(self):
        with patch.object(ingest.Fetcher, "fetch", side_effect=AssertionError("network used")), \
                patch.object(ingest.socket, "getaddrinfo", side_effect=AssertionError("DNS used")):
            self.assertEqual(ingest.validate(), 0)


if __name__ == "__main__":
    unittest.main()
