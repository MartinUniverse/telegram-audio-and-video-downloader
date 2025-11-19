import os
import re
import tempfile
import shutil
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from dotenv import load_dotenv
import telebot
from telebot import types, apihelper

import yt_dlp
import requests

# ---------- НАСТРОЙКИ ----------

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise RuntimeError("В окружении нет BOT_TOKEN")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode=None)


# ---------- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ----------

def is_url(text: str) -> bool:
    return bool(re.match(r"https?://\S+", text))


def download_from_yt(url: str, mode: str = "audio"):
    """
    Качает с YouTube через yt_dlp.
    mode = "audio" или "video".
    Возвращает (путь_к_файлу, путь_к_временной_папке)
    """
    tmp_dir = tempfile.mkdtemp(prefix="yt_")
    outtmpl = os.path.join(tmp_dir, "%(title).80s.%(ext)s")

    if mode == "audio":
        ydl_opts = {
            "format": "bestaudio/best",
            "outtmpl": outtmpl,
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                }
            ],
            "quiet": True,
            "no_warnings": True,
        }
    else:
        ydl_opts = {
            "format": "bv*+ba/best",
            "outtmpl": outtmpl,
            "quiet": True,
            "no_warnings": True,
        }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        file_path = ydl.prepare_filename(info)

    if mode == "audio":
        base, _ = os.path.splitext(file_path)
        file_path = base + ".mp3"

    return file_path, tmp_dir


def upload_to_gofile(file_path: str) -> str:
    """
    Заливает файл на gofile.io и возвращает ссылку на страницу скачивания.
    """
    with open(file_path, "rb") as f:
        resp = requests.post(
            "https://upload.gofile.io/uploadfile",
            files={"file": f},
            timeout=300,
        )

    data = resp.json()
    status = data.get("status")
    if status not in ("ok", "success"):
        raise RuntimeError(f"Ошибка от Gofile: {data}")

    inner = data.get("data") or {}
    link = inner.get("downloadPage") or inner.get("directLink")

    if not link:
        raise RuntimeError(f"Не нашёл ссылку в ответе Gofile: {data}")

    return link


# ---------- ЛОГИКА АУДИО ----------

def process_audio(message: types.Message, url: str):
    chat_id = message.chat.id
    bot.send_message(chat_id, "🎧 Качаю аудио, подожди...")

    file_path = None
    tmp_dir = None

    try:
        file_path, tmp_dir = download_from_yt(url, mode="audio")

        size_mb = os.path.getsize(file_path) / (1024 * 1024)
        if size_mb > 49:
            bot.send_message(
                chat_id,
                f"⚠️ Аудио получилось {size_mb:.1f} МБ — больше лимита Телеграма для ботов (50 МБ).\n"
                f"Попробуй более короткое видео.",
            )
            return

        try:
            with open(file_path, "rb") as f:
                bot.send_audio(
                    chat_id,
                    f,
                    caption=f"Аудио с YouTube ({size_mb:.1f} МБ)",
                    timeout=600,
                )
            bot.send_message(chat_id, f"✅ Готово! ({size_mb:.1f} МБ)")
        except Exception as send_err:
            bot.send_message(
                chat_id,
                f"❌ Не удалось отправить файл в Телеграм (таймаут или сеть):\n{send_err}",
            )

    except Exception as e:
        bot.send_message(
            chat_id,
            f"❌ Ошибка при скачивании аудио:\n{e}",
        )

    finally:
        if tmp_dir and os.path.exists(tmp_dir):
            shutil.rmtree(tmp_dir)


# ---------- ЛОГИКА ВИДЕО ----------

def process_video(message: types.Message, url: str):
    chat_id = message.chat.id
    wait = bot.send_message(chat_id, "📹 Качаю видео, подожди...")

    file_path = None
    tmp_dir = None

    try:
        file_path, tmp_dir = download_from_yt(url, mode="video")
        size_mb = os.path.getsize(file_path) / (1024 * 1024)

        bot.edit_message_text(
            chat_id=wait.chat.id,
            message_id=wait.message_id,
            text=f"⬆️ Видео скачано ({size_mb:.1f} МБ). Загружаю на gofile.io...",
        )

        link = upload_to_gofile(file_path)

        bot.edit_message_text(
            chat_id=wait.chat.id,
            message_id=wait.message_id,
            text=(
                "✅ Готово!\n\n"
                f"Размер: {size_mb:.1f} МБ\n"
                f"Ссылка на скачивание:\n{link}"
            ),
        )

    except Exception as e:
        try:
            bot.edit_message_text(
                chat_id=wait.chat.id,
                message_id=wait.message_id,
                text=f"❌ Ошибка при обработке видео:\n{e}",
            )
        except Exception:
            bot.send_message(
                chat_id,
                f"❌ Ошибка при обработке видео:\n{e}",
            )

    finally:
        if tmp_dir and os.path.exists(tmp_dir):
            shutil.rmtree(tmp_dir)


# ---------- ХЕНДЛЕРЫ ----------

@bot.message_handler(commands=["start"])
def cmd_start(message: types.Message):
    bot.send_message(
        message.chat.id,
        "Привет! Я качаю с YouTube.\n\n"
        "🎧 /audio <url> — вытащить аудио (до 50 МБ, отправлю прямо в чат)\n"
        "📹 /video <url> — скачать видео и залить на gofile.io (отдам ссылку)\n\n"
        "Можно просто кинуть ссылку — по умолчанию сделаю аудио.",
    )


@bot.message_handler(commands=["audio"])
def cmd_audio(message: types.Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        bot.send_message(
            message.chat.id,
            "Используй: /audio <ссылка на YouTube>",
        )
        return

    url = parts[1].strip()
    process_audio(message, url)


@bot.message_handler(commands=["video"])
def cmd_video(message: types.Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        bot.send_message(
            message.chat.id,
            "Используй: /video <ссылка на YouTube>",
        )
        return

    url = parts[1].strip()
    process_video(message, url)


@bot.message_handler(content_types=["text"])
def handle_text(message: types.Message):
    text = message.text.strip()
    if is_url(text):
        process_audio(message, text)  # по умолчанию аудио
    else:
        bot.send_message(
            message.chat.id,
            "Пришли ссылку на видео или используй /audio /video",
        )


# ---------- HTTP-СЕРВЕР ДЛЯ RENDER И ЦИКЛ POLLING ----------

class KeepAliveHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is alive!")


def run_keepalive_server():
    port = int(os.environ.get("PORT", 5000))
    server = HTTPServer(("", port), KeepAliveHandler)
    server.serve_forever()


if __name__ == "__main__":
    print("Bot starting...")

    # мини-сервер, чтобы Render видел открытый порт
    threading.Thread(target=run_keepalive_server, daemon=True).start()

    # основной цикл polling с авто-перезапуском
    while True:
        try:
            print("Starting polling...")
            bot.infinity_polling(skip_pending=True, timeout=60, long_polling_timeout=60)
        except apihelper.ApiTelegramException as e:
            if e.error_code == 409:
                print("409 conflict, retrying in 10s...")
                time.sleep(10)
                continue
            else:
                print(f"Telegram API error: {e}, retrying in 10s...")
                time.sleep(10)
        except Exception as e:
            print(f"Unexpected error: {e}, retrying in 10s...")
            time.sleep(10)
