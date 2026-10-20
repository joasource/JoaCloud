import hashlib
import io
import sys
from pathlib import Path

import pytest
import requests_mock

import cli
import core
from providers import apple


def _write_csv(path: Path, header: list[str], rows: list[list[str]]) -> None:
    lines = [",".join(header)]
    lines += [",".join(row) for row in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class _FakeStdin:
    """Stand-in pra sys.stdin em testes: controla isatty()/readline() sem
    depender de um terminal real (io.StringIO nao permite mockar isatty)."""

    def __init__(self, text: str = "", tty: bool = False):
        self._buf = io.StringIO(text)
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty

    def readline(self) -> str:
        return self._buf.readline()


# --------------------------------------------------------------------- argparse


def test_build_parser_run_defaults():
    parser = cli.build_parser()
    args = parser.parse_args(["run", "--csv", "a.csv", "--output-dir", "out"])
    assert args.download is True
    assert args.verify is True
    assert args.decrypt is True
    assert args.workers == 4
    assert args.only is None
    assert args.exclude is None
    assert args.pattern is None
    assert args.yes is False
    assert args.menu is False
    assert args.quiet is False
    assert args.no_color is False


def test_build_parser_run_boolean_optional_flags():
    parser = cli.build_parser()
    args = parser.parse_args(["run", "--no-download", "--no-verify", "--no-decrypt"])
    assert args.download is False
    assert args.verify is False
    assert args.decrypt is False


def test_build_parser_run_passphrase_mutually_exclusive():
    parser = cli.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["run", "--passphrase-env", "X", "--passphrase-stdin"])


def test_build_parser_list_minimal():
    parser = cli.build_parser()
    args = parser.parse_args(["list", "--csv", "a.csv"])
    assert args.csv == Path("a.csv")
    assert args.output_dir is None


def test_build_parser_delete_only_is_optional_at_parse_time():
    parser = cli.build_parser()
    args = parser.parse_args(["delete", "--csv", "a.csv", "--output-dir", "out"])
    # --only nao e obrigatorio pro argparse: falta dele forca o menu interativo
    # em runtime (checklist comecando vazio), pra nunca apagar tudo por engano.
    assert args.only is None


def test_build_parser_report_defaults():
    parser = cli.build_parser()
    args = parser.parse_args(["report"])
    assert args.format == "md"
    assert args.portal is False
    assert args.email is False
    assert args.original is False
    assert args.processada is False
    assert args.responsavel is None  # nunca pre-preenchido


def test_main_no_command_prints_help_and_exits_fatal(capsys):
    code = cli.main([])
    assert code == cli.EXIT_FATAL
    captured = capsys.readouterr()
    assert "usage" in captured.out.lower()


# ----------------------------------------------------------------- selecao


def _entries():
    return [
        core.FileEntry(file_name="a.txt", file_link="http://x/a", expected_hash=None),
        core.FileEntry(file_name="b.txt.gpg", file_link="http://x/b", expected_hash=None),
        core.FileEntry(file_name="c.pdf", file_link="http://x/c", expected_hash=None),
    ]


def test_select_entries_default_is_all():
    result = cli._select_entries(_entries(), only=None, exclude=None, pattern=None)
    assert [e.file_name for e in result] == ["a.txt", "b.txt.gpg", "c.pdf"]


def test_select_entries_only():
    result = cli._select_entries(_entries(), only="a.txt,c.pdf", exclude=None, pattern=None)
    assert [e.file_name for e in result] == ["a.txt", "c.pdf"]


def test_select_entries_exclude():
    result = cli._select_entries(_entries(), only=None, exclude="b.txt.gpg", pattern=None)
    assert [e.file_name for e in result] == ["a.txt", "c.pdf"]


def test_select_entries_pattern():
    result = cli._select_entries(_entries(), only=None, exclude=None, pattern="*.gpg")
    assert [e.file_name for e in result] == ["b.txt.gpg"]


def test_select_entries_combined_filters():
    result = cli._select_entries(
        _entries(), only="a.txt,b.txt.gpg", exclude="b.txt.gpg", pattern="*.txt"
    )
    assert [e.file_name for e in result] == ["a.txt"]


# ------------------------------------------------------------------- senha


class _PassArgs:
    def __init__(self, passphrase_env=None, passphrase_stdin=False):
        self.passphrase_env = passphrase_env
        self.passphrase_stdin = passphrase_stdin


def test_resolve_passphrase_from_env(monkeypatch):
    monkeypatch.setenv("JK_TEST_PASS", "segredo")
    args = _PassArgs(passphrase_env="JK_TEST_PASS")
    assert cli._resolve_passphrase(args) == "segredo"


def test_resolve_passphrase_env_missing_raises(monkeypatch):
    monkeypatch.delenv("JK_TEST_PASS_MISSING", raising=False)
    args = _PassArgs(passphrase_env="JK_TEST_PASS_MISSING")
    with pytest.raises(core.PipelineError):
        cli._resolve_passphrase(args)


def test_resolve_passphrase_from_stdin(monkeypatch):
    monkeypatch.setattr(sys, "stdin", _FakeStdin("minha-senha\n"))
    args = _PassArgs(passphrase_stdin=True)
    assert cli._resolve_passphrase(args) == "minha-senha"


def test_resolve_passphrase_stdin_empty_raises(monkeypatch):
    monkeypatch.setattr(sys, "stdin", _FakeStdin(""))
    args = _PassArgs(passphrase_stdin=True)
    with pytest.raises(core.PipelineError):
        cli._resolve_passphrase(args)


def test_resolve_passphrase_prefers_env_over_stdin(monkeypatch):
    monkeypatch.setenv("JK_TEST_PASS2", "do-env")
    args = _PassArgs(passphrase_env="JK_TEST_PASS2", passphrase_stdin=True)
    assert cli._resolve_passphrase(args) == "do-env"


