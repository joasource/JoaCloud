"""Gera o icone do JoaCloud (mesma paleta da GUI) em varios tamanhos + .ico/.png.

Fonte unica de verdade pro visual do icone. Requer Pillow (``pip install
Pillow``) e a fonte DejaVu Sans Bold (mesma familia usada na GUI).
Rode com ``python packaging/make_icon.py`` a partir da raiz do repo, ou de
qualquer lugar - os caminhos de saida sao resolvidos relativos a este
arquivo.
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ACCENT = "#0F6B62"
CREAM = "#FBFAF7"

SIZE = 1024
FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans-Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
]

PACKAGING_DIR = Path(__file__).resolve().parent


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    for path in FONT_CANDIDATES:
        if Path(path).is_file():
            return ImageFont.truetype(path, size)
    raise FileNotFoundError(
        "Nenhuma fonte bold encontrada; instale dejavu-fonts ou ajuste FONT_CANDIDATES."
    )


def make_master() -> Image.Image:
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Squircle de fundo (superellipse) na cor de destaque da GUI.
    radius = int(SIZE * 0.225)
    draw.rounded_rectangle([0, 0, SIZE - 1, SIZE - 1], radius=radius, fill=ACCENT)

    mask = Image.new("L", (SIZE, SIZE), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, SIZE - 1, SIZE - 1], radius=radius, fill=255)
    img.putalpha(mask)

    # Monograma "C" (de JoaCloud) em creme, bold, centralizado.
    font = _load_font(int(SIZE * 0.56))
    text = "C"
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    pos = ((SIZE - tw) / 2 - bbox[0], (SIZE - th) / 2 - bbox[1] - SIZE * 0.035)
    draw.text(pos, text, font=font, fill=CREAM)

    return img


def main():
    master = make_master()

    # AppImage / icone da janela em runtime / README
    linux_dir = PACKAGING_DIR / "linux"
    linux_dir.mkdir(exist_ok=True)
    master.resize((256, 256), Image.LANCZOS).convert("RGB").save(linux_dir / "icon.png")

    # Windows .ico multi-resolucao (usado no --icon do PyInstaller)
    windows_dir = PACKAGING_DIR / "windows"
    windows_dir.mkdir(exist_ok=True)
    sizes = [16, 24, 32, 48, 64, 128, 256]
    master.save(windows_dir / "icon.ico", sizes=[(s, s) for s in sizes])

    # PNG grande pra material de divulgacao
    master.save(PACKAGING_DIR / "icon-1024.png")

    print("ok")


if __name__ == "__main__":
    main()
