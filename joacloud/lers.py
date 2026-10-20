"""
JoaCloud - login e download automatico do Google LERS (lers.google.com).

O LERS so' aceita login Google com 2FA (e reautenticacao periodica), entao o
app nao pede senha nenhuma: abre o navegador instalado (Edge no Windows;
Chrome/Chromium/Brave/Edge no Linux) com um perfil proprio do JoaCloud e a
porta de depuracao (CDP) ligada, o usuario faz o login normalmente, e o app
conversa com a pagina do LERS pelo CDP:

- a API interna do LERS e' chamada com ``fetch`` de dentro da propria pagina
  (os cookies da sessao nunca saem do navegador);
- os arquivos sao baixados pelo gerenciador de downloads do navegador, direto
  na pasta escolhida, exatamente como o LERS entrega (nada e' extraido).

A pasta resultante e' a "fonte" do provedor Google (providers/google.py).

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

import itertools
import json
import re
import os
import queue
import shutil
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import requests

import core

LERS_URL = "https://lers.google.com/"
_LOGGED_IN_RE = re.compile(r"^https://lers\.google\.com/u/\d+/v2(/|$|\?)")
# Sem nenhum byte novo nesse tempo, o download e' dado como travado.
STALL_TIMEOUT = 120

_LINUX_BROWSERS = (
    "google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
    "brave-browser", "microsoft-edge", "microsoft-edge-stable",
)


def _windows_browsers() -> list[Path]:
    roots = [os.environ.get(k) for k in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA")]
    rel = (
        r"Microsoft\Edge\Application\msedge.exe",
        r"Google\Chrome\Application\chrome.exe",
        r"BraveSoftware\Brave-Browser\Application\brave.exe",
    )
    return [Path(root) / r for r in rel for root in roots if root]


def find_browser() -> Optional[str]:
    """Executavel de um navegador baseado em Chromium, ou None."""
    if sys.platform == "win32":
        return next((str(p) for p in _windows_browsers() if p.is_file()), None)
    if sys.platform == "darwin":
        for app in ("Google Chrome", "Microsoft Edge", "Brave Browser", "Chromium"):
            path = Path("/Applications") / f"{app}.app" / "Contents" / "MacOS" / app
            if path.is_file():
                return str(path)
        return None
    return next((shutil.which(name) for name in _LINUX_BROWSERS if shutil.which(name)), None)


def default_profile_dir() -> Path:
    """Perfil do navegador so' do JoaCloud (mantem o login entre usos)."""
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "JoaCloud" / "navegador-lers"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class LersError(core.PipelineError):
    """Falha ao falar com o navegador ou com o LERS."""


class CDPSession:
    """Cliente minimo do Chrome DevTools Protocol sobre um websocket."""

    def __init__(self, ws_url: str):
        try:
            import websocket
        except ImportError as exc:  # pragma: no cover - dependencia em requirements.txt
            raise LersError("A biblioteca 'websocket-client' nao esta instalada.") from exc
        self._ws = websocket.create_connection(ws_url, timeout=30, suppress_origin=True)
        self._ids = itertools.count(1)
        self._pending: dict[int, queue.Queue] = {}
        self._lock = threading.Lock()
        self.events: queue.Queue = queue.Queue()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self) -> None:
        while True:
            try:
                raw = self._ws.recv()
            except Exception:  # noqa: BLE001 - conexao fechada: acorda quem espera
                with self._lock:
                    for q in self._pending.values():
                        q.put({"error": {"message": "conexao com o navegador encerrada"}})
                    self._pending.clear()
                self.events.put(None)
                return
            if not raw:
                continue
            msg = json.loads(raw)
            if "id" in msg:
                with self._lock:
                    q = self._pending.pop(msg["id"], None)
                if q:
                    q.put(msg)
            else:
                self.events.put(msg)

    def send(self, method: str, params: Optional[dict] = None, timeout: float = 60) -> dict:
        msg_id = next(self._ids)
        q: queue.Queue = queue.Queue()
        with self._lock:
            self._pending[msg_id] = q
        self._ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
        try:
            reply = q.get(timeout=timeout)
        except queue.Empty:
            raise LersError(f"o navegador nao respondeu a {method}") from None
        if "error" in reply:
            raise LersError(f"{method}: {reply['error'].get('message')}")
        return reply.get("result", {})

    def evaluate(self, expression: str, timeout: float = 120) -> Any:
        """Executa JS na pagina (aguardando Promise) e devolve o valor."""
        result = self.send(
            "Runtime.evaluate",
            {"expression": expression, "awaitPromise": True, "returnByValue": True},
            timeout=timeout,
        )
        if "exceptionDetails" in result:
            details = result["exceptionDetails"]
            text = details.get("exception", {}).get("description") or details.get("text")
            raise LersError(f"erro na pagina do LERS: {text}")
        return result.get("result", {}).get("value")

    def close(self) -> None:
        try:
            self._ws.close()
        except Exception:  # noqa: BLE001
            pass


