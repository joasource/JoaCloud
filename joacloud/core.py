"""
JoaCloud - motor do pipeline (baixar, conferir hash, descriptografar).

Autor: Joaquim Ferreira Silva Neto <joaquimfsneto@gmail.com>

Este modulo nao depende de interface grafica: pode ser usado tanto pela GUI
(gui.py) quanto pela linha de comando (cli.py). O que muda de uma nuvem para
outra (Apple, Google...) fica nos provedores, em providers/.

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

import hashlib
import shutil
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

if TYPE_CHECKING:
    from providers.base import Provider

CONNECT_TIMEOUT = 15
READ_TIMEOUT = 60
MAX_ATTEMPTS = 4
RETRY_BACKOFF_SECONDS = 5
CHUNK_SIZE = 1024 * 1024  # 1 MiB
PROGRESS_THROTTLE_SECONDS = 0.2


class PipelineError(Exception):
    """Erro fatal que impede o pipeline de iniciar (config invalida, gpg ausente etc.)."""


# --------------------------------------------------------------- log estruturado

def _format_size(n: float) -> str:
    """Formatacao compacta de bytes para uso em campos de log (ex.: 8.54MB)."""
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{int(size)}{unit}" if unit == "B" else f"{size:.2f}{unit}"
        size /= 1024
    return f"{size:.2f}TB"


def _fmt_field(value) -> str:
    if isinstance(value, float):
        text = f"{value:.3f}"
    else:
        text = str(value)
    if not text or any(ch in text for ch in " \t\"="):
        text = text.replace('"', '\\"')
        return f'"{text}"'
    return text


def log_line(level: str, component: str, message: str, **fields) -> str:
    """Monta uma linha de log estruturada: timestamp, nivel, componente, mensagem
    e campos tecnicos chave=valor (bytes, duracao, velocidade, tentativa, etc.)."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    line = f"{ts} [{level:<5}] {component:<10} {message}"
    if fields:
        line += "  " + " ".join(f"{k}={_fmt_field(v)}" for k, v in fields.items())
    return line


@dataclass
class FileEntry:
    file_name: str
    # Origem do arquivo: URL de download (Apple) ou caminho do ZIP de producao
    # que contem o arquivo (Google) — quem interpreta e' o provedor.
    file_link: str
    expected_hash: Optional[str]
    hash_algorithm: str = "sha256"
    status: str = "pendente"
    message: str = ""
    # Preenchidos quando a etapa de verificacao do pipeline calcula o hash do
    # arquivo, pra' quem vier depois (ex.: tela do Termo de Recebimento) poder
    # reaproveitar em vez de recalcular — ver cached_file_hash().
    computed_hash: Optional[str] = None
    computed_hash_stat: Optional[tuple[int, float]] = None

    @property
    def is_encrypted(self) -> bool:
        return self.file_name.lower().endswith(".gpg")


@dataclass
class ProgressEvent:
    """Progresso de bytes de uma etapa em andamento (download ou verificacao de hash),
    para a barra por arquivo na GUI/CLI."""

    file_name: str
    bytes_done: int
    bytes_total: int
    phase: str = "download"  # "download" | "hash"


def format_eta(seconds: float) -> str:
    """mm:ss (ou hh:mm:ss acima de 1h) para exibir tempo restante estimado."""
    if seconds != seconds or seconds < 0:  # nan ou negativo
        seconds = 0
    seconds = int(seconds)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


@dataclass
class SpeedEstimate:
    speed_bps: float
    eta_seconds: Optional[float]


class ProgressTracker:
    """Velocidade media desde o inicio do progresso atual; reinicia quando
    bytes_done cai (nova tentativa recomecando do zero)."""

    def __init__(self):
        self._start: Optional[float] = None
        self._last_bytes = 0

    def update(self, bytes_done: int, bytes_total: int, now: Optional[float] = None) -> SpeedEstimate:
        if now is None:
            now = time.monotonic()
        if self._start is None or bytes_done < self._last_bytes:
            self._start = now  # primeiro evento, ou nova tentativa reiniciando do zero
        self._last_bytes = bytes_done
        elapsed = now - self._start
        speed = bytes_done / elapsed if elapsed > 0.2 and bytes_done > 0 else 0.0

        eta_seconds: Optional[float] = None
        if speed > 0 and bytes_total > bytes_done:
            eta_seconds = (bytes_total - bytes_done) / speed

        return SpeedEstimate(speed_bps=speed, eta_seconds=eta_seconds)


