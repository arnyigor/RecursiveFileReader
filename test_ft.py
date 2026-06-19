#!/usr/bin/env python3
"""Tests for ft.py — FT Transfer core logic, CLI, web handler."""

import base64
import hashlib
import io
import json
import tarfile
import tempfile
import threading
import time
import urllib.request
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import patch

import pytest

import ft


# =========================================================================
# Fixtures
# =========================================================================


@pytest.fixture
def tmp_path_obj():
    """Provides a temporary directory that is cleaned up after the test."""
    with tempfile.TemporaryDirectory(prefix="ft_test_") as d:
        yield Path(d)


@pytest.fixture
def sample_file(tmp_path_obj):
    """Creates a small text file for testing."""
    p = tmp_path_obj / "hello.txt"
    p.write_text("Hello, FT Transfer!", encoding="utf-8")
    return p


@pytest.fixture
def sample_dir(tmp_path_obj):
    """Creates a small directory tree for testing."""
    d = tmp_path_obj / "mydir"
    d.mkdir()
    (d / "a.txt").write_text("file a", encoding="utf-8")
    sub = d / "sub"
    sub.mkdir()
    (sub / "b.txt").write_text("file b", encoding="utf-8")
    return d


@pytest.fixture
def sample_binary_file(tmp_path_obj):
    """Creates a small binary file."""
    p = tmp_path_obj / "data.bin"
    p.write_bytes(bytes(range(256)))
    return p


# =========================================================================
# 1. sha256_bytes
# =========================================================================


class TestSha256Bytes:
    def test_known_value(self):
        result = ft.sha256_bytes(b"hello")
        assert len(result) == 64
        assert result == hashlib.sha256(b"hello").hexdigest()

    def test_empty(self):
        result = ft.sha256_bytes(b"")
        assert result == hashlib.sha256(b"").hexdigest()

    def test_deterministic(self):
        assert ft.sha256_bytes(b"abc") == ft.sha256_bytes(b"abc")

    def test_different_inputs(self):
        assert ft.sha256_bytes(b"a") != ft.sha256_bytes(b"b")


# =========================================================================
# 2. normalize_text
# =========================================================================


class TestNormalizeText:
    def test_strips_spaces(self):
        assert ft.normalize_text("a b c") == "abc"

    def test_strips_newlines(self):
        assert ft.normalize_text("a\nb\nc") == "abc"

    def test_strips_tabs(self):
        assert ft.normalize_text("a\tb") == "ab"

    def test_strips_mixed(self):
        assert ft.normalize_text("  a \n\t b  \n c  ") == "abc"

    def test_empty(self):
        assert ft.normalize_text("") == ""

    def test_no_whitespace(self):
        assert ft.normalize_text("abc") == "abc"


# =========================================================================
# 3. Token format: build_token / decode_token roundtrip
# =========================================================================


class TestTokenRoundtrip:
    def test_single_file_roundtrip(self, sample_file):
        token = ft.build_token(sample_file)
        meta, archive_bytes = ft.decode_token(token)
        assert meta["name"] == "hello.txt"
        assert meta["kind"] == "file"
        assert meta["archive"] == "tar.xz"
        assert meta["version"] == 1
        assert len(archive_bytes) > 0

    def test_directory_roundtrip(self, sample_dir):
        token = ft.build_token(sample_dir)
        meta, archive_bytes = ft.decode_token(token)
        assert meta["name"] == "mydir"
        assert meta["kind"] == "dir"
        assert len(archive_bytes) > 0

    def test_token_format(self, sample_file):
        token = ft.build_token(sample_file)
        parts = token.split(".")
        assert len(parts) == 3
        assert parts[0] == "FTPKG1"
        # Middle part is base64url
        assert all(
            c in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
            for c in parts[1]
        )
        # Last part is hex hash
        assert len(parts[2]) == 64
        assert all(c in "0123456789abcdef" for c in parts[2])

    def test_hash_integrity(self, sample_file):
        token = ft.build_token(sample_file)
        parts = token.split(".")
        payload = base64.urlsafe_b64decode(
            parts[1] + "=" * ((4 - len(parts[1]) % 4) % 4)
        )
        actual_hash = ft.sha256_bytes(payload)
        assert actual_hash == parts[2]

    def test_decode_rejects_corrupted_hash(self, sample_file):
        token = ft.build_token(sample_file)
        # Corrupt the hash
        parts = token.split(".")
        corrupted_hash = "0" * 64
        corrupted_token = "{}.{}.{}".format(parts[0], parts[1], corrupted_hash)
        with pytest.raises(ValueError, match="Контрольная сумма"):
            ft.decode_token(corrupted_token)

    def test_decode_rejects_wrong_prefix(self, sample_file):
        token = ft.build_token(sample_file)
        parts = token.split(".")
        bad_token = "WRONG.{}.{}".format(parts[1], parts[2])
        with pytest.raises(ValueError, match="Неизвестный формат"):
            ft.decode_token(bad_token)

    def test_decode_rejects_truncated(self, sample_file):
        token = ft.build_token(sample_file)
        parts = token.split(".")
        truncated = "{}.{}.".format(parts[0], parts[1][:50])
        with pytest.raises(ValueError):
            ft.decode_token(truncated)

    def test_decode_rejects_too_few_parts(self):
        with pytest.raises(ValueError, match="Неверный формат"):
            ft.decode_token("FTPKG1.onlytwo")


