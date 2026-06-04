import os
import re
import math
import json
import asyncio
import tempfile
from datetime import datetime, timezone

import pandas as pd
import streamlit as st
import gspread

from PIL import Image
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import InputMessagesFilterPhotos

from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase.pdfmetrics import stringWidth

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload


# =========================================================
# STREAMLIT PAGE CONFIG
# =========================================================

st.set_page_config(
    page_title="Telegram Scraper",
    page_icon="📄",
    layout="wide"
)


# =========================================================
# SECRETS / LOCAL FALLBACK
# =========================================================

def get_secret_value(key, default=None):
    try:
        return st.secrets[key]
    except Exception:
        return default


# =========================================================
# CONFIG GENERAL
# =========================================================

SPREADSHEET_ID = get_secret_value(
    "SPREADSHEET_ID",
    "1obBAodP9HqNzc3ATJy9opwlGWgYL3lymvlA_HErfPaQ"
)

DRIVE_ROOT_FOLDER_ID = get_secret_value(
    "DRIVE_ROOT_FOLDER_ID",
    "10TamCVdydCD8dYBO-xGpFIApMWtLXPp3"
)

TOKEN_FILE = "token_google.json"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

MESSAGES_SHEET_NAME = "messages"

SCREENSHOTS_FOLDER_NAME = "screenshots_receptor"
PDF_REPORTS_FOLDER_NAME = "pdf_reports"

LOCAL_SESSION_NAME = "scraper_rodrigo"

CHUNK_SIZE = 200


# =========================================================
# PDF CONFIG
# =========================================================

PAGE_W, PAGE_H = A4

MARGIN_LR = 28
MARGIN_TB = 28
GUTTER = 12

CONTENT_W = PAGE_W - 2 * MARGIN_LR
HALF_H = (PAGE_H - 2 * MARGIN_TB - GUTTER) / 2

CAPTION_H = 80
IMG_AREA_H = HALF_H - CAPTION_H - 6

FONT = "Helvetica"
FONT_BOLD = "Helvetica-Bold"
CAPTION_FS = 10
CAPTION_LH = 12


# =========================================================
# GOOGLE AUTH
# =========================================================

@st.cache_resource
def get_google_services():
    google_token_json = get_secret_value("GOOGLE_TOKEN_JSON")

    if google_token_json:
        token_info = json.loads(google_token_json)
        creds = Credentials.from_authorized_user_info(token_info, SCOPES)
    else:
        if not os.path.exists(TOKEN_FILE):
            raise FileNotFoundError(
                f"No encontré {TOKEN_FILE} ni GOOGLE_TOKEN_JSON en secrets."
            )

        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())

        if not google_token_json:
            with open(TOKEN_FILE, "w") as token:
                token.write(creds.to_json())

    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SPREADSHEET_ID)
    drive_service = build("drive", "v3", credentials=creds)

    return gc, sh, drive_service


# =========================================================
# DRIVE HELPERS
# =========================================================

def list_drive_folder_items(drive_service, folder_id):
    query = f"'{folder_id}' in parents and trashed = false"

    results = drive_service.files().list(
        q=query,
        fields="files(id, name, mimeType, webViewLink)"
    ).execute()

    return results.get("files", [])


def get_child_folder_id(drive_service, parent_folder_id, folder_name):
    query = (
        f"name = '{folder_name}' "
        f"and mimeType = 'application/vnd.google-apps.folder' "
        f"and '{parent_folder_id}' in parents "
        f"and trashed = false"
    )

    results = drive_service.files().list(
        q=query,
        fields="files(id, name)"
    ).execute()

    folders = results.get("files", [])

    if not folders:
        raise FileNotFoundError(
            f"No encontré la carpeta '{folder_name}' dentro de la carpeta raíz de Drive."
        )

    return folders[0]["id"]


def upload_file_to_drive(
    drive_service,
    local_file_path,
    parent_folder_id,
    drive_file_name,
    mimetype
):
    file_metadata = {
        "name": drive_file_name,
        "parents": [parent_folder_id]
    }

    media = MediaFileUpload(
        local_file_path,
        mimetype=mimetype,
        resumable=True
    )

    uploaded_file = drive_service.files().create(
        body=file_metadata,
        media_body=media,
        fields="id, name, webViewLink"
    ).execute()

    return uploaded_file


