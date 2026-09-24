

import io
import json
import math
import os
import re
import sys
import tempfile
import uuid

import pymupdf as fitz

from flask import (
    Flask,
    jsonify,
    render_template_string,
    request,
    send_file,
)

from werkzeug.utils import secure_filename


# ============================================================
# KONSOL KODIROVKASI
#
# Windows konsolida Ó, Ǵ kabi belgilarni chop etishda
# UnicodeEncodeError chiqmasligi uchun.
# ============================================================

for _stream in (sys.stdout, sys.stderr):

    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


# ============================================================
# FLASK
# ============================================================

app = Flask(__name__)

app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024


# ============================================================
# ISHLASH PAPKALARI
#
# Vaqtinchalik fayllar uchun bitta papka ishlatiladi va u
# har bir so'rovdan keyin tozalanadi.
# ============================================================

WORK_FOLDER = os.path.join(
    tempfile.gettempdir(),
    "alifbo_work"
)

os.makedirs(
    WORK_FOLDER,
    exist_ok=True
)


# ============================================================
# REDAKSIYA REJIMLARI
#
# Eski kod apply_redactions() ni standart sozlamalarda chaqirar
# edi. Standart rejim redaksiya to'rtburchagiga tegib turgan
# rasmlarni oqlaydi va vektor chizmalarni (jadval, ramka,
# logotip) o'chiradi — hujjatning katta qismi yo'qolar edi.
# ============================================================

REDACT_IMAGE_NONE = getattr(
    fitz, "PDF_REDACT_IMAGE_NONE", 0
)

REDACT_LINE_ART_NONE = getattr(
    fitz, "PDF_REDACT_LINE_ART_NONE", 0
)


# ============================================================
# SHRIFTLAR
#
# MUHIM: Arial'da "Ǵ" (U+01F4) va "ǵ" (U+01F5) belgilari YO'Q.
# Shrift yetishmayotgan glifni bo'sh joy sifatida chizadi,
# shuning uchun matn "ko'rinmay qoladi". Shu sababli shrift
# tanlashda avval glif qamrovi tekshiriladi.
# ============================================================

FONT_DIR = r"C:\Windows\Fonts"

# Konvertatsiya natijasida uchraydigan barcha maxsus belgilar.
REQUIRED_GLYPHS = (
    "ʻ"   # ʻ  (oʻ / gʻ uchun)
    "Öö"   # Ó ó
    "Ǵǵ"   # Ǵ ǵ
    "Şş"   # Ş ş
    "Çç"   # Ç ç
    
)

FONT_CANDIDATES = {
    (False, False): [
        "arial.ttf",
        "arialuni.ttf",
        "Arial Unicode MS.ttf",
        "segoeui.ttf",
        "tahoma.ttf",
        "calibri.ttf",
        "verdana.ttf",
        "DejaVuSans.ttf",
        "NotoSans-Regular.ttf",
        "times.ttf",
    ],
    (True, False): [
        "arialbd.ttf",
        "segoeuib.ttf",
        "tahomabd.ttf",
        "calibrib.ttf",
        "verdanab.ttf",
        "DejaVuSans-Bold.ttf",
        "NotoSans-Bold.ttf",
        "timesbd.ttf",
    ],
    (False, True): [
        "ariali.ttf",
        "segoeuii.ttf",
        "calibrii.ttf",
        "verdanai.ttf",
        "DejaVuSans-Oblique.ttf",
        "timesi.ttf",
    ],
    (True, True): [
        "arialbi.ttf",
        "segoeuiz.ttf",
        "calibriz.ttf",
        "verdanaz.ttf",
        "DejaVuSans-BoldOblique.ttf",
        "timesbi.ttf",
    ],
}

# path -> fitz.Font
_FONT_OBJECTS = {}

# path -> PDF resurs nomi
_FONT_NAMES = {}

# (bold, italic, maxsus belgilar) -> path
_RESOLVED_FONTS = {}

FONT_WARNINGS = []


def _load_font(path):

    font = _FONT_OBJECTS.get(path)

    if font is None:
        font = fitz.Font(fontfile=path)
        _FONT_OBJECTS[path] = font

    return font


def _covers(font, text):

    for char in text:

        if char.isspace():
            continue

        if not font.has_glyph(ord(char)):
            return False

    return True


def _font_name(path):

    name = _FONT_NAMES.get(path)

    if name is None:
        name = "Alifbo-F{}".format(len(_FONT_NAMES) + 1)
        _FONT_NAMES[path] = name

    return name


_SCAN_HITS = []


def _candidate_paths(bold, italic):

    seen = []

    # Avval kerakli uslub, keyin oddiy shrift.
    for style in [(bold, italic), (False, False)]:

        for file_name in FONT_CANDIDATES.get(style, []):

            path = os.path.join(FONT_DIR, file_name)

            if path in seen:
                continue

            if os.path.exists(path):
                seen.append(path)

    # Papka skanerida topilgan shriftlar ham nomzod bo'lib qoladi,
    # shunda qimmat skaner har bir matn uchun takrorlanmaydi.
    for path in _SCAN_HITS:

        if path not in seen:
            seen.append(path)

    return seen


MAX_DIR_SCANS = 3

_scan_budget = MAX_DIR_SCANS


