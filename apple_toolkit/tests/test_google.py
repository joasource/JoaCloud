import hashlib
import io
import threading
import zipfile
from pathlib import Path

import pytest

import cli
import core
import report
from providers import get_provider
from providers import google


# ------------------------------------------------------------------ fixtures
#
# Dados sinteticos no mesmo formato do retorno do LERS (ver docstring de
# providers/google.py) — nunca usar dados reais de caso aqui.

INNER_A = "111111111111111.2222222222222222222.AndroidDeviceConfigurationService.DeviceAndUserProfile_001.zip"
INNER_B = "333333333333333.4444444444444444444.AndroidDeviceConfigurationService.DeviceAndUserProfile_001.001.zip"
INNER_EMAIL = "fulano.de.tal@gmail.com.5555555555555555555.GoogleAccount.SubscriberInfo_001.zip"


def _inner_zip_bytes(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _write_production_zip(path: Path, inner: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in inner.items():
            zf.writestr(name, data)


def _hash_pdf_lines(hashes: dict[str, str], wrap: int = 52) -> list[str]:
    """Imita o layout do PDF do Google: cabecalho com CEP, nome e hash
    quebrados em pedacos de tamanho fixo."""
    lines = [
        "Google LLC1600 Amphitheatre",
        "ParkwMountain View, California",
        "94043",
        "Mountain View, 01/01/2026 (MM/DD/AAAA)",
        "Ref. Interna: 999999999",
        "Valores de Hash dos Arquivos Produzidos",
    ]
    for name, digest in hashes.items():
        label = name + ":"
        lines += [label[i:i + wrap] for i in range(0, len(label), wrap)]
        lines.append("SHA512-")
        lines += [digest[:80], digest[80:]]
    return lines


def _write_pdf(path: Path, lines: list[str]) -> None:
    """PDF minimo de uma pagina com uma linha de texto por item (Helvetica)."""
    def esc(text: str) -> str:
        return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    content = "BT /F1 9 Tf 40 800 Td 11 TL " + " ".join(f"({esc(line)}) Tj T*" for line in lines) + " ET"
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R "
        "/Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(content)} >>\nstream\n{content}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{body}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    path.write_bytes(bytes(out))


@pytest.fixture
def lers_folder(tmp_path):
    """Pasta como a baixada do LERS: 1 ZIP de producao com 2 ZIPs internos +
    PDF de hashes + um PDF de correspondencia qualquer."""
    folder = tmp_path / "lers"
    folder.mkdir()
    inner = {
        INNER_A: _inner_zip_bytes({
            "AndroidDeviceConfigurationService.DeviceAndUserProfile.ExportSummary.txt": b"Export Summary A",
            "Android Device Configuration Service/Device-1.html": b"<html>A</html>",
        }),
        INNER_B: _inner_zip_bytes({
            "AndroidDeviceConfigurationService.DeviceAndUserProfile.ExportSummary.txt": b"Export Summary B",
            "Android Device Configuration Service/Device-2.html": b"<html>B</html>",
        }),
    }
    _write_production_zip(folder / "12345678-20260101-1.zip", inner)
    hashes = {name: hashlib.sha512(data).hexdigest() for name, data in inner.items()}
    _write_pdf(folder / "Valores de Hash 999999999.pdf", _hash_pdf_lines(hashes))
    _write_pdf(folder / "Carta 999999999.pdf", ["Prezados,", "Segue a producao."])
    return folder, inner, hashes


def _run(folder: Path, output_dir: Path, **kwargs):
    provider = get_provider("google")
    entries = provider.load_entries(folder)
    config = core.PipelineConfig(source=folder, output_dir=output_dir, provider=provider, **kwargs)
    summary = core.run_pipeline(config, entries, on_update=lambda e: None, log=lambda m: None)
    return config, entries, summary


# ------------------------------------------------------------- parse_hash_text


def test_parse_hash_text_wrapped_names_and_hashes():
    digest_a, digest_b = "a" * 128, "b" * 128
    text = "\n".join(_hash_pdf_lines({INNER_A: digest_a, INNER_B: digest_b}))
    result = google.parse_hash_text(text, known_names={INNER_A, INNER_B})
    assert result == {INNER_A: digest_a, INNER_B: digest_b}


def test_parse_hash_text_accepts_label_without_hyphen():
    digest = "c" * 128
    text = f"{INNER_A}:\nSHA512{digest[:70]}\n{digest[70:]}"
    assert google.parse_hash_text(text, known_names={INNER_A}) == {INNER_A: digest}


def test_parse_hash_text_without_sha512_label():
    # Formato da carta com "Valores de Hash dos Arquivos Produzidos": so' o hex,
    # e o hash seguinte colado no proximo nome (que pode comecar com digito hex).
    digest_a, digest_b = "a" * 128, "b" * 128
    text = f"Valores de Hash dos Arquivos Produzidos\n{INNER_A}:\n{digest_a[:90]}\n{digest_a[90:]}{INNER_B}:\n{digest_b}"
    assert google.parse_hash_text(text, known_names={INNER_A, INNER_B}) == {INNER_A: digest_a, INNER_B: digest_b}


def test_parse_hash_text_known_name_not_glued_to_header_digits():
    # Na 2a pagina o cabecalho termina no CEP (94043) colado no nome que
    # comeca com digitos — sem os nomes conhecidos a heuristica erraria.
    digest = "d" * 128
    text = f"Google LLC\nMountain View, California\n94043\n{INNER_A}:\nSHA512-{digest}"
    assert google.parse_hash_text(text, known_names={INNER_A}) == {INNER_A: digest}


def test_parse_hash_text_email_identifier():
    digest = "e" * 128
    text = "\n".join(_hash_pdf_lines({INNER_EMAIL: digest}))
    assert google.parse_hash_text(text, known_names={INNER_EMAIL}) == {INNER_EMAIL: digest}


def test_parse_hash_text_fallback_without_known_names():
    digest = "f" * 128
    text = f"Valores de Hash\n{INNER_A}:\nSHA512-{digest}"
    result = google.parse_hash_text(text)
    assert list(result.values()) == [digest]
    assert next(iter(result)).endswith(INNER_A)


def test_parse_hash_text_normalizes_to_lowercase():
    digest = "AB" * 64
    assert google.parse_hash_text(f"{INNER_A}:SHA512-{digest}", {INNER_A}) == {INNER_A: digest.lower()}


# --------------------------------------------------------------- load_entries


def test_load_entries_from_lers_folder(lers_folder):
    folder, inner, hashes = lers_folder
    entries = google.load_entries(folder)
    assert sorted(e.file_name for e in entries) == sorted(inner)
    for entry in entries:
        assert entry.hash_algorithm == "sha512"
        assert entry.expected_hash == hashes[entry.file_name]
        assert entry.file_link == str(folder / "12345678-20260101-1.zip")


def test_load_entries_reads_real_pdf_text(lers_folder):
    folder, _, hashes = lers_folder
    text = google.extract_pdf_text(folder / "Valores de Hash 999999999.pdf")
    assert google.parse_hash_text(text, set(hashes)) == hashes


def test_load_entries_inner_zip_missing_from_pdf_has_no_expected_hash(lers_folder):
    folder, inner, _ = lers_folder
    _write_production_zip(folder / "12345678-20260101-2.zip", {INNER_EMAIL: _inner_zip_bytes({"x.txt": b"x"})})
    by_name = {e.file_name: e for e in google.load_entries(folder)}
    assert by_name[INNER_EMAIL].expected_hash is None
    assert by_name[INNER_EMAIL].file_link.endswith("12345678-20260101-2.zip")


def test_load_entries_pdf_name_without_zip_has_empty_link(tmp_path):
    folder = tmp_path / "lers"
    folder.mkdir()
    _write_pdf(folder / "hashes.pdf", _hash_pdf_lines({INNER_A: "0" * 128}))
    entries = google.load_entries(folder)
    assert len(entries) == 1
    assert entries[0].file_link == ""
    assert entries[0].file_name.endswith(INNER_A)


def test_load_entries_accepts_loose_inner_zip(tmp_path):
    folder = tmp_path / "lers"
    folder.mkdir()
    data = _inner_zip_bytes({"a.txt": b"a"})
    (folder / INNER_A).write_bytes(data)
    _write_pdf(folder / "hashes.pdf", _hash_pdf_lines({INNER_A: hashlib.sha512(data).hexdigest()}))
    [entry] = google.load_entries(folder)
    assert entry.file_link == str(folder / INNER_A)
    assert entry.expected_hash == hashlib.sha512(data).hexdigest()


def test_load_entries_missing_folder_raises(tmp_path):
    with pytest.raises(core.PipelineError):
        google.load_entries(tmp_path / "nao_existe")


def test_load_entries_empty_folder_raises(tmp_path):
    with pytest.raises(core.PipelineError):
        google.load_entries(tmp_path)


# --------------------------------------------------------------- pipeline


PRODUCTION = "12345678-20260101-1.zip"


def test_run_pipeline_google_keeps_original_zips_without_extracting(lers_folder, tmp_path):
    folder, inner, _ = lers_folder
    out = tmp_path / "out"
    _, entries, summary = _run(folder, out)

    assert summary.concluidos == 2 and summary.erros == 0 and summary.hash_invalido == 0
    # O destino tem so' copias identicas do que veio do LERS: nenhum ZIP
    # interno tirado do de producao, nenhuma pasta extraida.
    assert sorted(p.name for p in out.iterdir()) == sorted(
        [PRODUCTION, "Valores de Hash 999999999.pdf", "Carta 999999999.pdf", "joakapple_log.txt"]
    )
    for name in (PRODUCTION, "Valores de Hash 999999999.pdf", "Carta 999999999.pdf"):
        assert (out / name).read_bytes() == (folder / name).read_bytes()
    assert all(e.computed_hash == e.expected_hash for e in entries)


def test_run_pipeline_google_copies_production_zip_once(lers_folder, tmp_path):
    folder, _, _ = lers_folder
    lines = []
    provider = get_provider("google")
    entries = provider.load_entries(folder)
    config = core.PipelineConfig(source=folder, output_dir=tmp_path / "out", provider=provider, max_workers=4)
    core.run_pipeline(config, entries, on_update=lambda e: None, log=lines.append)
    assert sum("ZIP de producao copiado" in line for line in lines) == 1


def test_run_pipeline_google_hash_mismatch(lers_folder, tmp_path):
    folder, _, hashes = lers_folder
    # PDF com hash adulterado para INNER_A
    tampered = dict(hashes, **{INNER_A: "0" * 128})
    _write_pdf(folder / "Valores de Hash 999999999.pdf", _hash_pdf_lines(tampered))
    _, entries, _ = _run(folder, tmp_path / "out")
    by_name = {e.file_name: e for e in entries}
    assert by_name[INNER_A].status == "hash_invalido"
    assert by_name[INNER_B].status == "concluido"


def test_run_pipeline_google_corrupted_production_zip_marks_erro(lers_folder, tmp_path):
    folder, _, _ = lers_folder
    out = tmp_path / "out"
    out.mkdir()
    (out / PRODUCTION).write_bytes(b"isto nao e' um zip")
    _, entries, summary = _run(folder, out)
    assert summary.erros == 2
    assert all("falha ao calcular o hash" in e.message for e in entries)


def test_run_pipeline_google_second_run_reuses_copy(lers_folder, tmp_path):
    folder, _, _ = lers_folder
    _run(folder, tmp_path / "out")
    _, entries, summary = _run(folder, tmp_path / "out")
    assert summary.concluidos == 2 and summary.erros == 0
    assert all(e.message == "arquivo ja baixado" for e in entries)


def test_run_pipeline_google_verify_only(lers_folder):
    folder, _, hashes = lers_folder
    _, entries, summary = _run(folder, folder, download=False)
    assert summary.concluidos == 2
    assert {e.file_name: e.computed_hash for e in entries} == hashes


def test_run_pipeline_google_missing_zip_marks_erro(tmp_path):
    folder = tmp_path / "lers"
    folder.mkdir()
    _write_pdf(folder / "hashes.pdf", _hash_pdf_lines({INNER_A: "0" * 128}))
    _, entries, summary = _run(folder, tmp_path / "out")
    assert summary.erros == 1
    assert "nao encontrado em nenhum ZIP" in entries[0].message


def test_run_pipeline_google_output_dir_same_as_source(lers_folder):
    folder, inner, _ = lers_folder
    before = {p.name: p.read_bytes() for p in folder.iterdir()}
    _, _, summary = _run(folder, folder)
    assert summary.concluidos == 2 and summary.erros == 0
    after = {p.name: p.read_bytes() for p in folder.iterdir() if p.name != "joakapple_log.txt"}
    assert after == before


def test_run_pipeline_google_loose_inner_zip(tmp_path):
    folder = tmp_path / "lers"
    folder.mkdir()
    data = _inner_zip_bytes({"a.txt": b"a"})
    (folder / INNER_A).write_bytes(data)
    _write_pdf(folder / "hashes.pdf", _hash_pdf_lines({INNER_A: hashlib.sha512(data).hexdigest()}))
    out = tmp_path / "out"
    _, _, summary = _run(folder, out)
    assert summary.concluidos == 1
    assert (out / INNER_A).read_bytes() == data


def test_fetch_cancelled_leaves_no_partial_file(lers_folder, tmp_path):
    folder, _, _ = lers_folder
    provider = get_provider("google")
    entry = next(e for e in provider.load_entries(folder) if e.file_name == INNER_A)
    cancel = threading.Event()
    cancel.set()
    dest = provider.local_path(entry, tmp_path)
    assert provider.fetch(None, entry, dest, cancel, log=lambda m: None) is False
    assert not dest.exists()
    assert not dest.with_suffix(dest.suffix + ".part").exists()


def test_delete_entry_files_removes_production_copy(lers_folder, tmp_path):
    folder, _, _ = lers_folder
    out = tmp_path / "out"
    config, entries, _ = _run(folder, out)
    entry = next(e for e in entries if e.file_name == INNER_A)
    assert core.delete_entry_files(entry, out, config.provider) == [out / PRODUCTION]
    assert (folder / PRODUCTION).is_file()


def test_delete_entry_files_never_removes_lers_original(lers_folder):
    folder, _, _ = lers_folder
    config, entries, _ = _run(folder, folder)
    assert core.delete_entry_files(entries[0], folder, config.provider) == []
    assert (folder / PRODUCTION).is_file()


# ------------------------------------------------------------- relatorio/CLI


def test_report_uses_google_provider_fields():
    data = report.ReportData(
        hash_rows=[report.HashRow(file_name=INNER_A, digest="ab" * 64, size_bytes=10)],
        **report.provider_fields(get_provider("google")),
    )
    text = report.render_report(data)
    assert "**Provedor de Aplicação:** Google LLC" in text
    assert "**Algoritmo utilizado:** SHA-512" in text
    assert "pacotes compactados (formato ZIP)" in text
    html = report.render_report_html(data)
    assert "Google LLC" in html and "SHA-512" in html


def test_compute_hash_rows_uses_entry_algorithm(lers_folder, tmp_path):
    folder, inner, hashes = lers_folder
    _, entries, _ = _run(folder, tmp_path / "out")
    provider = get_provider("google")
    rows = report.compute_hash_rows_cached(entries, tmp_path / "out", provider=provider)
    assert {r.file_name: r.digest for r in rows} == hashes
    assert {r.file_name: r.size_bytes for r in rows} == {n: len(d) for n, d in inner.items()}


def test_compute_hash_rows_without_cache_reads_inside_production_zip(lers_folder, tmp_path):
    folder, _, hashes = lers_folder
    _run(folder, tmp_path / "out")
    fresh = google.load_entries(folder)  # sem computed_hash
    rows = report.compute_hash_rows(fresh, tmp_path / "out", get_provider("google"))
    assert {r.file_name: r.digest for r in rows} == hashes


def test_cli_run_google_end_to_end(lers_folder, tmp_path):
    folder, _, _ = lers_folder
    out = tmp_path / "out"
    code = cli.main(["run", "--provider", "google", "--source", str(folder), "--output-dir", str(out), "--yes", "--quiet"])
    assert code == cli.EXIT_OK
    assert (out / PRODUCTION).read_bytes() == (folder / PRODUCTION).read_bytes()
    assert not (out / "extraido").exists()


def test_cli_report_google_lists_sha512(lers_folder, tmp_path, capsys):
    folder, _, hashes = lers_folder
    out = tmp_path / "out"
    cli.main(["run", "--provider", "google", "--source", str(folder), "--output-dir", str(out), "--yes", "--quiet"])
    capsys.readouterr()
    code = cli.main(["report", "--provider", "google", "--source", str(folder), "--output-dir", str(out)])
    text = capsys.readouterr().out
    assert code == cli.EXIT_OK
    assert "Google LLC" in text and "SHA-512" in text
    for digest in hashes.values():
        assert digest in text


def test_cli_run_google_ignores_decrypt_flag(lers_folder, tmp_path):
    folder, _, _ = lers_folder
    code = cli.main(["run", "--provider", "google", "--source", str(folder), "--output-dir",
                     str(tmp_path / "out"), "--no-download", "--no-verify", "--decrypt", "--yes", "--quiet"])
    assert code == cli.EXIT_FATAL  # sem etapa 3 no Google, nao sobra etapa nenhuma