def download_drive_file(drive_service, file_id, local_file_path):
    request = drive_service.files().get_media(fileId=file_id)

    with open(local_file_path, "wb") as f:
        downloader = MediaIoBaseDownload(f, request)
        done = False

        while not done:
            _, done = downloader.next_chunk()

    return local_file_path


def find_drive_file_by_name(drive_service, parent_folder_id, file_name):
    query = (
        f"name = '{file_name}' "
        f"and '{parent_folder_id}' in parents "
        f"and trashed = false"
    )

    results = drive_service.files().list(
        q=query,
        fields="files(id, name, webViewLink)"
    ).execute()

    files = results.get("files", [])

    if files:
        return files[0]

    return None


def delete_drive_file_if_exists(drive_service, parent_folder_id, file_name):
    file_found = find_drive_file_by_name(
        drive_service=drive_service,
        parent_folder_id=parent_folder_id,
        file_name=file_name
    )

    if file_found:
        drive_service.files().delete(fileId=file_found["id"]).execute()
        return True

    return False


# =========================================================
# SHEETS HELPERS
# =========================================================

def get_messages_df(sh):
    ws_messages = sh.worksheet(MESSAGES_SHEET_NAME)
    records = ws_messages.get_all_records()
    df = pd.DataFrame(records)

    expected_cols = [
        "id_mensaje",
        "fecha",
        "City",
        "Drv_Phone",
        "App",
        "Category",
        "drive_file_id",
        "drive_url",
        "texto",
        "created_at"
    ]

    if df.empty:
        df = pd.DataFrame(columns=expected_cols)
    else:
        for col in expected_cols:
            if col not in df.columns:
                df[col] = None

        df = df[expected_cols]

    return df


def append_messages_to_sheet(sh, rows):
    ws_messages = sh.worksheet(MESSAGES_SHEET_NAME)

    if rows:
        ws_messages.append_rows(rows)


# =========================================================
# PARSEO TEXTO TELEGRAM
# =========================================================

CIUDADES = [
    "Ciudad de México",
    "Guadalajara",
    "Monterrey",
    "Puebla",
    "Chihuahua",
    "Ciudad Juárez"
]


def parsear_texto(texto):
    texto = str(texto or "").strip()

    drv_phone = None
    ciudad = None
    app = None
    categoria = None

    for c in CIUDADES:
        patron = rf"(.*)\s+{re.escape(c)}"
        match = re.search(patron, texto)
        if match:
            ciudad = c
            drv_phone = match.group(1).strip().replace(" ", "")
            break

    if "App Naranja" in texto:
        app = "DiDi"
    elif "App Negra" in texto:
        app = "UBER"

    if "Incentivos" in texto:
        categoria = "Incentivos"
    elif "Desglose" in texto:
        categoria = "Desglose de tarifa"
    elif "Recibos" in texto:
        categoria = "Recibos"

    return drv_phone, ciudad, app, categoria


# =========================================================
# TELEGRAM EXTRACTION
# =========================================================

def get_telegram_client(api_id, api_hash):
    telegram_string_session = get_secret_value("TELEGRAM_STRING_SESSION")

    if telegram_string_session:
        return TelegramClient(
            StringSession(telegram_string_session),
            api_id,
            api_hash
        )

    return TelegramClient(
        LOCAL_SESSION_NAME,
        api_id,
        api_hash
    )