def aggregate_progress(
    events: dict[str, "ProgressEvent"], start_time: float, now: Optional[float] = None
) -> dict:
    """Agrega bytes/velocidade/ETA de varios ProgressEvent em andamento (lote inteiro)."""
    if now is None:
        now = time.monotonic()

    bytes_done = sum(e.bytes_done for e in events.values())
    bytes_known = sum(e.bytes_total for e in events.values() if e.bytes_total > 0)
    elapsed = max(now - start_time, 0.001)
    speed_bps = bytes_done / elapsed if bytes_done > 0 else 0.0

    eta_seconds: Optional[float] = None
    if speed_bps > 0 and bytes_known > bytes_done:
        eta_seconds = (bytes_known - bytes_done) / speed_bps

    return {
        "bytes_done": bytes_done,
        "bytes_known": bytes_known,
        "speed_bps": speed_bps,
        "eta_seconds": eta_seconds,
    }


@dataclass
class PipelineSummary:
    total: int = 0
    concluidos: int = 0
    ja_existiam: int = 0
    hash_invalido: int = 0
    erros: int = 0
    cancelados: int = 0
    elapsed_seconds: float = 0.0


@dataclass
class PipelineConfig:
    # CSV da Apple, pasta com o retorno do LERS do Google... (ver provider.load_entries)
    source: Path
    output_dir: Path
    passphrase: str = ""
    max_workers: int = 4
    download: bool = True
    verify: bool = True
    # Etapa 3: descriptografar (so' Apple; o Google nao tem etapa 3).
    unpack: bool = True
    log_path: Optional[Path] = None
    provider: Optional["Provider"] = None

    def __post_init__(self):
        from providers import get_provider

        self.source = Path(self.source)
        self.output_dir = Path(self.output_dir)
        if self.provider is None:
            self.provider = get_provider()
        if self.log_path is None:
            self.log_path = self.output_dir / "joacloud_log.txt"
        else:
            self.log_path = Path(self.log_path)

    @property
    def unpacked_dir(self) -> Path:
        return self.output_dir / self.provider.unpack_dir_name


def hash_of_stream(
    fh,
    total: int,
    algorithm: str = "sha256",
    on_progress: Optional[Callable[[int, int], None]] = None,
) -> str:
    """Hash de um arquivo ja aberto (leitura em blocos), com progresso."""
    hasher = hashlib.new(algorithm)
    bytes_done = 0
    last_emit = 0.0
    while True:
        data = fh.read(CHUNK_SIZE)
        if not data:
            break
        hasher.update(data)
        bytes_done += len(data)
        if on_progress:
            now = time.monotonic()
            if now - last_emit >= PROGRESS_THROTTLE_SECONDS:
                on_progress(bytes_done, total)
                last_emit = now
    if on_progress:
        on_progress(bytes_done, total)  # evento final, garante 100%
    return hasher.hexdigest()


def hash_of_file(
    path: Path,
    algorithm: str = "sha256",
    on_progress: Optional[Callable[[int, int], None]] = None,
) -> str:
    with path.open("rb") as fh:
        return hash_of_stream(fh, path.stat().st_size, algorithm, on_progress)


def cached_file_hash(entry: FileEntry, path: Path) -> Optional[str]:
    """Hash ja calculado pro arquivo (na verificacao do pipeline), se o arquivo
    em disco nao mudou desde entao (mesmo tamanho/mtime); None se precisa
    (re)calcular."""
    if entry.computed_hash is None or entry.computed_hash_stat is None:
        return None
    try:
        stat = path.stat()
    except OSError:
        return None
    if (stat.st_size, stat.st_mtime) != entry.computed_hash_stat:
        return None
    return entry.computed_hash


