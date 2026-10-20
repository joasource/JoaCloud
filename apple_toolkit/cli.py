"""
JoaKApple - interface de linha de comando (Linux).

Cobre as mesmas funcionalidades da GUI (baixar, verificar, descriptografar,
listar, excluir e gerar o Termo de Recebimento), para qualquer
provedor (--provider apple|google), sem depender de
tkinter/customtkinter, para rodar em qualquer maquina Linux mesmo sem
X11/Tk instalado (ex.: servidor, container). So existe no build Linux: o
.exe do Windows continua sendo compilado a partir de gui.py, ver
CHANGELOG/.github/workflows/release.yml.

Autor: Joaquim Ferreira Silva Neto <joaquimfsneto@gmail.com>

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

import argparse
import fnmatch
import getpass
import os
import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import core
import providers
import report

EXIT_OK = 0
EXIT_PENDING = 1
EXIT_FATAL = 2
EXIT_CANCELLED = 130

_LEVEL_RE = re.compile(r"\[(DEBUG|INFO|OK|WARN|ERROR)\s*\]")
_ANSI_BY_LEVEL = {
    "DEBUG": "\033[90m",
    "INFO": "\033[36m",
    "OK": "\033[32m",
    "WARN": "\033[33m",
    "ERROR": "\033[31m",
}
_ANSI_RESET = "\033[0m"

_YES_ANSWERS = {"s", "sim", "y", "yes"}


def _colorize(message: str) -> str:
    match = _LEVEL_RE.search(message)
    if not match:
        return message
    color = _ANSI_BY_LEVEL.get(match.group(1))
    if not color:
        return message
    return f"{color}{message}{_ANSI_RESET}"


def _use_color(args: argparse.Namespace) -> bool:
    if getattr(args, "no_color", False):
        return False
    return sys.stdout.isatty()


# ------------------------------------------------------------------- prompts

def _prompt(label: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{label}{suffix}: ").strip()
    return value or default


def _prompt_required(label: str) -> str:
    while True:
        value = input(f"{label}: ").strip()
        if value:
            return value
        print("  (obrigatorio)")


def _prompt_bool(label: str, default: bool) -> bool:
    suffix = "S/n" if default else "s/N"
    value = input(f"{label} ({suffix}): ").strip().lower()
    if not value:
        return default
    return value in _YES_ANSWERS


def _menu_select_files(entries: list[core.FileEntry], preselected: list[core.FileEntry]) -> list[core.FileEntry]:
    preselected_names = {e.file_name for e in preselected}
    marked = {e.file_name: (e.file_name in preselected_names) for e in entries}

    while True:
        print("\nARQUIVOS — numero(s) p/ marcar/desmarcar, 'a'=todos, 'n'=nenhum, Enter confirma:")
        for i, entry in enumerate(entries, start=1):
            mark = "x" if marked[entry.file_name] else " "
            print(f"  [{mark}] {i:>2}. {entry.file_name}")
        choice = input("> ").strip().lower()
        if choice == "":
            break
        if choice == "a":
            for name in marked:
                marked[name] = True
            continue
        if choice == "n":
            for name in marked:
                marked[name] = False
            continue
        for token in choice.split():
            if token.isdigit():
                idx = int(token) - 1
                if 0 <= idx < len(entries):
                    name = entries[idx].file_name
                    marked[name] = not marked[name]

    return [e for e in entries if marked[e.file_name]]


# --------------------------------------------------------------- selecao/senha

def _split_names(value: Optional[str]) -> set[str]:
    if not value:
        return set()
    return {n.strip() for n in value.split(",") if n.strip()}


def _select_entries(
    entries: list[core.FileEntry],
    only: Optional[str],
    exclude: Optional[str],
    pattern: Optional[str],
) -> list[core.FileEntry]:
    selected = entries
    only_names = _split_names(only)
    if only_names:
        selected = [e for e in selected if e.file_name in only_names]
    exclude_names = _split_names(exclude)
    if exclude_names:
        selected = [e for e in selected if e.file_name not in exclude_names]
    if pattern:
        selected = [e for e in selected if fnmatch.fnmatch(e.file_name, pattern)]
    return selected


def _resolve_passphrase(args: argparse.Namespace) -> str:
    if args.passphrase_env:
        value = os.environ.get(args.passphrase_env)
        if not value:
            raise core.PipelineError(
                f"Variavel de ambiente '{args.passphrase_env}' nao definida ou vazia."
            )
        return value
    if args.passphrase_stdin:
        line = sys.stdin.readline()
        if not line:
            raise core.PipelineError("Nenhuma senha recebida via stdin (--passphrase-stdin).")
        return line.rstrip("\n")
    if sys.stdin.isatty():
        return getpass.getpass("Senha GPG: ")
    raise core.PipelineError(
        "Nenhuma fonte de senha GPG disponivel (sem terminal para prompt, sem --passphrase-env, "
        "sem --passphrase-stdin) e a descriptografia esta ativada."
    )


# ------------------------------------------------------------ progresso no terminal

class TerminalReporter:
    """Log + status ao vivo no terminal, reaproveitando core.ProgressTracker/
    aggregate_progress/format_eta (mesma conta que a GUI usa)."""

    def __init__(self, total_entries: int, use_color: bool, quiet: bool):
        self.total_entries = max(1, total_entries)
        self.done_entries = 0
        self.use_color = use_color
        self.quiet = quiet
        self.start_time = time.monotonic()
        self.file_progress: dict[str, core.ProgressEvent] = {}
        self._trackers: dict[str, core.ProgressTracker] = {}
        self._last_line_count = 0
        self._lock = threading.Lock()

    def log(self, message: str) -> None:
        text = _colorize(message) if self.use_color else message
        if self.quiet:
            print(text)
            return
        with self._lock:
            self._clear_block()
            print(text)
            self._redraw_locked()

    def on_progress(
        self, entry: core.FileEntry, bytes_done: int, bytes_total: int, phase: str = "download"
    ) -> None:
        if self.quiet:
            return
        with self._lock:
            self.file_progress[entry.file_name] = core.ProgressEvent(
                entry.file_name, bytes_done, bytes_total, phase=phase
            )
            self._redraw_locked()

    def on_update(self, entry: core.FileEntry) -> None:
        if entry.status not in ("concluido", "hash_invalido", "erro", "cancelado"):
            return
        with self._lock:
            self.done_entries += 1
            self.file_progress.pop(entry.file_name, None)
            self._trackers.pop(entry.file_name, None)
            if not self.quiet:
                self._redraw_locked()

    def finish(self) -> None:
        if self.quiet:
            return
        with self._lock:
            self._clear_block()

    # -- internos (chamar sempre com self._lock adquirido) --

    def _clear_block(self) -> None:
        if not self._last_line_count:
            return
        out = sys.stdout
        out.write(f"\033[{self._last_line_count}A")
        for _ in range(self._last_line_count):
            out.write("\033[2K\n")
        out.write(f"\033[{self._last_line_count}A")
        out.flush()
        self._last_line_count = 0

    def _redraw_locked(self) -> None:
        lines = []
        for name, ev in self.file_progress.items():
            tracker = self._trackers.setdefault(name, core.ProgressTracker())
            estimate = tracker.update(ev.bytes_done, ev.bytes_total)
            verb = "baixando" if ev.phase == "download" else "conferindo hash"
            if ev.bytes_total > 0:
                pct = int(min(1.0, ev.bytes_done / ev.bytes_total) * 100)
                speed_text = (
                    f"{report.humanize_bytes(estimate.speed_bps)}/s" if estimate.speed_bps > 0 else "calculando velocidade…"
                )
                eta_text = core.format_eta(estimate.eta_seconds) if estimate.eta_seconds is not None else "--:--"
                lines.append(
                    f"  {name}: {verb} {pct}%  {report.humanize_bytes(ev.bytes_done)}/{report.humanize_bytes(ev.bytes_total)}"
                    f"  ·  {speed_text}  ·  ETA {eta_text}"
                )
            else:
                lines.append(f"  {name}: {report.humanize_bytes(ev.bytes_done)}  ·  {verb}…")

        # Agregado do lote so soma progresso de download — hash e' rapido/local e
        # misturar os dois faria a barra agregada "voltar" quando a fase muda.
        download_progress = {name: ev for name, ev in self.file_progress.items() if ev.phase == "download"}
        agg = core.aggregate_progress(download_progress, self.start_time)
        pct_files = int(round(self.done_entries / self.total_entries * 100))
        parts = [f"Progresso: {self.done_entries}/{self.total_entries} arquivos ({pct_files}%)"]
        if agg["bytes_known"] > 0:
            parts.append(f"{report.humanize_bytes(agg['bytes_done'])}/{report.humanize_bytes(agg['bytes_known'])}")
        parts.append(f"↓ {report.humanize_bytes(agg['speed_bps'])}/s" if agg["speed_bps"] > 0 else "↓ calculando…")
        if agg["eta_seconds"] is not None:
            parts.append(f"restante ~{core.format_eta(agg['eta_seconds'])}")
        lines.append("  ·  ".join(parts))

        out = sys.stdout
        for line in lines:
            out.write(line + "\n")
        out.flush()
        self._last_line_count = len(lines)


# --------------------------------------------------------------------- run

def _menu_run_params(
    args: argparse.Namespace, provider
) -> tuple[Path, Path, bool, bool, bool, int, list[core.FileEntry]]:
    csv_path: Optional[Path] = args.csv
    while True:
        if not csv_path:
            csv_path = Path(_prompt_required(provider.source_label))
        try:
            entries = provider.load_entries(csv_path)
            break
        except core.PipelineError as exc:
            print(f"Erro: {exc}", file=sys.stderr)
            csv_path = None

    output_dir = args.output_dir or Path(_prompt_required("Pasta de destino"))

    print("\nETAPAS DO PIPELINE")
    download = _prompt_bool("Baixar", args.download)
    verify = _prompt_bool("Verificar hash", args.verify)
    decrypt = _prompt_bool(provider.unpack_label, args.decrypt) if provider.has_unpack_step else False

    workers_str = _prompt("Downloads simultaneos", str(args.workers))
    try:
        workers = max(1, int(workers_str))
    except ValueError:
        workers = args.workers

    preselected = _select_entries(entries, args.only, args.exclude, args.pattern)
    selected = _menu_select_files(entries, preselected)

    return csv_path, output_dir, download, verify, decrypt, workers, selected


def _cmd_run(args: argparse.Namespace) -> int:
    use_color = _use_color(args)
    provider = providers.get_provider(args.provider)
    interactive = args.menu or not (args.csv and args.output_dir)

    try:
        if interactive:
            csv_path, output_dir, download, verify, decrypt, workers, selected = _menu_run_params(args, provider)
        else:
            csv_path, output_dir = args.csv, args.output_dir
            try:
                entries = provider.load_entries(csv_path)
            except core.PipelineError as exc:
                print(f"Erro: {exc}", file=sys.stderr)
                return EXIT_FATAL
            download, verify, decrypt = args.download, args.verify, args.decrypt
            workers = max(1, args.workers)
            selected = _select_entries(entries, args.only, args.exclude, args.pattern)
    except KeyboardInterrupt:
        print()
        return EXIT_CANCELLED
    except EOFError:
        print(
            "\nErro: informacao obrigatoria ausente e nao ha terminal interativo para perguntar.",
            file=sys.stderr,
        )
        return EXIT_FATAL

    decrypt = decrypt and provider.has_unpack_step
    if not (download or verify or decrypt):
        print("Erro: selecione ao menos uma etapa (--download/--verify/--decrypt).", file=sys.stderr)
        return EXIT_FATAL

    if not selected:
        print("Erro: nenhum arquivo selecionado.", file=sys.stderr)
        return EXIT_FATAL

    passphrase = ""
    if decrypt and any(provider.needs_unpack(e) for e in selected):
        missing_tool = provider.missing_tool_message()
        if missing_tool:
            print(f"Erro: {missing_tool}", file=sys.stderr)
            return EXIT_FATAL
    if decrypt and provider.needs_passphrase and any(provider.needs_unpack(e) for e in selected):
        try:
            passphrase = _resolve_passphrase(args)
        except core.PipelineError as exc:
            print(f"Erro: {exc}", file=sys.stderr)
            return EXIT_FATAL
        except KeyboardInterrupt:
            print()
            return EXIT_CANCELLED
        except EOFError:
            print("\nErro: nao foi possivel ler a senha (sem terminal interativo).", file=sys.stderr)
            return EXIT_FATAL

    if interactive and not args.yes:
        print(
            f"\nResumo: {provider.display_name}  ·  {len(selected)} arquivo(s)  ·  baixar={download}  ·  "
            f"verificar={verify}"
            + (f"  ·  {provider.unpack_label.lower()}={decrypt}" if provider.has_unpack_step else "")
            + f"  ·  workers={workers}"
        )
        try:
            if not _prompt_bool("Iniciar", True):
                print("Cancelado.")
                return EXIT_CANCELLED
        except KeyboardInterrupt:
            print()
            return EXIT_CANCELLED
        except EOFError:
            print("\nErro: sem terminal interativo para confirmar.", file=sys.stderr)
            return EXIT_FATAL

    config = core.PipelineConfig(
        source=csv_path,
        output_dir=output_dir,
        passphrase=passphrase,
        max_workers=workers,
        download=download,
        verify=verify,
        unpack=decrypt,
        provider=provider,
    )

    reporter = TerminalReporter(len(selected), use_color=use_color, quiet=args.quiet)
    cancel_event = threading.Event()

    try:
        summary = core.run_pipeline(
            config,
            selected,
            on_update=reporter.on_update,
            log=reporter.log,
            cancel_event=cancel_event,
            on_progress=reporter.on_progress,
        )
    except KeyboardInterrupt:
        cancel_event.set()
        reporter.finish()
        print("\nCancelado pelo usuario.", file=sys.stderr)
        return EXIT_CANCELLED
    except core.PipelineError as exc:
        reporter.finish()
        print(f"Erro: {exc}", file=sys.stderr)
        return EXIT_FATAL

    reporter.finish()
    print(
        f"Concluidos: {summary.concluidos}/{summary.total}  ·  "
        f"Hash invalido: {summary.hash_invalido}  ·  Erros: {summary.erros}  ·  "
        f"Cancelados: {summary.cancelados}  ·  ({summary.elapsed_seconds:.1f}s)"
    )
    if summary.erros or summary.hash_invalido:
        return EXIT_PENDING
    return EXIT_OK


# -------------------------------------------------------------------- list

def _cmd_list(args: argparse.Namespace) -> int:
    provider = providers.get_provider(args.provider)
    csv_path = args.csv
    try:
        if args.menu or not csv_path:
            csv_path = csv_path or Path(_prompt_required(provider.source_label))
    except KeyboardInterrupt:
        print()
        return EXIT_CANCELLED
    except EOFError:
        print("\nErro: --csv ausente e nao ha terminal interativo para perguntar.", file=sys.stderr)
        return EXIT_FATAL

    try:
        entries = provider.load_entries(csv_path)
    except core.PipelineError as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return EXIT_FATAL

    output_dir = args.output_dir
    for entry in entries:
        tags = []
        if entry.is_encrypted:
            tags.append("gpg")
        if output_dir and (Path(output_dir) / entry.file_name).exists():
            tags.append("ja em disco")
        tag_text = f"  [{', '.join(tags)}]" if tags else ""
        hash_info = entry.expected_hash or "(sem hash informado pelo provedor)"
        print(f"{entry.file_name}{tag_text}  ·  {entry.file_link}  ·  {hash_info}")

    print(f"\n{len(entries)} arquivo(s) no total.")
    return EXIT_OK


# ------------------------------------------------------------------ delete

def _cmd_delete(args: argparse.Namespace) -> int:
    provider = providers.get_provider(args.provider)
    csv_path = args.csv
    output_dir = args.output_dir
    interactive = args.menu or not (csv_path and output_dir and args.only)

    try:
        if not csv_path:
            csv_path = Path(_prompt_required(provider.source_label))
        entries = provider.load_entries(csv_path)
        if not output_dir:
            output_dir = Path(_prompt_required("Pasta de destino"))

        if interactive:
            preselected = _select_entries(entries, args.only, None, None) if args.only else []
            selected = _menu_select_files(entries, preselected)
        else:
            selected = _select_entries(entries, args.only, None, None)
    except core.PipelineError as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return EXIT_FATAL
    except KeyboardInterrupt:
        print()
        return EXIT_CANCELLED
    except EOFError:
        print("\nErro: informacao obrigatoria ausente e nao ha terminal interativo para perguntar.", file=sys.stderr)
        return EXIT_FATAL

    if not selected:
        print("Erro: nenhum arquivo selecionado para excluir (use --only ou --menu).", file=sys.stderr)
        return EXIT_FATAL

    if not args.yes:
        names = ", ".join(e.file_name for e in selected)
        try:
            confirm = input(
                f"Excluir do disco os arquivos ja obtidos (e a copia aberta) de: {names}\n"
                "Essa acao nao pode ser desfeita. Confirma? (s/N): "
            ).strip().lower()
        except KeyboardInterrupt:
            print()
            return EXIT_CANCELLED
        except EOFError:
            print("\nErro: sem terminal interativo para confirmar.", file=sys.stderr)
            return EXIT_FATAL
        if confirm not in _YES_ANSWERS:
            print("Cancelado.")
            return EXIT_CANCELLED

    total_removed = 0
    for entry in selected:
        removed = core.delete_entry_files(entry, Path(output_dir), provider)
        total_removed += len(removed)
        status = f"{len(removed)} arquivo(s) removido(s)" if removed else "nada encontrado em disco"
        print(f"{entry.file_name}: {status}")

    print(f"\nTotal: {total_removed} arquivo(s) removido(s) de {len(selected)} entrada(s).")
    return EXIT_OK


# ------------------------------------------------------------------ report

def _cmd_report(args: argparse.Namespace) -> int:
    provider = providers.get_provider(args.provider)
    interactive = args.menu
    now = datetime.now()

    values = {
        "numero_processo": args.processo or "",
        "numero_pic": args.pic or "",
        "orgao_execucao": args.orgao or "",
        "data": args.data or now.strftime("%d/%m/%Y"),
        "hora": args.hora or now.strftime("%H:%M"),
        "responsavel": args.responsavel or "",
        "matricula": args.matricula or "",
        "recebido_portal": args.portal,
        "recebido_email": args.email,
        "procedimento_referenciado": args.procedimento_ref or "",
        "id_documento": args.id_documento or "",
        "disponibiliza_original": args.original,
        "disponibiliza_processada": args.processada,
    }
    fmt = args.format
    out_path = args.out
    csv_path = args.csv
    output_dir = args.output_dir
    selected_entries: list[core.FileEntry] = []

    if fmt == "docx" and not out_path and not interactive:
        print("Erro: --out e obrigatorio para --format docx.", file=sys.stderr)
        return EXIT_FATAL

    try:
        if interactive:
            print("1. IDENTIFICACAO DO CASO")
            values["numero_processo"] = _prompt("Numero do Processo Judicial", values["numero_processo"])
            values["numero_pic"] = _prompt("PIC/Inquerito", values["numero_pic"])
            values["orgao_execucao"] = _prompt("Orgao de Execucao", values["orgao_execucao"])

            print("\n2. DADOS DA RECEPCAO")
            values["data"] = _prompt("Data", values["data"])
            values["hora"] = _prompt("Hora", values["hora"])
            values["responsavel"] = _prompt("Responsavel", values["responsavel"])
            values["matricula"] = _prompt("Matricula", values["matricula"])
            values["recebido_portal"] = _prompt_bool("Recebido via Portal do Provedor", values["recebido_portal"])
            values["recebido_email"] = _prompt_bool("Recebido via E-mail Oficial", values["recebido_email"])

            print("\n3. ESPECIFICACOES TECNICAS")
            if not csv_path:
                csv_str = _prompt(f"{provider.source_label} (opcional, p/ calcular hashes)", "")
                csv_path = Path(csv_str) if csv_str else None
            if not output_dir:
                out_dir_str = _prompt("Pasta de destino (opcional, p/ calcular hashes)", "")
                output_dir = Path(out_dir_str) if out_dir_str else None
            if csv_path and output_dir:
                try:
                    entries = provider.load_entries(Path(csv_path))
                except core.PipelineError as exc:
                    print(f"Erro: {exc}", file=sys.stderr)
                    return EXIT_FATAL
                preselected = _select_entries(entries, args.only, args.exclude, args.pattern)
                selected_entries = _menu_select_files(entries, preselected)
            values["procedimento_referenciado"] = _prompt(
                "Procedimento referenciado (autos)", values["procedimento_referenciado"]
            )
            values["id_documento"] = _prompt("ID do documento", values["id_documento"])

            print("\n5. DISPONIBILIZACAO")
            values["disponibiliza_original"] = _prompt_bool(
                "Copia da Aquisicao Forense Original", values["disponibiliza_original"]
            )
            values["disponibiliza_processada"] = _prompt_bool(
                "Copia Processada/Indexada", values["disponibiliza_processada"]
            )

            fmt = _prompt("Formato de saida (md/html/docx)", fmt)
            if fmt not in ("md", "html", "docx"):
                print("Erro: formato invalido.", file=sys.stderr)
                return EXIT_FATAL
            if fmt == "docx" and not out_path:
                out_path = Path(_prompt_required("Caminho do arquivo .docx de saida"))
    except KeyboardInterrupt:
        print()
        return EXIT_CANCELLED
    except EOFError:
        print("\nErro: sem terminal interativo para o menu do relatorio.", file=sys.stderr)
        return EXIT_FATAL

    if fmt == "docx" and not out_path:
        print("Erro: --out e obrigatorio para --format docx.", file=sys.stderr)
        return EXIT_FATAL

    hash_rows = []
    if csv_path and output_dir:
        if interactive:
            selected = selected_entries
        else:
            try:
                entries = provider.load_entries(Path(csv_path))
            except core.PipelineError as exc:
                print(f"Erro: {exc}", file=sys.stderr)
                return EXIT_FATAL
            selected = _select_entries(entries, args.only, args.exclude, args.pattern)
        hash_rows = report.compute_hash_rows(selected, Path(output_dir), provider)

    data = report.ReportData(
        numero_processo=values["numero_processo"],
        numero_pic=values["numero_pic"],
        orgao_execucao=values["orgao_execucao"],
        data=values["data"],
        hora=values["hora"],
        responsavel=values["responsavel"],
        matricula=values["matricula"],
        recebido_portal=values["recebido_portal"],
        recebido_email=values["recebido_email"],
        procedimento_referenciado=values["procedimento_referenciado"],
        id_documento=values["id_documento"],
        disponibiliza_original=values["disponibiliza_original"],
        disponibiliza_processada=values["disponibiliza_processada"],
        hash_rows=hash_rows,
        **report.provider_fields(provider),
    )

    if fmt == "docx":
        try:
            report.build_docx(data, Path(out_path))
        except ImportError:
            print("Erro: a biblioteca 'python-docx' nao esta instalada.", file=sys.stderr)
            return EXIT_FATAL
        print(f"Termo salvo em: {out_path}")
        return EXIT_OK

    text = report.render_report_html(data) if fmt == "html" else report.render_report(data)
    if out_path:
        Path(out_path).write_text(text, encoding="utf-8")
        print(f"Termo salvo em: {out_path}")
    else:
        print(text)
    return EXIT_OK


# ----------------------------------------------------------------- argparse

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="joakapple",
        description="JoaKApple (modo texto, Linux) — baixa, verifica, descriptografa e gera o "
        "Termo de Recebimento de retorno de oficios judiciais (Apple, Google).",
    )
    sub = parser.add_subparsers(dest="command")

    def add_provider_args(p: argparse.ArgumentParser, source_help: Optional[str] = None) -> None:
        p.add_argument(
            "--provider", choices=sorted(providers.PROVIDERS), default=providers.DEFAULT_PROVIDER,
            help="Nuvem de origem (default: apple)",
        )
        p.add_argument(
            "--csv", "--source", dest="csv", type=Path, default=None,
            help=source_help or "CSV da Apple, ou pasta com o retorno do LERS (--provider google)",
        )

    run_p = sub.add_parser("run", help="Executa o pipeline (baixar/verificar/descriptografar).")
    add_provider_args(run_p)
    run_p.add_argument("--output-dir", type=Path, default=None, help="Pasta de destino")
    run_p.add_argument("--download", action=argparse.BooleanOptionalAction, default=True)
    run_p.add_argument("--verify", action=argparse.BooleanOptionalAction, default=True)
    run_p.add_argument(
        "--decrypt", dest="decrypt", action=argparse.BooleanOptionalAction, default=True,
        help="Etapa 3 (so' Apple): descriptografar. O Google nao tem etapa 3: os ZIPs ficam originais",
    )
    run_p.add_argument("--workers", type=int, default=4, help="Downloads simultaneos")
    run_p.add_argument("--only", default=None, help="Nomes de arquivo separados por virgula")
    run_p.add_argument("--exclude", default=None, help="Nomes de arquivo separados por virgula")
    run_p.add_argument("--pattern", default=None, help="Glob (fnmatch) sobre o nome do arquivo")
    pass_group = run_p.add_mutually_exclusive_group()
    pass_group.add_argument("--passphrase-env", default=None, help="Variavel de ambiente com a senha GPG")
    pass_group.add_argument("--passphrase-stdin", action="store_true", help="Le a senha GPG de uma linha do stdin")
    run_p.add_argument("--yes", "-y", action="store_true", help="Pula confirmacoes")
    run_p.add_argument("--menu", "-i", action="store_true", help="Forca o menu interativo")
    run_p.add_argument("--quiet", action="store_true", help="So imprime o log estruturado, sem status ao vivo")
    run_p.add_argument("--no-color", action="store_true", help="Desliga cores ANSI no log")
    run_p.set_defaults(handler=_cmd_run)

    list_p = sub.add_parser("list", help="Lista os arquivos da fonte (CSV/pasta), sem baixar nada.")
    add_provider_args(list_p)
    list_p.add_argument("--output-dir", type=Path, default=None)
    list_p.add_argument("--menu", "-i", action="store_true")
    list_p.set_defaults(handler=_cmd_list)

    delete_p = sub.add_parser("delete", help="Remove do disco os arquivos ja obtidos de uma selecao.")
    add_provider_args(delete_p)
    delete_p.add_argument("--output-dir", type=Path, default=None)
    delete_p.add_argument("--only", default=None, help="Nomes de arquivo separados por virgula (obrigatorio)")
    delete_p.add_argument("--yes", "-y", action="store_true", help="Pula a confirmacao")
    delete_p.add_argument("--menu", "-i", action="store_true")
    delete_p.set_defaults(handler=_cmd_delete)

    report_p = sub.add_parser("report", help="Gera o Termo de Recebimento.")
    report_p.add_argument("--processo", default=None)
    report_p.add_argument("--pic", default=None)
    report_p.add_argument("--orgao", default=None)
    report_p.add_argument("--data", default=None, help="Default: hoje")
    report_p.add_argument("--hora", default=None, help="Default: agora")
    report_p.add_argument("--responsavel", default=None, help="Sem default — nunca pre-preencher identidade")
    report_p.add_argument("--matricula", default=None)
    report_p.add_argument("--portal", action=argparse.BooleanOptionalAction, default=False)
    report_p.add_argument("--email", action=argparse.BooleanOptionalAction, default=False)
    report_p.add_argument("--procedimento-ref", default=None)
    report_p.add_argument("--id-documento", default=None)
    report_p.add_argument("--original", action=argparse.BooleanOptionalAction, default=False)
    report_p.add_argument("--processada", action=argparse.BooleanOptionalAction, default=False)
    add_provider_args(report_p, source_help="Para calcular hashes (CSV da Apple ou pasta do LERS)")
    report_p.add_argument("--output-dir", type=Path, default=None, help="Para calcular hashes")
    report_p.add_argument("--only", default=None, help="Nomes de arquivo separados por virgula")
    report_p.add_argument("--exclude", default=None, help="Nomes de arquivo separados por virgula")
    report_p.add_argument("--pattern", default=None, help="Glob (fnmatch) sobre o nome do arquivo")
    report_p.add_argument("--format", choices=["md", "html", "docx"], default="md")
    report_p.add_argument("--out", type=Path, default=None, help="Obrigatorio para --format docx")
    report_p.add_argument("--menu", "-i", action="store_true")
    report_p.set_defaults(handler=_cmd_report)

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not getattr(args, "command", None):
        parser.print_help()
        return EXIT_FATAL

    try:
        return args.handler(args)
    except KeyboardInterrupt:
        print()
        return EXIT_CANCELLED


if __name__ == "__main__":
    raise SystemExit(main())