class LersBrowser:
    """Navegador aberto com perfil proprio e porta CDP, apontando pro LERS."""

    def __init__(self, browser: Optional[str] = None, profile_dir: Optional[Path] = None):
        self.browser = browser or find_browser()
        self.profile_dir = Path(profile_dir or default_profile_dir())
        self.port = 0
        self.process: Optional[subprocess.Popen] = None

    @property
    def _port_file(self) -> Path:
        return self.profile_dir / "joacloud-cdp-port"

    def _known_ports(self) -> list[int]:
        """Portas de uma instancia ja aberta neste perfil: a que o JoaCloud
        anotou e a que o Chromium as vezes grava em DevToolsActivePort."""
        ports = []
        for path in (self._port_file, self.profile_dir / "DevToolsActivePort"):
            try:
                ports.append(int(path.read_text().split()[0]))
            except (OSError, ValueError, IndexError):
                pass
        return ports

    def _responding(self, port: int) -> bool:
        try:
            requests.get(f"http://127.0.0.1:{port}/json/version", timeout=1)
            return True
        except requests.RequestException:
            return False

    def start(self, url: str = LERS_URL, timeout: float = 30) -> None:
        """Abre o navegador no LERS — ou reaproveita o que o JoaCloud ja abriu
        (o perfil so' aceita uma instancia por vez)."""
        for port in self._known_ports():
            if self._responding(port):
                self.port = port
                if not self.lers_page():
                    requests.put(f"http://127.0.0.1:{port}/json/new?{url}", timeout=5)
                return

        if not self.browser:
            raise LersError(
                "Nenhum navegador compativel encontrado (Edge, Chrome, Chromium ou Brave)."
            )
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self.port = _free_port()
        self.process = subprocess.Popen(
            [
                self.browser,
                f"--user-data-dir={self.profile_dir}",
                f"--remote-debugging-port={self.port}",
                "--remote-allow-origins=http://127.0.0.1",
                "--no-first-run",
                "--no-default-browser-check",
                url,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise LersError(
                    "O navegador fechou logo ao abrir. Se ja houver uma janela do JoaCloud "
                    "com o LERS aberta, feche-a e tente de novo."
                )
            if self._responding(self.port):
                self._port_file.write_text(str(self.port))
                return
            time.sleep(0.3)
        raise LersError("O navegador nao abriu a porta de depuracao a tempo.")

    def targets(self) -> list[dict]:
        return requests.get(f"http://127.0.0.1:{self.port}/json/list", timeout=5).json()

    def lers_page(self) -> Optional[dict]:
        """Aba aberta no LERS (a primeira), ou None."""
        for t in self.targets():
            if t.get("type") == "page" and t.get("url", "").startswith("https://lers.google.com/"):
                return t
        return None

    def connect(self, target: dict) -> CDPSession:
        return CDPSession(target["webSocketDebuggerUrl"])

    def browser_session(self) -> CDPSession:
        info = requests.get(f"http://127.0.0.1:{self.port}/json/version", timeout=5).json()
        return CDPSession(info["webSocketDebuggerUrl"])

    def close(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()


# ------------------------------------------------------------------ API do LERS
#
# A API interna recebe POST com corpo ``data=<JSON>`` (arrays posicionais) e
# responde com o prefixo anti-JSON-hijacking ``)]}'``. Exige o header
# X-XSRF-TOKEN; o token vem no erro 400 da primeira chamada sem ele.

_API_JS = r"""
window.__joacloud = window.__joacloud || {};
window.__joacloud.call = async (method, payload) => {
  const base = '/api/u/' + ((window.LERS_GLOBALS || {}).sessionIndex || '0') + '/';
  const go = (tok) => fetch(base + method, {
    method: 'POST', credentials: 'same-origin',
    headers: Object.assign({'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8'},
                           tok ? {'X-XSRF-TOKEN': tok} : {}),
    body: 'data=' + encodeURIComponent(JSON.stringify(payload)),
  });
  const strip = (t) => t.slice(t.indexOf('\n') + 1);
  let r = await go(window.__joacloud.xsrf);
  let t = await r.text();
  if (r.status === 400 && t.includes('"xsrf"')) {
    window.__joacloud.xsrf = JSON.parse(strip(t))[0][10][0]["48448350"][1];
    r = await go(window.__joacloud.xsrf);
    t = await r.text();
  }
  return JSON.stringify([r.status, strip(t)]);
};
"""

REQUEST_STATUS_LABELS = {
    "FILES_READY": "arquivos disponiveis",
    "IN_PROCESS": "em andamento",
    "CLOSED": "encerrado",
    "NO_INFO": "sem informacoes",
    "WITHDRAWN": "retirado",
}

# Tipos de arquivo de um request. LEGAL_PROCESS e' o oficio/decisao enviado
# pelo proprio orgao — por padrao so' baixa o que o Google produziu.
DEFAULT_FILE_KINDS = ("PRODUCTION", "CORRESPONDENCE")

# Registro, na pasta de destino, do que veio do LERS (nome -> tipo). E' por ele
# que o Termo sabe quais PDFs da pasta sao do Google, e nao colocados a mao.
MANIFEST_NAME = "joacloud_lers.json"


@dataclass
class LersRequest:
    request_id: int
    google_ref: str  # "Ref. Interna" do Google (aparece nos PDFs)
    case_number: str  # numero do processo/procedimento informado no LERS
    kind: str
    status: str
    created_ms: Optional[int]

    @property
    def status_label(self) -> str:
        return REQUEST_STATUS_LABELS.get(self.status, self.status.lower())

    @property
    def created(self) -> str:
        if not self.created_ms:
            return ""
        return datetime.fromtimestamp(self.created_ms / 1000, tz=timezone.utc).strftime("%d/%m/%Y")


@dataclass
class LersFile:
    file_id: int
    name: str
    size: int
    kind: str
    uploaded_ms: Optional[int] = None


def _at(row: list, index: int, default=None):
    return row[index] if isinstance(row, list) and len(row) > index and row[index] is not None else default


def _unwrap(body: str, response_tag: str) -> list:
    """Payload de uma resposta da API: ``[[<tag>, <dados>...]]`` -> [<dados>...]."""
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise LersError(f"resposta inesperada do LERS: {exc}") from exc
    if not (isinstance(data, list) and data and isinstance(data[0], list) and data[0][:1] == [response_tag]):
        raise LersError("resposta inesperada do LERS (formato mudou?)")
    return data[0]


def parse_requests(body: str) -> list[LersRequest]:
    rows = _at(_unwrap(body, "lers:getrequestsbygaiaidresponse"), 1, [])
    return [
        LersRequest(
            request_id=int(row[0]),
            google_ref=str(_at(row, 2, "")),
            case_number=str(_at(row, 11, "")),
            kind=str(_at(row, 6, "")),
            status=str(_at(row, 23, "")),
            created_ms=_at(row, 12),
        )
        for row in rows
        if isinstance(row, list) and row and isinstance(row[0], int)
    ]


def parse_files(body: str) -> list[LersFile]:
    rows = _at(_unwrap(body, "lers:getfilesbyrequestidresponse"), 1, [])
    return [
        LersFile(
            file_id=int(row[0]),
            name=str(_at(row, 3, "")),
            size=int(_at(row, 4, 0)),
            kind=str(_at(row, 10, "")),
            uploaded_ms=_at(row, 6),
        )
        for row in rows
        if isinstance(row, list) and row and isinstance(row[0], int)
    ]


def _safe_file_name(name: str) -> bool:
    return bool(name) and name not in (".", "..") and "/" not in name and "\\" not in name


def read_manifest(folder: Path) -> dict[str, dict]:
    """{nome_do_arquivo: {"kind", "size", "file_id", "request_id"}} do que foi
    baixado do LERS para ``folder``; vazio se a pasta nao tem manifesto (ou
    ele esta' ilegivel)."""
    try:
        data = json.loads((Path(folder) / MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    files = data.get("files") if isinstance(data, dict) else None
    if not isinstance(files, list):
        return {}
    return {
        f["name"]: f for f in files
        if isinstance(f, dict) and isinstance(f.get("name"), str) and _safe_file_name(f["name"])
    }


def _record_manifest(dest_dir: Path, request_id: int, saved: list[tuple[Path, LersFile]]) -> None:
    """Acrescenta ``saved`` ao manifesto de ``dest_dir`` (mesmo nome: substitui)."""
    if not saved:
        return
    files = read_manifest(dest_dir)
    for path, f in saved:
        files[path.name] = {
            "name": path.name, "kind": f.kind, "size": f.size, "file_id": f.file_id, "request_id": int(request_id),
        }
    target = Path(dest_dir) / MANIFEST_NAME
    part = target.with_suffix(target.suffix + ".part")
    part.write_text(
        json.dumps({"files": [files[name] for name in sorted(files)]}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    part.replace(target)


class LersClient:
    """Conversa com o LERS pela aba logada no navegador do JoaCloud."""

    def __init__(self, browser: LersBrowser):
        self.browser = browser
        self._page: Optional[CDPSession] = None
        self._page_id: Optional[str] = None

    # ----------------------------------------------------------- sessao

    def wait_for_login(self, timeout: float = 600, cancel_event: Optional[threading.Event] = None) -> None:
        """Espera o usuario terminar o login (aba do LERS na area logada)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if cancel_event is not None and cancel_event.is_set():
                raise LersError("cancelado pelo usuario")
            for t in self.browser.targets():
                if t.get("type") == "page" and _LOGGED_IN_RE.match(t.get("url", "")):
                    self._attach(t)
                    if self._page.evaluate("!!window.LERS_GLOBALS"):
                        return
            time.sleep(1)
        raise LersError("Tempo esgotado esperando o login no LERS.")

    def _attach(self, target: dict) -> None:
        if self._page_id == target["id"] and self._page is not None:
            return
        if self._page is not None:
            self._page.close()
        self._page = self.browser.connect(target)
        self._page_id = target["id"]

    def _call(self, method: str, payload: list) -> str:
        if self._page is None:
            raise LersError("Sem sessao no LERS: faca o login primeiro.")
        raw = self._page.evaluate(_API_JS + f"window.__joacloud.call({json.dumps(method)}, {json.dumps(payload)})")
        status, body = json.loads(raw)
        if status == 401 or status == 403:
            raise LersError("O LERS recusou o acesso (sessao expirada?). Faca o login de novo.")
        if status != 200:
            raise LersError(f"O LERS respondeu HTTP {status} em {method}.")
        return body

    @property
    def session_index(self) -> str:
        return str(self._page.evaluate("(window.LERS_GLOBALS || {}).sessionIndex || '0'"))

    # ----------------------------------------------------------- consultas

    def list_requests(self) -> list[LersRequest]:
        return parse_requests(self._call("get_requests_by_gaia_id", ["lers:getrequestsbygaiaidrequest"]))

    def list_files(self, request_id: int) -> list[LersFile]:
        return parse_files(
            self._call("get_files_by_request_id", ["lers:getfilesbyrequestidrequest", int(request_id)])
        )

    # ----------------------------------------------------------- download

    def _rapt(self) -> str:
        return self._page.evaluate(
            "(document.cookie.match(/(?:^|; )DOWNLOAD_FILES_RAPT=([^;]+)/) || [])[1] || ''"
        )

    def download_files(
        self,
        request_id: int,
        files: list[LersFile],
        dest_dir: Path,
        log: Callable[[str], None] = lambda m: None,
        on_progress: Optional[Callable[[LersFile, int, int], None]] = None,
        cancel_event: Optional[threading.Event] = None,
        reauth_timeout: float = 600,
    ) -> list[Path]:
        """Baixa ``files`` para ``dest_dir`` pelo gerenciador de downloads do
        navegador, um por vez, exatamente como o LERS entrega.

        O download exige reautenticacao recente (token "rapt", guardado num
        cookie): sem ele, o primeiro arquivo vai pelo fluxo da propria
        interface do LERS (``download_confirm``), que pede a senha de novo no
        navegador. Arquivo ja presente com o tamanho certo e' pulado.

        O que fica em ``dest_dir`` (baixado agora ou ja presente) e' registrado
        no manifesto ``MANIFEST_NAME`` da pasta."""
        cancel_event = cancel_event or threading.Event()
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        for f in files:
            if not _safe_file_name(f.name):
                raise LersError(f"nome de arquivo suspeito vindo do LERS: {f.name!r}")

        api = f"https://lers.google.com/api/u/{self.session_index}"
        browser = self.browser.browser_session()
        saved: list[Path] = []
        recorded: list[tuple[Path, LersFile]] = []
        try:
            browser.send("Browser.setDownloadBehavior", {
                "behavior": "allow", "downloadPath": str(dest_dir.resolve()), "eventsEnabled": True,
            })
            for f in files:
                target = dest_dir / f.name
                if target.is_file() and target.stat().st_size == f.size:
                    log(core.log_line("DEBUG", "lers", "arquivo ja baixado, pulando", file=f.name, bytes=f.size))
                    if on_progress:
                        on_progress(f, f.size, f.size)
                    saved.append(target)
                    recorded.append((target, f))
                    continue
                if cancel_event.is_set():
                    break

                query = f"listId={int(request_id)}&fileId={f.file_id}"
                rapt = self._rapt()
                if rapt:
                    url, wait_start = f"{api}/download_file?{query}&rapt={rapt}", 60
                else:
                    log(core.log_line("INFO", "lers", "reautenticacao necessaria: confirme a senha no navegador",
                                      file=f.name))
                    url, wait_start = f"{api}/download_confirm?continueTo=v2&{query}", reauth_timeout
                # Aba aberta pelo navegador (CDP), nao pela pagina: nao cai no
                # bloqueio de "varios downloads automaticos". Quando vira
                # download o navegador fecha a aba sozinho; a do fluxo de
                # reautenticacao volta pro LERS e e' fechada aqui.
                tab_id = browser.send("Target.createTarget", {"url": url})["targetId"]
                try:
                    path = self._wait_download(browser, f, dest_dir, wait_start, log, on_progress, cancel_event)
                finally:
                    try:
                        browser.send("Target.closeTarget", {"targetId": tab_id}, timeout=5)
                    except LersError:
                        pass
                if path is None:
                    break
                saved.append(path)
                recorded.append((path, f))
        finally:
            _record_manifest(dest_dir, request_id, recorded)
            try:
                browser.send("Browser.setDownloadBehavior", {"behavior": "default"})
            except LersError:
                pass
            browser.close()
        return saved

    def _wait_download(self, browser, f, dest_dir, wait_start, log, on_progress, cancel_event) -> Optional[Path]:
        started = time.monotonic()
        guid = None
        name = f.name
        last_activity = time.monotonic()
        while True:
            if cancel_event.is_set():
                if guid:
                    try:
                        browser.send("Browser.cancelDownload", {"guid": guid})
                    except LersError:
                        pass
                log(core.log_line("WARN", "lers", "download cancelado pelo usuario", file=f.name))
                return None
            now = time.monotonic()
            if guid is None and now - started > wait_start:
                raise LersError(f"o download de {f.name} nao comecou (reautenticacao nao concluida?)")
            if guid is not None and now - last_activity > STALL_TIMEOUT:
                raise LersError(f"o download de {f.name} parou de avancar")
            try:
                ev = browser.events.get(timeout=0.5)
            except queue.Empty:
                continue
            if ev is None:
                raise LersError("a conexao com o navegador foi encerrada")
            method, params = ev.get("method"), ev.get("params", {})
            if method == "Browser.downloadWillBegin" and guid is None:
                guid = params["guid"]
                name = params.get("suggestedFilename") or f.name
                last_activity = now
                log(core.log_line("INFO", "lers", "download iniciado", file=name, tamanho=f.size))
            elif method == "Browser.downloadProgress" and params.get("guid") == guid:
                last_activity = now
                if on_progress:
                    on_progress(f, int(params.get("receivedBytes", 0)), int(params.get("totalBytes") or f.size))
                state = params.get("state")
                if state == "canceled":
                    raise LersError(f"o navegador cancelou o download de {f.name}")
                if state == "completed":
                    path = dest_dir / name
                    size = path.stat().st_size if path.is_file() else -1
                    if size != f.size:
                        raise LersError(
                            f"{name}: tamanho baixado ({size}) diferente do informado pelo LERS ({f.size})"
                        )
                    log(core.log_line("OK", "lers", "download concluido", file=name, bytes=size,
                                      duracao=f"{time.monotonic() - started:.2f}s"))
                    return path