async def extraer_telegram_a_drive(
    api_id,
    api_hash,
    chat_id,
    drive_service,
    screenshots_folder_id,
    ids_existentes,
    min_id=None,
    max_to_process=5
):
    registros_nuevos = []
    logs = []

    telegram_client = get_telegram_client(api_id, api_hash)

    async with telegram_client as client:
        iter_kwargs = {
            "reverse": False,
            "limit": None,
            "filter": InputMessagesFilterPhotos
        }

        if min_id is not None:
            iter_kwargs["min_id"] = int(min_id)

        procesados = 0
        revisados = 0

        async for msg in client.iter_messages(chat_id, **iter_kwargs):
            revisados += 1

            if msg.id in ids_existentes:
                continue

            texto_original = (msg.text or "(Sin texto)").replace("\n", " ").strip()

            fecha_utc = msg.date.astimezone(timezone.utc)
            fecha_str = fecha_utc.strftime("%Y-%m-%d")

            drv_phone, ciudad, app, categoria = parsear_texto(texto_original)

            with tempfile.TemporaryDirectory() as tmpdir:
                local_file_path = os.path.join(tmpdir, f"{msg.id}.jpg")

                try:
                    downloaded_path = await msg.download_media(file=local_file_path)
                except Exception as e:
                    logs.append(f"[ERROR] No se pudo descargar msg_id={msg.id}: {e}")
                    continue

                if downloaded_path is None or not os.path.exists(downloaded_path):
                    logs.append(f"[WARN] No se encontró archivo descargado para msg_id={msg.id}")
                    continue

                drive_file_name = f"{msg.id}.jpg"

                try:
                    uploaded = upload_file_to_drive(
                        drive_service=drive_service,
                        local_file_path=downloaded_path,
                        parent_folder_id=screenshots_folder_id,
                        drive_file_name=drive_file_name,
                        mimetype="image/jpeg"
                    )
                except Exception as e:
                    logs.append(f"[ERROR] No se pudo subir a Drive msg_id={msg.id}: {e}")
                    continue

            created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            registro = [
                int(msg.id),
                fecha_str,
                ciudad,
                drv_phone,
                app,
                categoria,
                uploaded["id"],
                uploaded.get("webViewLink", ""),
                texto_original,
                created_at
            ]

            registros_nuevos.append(registro)

            logs.append(
                f"[OK] msg_id={msg.id} | fecha={fecha_str} | city={ciudad} | app={app} | category={categoria}"
            )

            procesados += 1

            if procesados >= max_to_process:
                break

        logs.append(f"Revisados: {revisados}")
        logs.append(f"Procesados nuevos: {procesados}")

    return registros_nuevos, logs


# =========================================================
# PDF HELPERS
# =========================================================

def str_or_blank(x):
    return "" if pd.isna(x) else str(x)


def wrap_text(text, max_width, font=FONT, font_size=CAPTION_FS):
    text = str(text or "")

    if not text:
        return []

    words = text.split()
    lines = []
    cur = ""

    for w in words:
        cand = w if cur == "" else cur + " " + w

        if stringWidth(cand, font, font_size) <= max_width:
            cur = cand
        else:
            if cur:
                lines.append(cur)
            cur = w

    if cur:
        lines.append(cur)

    return lines


def build_caption_lines(row, width):
    kv = [
        ("id_mensaje", row.get("id_mensaje")),
        ("fecha", row.get("fecha")),
        ("City", row.get("City")),
        ("Drv_Phone", row.get("Drv_Phone")),
        ("App", row.get("App")),
        ("Category", row.get("Category")),
    ]

    lines = []

    for k, v in kv:
        base = f"{k}: {str_or_blank(v)}"
        wrapped = wrap_text(base, width, FONT, CAPTION_FS)

        if not wrapped:
            wrapped = [base]

        lines.extend(wrapped)

    return lines


def find_local_image_path(row, local_images_dir):
    msg_id = str(int(row["id_mensaje"]))

    for ext in [".jpg", ".jpeg", ".png"]:
        p = os.path.join(local_images_dir, msg_id + ext)

        if os.path.exists(p):
            return p

    return None