def _scan_font_dir(needed):

    """Butun shrift papkasidan qamrovi yetarli shrift qidiradi."""

    global _scan_budget

    if _scan_budget <= 0:
        return None

    _scan_budget -= 1

    try:
        entries = sorted(os.listdir(FONT_DIR))
    except OSError:
        return None

    for entry in entries:

        if not entry.lower().endswith((".ttf", ".otf")):
            continue

        path = os.path.join(FONT_DIR, entry)

        try:
            if _covers(_load_font(path), needed):

                if path not in _SCAN_HITS:
                    _SCAN_HITS.append(path)

                return path

        except Exception:
            continue

    return None


def resolve_font(bold, italic, text):

    """Berilgan matn uchun glif qamrovi yetarli shriftni qaytaradi."""

    specials = "".join(
        sorted({
            c for c in text
            if ord(c) > 0x7F and not c.isspace()
        })
    )

    key = (bold, italic, specials)

    if key in _RESOLVED_FONTS:
        return _RESOLVED_FONTS[key]

    needed = REQUIRED_GLYPHS + specials

    candidates = _candidate_paths(bold, italic)

    chosen = None

    for path in candidates:

        try:
            if _covers(_load_font(path), needed):
                chosen = path
                break
        except Exception:
            continue

    if chosen is None:
        chosen = _scan_font_dir(needed)

    if chosen is None:

        # Qamrovi yetarli shrift topilmadi. Matn umuman yo'qolmasligi
        # uchun birinchi mavjud shrift ishlatiladi.
        chosen = candidates[0] if candidates else None

        _warn_missing_glyphs(needed, chosen)

    _RESOLVED_FONTS[key] = chosen or ""

    return _RESOLVED_FONTS[key]


def _warn_missing_glyphs(needed, path):

    if not path or len(FONT_WARNINGS) >= 3:
        return

    missing = sorted({
        c for c in needed
        if not c.isspace() and not _covers_safe(path, c)
    })

    
def _covers_safe(path, char):

    if not path:
        return False

    try:
        return bool(_load_font(path).has_glyph(ord(char)))
    except Exception:
        return False


# Ishga tushishda asosiy shriftni aniqlab olamiz.
PRIMARY_FONT = resolve_font(False, False, REQUIRED_GLYPHS)


# ============================================================
# HIMOYALANADIGAN MATNLAR
# ============================================================

PROTECTED_PATTERNS = [

    # URL
    r"https?://[^\s<>()\[\]{}\"']+",

    # Email
    r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b",

    # Fayl nomlari
    r"\b[\w-]+\.(?:pdf|docx|doc|xlsx|xls|pptx|txt|png|jpg|jpeg|gif)\b",

    # Sana / vaqt
    r"\b\d{1,4}[-./]\d{1,2}[-./]\d{1,4}\b",

]

PROTECTED_RE = re.compile(
    "|".join(
        "(?:{})".format(pattern)
        for pattern in PROTECTED_PATTERNS
    ),
    flags=re.IGNORECASE
)


# ============================================================
# MATNNI HIMOYALASH
#
# Eski kod re.findall + str.replace ishlatar edi: bir xil matn
# ikki marta uchrasa joylar almashtirilmas, qisqa moslik esa
# uzunroq so'zning ichini buzib yuborar edi. Hozir bitta
# o'tishda, har bir moslikka bitta nishon beriladi.
# ============================================================

def mask_text(text):

    table = {}

    def repl(match):

        token = "\x00{}\x00".format(len(table))
        table[token] = match.group(0)

        return token

    return PROTECTED_RE.sub(repl, text), table


def unmask_text(text, table):

    if not table:
        return text

    def repl(match):
        return table.get(match.group(0), match.group(0))

    return re.sub("\x00\\d+\x00", repl, text)


# ============================================================
# APOSTROF VARIANTLARINI NORMLLASHTIRISH
#
# Eslatma: bu yerda so'z boshi chegarasi (\b) ISHLATILMAYDI.
# O'zbekcha "bog'cha", "o'g'il", "yig'ish" kabi so'zlarda
# apostrof so'zning o'rtasida keladi.
# ============================================================

APOSTROPHES = "’‘'ʻʼ`´"

NORMALIZE_RULES = [
    (re.compile("O[{}]".format(re.escape(APOSTROPHES))), "Oʻ"),
    (re.compile("o[{}]".format(re.escape(APOSTROPHES))), "oʻ"),
    (re.compile("G[{}]".format(re.escape(APOSTROPHES))), "Gʻ"),
    (re.compile("g[{}]".format(re.escape(APOSTROPHES))), "gʻ"),
]


def normalize_apostrophes(text):

    for pattern, replacement in NORMALIZE_RULES:
        text = pattern.sub(replacement, text)

    return text


# ============================================================
# O'ZBEK ALIFBOSI KONVERTORI
# ============================================================

MODE_TO_NEW = {
    "Oʻ": "Ö",
    "oʻ": "ö",
    "Gʻ": "Ğ",
    "gʻ": "ğ",
    "SH": "Ş",
    "Sh": "Ş",
    "sh": "ş",
    "CH": "Ç",
    "Ch": "Ç",
    "ch": "ç",
    
}

MODE_TO_OLD = {
    "Ö": "Oʻ",
    "ö": "oʻ",
    "Ğ": "Gʻ",
    "ğ": "gʻ",
    "Ş": "SH",
    "ş": "sh",
    "Ç": "CH",
    "ç": "ch",
  
}

# Uzunroq kalitlar avval almashtirilishi shart.
_RULES_TO_NEW = sorted(
    MODE_TO_NEW.items(),
    key=lambda item: len(item[0]),
    reverse=True
)

