"""
JoaCloud - registro dos provedores de nuvem suportados (Apple, Google...).

Cada provedor sabe de onde vem a lista de arquivos (CSV da Apple, pasta com o
retorno do LERS do Google), qual algoritmo de hash o provedor usa, e como
"abrir" cada arquivo depois de conferido (descriptografar .gpg; no Google os
ZIPs ficam como vieram).
O motor do pipeline (core.py) e' o mesmo para todos.

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

from providers.apple import AppleProvider
from providers.base import Provider
from providers.google import GoogleProvider

PROVIDERS: dict[str, Provider] = {
    p.key: p for p in (AppleProvider(), GoogleProvider())
}
DEFAULT_PROVIDER = "apple"


def get_provider(key: str | None = None) -> Provider:
    try:
        return PROVIDERS[key or DEFAULT_PROVIDER]
    except KeyError:
        raise ValueError(
            f"Provedor desconhecido: {key!r} (opcoes: {', '.join(PROVIDERS)})"
        ) from None
