"""idcards/utils.py — QR code image generation."""
import io


def qr_png_bytes(data: str, box_size: int = 10, border: int = 2) -> bytes:
    """Render `data` as a QR code and return raw PNG bytes.

    Imports the `qrcode` package lazily. If it isn't installed, this raises
    only when a QR image is actually requested — it never breaks importing
    idcards.views / idcards.urls, which are loaded as part of every tenant's
    URLconf on every request.
    """
    import qrcode
    from qrcode.constants import ERROR_CORRECT_M

    qr = qrcode.QRCode(
        version=None,
        error_correction=ERROR_CORRECT_M,
        box_size=box_size,
        border=border,
    )
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    return buf.getvalue()


def placeholder_png_bytes(size: int = 300) -> bytes:
    """A plain 'QR unavailable' placeholder, drawn with Pillow only (no
    `qrcode` dependency). Used as a fallback so a missing/broken `qrcode`
    install shows a clear message instead of a broken-image icon."""
    from PIL import Image, ImageDraw

    img = Image.new('RGB', (size, size), color='white')
    draw = ImageDraw.Draw(img)
    pad = 10
    draw.rectangle([pad, pad, size - pad, size - pad], outline='black', width=3)
    text = "QR\nunavailable"
    draw.multiline_text((size / 2, size / 2), text, fill='black', anchor='mm', align='center')
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    return buf.getvalue()
