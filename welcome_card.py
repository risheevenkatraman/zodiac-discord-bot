"""Render personalized join cards locally; no image-generation service is needed."""

from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

ASSETS = Path(__file__).resolve().parent / 'assets'
SIZE = (1500, 500)


def card_font(size: int) -> ImageFont.FreeTypeFont:
    for path in ('DejaVuSans.ttf', '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
                 'C:/Windows/Fonts/arial.ttf'):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def render_join_card(name: str, member_count: int | None, avatar: bytes | None) -> BytesIO:
    with Image.open(ASSETS / 'zodiac-banner.png') as banner:
        card = ImageOps.fit(banner.convert('RGBA'), SIZE)
    overlay = Image.new('RGBA', SIZE)
    draw = ImageDraw.Draw(overlay)
    draw.rectangle((0, 0, *SIZE), fill=(18, 12, 32, 65))
    draw.rounded_rectangle((55, 25, 1445, 475), radius=12, fill=(18, 12, 32, 165))
    card = Image.alpha_composite(card, overlay)
    portrait = Image.new('RGB', (210, 210), '#7654ad')
    if avatar:
        try:
            with Image.open(BytesIO(avatar)) as source:
                portrait = ImageOps.fit(source.convert('RGB'), (210, 210))
        except (OSError, ValueError, Image.DecompressionBombError):
            avatar = None
    if not avatar:
        ImageDraw.Draw(portrait).text((105, 105), 'Z', font=card_font(100), fill='white', anchor='mm')
    mask = Image.new('L', (210, 210))
    ImageDraw.Draw(mask).ellipse((0, 0, 209, 209), fill=255)
    draw = ImageDraw.Draw(card)
    draw.ellipse((638, 53, 862, 277), fill='white')
    card.paste(portrait, (645, 60), mask)
    name = ' '.join(name.split())[:100] or 'New member'
    text = f'{name} just joined the server'
    font_size = 48
    font = card_font(font_size)
    while draw.textlength(text, font=font) > 1290 and font_size > 20:
        font_size -= 2
        font = card_font(font_size)
    while draw.textlength(text, font=font) > 1290 and len(name) > 1:
        name = name[:-1]
        text = f'{name}… just joined the server'
    draw.text((750, 345), text, font=font, fill='white', anchor='mm')
    count = f'Member #{member_count}' if member_count is not None else 'Welcome to Zodiac'
    draw.text((750, 407), count, font=card_font(36), fill='#e1d4f2', anchor='mm')
    result = BytesIO()
    card.convert('RGB').save(result, format='PNG')
    result.seek(0)
    return result
