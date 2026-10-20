"""
JoaCloud - provedor Apple: CSV com links de download, SHA-256 e pacotes .gpg.

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

import csv
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import core
from providers.base import Provider

GPG_TIMEOUT_SECONDS = 300

# Nomes de coluna aceitos no CSV da Apple (variam entre exportacoes/idiomas)
COLUMN_ALIASES = {
    "file_name": ["File_Name", "File Name", "FileName", "Filename", "Nome_Arquivo"],
    "file_link": ["File_Link", "File Link", "FileLink", "Link", "URL", "Download_Link"],
    "sha256": ["GPG_SHA256", "SHA256", "Sha256", "Hash", "GPG_Sha256"],
}


def _bundled_gpg_path() -> Optional[Path]:
    """Caminho do gpg embutido no executavel (PyInstaller), se houver.

    So o build do Windows embute um GnuPG portatil em ``gnupg-bin/`` ao lado
    do executavel; no Linux o GnuPG do sistema (quase sempre ja instalado)
    continua sendo usado via PATH.
    """
    base = getattr(sys, "_MEIPASS", None)
    if not base:
        return None
    name = "gpg.exe" if os.name == "nt" else "gpg"
    candidate = Path(base) / "gnupg-bin" / name
    return candidate if candidate.is_file() else None


def _gpg_executable() -> str:
    bundled = _bundled_gpg_path()
    return str(bundled) if bundled else "gpg"


def check_gpg_available() -> bool:
    return _bundled_gpg_path() is not None or shutil.which("gpg") is not None


def _resolve_column(fieldnames: list[str], aliases: list[str]) -> Optional[str]:
    normalized = {f.strip().lower(): f for f in fieldnames if f}
    for alias in aliases:
        hit = normalized.get(alias.strip().lower())
        if hit:
            return hit
    return None


def load_entries(csv_path: Path) -> list[core.FileEntry]:
    csv_path = Path(csv_path)
    if not csv_path.is_file():
        raise core.PipelineError(f"Arquivo CSV nao encontrado: {csv_path}")

    with csv_path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            raise core.PipelineError("CSV vazio ou sem cabecalho.")

        col_name = _resolve_column(reader.fieldnames, COLUMN_ALIASES["file_name"])
        col_link = _resolve_column(reader.fieldnames, COLUMN_ALIASES["file_link"])
        col_hash = _resolve_column(reader.fieldnames, COLUMN_ALIASES["sha256"])

        if not col_name or not col_link:
            raise core.PipelineError(
                "Nao foi possivel identificar as colunas de nome/link do arquivo no CSV. "
                f"Cabecalho encontrado: {reader.fieldnames}"
            )

        entries: list[core.FileEntry] = []
        for row in reader:
            file_name = (row.get(col_name) or "").strip()
            file_link = (row.get(col_link) or "").strip()
            expected_hash = (row.get(col_hash) or "").strip().lower() if col_hash else ""

            if not file_name or not file_link:
                continue

            entries.append(
                core.FileEntry(
                    file_name=file_name,
                    file_link=file_link,
                    expected_hash=expected_hash or None,
                )
            )

    if not entries:
        raise core.PipelineError("Nenhuma linha valida encontrada no CSV.")

    return entries


def decrypt(
    entry: core.FileEntry,
    encrypted_path: Path,
    decrypted_path: Path,
    passphrase: str,
    log,
) -> bool:
    decrypted_path.parent.mkdir(parents=True, exist_ok=True)
    comando = [
        _gpg_executable(),
        "--batch",
        "--yes",
        "--pinentry-mode",
        "loopback",
        "--passphrase-fd",
        "0",
        "--output",
        str(decrypted_path),
        "--decrypt",
        str(encrypted_path),
    ]
    started = time.monotonic()
    try:
        result = subprocess.run(
            comando,
            input=passphrase.encode("utf-8"),
            capture_output=True,
            timeout=GPG_TIMEOUT_SECONDS,
        )
        duration = time.monotonic() - started
        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace").strip()
            entry.message = f"gpg falhou: {stderr or 'erro desconhecido'}"
            decrypted_path.unlink(missing_ok=True)
            log(
                core.log_line(
                    "ERROR", "gpg", "descriptografia falhou",
                    file=entry.file_name, exit_code=result.returncode,
                    duracao=f"{duration:.2f}s", stderr=stderr or "erro desconhecido",
                )
            )
            return False
        log(
            core.log_line(
                "OK", "gpg", "descriptografia concluida",
                file=entry.file_name, exit_code=0, duracao=f"{duration:.2f}s",
                saida=str(decrypted_path),
            )
        )
        return True
    except subprocess.TimeoutExpired:
        entry.message = "gpg excedeu o tempo limite"
        decrypted_path.unlink(missing_ok=True)
        log(
            core.log_line(
                "ERROR", "gpg", "tempo limite excedido",
                file=entry.file_name, timeout_s=GPG_TIMEOUT_SECONDS,
            )
        )
        return False
    except Exception as exc:  # noqa: BLE001
        entry.message = f"erro ao executar gpg: {exc}"
        decrypted_path.unlink(missing_ok=True)
        log(core.log_line("ERROR", "gpg", "erro ao executar processo", file=entry.file_name, erro=str(exc)))
        return False


class AppleProvider(Provider):
    key = "apple"
    display_name = "Apple"
    legal_name = "Apple Inc."
    hash_algorithm = "sha256"
    package_description = "pacotes criptografados (formato GPG)"

    source_label = "Arquivo CSV da Apple"
    source_is_dir = False
    download_description = "Baixa os arquivos do CSV, retomando downloads incompletos automaticamente."
    verify_description = "Confere o hash SHA256 de cada arquivo contra o valor informado pela Apple."
    unpack_label = "Descriptografar"
    unpack_description = "Usa a senha GPG para descriptografar os arquivos .gpg já conferidos."
    unpack_status = "decriptando"
    unpack_dir_name = "decriptado"
    unpack_done_message = "ja existia descriptografado"
    needs_passphrase = True

    def load_entries(self, source: Path) -> list[core.FileEntry]:
        return load_entries(source)

    def fetch(self, session, entry, dest, cancel_event, log, on_progress=None) -> bool:
        return core.download_http(session, entry, dest, cancel_event, log, on_progress=on_progress)

    def needs_unpack(self, entry: core.FileEntry) -> bool:
        return entry.is_encrypted

    def unpacked_path(self, entry: core.FileEntry, output_dir: Path) -> Path:
        return Path(output_dir) / self.unpack_dir_name / Path(entry.file_name).stem

    def unpack(self, entry, src, dest, config, log) -> bool:
        return decrypt(entry, src, dest, config.passphrase, log)

    def preflight(self, config, entries) -> None:
        if entries and any(e.is_encrypted for e in entries) and config.unpack:
            if not check_gpg_available():
                raise core.PipelineError(
                    "O executavel 'gpg' nao foi encontrado no PATH. "
                    "Instale o Gpg4win (https://gpg4win.org) para descriptografar os arquivos."
                )
            if not config.passphrase:
                raise core.PipelineError("Senha (passphrase) do GPG nao informada.")

    def missing_tool_message(self) -> Optional[str]:
        if check_gpg_available():
            return None
        return "O executavel 'gpg' nao foi encontrado no PATH."