def test_resolve_passphrase_no_source_no_tty_raises(monkeypatch):
    monkeypatch.setattr(sys, "stdin", _FakeStdin("", tty=False))
    args = _PassArgs()
    with pytest.raises(core.PipelineError):
        cli._resolve_passphrase(args)


def test_resolve_passphrase_prompts_when_tty(monkeypatch):
    monkeypatch.setattr(sys, "stdin", _FakeStdin("", tty=True))
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": "digitada-no-prompt")
    args = _PassArgs()
    assert cli._resolve_passphrase(args) == "digitada-no-prompt"


# ------------------------------------------------------- run: codigos de saida


def test_cmd_run_success_exit_ok(tmp_path):
    content = b"conteudo de teste"
    sha = hashlib.sha256(content).hexdigest()
    csv_path = tmp_path / "in.csv"
    _write_csv(
        csv_path, ["File_Name", "File_Link", "GPG_SHA256"],
        [["a.txt", "https://example.com/a.txt", sha]],
    )
    output_dir = tmp_path / "out"

    with requests_mock.Mocker() as m:
        m.get("https://example.com/a.txt", content=content)
        code = cli.main(
            [
                "run", "--csv", str(csv_path), "--output-dir", str(output_dir),
                "--no-decrypt", "--yes", "--quiet",
            ]
        )

    assert code == cli.EXIT_OK
    assert (output_dir / "a.txt").read_bytes() == content


def test_cmd_run_hash_mismatch_exit_pending(tmp_path):
    csv_path = tmp_path / "in.csv"
    _write_csv(
        csv_path, ["File_Name", "File_Link", "GPG_SHA256"],
        [["a.txt", "https://example.com/a.txt", "0" * 64]],
    )
    output_dir = tmp_path / "out"

    with requests_mock.Mocker() as m:
        m.get("https://example.com/a.txt", content=b"outro conteudo")
        code = cli.main(
            [
                "run", "--csv", str(csv_path), "--output-dir", str(output_dir),
                "--no-decrypt", "--yes", "--quiet",
            ]
        )

    assert code == cli.EXIT_PENDING


def test_cmd_run_missing_csv_exit_fatal(tmp_path):
    code = cli.main(
        [
            "run", "--csv", str(tmp_path / "nao-existe.csv"), "--output-dir", str(tmp_path / "out"),
            "--yes", "--quiet",
        ]
    )
    assert code == cli.EXIT_FATAL


def test_cmd_run_decrypt_without_passphrase_source_exit_fatal(tmp_path, monkeypatch):
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    (output_dir / "secreto.txt.gpg").write_bytes(b"nao importa, nao chega a ler")
    csv_path = tmp_path / "in.csv"
    _write_csv(
        csv_path, ["File_Name", "File_Link", "GPG_SHA256"],
        [["secreto.txt.gpg", "https://example.com/x", ""]],
    )

    monkeypatch.setattr(sys, "stdin", _FakeStdin("", tty=False))
    monkeypatch.setattr(apple, "check_gpg_available", lambda: True)

    code = cli.main(
        [
            "run", "--csv", str(csv_path), "--output-dir", str(output_dir),
            "--no-download", "--no-verify", "--decrypt", "--yes", "--quiet",
        ]
    )
    assert code == cli.EXIT_FATAL


def test_cmd_run_no_steps_selected_exit_fatal(tmp_path):
    csv_path = tmp_path / "in.csv"
    _write_csv(csv_path, ["File_Name", "File_Link", "GPG_SHA256"], [["a.txt", "https://example.com/a.txt", ""]])
    code = cli.main(
        [
            "run", "--csv", str(csv_path), "--output-dir", str(tmp_path / "out"),
            "--no-download", "--no-verify", "--no-decrypt", "--yes", "--quiet",
        ]
    )
    assert code == cli.EXIT_FATAL


# ------------------------------------------------------------------- list/delete


def test_cmd_list_exit_ok(tmp_path, capsys):
    csv_path = tmp_path / "in.csv"
    _write_csv(csv_path, ["File_Name", "File_Link", "GPG_SHA256"], [["a.txt", "https://example.com/a", ""]])
    code = cli.main(["list", "--csv", str(csv_path)])
    assert code == cli.EXIT_OK
    assert "a.txt" in capsys.readouterr().out


def test_cmd_delete_removes_selected_files(tmp_path):
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    (output_dir / "a.txt").write_bytes(b"conteudo")
    csv_path = tmp_path / "in.csv"
    _write_csv(csv_path, ["File_Name", "File_Link", "GPG_SHA256"], [["a.txt", "https://example.com/a", ""]])

    code = cli.main(
        ["delete", "--csv", str(csv_path), "--output-dir", str(output_dir), "--only", "a.txt", "--yes"]
    )

    assert code == cli.EXIT_OK
    assert not (output_dir / "a.txt").exists()


# --------------------------------------------------------------------- report


def test_cmd_report_only_filters_hash_rows(tmp_path, capsys):
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    (output_dir / "a.txt").write_bytes(b"conteudo a")
    (output_dir / "b.txt").write_bytes(b"conteudo b")
    csv_path = tmp_path / "in.csv"
    _write_csv(
        csv_path, ["File_Name", "File_Link", "GPG_SHA256"],
        [
            ["a.txt", "https://example.com/a", ""],
            ["b.txt", "https://example.com/b", ""],
        ],
    )

    code = cli.main(
        [
            "report", "--csv", str(csv_path), "--output-dir", str(output_dir),
            "--only", "a.txt", "--format", "md",
        ]
    )

    out = capsys.readouterr().out
    assert code == cli.EXIT_OK
    assert "a.txt" in out
    assert "b.txt" not in out