# =========================================================================
# 4. extract_token_from_text
# =========================================================================


class TestExtractTokenFromText:
    def test_extracts_from_plain_text(self, sample_file):
        token = ft.build_token(sample_file)
        wrapped = "Here is the token:\n{}\nEnd of token.".format(token)
        extracted = ft.extract_token_from_text(wrapped)
        assert extracted == token

    def test_extracts_with_extra_whitespace(self, sample_file):
        token = ft.build_token(sample_file)
        # Simulate paste into rich text editor (add consistent spaces/newlines)
        noisy = ""
        for i, ch in enumerate(token):
            noisy += ch
            if i % 3 == 0:
                noisy += " "
            if i % 7 == 0:
                noisy += "\n"
        # normalize_text strips ALL whitespace, so this should reconstruct
        extracted = ft.extract_token_from_text(noisy)
        assert extracted == token

    def test_raises_when_no_token(self):
        with pytest.raises(ValueError, match="не найден"):
            ft.extract_token_from_text("no token here, just plain text")

    def test_raises_on_empty(self):
        with pytest.raises(ValueError):
            ft.extract_token_from_text("")

    def test_find_token_among_noise(self, sample_file):
        token = ft.build_token(sample_file)
        noise = "%%%<script>alert(1)</script>%%%   {}   %%%<b>bold</b>%%%".format(token)
        extracted = ft.extract_token_from_text(noise)
        assert extracted == token


# =========================================================================
# 5. Full pack → extract → unpack roundtrip
# =========================================================================


