import json
import queue
import threading

import pytest

import cli
import lers

# Respostas sinteticas no formato da API interna do LERS (arrays posicionais)
# — nunca usar dados reais de caso aqui.


def _request_row(request_id, ref, case, status, created_ms=1767225600000):
    row = [None] * 30
    row[0], row[2], row[6], row[11], row[12], row[23] = request_id, ref, "COURT_ORDER", case, created_ms, status
    return row


def _file_row(file_id, name, size, kind):
    row = [None] * 16
    row[0], row[3], row[4], row[6], row[10] = file_id, name, size, 1767225600000, kind
    return row


REQUESTS_BODY = json.dumps([[
    "lers:getrequestsbygaiaidresponse",
    [_request_row(111, "999000111", "0000001-00.2026.8.08.0001", "FILES_READY"),
     _request_row(222, "999000222", None, "CLOSED", created_ms=None)],
]])

FILES = [
    _file_row(1, "Carta 999000111.pdf", 30, "CORRESPONDENCE"),
    _file_row(2, "Oficio.pdf", 20, "LEGAL_PROCESS"),
    _file_row(3, "111-20260101-1.zip", 50, "PRODUCTION"),
]
FILES_BODY = json.dumps([["lers:getfilesbyrequestidresponse", FILES, [None] * 52]])


def test_parse_requests():
    reqs = lers.parse_requests(REQUESTS_BODY)
    assert [(r.request_id, r.google_ref, r.case_number, r.status) for r in reqs] == [
        (111, "999000111", "0000001-00.2026.8.08.0001", "FILES_READY"),
        (222, "999000222", "", "CLOSED"),
    ]
    assert reqs[0].status_label == "arquivos disponiveis"
    assert reqs[0].created == "01/01/2026"
    assert reqs[1].created == ""


def test_parse_files():
    files = lers.parse_files(FILES_BODY)
    assert [(f.file_id, f.name, f.size, f.kind) for f in files] == [
        (1, "Carta 999000111.pdf", 30, "CORRESPONDENCE"),
        (2, "Oficio.pdf", 20, "LEGAL_PROCESS"),
        (3, "111-20260101-1.zip", 50, "PRODUCTION"),
    ]


def test_parse_rejects_unexpected_format():
    with pytest.raises(lers.LersError):
        lers.parse_files(REQUESTS_BODY)  # tag de resposta errada
    with pytest.raises(lers.LersError):
        lers.parse_requests("nao e' json")


def test_find_request_by_id_or_google_ref():
    reqs = lers.parse_requests(REQUESTS_BODY)
    assert cli._find_request(reqs, "111").request_id == 111
    assert cli._find_request(reqs, "999000222").request_id == 222
    assert cli._find_request(reqs, "333") is None


def test_known_ports_reads_joakapple_and_chromium_files(tmp_path):
    browser = lers.LersBrowser(browser="/bin/true", profile_dir=tmp_path)
    assert browser._known_ports() == []
    (tmp_path / "joakapple-cdp-port").write_text("41000")
    (tmp_path / "DevToolsActivePort").write_text("42000\n/devtools/browser/x")
    assert browser._known_ports() == [41000, 42000]


# ------------------------------------------------------- download (CDP falso)


