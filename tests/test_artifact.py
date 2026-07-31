"""Tests for artifact transport — path validation, hash verification, descriptor integrity."""
from __future__ import annotations

import hashlib
from pathlib import Path
from unittest import mock

import pytest

from codeagent.artifact import (
    ArtifactDescriptor,
    pull_artifact,
    validate_descriptor,
    verify_artifact,
)
from codeagent.transport.base import TransportError


# ── valid descriptor ───────────────────────────────────────────────────

_VALID_DESC = ArtifactDescriptor(
    artifact_id="art-001",
    relative_path="outputs/report.json",
    size=100,
    sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    media_type="application/json",
)


# ════════════════════════════════════════════════════════════════════════
# validate_descriptor
# ════════════════════════════════════════════════════════════════════════


class TestValidateDescriptor:
    def test_happy_path(self):
        validate_descriptor(_VALID_DESC)

    def test_empty_path(self):
        desc = ArtifactDescriptor(artifact_id="x", relative_path="", size=0, sha256="a" * 64)
        with pytest.raises(ValueError, match="empty"):
            validate_descriptor(desc)

    def test_whitespace_only_path(self):
        desc = ArtifactDescriptor(artifact_id="x", relative_path="   ", size=0, sha256="a" * 64)
        with pytest.raises(ValueError, match="empty"):
            validate_descriptor(desc)

    def test_absolute_path_rejected(self):
        desc = ArtifactDescriptor(artifact_id="x", relative_path="/etc/passwd", size=0, sha256="a" * 64)
        with pytest.raises(ValueError, match="absolute"):
            validate_descriptor(desc)

    def test_dotdot_rejected(self):
        desc = ArtifactDescriptor(artifact_id="x", relative_path="../secret", size=0, sha256="a" * 64)
        with pytest.raises(ValueError, match=r"\.\."):
            validate_descriptor(desc)

    def test_dotdot_midpath_rejected(self):
        desc = ArtifactDescriptor(artifact_id="x", relative_path="foo/../../secret", size=0, sha256="a" * 64)
        with pytest.raises(ValueError, match=r"\.\."):
            validate_descriptor(desc)

    def test_dotdot_suffix_normalizes_safely(self):
        """``a/b/..`` normalizes to ``a``, which is safe — no '..' survives."""
        desc = ArtifactDescriptor(artifact_id="x", relative_path="a/b/..", size=0, sha256="a" * 64)
        validate_descriptor(desc)  # should not raise

    def test_dotdot_only_rejected(self):
        desc = ArtifactDescriptor(artifact_id="x", relative_path="..", size=0, sha256="a" * 64)
        with pytest.raises(ValueError, match=r"\.\."):
            validate_descriptor(desc)

    def test_dotdot_dotdot_only_rejected(self):
        desc = ArtifactDescriptor(artifact_id="x", relative_path="../..", size=0, sha256="a" * 64)
        with pytest.raises(ValueError, match=r"\.\."):
            validate_descriptor(desc)

    def test_hidden_dotdot_in_component_allowed(self):
        """``..bar`` is not ``..`` — allowed."""
        desc = ArtifactDescriptor(artifact_id="x", relative_path="foo/..bar/baz", size=0, sha256="a" * 64)
        validate_descriptor(desc)  # "..bar" is not ".."

    def test_normal_dot_components_allowed(self):
        desc = ArtifactDescriptor(artifact_id="x", relative_path="./foo/./bar", size=0, sha256="a" * 64)
        validate_descriptor(desc)

    def test_simple_filename_ok(self):
        desc = ArtifactDescriptor(artifact_id="x", relative_path="file.txt", size=0, sha256="a" * 64)
        validate_descriptor(desc)

    def test_nested_path_ok(self):
        desc = ArtifactDescriptor(artifact_id="x", relative_path="a/b/c/d.txt", size=0, sha256="a" * 64)
        validate_descriptor(desc)


# ════════════════════════════════════════════════════════════════════════
# verify_artifact
# ════════════════════════════════════════════════════════════════════════


class TestVerifyArtifact:
    @pytest.fixture
    def tmp_file(self, tmp_path: Path) -> Path:
        p = tmp_path / "test.bin"
        p.write_bytes(b"hello world")
        return p

    def test_match(self, tmp_file: Path):
        sha = hashlib.sha256(b"hello world").hexdigest()
        assert verify_artifact(tmp_file, sha, 11) is True

    def test_size_mismatch(self, tmp_file: Path):
        sha = hashlib.sha256(b"hello world").hexdigest()
        with pytest.raises(ValueError, match="size mismatch"):
            verify_artifact(tmp_file, sha, 999)

    def test_hash_mismatch(self, tmp_file: Path):
        with pytest.raises(ValueError, match="sha256 mismatch"):
            verify_artifact(tmp_file, "a" * 64, 11)

    def test_not_a_file(self, tmp_path: Path):
        with pytest.raises(ValueError, match="not a file"):
            verify_artifact(tmp_path / "missing.txt", "a" * 64, 0)

    def test_empty_file(self, tmp_path: Path):
        p = tmp_path / "empty.bin"
        p.write_bytes(b"")
        sha = hashlib.sha256(b"").hexdigest()
        assert verify_artifact(p, sha, 0) is True