class TestFullRoundtrip:
    def test_file_pack_unpack(self, sample_file, tmp_path_obj):
        token = ft.build_token(sample_file)
        meta, archive_bytes = ft.decode_token(token)
        out_dir = tmp_path_obj / "restored"
        ft.extract_tar_xz(archive_bytes, out_dir)
        restored = out_dir / meta["name"]
        assert restored.is_file()
        assert restored.read_text(encoding="utf-8") == "Hello, FT Transfer!"

    def test_dir_pack_unpack(self, sample_dir, tmp_path_obj):
        token = ft.build_token(sample_dir)
        meta, archive_bytes = ft.decode_token(token)
        out_dir = tmp_path_obj / "restored"
        ft.extract_tar_xz(archive_bytes, out_dir)
        restored = out_dir / meta["name"]
        assert restored.is_dir()
        assert (restored / "a.txt").read_text(encoding="utf-8") == "file a"
        assert (restored / "sub" / "b.txt").read_text(encoding="utf-8") == "file b"

    def test_binary_file_roundtrip(self, sample_binary_file, tmp_path_obj):
        token = ft.build_token(sample_binary_file)
        meta, archive_bytes = ft.decode_token(token)
        out_dir = tmp_path_obj / "restored"
        ft.extract_tar_xz(archive_bytes, out_dir)
        restored = out_dir / meta["name"]
        assert restored.read_bytes() == bytes(range(256))

    def test_empty_file_roundtrip(self, tmp_path_obj):
        empty = tmp_path_obj / "empty.txt"
        empty.write_text("", encoding="utf-8")
        token = ft.build_token(empty)
        meta, archive_bytes = ft.decode_token(token)
        out_dir = tmp_path_obj / "restored"
        ft.extract_tar_xz(archive_bytes, out_dir)
        restored = out_dir / meta["name"]
        assert restored.read_text(encoding="utf-8") == ""

    def test_large_text_roundtrip(self, tmp_path_obj):
        large = tmp_path_obj / "large.txt"
        content = "x" * 100_000
        large.write_text(content, encoding="utf-8")
        token = ft.build_token(large)
        meta, archive_bytes = ft.decode_token(token)
        out_dir = tmp_path_obj / "restored"
        ft.extract_tar_xz(archive_bytes, out_dir)
        restored = out_dir / meta["name"]
        assert restored.read_text(encoding="utf-8") == content

    def test_unicode_filename_roundtrip(self, tmp_path_obj):
        f = tmp_path_obj / "файл_тест.txt"
        f.write_text("юникод", encoding="utf-8")
        token = ft.build_token(f)
        meta, archive_bytes = ft.decode_token(token)
        out_dir = tmp_path_obj / "restored"
        ft.extract_tar_xz(archive_bytes, out_dir)
        restored = out_dir / meta["name"]
        assert restored.read_text(encoding="utf-8") == "юникод"

    def test_token_survives_text_normalization(self, sample_file):
        """Token should be extractable even after whitespace injection."""
        token = ft.build_token(sample_file)
        # Simulate paste into rich text editor (add random spaces/newlines)
        noisy = ""
        for ch in token:
            noisy += ch
            if hash(ch) % 3 == 0:
                noisy += " "
            if hash(ch) % 5 == 0:
                noisy += "\n"
        extracted = ft.extract_token_from_text(noisy)
        meta, archive_bytes = ft.decode_token(extracted)
        assert meta["name"] == "hello.txt"


# =========================================================================
# 6. Tar safety
# =========================================================================


class TestTarSafety:
    def test_symlink_rejected(self, tmp_path_obj):
        """Symlinks inside tar should be rejected."""
        with tarfile.open(tmp_path_obj / "evil.tar.xz", "w:xz") as tar:
            link_info = tarfile.TarInfo(name="evil_link")
            link_info.type = tarfile.SYMTYPE
            link_info.linkname = "/etc/passwd"
            tar.addfile(link_info)

        archive_bytes = (tmp_path_obj / "evil.tar.xz").read_bytes()
        out = tmp_path_obj / "out"
        with pytest.raises(ValueError, match="ссылк"):
            ft.extract_tar_xz(archive_bytes, out)

    def test_hardlink_rejected(self, tmp_path_obj):
        """Hardlinks inside tar should be rejected."""
        with tarfile.open(tmp_path_obj / "evil2.tar.xz", "w:xz") as tar:
            link_info = tarfile.TarInfo(name="evil_hlink")
            link_info.type = tarfile.LNKTYPE
            link_info.linkname = "/etc/passwd"
            tar.addfile(link_info)

        archive_bytes = (tmp_path_obj / "evil2.tar.xz").read_bytes()
        out = tmp_path_obj / "out"
        with pytest.raises(ValueError, match="ссылк"):
            ft.extract_tar_xz(archive_bytes, out)

    def test_path_traversal_rejected(self, tmp_path_obj):
        """Paths with .. in tar should be rejected."""
        with tarfile.open(tmp_path_obj / "traversal.tar.xz", "w:xz") as tar:
            info = tarfile.TarInfo(name="../../etc/passwd")
            info.size = 0
            tar.addfile(info)

        archive_bytes = (tmp_path_obj / "traversal.tar.xz").read_bytes()
        out = tmp_path_obj / "out"
        with pytest.raises(ValueError, match="небезопасный путь"):
            ft.extract_tar_xz(archive_bytes, out)


# =========================================================================
# 7. _is_path_inside
# =========================================================================


class TestIsPathInside:
    def test_inside(self, tmp_path_obj):
        base = tmp_path_obj / "base"
        base.mkdir()
        target = base / "child" / "file.txt"
        assert ft._is_path_inside(base, target) is True

    def test_outside(self, tmp_path_obj):
        base = tmp_path_obj / "base"
        base.mkdir()
        target = tmp_path_obj / "other" / "file.txt"
        assert ft._is_path_inside(base, target) is False

    def test_same_dir(self, tmp_path_obj):
        base = tmp_path_obj / "base"
        base.mkdir()
        assert ft._is_path_inside(base, base) is True


