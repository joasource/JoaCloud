"""
JoaCloud - ponto de entrada unico: GUI por padrao, CLI de texto com
--no-gui (so no build Linux — o .exe do Windows continua apontando pra
gui.py direto, ver .github/workflows/release.yml).

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

import sys


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--no-gui":
        import cli

        return cli.main(sys.argv[2:])
    import gui

    gui.main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
