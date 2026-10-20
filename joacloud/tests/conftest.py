import sys
from pathlib import Path

# core.py e gui.py nao sao um pacote instalavel (sao rodados direto pelo
# PyInstaller a partir de joacloud/), entao os testes precisam do
# mesmo truque de sys.path que gui.py usa implicitamente ao rodar de dentro
# dessa pasta.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
