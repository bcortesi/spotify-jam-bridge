from io import BytesIO
from typing import Optional

import qrcode
from PIL import Image, ImageDraw


def render_png(payload: Optional[str], size: int = 600, dark: bool = True) -> bytes:
    """QR code PNG for the payload, or a placeholder when there is no active Jam."""
    bg = (18, 18, 18) if dark else (255, 255, 255)
    fg = (255, 255, 255) if dark else (0, 0, 0)

    if not payload:
        img = Image.new("RGB", (size, size), bg)
        d = ImageDraw.Draw(img)
        msg = "No active Jam"
        w = d.textlength(msg)
        d.text(((size - w) / 2, size / 2 - 8), msg, fill=fg)
        out = BytesIO()
        img.save(out, format="PNG")
        return out.getvalue()

    q = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=10, border=2)
    q.add_data(payload)
    q.make(fit=True)
    # Spotify-style: white module block on dark background, black modules.
    img = q.make_image(fill_color="black", back_color="white").convert("RGB")
    img = img.resize((size - 40, size - 40), Image.NEAREST)
    canvas = Image.new("RGB", (size, size), bg)
    canvas.paste(img, (20, 20))
    out = BytesIO()
    canvas.save(out, format="PNG")
    return out.getvalue()


def render_svg(payload: Optional[str]) -> str:
    if not payload:
        return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 200">'
                '<rect width="200" height="200" fill="#121212"/>'
                '<text x="100" y="105" fill="#fff" font-size="14" text-anchor="middle">No active Jam</text></svg>')
    import qrcode.image.svg as svg
    q = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=2,
                      image_factory=svg.SvgPathImage)
    q.add_data(payload)
    q.make(fit=True)
    out = BytesIO()
    q.make_image().save(out)
    return out.getvalue().decode()
