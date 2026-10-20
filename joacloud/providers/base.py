"""
JoaCloud - interface comum dos provedores de nuvem.

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

import threading
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

if TYPE_CHECKING:
    import requests

    from core import FileEntry, PipelineConfig

ProgressCallback = Callable[["FileEntry", int, int, str], None]


class Provider:
    """Comportamento especifico de uma nuvem; o resto do pipeline e' generico.

    As tres etapas do pipeline continuam sendo baixar -> verificar -> abrir,
    mas o que cada uma faz depende do provedor:

    - ``fetch``: Apple baixa por HTTP o link do CSV; Google copia, intacto, o
      ZIP de producao baixado do LERS.
    - verificacao: ``hash_entry`` com o ``hash_algorithm`` do provedor (Google
      le o ZIP interno direto de dentro do ZIP de producao, sem extrair).
    - ``unpack``: Apple descriptografa .gpg; Google nao tem essa etapa
      (``has_unpack_step = False``): os ZIPs ficam como vieram.
    """

    key: str = ""
    display_name: str = ""
    # Nome que vai no campo "Provedor de Aplicacao" do Termo de Recebimento.
    legal_name: str = ""
    hash_algorithm: str = "sha256"
    # Usada no Termo ("Os hashes referentes aos arquivos contidos nos ...").
    package_description: str = ""

    # Textos da interface (GUI/CLI).
    source_label: str = ""
    source_is_dir: bool = False
    download_description: str = ""
    verify_description: str = ""
    unpack_label: str = ""
    unpack_description: str = ""
    unpack_status: str = ""  # status do FileEntry durante a etapa de abrir
    unpack_dir_name: str = ""
    unpack_done_message: str = ""  # sufixo de "ja existia ..."
    needs_passphrase: bool = False
    # False quando o provedor nao tem a etapa 3 (os arquivos ficam como vieram).
    has_unpack_step: bool = True

    @property
    def hash_label(self) -> str:
        """sha256 -> SHA-256, para exibir ao usuario."""
        name = self.hash_algorithm.upper()
        return name[:3] + "-" + name[3:] if name.startswith("SHA") else name

    # ------------------------------------------------------------- entrada

    def load_entries(self, source: Path) -> list["FileEntry"]:
        raise NotImplementedError

    # ------------------------------------------------------- arquivos locais

    def local_path(self, entry: "FileEntry", output_dir: Path) -> Path:
        """Arquivo em disco (no destino) que contem ``entry``."""
        return Path(output_dir) / entry.file_name

    def hash_entry(
        self,
        entry: "FileEntry",
        output_dir: Path,
        on_progress: Optional[Callable[[int, int], None]] = None,
    ) -> str:
        from core import hash_of_file

        return hash_of_file(self.local_path(entry, output_dir), entry.hash_algorithm, on_progress=on_progress)

    def entry_size(self, entry: "FileEntry", output_dir: Path) -> int:
        """Tamanho em bytes do arquivo de ``entry`` (o que entra no hash)."""
        return self.local_path(entry, output_dir).stat().st_size

    def removable_paths(self, entry: "FileEntry", output_dir: Path) -> list[Path]:
        """O que "excluir" remove para ``entry``: baixado, .part e copia aberta."""
        dest = self.local_path(entry, output_dir)
        paths = [dest, dest.with_suffix(dest.suffix + ".part")]
        if self.has_unpack_step:
            paths.append(self.unpacked_path(entry, output_dir))
        return paths

    # ------------------------------------------------------------- etapas

    def prepare(self, config: "PipelineConfig", entries: list["FileEntry"], log: Callable[[str], None]) -> None:
        """Chamado uma vez, com o destino ja criado, antes dos arquivos do lote."""

    def fetch(
        self,
        session: "requests.Session",
        entry: "FileEntry",
        dest: Path,
        cancel_event: threading.Event,
        log: Callable[[str], None],
        on_progress: Optional[ProgressCallback] = None,
    ) -> bool:
        """Coloca o arquivo de ``entry`` em ``dest``. Em caso de falha,
        preenche entry.message e retorna False."""
        raise NotImplementedError

    def needs_unpack(self, entry: "FileEntry") -> bool:
        return False

    def unpacked_path(self, entry: "FileEntry", output_dir: Path) -> Path:
        return Path(output_dir) / self.unpack_dir_name / entry.file_name

    def unpack(
        self,
        entry: "FileEntry",
        src: Path,
        dest: Path,
        config: "PipelineConfig",
        log: Callable[[str], None],
    ) -> bool:
        raise NotImplementedError

    def preflight(self, config: "PipelineConfig", entries: list["FileEntry"]) -> None:
        """Valida pre-requisitos antes do lote comecar; levanta PipelineError."""

    def missing_tool_message(self) -> Optional[str]:
        """Mensagem de erro se falta alguma ferramenta externa pra etapa de abrir."""
        return None