_RULES_TO_OLD = sorted(
    MODE_TO_OLD.items(),
    key=lambda item: len(item[0]),
    reverse=True
)


def _apply_rules(text, rules):

    pattern = re.compile(
        "|".join(
            re.escape(key) for key, _ in rules
        )
    )

    table = dict(rules)

    return pattern.sub(
        lambda match: table[match.group(0)],
        text
    )


def convert_uzbek_text(text, mode="1", normalize=None):

    """
    mode="1" : amaldagi lotin  -> yangi lotin
    mode="2" : yangi lotin     -> amaldagi lotin

    normalize=None bo'lsa rejimga qarab avtomatik tanlanadi:
    apostrof normallashtirish faqat 1-rejimda kerak, aks holda
    2-rejimda hujjatga tegishli bo'lmagan o'zgarish kiritiladi.
    """

    if not text:
        return ""

    if normalize is None:
        normalize = (mode == "1")

    masked, table = mask_text(text)

    if normalize:
        masked = normalize_apostrophes(masked)

    if mode == "1":
        masked = _apply_rules(masked, _RULES_TO_NEW)
    elif mode == "2":
        masked = _apply_rules(masked, _RULES_TO_OLD)

    return unmask_text(masked, table)


# ============================================================
# YORDAMCHI FUNKSIYALAR
# ============================================================

def _to_rgb(color_int):

    value = int(color_int or 0)

    return (
        ((value >> 16) & 0xFF) / 255.0,
        ((value >> 8) & 0xFF) / 255.0,
        (value & 0xFF) / 255.0,
    )


def _span_style(span):

    flags = span.get("flags", 0)

    return {
        "size": float(span.get("size") or 0.0),
        "bold": bool(flags & 16),
        "italic": bool(flags & 2),
        "color": _to_rgb(span.get("color", 0)),
        "dir": span.get("dir") or (1.0, 0.0),
        "origin": fitz.Point(span.get("origin") or span["bbox"][:2]),
        "rect": fitz.Rect(span["bbox"]),
    }


MIN_FONTSIZE = 3.0

# Ikkita span bitta bo'lak hisoblanishi uchun ruxsat etilgan
# oraliq (shrift o'lchamiga nisbatan).
SPAN_GAP_RATIO = 0.75

# Redaksiya to'rtburchagi shu qadar kengaytiriladi — float
# yaxlitlashida glif chetlari tashqarida qolib ketmasligi uchun.
REDACT_PADDING = 0.15

# Almashtirilgan shrift aslisidan shu foizgacha kengroq bo'lsa,
# o'lcham kichraytirilmaydi.
WIDTH_TOLERANCE = 1.02


def _gap_between(previous, style):

    """Ikki span orasidagi masofani matn yo'nalishi bo'yicha hisoblaydi."""

    previous_rect = previous["rect"]
    rect = style["rect"]

    dx, dy = style["dir"]

    if abs(dx) >= abs(dy):

        if dx >= 0:
            return rect.x0 - previous_rect.x1

        return previous_rect.x0 - rect.x1

    if dy >= 0:
        return rect.y0 - previous_rect.y1

    return previous_rect.y0 - rect.y1


def _build_runs(spans, styles):

    """
    Bir qatordagi span'larni yonma-yon bo'laklarga (run) ajratadi.

    Nima uchun kerak: PDF'da "sh" yoki "oʻ" ikki xil span'ga
    bo'linib qolishi mumkin (masalan apostrof boshqa shriftda
    yozilgan). Har span'ni alohida konvert qilsak bunday
    digraflar o'tkazib yuboriladi.
    """

    runs = []
    current = []

    for index in range(len(spans)):

        style = styles[index]

        if current:

            previous = styles[index - 1]

            limit = max(style["size"], previous["size"]) * SPAN_GAP_RATIO

            same_direction = (
                abs(style["dir"][0] - previous["dir"][0]) < 0.01
                and abs(style["dir"][1] - previous["dir"][1]) < 0.01
            )

            if not same_direction or _gap_between(previous, style) > limit:
                runs.append(current)
                current = []

        current.append(index)

    if current:
        runs.append(current)

    return runs


def _draw_text(page, text, style, target_width, font_path, stats):

    """Matnni asl baseline nuqtasiga, kerak bo'lsa burab yozadi."""

    if not text.strip():
        return

    size = style["size"]

    if size < MIN_FONTSIZE:
        size = MIN_FONTSIZE

    if font_path:

        font = _load_font(font_path)
        fontname = _font_name(font_path)

        try:
            page.insert_font(
                fontname=fontname,
                fontfile=font_path
            )
        except ValueError:
            # Bu shrift sahifada allaqachon ro'yxatdan o'tgan.
            pass

    else:

        font = None
        fontname = "helv"

    if target_width and target_width > 0:

        try:
            width = (
                font.text_length(text, fontsize=size)
                if font
                else fitz.get_text_length(text, "helv", size)
            )
        except Exception:
            width = 0.0

        limit = target_width * WIDTH_TOLERANCE

        if width > limit:

            scaled = size * (target_width / width)

            if scaled < size:
                size = max(scaled, MIN_FONTSIZE)
                stats["shrunk"] += 1

    origin = fitz.Point(style["origin"])

    dx, dy = style["dir"]

    angle = 0.0

    if abs(dx - 1.0) > 0.001 or abs(dy) > 0.001:
        angle = math.degrees(math.atan2(-dy, dx))

    morph = None

    if abs(angle) > 0.01:
        morph = (origin, fitz.Matrix(angle))

    page.insert_text(
        origin,
        text,
        fontsize=size,
        fontname=fontname,
        color=style["color"],
        morph=morph,
        render_mode=0,
        overlay=True,
    )