def _make_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=MAX_ATTEMPTS,
        backoff_factor=RETRY_BACKOFF_SECONDS,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def download_http(
    session: requests.Session,
    entry: FileEntry,
    dest: Path,
    cancel_event: threading.Event,
    log: Callable[[str], None],
    on_progress: Optional[Callable[[FileEntry, int, int, str], None]] = None,
) -> bool:
    """Baixa entry.file_link para dest, com retomada em arquivo .part e retries."""
    part_path = dest.with_suffix(dest.suffix + ".part")

    for attempt in range(1, MAX_ATTEMPTS + 1):
        if cancel_event.is_set():
            return False
        started = time.monotonic()
        bytes_done = 0
        last_emit = 0.0
        try:
            with session.get(
                entry.file_link, stream=True, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT)
            ) as response:
                response.raise_for_status()
                total_bytes = int(response.headers.get("Content-Length", 0) or 0)
                log(
                    log_line(
                        "INFO", "download", "conexao estabelecida",
                        file=entry.file_name, tentativa=f"{attempt}/{MAX_ATTEMPTS}",
                        status_http=response.status_code,
                        tamanho=_format_size(total_bytes) if total_bytes else "desconhecido",
                    )
                )
                if on_progress:
                    on_progress(entry, 0, total_bytes, "download")
                with part_path.open("wb") as fh:
                    for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                        if cancel_event.is_set():
                            fh.close()
                            part_path.unlink(missing_ok=True)
                            return False
                        if chunk:
                            fh.write(chunk)
                            bytes_done += len(chunk)
                            now = time.monotonic()
                            if on_progress and (now - last_emit) >= PROGRESS_THROTTLE_SECONDS:
                                on_progress(entry, bytes_done, total_bytes, "download")
                                last_emit = now
                if on_progress:
                    on_progress(entry, bytes_done, total_bytes or bytes_done, "download")
            part_path.replace(dest)
            duration = max(time.monotonic() - started, 0.001)
            log(
                log_line(
                    "OK", "download", "download concluido",
                    file=entry.file_name, bytes=bytes_done,
                    duracao=f"{duration:.2f}s", velocidade=f"{_format_size(bytes_done / duration)}/s",
                )
            )
            return True
        except Exception as exc:  # noqa: BLE001 - queremos capturar qualquer falha de rede
            log(
                log_line(
                    "WARN", "download", "tentativa falhou",
                    file=entry.file_name, tentativa=f"{attempt}/{MAX_ATTEMPTS}",
                    bytes_recebidos=bytes_done, erro=str(exc),
                )
            )
            part_path.unlink(missing_ok=True)
            if attempt < MAX_ATTEMPTS:
                for _ in range(RETRY_BACKOFF_SECONDS * attempt):
                    if cancel_event.is_set():
                        return False
                    time.sleep(1)
            else:
                entry.message = f"Falha no download apos {MAX_ATTEMPTS} tentativas: {exc}"
                return False
    return False


def delete_entry_files(
    entry: FileEntry, output_dir: Path, provider: Optional["Provider"] = None
) -> list[Path]:
    """Remove do disco os arquivos ja obtidos para ``entry`` (baixado, .part e
    a copia descriptografada, quando houver — ver Provider.removable_paths),
    sem tocar no restante do lote. Retorna os caminhos removidos."""
    if provider is None:
        from providers import get_provider

        provider = get_provider()

    removed: list[Path] = []
    for path in provider.removable_paths(entry, Path(output_dir)):
        if path.is_dir():
            shutil.rmtree(path)
            removed.append(path)
        elif path.is_file():
            path.unlink()
            removed.append(path)
    return removed


def process_entry(
    entry: FileEntry,
    config: PipelineConfig,
    session: requests.Session,
    cancel_event: threading.Event,
    log: Callable[[str], None],
    on_update: Callable[[FileEntry], None],
    on_progress: Optional[Callable[[FileEntry, int, int, str], None]] = None,
) -> None:
    provider = config.provider
    dest = provider.local_path(entry, config.output_dir)
    unpacked_path = provider.unpacked_path(entry, config.output_dir)
    needs_unpack = config.unpack and provider.has_unpack_step and provider.needs_unpack(entry)
    thread_name = threading.current_thread().name

    if needs_unpack and unpacked_path.exists():
        entry.status = "concluido"
        entry.message = provider.unpack_done_message
        on_update(entry)
        return

    if cancel_event.is_set():
        entry.status = "cancelado"
        on_update(entry)
        return

    # --- download (etapa opcional, com retomada: pula se ja existe) ---
    if config.download:
        if dest.exists():
            entry.message = "arquivo ja baixado"
            size = dest.stat().st_size
            if on_progress:
                on_progress(entry, size, size, "download")
            log(log_line("DEBUG", "download", "arquivo ja existe, pulando", file=entry.file_name, bytes=size))
        else:
            entry.status = "baixando"
            on_update(entry)
            log(log_line("INFO", "download", "iniciando download", file=entry.file_name, thread=thread_name, url=entry.file_link))
            ok = provider.fetch(session, entry, dest, cancel_event, log, on_progress=on_progress)
            if cancel_event.is_set():
                entry.status = "cancelado"
                on_update(entry)
                log(log_line("WARN", "download", "cancelado pelo usuario", file=entry.file_name))
                return
            if not ok:
                entry.status = "erro"
                on_update(entry)
                log(log_line("ERROR", "download", "falha definitiva", file=entry.file_name, motivo=entry.message))
                return
    elif not dest.exists():
        entry.status = "erro"
        entry.message = "arquivo nao encontrado (etapa de download desmarcada)"
        on_update(entry)
        log(log_line("ERROR", "download", "arquivo ausente", file=entry.file_name, motivo=entry.message))
        return

    # --- verificacao de hash (etapa opcional) ---
    if config.verify:
        if entry.expected_hash:
            entry.status = "conferindo"
            on_update(entry)
            hash_started = time.monotonic()
            hash_progress = (
                (lambda done, total: on_progress(entry, done, total, "hash"))
                if on_progress
                else None
            )
            try:
                hash_calculado = provider.hash_entry(entry, config.output_dir, on_progress=hash_progress)
            except Exception as exc:  # noqa: BLE001 - arquivo ilegivel/corrompido nao derruba o lote
                entry.status = "erro"
                entry.message = f"falha ao calcular o hash: {exc}"
                on_update(entry)
                log(log_line("ERROR", "hash", "falha ao calcular o hash", file=entry.file_name, erro=str(exc)))
                return
            hash_duration = time.monotonic() - hash_started
            dest_stat = dest.stat()
            entry.computed_hash = hash_calculado
            entry.computed_hash_stat = (dest_stat.st_size, dest_stat.st_mtime)
            if hash_calculado.lower() != entry.expected_hash.lower():
                entry.status = "hash_invalido"
                entry.message = (
                    f"hash esperado {entry.expected_hash} != calculado {hash_calculado}"
                )
                on_update(entry)
                log(
                    log_line(
                        "ERROR", "hash", "divergencia de integridade",
                        file=entry.file_name, esperado=entry.expected_hash,
                        calculado=hash_calculado, duracao=f"{hash_duration:.2f}s",
                    )
                )
                return
            log(
                log_line(
                    "OK", "hash", "integridade confirmada",
                    file=entry.file_name, algoritmo=entry.hash_algorithm, hash=hash_calculado,
                    duracao=f"{hash_duration:.2f}s",
                )
            )
        else:
            log(log_line("DEBUG", "hash", "sem hash informado pelo provedor, verificacao pulada", file=entry.file_name))
    else:
        log(log_line("DEBUG", "hash", "etapa desmarcada pelo usuario", file=entry.file_name))

    # --- descriptografia (etapa opcional) ---
    if needs_unpack:
        entry.status = provider.unpack_status
        on_update(entry)
        log(
            log_line(
                "INFO", "pipeline", f"iniciando etapa: {provider.unpack_label.lower()}",
                file=entry.file_name, thread=thread_name,
            )
        )
        ok = provider.unpack(entry, dest, unpacked_path, config, log)
        if not ok:
            entry.status = "erro"
            on_update(entry)
            return

    entry.status = "concluido"
    entry.message = entry.message or "ok"
    on_update(entry)
    log(log_line("OK", "pipeline", "arquivo finalizado", file=entry.file_name, status=entry.status))


