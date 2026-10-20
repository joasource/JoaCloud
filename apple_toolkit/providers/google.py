"""
JoaKApple - provedor Google: retorno baixado do LERS (lers.google.com).

O Google nao fornece CSV. Pelo LERS chegam, por request:

- um ou mais ZIPs de producao (``<id>-<AAAAMMDD>-<n>.zip``, sem senha), cada
  um contendo ZIPs internos ``<identificador>.<num>.<Servico>.<Recurso>_001.zip``
  com os dados de fato;
- PDFs de correspondencia, entre eles o "Valores de Hash", que lista o
  SHA-512 de cada ZIP INTERNO (nao do ZIP de producao), no formato
  ``<nome_do_zip_interno>:`` + ``SHA512-<128 hex>`` — com nome e hash
  quebrados em varias linhas pelo layout do PDF.

Aqui a "fonte" e' a pasta com esses arquivos. Cada ZIP interno vira um
FileEntry; a etapa de "baixar" tira o ZIP interno de dentro do ZIP de producao,
a verificacao confere o SHA-512 contra o PDF, e a etapa de abrir extrai o ZIP
interno em ``extraido/<nome>/``.

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
import time
import zipfile
from pathlib import Path
from typing import Optional

import core
from providers.base import Provider

HASH_ALGORITHM = "sha512"

# Rotulo + hash, depois de remover todo espaco/quebra de linha do texto do PDF.
# Dependendo do extrator, o hifen de "SHA512-" pode sumir.
_HASH_RE = re.compile(r":SHA-?512[-:]?([0-9a-fA-F]{128})")
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
    ser ZIPs internos ja extraidos soltos na pasta, tratados a parte)."""
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


def _safe_target(root: Path, member: str) -> Optional[Path]:
    """Caminho de extracao dentro de root, ou None se o membro tentar escapar
    (zip slip: caminho absoluto ou com '..')."""
    target = (root / member).resolve()
    root_resolved = root.resolve()
    if target != root_resolved and root_resolved not in target.parents:
        return None
    return target


class GoogleProvider(Provider):
    key = "google"
    display_name = "Google"
    legal_name = "Google LLC"
    hash_algorithm = HASH_ALGORITHM
    package_description = "pacotes compactados (formato ZIP)"

    source_label = "Pasta com o retorno do LERS"
    source_is_dir = True
    download_description = (
        "Tira cada ZIP de dados de dentro dos ZIPs de produção baixados do LERS."
    )
    verify_description = "Confere o hash SHA-512 de cada ZIP contra o PDF \"Valores de Hash\" do Google."
    unpack_label = "Extrair"
    unpack_description = "Extrai o conteúdo de cada ZIP já conferido para a subpasta extraido/."
    unpack_status = "extraindo"
    unpack_dir_name = "extraido"
    unpack_done_message = "ja existia extraido"
    needs_passphrase = False

    def load_entries(self, source: Path) -> list[core.FileEntry]:
        return load_entries(source)

    def fetch(self, session, entry, dest, cancel_event, log, on_progress=None) -> bool:
        if not entry.file_link:
            entry.message = "listado no PDF de hashes, mas nao encontrado em nenhum ZIP de producao"
            return False
        source = Path(entry.file_link)
        part_path = dest.with_suffix(dest.suffix + ".part")
        started = time.monotonic()
        try:
            with part_path.open("wb") as out:
                if source.name == entry.file_name:
                    # ZIP interno ja solto na pasta de origem
                    total = source.stat().st_size
                    with source.open("rb") as src:
                        copied = _copy_with_progress(src, out, entry, total, cancel_event, on_progress)
                else:
                    with zipfile.ZipFile(source) as zf:
                        total = zf.getinfo(entry.file_name).file_size
                        with zf.open(entry.file_name) as src:
                            copied = _copy_with_progress(src, out, entry, total, cancel_event, on_progress)
        except (OSError, KeyError, zipfile.BadZipFile) as exc:
            part_path.unlink(missing_ok=True)
            entry.message = f"falha ao ler {source.name}: {exc}"
            log(core.log_line("ERROR", "zip", "falha ao tirar arquivo do ZIP de producao",
                              file=entry.file_name, origem=source.name, erro=str(exc)))
            return False
        if copied is None:
            part_path.unlink(missing_ok=True)
            return False
        part_path.replace(dest)
        log(core.log_line("OK", "zip", "arquivo obtido do ZIP de producao",
                          file=entry.file_name, origem=source.name, bytes=copied,
                          duracao=f"{time.monotonic() - started:.2f}s"))
        return True

    def needs_unpack(self, entry: core.FileEntry) -> bool:
        return entry.file_name.lower().endswith(".zip")

    def unpacked_path(self, entry: core.FileEntry, output_dir: Path) -> Path:
        return Path(output_dir) / self.unpack_dir_name / Path(entry.file_name).stem

    def unpack(self, entry, src, dest, config, log) -> bool:
        # Extrai numa pasta temporaria e so' renomeia no fim: uma extracao
        # interrompida nunca deixa ``dest`` pela metade (que seria pulado como
        # "ja existia extraido" na proxima rodada).
        tmp = dest.with_name(dest.name + ".part")
        shutil.rmtree(tmp, ignore_errors=True)
        started = time.monotonic()
        try:
            with zipfile.ZipFile(src) as zf:
                for info in zf.infolist():
                    target = _safe_target(tmp, info.filename)
                    if target is None:
                        raise core.PipelineError(f"caminho suspeito dentro do ZIP: {info.filename}")
                    if info.is_dir():
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(info) as member, target.open("wb") as out:
                        shutil.copyfileobj(member, out, core.CHUNK_SIZE)
                count = len(zf.infolist())
            tmp.replace(dest)
        except (OSError, zipfile.BadZipFile, core.PipelineError) as exc:
            shutil.rmtree(tmp, ignore_errors=True)
            entry.message = f"falha ao extrair: {exc}"
            log(core.log_line("ERROR", "zip", "extracao falhou", file=entry.file_name, erro=str(exc)))
            return False
        log(core.log_line("OK", "zip", "extracao concluida", file=entry.file_name, itens=count,
                          duracao=f"{time.monotonic() - started:.2f}s", saida=str(dest)))
        return True