# ============================================================
# SAHIFANI KONVERT QILISH
# ============================================================

def _convert_page(page, mode, stats):

    data = page.get_text("dict")

    # Har bir edit: (style, yangi_matn, maqsad_en)
    edits = []

    # Qayta yoziladigan span'lar — ikki marta chizilmasligi uchun.
    handled = set()

    redact_rects = []

    lines = []

    for block in data.get("blocks", []):

        if block.get("type") != 0:
            continue

        for line in block.get("lines", []):

            spans = [
                span for span in line.get("spans", [])
                if span.get("text")
            ]

            if not spans:
                continue

            lines.append(spans)

    for spans in lines:

        styles = [_span_style(span) for span in spans]

        for run in _build_runs(spans, styles):

            run_text = "".join(spans[i]["text"] for i in run)

            if not run_text.strip():
                continue

            run_converted = convert_uzbek_text(run_text, mode)

            if run_converted == run_text:
                continue

            run_rect = fitz.Rect(styles[run[0]]["rect"])

            for index in run[1:]:
                run_rect |= styles[index]["rect"]

            for index in run:
                handled.add(id(spans[index]))

            redact_rects.append(run_rect)

            per_span = "".join(
                convert_uzbek_text(spans[i]["text"], mode)
                for i in run
            )

            if per_span == run_converted:

                # Har bir span o'z uslubi bilan qayta yoziladi.
                for index in run:

                    style = styles[index]

                    edits.append((
                        style,
                        convert_uzbek_text(spans[index]["text"], mode),
                        style["rect"].width,
                    ))

            else:

                # Digraf span chegarasiga to'g'ri keldi (masalan
                # "o" va "ʻ" ikki xil shriftda): butun bo'lak
                # bitta yozuv sifatida qaytariladi.
                stats["merged_runs"] += 1

                dominant = max(
                    (styles[i] for i in run),
                    key=lambda style: style["size"]
                )

                edits.append((
                    {
                        **dominant,
                        "origin": styles[run[0]]["origin"],
                        "dir": styles[run[0]]["dir"],
                    },
                    run_converted,
                    run_rect.width,
                ))

    if not edits:
        return 0

    # ------------------------------------------------------------
    # Qo'shni matnlarni himoya qilish
    #
    # Redaksiya to'rtburchagiga tegib turgan istalgan matn ham
    # o'chib ketadi. Shuning uchun bunday span'lar qayta yoziladigan
    # ro'yxatga olinadi — aks holda hujjatdan qism yo'qoladi.
    # ------------------------------------------------------------

    target_rects = [
        fitz.Rect(
            rect.x0 - REDACT_PADDING,
            rect.y0 - REDACT_PADDING,
            rect.x1 + REDACT_PADDING,
            rect.y1 + REDACT_PADDING,
        )
        for rect in redact_rects
    ]

    for spans in lines:

        for span in spans:

            if id(span) in handled:
                continue

            source = span["text"]

            if not source.strip():
                continue

            rect = fitz.Rect(span["bbox"])

            if not any(rect.intersects(target) for target in target_rects):
                continue

            handled.add(id(span))

            edits.append((
                _span_style(span),
                convert_uzbek_text(source, mode, normalize=False),
                rect.width,
            ))

            # Bu span ham o'chiriladi, demak uning qo'shnilarini
            # ham himoya qilish kerak.
            target_rects.append(rect)

    # ------------------------------------------------------------
    # Eski matnni o'chirish
    #
    # fill=False — rangli fonga oq to'rtburchak chizilmaydi.
    # images / line-art NONE — rasm, jadval va ramkalar saqlanadi.
    # ------------------------------------------------------------

    for rect in target_rects:
        page.add_redact_annot(rect, fill=False)

    try:
        page.apply_redactions(
            images=REDACT_IMAGE_NONE,
            graphics=REDACT_LINE_ART_NONE
        )
    except TypeError:
        # Eski PyMuPDF versiyalari uchun.
        page.apply_redactions()

    # ------------------------------------------------------------
    # Yangi matnni yozish
    # ------------------------------------------------------------

    for style, text, target_width in edits:

        if not text.strip():
            continue

        _draw_text(
            page,
            text,
            style,
            target_width,
            resolve_font(style["bold"], style["italic"], text),
            stats
        )

        stats["spans"] += 1

    return len(edits)


# ============================================================
# PDF PROCESSOR
# ============================================================

class PdfRejected(Exception):
    """Foydalanuvchiga ko'rsatish mumkin bo'lgan aniq xato."""


def process_pdf(input_path, output_path, mode="1"):

    stats = {
        "pages_total": 0,
        "pages_changed": 0,
        "spans": 0,
        "merged_runs": 0,
        "shrunk": 0,
        "skipped": [],
        "warnings": [],
    }

    try:
        doc = fitz.open(input_path)
    except Exception:
        raise PdfRejected(
            "PDF faylni o'qib bo'lmadi — fayl buzilgan bo'lishi mumkin."
        )

    try:

        if doc.needs_pass:

            if not doc.authenticate(""):
                raise PdfRejected(
                    "PDF parol bilan himoyalangan."
                )

        stats["pages_total"] = doc.page_count

        for number, page in enumerate(doc, start=1):

            try:

                changed = _convert_page(page, mode, stats)

                if changed:
                    stats["pages_changed"] += 1

            except PdfRejected:
                raise

            except Exception as error:

                # Tafsilot faqat jurnalga — mijozga yo'l va ichki
                # ma'lumotlar chiqib ketmasligi kerak.
                app.logger.exception(
                    "%s-sahifa konvertatsiya qilinmadi", number
                )

                stats["skipped"].append(
                    "{}-sahifa o'tkazib yuborildi".format(number)
                )

        doc.save(
            output_path,
            garbage=4,
            deflate=True,
            deflate_images=True,
            deflate_fonts=True,
        )

    finally:

        doc.close()

    if FONT_WARNINGS:
        stats["warnings"].extend(FONT_WARNINGS)

    return stats


