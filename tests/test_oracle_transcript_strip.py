"""Tests for codeagent.scripts.oracle_transcript_strip.

Covers every public entry point (strip_oracle_session, strip_for_manifest,
main) plus all private helpers with normal, empty, and malformed inputs,
and real file I/O via tmp_path.
"""
from __future__ import annotations

import json
import os
import runpy
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

import codeagent.scripts.oracle_transcript_strip as mod


@pytest.fixture
def sessions_root(tmp_path: Path, monkeypatch) -> Path:
    """Point the module's session search root at a temp dir."""
    root = tmp_path / "root"
    monkeypatch.setattr(mod, "_SESSIONS_ROOT", root)
    return root


def _write_jsonl(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _mixed_lines() -> list[str]:
    """A JSONL body exercising every keep/drop rule (no terminal marker)."""
    return [
        json.dumps({"type": "title", "title": "t"}),
        json.dumps({"type": "session", "id": "s1"}),
        json.dumps({"type": "message", "message": {"role": "user", "content": "hi"}}),
        json.dumps({"type": "message", "message": {"role": "assistant", "content": "ok"}}),
        json.dumps({"type": "message", "message": {"role": "toolResult", "content": "r"}}),
        json.dumps({"type": "message", "message": {"role": "developer", "content": "sys"}}),
        json.dumps({"type": "message"}),  # missing message key
        json.dumps({"type": "custom", "customType": "tool_execution_start", "payload": {}}),
        json.dumps({"type": "custom", "customType": "session_exit"}),
        json.dumps({"type": "toolResult", "content": "x"}),
        json.dumps({"type": "custom"}),  # custom without customType
        "this is not json",  # decode error
        "",  # blank line — skipped, not counted as kept or dropped
    ]


def _mixed_kept_lines() -> list[str]:
    """The lines _filter_session_jsonl should keep from _mixed_lines()."""
    return _mixed_lines()[:5] + [_mixed_lines()[7]]


def _set_mtime(path: Path, mtime: float) -> None:
    os.utime(path, (mtime, mtime))


# ── _locate_session_dir ───────────────────────────────────────────────


class TestLocateSessionDir:
    def test_session_dir_arg_returns_dir_when_jsonl_exists(self, tmp_path: Path):
        d = tmp_path / "d"
        d.mkdir()
        (d / "abc_s1.jsonl").write_text("x\n")
        assert mod._locate_session_dir("s1", session_dir=str(d)) == d

    def test_session_dir_arg_nonexistent_dir_returns_none(self, tmp_path: Path):
        assert mod._locate_session_dir("s1", session_dir=str(tmp_path / "nope")) is None

    def test_session_dir_arg_empty_dir_returns_none(self, tmp_path: Path):
        d = tmp_path / "d"
        d.mkdir()
        assert mod._locate_session_dir("s1", session_dir=str(d)) is None

    def test_session_dir_arg_mismatched_sid_returns_none(self, tmp_path: Path):
        d = tmp_path / "d"
        d.mkdir()
        (d / "abc_s2.jsonl").write_text("x\n")
        assert mod._locate_session_dir("s1", session_dir=str(d)) is None

    def test_default_root_missing_returns_none(self, sessions_root: Path):
        assert mod._locate_session_dir("s1") is None

    def test_default_root_no_match_returns_none(self, sessions_root: Path):
        sessions_root.mkdir()
        (sessions_root / "unrelated.jsonl").write_text("x\n")
        assert mod._locate_session_dir("s1") is None

    def test_default_root_finds_newest_parent(self, sessions_root: Path):
        sessions_root.mkdir()
        a = sessions_root / "a"
        b = sessions_root / "b"
        c = sessions_root / "c"
        for d in (a, b, c):
            d.mkdir()
        (a / "aaa_s1.jsonl").write_text("x\n")
        (b / "bbb_s1.jsonl").write_text("x\n")
        (c / "ccc_s1.jsonl").write_text("x\n")
        (sessions_root / "other" / "x_s2.jsonl").parent.mkdir()
        (sessions_root / "other" / "x_s2.jsonl").write_text("x\n")
        # Explicit mtimes so the comparison is deterministic regardless of
        # os.walk order: b is the newest (its parent dir mtime beats every
        # other candidate file's mtime).
        _set_mtime(a / "aaa_s1.jsonl", 1000)
        _set_mtime(a, 900)
        _set_mtime(b / "bbb_s1.jsonl", 2000)
        _set_mtime(b, 1500)
        _set_mtime(c / "ccc_s1.jsonl", 100)
        _set_mtime(c, 50)
        assert mod._locate_session_dir("s1") == b


# ── _locate_session_jsonl ─────────────────────────────────────────────


class TestLocateSessionJsonl:
    def test_returns_newest_matching_jsonl(self, tmp_path: Path):
        d = tmp_path / "d"
        d.mkdir()
        old = d / "a_s1.jsonl"
        new = d / "b_s1.jsonl"
        old.write_text("x\n")
        new.write_text("x\n")
        _set_mtime(old, 1000)
        _set_mtime(new, 2000)
        assert mod._locate_session_jsonl("s1", d) == new

    def test_no_match_returns_none(self, tmp_path: Path):
        d = tmp_path / "d"
        d.mkdir()
        assert mod._locate_session_jsonl("s1", d) is None


# ── _is_session_terminal ──────────────────────────────────────────────


class TestIsSessionTerminal:
    def test_empty_file_is_terminal(self, tmp_path: Path):
        p = tmp_path / "e.jsonl"
        p.write_text("")
        assert mod._is_session_terminal(p) is True

    def test_terminal_marker_last_line_true(self, tmp_path: Path):
        p = tmp_path / "t.jsonl"
        _write_jsonl(p, [
            json.dumps({"type": "message", "message": {"role": "assistant", "content": "x"}}),
            json.dumps({"type": "custom", "customType": "session_exit"}),
        ])
        assert mod._is_session_terminal(p) is True

    def test_non_terminal_last_line_false(self, tmp_path: Path):
        p = tmp_path / "n.jsonl"
        _write_jsonl(p, [
            json.dumps({"type": "custom", "customType": "session_exit"}),
            json.dumps({"type": "message", "message": {"role": "assistant", "content": "x"}}),
        ])
        assert mod._is_session_terminal(p) is False

    def test_unknown_custom_type_false(self, tmp_path: Path):
        p = tmp_path / "u.jsonl"
        _write_jsonl(p, [json.dumps({"type": "custom", "customType": "tool_execution_start"})])
        assert mod._is_session_terminal(p) is False

    def test_invalid_json_false(self, tmp_path: Path):
        p = tmp_path / "bad.jsonl"
        p.write_text("not json\n")
        assert mod._is_session_terminal(p) is False

    def test_missing_file_false(self, tmp_path: Path):
        assert mod._is_session_terminal(tmp_path / "missing.jsonl") is False

    def test_line_longer_than_tail_window_false(self, tmp_path: Path):
        # The tail window is 4096 bytes; a final line longer than that is
        # truncated mid-JSON and fails to parse -> treated as non-terminal.
        p = tmp_path / "huge.jsonl"
        p.write_text(json.dumps({"type": "message", "message": {"role": "assistant",
                                                                "content": "a" * 8000}}))
        assert mod._is_session_terminal(p) is False

    def test_all_blank_tail_false(self, tmp_path: Path):
        # Non-empty file whose content is only newlines: no line parses.
        p = tmp_path / "blanks.jsonl"
        p.write_text("\n\n\n")
        assert mod._is_session_terminal(p) is False


# ── _delete_bash_original ─────────────────────────────────────────────


class TestDeleteBashOriginal:
    def test_removes_in_dir_and_subdirs(self, tmp_path: Path):
        sd = tmp_path / "sess"
        sd.mkdir()
        top = sd / "1.bash-original.log"
        top.write_text("B" * 100)
        sub = sd / "sub_s1"
        sub.mkdir()
        inner = sub / "2.bash-original.log"
        inner.write_text("C" * 50)
        # Non-matching files are untouched.
        keep = sd / "1.bash.log"
        keep.write_text("k")
        removed = mod._delete_bash_original(sd, "s1")
        assert set(removed) == {str(top), str(inner)}
        assert not top.exists() and not inner.exists()
        assert keep.exists()

    def test_no_matches_returns_empty(self, tmp_path: Path):
        sd = tmp_path / "sess"
        sd.mkdir()
        (sd / "a.log").write_text("x")
        assert mod._delete_bash_original(sd, "s1") == []

    def test_unlink_error_warns_and_continues(self, tmp_path: Path, capsys):
        sd = tmp_path / "sess"
        sd.mkdir()
        # A directory matching the glob: unlink() raises IsADirectoryError.
        bad = sd / "d.bash-original.log"
        bad.mkdir()
        good = sd / "g.bash-original.log"
        good.write_text("x")
        removed = mod._delete_bash_original(sd, "s1")
        assert removed == [str(good)]
        assert bad.exists()
        assert "delete bash-original failed" in capsys.readouterr().err


# ── _truncate_sidecar_logs ────────────────────────────────────────────


class TestTruncateSidecarLogs:
    def test_truncates_large_keeps_small_and_is_idempotent(self, tmp_path: Path):
        sd = tmp_path / "sess"
        sd.mkdir()
        bash = sd / "1.bash.log"
        bash.write_text("b" * 5000)
        small = sd / "2.eval.log"
        small.write_text("e" * 30)  # already ≤ head_bytes -> untouched
        readf = sd / "3.read.1"
        readf.write_text("r" * 3000)
        head = 2048
        truncated = mod._truncate_sidecar_logs(sd, "s1", head_bytes=head)
        assert set(truncated) == {f"{bash} ({5000}→{head})", f"{readf} ({3000}→{head})"}
        assert bash.read_bytes() == b"b" * head
        assert readf.read_bytes() == b"r" * head
        assert small.read_bytes() == b"e" * 30
        # Second run: nothing to do.
        assert mod._truncate_sidecar_logs(sd, "s1", head_bytes=head) == []

    def test_truncates_in_sid_subdir(self, tmp_path: Path):
        sd = tmp_path / "sess"
        sd.mkdir()
        sub = sd / "sub_s1"
        sub.mkdir()
        f = sub / "x.bash.log"
        f.write_text("y" * 100)
        truncated = mod._truncate_sidecar_logs(sd, "s1", head_bytes=10)
        assert truncated == [f"{f} ({100}→10)"]
        assert f.read_bytes() == b"y" * 10

    def test_no_matches_returns_empty(self, tmp_path: Path):
        sd = tmp_path / "sess"
        sd.mkdir()
        (sd / "a.txt").write_text("x")
        assert mod._truncate_sidecar_logs(sd, "s1") == []

    def test_error_warns_and_truncates_valid_files(self, tmp_path: Path, capsys):
        sd = tmp_path / "sess"
        sd.mkdir()
        bad = sd / "d.bash.log"
        bad.mkdir()  # opening a dir for read raises IsADirectoryError
        good = sd / "g.bash.log"
        good.write_text("g" * 50)
        head = 1
        truncated = mod._truncate_sidecar_logs(sd, "s1", head_bytes=head)
        assert truncated == [f"{good} ({50}→{head})"]
        assert good.read_bytes() == b"g"
        assert "truncate failed" in capsys.readouterr().err


# ── _filter_session_jsonl ─────────────────────────────────────────────


class TestFilterSessionJsonl:
    def test_mixed_lines_keeps_and_drops_correctly(self, tmp_path: Path):
        p = tmp_path / "s1.jsonl"
        _write_jsonl(p, _mixed_lines())
        original = p.read_bytes()
        report = mod._filter_session_jsonl(p)
        assert report["kept"] == 6
        assert report["dropped"] == 6
        assert report["rewritten"] is True
        assert report["original_bytes"] == len(original)
        expected_content = "\n".join(_mixed_kept_lines()) + "\n"
        assert p.read_bytes() == expected_content.encode("utf-8")
        assert report["filtered_bytes"] == len(expected_content.encode("utf-8"))

    def test_dry_run_does_not_rewrite(self, tmp_path: Path):
        p = tmp_path / "s1.jsonl"
        _write_jsonl(p, _mixed_lines())
        original = p.read_bytes()
        report = mod._filter_session_jsonl(p, dry_run=True)
        assert report["kept"] == 6
        assert report["dropped"] == 6
        assert report["rewritten"] is False
        assert p.read_bytes() == original

    def test_missing_file_zero_report(self, tmp_path: Path):
        assert mod._filter_session_jsonl(tmp_path / "nope.jsonl") == {
            "kept": 0, "dropped": 0, "rewritten": False,
            "original_bytes": 0, "filtered_bytes": 0,
        }

    def test_empty_file_no_rewrite(self, tmp_path: Path):
        p = tmp_path / "e.jsonl"
        p.write_text("")
        report = mod._filter_session_jsonl(p)
        assert report == {"kept": 0, "dropped": 0, "rewritten": False,
                          "original_bytes": 0, "filtered_bytes": 0}

    def test_already_filtered_not_rewritten(self, tmp_path: Path):
        p = tmp_path / "s1.jsonl"
        _write_jsonl(p, _mixed_kept_lines())
        report = mod._filter_session_jsonl(p)
        assert report["dropped"] == 0
        assert report["rewritten"] is False

    def test_all_dropped_becomes_empty_file(self, tmp_path: Path):
        p = tmp_path / "s1.jsonl"
        _write_jsonl(p, ["not json", json.dumps({"type": "toolResult"})])
        report = mod._filter_session_jsonl(p)
        assert report["kept"] == 0
        assert report["dropped"] == 2
        assert report["rewritten"] is True
        assert p.read_bytes() == b""
        assert report["filtered_bytes"] == 0


# ── _delete_advisor_files ─────────────────────────────────────────────


class TestDeleteAdvisorFiles:
    def test_removes_advisor_files_and_dirs(self, tmp_path: Path):
        sd = tmp_path / "sess"
        sd.mkdir()
        f1 = sd / "__advisor.jsonl"
        f1.write_text("a\n")
        d1 = sd / "__advisor"
        d1.mkdir()
        (d1 / "inner").write_text("i")
        sub = sd / "sub_s1"
        sub.mkdir()
        f2 = sub / "__advisor.log"
        f2.write_text("a2\n")
        removed = mod._delete_advisor_files(sd, "s1")
        assert set(removed) == {str(f1), str(d1), str(f2)}
        assert not f1.exists() and not d1.exists() and not f2.exists()

    def test_rmtree_error_warns(self, tmp_path: Path, monkeypatch, capsys):
        sd = tmp_path / "sess"
        sd.mkdir()
        d1 = sd / "__advisor"
        d1.mkdir()

        def _boom(*args, **kwargs):
            raise OSError("boom")

        monkeypatch.setattr(shutil, "rmtree", _boom)
        assert mod._delete_advisor_files(sd, "s1") == []
        assert d1.exists()
        assert "delete advisor file failed" in capsys.readouterr().err


# ── strip_oracle_session ──────────────────────────────────────────────


def _full_session(root: Path, sid: str, *, terminal: bool = True) -> Path:
    """Create a session with every artifact type; return the session dir."""
    sd = root / "sess"
    sd.mkdir(parents=True, exist_ok=True)
    (sd / "1.bash-original.log").write_text("B" * 100)
    (sd / "2.bash.log").write_text("b" * 5000)
    (sd / "2.eval.log").write_text("e" * 30)
    (sd / "2.read.1").write_text("r" * 3000)
    sub = sd / f"sub_{sid}"
    sub.mkdir()
    (sub / "3.bash-original.log").write_text("C" * 50)
    (sub / "3.eval.log").write_text("E" * 4000)
    (sd / "__advisor.jsonl").write_text("ad\n")
    (sd / "__advisor").mkdir()
    lines = _mixed_lines()
    if terminal:
        lines = lines + [json.dumps({"type": "custom", "customType": "session_exit"})]
    _write_jsonl(sd / f"abc_{sid}.jsonl", lines)
    return sd


class TestStripOracleSession:
    def test_empty_sid(self):
        report = mod.strip_oracle_session("")
        assert report["error"] == "empty_sid"
        assert report["sid"] == ""

    def test_session_not_found(self, sessions_root: Path):
        report = mod.strip_oracle_session("nosuch")
        assert report["error"] == "session_not_found"
        assert report["session_dir"] is None

    def test_live_guard_refuses_without_terminal(self, sessions_root: Path):
        sd = _full_session(sessions_root, "s1", terminal=False)
        report = mod.strip_oracle_session("s1")
        assert report["live_guard"] == "refused"
        assert report["error"] == "session_looks_live"
        assert "hint" in report
        # Nothing was touched.
        assert report["removed"] == []
        assert report["truncated"] == []
        assert report["jsonl"] is None
        assert (sd / "1.bash-original.log").exists()

    def test_live_guard_passed_and_full_strip(self, sessions_root: Path):
        sd = _full_session(sessions_root, "s1", terminal=True)
        head = 2048
        report = mod.strip_oracle_session("s1")
        assert report["sid"] == "s1"
        assert report["session_dir"] == str(sd)
        assert report["live_guard"] == "passed"
        assert "error" not in report
        assert set(report["removed"]) == {
            str(sd / "1.bash-original.log"), str(sd / "sub_s1" / "3.bash-original.log"),
        }
        assert set(report["truncated"]) == {
            f"{sd / '2.bash.log'} ({5000}→{head})",
            f"{sd / '2.read.1'} ({3000}→{head})",
            f"{sd / 'sub_s1' / '3.eval.log'} ({4000}→{head})",
        }
        assert report["jsonl"]["kept"] == 6
        assert report["jsonl"]["dropped"] == 7
        assert report["jsonl"]["rewritten"] is True
        assert set(report["advisors_removed"]) == {
            str(sd / "__advisor.jsonl"), str(sd / "__advisor"),
        }
        # Side effects on disk.
        assert not (sd / "1.bash-original.log").exists()
        assert (sd / "2.bash.log").read_bytes() == b"b" * head
        assert (sd / "2.eval.log").read_bytes() == b"e" * 30
        assert not (sd / "__advisor.jsonl").exists()
        assert not (sd / "__advisor").exists()

    def test_idempotent_second_run(self, sessions_root: Path):
        _full_session(sessions_root, "s1", terminal=True)
        mod.strip_oracle_session("s1")
        # The first run strips the terminal marker, so the guard refuses on
        # re-run; force=True models the release path.
        report = mod.strip_oracle_session("s1", force=True)
        assert report["removed"] == []
        assert report["truncated"] == []
        assert report["jsonl"]["dropped"] == 0
        assert report["jsonl"]["rewritten"] is False
        assert report["advisors_removed"] == []

    def test_force_skips_live_guard(self, sessions_root: Path):
        sd = _full_session(sessions_root, "s1", terminal=False)
        report = mod.strip_oracle_session("s1", force=True)
        assert report["live_guard"] == "forced"
        assert "error" not in report
        assert not (sd / "1.bash-original.log").exists()

    def test_force_without_jsonl(self, sessions_root: Path, monkeypatch):
        # jsonl_path is None only via a race between locating and listing;
        # simulate it by making the listing return nothing.
        sd = sessions_root / "sess"
        sd.mkdir(parents=True)
        _write_jsonl(sd / "abc_s1.jsonl", [json.dumps({"type": "message"})])
        (sd / "1.bash-original.log").write_text("x")
        monkeypatch.setattr(mod, "_locate_session_jsonl", lambda *a, **k: None)
        report = mod.strip_oracle_session("s1", force=True)
        assert report["live_guard"] == "forced"
        assert report["jsonl"]["note"] == "jsonl_not_found"
        assert not (sd / "1.bash-original.log").exists()

    def test_no_jsonl_and_no_force_guard_skipped(self, sessions_root: Path, monkeypatch):
        sd = sessions_root / "sess"
        sd.mkdir(parents=True)
        _write_jsonl(sd / "abc_s1.jsonl", [json.dumps({"type": "message"})])
        monkeypatch.setattr(mod, "_locate_session_jsonl", lambda *a, **k: None)
        report = mod.strip_oracle_session("s1")
        assert report["live_guard"] == "skipped"
        assert report["jsonl"]["note"] == "jsonl_not_found"

    def test_dry_run_only_guards_jsonl(self, sessions_root: Path):
        # Real behavior: dry_run prevents the jsonl rewrite but NOT deletion
        # or truncation of sidecar logs.
        sd = _full_session(sessions_root, "s1", terminal=True)
        jsonl = sd / "abc_s1.jsonl"
        before = jsonl.read_bytes()
        report = mod.strip_oracle_session("s1", dry_run=True)
        assert report["jsonl"]["rewritten"] is False
        assert jsonl.read_bytes() == before
        assert report["removed"] != []
        assert report["truncated"] != []

    def test_head_bytes_passthrough(self, sessions_root: Path):
        sd = _full_session(sessions_root, "s1", terminal=True)
        report = mod.strip_oracle_session("s1", head_bytes=5)
        assert f"{sd / '2.bash.log'} ({5000}→5)" in report["truncated"]
        assert (sd / "2.bash.log").read_bytes() == b"b" * 5

    def test_session_dir_param_restricts_search(self, tmp_path: Path):
        # session_dir pointing at a live-looking session refuses without force.
        sd = tmp_path / "direct"
        sd.mkdir()
        _write_jsonl(sd / f"abc_s1.jsonl", [json.dumps({"type": "message"})])
        report = mod.strip_oracle_session("s1", session_dir=str(sd))
        assert report["error"] == "session_looks_live"
        report = mod.strip_oracle_session("s1", session_dir=str(sd), force=True)
        assert "error" not in report
        assert report["session_dir"] == str(sd)


# ── strip_for_manifest ────────────────────────────────────────────────


class TestStripForManifest:
    def test_extracts_sid_and_session_dir(self, tmp_path: Path):
        sd = tmp_path / "sess"
        sd.mkdir()
        _write_jsonl(sd / f"abc_s9.jsonl",
                     [json.dumps({"type": "custom", "customType": "session_exit"})])
        manifest = SimpleNamespace(
            backend_session_id="s9",
            omp_session_path=str(sd / "session.jsonl"),
        )
        report = mod.strip_for_manifest(manifest)
        assert report["sid"] == "s9"
        assert report["session_dir"] == str(sd)
        assert report["live_guard"] == "forced"  # force defaults True
        assert "error" not in report

    def test_missing_attrs_returns_empty_sid(self):
        report = mod.strip_for_manifest(SimpleNamespace())
        assert report["error"] == "empty_sid"

    def test_bad_path_session_not_found(self, tmp_path: Path):
        manifest = SimpleNamespace(
            backend_session_id="s9",
            omp_session_path=str(tmp_path / "nope" / "session.jsonl"),
        )
        report = mod.strip_for_manifest(manifest)
        assert report["error"] == "session_not_found"

    def test_force_false_refuses_live(self, tmp_path: Path):
        sd = tmp_path / "sess"
        sd.mkdir()
        _write_jsonl(sd / f"abc_s9.jsonl", [json.dumps({"type": "message"})])
        manifest = SimpleNamespace(
            backend_session_id="s9",
            omp_session_path=str(sd / "session.jsonl"),
        )
        report = mod.strip_for_manifest(manifest, force=False)
        assert report["error"] == "session_looks_live"


# ── main (CLI) ────────────────────────────────────────────────────────


class TestMain:
    def test_success_returns_zero_and_prints_report(self, sessions_root: Path, capsys):
        _full_session(sessions_root, "s10", terminal=True)
        code = mod.main(["s10"])
        assert code == 0
        report = json.loads(capsys.readouterr().out)
        assert report["sid"] == "s10"
        assert report["live_guard"] == "passed"

    def test_error_returns_one(self, sessions_root: Path, capsys):
        code = mod.main(["nosuch"])
        assert code == 1
        report = json.loads(capsys.readouterr().out)
        assert report["error"] == "session_not_found"

    def test_flags_passthrough(self, sessions_root: Path, capsys):
        _full_session(sessions_root, "s10", terminal=False)
        code = mod.main(["s10", "--head-bytes", "5", "--force", "--dry-run"])
        assert code == 0
        report = json.loads(capsys.readouterr().out)
        assert report["live_guard"] == "forced"
        assert report["jsonl"]["rewritten"] is False
        assert f"({5000}→5)" in report["truncated"][0]

    def test_module_main_block(self, tmp_path: Path, monkeypatch):
        # Execute the file as __main__ (covers `raise SystemExit(main())`).
        monkeypatch.setenv("HOME", str(tmp_path))
        root = tmp_path / ".omp" / "agent" / "sessions"
        sd = root / "sess"
        sd.mkdir(parents=True)
        _write_jsonl(sd / "abc_s11.jsonl",
                     [json.dumps({"type": "custom", "customType": "session_exit"})])
        monkeypatch.setattr("sys.argv", ["prog", "s11"])
        script = str(Path(__file__).resolve().parent.parent
                     / "src" / "codeagent" / "scripts" / "oracle_transcript_strip.py")
        with pytest.raises(SystemExit) as exc:
            runpy.run_path(script, run_name="__main__")
        assert exc.value.code == 0