# =========================================================================
# 8. read_text_preview
# =========================================================================


class TestReadTextPreview:
    def test_utf8_text(self, tmp_path_obj):
        p = tmp_path_obj / "utf8.txt"
        p.write_text("Привет мир!", encoding="utf-8")
        is_text, content, truncated, size = ft.read_text_preview(p)
        assert is_text is True
        assert "Привет мир!" in content
        assert truncated is False
        assert size > 0

    def test_binary_file(self, tmp_path_obj):
        p = tmp_path_obj / "bin.dat"
        p.write_bytes(bytes(range(256)))
        is_text, content, truncated, size = ft.read_text_preview(p)
        assert is_text is False
        assert content == ""

    def test_empty_file(self, tmp_path_obj):
        p = tmp_path_obj / "empty.txt"
        p.write_bytes(b"")
        is_text, content, truncated, size = ft.read_text_preview(p)
        assert size == 0
        # Empty file: no bytes < 32 found, decode succeeds
        assert is_text is True

    def test_truncation(self, tmp_path_obj):
        p = tmp_path_obj / "big.txt"
        p.write_text("A" * 1000, encoding="utf-8")
        is_text, content, truncated, size = ft.read_text_preview(p, max_bytes=100)
        assert truncated is True
        assert len(content) <= 100

    def test_cp1251_fallback(self, tmp_path_obj):
        p = tmp_path_obj / "cp1251.txt"
        p.write_bytes("Привет".encode("cp1251"))
        is_text, content, truncated, size = ft.read_text_preview(p)
        assert is_text is True
        assert "Привет" in content

    def test_utf8_bom(self, tmp_path_obj):
        p = tmp_path_obj / "bom.txt"
        p.write_bytes(b"\xef\xbb\xbf" + "BOM text".encode("utf-8"))
        is_text, content, truncated, size = ft.read_text_preview(p)
        assert is_text is True
        assert "BOM text" in content


# =========================================================================
# 9. save_text_file / read_text_file
# =========================================================================


class TestTextFileIO:
    def test_roundtrip(self, tmp_path_obj):
        p = tmp_path_obj / "test.txt"
        ft.save_text_file(p, "hello world")
        assert ft.read_text_file(p) == "hello world"

    def test_unicode(self, tmp_path_obj):
        p = tmp_path_obj / "unicode.txt"
        ft.save_text_file(p, "Тест юникода: 日本語 🎉")
        assert ft.read_text_file(p) == "Тест юникода: 日本語 🎉"


# =========================================================================
# 10. CLI commands: pack_command / unpack_command
# =========================================================================


class TestPackCommand:
    def test_pack_to_file(self, sample_file, tmp_path_obj, capsys):
        out = tmp_path_obj / "output.ft.txt"
        rc = ft.pack_command(str(sample_file), str(out))
        assert rc == 0
        assert out.exists()
        content = out.read_text(encoding="utf-8")
        assert content.startswith("FTPKG1.")

    def test_pack_nonexistent(self, tmp_path_obj):
        rc = ft.pack_command("/nonexistent/path", None)
        assert rc == 1


class TestUnpackCommand:
    def test_unpack_from_file(self, sample_file, tmp_path_obj, capsys):
        # Pack first
        token = ft.build_token(sample_file)
        token_file = tmp_path_obj / "input.ft.txt"
        ft.save_text_file(token_file, token)

        out_dir = tmp_path_obj / "restored"
        rc = ft.unpack_command(str(token_file), str(out_dir))
        assert rc == 0
        assert (out_dir / "hello.txt").exists()
        assert (out_dir / "hello.txt").read_text(
            encoding="utf-8"
        ) == "Hello, FT Transfer!"

    def test_unpack_nonexistent(self):
        rc = ft.unpack_command("/nonexistent/file.txt", None)
        assert rc == 1

    def test_unpack_invalid_token(self, tmp_path_obj):
        bad_file = tmp_path_obj / "bad.ft.txt"
        ft.save_text_file(bad_file, "not a token at all")
        rc = ft.unpack_command(str(bad_file), str(tmp_path_obj / "out"))
        assert rc == 1


# =========================================================================
# 11. Web server handler (integration test)
# =========================================================================