def draw_half(cnv, row, x0, y_top, width, local_images_dir):
    img_top = y_top
    img_height = IMG_AREA_H

    caption_top = y_top - IMG_AREA_H - 6
    caption_height = CAPTION_H

    img_path = find_local_image_path(row, local_images_dir)

    if img_path:
        try:
            with Image.open(img_path) as im:
                iw, ih = im.size

            scale = min(width / iw, img_height / ih)
            draw_w = iw * scale
            draw_h = ih * scale

            img_x = x0 + (width - draw_w) / 2
            img_y = img_top - draw_h

            cnv.drawImage(
                ImageReader(img_path),
                img_x,
                img_y,
                width=draw_w,
                height=draw_h,
                preserveAspectRatio=True,
                mask="auto"
            )
        except Exception as e:
            cnv.setStrokeColorRGB(1, 0, 0)
            cnv.rect(x0, img_top - img_height, width, img_height, stroke=1, fill=0)
            cnv.setFont(FONT_BOLD, 10)
            cnv.drawString(
                x0 + 4,
                img_top - 14,
                f"Error al cargar: {os.path.basename(str(img_path))} ({e})"
            )
    else:
        cnv.setStrokeColorRGB(1, 0.2, 0.2)
        cnv.rect(x0, img_top - img_height, width, img_height, stroke=1, fill=0)
        cnv.setFont(FONT_BOLD, 10)
        cnv.drawString(x0 + 4, img_top - 14, "Imagen no encontrada")

    cap_lines = build_caption_lines(row, width)

    cnv.setFont(FONT, CAPTION_FS)
    y_cursor = caption_top - 2

    max_lines = int(caption_height // CAPTION_LH)

    for i, line in enumerate(cap_lines[:max_lines]):
        cnv.drawString(x0, y_cursor - i * CAPTION_LH, line)

    if len(cap_lines) > max_lines:
        cnv.setFont(FONT_BOLD, CAPTION_FS)
        cnv.drawString(x0, y_cursor - max_lines * CAPTION_LH, "... (texto truncado)")


def chunker(seq, size):
    for pos in range(0, len(seq), size):
        yield pos, seq[pos:pos + size]


def prepare_pdf_start_index(df_src, last_old_id, chunk_size):
    ids_series = pd.to_numeric(df_src["id_mensaje"], errors="coerce").astype("Int64")

    mask_prev = ids_series.notna() & (ids_series <= last_old_id)
    n_prev = int(mask_prev.sum())

    rem = n_prev % chunk_size

    info = {
        "n_prev": n_prev,
        "rem": rem,
        "deleted_incomplete_pdf_name": None,
        "start_idx": None,
        "message": None
    }

    if n_prev == 0:
        start_idx = 0
        info["message"] = "No había registros previos. Generando desde el inicio."
    elif rem == 0:
        start_idx = n_prev
        info["message"] = f"No hay chunk incompleto previo. Generando desde índice {start_idx}."
    else:
        old_start_idx = n_prev - rem
        old_start_id = int(df_src.iloc[old_start_idx]["id_mensaje"])
        old_end_id = int(df_src.iloc[n_prev - 1]["id_mensaje"])
        old_pdf_name = f"{old_start_id}-{old_end_id}.pdf"

        start_idx = old_start_idx

        info["deleted_incomplete_pdf_name"] = old_pdf_name
        info["message"] = (
            f"Chunk incompleto detectado. Se regenerará desde idx {start_idx}, "
            f"id inicial {old_start_id}."
        )

    info["start_idx"] = start_idx

    return info


def download_images_for_rows(drive_service, rows, local_images_dir):
    logs = []

    for row in rows:
        msg_id = int(row["id_mensaje"])
        drive_file_id = row.get("drive_file_id")

        if not drive_file_id or pd.isna(drive_file_id):
            logs.append(f"[WARN] msg_id={msg_id} no tiene drive_file_id.")
            continue

        local_path = os.path.join(local_images_dir, f"{msg_id}.jpg")

        if os.path.exists(local_path):
            continue

        try:
            download_drive_file(
                drive_service=drive_service,
                file_id=str(drive_file_id),
                local_file_path=local_path
            )
        except Exception as e:
            logs.append(f"[ERROR] No se pudo descargar imagen msg_id={msg_id}: {e}")

    return logs


def create_pdf_for_chunk(chunk, output_pdf_path, local_images_dir):
    cnv = canvas.Canvas(output_pdf_path, pagesize=A4)

    start_id = int(chunk[0]["id_mensaje"])
    end_id = int(chunk[-1]["id_mensaje"])

    cnv.setAuthor("Alfred Bot")
    cnv.setTitle(f"Reporte Screenshots {start_id}-{end_id}")

    try:
        cnv.setPageCompression(1)
    except Exception:
        pass

    x = MARGIN_LR
    top_half_top = PAGE_H - MARGIN_TB
    bottom_half_top = MARGIN_TB + HALF_H + GUTTER

    pair = []

    for r in chunk:
        pair.append(r)

        if len(pair) == 2:
            draw_half(cnv, pair[0], x, top_half_top, CONTENT_W, local_images_dir)
            draw_half(cnv, pair[1], x, bottom_half_top, CONTENT_W, local_images_dir)
            cnv.showPage()
            pair = []

    if len(pair) == 1:
        draw_half(cnv, pair[0], x, top_half_top, CONTENT_W, local_images_dir)
        cnv.showPage()

    cnv.save()


def generate_pdfs_and_upload_to_drive(
    df_messages,
    last_old_id,
    chunk_size,
    drive_service,
    pdf_reports_folder_id
):
    logs = []
    uploaded_pdfs = []

    df_src = df_messages.copy()

    if df_src.empty:
        return [], ["[INFO] La DB está vacía. No hay PDFs por generar."]

    df_src["id_mensaje"] = pd.to_numeric(df_src["id_mensaje"], errors="coerce")
    df_src = (
        df_src.dropna(subset=["id_mensaje"])
              .drop_duplicates(subset=["id_mensaje"])
              .sort_values("id_mensaje")
              .reset_index(drop=True)
    )

    df_src["id_mensaje"] = df_src["id_mensaje"].astype(int)

    start_info = prepare_pdf_start_index(
        df_src=df_src,
        last_old_id=last_old_id,
        chunk_size=chunk_size
    )

    logs.append(f"[INFO] {start_info['message']}")
    logs.append(f"[INFO] Registros previos hasta LAST_OLD_ID: {start_info['n_prev']}")
    logs.append(f"[INFO] Residuo chunk previo: {start_info['rem']}")

    if start_info["deleted_incomplete_pdf_name"]:
        deleted = delete_drive_file_if_exists(
            drive_service=drive_service,
            parent_folder_id=pdf_reports_folder_id,
            file_name=start_info["deleted_incomplete_pdf_name"]
        )

        if deleted:
            logs.append(f"[INFO] PDF incompleto eliminado de Drive: {start_info['deleted_incomplete_pdf_name']}")
        else:
            logs.append(f"[WARN] No se encontró PDF incompleto en Drive: {start_info['deleted_incomplete_pdf_name']}")

    start_idx = start_info["start_idx"]

    rows_all = df_src.to_dict(orient="records")
    total = len(rows_all)

    if start_idx >= total:
        return uploaded_pdfs, logs + ["[INFO] No hay nuevas imágenes para generar PDF."]

    rows_tail = rows_all[start_idx:]
    total_tail = len(rows_tail)
    num_pdfs = math.ceil(total_tail / chunk_size)

    logs.append(f"[INFO] Imágenes a procesar desde idx {start_idx}: {total_tail}")
    logs.append(f"[INFO] PDFs a generar: {num_pdfs}")

    with tempfile.TemporaryDirectory() as tmpdir:
        local_images_dir = os.path.join(tmpdir, "images")
        local_pdfs_dir = os.path.join(tmpdir, "pdfs")

        os.makedirs(local_images_dir, exist_ok=True)
        os.makedirs(local_pdfs_dir, exist_ok=True)

        img_logs = download_images_for_rows(
            drive_service=drive_service,
            rows=rows_tail,
            local_images_dir=local_images_dir
        )

        logs.extend(img_logs)

        for _, chunk in chunker(rows_tail, chunk_size):
            start_id = int(chunk[0]["id_mensaje"])
            end_id = int(chunk[-1]["id_mensaje"])

            pdf_name = f"{start_id}-{end_id}.pdf"
            local_pdf_path = os.path.join(local_pdfs_dir, pdf_name)

            create_pdf_for_chunk(
                chunk=chunk,
                output_pdf_path=local_pdf_path,
                local_images_dir=local_images_dir
            )

            delete_drive_file_if_exists(
                drive_service=drive_service,
                parent_folder_id=pdf_reports_folder_id,
                file_name=pdf_name
            )

            uploaded_pdf = upload_file_to_drive(
                drive_service=drive_service,
                local_file_path=local_pdf_path,
                parent_folder_id=pdf_reports_folder_id,
                drive_file_name=pdf_name,
                mimetype="application/pdf"
            )

            uploaded_pdfs.append(uploaded_pdf)

            logs.append(f"[OK] PDF generado y subido: {pdf_name}")

    return uploaded_pdfs, logs


# =========================================================
# SESSION STATE
# =========================================================

if "last_extraction_logs" not in st.session_state:
    st.session_state["last_extraction_logs"] = None

if "last_extraction_count" not in st.session_state:
    st.session_state["last_extraction_count"] = None

if "show_extraction_success" not in st.session_state:
    st.session_state["show_extraction_success"] = False


# =========================================================
# STREAMLIT APP
# =========================================================

st.title("Telegram Scraper")
st.caption("Extracción de imágenes de Telegram, DB en Google Sheets y PDFs en Drive.")

st.divider()


try:
    gc, sh, drive_service = get_google_services()

    screenshots_folder_id = get_child_folder_id(
        drive_service,
        DRIVE_ROOT_FOLDER_ID,
        SCREENSHOTS_FOLDER_NAME
    )

    pdf_reports_folder_id = get_child_folder_id(
        drive_service,
        DRIVE_ROOT_FOLDER_ID,
        PDF_REPORTS_FOLDER_NAME
    )

    st.success("Conexión a Google exitosa.")

    df_messages = get_messages_df(sh)

    # =====================================================
    # SIDEBAR
    # =====================================================

    st.sidebar.header("Configuración Telegram")

    default_api_id = int(get_secret_value("TELEGRAM_API_ID", 21360469))
    default_api_hash = get_secret_value("TELEGRAM_API_HASH", "")

    api_id = st.sidebar.number_input(
        "API ID",
        value=default_api_id,
        step=1
    )

    if default_api_hash:
        api_hash = default_api_hash
        st.sidebar.success("API Hash cargado desde secrets.")
    else:
        api_hash = st.sidebar.text_input(
            "API Hash",
            type="password",
            help="Pega aquí tu API Hash de Telegram. No se muestra en pantalla."
        )

    chat_id = st.sidebar.number_input(
        "Chat ID",
        value=-1002642749020,
        step=1
    )

    st.sidebar.header("Extracción")

    if df_messages.empty:
        suggested_last_id = 0
    else:
        suggested_last_id = int(
            pd.to_numeric(df_messages["id_mensaje"], errors="coerce")
            .dropna()
            .max()
        )

    last_processed_id = st.sidebar.number_input(
        "last_processed_id",
        value=suggested_last_id,
        step=1,
        help="Se extraerán mensajes con ID mayor a este valor."
    )

    max_to_process = st.sidebar.number_input(
        "Máximo de imágenes a procesar",
        value=5,
        min_value=1,
        max_value=1000,
        step=1
    )

    st.sidebar.header("PDFs")

    last_old_id_pdf = st.sidebar.number_input(
        "LAST_OLD_ID para PDFs",
        value=suggested_last_id,
        step=1,
        help="Se generarán PDFs con las imágenes nuevas desde este ID, respetando chunks de 200."
    )

    chunk_size_pdf = st.sidebar.number_input(
        "CHUNK_SIZE",
        value=CHUNK_SIZE,
        min_value=1,
        max_value=1000,
        step=1
    )

    # =====================================================
    # ESTADO GENERAL
    # =====================================================

    col1, col2 = st.columns(2)

    with col1:
        st.subheader("Google Sheets")

        st.write("Archivo conectado:")
        st.code(sh.title)

        worksheets = [ws.title for ws in sh.worksheets()]
        st.write("Hojas disponibles:")
        st.write(worksheets)

        st.metric("Total registros en DB", len(df_messages))

        if not df_messages.empty:
            st.write("Últimos registros:")
            st.dataframe(df_messages.tail(20), use_container_width=True)
        else:
            st.info("La hoja messages está vacía.")

    with col2:
        st.subheader("Google Drive")

        folder = drive_service.files().get(
            fileId=DRIVE_ROOT_FOLDER_ID,
            fields="id, name, mimeType"
        ).execute()

        st.write("Carpeta raíz conectada:")
        st.code(folder["name"])

        items = list_drive_folder_items(drive_service, DRIVE_ROOT_FOLDER_ID)
        df_items = pd.DataFrame(items)

        st.write("Elementos dentro de la carpeta:")
        st.dataframe(df_items, use_container_width=True)

        st.write("Carpeta de imágenes:")
        st.code(f"{SCREENSHOTS_FOLDER_NAME} | {screenshots_folder_id}")

        st.write("Carpeta de PDFs:")
        st.code(f"{PDF_REPORTS_FOLDER_NAME} | {pdf_reports_folder_id}")

    st.divider()

    # =====================================================
    # EXTRACCIÓN TELEGRAM
    # =====================================================

    st.subheader("1. Extraer imágenes de Telegram")

    st.write(
        "Este proceso descargará fotos nuevas desde Telegram, "
        "las subirá a Drive y guardará la metadata en Google Sheets."
    )

    if st.button("Extraer imágenes nuevas", type="primary"):
        if not api_hash:
            st.error("Primero configura TELEGRAM_API_HASH en secrets o pégalo manualmente.")
        else:
            with st.spinner("Extrayendo imágenes de Telegram y subiendo a Drive..."):
                ids_existentes = set()

                if not df_messages.empty and "id_mensaje" in df_messages.columns:
                    ids_existentes = set(
                        pd.to_numeric(df_messages["id_mensaje"], errors="coerce")
                        .dropna()
                        .astype(int)
                        .tolist()
                    )

                registros_nuevos, logs = asyncio.run(
                    extraer_telegram_a_drive(
                        api_id=int(api_id),
                        api_hash=api_hash,
                        chat_id=int(chat_id),
                        drive_service=drive_service,
                        screenshots_folder_id=screenshots_folder_id,
                        ids_existentes=ids_existentes,
                        min_id=int(last_processed_id) if last_processed_id > 0 else None,
                        max_to_process=int(max_to_process)
                    )
                )

                if registros_nuevos:
                    append_messages_to_sheet(sh, registros_nuevos)

                st.session_state["last_extraction_count"] = len(registros_nuevos)
                st.session_state["last_extraction_logs"] = logs
                st.session_state["show_extraction_success"] = True

                st.rerun()

    if st.session_state["show_extraction_success"]:
        count = st.session_state["last_extraction_count"]
        logs = st.session_state["last_extraction_logs"]

        if count and count > 0:
            st.success(
                f"{count} registros nuevos guardados en Google Sheets. "
                "La DB ya fue recargada automáticamente."
            )
        else:
            st.info(
                "No se encontraron registros nuevos para guardar. "
                "La DB ya fue recargada automáticamente."
            )

        if logs:
            st.write("Logs:")
            st.code("\n".join(logs))

    st.divider()

    # =====================================================
    # GENERACIÓN DE PDFs
    # =====================================================

    st.subheader("2. Generar PDFs")

    st.write(
        "Este proceso toma todas las imágenes nuevas desde LAST_OLD_ID, "
        "genera PDFs por chunks de 200 y los sube a Drive/pdf_reports."
    )

    col_pdf_1, col_pdf_2, col_pdf_3 = st.columns(3)

    with col_pdf_1:
        st.metric("LAST_OLD_ID seleccionado", int(last_old_id_pdf))

    with col_pdf_2:
        st.metric("CHUNK_SIZE", int(chunk_size_pdf))

    with col_pdf_3:
        if df_messages.empty:
            new_rows_count = 0
        else:
            tmp_ids = pd.to_numeric(df_messages["id_mensaje"], errors="coerce")
            new_rows_count = int((tmp_ids > int(last_old_id_pdf)).sum())

        st.metric("Imágenes nuevas directas", new_rows_count)

    if st.button("Generar PDFs y subir a Drive", type="primary"):
        if df_messages.empty:
            st.error("La DB está vacía. Primero extrae imágenes.")
        else:
            with st.spinner("Generando PDFs y subiendo a Drive..."):
                uploaded_pdfs, pdf_logs = generate_pdfs_and_upload_to_drive(
                    df_messages=df_messages,
                    last_old_id=int(last_old_id_pdf),
                    chunk_size=int(chunk_size_pdf),
                    drive_service=drive_service,
                    pdf_reports_folder_id=pdf_reports_folder_id
                )

            if uploaded_pdfs:
                st.success(f"{len(uploaded_pdfs)} PDFs generados y subidos a Drive.")

                df_pdfs = pd.DataFrame(uploaded_pdfs)

                st.write("PDFs generados:")
                st.dataframe(df_pdfs, use_container_width=True)

                st.write("Links:")
                for pdf in uploaded_pdfs:
                    st.markdown(f"- [{pdf['name']}]({pdf.get('webViewLink', '')})")
            else:
                st.info("No se generaron PDFs nuevos.")

            st.write("Logs PDFs:")
            st.code("\n".join(pdf_logs))

except Exception as e:
    st.error("Ocurrió un error.")
    st.exception(e)