# ============================================================
# HTML
# ============================================================

HTML_TEMPLATE = r"""
<!DOCTYPE html>
<html lang="uz">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>ALIFBO — Uzbek PDF Converter</title>

    <script src="https://cdn.tailwindcss.com"></script>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.1/css/all.min.css">
    <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@300;400;500;600;700;800;900&display=swap" rel="stylesheet">

    <script>
        tailwind.config = {
            theme: {
                extend: {
                    fontFamily: {
                        sans: ["Plus Jakarta Sans", "sans-serif"]
                    },
                    colors: {
                        brand: {
                            50:  "#eef2ff",
                            100: "#e0e7ff",
                            200: "#c7d2fe",
                            300: "#a5b4fc",
                            400: "#818cf8",
                            500: "#6366f1",
                            600: "#4f46e5",
                            700: "#4338ca",
                            800: "#3730a3",
                            900: "#312e81"
                        }
                    }
                }
            }
        }
    </script>

    <style>
        .bg-grid {
            background-image: radial-gradient(rgba(99, 102, 241, 0.15) 1px, transparent 1px);
            background-size: 24px 24px;
        }
        #toast { transition: opacity .25s ease, transform .25s ease; }
        .drop-active { border-color: #6366f1 !important; background: rgba(99,102,241,.08) !important; }
    </style>
</head>

<body class="bg-slate-950 text-slate-100 font-sans min-h-screen bg-grid flex flex-col">

    <div class="fixed top-0 left-1/2 -translate-x-1/2 w-[1000px] h-[400px] bg-gradient-to-tr from-brand-600/30 to-purple-600/20 blur-[120px] pointer-events-none -z-10 rounded-full"></div>

    <!-- HEADER -->
    <header class="border-b border-slate-800/80 bg-slate-950/60 backdrop-blur-md sticky top-0 z-50">
        <div class="max-w-7xl mx-auto px-6 h-20 flex items-center justify-between gap-4">

            <div class="flex items-center gap-3">
                <div class="w-10 h-10 rounded-xl bg-gradient-to-tr from-brand-600 to-violet-500 flex items-center justify-center shadow-lg shadow-brand-500/30">
                    <i class="fa-solid fa-language text-xl text-white"></i>
                </div>
                <div>
                    <span class="text-xl font-extrabold bg-gradient-to-r from-white via-slate-200 to-slate-400 bg-clip-text text-transparent">ALIFBO</span>
                    <span class="text-[10px] font-bold uppercase tracking-widest text-brand-500 block">PDF ENGINE v4.0</span>
                </div>
            </div>

            <div class="flex items-center gap-2 bg-slate-900/80 border border-slate-800 p-1.5 rounded-full">
                <span class="text-xs font-semibold px-3 py-1 text-slate-400 hidden sm:inline">Rejim:</span>
                <select id="modeSelect" class="bg-slate-800 hover:bg-slate-700 text-xs font-semibold text-white px-3 py-1.5 rounded-full border border-slate-700/50 focus:outline-none focus:ring-2 focus:ring-brand-500 transition cursor-pointer max-w-[240px]">
                    <option value="1">Amaldagi Lotin → Yangi Lotin (Ó, Ǵ, Ş, Ç, Ñ)</option>
                    <option value="2">Yangi Lotin → Amaldagi Lotin (Oʻ, Gʻ, Sh, Ch, Ng)</option>
                </select>
            </div>

        </div>
    </header>

    <!-- MAIN -->
    <main class="max-w-7xl mx-auto px-6 py-10 flex-1 w-full">

        <div class="text-center max-w-3xl mx-auto mb-10">
            <div class="inline-flex items-center gap-2 px-3 py-1.5 rounded-full bg-brand-500/10 border border-brand-500/20 text-brand-300 text-xs font-semibold mb-4">
                <i class="fa-solid fa-shield-halved"></i>
                PDF formatini saqlashga mo‘ljallangan konvertor
            </div>

            <h1 class="text-4xl sm:text-5xl font-black tracking-tight text-white mb-4 leading-tight">
                Hujjatlar Formatini Buzmasdan
                <br>
                <span class="bg-gradient-to-r from-brand-400 via-violet-400 to-pink-400 bg-clip-text text-transparent">Aqlli Konvertatsiya Qiling</span>
            </h1>
        </div>

        <div class="grid lg:grid-cols-2 gap-8 items-start">

            <!-- TEXT CONVERTER -->
            <div class="bg-slate-900/60 border border-slate-800 rounded-3xl p-6 backdrop-blur-xl shadow-2xl">

                <div class="flex items-center justify-between mb-4">
                    <div class="flex items-center gap-2">
                        <i class="fa-solid fa-paragraph text-brand-400"></i>
                        <h2 class="font-bold text-white">Matn Konvertori</h2>
                    </div>
                    <button type="button" onclick="clearText()" class="text-xs text-slate-500 hover:text-slate-300 transition">
                        <i class="fa-solid fa-rotate-left"></i> Tozalash
                    </button>
                </div>

                <textarea id="inputText" placeholder="Matnni kiriting..."
                    class="w-full h-44 bg-slate-950/80 border border-slate-800 rounded-2xl p-4 text-sm text-slate-200 placeholder-slate-600 focus:outline-none focus:ring-2 focus:ring-brand-500/50 resize-none"></textarea>

                <div class="flex items-center justify-between mt-4 mb-2">
                    <span class="text-xs font-semibold text-slate-400">Natija:</span>
                    <button type="button" onclick="copyOutput()" class="text-xs bg-brand-500/10 hover:bg-brand-500/20 text-brand-200 border border-brand-500/20 px-3 py-1 rounded-lg">
                        <i class="fa-regular fa-copy"></i> Nusxalash
                    </button>
                </div>

                <div id="outputText" data-empty="1"
                    class="w-full min-h-[140px] max-h-[220px] overflow-y-auto bg-slate-950 border border-slate-800 rounded-2xl p-4 text-sm text-brand-100 whitespace-pre-wrap"><span class="text-slate-600 italic">Natija bu yerda ko‘rinadi...</span></div>

            </div>

            <!-- PDF CONVERTER -->
            <div class="bg-gradient-to-b from-slate-900/90 to-slate-900/40 border border-slate-800 rounded-3xl p-6 backdrop-blur-xl shadow-2xl">

                <div class="flex items-center justify-between mb-5">
                    <div class="flex items-center gap-3">
                        <div class="p-3 rounded-xl bg-red-500/10 text-red-400">
                            <i class="fa-solid fa-file-pdf text-xl"></i>
                        </div>
                        <div>
                            <h2 class="font-bold text-white">PDF Hujjat Konvertori</h2>
                            <p class="text-xs text-slate-400">PDF → PDF · format, rasm va jadval saqlanadi</p>
                        </div>
                    </div>
                </div>

                <div id="dropZone"
                    class="border-2 border-dashed border-slate-700 hover:border-brand-500 rounded-2xl p-7 text-center bg-slate-950/60 transition">

                    <i class="fa-solid fa-file-pdf text-5xl text-red-400 mb-4"></i>

                    <label for="pdfFile" class="cursor-pointer block text-sm font-semibold text-brand-400 hover:underline mb-3">
                        PDF faylni tanlang yoki bu yerga tashlang
                    </label>

                    <input type="file" id="pdfFile" accept=".pdf,application/pdf"
                        class="text-xs text-slate-400 w-full max-w-xs mx-auto block cursor-pointer file:mr-3 file:px-3 file:py-1.5 file:rounded-lg file:border-0 file:bg-slate-800 file:text-slate-200 file:text-xs file:cursor-pointer">

                    <p id="fileNameDisplay" class="mt-4 text-xs text-emerald-400 font-semibold hidden"></p>
                </div>

                <button type="button" id="submitBtn" onclick="convertPdf()"
                    class="mt-6 w-full py-4 rounded-2xl bg-gradient-to-r from-brand-600 via-indigo-600 to-violet-600 hover:from-brand-500 hover:to-violet-500 disabled:opacity-50 disabled:cursor-not-allowed text-white font-bold text-sm shadow-xl transition flex items-center justify-center gap-2">
                    <i id="submitIcon" class="fa-solid fa-wand-magic-sparkles"></i>
                    <span id="submitLabel">PDF Hujjatni O‘girish va Yuklab Olish</span>
                </button>

                <div id="pdfStatus" class="mt-4 hidden rounded-2xl border p-4 text-xs leading-relaxed"></div>

            </div>

        </div>
    </main>

    <footer class="border-t border-slate-900 py-6 text-center text-xs text-slate-600">
        O‘zbek Alifbosi Konvertor Platformasi © 2026
    </footer>

    <!-- TOAST -->
    <div id="toast" class="fixed bottom-6 left-1/2 -translate-x-1/2 px-4 py-2.5 rounded-xl text-xs font-semibold bg-slate-800 border border-slate-700 text-white shadow-2xl opacity-0 pointer-events-none translate-y-2 z-[100]"></div>

    <script>
        const PLACEHOLDER = "Natija bu yerda ko‘rinadi...";

        function toast(message, ok = true) {
            const el = document.getElementById("toast");
            el.innerText = message;
            el.className = el.className.replace(/bg-\S+/g, "").trim() +
                (ok ? " bg-emerald-600/90" : " bg-rose-600/90");
            el.style.opacity = "1";
            el.style.transform = "translate(-50%, 0)";
            clearTimeout(el._t);
            el._t = setTimeout(() => {
                el.style.opacity = "0";
                el.style.transform = "translate(-50%, 8px)";
            }, 2600);
        }

        // ================================================
        // FAYL TANLASH
        // ================================================

        const fileInput = document.getElementById("pdfFile");
        const dropZone = document.getElementById("dropZone");

        fileInput.addEventListener("change", showName);

        ["dragenter", "dragover"].forEach(evt =>
            dropZone.addEventListener(evt, e => {
                e.preventDefault();
                dropZone.classList.add("drop-active");
            })
        );

        ["dragleave", "drop"].forEach(evt =>
            dropZone.addEventListener(evt, e => {
                e.preventDefault();
                dropZone.classList.remove("drop-active");
            })
        );

        dropZone.addEventListener("drop", e => {
            const files = e.dataTransfer && e.dataTransfer.files;
            if (files && files.length) {
                fileInput.files = files;
                showName();
            }
        });

        function showName() {
            const display = document.getElementById("fileNameDisplay");

            if (fileInput.files && fileInput.files.length > 0) {
                const file = fileInput.files[0];
                display.innerText = "Tanlangan fayl: " + file.name +
                    "  (" + (file.size / 1048576).toFixed(2) + " MB)";
                display.classList.remove("hidden");
            } else {
                display.classList.add("hidden");
            }
        }

        // ================================================
        // MATN
        // ================================================

        function clearText() {
            document.getElementById("inputText").value = "";
            const output = document.getElementById("outputText");
            output.dataset.empty = "1";
            output.innerHTML = '<span class="text-slate-600 italic">' + PLACEHOLDER + "</span>";
        }

        let textTimer = null;

        document.getElementById("inputText").addEventListener("input", () => {
            clearTimeout(textTimer);
            textTimer = setTimeout(convertText, 220);
        });

        async function convertText() {
            const text = document.getElementById("inputText").value;
            const mode = document.getElementById("modeSelect").value;
            const output = document.getElementById("outputText");

            if (!text.trim()) {
                clearText();
                return;
            }

            try {
                const response = await fetch("/convert-text", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ text: text, mode: mode })
                });

                if (!response.ok) throw new Error(response.status);

                const data = await response.json();
                output.dataset.empty = "0";
                output.innerText = data.result;

            } catch (error) {
                console.error(error);
                output.dataset.empty = "1";
                output.innerText = "Server bilan bog‘lanishda xatolik.";
            }
        }

        document.getElementById("modeSelect").addEventListener("change", convertText);

        async function copyOutput() {
            const output = document.getElementById("outputText");

            if (output.dataset.empty === "1") return;

            try {
                await navigator.clipboard.writeText(output.innerText);
                toast("Matn nusxalandi");
            } catch (error) {
                toast("Nusxalash amalga oshmadi.", false);
            }
        }

        // ================================================
        // PDF
        // ================================================

        async function convertPdf() {

            const button = document.getElementById("submitBtn");
            const icon = document.getElementById("submitIcon");
            const label = document.getElementById("submitLabel");
            const status = document.getElementById("pdfStatus");

            if (!fileInput.files || !fileInput.files.length) {
                toast("Avval PDF faylni tanlang.", false);
                return;
            }

            const file = fileInput.files[0];

            if (!/\.pdf$/i.test(file.name) && file.type !== "application/pdf") {
                toast("Faqat PDF fayl yuklash mumkin.", false);
                return;
            }

            const form = new FormData();
            form.append("file", file);
            form.append("mode", document.getElementById("modeSelect").value);

            button.disabled = true;
            icon.className = "fa-solid fa-circle-notch fa-spin";
            label.innerText = "Ishlanmoqda...";
            status.classList.add("hidden");

            try {
                const response = await fetch("/convert-pdf", {
                    method: "POST",
                    body: form
                });

                if (!response.ok) {
                    const message = await response.text();
                    throw new Error(message || ("HTTP " + response.status));
                }

                const blob = await response.blob();

                let report = null;
                try {
                    report = JSON.parse(response.headers.get("X-Alifbo-Report"));
                } catch (e) { /* ixtiyoriy */ }

                const url = URL.createObjectURL(blob);
                const link = document.createElement("a");
                link.href = url;
                link.download = response.headers.get("X-Alifbo-Filename") ||
                    ("converted_" + file.name);
                document.body.appendChild(link);
                link.click();
                link.remove();
                setTimeout(() => URL.revokeObjectURL(url), 4000);

                renderReport(report);
                toast("PDF tayyor va yuklandi");

            } catch (error) {
                console.error(error);
                status.className = "mt-4 rounded-2xl border p-4 text-xs leading-relaxed border-rose-500/40 bg-rose-500/10 text-rose-200";
                status.innerHTML = '<i class="fa-solid fa-triangle-exclamation mr-1"></i>' +
                    escapeHtml(error.message || "Noma'lum xatolik");
                toast("Xatolik yuz berdi.", false);

            } finally {
                button.disabled = false;
                icon.className = "fa-solid fa-wand-magic-sparkles";
                label.innerText = "PDF Hujjatni O‘girish va Yuklab Olish";
            }
        }

        function renderReport(report) {

            const status = document.getElementById("pdfStatus");

            if (!report) {
                status.classList.add("hidden");
                return;
            }

            const rows = [
                ["Jami sahifa", report.pages_total],
                ["O‘zgargan sahifa", report.pages_changed],
                ["Qayta yozilgan matn bo‘lagi", report.spans],
            ];

            if (report.merged_runs) rows.push(["Birlashtirilgan bo‘lak", report.merged_runs]);
            if (report.shrunk) rows.push(["Eniga moslashtirilgan", report.shrunk]);

            let html = '<div class="grid grid-cols-2 gap-2">';

            rows.forEach(([key, value]) => {
                html += '<div class="bg-slate-950/60 border border-slate-800 rounded-xl px-3 py-2">' +
                    '<div class="text-slate-500 text-[10px] uppercase tracking-wide">' + key + '</div>' +
                    '<div class="text-slate-100 font-bold text-sm">' + value + '</div></div>';
            });

            html += '</div>';

            if (report.skipped && report.skipped.length) {
                html += '<div class="mt-3 text-amber-300"><i class="fa-solid fa-circle-exclamation mr-1"></i>Ba\'zi sahifalar o\'tkazib yuborildi:<ul class="mt-1 ml-5 list-disc text-amber-200/80">';
                report.skipped.forEach(item => { html += "<li>" + escapeHtml(item) + "</li>"; });
                html += "</ul></div>";
            }

            if (report.warnings && report.warnings.length) {
                html += '<div class="mt-3 text-amber-300"><i class="fa-solid fa-font mr-1"></i>';
                report.warnings.forEach(item => { html += "<div>" + escapeHtml(item) + "</div>"; });
                html += "</div>";
            }

            if (!report.pages_changed) {
                html += '<div class="mt-3 text-slate-400"><i class="fa-solid fa-circle-info mr-1"></i>Hujjatda konvertatsiya qilinadigan matn topilmadi.</div>';
            }

            status.className = "mt-4 rounded-2xl border border-slate-800 bg-slate-950/50 p-4 text-xs leading-relaxed";
            status.innerHTML = html;
        }

        function escapeHtml(value) {
            return String(value).replace(/[&<>"']/g, ch => ({
                "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
            })[ch]);
        }
    </script>
</body>
</html>
"""