class FakeBrowserSession:
    """Imita o gerenciador de downloads: ao abrir a aba de download, grava o
    arquivo na pasta configurada e emite os eventos Browser.download*."""

    def __init__(self, contents: dict[int, bytes], rapt: bool):
        self.contents = contents
        self.rapt = rapt
        self.events: queue.Queue = queue.Queue()
        self.download_path = None
        self.urls: list[str] = []
        self.closed_tabs: list[str] = []

    def send(self, method, params=None, timeout=60):
        params = params or {}
        if method == "Browser.setDownloadBehavior":
            self.download_path = params.get("downloadPath")
        elif method == "Target.createTarget":
            url = params["url"]
            self.urls.append(url)
            file_id = int(url.split("fileId=")[1].split("&")[0])
            name, data = self.contents[file_id]
            from pathlib import Path

            (Path(self.download_path) / name).write_bytes(data)
            guid = f"g{file_id}"
            self.events.put({"method": "Browser.downloadWillBegin",
                             "params": {"guid": guid, "suggestedFilename": name}})
            self.events.put({"method": "Browser.downloadProgress",
                             "params": {"guid": guid, "receivedBytes": len(data), "totalBytes": len(data),
                                        "state": "completed"}})
            self.rapt = True  # depois do download_confirm o cookie existe
            return {"targetId": f"t{file_id}"}
        elif method == "Target.closeTarget":
            self.closed_tabs.append(params["targetId"])
        return {}

    def close(self):
        pass


class FakePage:
    def __init__(self, session: FakeBrowserSession):
        self.session = session

    def evaluate(self, expression, timeout=120):
        if "DOWNLOAD_FILES_RAPT" in expression:
            return "tok" if self.session.rapt else ""
        if "sessionIndex" in expression:
            return "0"
        raise AssertionError(expression)


def _client(contents, rapt):
    session = FakeBrowserSession(contents, rapt)
    browser = lers.LersBrowser(browser="/bin/true")
    browser.browser_session = lambda: session
    client = lers.LersClient(browser)
    client._page = FakePage(session)
    return client, session


def test_download_files_first_via_confirm_then_direct(tmp_path):
    files = [f for f in lers.parse_files(FILES_BODY) if f.kind in lers.DEFAULT_FILE_KINDS]
    contents = {1: ("Carta 999000111.pdf", b"c" * 30), 3: ("111-20260101-1.zip", b"z" * 50)}
    client, session = _client(contents, rapt=False)
    progress = []
    saved = client.download_files(111, files, tmp_path, on_progress=lambda f, d, t: progress.append((f.file_id, d, t)))

    assert [p.name for p in saved] == ["Carta 999000111.pdf", "111-20260101-1.zip"]
    assert (tmp_path / "111-20260101-1.zip").read_bytes() == b"z" * 50
    # sem o cookie rapt, o 1o arquivo vai pelo fluxo de reautenticacao do LERS
    assert "/download_confirm?continueTo=v2&listId=111&fileId=1" in session.urls[0]
    assert "/download_file?listId=111&fileId=3&rapt=tok" in session.urls[1]
    assert progress == [(1, 30, 30), (3, 50, 50)]
    assert session.closed_tabs == ["t1", "t3"]


def test_download_files_skips_existing_with_same_size(tmp_path):
    files = [f for f in lers.parse_files(FILES_BODY) if f.kind == "PRODUCTION"]
    (tmp_path / "111-20260101-1.zip").write_bytes(b"z" * 50)
    client, session = _client({}, rapt=True)
    saved = client.download_files(111, files, tmp_path)
    assert saved == [tmp_path / "111-20260101-1.zip"]
    assert session.urls == []


def test_download_files_size_mismatch_raises(tmp_path):
    files = [f for f in lers.parse_files(FILES_BODY) if f.kind == "PRODUCTION"]
    client, _ = _client({3: ("111-20260101-1.zip", b"z" * 49)}, rapt=True)
    with pytest.raises(lers.LersError, match="tamanho"):
        client.download_files(111, files, tmp_path)


def test_download_files_cancelled_before_start(tmp_path):
    files = lers.parse_files(FILES_BODY)
    client, session = _client({}, rapt=True)
    cancel = threading.Event()
    cancel.set()
    assert client.download_files(111, files, tmp_path, cancel_event=cancel) == []
    assert session.urls == []


def test_download_files_rejects_path_in_name(tmp_path):
    bad = [lers.LersFile(file_id=9, name="../fora.zip", size=1, kind="PRODUCTION")]
    client, _ = _client({}, rapt=True)
    with pytest.raises(lers.LersError, match="suspeito"):
        client.download_files(111, bad, tmp_path)
