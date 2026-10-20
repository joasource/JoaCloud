import hashlib
import shutil
import subprocess
import threading
from pathlib import Path

import pytest
import requests_mock

import core
from providers import apple


def _write_csv(path: Path, header: list[str], rows: list[list[str]]) -> None:
    lines = [",".join(header)]
    lines += [",".join(row) for row in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# --------------------------------------------------------------------- CSV


def test_load_entries_happy_path(tmp_path):
    csv_path = tmp_path / "retorno.csv"
    _write_csv(
        csv_path,
        ["File_Name", "File_Link", "GPG_SHA256"],
        [["a.txt.gpg", "https://example.com/a", "AAAA"], ["b.txt", "https://example.com/b", ""]],
    )

    entries = apple.load_entries(csv_path)

    assert [e.file_name for e in entries] == ["a.txt.gpg", "b.txt"]
    assert entries[0].expected_hash == "aaaa"
    assert entries[1].expected_hash is None
    assert entries[0].is_encrypted is True
    assert entries[1].is_encrypted is False


def test_load_entries_accepts_alternate_column_names(tmp_path):
    csv_path = tmp_path / "retorno.csv"
    _write_csv(
        csv_path,
        ["File Name", "Download_Link", "Hash"],
        [["c.txt", "https://example.com/c", "BEEF"]],
    )

    entries = apple.load_entries(csv_path)

    assert entries[0].file_name == "c.txt"
    assert entries[0].file_link == "https://example.com/c"
    assert entries[0].expected_hash == "beef"


def test_load_entries_skips_blank_rows(tmp_path):
    csv_path = tmp_path / "retorno.csv"
    _write_csv(
        csv_path,
        ["File_Name", "File_Link", "GPG_SHA256"],
        [["", "", ""], ["d.txt", "https://example.com/d", ""]],
    )

    entries = apple.load_entries(csv_path)

    assert len(entries) == 1
    assert entries[0].file_name == "d.txt"


def test_load_entries_missing_file_raises(tmp_path):
    with pytest.raises(core.PipelineError):
        apple.load_entries(tmp_path / "nao_existe.csv")


def test_load_entries_missing_columns_raises(tmp_path):
    csv_path = tmp_path / "retorno.csv"
    _write_csv(csv_path, ["Coluna_Qualquer"], [["x"]])

    with pytest.raises(core.PipelineError):
        apple.load_entries(csv_path)


def test_load_entries_no_valid_rows_raises(tmp_path):
    csv_path = tmp_path / "retorno.csv"
    _write_csv(csv_path, ["File_Name", "File_Link"], [["", ""]])

    with pytest.raises(core.PipelineError):
        apple.load_entries(csv_path)


# ------------------------------------------------------------- PipelineConfig


def test_pipeline_config_default_log_path(tmp_path):
    config = core.PipelineConfig(source=tmp_path / "x.csv", output_dir=tmp_path / "out")

    assert config.log_path == tmp_path / "out" / "joacloud_log.txt"
    assert config.unpacked_dir == tmp_path / "out" / "decriptado"


def test_pipeline_config_custom_log_path(tmp_path):
    custom_log = tmp_path / "custom.log"
    config = core.PipelineConfig(
        source=tmp_path / "x.csv", output_dir=tmp_path / "out", log_path=custom_log
    )

    assert config.log_path == custom_log


# -------------------------------------------------------------------- format_eta


def test_format_eta_minutes_seconds():
    assert core.format_eta(75) == "01:15"


def test_format_eta_hours():
    assert core.format_eta(3725) == "01:02:05"


def test_format_eta_negative_or_nan_is_zero():
    assert core.format_eta(-5) == "00:00"
    assert core.format_eta(float("nan")) == "00:00"


# --------------------------------------------------------------- ProgressTracker


def test_progress_tracker_computes_speed_after_settling():
    tracker = core.ProgressTracker()
    start = 1000.0

    estimate = tracker.update(0, 100, now=start)
    assert estimate.speed_bps == 0.0
    assert estimate.eta_seconds is None

    estimate = tracker.update(50, 100, now=start + 1.0)
    assert estimate.speed_bps == pytest.approx(50.0)
    assert estimate.eta_seconds == pytest.approx(1.0)


def test_progress_tracker_no_speed_before_settle_window():
    tracker = core.ProgressTracker()
    start = 1000.0
    estimate = tracker.update(50, 100, now=start + 0.1)
    assert estimate.speed_bps == 0.0


def test_progress_tracker_restarts_on_new_attempt():
    tracker = core.ProgressTracker()
    start = 1000.0

    tracker.update(80, 100, now=start)
    tracker.update(100, 100, now=start + 1.0)

    # nova tentativa: bytes_done cai de volta pra 0
    estimate = tracker.update(0, 100, now=start + 2.0)
    assert estimate.speed_bps == 0.0

    estimate = tracker.update(30, 100, now=start + 3.0)
    assert estimate.speed_bps == pytest.approx(30.0)


# ------------------------------------------------------------- aggregate_progress


def test_aggregate_progress_sums_known_files():
    events = {
        "a.txt": core.ProgressEvent("a.txt", bytes_done=40, bytes_total=100),
        "b.txt": core.ProgressEvent("b.txt", bytes_done=10, bytes_total=50),
    }
    result = core.aggregate_progress(events, start_time=1000.0, now=1002.0)

    assert result["bytes_done"] == 50
    assert result["bytes_known"] == 150
    assert result["speed_bps"] == pytest.approx(25.0)
    assert result["eta_seconds"] == pytest.approx((150 - 50) / 25.0)


def test_aggregate_progress_unknown_total_has_no_eta():
    events = {"a.txt": core.ProgressEvent("a.txt", bytes_done=40, bytes_total=0)}
    result = core.aggregate_progress(events, start_time=1000.0, now=1001.0)

    assert result["bytes_known"] == 0
    assert result["eta_seconds"] is None


def test_aggregate_progress_empty_events():
    result = core.aggregate_progress({}, start_time=1000.0, now=1001.0)

    assert result["bytes_done"] == 0
    assert result["speed_bps"] == 0.0
    assert result["eta_seconds"] is None


# ------------------------------------------------------------------ pipeline


def _collect_pipeline(config, entries):
    updates: list[core.FileEntry] = []
    logs: list[str] = []
    summary = core.run_pipeline(
        config, entries, on_update=lambda e: updates.append(e.status), log=logs.append
    )
    return summary, updates, logs


def test_run_pipeline_download_and_verify_ok(tmp_path):
    content = b"conteudo de teste"
    sha = hashlib.sha256(content).hexdigest()
    entry = core.FileEntry(file_name="ok.txt", file_link="https://example.com/ok.txt", expected_hash=sha)
    config = core.PipelineConfig(
        source=tmp_path / "x.csv", output_dir=tmp_path / "out", unpack=False
    )

    with requests_mock.Mocker() as m:
        m.get("https://example.com/ok.txt", content=content)
        summary, updates, _ = _collect_pipeline(config, [entry])

    assert summary.concluidos == 1
    assert summary.erros == 0
    assert (config.output_dir / "ok.txt").read_bytes() == content
    assert updates[-1] == "concluido"


def test_run_pipeline_hash_mismatch(tmp_path):
    entry = core.FileEntry(
        file_name="ruim.txt", file_link="https://example.com/ruim.txt", expected_hash="0" * 64
    )
    config = core.PipelineConfig(
        source=tmp_path / "x.csv", output_dir=tmp_path / "out", unpack=False
    )

    with requests_mock.Mocker() as m:
        m.get("https://example.com/ruim.txt", content=b"outro conteudo")
        summary, updates, _ = _collect_pipeline(config, [entry])

    assert summary.hash_invalido == 1
    assert summary.concluidos == 0
    assert updates[-1] == "hash_invalido"


def test_run_pipeline_skips_already_downloaded_file(tmp_path):
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    (output_dir / "existente.txt").write_bytes(b"ja estava aqui")
    entry = core.FileEntry(
        file_name="existente.txt", file_link="https://example.com/nao-deveria-ser-chamado",
        expected_hash=None,
    )
    config = core.PipelineConfig(source=tmp_path / "x.csv", output_dir=output_dir, unpack=False)

    with requests_mock.Mocker() as m:
        summary, updates, _ = _collect_pipeline(config, [entry])
        assert m.call_count == 0  # nao deveria baixar de novo

    assert summary.concluidos == 1
    assert updates[-1] == "concluido"


def test_run_pipeline_download_failure_marks_erro(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "RETRY_BACKOFF_SECONDS", 0)  # nao esperar entre tentativas no teste
    entry = core.FileEntry(
        file_name="falha.txt", file_link="https://example.com/falha.txt", expected_hash=None
    )
    config = core.PipelineConfig(source=tmp_path / "x.csv", output_dir=tmp_path / "out", unpack=False)

    with requests_mock.Mocker() as m:
        m.get("https://example.com/falha.txt", status_code=404)
        summary, updates, _ = _collect_pipeline(config, [entry])

    assert summary.erros == 1
    assert updates[-1] == "erro"
    assert not (config.output_dir / "falha.txt").exists()


def test_run_pipeline_requires_passphrase_when_decrypting(tmp_path):
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    (output_dir / "arquivo.gpg").write_bytes(b"nao importa, nao chega a ler")
    entry = core.FileEntry(
        file_name="arquivo.gpg", file_link="https://example.com/arquivo.gpg", expected_hash=None
    )
    config = core.PipelineConfig(
        source=tmp_path / "x.csv", output_dir=output_dir, download=False, passphrase=""
    )

    with pytest.raises(core.PipelineError):
        core.run_pipeline(config, [entry], on_update=lambda e: None, log=lambda s: None)


def test_run_pipeline_reports_download_progress(tmp_path):
    content = b"x" * (5 * 1024 * 1024)
    entry = core.FileEntry(file_name="grande.bin", file_link="https://example.com/grande.bin", expected_hash=None)
    config = core.PipelineConfig(source=tmp_path / "x.csv", output_dir=tmp_path / "out", unpack=False)

    events: list[tuple[str, int, int]] = []

    def on_progress(entry, bytes_done, bytes_total, phase="download"):
        events.append((entry.file_name, bytes_done, bytes_total))

    with requests_mock.Mocker() as m:
        m.get("https://example.com/grande.bin", content=content, headers={"Content-Length": str(len(content))})
        core.run_pipeline(
            config, [entry], on_update=lambda e: None, log=lambda s: None, on_progress=on_progress
        )

    assert events, "esperava pelo menos um evento de progresso"
    assert all(name == "grande.bin" for name, _, _ in events)
    assert events[-1][1] == len(content)
    assert events[-1][2] == len(content)


def test_hash_of_file_reports_progress(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "CHUNK_SIZE", 4)
    monkeypatch.setattr(core, "PROGRESS_THROTTLE_SECONDS", 0)
    content = b"conteudo de teste para hash com progresso"
    path = tmp_path / "arquivo.bin"
    path.write_bytes(content)

    events: list[tuple[int, int]] = []
    digest = core.hash_of_file(path, on_progress=lambda done, total: events.append((done, total)))

    assert digest == hashlib.sha256(content).hexdigest()
    assert events, "esperava pelo menos um evento de progresso"
    assert all(total == len(content) for _, total in events)
    assert events[-1][0] == len(content)


def test_hash_of_file_without_callback_still_works(tmp_path):
    content = b"sem callback nenhum"
    path = tmp_path / "arquivo.bin"
    path.write_bytes(content)

    assert core.hash_of_file(path) == hashlib.sha256(content).hexdigest()


def test_run_pipeline_caches_computed_hash_on_entry(tmp_path):
    content = b"conteudo de teste"
    sha = hashlib.sha256(content).hexdigest()
    entry = core.FileEntry(file_name="ok.txt", file_link="https://example.com/ok.txt", expected_hash=sha)
    config = core.PipelineConfig(source=tmp_path / "x.csv", output_dir=tmp_path / "out", unpack=False)

    with requests_mock.Mocker() as m:
        m.get("https://example.com/ok.txt", content=content)
        _collect_pipeline(config, [entry])

    dest = config.output_dir / "ok.txt"
    assert entry.computed_hash == sha
    assert entry.computed_hash_stat == (dest.stat().st_size, dest.stat().st_mtime)


def test_cached_file_hash_valid_when_stat_unchanged(tmp_path):
    path = tmp_path / "a.txt"
    path.write_bytes(b"conteudo")
    stat = path.stat()
    entry = core.FileEntry(
        file_name="a.txt", file_link="x", expected_hash=None,
        computed_hash="deadbeef", computed_hash_stat=(stat.st_size, stat.st_mtime),
    )

    assert core.cached_file_hash(entry, path) == "deadbeef"


def test_cached_file_hash_invalidated_when_file_changes(tmp_path):
    path = tmp_path / "a.txt"
    path.write_bytes(b"conteudo")
    stat = path.stat()
    entry = core.FileEntry(
        file_name="a.txt", file_link="x", expected_hash=None,
        computed_hash="deadbeef", computed_hash_stat=(stat.st_size, stat.st_mtime),
    )

    path.write_bytes(b"conteudo diferente, tamanho mudou")

    assert core.cached_file_hash(entry, path) is None


def test_cached_file_hash_none_when_never_computed(tmp_path):
    path = tmp_path / "a.txt"
    path.write_bytes(b"conteudo")
    entry = core.FileEntry(file_name="a.txt", file_link="x", expected_hash=None)

    assert core.cached_file_hash(entry, path) is None


def test_run_pipeline_reports_hash_progress_as_separate_phase(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "PROGRESS_THROTTLE_SECONDS", 0)
    content = b"y" * (2 * 1024 * 1024)
    sha = hashlib.sha256(content).hexdigest()
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    (output_dir / "existente.bin").write_bytes(content)
    entry = core.FileEntry(
        file_name="existente.bin", file_link="https://example.com/nao-usado", expected_hash=sha
    )
    config = core.PipelineConfig(
        source=tmp_path / "x.csv", output_dir=output_dir, download=False, unpack=False
    )

    events: list[tuple[str, int, int, str]] = []

    def on_progress(entry, bytes_done, bytes_total, phase="download"):
        events.append((entry.file_name, bytes_done, bytes_total, phase))

    summary = core.run_pipeline(
        config, [entry], on_update=lambda e: None, log=lambda s: None, on_progress=on_progress
    )

    assert summary.concluidos == 1
    hash_events = [e for e in events if e[3] == "hash"]
    assert hash_events, "esperava eventos de progresso de fase 'hash'"
    assert all(e[0] == "existente.bin" for e in hash_events)
    assert hash_events[-1][1] == len(content)
    assert hash_events[-1][2] == len(content)
    assert not any(e[3] == "download" for e in events)  # download desligado, so hash roda


def test_delete_entry_files_removes_downloaded_and_decrypted_copies(tmp_path):
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    decrypted_dir = output_dir / "decriptado"
    decrypted_dir.mkdir()

    (output_dir / "arquivo.txt.gpg").write_bytes(b"cifrado")
    (decrypted_dir / "arquivo.txt").write_bytes(b"decifrado")

    entry = core.FileEntry(file_name="arquivo.txt.gpg", file_link="https://example.com/x", expected_hash=None)

    removed = core.delete_entry_files(entry, output_dir)

    assert len(removed) == 2
    assert not (output_dir / "arquivo.txt.gpg").exists()
    assert not (decrypted_dir / "arquivo.txt").exists()


def test_delete_entry_files_returns_empty_when_nothing_on_disk(tmp_path):
    entry = core.FileEntry(file_name="fantasma.txt", file_link="https://example.com/x", expected_hash=None)

    removed = core.delete_entry_files(entry, tmp_path)

    assert removed == []


def test_run_pipeline_cancel_stops_before_download(tmp_path):
    entry = core.FileEntry(
        file_name="cancelado.txt", file_link="https://example.com/cancelado.txt", expected_hash=None
    )
    config = core.PipelineConfig(source=tmp_path / "x.csv", output_dir=tmp_path / "out", unpack=False)
    cancel_event = threading.Event()
    cancel_event.set()

    with requests_mock.Mocker() as m:
        summary = core.run_pipeline(
            config, [entry], on_update=lambda e: None, log=lambda s: None, cancel_event=cancel_event
        )
        assert m.call_count == 0

    assert summary.cancelados == 1


# -------------------------------------------------------- GPG (se disponivel)

pytestmark_gpg = pytest.mark.skipif(shutil.which("gpg") is None, reason="gpg nao encontrado no PATH")


@pytestmark_gpg
def test_run_pipeline_decrypts_with_correct_passphrase(tmp_path):
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    plaintext = b"segredo do oficio judicial"
    passphrase = "senha-de-teste-123"

    plain_path = tmp_path / "segredo.txt"
    plain_path.write_bytes(plaintext)
    encrypted_path = output_dir / "segredo.txt.gpg"
    subprocess.run(
        [
            "gpg", "--batch", "--yes", "--pinentry-mode", "loopback",
            "--passphrase-fd", "0", "--symmetric", "--cipher-algo", "AES256",
            "--output", str(encrypted_path), str(plain_path),
        ],
        input=passphrase.encode("utf-8"),
        check=True,
        capture_output=True,
    )

    entry = core.FileEntry(
        file_name="segredo.txt.gpg",
        file_link="https://example.com/nao-usado",
        expected_hash=hashlib.sha256(encrypted_path.read_bytes()).hexdigest(),
    )
    config = core.PipelineConfig(
        source=tmp_path / "x.csv", output_dir=output_dir, download=False, passphrase=passphrase
    )

    summary, updates, _ = _collect_pipeline(config, [entry])

    assert summary.concluidos == 1
    assert updates[-1] == "concluido"
    assert (config.unpacked_dir / "segredo.txt").read_bytes() == plaintext


@pytestmark_gpg
def test_run_pipeline_wrong_passphrase_marks_erro(tmp_path):
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    plain_path = tmp_path / "segredo2.txt"
    plain_path.write_bytes(b"outro segredo")
    encrypted_path = output_dir / "segredo2.txt.gpg"
    subprocess.run(
        [
            "gpg", "--batch", "--yes", "--pinentry-mode", "loopback",
            "--passphrase-fd", "0", "--symmetric", "--cipher-algo", "AES256",
            "--output", str(encrypted_path), str(plain_path),
        ],
        input=b"senha-certa",
        check=True,
        capture_output=True,
    )

    entry = core.FileEntry(
        file_name="segredo2.txt.gpg", file_link="https://example.com/nao-usado", expected_hash=None
    )
    config = core.PipelineConfig(
        source=tmp_path / "x.csv", output_dir=output_dir, download=False, passphrase="senha-errada"
    )

    summary, updates, _ = _collect_pipeline(config, [entry])

    assert summary.erros == 1
    assert updates[-1] == "erro"