# ============================================================
# HOME
# ============================================================

@app.route("/")
def index():

    return render_template_string(HTML_TEMPLATE)


# ============================================================
# TEXT API
# ============================================================

@app.route("/convert-text", methods=["POST"])
def convert_text_route():

    data = request.get_json(silent=True) or {}

    text = data.get("text", "")
    mode = str(data.get("mode", "1"))

    if mode not in ("1", "2"):
        mode = "1"

    if not isinstance(text, str):
        return jsonify({"error": "Noto'g'ri matn."}), 400

    return jsonify({
        "result": convert_uzbek_text(text, mode)
    })


# ============================================================
# PDF API
# ============================================================

@app.route("/convert-pdf", methods=["POST"])
def convert_pdf_route():

    if "file" not in request.files:
        return "Fayl yuborilmadi.", 400

    file = request.files["file"]

    if not file or not file.filename:
        return "Fayl tanlanmadi.", 400

    mode = str(request.form.get("mode", "1"))

    if mode not in ("1", "2"):
        mode = "1"

    raw_name = file.filename or ""

    if not raw_name.lower().endswith(".pdf"):
        return "Faqat PDF fayl yuklash mumkin.", 400

    # Diskdagi yo'l faqat tasodifiy identifikatordan tuziladi —
    # foydalanuvchi fayl nomi yo'lga hech qachon qo'yilmaydi.
    token = uuid.uuid4().hex[:12]

    base_name = secure_filename(
        os.path.splitext(raw_name)[0]
    ) or "hujjat"

    input_path = os.path.join(WORK_FOLDER, token + "_in.pdf")
    output_path = os.path.join(WORK_FOLDER, token + "_out.pdf")

    download_name = "converted_" + base_name + ".pdf"

    try:

        file.save(input_path)

        print("PDF ishlanmoqda:", base_name, "| rejim:", mode)

        stats = process_pdf(input_path, output_path, mode)

        print(
            "Tayyor:",
            stats["pages_changed"], "/", stats["pages_total"],
            "sahifa,", stats["spans"], "matn bo'lagi"
        )

        # Fayl xotiraga o'qiladi, shundan keyin vaqtinchalik
        # fayllarni darhol o'chirish mumkin.
        with open(output_path, "rb") as handle:
            payload = io.BytesIO(handle.read())

        response = send_file(
            payload,
            as_attachment=True,
            download_name=download_name,
            mimetype="application/pdf"
        )

        response.headers["X-Alifbo-Report"] = json.dumps(
            stats,
            ensure_ascii=True
        )

        response.headers["X-Alifbo-Filename"] = download_name

        return response

    except PdfRejected as error:

        return str(error), 422

    except Exception as error:

        # Xato tafsiloti mijozga yuborilmaydi — faqat jurnalga.
        app.logger.exception("PDF konvertatsiyasi amalga oshmadi")

        print("PDF XATOSI:", repr(error))

        return "PDF bilan ishlashda xatolik yuz berdi.", 500

    finally:

        for path in (input_path, output_path):

            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass


