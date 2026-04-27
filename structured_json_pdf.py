import json
import os
from importlib import reload
from pathlib import Path

from PIL import Image
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas


def _page_styles(data):
    styles = {}
    for style in data.get("style") or []:
        for style_id in style.get("c") or []:
            styles.setdefault(style_id, {}).update(style.get("s") or {})
    return styles


def _safe_float(value, fallback=0.0):
    try:
        return float(value)
    except Exception:
        return fallback


def normalize_text_for_pdf(text, default_font=None):
    if default_font:
        text = text.replace("•", "·")
    return text


def choose_pdf_font_for_text(text, default_font=None):
    if default_font and text.strip() in {"•", "·"}:
        return "Helvetica"
    return default_font


def _register_page_fonts(temp_dir, pagenum):
    reload(pdfmetrics)
    fonts = []
    suffix = f"{pagenum:04x}.ttf"
    for path in Path(temp_dir).glob("*.ttf"):
        if path.name.endswith(suffix):
            try:
                pdfmetrics.registerFont(TTFont(path.stem, str(path)))
                fonts.append(path.stem)
            except Exception:
                pass
    return fonts


def save_structured_page_pdf(temp_dir, pagenum, font_replace=None, default_font=None):
    temp_dir = Path(temp_dir)
    font_replace = font_replace or {}
    data = json.loads((temp_dir / f"{pagenum}.json").read_text(encoding="utf-8"))
    page_info = data.get("page") or {}
    page_width = _safe_float(page_info.get("pw"), 892.979)
    page_height = _safe_float(page_info.get("ph"), 1262.879)
    output_pdf = temp_dir / f"{pagenum}.pdf"

    pdf = canvas.Canvas(str(output_pdf), pagesize=(page_width, page_height))
    styles = _page_styles(data)
    if default_font:
        try:
            pdfmetrics.registerFont(UnicodeCIDFont(default_font))
        except Exception:
            pass
        page_fonts = [default_font]
    else:
        page_fonts = _register_page_fonts(temp_dir, pagenum)
    page_image_path = temp_dir / f"{pagenum}.png"
    page_image = Image.open(page_image_path) if page_image_path.exists() else None
    crop_dir = temp_dir / f"{pagenum}_crops"
    crop_dir.mkdir(exist_ok=True)

    for item in sorted(data.get("body") or [], key=lambda value: (value.get("p") or {}).get("z", 0)):
        item_type = item.get("t")
        position = item.get("p") or {}
        if item_type == "word":
            style = {}
            for style_id in item.get("r") or []:
                style.update(styles.get(style_id) or {})
            style.update(item.get("s") or {})

            text = normalize_text_for_pdf(str(item.get("c") or ""), default_font=default_font)
            if not text:
                continue
            text_object = pdf.beginText()
            font_size = _safe_float(style.get("font-size"), 16)
            y = page_height - _safe_float(position.get("y")) - font_size
            text_object.setTextOrigin(_safe_float(position.get("x")), y)

            font_family = style.get("font-family")
            if default_font:
                try:
                    text_object.setFont(choose_pdf_font_for_text(text, default_font), font_size)
                except Exception:
                    pass
            elif font_family:
                if font_family in page_fonts:
                    chosen_font = font_family
                elif font_family in font_replace and font_replace[font_family] in page_fonts:
                    chosen_font = font_replace[font_family]
                elif len(page_fonts) == 1:
                    chosen_font = page_fonts[0]
                    font_replace[font_family] = chosen_font
                elif page_fonts:
                    chosen_font = page_fonts[0]
                    font_replace[font_family] = chosen_font
                else:
                    chosen_font = None
                if chosen_font:
                    try:
                        text_object.setFont(chosen_font, font_size)
                    except Exception:
                        pass

            if style.get("letter-spacing"):
                text_object.setCharSpace(_safe_float(style.get("letter-spacing")))
            color = style.get("color")
            if isinstance(color, str) and re_match_hex_color(color):
                pdf.setFillColorRGB(int(color[1:3], 16) / 255, int(color[3:5], 16) / 255, int(color[5:7], 16) / 255)
                text_object.setFillColorRGB(int(color[1:3], 16) / 255, int(color[3:5], 16) / 255, int(color[5:7], 16) / 255)
            text_object.textLine(text)
            pdf.drawText(text_object)
        elif item_type == "pic" and page_image is not None:
            content = item.get("c") or {}
            try:
                ix = int(_safe_float(content.get("ix")))
                iy = int(_safe_float(content.get("iy")))
                iw = int(_safe_float(content.get("iw")))
                ih = int(_safe_float(content.get("ih")))
            except Exception:
                continue
            if iw <= 0 or ih <= 0:
                continue
            cropped = page_image.crop((ix, iy, ix + iw, iy + ih))
            crop_path = crop_dir / f"{len(list(crop_dir.glob('*.png'))) + 1}.png"
            cropped.save(crop_path)
            draw_width = _safe_float(position.get("w"), iw)
            draw_height = _safe_float(position.get("h"), ih)
            x = _safe_float(position.get("x"))
            y = page_height - _safe_float(position.get("y")) - draw_height
            pdf.drawImage(str(crop_path), x, y, width=draw_width, height=draw_height, mask="auto")

    pdf.showPage()
    pdf.save()
    if page_image is not None:
        page_image.close()
    return font_replace, output_pdf


def re_match_hex_color(value):
    return len(value) == 7 and value.startswith("#") and all(ch in "0123456789abcdefABCDEF" for ch in value[1:])