def run_pipeline(
    config: PipelineConfig,
    entries: list[FileEntry],
    on_update: Callable[[FileEntry], None],
    log: Callable[[str], None],
    cancel_event: Optional[threading.Event] = None,
    on_progress: Optional[Callable[[FileEntry, int, int, str], None]] = None,
) -> PipelineSummary:
    if cancel_event is None:
        cancel_event = threading.Event()

    config.provider.preflight(config, entries)

    config.output_dir.mkdir(parents=True, exist_ok=True)
    if config.unpack and config.provider.has_unpack_step:
        config.unpacked_dir.mkdir(parents=True, exist_ok=True)

    log_lock = threading.Lock()

    def safe_log(message: str) -> None:
        with log_lock:
            with config.log_path.open("a", encoding="utf-8") as fh:
                fh.write(message + "\n")
        log(message)

    safe_log(
        log_line(
            "INFO", "pipeline", "iniciando lote",
            provedor=config.provider.key, arquivos=len(entries), workers=max(1, config.max_workers),
            baixar=config.download, verificar=config.verify, abrir=config.unpack,
        )
    )

    config.provider.prepare(config, entries, safe_log)

    start = time.monotonic()
    session = _make_session()

    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(
        max_workers=max(1, config.max_workers), thread_name_prefix="worker"
    ) as executor:
        futures = [
            executor.submit(
                process_entry, entry, config, session, cancel_event, safe_log, on_update, on_progress
            )
            for entry in entries
        ]
        for future in futures:
            future.result()  # propaga excecoes inesperadas em vez de engoli-las

    elapsed = time.monotonic() - start

    summary = PipelineSummary(total=len(entries), elapsed_seconds=elapsed)
    for entry in entries:
        if entry.status == "concluido":
            if "ja existia" in entry.message:
                summary.ja_existiam += 1
            summary.concluidos += 1
        elif entry.status == "hash_invalido":
            summary.hash_invalido += 1
        elif entry.status == "cancelado":
            summary.cancelados += 1
        elif entry.status == "erro":
            summary.erros += 1

    safe_log(
        log_line(
            "INFO", "pipeline", "lote finalizado",
            concluidos=f"{summary.concluidos}/{summary.total}",
            ja_existiam=summary.ja_existiam, hash_invalido=summary.hash_invalido,
            erros=summary.erros, cancelados=summary.cancelados,
            duracao=f"{summary.elapsed_seconds:.1f}s",
        )
    )

    return summary
