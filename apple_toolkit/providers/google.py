"""
JoaKApple - provedor Google: retorno baixado do LERS (lers.google.com).

O Google nao fornece CSV. Pelo LERS chegam, por request:

- um ou mais ZIPs de producao (``<id>-<AAAAMMDD>-<n>.zip``, sem senha), cada
  um contendo ZIPs internos ``<identificador>.<num>.<Servico>.<Recurso>_001.zip``
  com os dados de fato;
- PDFs de correspondencia: o "Valores de Hash" (ou a propria carta, com a
  secao "Valores de Hash dos Arquivos Produzidos") lista o SHA-512 de cada
  ZIP INTERNO (nao do ZIP de producao), no formato ``<nome_do_zip_interno>:``
  + ``SHA512-<128 hex>`` ou so' ``<128 hex>`` — com nome e hash quebrados em
  varias linhas pelo layout do PDF.

Aqui a "fonte" e' a pasta com esses arquivos. Cada ZIP interno vira um
FileEntry. Nada e' extraido nem descompactado em disco: os dados ficam nos ZIPs
originais, como o Google entregou. A etapa de "baixar" copia, byte a byte, o
ZIP de producao (e os PDFs) para o destino, e a verificacao calcula o SHA-512
de cada ZIP interno lendo-o direto de dentro do ZIP de producao, conferindo
contra o PDF. Nao ha etapa 3.

Copyright (C) 2026 Joaquim Ferreira Silva Neto <joaquimfsneto@gmail.com>

Este programa e' software livre: voce pode redistribui-lo e/ou modifica-lo
sob os termos da GNU General Public License, conforme publicada pela Free
Software Foundation, na versao 3 da licenca, ou (a seu criterio) qualquer
versao posterior. Este programa e' distribuido na esperanca de ser util,
mas SEM NENHUMA GARANTIA; nem mesmo a garantia implicita de COMERCIALIZACAO
ou ADEQUACAO A UM PROPOSITO ESPECIFICO. Veja a GNU General Public License
para mais detalhes: <https://www.gnu.org/licenses/>.
"""

from __future__ import annotations

import re
import shutil
import threading
import time
import zipfile
from pathlib import Path
from typing import Optional

import core
from providers.base import Provider

HASH_ALGORITHM = "sha512"

# "<nome>:" + hash, depois de remover todo espaco/quebra de linha do texto do
# PDF. Algumas cartas trazem o rotulo "SHA512-" antes do hash (dependendo do
# extrator, sem o hifen); outras, so' o hash. O hash pode vir colado no nome
# seguinte, por isso nao ha' limite de palavra depois dos 128 hex.
_HASH_RE = re.compile(r":(?:SHA-?512[-:]?)?([0-9a-fA-F]{128})")
# Fallback pra nome de ZIP listado no PDF que nao esta em nenhum ZIP de
# producao: o maior sufixo do trecho antes do hash com cara de nome de arquivo.
_NAME_TAIL_RE = re.compile(r"([\w.@+\-]+\.zip)$", re.IGNORECASE)


def _flatten(text: str) -> str:
    return re.sub(r"\s+", "", text)


def parse_hash_text(text: str, known_names: Optional[set[str]] = None) -> dict[str, str]:
    """Extrai {nome_do_zip: sha512} do texto do PDF "Valores de Hash".

    O texto e' achatado (sem espacos) porque o PDF quebra nome e hash em
    varias linhas. O nome de cada hash e' o trecho entre o hash anterior e o
    atual; como esse trecho pode vir grudado em cabecalho de pagina/titulo
    (ex.: CEP do endereco do Google), quando ``known_names`` (nomes reais
    dos ZIPs internos) e' informado, usa o maior nome conhecido que termina
    exatamente ali — so' cai na heuristica de nome se nao houver nenhum.
    """
    flat = _flatten(text)
    known = sorted(
        {(_flatten(n), n) for n in (known_names or set())}, key=lambda item: len(item[0]), reverse=True
    )

    result: dict[str, str] = {}
    previous_end = 0
    for match in _HASH_RE.finditer(flat):
        segment = flat[previous_end:match.start()]
        previous_end = match.end()
        name = next((original for flat_name, original in known if segment.endswith(flat_name)), None)
        if name is None:
            tail = _NAME_TAIL_RE.search(segment)
            if not tail:
                continue
            name = tail.group(1)
        result[name] = match.group(1).lower()
    return result