class TestWebHandler:
    """Tests the web API endpoints by starting a server on a random port."""

    @pytest.fixture
    def web_server(self):
        """Start the web server on a random available port and return (base_url, shutdown_fn)."""
        import socket

        # Find a free port
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]

        server_thread = threading.Thread(
            target=lambda: ft.launch_web(port),
            daemon=True,
        )
        server_thread.start()
        time.sleep(0.5)  # Give server time to start

        base_url = "http://127.0.0.1:{}".format(port)
        yield base_url

    def test_get_index(self, web_server):
        """GET / should return HTML with FT Transfer."""
        conn = HTTPConnection("127.0.0.1", int(web_server.split(":")[-1]), timeout=5)
        conn.request("GET", "/")
        resp = conn.getresponse()
        assert resp.status == 200
        body = resp.read().decode("utf-8")
        assert "FT Transfer" in body
        assert "autoMode" in body  # Auto-mode checkbox present
        conn.close()

    def test_get_404(self, web_server):
        """GET /unknown should return 404."""
        port = int(web_server.split(":")[-1])
        conn = HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/unknown")
        resp = conn.getresponse()
        assert resp.status == 404
        conn.close()

    def test_post_empty_body(self, web_server):
        """POST with no body should return 400."""
        port = int(web_server.split(":")[-1])
        conn = HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("POST", "/api/pack", body="")
        resp = conn.getresponse()
        assert resp.status == 400
        conn.close()

    def test_api_pack_text(self, web_server):
        """POST /api/pack with text content should return a token."""
        port = int(web_server.split(":")[-1])
        payload = json.dumps(
            {
                "is_folder": False,
                "is_text": True,
                "filename": "test.txt",
                "text_content": "Hello from API test!",
            }
        )
        conn = HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request(
            "POST",
            "/api/pack",
            body=payload.encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        assert resp.status == 200
        data = json.loads(resp.read().decode("utf-8"))
        assert "token" in data
        assert data["token"].startswith("FTPKG1.")
        assert data["filename"] == "test.txt"
        conn.close()

    def test_api_pack_single_file(self, web_server):
        """POST /api/pack with a single file (base64) should return a token."""
        port = int(web_server.split(":")[-1])
        file_content = b"binary content test"
        payload = json.dumps(
            {
                "is_folder": False,
                "is_text": False,
                "filename": "data.bin",
                "content_b64": base64.b64encode(file_content).decode("ascii"),
            }
        )
        conn = HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request(
            "POST",
            "/api/pack",
            body=payload.encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        assert resp.status == 200
        data = json.loads(resp.read().decode("utf-8"))
        assert data["token"].startswith("FTPKG1.")
        assert data["filename"] == "data.bin"
        conn.close()

    def test_api_pack_path_injection_blocked(self, web_server):
        """POST /api/pack with path traversal in filename should be sanitized."""
        port = int(web_server.split(":")[-1])
        payload = json.dumps(
            {
                "is_folder": False,
                "is_text": True,
                "filename": "../../etc/evil.txt",
                "text_content": "evil",
            }
        )
        conn = HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request(
            "POST",
            "/api/pack",
            body=payload.encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        data = json.loads(resp.read().decode("utf-8"))
        # Should succeed but filename should be sanitized to just "evil.txt"
        assert "token" in data
        assert data["filename"] == "evil.txt"
        conn.close()

    def test_api_pack_folder_path_injection_blocked(self, web_server):
        """POST /api/pack folder with path traversal in folder_name should be sanitized."""
        port = int(web_server.split(":")[-1])
        payload = json.dumps(
            {"is_folder": True, "folder_name": "../../etc/evil_folder", "files": []}
        )
        conn = HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request(
            "POST",
            "/api/pack",
            body=payload.encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        data = json.loads(resp.read().decode("utf-8"))
        assert "token" in data
        assert data["filename"] == "evil_folder"
        conn.close()

    def test_api_unpack_roundtrip(self, web_server):
        """Pack text via API, then unpack the token."""
        port = int(web_server.split(":")[-1])

        # Step 1: Pack
        pack_payload = json.dumps(
            {
                "is_folder": False,
                "is_text": True,
                "filename": "roundtrip.txt",
                "text_content": "Roundtrip content!",
            }
        )
        conn = HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request(
            "POST",
            "/api/pack",
            body=pack_payload.encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        pack_data = json.loads(conn.getresponse().read().decode("utf-8"))
        token = pack_data["token"]
        conn.close()

        # Step 2: Unpack
        unpack_payload = json.dumps({"token": token})
        conn = HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request(
            "POST",
            "/api/unpack",
            body=unpack_payload.encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        assert resp.status == 200
        unpack_data = json.loads(resp.read().decode("utf-8"))
        assert unpack_data["status"] == "ok"
        assert unpack_data["is_text"] is True
        assert "Roundtrip content!" in unpack_data["text_content"]
        conn.close()

    def test_api_unpack_empty_token(self, web_server):
        """POST /api/unpack with empty token should return 400."""
        port = int(web_server.split(":")[-1])
        payload = json.dumps({"token": ""})
        conn = HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request(
            "POST",
            "/api/unpack",
            body=payload.encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        assert resp.status == 400
        conn.close()

    def test_api_unpack_invalid_token(self, web_server):
        """POST /api/unpack with invalid token should return 400."""
        port = int(web_server.split(":")[-1])
        payload = json.dumps({"token": "not a real token"})
        conn = HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request(
            "POST",
            "/api/unpack",
            body=payload.encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        assert resp.status == 400
        conn.close()

    def test_api_pack_unknown_endpoint(self, web_server):
        """POST /api/unknown should return 404."""
        port = int(web_server.split(":")[-1])
        conn = HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request(
            "POST",
            "/api/unknown",
            body=b"{}",
            headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        assert resp.status == 404
        conn.close()

    def test_api_pack_folder(self, web_server):
        """POST /api/pack with a folder (multiple files)."""
        port = int(web_server.split(":")[-1])
        files = [
            {"path": "mydir/file1.txt", "b64": base64.b64encode(b"content1").decode()},
            {
                "path": "mydir/sub/file2.txt",
                "b64": base64.b64encode(b"content2").decode(),
            },
        ]
        payload = json.dumps(
            {"is_folder": True, "folder_name": "mydir", "files": files}
        )
        conn = HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request(
            "POST",
            "/api/pack",
            body=payload.encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        data = json.loads(resp.read().decode("utf-8"))
        assert "token" in data
        assert data["filename"] == "mydir"
        conn.close()


# =========================================================================
# 12. Web auto-mode: HTML structure checks
# =========================================================================


class TestWebAutoModeStructure:
    """Verify the HTML template contains expected auto-mode elements."""

    def _get_html(self):
        """Extract HTML_TEMPLATE from launch_web by reading the source."""
        import inspect

        source = inspect.getsource(ft.launch_web)
        # Skip the docstring, find the HTML_TEMPLATE assignment
        idx = source.index('HTML_TEMPLATE = """')
        start = idx + len('HTML_TEMPLATE = """')
        end = source.index('"""', start)
        return source[start:end]

    def test_auto_mode_checkbox_present(self):
        html = self._get_html()
        assert 'id="autoMode"' in html
        assert "checked" in html

    def test_auto_pack_function_present(self):
        html = self._get_html()
        assert "async function autoPack()" in html

    def test_auto_unpack_function_present(self):
        html = self._get_html()
        assert "async function autoUnpack()" in html

    def test_debounce_function_present(self):
        html = self._get_html()
        assert "function _debounce" in html

    def test_event_listeners_present(self):
        html = self._get_html()
        assert "addEventListener('input'" in html

    def test_copy_no_alert(self):
        """copyTextElement should NOT contain alert()."""
        html = self._get_html()
        # The function should exist
        assert "copyTextElement" in html
        # But should not call alert
        assert "alert(" not in html.split("copyTextElement")[1].split("}")[0]

    def test_auto_unpack_checks_ftpkg(self):
        """autoUnpack should filter by FTPKG1 prefix."""
        html = self._get_html()
        assert "FTPKG1." in html

    def test_download_function_present(self):
        html = self._get_html()
        assert "downloadOutToken" in html


# =========================================================================
# 13. Edge cases
# =========================================================================


class TestEdgeCases:
    def test_unicode_content_roundtrip(self, tmp_path_obj):
        """File with mixed unicode content."""
        p = tmp_path_obj / "mixed.txt"
        content = "Line 1: Привет\nLine 2: 🎉\nLine 3: 日本語\nLine 4: ñ ü ö ä"
        p.write_text(content, encoding="utf-8")
        token = ft.build_token(p)
        meta, archive_bytes = ft.decode_token(token)
        out = tmp_path_obj / "restored"
        ft.extract_tar_xz(archive_bytes, out)
        restored = out / meta["name"]
        assert restored.read_text(encoding="utf-8") == content

    def test_deeply_nested_dirs(self, tmp_path_obj):
        """Nested directory structure (limited for Windows path length)."""
        base = tmp_path_obj / "d"
        base.mkdir()
        current = base
        depth = 4
        for i in range(depth):
            current = current / str(i)
            current.mkdir()
        (current / "leaf.txt").write_text("deep", encoding="utf-8")

        token = ft.build_token(base)
        meta, archive_bytes = ft.decode_token(token)
        out = tmp_path_obj / "r"
        ft.extract_tar_xz(archive_bytes, out)

        restored = out / meta["name"]
        for i in range(depth):
            restored = restored / str(i)
        assert (restored / "leaf.txt").read_text(encoding="utf-8") == "deep"

    def test_many_files_in_dir(self, tmp_path_obj):
        """Directory with many files."""
        d = tmp_path_obj / "many"
        d.mkdir()
        for i in range(100):
            (d / f"file_{i:03d}.txt").write_text(f"content {i}", encoding="utf-8")

        token = ft.build_token(d)
        meta, archive_bytes = ft.decode_token(token)
        out = tmp_path_obj / "restored"
        ft.extract_tar_xz(archive_bytes, out)

        restored = out / meta["name"]
        files = list(restored.iterdir())
        assert len(files) == 100

    def test_special_chars_in_content(self, tmp_path_obj):
        """Content with special characters."""
        p = tmp_path_obj / "special.txt"
        content = '<script>alert("xss")</script>\n{"json": true}\nSELECT * FROM users;'
        p.write_text(content, encoding="utf-8")
        token = ft.build_token(p)
        meta, archive_bytes = ft.decode_token(token)
        out = tmp_path_obj / "restored"
        ft.extract_tar_xz(archive_bytes, out)
        restored = out / meta["name"]
        assert restored.read_text(encoding="utf-8") == content

    def test_concurrent_pack_unpack(self, tmp_path_obj):
        """Multiple threads packing/unpacking simultaneously."""
        files = []
        for i in range(5):
            p = tmp_path_obj / f"concurrent_{i}.txt"
            p.write_text(f"thread {i}" * 1000, encoding="utf-8")
            files.append(p)

        tokens = []
        errors = []

        def pack_one(f):
            try:
                tokens.append(ft.build_token(f))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=pack_one, args=(f,)) for f in files]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        assert len(tokens) == 5

        # Unpack all concurrently
        results = []

        def unpack_one(token_str, idx):
            try:
                meta, archive_bytes = ft.decode_token(token_str)
                out = tmp_path_obj / f"restored_{idx}"
                ft.extract_tar_xz(archive_bytes, out)
                results.append(out / meta["name"])
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=unpack_one, args=(tok, i))
            for i, tok in enumerate(tokens)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        assert len(results) == 5
        for r in results:
            assert r.is_file()

    def test_token_deterministic_for_same_content(self, tmp_path_obj):
        """Same file should produce same token (deterministic tar.xz)."""
        p = tmp_path_obj / "det.txt"
        p.write_text("deterministic", encoding="utf-8")
        t1 = ft.build_token(p)
        t2 = ft.build_token(p)
        assert t1 == t2

    def test_different_files_produce_different_tokens(self, tmp_path_obj):
        """Different files should produce different tokens."""
        p1 = tmp_path_obj / "a.txt"
        p2 = tmp_path_obj / "b.txt"
        p1.write_text("aaa", encoding="utf-8")
        p2.write_text("bbb", encoding="utf-8")
        assert ft.build_token(p1) != ft.build_token(p2)

    def test_web_server_html_has_no_alert_in_copy(self):
        """Web HTML should not use alert() for copy feedback."""
        import inspect

        source = inspect.getsource(ft.launch_web)
        # Find the copyTextElement function and check it doesn't contain alert
        idx = source.index("copyTextElement")
        # Get 500 chars around it
        snippet = source[idx : idx + 600]
        # Should not have alert in the copy function
        assert "alert(" not in snippet
