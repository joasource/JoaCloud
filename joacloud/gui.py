"""
JoaCloud - interface grafica.

Toolkit para baixar, verificar e descriptografar retorno de oficios
judiciais (Apple, Google).

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

import os
import queue
import re
import sys
import threading
import time
from datetime import datetime
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

import core
import lers
import providers
import report

APP_TITLE = "JoaCloud"
APP_DESCRIPTION = "Toolkit para baixar, verificar e abrir retorno de ofícios judiciais — Apple e Google"
APP_VERSION = "2.1.0"
AUTHOR_LINE = "Joaquim Ferreira Silva Neto  ·  joaquimfsneto@gmail.com"


def _icon_image_path() -> Path | None:
    """Caminho do PNG do icone (embutido pelo PyInstaller ou no repo em dev)."""
    base = getattr(sys, "_MEIPASS", None)
    candidate = Path(base) / "icon.png" if base else Path(__file__).resolve().parent.parent / "packaging" / "linux" / "icon.png"
    return candidate if candidate.is_file() else None

ACCENT = "#0F6B62"
ACCENT_HOVER = "#0A4A45"
ACCENT_TINT = "#E4F1EF"
BG_APP = "#EDEAE3"
BG_CARD = "#FBFAF7"
BORDER = "#DEDAD1"
BORDER_SOFT = "#ECE8DF"
TEXT_PRIMARY = "#1C1B18"
TEXT_SECONDARY = "#6B6862"
BADGE_INACTIVE_BG = "#EDEAE3"
LOG_BG = "#12110E"
LOG_HEADER_BG = "#1D1C17"
LOG_TEXT = "#D8D4C8"
LOG_TEXT_DIM = "#726E63"
LOG_OK = "#59C68A"
LOG_WARN = "#E3AE55"
LOG_ERROR = "#E56A5D"
LOG_INFO = "#8FB7C9"
DISABLED_BG = "#F5F3EE"
BUTTON_DISABLED_BG = "#C9C5BA"
DANGER_TEXT = "#9A2E22"
DANGER_HOVER = "#F6E4E1"
STATUS_OK_COLOR = "#2E8B57"
STATUS_ERROR_COLOR = "#C0392B"
STATUS_WARN_COLOR = "#B8860B"

STATUS_LABELS = {
    "pendente": "Pendente",
    "baixando": "Baixando...",
    "conferindo": "Conferindo hash...",
    "decriptando": "Descriptografando...",
    "hash_invalido": "HASH INVALIDO",
    "erro": "ERRO",
    "cancelado": "Cancelado",
    "concluido": "Concluído",
}

STATUS_ROW_COLOR = {
    "hash_invalido": STATUS_ERROR_COLOR,
    "erro": STATUS_ERROR_COLOR,
    "cancelado": STATUS_WARN_COLOR,
    "concluido": STATUS_OK_COLOR,
}

STEP_NUMBERS = {"download": "1", "verify": "2", "unpack": "3"}


def step_meta(provider, key: str) -> tuple[str, str, str]:
    """(numero, titulo, descricao) do card da etapa, conforme o provedor."""
    if key == "download":
        return STEP_NUMBERS[key], "Baixar", provider.download_description
    if key == "verify":
        return STEP_NUMBERS[key], "Verificar", provider.verify_description
    return STEP_NUMBERS[key], provider.unpack_label, provider.unpack_description

_LEVEL_TAG_RE = re.compile(r"\[(DEBUG|INFO|OK|WARN|ERROR)\s*\]")


def _build_cf_html(html_fragment: str) -> bytes:
    """Monta o payload no formato CF_HTML exigido pelo clipboard do Windows
    (cabecalho com offsets em bytes + marcadores StartFragment/EndFragment)."""
    header_template = (
        "Version:0.9\r\n"
        "StartHTML:{start_html:010d}\r\n"
        "EndHTML:{end_html:010d}\r\n"
        "StartFragment:{start_fragment:010d}\r\n"
        "EndFragment:{end_fragment:010d}\r\n"
    )
    prefix = "<html><body>\r\n<!--StartFragment-->"
    suffix = "<!--EndFragment-->\r\n</body></html>"

    start_html = len(header_template.format(start_html=0, end_html=0, start_fragment=0, end_fragment=0).encode("utf-8"))
    start_fragment = start_html + len(prefix.encode("utf-8"))
    end_fragment = start_fragment + len(html_fragment.encode("utf-8"))
    end_html = end_fragment + len(suffix.encode("utf-8"))

    header = header_template.format(
        start_html=start_html, end_html=end_html,
        start_fragment=start_fragment, end_fragment=end_fragment,
    )
    return (header + prefix + html_fragment + suffix).encode("utf-8")


def _set_windows_html_clipboard(plain_text: str, html_fragment: str) -> None:
    """Coloca texto simples (CF_UNICODETEXT) e HTML (formato "HTML Format") na area de
    transferencia do Windows via ctypes, para que o Word cole com a formatacao (negrito,
    titulos, tabela) em vez de markdown cru. So deve ser chamada com os.name == "nt"."""
    import ctypes
    from ctypes import wintypes

    GMEM_MOVEABLE = 0x0002
    CF_UNICODETEXT = 13

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.CloseClipboard.restype = wintypes.BOOL
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    user32.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
    user32.RegisterClipboardFormatW.restype = wintypes.UINT

    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]

    def put(fmt, data: bytes):
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not handle:
            raise OSError("GlobalAlloc falhou ao reservar memoria para a area de transferencia.")
        ptr = kernel32.GlobalLock(handle)
        ctypes.memmove(ptr, data, len(data))
        kernel32.GlobalUnlock(handle)
        if not user32.SetClipboardData(fmt, handle):
            raise OSError("SetClipboardData falhou.")

    if not user32.OpenClipboard(None):
        raise OSError("Nao foi possivel abrir a area de transferencia do Windows.")
    try:
        user32.EmptyClipboard()
        put(CF_UNICODETEXT, (plain_text + "\0").encode("utf-16-le"))
        cf_html = user32.RegisterClipboardFormatW("HTML Format")
        put(cf_html, _build_cf_html(html_fragment) + b"\0")
    finally:
        user32.CloseClipboard()


class StepCard(ctk.CTkFrame):
    def __init__(self, master, key: str, provider, on_toggle, **kwargs):
        super().__init__(
            master,
            fg_color=ACCENT_TINT,
            border_width=2,
            border_color=ACCENT,
            corner_radius=10,
            **kwargs,
        )
        self.key = key
        num, title, desc = step_meta(provider, key)
        self.title = title
        self.var = tk.BooleanVar(value=True)

        top = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(fill="x", padx=14, pady=(12, 4))

        self.badge = ctk.CTkLabel(
            top, text=num, width=22, height=22, corner_radius=11,
            fg_color=ACCENT, text_color="#FFFFFF",
            font=ctk.CTkFont(family="DejaVu Sans", size=12, weight="bold"),
        )
        self.badge.pack(side="left")

        self.title_label = ctk.CTkLabel(
            top, text=title, text_color=TEXT_PRIMARY,
            font=ctk.CTkFont(family="DejaVu Sans", size=14, weight="bold"),
        )
        self.title_label.pack(side="left", padx=(8, 0))

        self.switch = ctk.CTkSwitch(
            top, text="", variable=self.var, width=36,
            progress_color=ACCENT, button_color="#FFFFFF",
            command=lambda: on_toggle(self.key),
        )
        self.switch.pack(side="right")

        self.desc_label = ctk.CTkLabel(
            self, text=desc, text_color=TEXT_SECONDARY, justify="left",
            wraplength=200, font=ctk.CTkFont(family="DejaVu Sans", size=11),
        )
        self.desc_label.pack(fill="x", padx=14, pady=(0, 12))

    def set_provider(self, provider):
        _, self.title, desc = step_meta(provider, self.key)
        self.title_label.configure(text=self.title)
        self.desc_label.configure(text=desc)

    def set_active(self, active: bool):
        self.configure(
            fg_color=ACCENT_TINT if active else "#FFFFFF",
            border_color=ACCENT if active else BORDER,
        )
        self.badge.configure(
            fg_color=ACCENT if active else BADGE_INACTIVE_BG,
            text_color="#FFFFFF" if active else TEXT_SECONDARY,
        )


class FileRow(ctk.CTkFrame):
    """Uma linha da lista de arquivos: selecao, nome, progresso individual,
    status e um botao para excluir o que ja foi baixado/descriptografado."""

    def __init__(self, master, entry: core.FileEntry, on_delete, **kwargs):
        super().__init__(master, fg_color="transparent", **kwargs)
        self.entry = entry
        self._on_delete = on_delete
        self.selected_var = tk.BooleanVar(value=True)

        self.columnconfigure(1, weight=1)

        self.checkbox = ctk.CTkCheckBox(
            self, text="", variable=self.selected_var, width=18,
            checkbox_width=18, checkbox_height=18, fg_color=ACCENT, border_color=BORDER,
        )
        self.checkbox.grid(row=0, column=0, rowspan=2, padx=(10, 8), pady=8, sticky="n")

        self.name_label = ctk.CTkLabel(
            self, text=entry.file_name, text_color=TEXT_PRIMARY, anchor="w",
            font=ctk.CTkFont(family="DejaVu Sans", size=12, weight="bold"),
        )
        self.name_label.grid(row=0, column=1, sticky="we", pady=(8, 2))

        self.delete_button = ctk.CTkButton(
            self, text="Excluir", width=68, height=22, fg_color="#FFFFFF", hover_color=DANGER_HOVER,
            text_color=DANGER_TEXT, border_width=1, border_color=BORDER, corner_radius=6,
            font=ctk.CTkFont(family="DejaVu Sans", size=10),
            command=lambda: self._on_delete(self.entry),
        )
        self.delete_button.grid(row=0, column=2, padx=(8, 10), pady=(8, 2), sticky="e")

        self.progress = ctk.CTkProgressBar(
            self, progress_color=ACCENT, fg_color=BORDER_SOFT, height=6, corner_radius=3,
        )
        self.progress.set(0)
        self.progress.grid(row=1, column=1, sticky="we", padx=(0, 10), pady=(0, 8))

        self.status_var = tk.StringVar(value=STATUS_LABELS["pendente"])
        self.status_label = ctk.CTkLabel(
            self, textvariable=self.status_var, text_color=TEXT_SECONDARY, anchor="e",
            font=ctk.CTkFont(family="DejaVu Sans", size=10),
        )
        self.status_label.grid(row=1, column=2, padx=(0, 10), pady=(0, 8), sticky="e")

        self._tracker = core.ProgressTracker()

    def set_enabled(self, enabled: bool):
        state = "normal" if enabled else "disabled"
        self.checkbox.configure(state=state)
        self.delete_button.configure(state=state)

    def reset(self):
        self.progress.stop()
        self.progress.configure(mode="determinate", progress_color=ACCENT)
        self.progress.set(0)
        self.status_var.set(STATUS_LABELS["pendente"])
        self.status_label.configure(text_color=TEXT_SECONDARY)
        self._tracker = core.ProgressTracker()

    def update_progress(self, bytes_done: int, bytes_total: int, phase: str = "download"):
        estimate = self._tracker.update(bytes_done, bytes_total)
        speed = estimate.speed_bps
        speed_text = f"{report.humanize_bytes(speed)}/s" if speed > 0 else "calculando velocidade…"
        verb = "Baixando" if phase == "download" else "Conferindo hash"

        if bytes_total > 0:
            self.progress.stop()
            self.progress.configure(mode="determinate")
            fraction = min(1.0, bytes_done / bytes_total)
            self.progress.set(fraction)
            pct = int(fraction * 100)
            eta_text = core.format_eta(estimate.eta_seconds) if estimate.eta_seconds is not None else "--:--"
            self.status_var.set(
                f"{verb} {pct}%  ·  {report.humanize_bytes(bytes_done)}/{report.humanize_bytes(bytes_total)}"
                f"  ·  {speed_text}  ·  ETA {eta_text}"
            )
        else:
            self.progress.configure(mode="indeterminate")
            self.progress.start()
            self.status_var.set(f"{verb}  ·  {report.humanize_bytes(bytes_done)}  ·  {speed_text}")
        self.status_label.configure(text_color=ACCENT)

    def set_status(self, status: str, message: str = ""):
        label = STATUS_LABELS.get(status, status)
        self.progress.stop()
        self.progress.configure(mode="determinate")
        if status == "concluido":
            self.progress.set(1.0)
        elif status in ("erro", "hash_invalido", "cancelado"):
            self.progress.configure(progress_color=STATUS_ROW_COLOR.get(status, ACCENT))
        if message and status in ("erro", "hash_invalido"):
            short = message if len(message) <= 70 else message[:67] + "…"
            text = f"{label} — {short}"
        else:
            text = label
        self.status_var.set(text)
        self.status_label.configure(text_color=STATUS_ROW_COLOR.get(status, TEXT_SECONDARY))


class LersDialog(ctk.CTkToplevel):
    """Login no Google LERS pelo navegador e download de um request.

    O trabalho com o navegador (CDP) roda numa thread; a janela so' le a fila
    de eventos. O navegador fica aberto ao fechar a janela, para manter o
    login na proxima vez."""

    def __init__(self, master, default_parent: Path, on_downloaded, log_queue: "queue.Queue"):
        super().__init__(master)
        self.title("Baixar do LERS (Google)")
        self.geometry("720x560")
        self.configure(fg_color=BG_APP)
        self.transient(master)
        self.default_parent = default_parent
        self.on_downloaded = on_downloaded
        self.app_log_queue = log_queue
        self.events: queue.Queue = queue.Queue()
        self.cancel_event = threading.Event()
        self.client: lers.LersClient | None = None
        self.requests: list[lers.LersRequest] = []
        self.request_var = tk.IntVar(value=-1)
        self.include_legal_var = tk.BooleanVar(value=False)
        self.dest_var = tk.StringVar()
        self.status_var = tk.StringVar(value="Abrindo o navegador…")
        self.busy = False
        self.downloading = False

        ctk.CTkLabel(
            self, textvariable=self.status_var, text_color=TEXT_PRIMARY, justify="left", anchor="w",
            wraplength=680, font=ctk.CTkFont(family="DejaVu Sans", size=13),
        ).pack(fill="x", padx=16, pady=(16, 8))

        self.list_frame = ctk.CTkScrollableFrame(
            self, fg_color="#FFFFFF", border_width=1, border_color=BORDER_SOFT, corner_radius=10,
        )
        self.list_frame.pack(fill="both", expand=True, padx=16, pady=4)

        opts = ctk.CTkFrame(self, fg_color="transparent")
        opts.pack(fill="x", padx=16, pady=(8, 0))
        ctk.CTkCheckBox(
            opts, text="Incluir também o ofício enviado pelo órgão (LEGAL_PROCESS)",
            variable=self.include_legal_var, checkbox_width=16, checkbox_height=16,
            fg_color=ACCENT, text_color=TEXT_SECONDARY, font=ctk.CTkFont(family="DejaVu Sans", size=11),
        ).pack(anchor="w")

        dest = ctk.CTkFrame(self, fg_color="transparent")
        dest.pack(fill="x", padx=16, pady=(8, 0))
        ctk.CTkLabel(
            dest, text="SALVAR EM", text_color=TEXT_SECONDARY,
            font=ctk.CTkFont(family="DejaVu Sans", size=10, weight="bold"),
        ).pack(anchor="w")
        dest_row = ctk.CTkFrame(dest, fg_color="transparent")
        dest_row.pack(fill="x", pady=(4, 0))
        ctk.CTkEntry(
            dest_row, textvariable=self.dest_var, fg_color="#FFFFFF", border_color=BORDER, text_color=TEXT_PRIMARY,
        ).pack(side="left", fill="x", expand=True)
        ctk.CTkButton(
            dest_row, text="Selecionar…", command=self._pick_dest, width=100,
            fg_color="#FFFFFF", hover_color=BORDER_SOFT, text_color=TEXT_PRIMARY,
            border_width=1, border_color=BORDER, corner_radius=8,
        ).pack(side="left", padx=(8, 0))

        self.progress = ctk.CTkProgressBar(self, progress_color=ACCENT, fg_color=BORDER_SOFT, height=8)
        self.progress.set(0)
        self.progress.pack(fill="x", padx=16, pady=(12, 0))

        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.pack(fill="x", padx=16, pady=12)
        self.download_button = ctk.CTkButton(
            bar, text="Baixar", command=self._start_download, state="disabled",
            fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color="#FFFFFF", corner_radius=8,
        )
        self.download_button.pack(side="left")
        self.close_button = ctk.CTkButton(
            bar, text="Cancelar", command=self._close,
            fg_color="#FFFFFF", hover_color=BORDER_SOFT, text_color=TEXT_PRIMARY,
            border_width=1, border_color=BORDER, corner_radius=8,
        )
        self.close_button.pack(side="left", padx=8)
        self.protocol("WM_DELETE_WINDOW", self._close)

        self._run(self._login_worker)
        self.after(150, self._poll)

    # ------------------------------------------------------------- threads

    def _run(self, target, *args):
        self.busy = True
        threading.Thread(target=target, args=args, daemon=True).start()

    def _log(self, message: str) -> None:
        self.app_log_queue.put(message)
        if "reautenticacao necessaria" in message:
            self.events.put(("status", "O Google pediu para confirmar sua senha: faça isso na janela do navegador."))

    def _login_worker(self):
        try:
            browser = lers.LersBrowser()
            browser.start()
            self.events.put(("status", "Faça o login no LERS na janela do navegador que abriu (com a verificação em duas etapas)."))
            client = lers.LersClient(browser)
            client.wait_for_login(cancel_event=self.cancel_event)
            self.events.put(("status", "Login reconhecido. Carregando a lista de requests (o LERS pode levar alguns segundos)…"))
            self.events.put(("requests", (client, client.list_requests())))
        except Exception as exc:  # noqa: BLE001 - mostra qualquer falha na janela
            self.events.put(("error", str(exc)))

    def _download_worker(self, req: lers.LersRequest, dest: Path, kinds: tuple[str, ...]):
        try:
            files = [f for f in self.client.list_files(req.request_id) if f.kind in kinds]
            if not files:
                raise lers.LersError("Esse request não tem arquivos para baixar.")
            total = sum(f.size for f in files) or 1
            done: dict[int, int] = {}

            def on_progress(f, received, _size):
                done[f.file_id] = received
                self.events.put(("progress", (sum(done.values()) / total, f.name, len(done), len(files))))

            saved = self.client.download_files(
                req.request_id, files, dest, log=self._log, on_progress=on_progress, cancel_event=self.cancel_event,
            )
            self.events.put(("done", (dest, len(saved), len(files))))
        except Exception as exc:  # noqa: BLE001
            self.events.put(("error", str(exc)))

    # ------------------------------------------------------------- UI

    def _poll(self):
        if not self.winfo_exists():
            return
        while True:
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "status":
                self.status_var.set(payload)
            elif kind == "requests":
                self.busy = False
                self.client, self.requests = payload
                self._show_requests()
            elif kind == "progress":
                fraction, name, index, count = payload
                self.progress.set(min(fraction, 1.0))
                self.status_var.set(f"Baixando {index}/{count}: {name}")
            elif kind == "done":
                self.busy = self.downloading = False
                dest, saved, count = payload
                self.progress.set(1.0 if saved == count else self.progress.get())
                if saved == count:
                    self.status_var.set(f"Concluído: {saved} arquivo(s) em {dest}. A pasta já foi selecionada como origem.")
                    self.on_downloaded(dest)
                else:
                    self.status_var.set(f"Interrompido: {saved} de {count} arquivo(s) baixados em {dest}.")
                self.download_button.configure(state="normal")
                self.close_button.configure(text="Fechar")
            elif kind == "error":
                self.busy = self.downloading = False
                self.status_var.set(f"Erro: {payload}")
                self.download_button.configure(state="normal" if self.requests else "disabled")
        self.after(150, self._poll)

    def _show_requests(self):
        if not self.requests:
            self.status_var.set("Nenhum request encontrado nesta conta do LERS.")
            return
        self.status_var.set("Escolha o request a baixar (os com arquivos disponíveis vêm primeiro):")
        ordered = sorted(self.requests, key=lambda r: (r.status != "FILES_READY", -(r.created_ms or 0)))
        for i, req in enumerate(ordered):
            text = f"Ref. {req.google_ref}  ·  {req.case_number or 'sem número'}  ·  {req.created}  ·  {req.status_label}"
            ctk.CTkRadioButton(
                self.list_frame, text=text, variable=self.request_var, value=req.request_id,
                command=self._on_select, fg_color=ACCENT, text_color=TEXT_PRIMARY,
                font=ctk.CTkFont(family="DejaVu Sans", size=12),
            ).grid(row=i, column=0, sticky="w", padx=10, pady=3)
        ready = [r for r in ordered if r.status == "FILES_READY"]
        if ready:
            self.request_var.set(ready[0].request_id)
            self._on_select()

    def _selected(self) -> lers.LersRequest | None:
        return next((r for r in self.requests if r.request_id == self.request_var.get()), None)

    def _on_select(self):
        req = self._selected()
        if req is None:
            return
        self.dest_var.set(str(self.default_parent / f"LERS {req.google_ref or req.request_id}"))
        self.download_button.configure(state="normal")

    def _pick_dest(self):
        path = filedialog.askdirectory(parent=self, title="Pasta onde salvar os arquivos do LERS")
        if path:
            self.dest_var.set(path)

    def _start_download(self):
        req = self._selected()
        dest = self.dest_var.get().strip()
        if req is None or not dest or self.busy:
            return
        kinds = lers.DEFAULT_FILE_KINDS + (("LEGAL_PROCESS",) if self.include_legal_var.get() else ())
        self.download_button.configure(state="disabled")
        self.close_button.configure(text="Cancelar")
        self.cancel_event.clear()
        self.progress.set(0)
        self.status_var.set("Listando os arquivos do request…")
        self.downloading = True
        self._run(self._download_worker, req, Path(dest), kinds)

    def _close(self):
        self.cancel_event.set()
        if self.downloading:
            # Espera o worker parar o download no navegador; ele avisa por "done"/"error".
            self.status_var.set("Cancelando…")
            return
        self.destroy()


class JoaCloudGUI(ctk.CTk):
    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode("light")

        self.title(f"{APP_TITLE} v{APP_VERSION}")
        self.minsize(960, 660)
        self.configure(fg_color=BG_APP)

        icon_path = _icon_image_path()
        if icon_path is not None:
            try:
                self._icon_photo = tk.PhotoImage(file=str(icon_path))
                self.iconphoto(True, self._icon_photo)
            except tk.TclError:
                pass

        self.update_queue: queue.Queue = queue.Queue()
        self.log_queue: queue.Queue = queue.Queue()
        self.cancel_event = threading.Event()
        self.worker_thread: threading.Thread | None = None
        self.provider = providers.get_provider()
        self.entries: list[core.FileEntry] = []
        self.rows: dict[str, FileRow] = {}
        self.step_cards: dict[str, StepCard] = {}
        self._file_progress: dict[str, core.ProgressEvent] = {}
        self._pipeline_start_time: float = 0.0

        self.provider_var = tk.StringVar(value=self.provider.display_name)
        self.source_path_var = tk.StringVar()
        self.output_dir_var = tk.StringVar()
        self.passphrase_var = tk.StringVar()
        self.workers_var = tk.StringVar(value="4")
        self.show_pass_var = tk.BooleanVar(value=False)
        self.select_all_var = tk.BooleanVar(value=True)
        self.file_count_var = tk.StringVar(value="Nenhum arquivo carregado")

        self._build_widgets()
        self._poll_queues()

    # ------------------------------------------------------------------ UI

    def _build_widgets(self):
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=20, pady=(18, 6))

        provider_col = ctk.CTkFrame(header, fg_color="transparent")
        provider_col.pack(side="right", anchor="n")
        ctk.CTkLabel(
            provider_col, text="PROVEDOR", text_color=TEXT_SECONDARY,
            font=ctk.CTkFont(family="DejaVu Sans", size=10, weight="bold"),
        ).pack(anchor="e")
        self.provider_selector = ctk.CTkSegmentedButton(
            provider_col, values=[p.display_name for p in providers.PROVIDERS.values()],
            variable=self.provider_var, command=self._on_provider_change,
            selected_color=ACCENT, selected_hover_color=ACCENT_HOVER,
            unselected_color="#FFFFFF", unselected_hover_color=BORDER_SOFT,
            text_color=TEXT_PRIMARY, fg_color=BORDER,
        )
        self.provider_selector.pack(anchor="e", pady=(4, 0))

        ctk.CTkLabel(
            header, text=APP_TITLE, text_color=TEXT_PRIMARY,
            font=ctk.CTkFont(family="DejaVu Sans", size=20, weight="bold"),
        ).pack(anchor="w")
        ctk.CTkLabel(
            header, text=APP_DESCRIPTION, text_color=TEXT_SECONDARY,
            font=ctk.CTkFont(family="DejaVu Sans", size=12),
        ).pack(anchor="w")
        ctk.CTkLabel(
            header, text=AUTHOR_LINE, text_color=TEXT_SECONDARY,
            font=ctk.CTkFont(family="DejaVu Sans", size=10),
        ).pack(anchor="w", pady=(4, 0))

        # --- Config card ---
        config_card = ctk.CTkFrame(
            self, fg_color=BG_CARD, border_width=1, border_color=BORDER_SOFT, corner_radius=10
        )
        config_card.pack(fill="x", padx=20, pady=8)
        config_card.columnconfigure((0, 1), weight=1)

        self.source_label = self._field(
            config_card, self.provider.source_label, self.source_path_var, self._pick_source, row=0, col=0
        )
        self.lers_button = ctk.CTkButton(
            self.source_label.row_frame, text="Baixar do LERS…", command=self._open_lers_dialog,
            width=130, fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color="#FFFFFF", corner_radius=8,
        )
        self._update_lers_button()
        self._field(config_card, "Pasta de destino", self.output_dir_var, self._pick_output_dir, row=0, col=1)

        row2 = ctk.CTkFrame(config_card, fg_color="transparent")
        row2.grid(row=1, column=0, columnspan=2, sticky="we", padx=16, pady=(4, 14))

        pass_col = ctk.CTkFrame(row2, fg_color="transparent")
        pass_col.pack(side="left", padx=(0, 32))
        self.pass_col = pass_col
        ctk.CTkLabel(
            pass_col, text="SENHA GPG", text_color=TEXT_SECONDARY,
            font=ctk.CTkFont(family="DejaVu Sans", size=10, weight="bold"),
        ).pack(anchor="w")
        pass_row = ctk.CTkFrame(pass_col, fg_color="transparent")
        pass_row.pack(anchor="w", pady=(4, 0))
        self.pass_entry = ctk.CTkEntry(
            pass_row, textvariable=self.passphrase_var, width=220, show="*",
            fg_color="#FFFFFF", border_color=BORDER, text_color=TEXT_PRIMARY,
        )
        self.pass_entry.pack(side="left")
        self.show_pass_check = ctk.CTkCheckBox(
            pass_row, text="mostrar", variable=self.show_pass_var,
            command=self._toggle_password, checkbox_width=16, checkbox_height=16,
            fg_color=ACCENT, text_color=TEXT_SECONDARY, font=ctk.CTkFont(family="DejaVu Sans", size=11),
        )
        self.show_pass_check.pack(side="left", padx=(10, 0))

        workers_col = ctk.CTkFrame(row2, fg_color="transparent")
        workers_col.pack(side="left")
        self.workers_col = workers_col
        ctk.CTkLabel(
            workers_col, text="DOWNLOADS SIMULTÂNEOS", text_color=TEXT_SECONDARY,
            font=ctk.CTkFont(family="DejaVu Sans", size=10, weight="bold"),
        ).pack(anchor="w")
        ctk.CTkEntry(
            workers_col, textvariable=self.workers_var, width=60,
            fg_color="#FFFFFF", border_color=BORDER, text_color=TEXT_PRIMARY,
        ).pack(anchor="w", pady=(4, 0))

        # --- Pipeline stepper card ---
        steps_card = ctk.CTkFrame(
            self, fg_color=BG_CARD, border_width=1, border_color=BORDER_SOFT, corner_radius=10
        )
        steps_card.pack(fill="x", padx=20, pady=8)
        ctk.CTkLabel(
            steps_card, text="ETAPAS DO PIPELINE — ATIVE QUANTAS QUISER", text_color=TEXT_SECONDARY,
            font=ctk.CTkFont(family="DejaVu Sans", size=10, weight="bold"),
        ).pack(anchor="w", padx=16, pady=(12, 6))

        steps_row = ctk.CTkFrame(steps_card, fg_color="transparent")
        steps_row.pack(fill="x", padx=16, pady=(0, 14))
        steps_row.columnconfigure((0, 1, 2), weight=1)

        self.steps_row = steps_row
        for key in ("download", "verify", "unpack"):
            self.step_cards[key] = StepCard(steps_row, key, self.provider, self._on_step_toggle)
        self._layout_step_cards()

        # --- Action bar ---
        action_bar = ctk.CTkFrame(self, fg_color="transparent")
        action_bar.pack(fill="x", padx=20, pady=8)
        self.start_button = ctk.CTkButton(
            action_bar, text="Iniciar", command=self._start,
            fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color="#FFFFFF",
            corner_radius=8, height=36,
        )
        self.start_button.pack(side="left")
        self.cancel_button = ctk.CTkButton(
            action_bar, text="Cancelar", command=self._cancel, state="disabled",
            fg_color=DISABLED_BG, hover_color=DISABLED_BG, text_color=TEXT_SECONDARY,
            corner_radius=8, height=36, border_width=1, border_color=BORDER,
        )
        self.cancel_button.pack(side="left", padx=8)

        self.progress = ctk.CTkProgressBar(
            action_bar, progress_color=ACCENT, fg_color=BORDER_SOFT, height=8, corner_radius=4,
        )
        self.progress.set(0)
        self.progress.pack(side="left", fill="x", expand=True, padx=14)

        self.progress_pct_var = tk.StringVar(value="")
        ctk.CTkLabel(
            action_bar, textvariable=self.progress_pct_var, text_color=ACCENT, width=36, anchor="e",
            font=ctk.CTkFont(family="DejaVu Sans", size=12, weight="bold"),
        ).pack(side="left")

        self.summary_var = tk.StringVar(value="")
        ctk.CTkLabel(
            action_bar, textvariable=self.summary_var, text_color=TEXT_SECONDARY,
            font=ctk.CTkFont(family="DejaVu Sans", size=12),
        ).pack(side="right")

        # --- Content row: file list + log ---
        content = ctk.CTkFrame(self, fg_color="transparent")
        content.pack(fill="both", expand=True, padx=20, pady=8)

        list_card = ctk.CTkFrame(
            content, fg_color="#FFFFFF", border_width=1, border_color=BORDER_SOFT, corner_radius=10,
        )
        list_card.pack(side="left", fill="both", expand=True, padx=(0, 8))

        list_header = ctk.CTkFrame(list_card, fg_color=BG_CARD, corner_radius=0, height=36)
        list_header.pack(fill="x")
        list_header.pack_propagate(False)
        self.select_all_check = ctk.CTkCheckBox(
            list_header, text="", variable=self.select_all_var, command=self._toggle_select_all,
            checkbox_width=18, checkbox_height=18, fg_color=ACCENT, border_color=BORDER, width=18,
        )
        self.select_all_check.pack(side="left", padx=(10, 8))
        ctk.CTkLabel(
            list_header, text="ARQUIVOS — SELECIONE OS QUE DESEJA PROCESSAR",
            text_color=TEXT_SECONDARY, font=ctk.CTkFont(family="DejaVu Sans", size=10, weight="bold"),
        ).pack(side="left")
        ctk.CTkButton(
            list_header, text="↻", width=26, height=22, command=self._reload_file_list,
            fg_color="transparent", hover_color=BORDER_SOFT, text_color=TEXT_SECONDARY,
            border_width=1, border_color=BORDER, corner_radius=6,
        ).pack(side="right", padx=(0, 10))
        ctk.CTkLabel(
            list_header, textvariable=self.file_count_var, text_color=TEXT_SECONDARY,
            font=ctk.CTkFont(family="DejaVu Sans", size=10),
        ).pack(side="right", padx=(0, 10))

        self.file_list_frame = ctk.CTkScrollableFrame(list_card, fg_color="#FFFFFF", corner_radius=0)
        self.file_list_frame.pack(fill="both", expand=True, padx=1, pady=(0, 1))

        self.empty_list_label = ctk.CTkLabel(
            self.file_list_frame,
            text=self._empty_list_text(),
            text_color=TEXT_SECONDARY, font=ctk.CTkFont(family="DejaVu Sans", size=12),
        )
        self.empty_list_label.pack(pady=40)

        log_card = ctk.CTkFrame(content, fg_color=LOG_BG, corner_radius=10, width=360)
        log_card.pack(side="left", fill="both", padx=(8, 0))
        log_card.pack_propagate(False)
        ctk.CTkLabel(
            log_card, text="LOG", text_color="#B7B2A6", fg_color=LOG_HEADER_BG,
            corner_radius=10,
            font=ctk.CTkFont(family="Consolas", size=10, weight="bold"), anchor="w", height=32,
        ).pack(fill="x")
        self.log_text = ctk.CTkTextbox(
            log_card, fg_color=LOG_BG, text_color=LOG_TEXT, font=("Consolas", 10),
            wrap="word", activate_scrollbars=True, corner_radius=0,
        )
        self.log_text.pack(fill="both", expand=True)
        self.log_text.tag_config("LVL_DEBUG", foreground=LOG_TEXT_DIM)
        self.log_text.tag_config("LVL_INFO", foreground=LOG_INFO)
        self.log_text.tag_config("LVL_OK", foreground=LOG_OK)
        self.log_text.tag_config("LVL_WARN", foreground=LOG_WARN)
        self.log_text.tag_config("LVL_ERROR", foreground=LOG_ERROR)
        self.log_text.configure(state="disabled")

        # --- Footer ---
        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.pack(fill="x", padx=20, pady=(0, 12))
        ctk.CTkLabel(
            footer, text=f"v{APP_VERSION}", text_color=TEXT_SECONDARY, font=ctk.CTkFont(family="DejaVu Sans", size=10),
        ).pack(side="right")
        ctk.CTkButton(
            footer, text="Gerar Termo de Recebimento…", command=self._open_report_window,
            fg_color="#FFFFFF", hover_color=BORDER_SOFT, text_color=TEXT_PRIMARY,
            border_width=1, border_color=BORDER, corner_radius=8, height=30,
        ).pack(side="left")

        self._update_step_state()

    def _field(self, parent, label, variable, command, row, col):
        """Campo com rotulo + entrada + botao Selecionar; retorna o rotulo
        (pra poder trocar o texto quando o provedor muda), com a linha da
        entrada em ``row_frame``."""
        wrap = ctk.CTkFrame(parent, fg_color="transparent")
        wrap.grid(row=row, column=col, sticky="we", padx=16, pady=(14, 4))
        label_widget = ctk.CTkLabel(
            wrap, text=label.upper(), text_color=TEXT_SECONDARY,
            font=ctk.CTkFont(family="DejaVu Sans", size=10, weight="bold"),
        )
        label_widget.pack(anchor="w")
        inner = ctk.CTkFrame(wrap, fg_color="transparent")
        inner.pack(fill="x", pady=(4, 0))
        entry = ctk.CTkEntry(
            inner, textvariable=variable, fg_color="#FFFFFF", border_color=BORDER,
            text_color=TEXT_PRIMARY,
        )
        entry.pack(side="left", fill="x", expand=True)
        ctk.CTkButton(
            inner, text="Selecionar…", command=command, width=100,
            fg_color="#FFFFFF", hover_color=BORDER_SOFT, text_color=TEXT_PRIMARY,
            border_width=1, border_color=BORDER, corner_radius=8,
        ).pack(side="left", padx=(8, 0))
        label_widget.row_frame = inner  # pra botoes extras ao lado do campo
        return label_widget

    def _empty_list_text(self) -> str:
        what = "a pasta com o retorno do LERS" if self.provider.source_is_dir else "o CSV da Apple"
        return f"Selecione {what} para listar os arquivos aqui."

    def _step_keys(self) -> tuple[str, ...]:
        """Etapas que o provedor atual tem (Google nao tem a 3: nada e' extraido)."""
        if self.provider.has_unpack_step:
            return ("download", "verify", "unpack")
        return ("download", "verify")

    def _layout_step_cards(self):
        keys = self._step_keys()
        for card in self.step_cards.values():
            card.grid_forget()
        for col in range(3):
            self.steps_row.columnconfigure(col, weight=1 if col < len(keys) else 0)
        for i, key in enumerate(keys):
            self.step_cards[key].grid(row=0, column=i, sticky="nsew", padx=6)

    def _unpack_on(self) -> bool:
        return self.provider.has_unpack_step and self.step_cards["unpack"].var.get()

    def _on_provider_change(self, display_name: str):
        provider = next(p for p in providers.PROVIDERS.values() if p.display_name == display_name)
        if provider is self.provider:
            return
        if self.worker_thread and self.worker_thread.is_alive():
            self.provider_var.set(self.provider.display_name)
            messagebox.showwarning(APP_TITLE, "Aguarde o pipeline atual terminar antes de trocar de provedor.")
            return
        self.provider = provider
        self.source_path_var.set("")
        self.source_label.configure(text=provider.source_label.upper())
        if provider.needs_passphrase:
            self.pass_col.pack(side="left", padx=(0, 32), before=self.workers_col)
        else:
            self.pass_col.pack_forget()
        for card in self.step_cards.values():
            card.set_provider(provider)
        self._layout_step_cards()
        self._update_lers_button()
        self.entries = []
        self._rebuild_file_list()
        self._update_step_state()

    def _update_lers_button(self):
        if self.provider.key == "google":
            self.lers_button.pack(side="left", padx=(8, 0))
        else:
            self.lers_button.pack_forget()

    def _open_lers_dialog(self):
        if self.worker_thread and self.worker_thread.is_alive():
            messagebox.showwarning(APP_TITLE, "Aguarde o pipeline atual terminar.")
            return
        if getattr(self, "_lers_dialog", None) is not None and self._lers_dialog.winfo_exists():
            self._lers_dialog.focus()
            return
        parent = self.output_dir_var.get().strip() or str(Path.home())
        self._lers_dialog = LersDialog(self, Path(parent), self._on_lers_downloaded, self.log_queue)

    def _on_lers_downloaded(self, folder: Path):
        self.source_path_var.set(str(folder))
        if not self.output_dir_var.get().strip():
            self.output_dir_var.set(str(folder))
        self._reload_file_list()

    def _toggle_password(self):
        self.pass_entry.configure(show="" if self.show_pass_var.get() else "*")

    def _on_step_toggle(self, key):
        self._update_step_state()

    def _update_step_state(self):
        unpack_on = self._unpack_on()
        pass_state = "normal" if unpack_on else "disabled"
        self.pass_entry.configure(state=pass_state)
        self.show_pass_check.configure(state=pass_state)

        labels = []
        for key in self._step_keys():
            card = self.step_cards[key]
            card.set_active(card.var.get())
            if card.var.get():
                labels.append(card.title)

        if labels:
            self.start_button.configure(
                text="Iniciar — " + " + ".join(labels), state="normal",
                fg_color=ACCENT, hover_color=ACCENT_HOVER,
            )
        else:
            self.start_button.configure(
                text="Selecione ao menos uma etapa", state="disabled",
                fg_color=BUTTON_DISABLED_BG, hover_color=BUTTON_DISABLED_BG,
            )

    def _pick_source(self):
        if self.provider.source_is_dir:
            path = filedialog.askdirectory(title=f"Selecione a {self.provider.source_label.lower()}")
        else:
            path = filedialog.askopenfilename(
                title=f"Selecione o {self.provider.source_label}", filetypes=[("CSV", "*.csv"), ("Todos", "*.*")]
            )
        if path:
            self.source_path_var.set(path)
            self._reload_file_list()

    def _pick_output_dir(self):
        path = filedialog.askdirectory(title="Selecione a pasta de destino")
        if path:
            self.output_dir_var.set(path)

    # --------------------------------------------------------------- file list

    def _reload_file_list(self):
        source_path = self.source_path_var.get().strip()
        if not source_path:
            self.entries = []
            self._rebuild_file_list()
            return
        try:
            self.entries = self.provider.load_entries(Path(source_path))
        except core.PipelineError as exc:
            self.entries = []
            self._rebuild_file_list()
            messagebox.showerror(APP_TITLE, str(exc))
            return
        self._rebuild_file_list()

    def _rebuild_file_list(self):
        for child in self.file_list_frame.winfo_children():
            child.destroy()
        self.rows = {}

        if not self.entries:
            self.empty_list_label = ctk.CTkLabel(
                self.file_list_frame,
                text=self._empty_list_text(),
                text_color=TEXT_SECONDARY, font=ctk.CTkFont(family="DejaVu Sans", size=12),
            )
            self.empty_list_label.pack(pady=40)
            self.file_count_var.set("Nenhum arquivo carregado")
            return

        for entry in self.entries:
            row = FileRow(self.file_list_frame, entry, on_delete=self._delete_entry)
            row.pack(fill="x", padx=4, pady=(2, 0))
            separator = ctk.CTkFrame(self.file_list_frame, fg_color=BORDER_SOFT, height=1)
            separator.pack(fill="x", padx=10, pady=(2, 2))
            self.rows[entry.file_name] = row

        self.select_all_var.set(True)
        self.file_count_var.set(f"{len(self.entries)} arquivo(s)")

    def _toggle_select_all(self):
        value = self.select_all_var.get()
        for row in self.rows.values():
            row.selected_var.set(value)

    def _set_rows_enabled(self, enabled: bool):
        self.select_all_check.configure(state="normal" if enabled else "disabled")
        for row in self.rows.values():
            row.set_enabled(enabled)

    def _delete_entry(self, entry: core.FileEntry):
        if self.worker_thread and self.worker_thread.is_alive():
            messagebox.showwarning(APP_TITLE, "Aguarde o pipeline atual terminar antes de excluir arquivos.")
            return
        output_dir = self.output_dir_var.get().strip()
        if not output_dir:
            messagebox.showerror(APP_TITLE, "Selecione a pasta de destino primeiro.")
            return
        if self.provider.has_unpack_step:
            what = f"os arquivos já obtidos (e a cópia {self.provider.unpack_label.lower()}da) de"
        else:
            what = "a cópia, no destino, do ZIP de produção que contém"
        if not messagebox.askyesno(
            APP_TITLE,
            f"Excluir do disco {what}:"
            f"\n\n{entry.file_name}\n\n"
            "Essa ação não pode ser desfeita.",
        ):
            return

        removed = core.delete_entry_files(entry, Path(output_dir), self.provider)
        entry.status = "pendente"
        entry.message = ""
        row = self.rows.get(entry.file_name)
        if row:
            row.reset()

        if removed:
            self.log_queue.put(
                core.log_line(
                    "WARN", "arquivo", "removido pelo usuario",
                    file=entry.file_name, arquivos_removidos=len(removed),
                )
            )
        else:
            messagebox.showinfo(APP_TITLE, "Nenhum arquivo encontrado em disco para excluir.")

    # ------------------------------------------------------------- pipeline

    def _start(self):
        provider = self.provider
        source_path = self.source_path_var.get().strip()
        output_dir = self.output_dir_var.get().strip()

        download_on = self.step_cards["download"].var.get()
        verify_on = self.step_cards["verify"].var.get()
        unpack_on = self._unpack_on()

        if not source_path:
            messagebox.showerror(APP_TITLE, f"Selecione: {provider.source_label}.")
            return
        if not output_dir:
            messagebox.showerror(APP_TITLE, "Selecione a pasta de destino.")
            return
        if not (download_on or verify_on or unpack_on):
            etapas = "Baixar ou Verificar"
            if provider.has_unpack_step:
                etapas = f"Baixar, Verificar ou {provider.unpack_label}"
            messagebox.showerror(APP_TITLE, f"Selecione ao menos uma etapa ({etapas}).")
            return

        if not self.entries:
            self._reload_file_list()
        if not self.entries:
            return

        selected_entries = [e for e in self.entries if self.rows[e.file_name].selected_var.get()]
        if not selected_entries:
            messagebox.showerror(APP_TITLE, "Selecione ao menos um arquivo na lista.")
            return

        if unpack_on and any(provider.needs_unpack(e) for e in selected_entries):
            missing_tool = provider.missing_tool_message()
            if missing_tool:
                messagebox.showerror(
                    APP_TITLE,
                    f"{missing_tool}\n"
                    "Instale o Gpg4win (https://gpg4win.org) e tente novamente,\n"
                    f"ou desmarque a opcao '{provider.unpack_label}'.",
                )
                return
            if provider.needs_passphrase and not self.passphrase_var.get():
                messagebox.showerror(APP_TITLE, "Informe a senha do GPG para descriptografar.")
                return

        try:
            max_workers = max(1, int(self.workers_var.get()))
        except ValueError:
            max_workers = 4

        config = core.PipelineConfig(
            source=Path(source_path),
            output_dir=Path(output_dir),
            passphrase=self.passphrase_var.get() if provider.needs_passphrase else "",
            max_workers=max_workers,
            download=download_on,
            verify=verify_on,
            unpack=unpack_on,
            provider=provider,
        )

        for entry in selected_entries:
            self.rows[entry.file_name].reset()

        self._total_entries = max(1, len(selected_entries))
        self._done_entries = 0
        self._file_progress = {}
        self._pipeline_start_time = time.monotonic()
        self.progress.set(0)
        self.progress_pct_var.set("0%")
        self.summary_var.set("")
        self.cancel_event.clear()
        self.start_button.configure(state="disabled")
        self.cancel_button.configure(
            state="normal", fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color="#FFFFFF", border_width=0,
        )
        for card in self.step_cards.values():
            card.switch.configure(state="disabled")
        self.provider_selector.configure(state="disabled")
        self._set_rows_enabled(False)

        self.worker_thread = threading.Thread(
            target=self._run_worker, args=(config, selected_entries), daemon=True
        )
        self.worker_thread.start()

    def _run_worker(self, config: core.PipelineConfig, entries: list[core.FileEntry]):
        def on_update(entry: core.FileEntry) -> None:
            self.update_queue.put(entry)

        def on_progress(entry: core.FileEntry, bytes_done: int, bytes_total: int, phase: str = "download") -> None:
            self.update_queue.put(core.ProgressEvent(entry.file_name, bytes_done, bytes_total, phase=phase))

        def log(message: str) -> None:
            self.log_queue.put(message)

        try:
            summary = core.run_pipeline(
                config, entries, on_update, log, cancel_event=self.cancel_event, on_progress=on_progress
            )
            self.update_queue.put(summary)
        except core.PipelineError as exc:
            self.update_queue.put(exc)
        except Exception as exc:  # noqa: BLE001 - nunca deixar a thread morrer silenciosa
            self.update_queue.put(exc)

    def _cancel(self):
        self.cancel_event.set()
        self.cancel_button.configure(state="disabled")
        self.log_queue.put(core.log_line("WARN", "usuario", "cancelamento solicitado"))

    # ------------------------------------------------------------ polling

    def _poll_queues(self):
        drained = 0
        while drained < 200:
            try:
                item = self.update_queue.get_nowait()
            except queue.Empty:
                break
            drained += 1
            self._handle_update_item(item)

        while True:
            try:
                message = self.log_queue.get_nowait()
            except queue.Empty:
                break
            self._append_log(message)

        self.after(100, self._poll_queues)

    def _handle_update_item(self, item):
        if isinstance(item, core.ProgressEvent):
            row = self.rows.get(item.file_name)
            if row:
                row.update_progress(item.bytes_done, item.bytes_total, phase=item.phase)
            if item.phase == "download":
                self._file_progress[item.file_name] = item
                self._update_aggregate_summary()
        elif isinstance(item, core.FileEntry):
            row = self.rows.get(item.file_name)
            if row:
                row.set_status(item.status, item.message)
            if item.status in ("concluido", "hash_invalido", "erro", "cancelado"):
                self._done_entries += 1
                fraction = min(1.0, self._done_entries / self._total_entries)
                self.progress.set(fraction)
                self.progress_pct_var.set(f"{int(round(fraction * 100))}%")
        elif isinstance(item, core.PipelineSummary):
            self.summary_var.set(
                f"Concluídos: {item.concluidos}/{item.total}  ·  "
                f"Hash inválido: {item.hash_invalido}  ·  "
                f"Erros: {item.erros}  ·  "
                f"Cancelados: {item.cancelados}  ·  "
                f"({item.elapsed_seconds:.1f}s)"
            )
            self._reenable_controls()
            if item.erros or item.hash_invalido:
                messagebox.showwarning(
                    APP_TITLE,
                    "Pipeline finalizado com pendencias. Veja a coluna Status e o log "
                    "para os arquivos com erro ou hash invalido.",
                )
            else:
                messagebox.showinfo(APP_TITLE, "Pipeline finalizado com sucesso.")
        elif isinstance(item, Exception):
            self._reenable_controls()
            messagebox.showerror(APP_TITLE, f"Erro fatal no pipeline:\n{item}")

    def _update_aggregate_summary(self):
        """Enquanto o pipeline roda, reaproveita o espaco do resumo final (vazio ate
        entao) pra mostrar o total baixado, a velocidade agregada e o ETA do lote."""
        if not self._file_progress:
            return
        agg = core.aggregate_progress(self._file_progress, self._pipeline_start_time)
        total_done = agg["bytes_done"]
        total_known = agg["bytes_known"]
        speed = agg["speed_bps"]

        if total_known > 0:
            parts = [f"{report.humanize_bytes(total_done)} / {report.humanize_bytes(total_known)}"]
        else:
            parts = [report.humanize_bytes(total_done)]

        parts.append(f"↓ {report.humanize_bytes(speed)}/s total" if speed > 0 else "↓ calculando…")

        if agg["eta_seconds"] is not None:
            parts.append(f"restante ~{core.format_eta(agg['eta_seconds'])}")

        self.summary_var.set("  ·  ".join(parts))

    def _reenable_controls(self):
        self.cancel_button.configure(
            state="disabled", fg_color=DISABLED_BG, hover_color=DISABLED_BG,
            text_color=TEXT_SECONDARY, border_width=1, border_color=BORDER,
        )
        for card in self.step_cards.values():
            card.switch.configure(state="normal")
        self.provider_selector.configure(state="normal")
        self._set_rows_enabled(True)
        self._update_step_state()

    def _append_log(self, message: str):
        match = _LEVEL_TAG_RE.search(message)
        tag = f"LVL_{match.group(1)}" if match else "LVL_INFO"
        self.log_text.configure(state="normal")
        self.log_text.insert("end", message + "\n", tag)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    # -------------------------------------------------------------- relatorio

    def _report_entry(self, parent, label, var, row, col, columnspan=1):
        wrap = ctk.CTkFrame(parent, fg_color="transparent")
        wrap.grid(row=row, column=col, columnspan=columnspan, sticky="we", padx=6, pady=6)
        ctk.CTkLabel(
            wrap, text=label.upper(), text_color=TEXT_SECONDARY,
            font=ctk.CTkFont(family="DejaVu Sans", size=10, weight="bold"),
        ).pack(anchor="w")
        ctk.CTkEntry(
            wrap, textvariable=var, fg_color="#FFFFFF", border_color=BORDER,
            text_color=TEXT_PRIMARY,
        ).pack(fill="x", pady=(4, 0))

    def _on_toggle_incluir_senha_gpg(self, win):
        """Nunca deixa marcar a opcao sem ter senha nenhuma pra publicar no termo —
        senha vazia ali seria confuso (pareceria erro em vez de "sem senha")."""
        if win.vars["incluir_senha_gpg"].get() and not self.passphrase_var.get().strip():
            win.vars["incluir_senha_gpg"].set(False)
            messagebox.showwarning(
                "Senha do GPG em branco",
                "O campo de senha GPG (tela principal) está em branco — preencha a senha antes "
                "de incluí-la no termo.",
                parent=win,
            )

    def _open_report_window(self):
        now = datetime.now()

        win = ctk.CTkToplevel(self)
        win.title("Gerar Termo de Recebimento de Evidência")
        win.geometry("760x780")
        win.configure(fg_color=BG_APP)
        win.transient(self)

        win.vars = {
            "numero_processo": tk.StringVar(),
            "numero_pic": tk.StringVar(),
            "orgao_execucao": tk.StringVar(),
            "data": tk.StringVar(value=now.strftime("%d/%m/%Y")),
            "hora": tk.StringVar(value=now.strftime("%H:%M")),
            "responsavel": tk.StringVar(),
            "matricula": tk.StringVar(),
            "recebido_portal": tk.BooleanVar(value=False),
            "recebido_email": tk.BooleanVar(value=False),
            "procedimento_referenciado": tk.StringVar(),
            "id_documento": tk.StringVar(),
            "disponibiliza_original": tk.BooleanVar(value=False),
            "disponibiliza_processada": tk.BooleanVar(value=False),
            # Nasce marcada so' se ja houver senha digitada na tela principal — nunca
            # marcada "no vazio" (ver _on_toggle_incluir_senha_gpg). Provedor sem
            # senha (Google) nunca publica senha nenhuma.
            "incluir_senha_gpg": tk.BooleanVar(
                value=self.provider.needs_passphrase and bool(self.passphrase_var.get().strip())
            ),
        }

        scroll = ctk.CTkScrollableFrame(win, fg_color="transparent")
        scroll.pack(fill="both", expand=True, padx=16, pady=(16, 8))
        scroll.columnconfigure((0, 1), weight=1)

        ctk.CTkLabel(
            scroll, text="1. IDENTIFICAÇÃO DO CASO", text_color=TEXT_PRIMARY,
            font=ctk.CTkFont(family="DejaVu Sans", size=13, weight="bold"),
        ).grid(row=0, column=0, columnspan=2, sticky="w", padx=6, pady=(4, 0))
        self._report_entry(scroll, "Número do Processo Judicial", win.vars["numero_processo"], 1, 0)
        self._report_entry(scroll, "PIC/Inquérito", win.vars["numero_pic"], 1, 1)
        self._report_entry(scroll, "Órgão de Execução", win.vars["orgao_execucao"], 2, 0, columnspan=2)

        ctk.CTkLabel(
            scroll, text="2. DADOS DA RECEPÇÃO", text_color=TEXT_PRIMARY,
            font=ctk.CTkFont(family="DejaVu Sans", size=13, weight="bold"),
        ).grid(row=3, column=0, columnspan=2, sticky="w", padx=6, pady=(14, 0))
        self._report_entry(scroll, "Data", win.vars["data"], 4, 0)
        self._report_entry(scroll, "Hora", win.vars["hora"], 4, 1)
        self._report_entry(scroll, "Responsável", win.vars["responsavel"], 5, 0)
        self._report_entry(scroll, "Matrícula", win.vars["matricula"], 5, 1)

        recebimento_row = ctk.CTkFrame(scroll, fg_color="transparent")
        recebimento_row.grid(row=6, column=0, columnspan=2, sticky="w", padx=6, pady=(2, 0))
        ctk.CTkLabel(
            recebimento_row, text="FORMA DE RECEBIMENTO", text_color=TEXT_SECONDARY,
            font=ctk.CTkFont(family="DejaVu Sans", size=10, weight="bold"),
        ).pack(anchor="w")
        ctk.CTkCheckBox(
            recebimento_row, text="Portal do Provedor", variable=win.vars["recebido_portal"],
            fg_color=ACCENT, text_color=TEXT_PRIMARY,
        ).pack(anchor="w", pady=(4, 0))
        ctk.CTkCheckBox(
            recebimento_row, text="E-mail Oficial", variable=win.vars["recebido_email"],
            fg_color=ACCENT, text_color=TEXT_PRIMARY,
        ).pack(anchor="w", pady=(4, 0))

        ctk.CTkLabel(
            scroll, text="3. ESPECIFICAÇÕES TÉCNICAS", text_color=TEXT_PRIMARY,
            font=ctk.CTkFont(family="DejaVu Sans", size=13, weight="bold"),
        ).grid(row=7, column=0, columnspan=2, sticky="w", padx=6, pady=(14, 0))

        win.provider = self.provider
        hash_status_var = tk.StringVar(
            value="Nenhum arquivo carregado — selecione a origem dos arquivos e a pasta de destino antes de "
            "gerar o termo para preencher volume/quantidade/hashes automaticamente."
        )
        ctk.CTkLabel(
            scroll, textvariable=hash_status_var, text_color=TEXT_SECONDARY, wraplength=680,
            justify="left", font=ctk.CTkFont(family="DejaVu Sans", size=11),
        ).grid(row=8, column=0, columnspan=2, sticky="w", padx=6, pady=(4, 0))

        gpg_row = ctk.CTkFrame(scroll, fg_color="transparent")
        if self.provider.needs_passphrase:
            gpg_row.grid(row=9, column=0, columnspan=2, sticky="w", padx=6, pady=(8, 0))
        ctk.CTkCheckBox(
            gpg_row, text="Incluir senha do GPG no termo", variable=win.vars["incluir_senha_gpg"],
            fg_color=ACCENT, text_color=TEXT_PRIMARY,
            command=lambda: self._on_toggle_incluir_senha_gpg(win),
        ).pack(anchor="w")
        ctk.CTkLabel(
            gpg_row, text="Adiciona a senha usada na descriptografia (campo acima, na tela principal) à Seção 3 do termo.",
            text_color=TEXT_SECONDARY, font=ctk.CTkFont(family="DejaVu Sans", size=10),
        ).pack(anchor="w", padx=(28, 0))

        self._report_entry(scroll, "Procedimento referenciado (autos)", win.vars["procedimento_referenciado"], 10, 0)
        self._report_entry(scroll, "ID do documento", win.vars["id_documento"], 10, 1)

        ctk.CTkLabel(
            scroll, text="5. DISPONIBILIZAÇÃO", text_color=TEXT_PRIMARY,
            font=ctk.CTkFont(family="DejaVu Sans", size=13, weight="bold"),
        ).grid(row=11, column=0, columnspan=2, sticky="w", padx=6, pady=(14, 0))
        disp_row = ctk.CTkFrame(scroll, fg_color="transparent")
        disp_row.grid(row=12, column=0, columnspan=2, sticky="w", padx=6, pady=(2, 0))
        ctk.CTkCheckBox(
            disp_row, text="Cópia da Aquisição Forense Original (Dados Brutos + Hashes + Metadados)",
            variable=win.vars["disponibiliza_original"], fg_color=ACCENT, text_color=TEXT_PRIMARY,
        ).pack(anchor="w", pady=(4, 0))
        ctk.CTkCheckBox(
            disp_row, text="Cópia Processada/Indexada",
            variable=win.vars["disponibiliza_processada"], fg_color=ACCENT, text_color=TEXT_PRIMARY,
        ).pack(anchor="w", pady=(4, 0))

        # --- area fixa: gerar / copiar / preview ---
        bottom = ctk.CTkFrame(win, fg_color="transparent")
        bottom.pack(fill="both", expand=False, padx=16, pady=(0, 16))

        buttons_row = ctk.CTkFrame(bottom, fg_color="transparent")
        buttons_row.pack(fill="x")

        generate_button = ctk.CTkButton(
            buttons_row, text="Gerar termo", fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color="#FFFFFF",
            corner_radius=8, height=34,
            command=lambda: self._generate_report_text(win),
        )
        generate_button.pack(side="left")

        copy_button = ctk.CTkButton(
            buttons_row, text="Copiar", state="disabled", fg_color="#FFFFFF", hover_color=BORDER_SOFT,
            text_color=TEXT_PRIMARY, border_width=1, border_color=BORDER, corner_radius=8, height=34,
            command=lambda: self._copy_report_text(win),
        )
        copy_button.pack(side="left", padx=8)

        docx_button = ctk.CTkButton(
            buttons_row, text="Salvar .docx", fg_color="#FFFFFF", hover_color=BORDER_SOFT,
            text_color=TEXT_PRIMARY, border_width=1, border_color=BORDER, corner_radius=8, height=34,
            command=lambda: self._save_report_docx(win),
        )
        docx_button.pack(side="left")

        output_box = ctk.CTkTextbox(
            bottom, fg_color="#FFFFFF", text_color=TEXT_PRIMARY, font=("Consolas", 11),
            wrap="word", height=220, border_width=1, border_color=BORDER_SOFT,
        )
        output_box.pack(fill="both", expand=True, pady=(10, 0))

        win.hash_rows = []
        win.document_hash_rows = []
        win.hash_status_var = hash_status_var
        win.generate_button = generate_button
        win.copy_button = copy_button
        win.docx_button = docx_button
        win.output_box = output_box

        # As variaveis (volume, hashes, quantidade) so ficam corretas depois que o
        # calculo em segundo plano termina — o botao fica desabilitado ate la para
        # nunca gerar o termo com placeholders por engano (ver CHANGELOG).
        #
        # So entra no termo o que esta marcado na lista (mesmo filtro que "Iniciar"
        # usa em selected_entries) — se a lista nunca foi carregada nesta sessao (sem
        # checkboxes pra consultar), cai no fallback de carregar tudo do CSV direto.
        output_dir = self.output_dir_var.get().strip()
        if self.entries:
            entries = [
                e for e in self.entries
                if self.rows.get(e.file_name) and self.rows[e.file_name].selected_var.get()
            ]
        else:
            entries = []
            if self.source_path_var.get().strip():
                try:
                    entries = self.provider.load_entries(Path(self.source_path_var.get().strip()))
                except core.PipelineError:
                    entries = []

        if entries and output_dir:
            try:
                max_workers = max(1, int(self.workers_var.get()))
            except ValueError:
                max_workers = 4

            generate_button.configure(state="disabled", text="Calculando hashes…")
            hash_status_var.set("Calculando hashes dos arquivos recebidos…")
            result_queue: queue.Queue = queue.Queue()
            progress_queue: queue.Queue = queue.Queue()
            win._hash_progress: dict[str, core.ProgressEvent] = {}
            win._hash_trackers: dict[str, core.ProgressTracker] = {}
            win._hash_start = time.monotonic()

            def on_progress(entry: core.FileEntry, bytes_done: int, bytes_total: int, phase: str) -> None:
                progress_queue.put((entry.file_name, bytes_done, bytes_total))

            def worker() -> None:
                rows = report.compute_hash_rows_cached(
                    entries, Path(output_dir), max_workers=max_workers, on_progress=on_progress,
                    provider=self.provider,
                )
                result_queue.put((rows, report.compute_document_hash_rows(Path(output_dir), self.provider)))

            threading.Thread(target=worker, daemon=True).start()
            self._poll_report_hash_queue(win, result_queue, progress_queue)
        elif self.entries and not entries:
            hash_status_var.set(
                "Nenhum arquivo selecionado na lista — marque ao menos um arquivo antes de gerar o termo."
            )

    def _poll_report_hash_queue(self, win, result_queue: "queue.Queue", progress_queue: "queue.Queue"):
        if not win.winfo_exists():
            return

        try:
            rows, document_rows = result_queue.get_nowait()
        except queue.Empty:
            while True:
                try:
                    name, bytes_done, bytes_total = progress_queue.get_nowait()
                except queue.Empty:
                    break
                win._hash_progress[name] = core.ProgressEvent(name, bytes_done, bytes_total, phase="hash")

            if win._hash_progress:
                agg = core.aggregate_progress(win._hash_progress, win._hash_start)
                if agg["bytes_known"] > 0:
                    speed_text = (
                        f"{report.humanize_bytes(agg['speed_bps'])}/s" if agg["speed_bps"] > 0 else "calculando velocidade…"
                    )
                    eta_text = core.format_eta(agg["eta_seconds"]) if agg["eta_seconds"] is not None else "--:--"
                    win.hash_status_var.set(
                        f"Calculando hashes… {report.humanize_bytes(agg['bytes_done'])}/"
                        f"{report.humanize_bytes(agg['bytes_known'])}  ·  {speed_text}  ·  ETA {eta_text}"
                    )
            win.after(150, self._poll_report_hash_queue, win, result_queue, progress_queue)
            return

        win.hash_rows = rows
        win.document_hash_rows = document_rows
        win.generate_button.configure(state="normal", text="Gerar termo")
        faltando = [row.file_name for row in rows if row.digest is None]
        total_volume = report.humanize_bytes(sum(row.size_bytes for row in rows))
        status = f"{len(rows)} arquivo(s) encontrado(s), volume total {total_volume}."
        if document_rows:
            status += f" Mais {len(document_rows)} documento(s) PDF do provedor."
        if faltando:
            status += f" Não encontrados em disco: {', '.join(faltando)}."
        win.hash_status_var.set(status)

    def _build_report_data(self, win) -> report.ReportData:
        v = win.vars
        return report.ReportData(
            numero_processo=v["numero_processo"].get().strip(),
            numero_pic=v["numero_pic"].get().strip(),
            orgao_execucao=v["orgao_execucao"].get().strip(),
            data=v["data"].get().strip(),
            hora=v["hora"].get().strip(),
            responsavel=v["responsavel"].get().strip(),
            matricula=v["matricula"].get().strip(),
            recebido_portal=v["recebido_portal"].get(),
            recebido_email=v["recebido_email"].get(),
            procedimento_referenciado=v["procedimento_referenciado"].get().strip(),
            id_documento=v["id_documento"].get().strip(),
            disponibiliza_original=v["disponibiliza_original"].get(),
            disponibiliza_processada=v["disponibiliza_processada"].get(),
            hash_rows=win.hash_rows,
            document_hash_rows=win.document_hash_rows,
            gpg_passphrase=self.passphrase_var.get(),
            incluir_senha_gpg=v["incluir_senha_gpg"].get(),
            **report.provider_fields(win.provider),
        )

    def _generate_report_text(self, win):
        data = self._build_report_data(win)
        text = report.render_report(data)
        win.output_box.configure(state="normal")
        win.output_box.delete("1.0", "end")
        win.output_box.insert("1.0", text)
        win.copy_button.configure(state="normal")

    def _copy_report_text(self, win):
        text = win.output_box.get("1.0", "end-1c")
        data = self._build_report_data(win)
        html_fragment = report.render_report_html(data)

        copied_formatted = False
        if os.name == "nt":
            try:
                _set_windows_html_clipboard(text, html_fragment)
                copied_formatted = True
            except Exception as exc:  # noqa: BLE001 - nunca deixar a copia quebrar por causa disso
                self.log_queue.put(
                    core.log_line(
                        "WARN", "relatorio", "falha ao copiar formatado, usando texto simples", erro=str(exc)
                    )
                )

        if not copied_formatted:
            win.clipboard_clear()
            win.clipboard_append(text)
            try:
                win.clipboard_append(html_fragment, type="text/html")
            except tk.TclError:
                pass

        original_text = win.copy_button.cget("text")
        win.copy_button.configure(text="Copiado!")
        win.after(1500, lambda: win.copy_button.configure(text=original_text))

    def _save_report_docx(self, win):
        data = self._build_report_data(win)
        path = filedialog.asksaveasfilename(
            title="Salvar Termo de Recebimento",
            defaultextension=".docx",
            filetypes=[("Documento Word", "*.docx")],
            initialfile="termo_recebimento.docx",
        )
        if not path:
            return

        try:
            report.build_docx(data, Path(path))
        except ImportError:
            messagebox.showerror(
                APP_TITLE,
                "A biblioteca 'python-docx' não está instalada.\n\n"
                "Rode: pip install -r joacloud/requirements.txt",
            )
            return
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror(APP_TITLE, f"Não foi possível salvar o .docx:\n{exc}")
            return

        original_text = win.docx_button.cget("text")
        win.docx_button.configure(text="Salvo!")
        win.after(1500, lambda: win.docx_button.configure(text=original_text))
        messagebox.showinfo(APP_TITLE, f"Termo salvo em:\n{path}")


def main():
    app = JoaCloudGUI()
    app.mainloop()


if __name__ == "__main__":
    main()