def extract_pdf_text(pdf_path: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - dependencia obrigatoria em requirements.txt
        raise core.PipelineError(
            "A biblioteca 'pypdf' nao esta instalada (necessaria para ler o PDF de hashes do Google)."
        ) from exc
    try:
        reader = PdfReader(str(pdf_path))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception as exc:  # noqa: BLE001 - PDF corrompido/protegido nao deve derrubar o lote
        raise core.PipelineError(f"Nao foi possivel ler o PDF {pdf_path.name}: {exc}") from exc


def _production_zips(folder: Path) -> dict[str, Path]:
    """{nome_do_zip_interno: zip_de_producao_que_o_contem} para os ZIPs da pasta.

    ZIPs que nao contem nenhum outro .zip na raiz sao ignorados aqui (podem
    ser ZIPs internos ja soltos na pasta, tratados a parte)."""
    index: dict[str, Path] = {}
    for path in sorted(folder.glob("*.zip")):
        try:
            with zipfile.ZipFile(path) as zf:
                members = [
                    info.filename for info in zf.infolist()
                    if not info.is_dir() and info.filename.lower().endswith(".zip") and "/" not in info.filename
                ]
        except zipfile.BadZipFile:
            continue
        for member in members:
            index.setdefault(member, path)
    return index


def load_entries(folder: Path) -> list[core.FileEntry]:
    folder = Path(folder)
    if not folder.is_dir():
        raise core.PipelineError(f"Pasta com o retorno do Google nao encontrada: {folder}")

    containers = _production_zips(folder)
    loose = {
        p.name: p for p in folder.glob("*.zip")
        if p.name not in containers and p not in containers.values()
    }
    known_names = set(containers) | set(loose)

    expected: dict[str, str] = {}
    for pdf in sorted(folder.glob("*.pdf")):
        expected.update(parse_hash_text(extract_pdf_text(pdf), known_names))

    if not known_names and not expected:
        raise core.PipelineError(
            "Nenhum ZIP de producao nem PDF de hashes do Google encontrado na pasta: "
            f"{folder}"
        )

    entries: list[core.FileEntry] = []
    for name in sorted(known_names | set(expected)):
        if name in containers:
            link = str(containers[name])
        elif name in loose:
            link = str(loose[name])
        else:
            link = ""  # listado no PDF, mas nao veio em nenhum ZIP: fetch acusa
        entries.append(
            core.FileEntry(
                file_name=name,
                file_link=link,
                expected_hash=expected.get(name),
                hash_algorithm=HASH_ALGORITHM,
            )
        )
    return entries


def _copy_with_progress(src, dst_fh, entry, total, cancel_event, on_progress) -> Optional[int]:
    """Copia em blocos; retorna bytes copiados, ou None se cancelado."""
    bytes_done = 0
    last_emit = 0.0
    if on_progress:
        on_progress(entry, 0, total, "download")
    while True:
        if cancel_event.is_set():
            return None
        chunk = src.read(core.CHUNK_SIZE)
        if not chunk:
            break
        dst_fh.write(chunk)
        bytes_done += len(chunk)
        now = time.monotonic()
        if on_progress and (now - last_emit) >= core.PROGRESS_THROTTLE_SECONDS:
            on_progress(entry, bytes_done, total, "download")
            last_emit = now
    if on_progress:
        on_progress(entry, bytes_done, total or bytes_done, "download")
    return bytes_done


# Varios ZIPs internos moram no mesmo ZIP de producao: so' um worker copia
# cada ZIP de producao, os outros esperam e reaproveitam.
_copy_locks: dict[Path, threading.Lock] = {}
_copy_locks_guard = threading.Lock()


def _copy_lock(dest: Path) -> threading.Lock:
    with _copy_locks_guard:
        return _copy_locks.setdefault(Path(dest).resolve(), threading.Lock())


def _is_container(entry: core.FileEntry) -> bool:
    """True quando o ZIP interno esta' dentro de um ZIP de producao (e nao solto na pasta)."""
    return bool(entry.file_link) and Path(entry.file_link).name != entry.file_name


class GoogleProvider(Provider):
    key = "google"
    display_name = "Google"
    legal_name = "Google LLC"
    hash_algorithm = HASH_ALGORITHM
    package_description = "pacotes compactados (formato ZIP)"

    source_label = "Pasta com o retorno do LERS"
    source_is_dir = True
    download_description = (
        "Copia os ZIPs de produção e os PDFs do LERS para o destino, sem alterá-los."
    )
    verify_description = (
        "Confere o SHA-512 de cada ZIP de dados (lido dentro do ZIP de produção, "
        "sem extrair) contra o PDF \"Valores de Hash\" do Google."
    )
    unpack_label = ""
    unpack_description = ""
    needs_passphrase = False
    has_unpack_step = False

    def load_entries(self, source: Path) -> list[core.FileEntry]:
        return load_entries(source)

    # ------------------------------------------------------- arquivos locais

    def local_path(self, entry: core.FileEntry, output_dir: Path) -> Path:
        name = Path(entry.file_link).name if entry.file_link else entry.file_name
        return Path(output_dir) / name

    def hash_entry(self, entry, output_dir, on_progress=None) -> str:
        path = self.local_path(entry, output_dir)
        if not _is_container(entry):
            return core.hash_of_file(path, entry.hash_algorithm, on_progress=on_progress)
        # Le o ZIP interno de dentro do ZIP de producao em memoria, em blocos:
        # nada e' gravado em disco.
        with zipfile.ZipFile(path) as zf:
            info = zf.getinfo(entry.file_name)
            with zf.open(info) as member:
                return core.hash_of_stream(member, info.file_size, entry.hash_algorithm, on_progress)

    def entry_size(self, entry, output_dir) -> int:
        path = self.local_path(entry, output_dir)
        if not _is_container(entry):
            return path.stat().st_size
        with zipfile.ZipFile(path) as zf:
            return zf.getinfo(entry.file_name).file_size

    def removable_paths(self, entry, output_dir) -> list[Path]:
        dest = self.local_path(entry, output_dir)
        part = dest.with_suffix(dest.suffix + ".part")
        # Nunca apaga o original do LERS: com destino == pasta de origem, o
        # "arquivo baixado" e' o proprio original.
        if entry.file_link and dest.resolve() == Path(entry.file_link).resolve():
            return [part]
        return [dest, part]

    # ------------------------------------------------------------- etapas

    def prepare(self, config, entries, log) -> None:
        """Copia os PDFs de correspondencia (incluindo o de hashes) junto com os ZIPs."""
        if not config.download or not config.source.is_dir():
            return
        if config.source.resolve() == config.output_dir.resolve():
            return
        for pdf in sorted(config.source.glob("*.pdf")):
            dest = config.output_dir / pdf.name
            if dest.exists():
                continue
            shutil.copy2(pdf, dest)
            log(core.log_line("OK", "copia", "PDF copiado", file=pdf.name, bytes=pdf.stat().st_size))

    def fetch(self, session, entry, dest, cancel_event, log, on_progress=None) -> bool:
        if not entry.file_link:
            entry.message = "listado no PDF de hashes, mas nao encontrado em nenhum ZIP de producao"
            return False
        source = Path(entry.file_link)
        with _copy_lock(dest):
            if dest.exists():  # outro worker ja copiou este ZIP de producao
                if on_progress:
                    size = dest.stat().st_size
                    on_progress(entry, size, size, "download")
                return True
            part_path = dest.with_suffix(dest.suffix + ".part")
            started = time.monotonic()
            try:
                total = source.stat().st_size
                with source.open("rb") as src, part_path.open("wb") as out:
                    copied = _copy_with_progress(src, out, entry, total, cancel_event, on_progress)
                if copied is not None:
                    shutil.copystat(source, part_path)
            except OSError as exc:
                part_path.unlink(missing_ok=True)
                entry.message = f"falha ao copiar {source.name}: {exc}"
                log(core.log_line("ERROR", "copia", "falha ao copiar ZIP de producao",
                                  file=entry.file_name, origem=str(source), erro=str(exc)))
                return False
            if copied is None:
                part_path.unlink(missing_ok=True)
                return False
            part_path.replace(dest)
        log(core.log_line("OK", "copia", "ZIP de producao copiado (original, sem alteracao)",
                          file=dest.name, origem=str(source), bytes=copied,
                          duracao=f"{time.monotonic() - started:.2f}s"))
        return True