# ════════════════════════════════════════════════════════════════════════
# ArtifactDescriptor
# ════════════════════════════════════════════════════════════════════════


class TestArtifactDescriptor:
    def test_construction(self):
        d = ArtifactDescriptor(
            artifact_id="art-1",
            relative_path="out/report.json",
            size=42,
            sha256="a" * 64,
            media_type="application/json",
        )
        assert d.artifact_id == "art-1"
        assert d.relative_path == "out/report.json"
        assert d.size == 42
        assert d.sha256 == "a" * 64
        assert d.media_type == "application/json"

    def test_default_media_type(self):
        d = ArtifactDescriptor(
            artifact_id="art-2",
            relative_path="file.bin",
            size=0,
            sha256="a" * 64,
        )
        assert d.media_type == "application/octet-stream"

    def test_frozen(self):
        d = _VALID_DESC
        with pytest.raises(Exception):  # dataclasses.FrozenInstanceError
            d.size = 999  # type: ignore[misc]

    def test_equality(self):
        a = ArtifactDescriptor(artifact_id="x", relative_path="f", size=1, sha256="h")
        b = ArtifactDescriptor(artifact_id="x", relative_path="f", size=1, sha256="h")
        assert a == b

    def test_inequality(self):
        a = ArtifactDescriptor(artifact_id="x", relative_path="f", size=1, sha256="h")
        b = ArtifactDescriptor(artifact_id="y", relative_path="f", size=1, sha256="h")
        assert a != b


# ════════════════════════════════════════════════════════════════════════
# pull_artifact — error cases (no real SSH needed)
# ════════════════════════════════════════════════════════════════════════


class TestPullArtifactErrors:
    def test_no_control_master(self, tmp_path: Path):
        """pull_artifact raises TransportError when socket doesn't exist."""
        dest = tmp_path / "out.json"
        with pytest.raises(TransportError, match="ControlMaster"):
            pull_artifact(
                host_alias="nonexistent-host-xyz123",
                remote_root="/tmp/artifacts",
                desc=_VALID_DESC,
                dest=dest,
            )

    def test_invalid_descriptor_rejected_before_ssh(self, tmp_path: Path):
        """Path validation runs before any SSH connection."""
        bad_desc = ArtifactDescriptor(
            artifact_id="x",
            relative_path="../secret",
            size=0,
            sha256="a" * 64,
        )
        dest = tmp_path / "out.json"
        with pytest.raises(ValueError, match=r"\.\."):
            pull_artifact(
                host_alias="any-host",
                remote_root="/tmp",
                desc=bad_desc,
                dest=dest,
            )


# ════════════════════════════════════════════════════════════════════════
# CLI integration smoke tests (in-process, like test_cli.py)
# ════════════════════════════════════════════════════════════════════════


class TestArtifactCLI:
    """Verify the artifact subcommand is wired into the CLI parser."""

    def test_artifact_help(self, capsys):
        """``codeagent artifact --help`` lists subcommands."""
        from codeagent.cli import main

        with pytest.raises(SystemExit):
            main(["artifact", "--help"])
        out = capsys.readouterr().out
        assert "pull" in out
        assert "verify" in out

    def test_pull_help(self, capsys):
        """``codeagent artifact pull --help`` shows required args."""
        from codeagent.cli import main

        with pytest.raises(SystemExit):
            main(["artifact", "pull", "--help"])
        out = capsys.readouterr().out
        assert "--host" in out
        assert "--artifact-id" in out
        assert "--dest" in out

    def test_verify_happy_path(self, capsys, tmp_path: Path):
        """``artifact verify`` on a real file returns 0."""
        from codeagent.cli import main

        p = tmp_path / "test.bin"
        data = b"hello artifact verify"
        p.write_bytes(data)
        sha = hashlib.sha256(data).hexdigest()

        rc = main(["artifact", "verify",
                    "--file", str(p),
                    "--sha256", sha,
                    "--size", str(len(data))])
        assert rc == 0
        out = capsys.readouterr().out
        assert "ok" in out

    def test_verify_hash_mismatch(self, capsys, tmp_path: Path):
        """``artifact verify`` with wrong hash returns 1."""
        from codeagent.cli import main

        p = tmp_path / "test.bin"
        p.write_bytes(b"hello")
        bad_sha = "a" * 64

        rc = main(["artifact", "verify",
                    "--file", str(p),
                    "--sha256", bad_sha,
                    "--size", "5"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "sha256 mismatch" in err