# ============================================================
# XATO SAHIFALARI
# ============================================================

@app.errorhandler(413)
def file_too_large(error):

    return "Fayl juda katta. Maksimal hajm 50 MB.", 413


@app.errorhandler(404)
def not_found(error):

    return "Sahifa topilmadi.", 404


@app.errorhandler(405)
def method_not_allowed(error):

    return "Bu so'rov usuli qo'llab-quvvatlanmaydi.", 405


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    print("")
    print("=" * 60)
    print("ALIFBO — UZBEK PDF CONVERTER")
    print("=" * 60)

    if PRIMARY_FONT:

        print("PDF SHRIFFT:", PRIMARY_FONT)

        for warning in FONT_WARNINGS:
            print("OGOHLANTIRISH:", warning)

    else:

        print("DIQQAT: mos shrift topilmadi, standart Helvetica ishlatiladi.")

    print("")
    print("Server: http://127.0.0.1:5000")
    print("")
    print("=" * 60)

    # Debug rejimi faqat aniq yoqilganda ishlaydi:
    # Werkzeug debug konsoli tarmoqqa chiqarilsa xavfli.
    app.run(
        host="127.0.0.1",
        port=int(os.environ.get("PORT", 5000)),
        debug=os.environ.get("FLASK_DEBUG") == "1"
    )
