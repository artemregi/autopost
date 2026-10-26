"""Обложка рилса: кадр из ролика + заголовок крупным белым шрифтом сверху. Монохром, без эффектов.
    python cover.py <видео> "<заголовок 2–5 слов>" [<выход.jpg>] [--at СЕК]
--at – секунда кадра (по умолчанию 1). Выбирай кадр, где лицо спокойное и глаза открыты.
Шрифт – Arial Bold из системы. Свой шрифт: строка AUTOPOST_FONT=<путь к .ttf> в .env."""
import config  # первым: переключает на окружение .venv
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, H = 1080, 1920
TOP = 300        # в сетке профиля Instagram показывает середину обложки – текст не у самого края
SIDE = 80
FONTS = [os.getenv("AUTOPOST_FONT", ""), "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
         "/Library/Fonts/Arial Bold.ttf", r"C:\Windows\Fonts\arialbd.ttf", r"C:\Windows\Fonts\segoeuib.ttf"]
NO_WINDOW = 0x08000000 if os.name == "nt" else 0   # Windows: без чёрного окна у ffmpeg


def font_path():
    for p in FONTS:
        if p and Path(p).exists():
            return p
    raise RuntimeError("Не нашёл жирный шрифт с русскими буквами. Впиши в .env AUTOPOST_FONT=<путь к .ttf>")


def grab(video, at, out):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(at), "-i", str(video), "-frames:v", "1", str(out)],
                   check=True, creationflags=NO_WINDOW)


def fill(img):
    """Растянуть кадр на 1080×1920 без искажений: лишнее по краям обрезается."""
    k = max(W / img.width, H / img.height)
    img = img.resize((round(img.width * k), round(img.height * k)), Image.LANCZOS)
    x, y = (img.width - W) // 2, (img.height - H) // 2
    return img.crop((x, y, x + W, y + H))


def wrap(draw, text, font, width):
    lines, cur = [], ""
    for word in text.split():
        test = (cur + " " + word).strip()
        if cur and draw.textlength(test, font=font) > width:
            lines.append(cur)
            cur = word
        else:
            cur = test
    return lines + [cur] if cur else lines


def make(video, title, out, at=1.0):
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "frame.png"
        grab(video, at, src)
        img = fill(Image.open(src).convert("RGB"))
    # сверху затемнение в чёрный, чтобы белый текст читался на любом кадре
    shade = Image.new("L", (1, H))
    for y in range(H):
        shade.putpixel((0, y), 200 if y < 750 else max(0, int(200 * (1 - (y - 750) / 450))))
    img = Image.composite(Image.new("RGB", (W, H)), img, shade.resize((W, H)))
    draw = ImageDraw.Draw(img)
    path = font_path()
    for size in range(130, 50, -6):   # самый крупный размер, при котором влезает в 3 строки
        font = ImageFont.truetype(path, size)
        lines = wrap(draw, title.strip(), font, W - 2 * SIDE)
        if len(lines) <= 3 and all(draw.textlength(ln, font=font) <= W - 2 * SIDE for ln in lines):
            break
    y = TOP
    for ln in lines:
        x = (W - draw.textlength(ln, font=font)) / 2
        draw.text((x, y), ln, font=font, fill="white")
        y += int(size * 1.12)
    out = Path(out)
    img.save(out, "JPEG", quality=90)
    return out


def to_jpg(image, out):
    """Своя обложка (PNG, JPG) → JPEG 1080×1920, как просит Instagram."""
    fill(Image.open(image).convert("RGB")).save(out, "JPEG", quality=90)
    return Path(out)


if __name__ == "__main__":
    args, at = [], 1.0
    it = iter(sys.argv[1:])
    for a in it:
        if a == "--at":
            at = float(next(it))
        else:
            args.append(a)
    if len(args) < 2:
        sys.exit(print(__doc__))
    video = Path(args[0])
    print(make(video, args[1], args[2] if len(args) > 2 else video.with_name(video.stem + "_cover.jpg"), at))
