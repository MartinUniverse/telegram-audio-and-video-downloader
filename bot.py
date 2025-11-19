import os
import re
import tempfile
import shutil

from dotenv import load_dotenv
import telebot
from telebot import types

import yt_dlp
import requests

# ---------- НАСТРОЙКИ ----------

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise RuntimeError("В .env нет BOT_TOKEN")

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

    try:
        data = resp.json()
    except Exception:
        raise RuntimeError(f"Gofile вернул не JSON: {resp.text[:500]}")

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
    wait = bot.send_message(message.chat.id, "🎧 Качаю аудио...")

    file_path = None
    tmp_dir = None

    try:
        file_path, tmp_dir = download_from_yt(url, mode="audio")

        size_mb = os.path.getsize(file_path) / (1024 * 1024)
        if size_mb > 49:
            bot.edit_message_text(
                chat_id=wait.chat.id,
                message_id=wait.message_id,
                text=f"⚠️ Аудио получилось {size_mb:.1f} МБ — больше лимита Телеграма (50 МБ).\n"
                     f"Попробуй более короткое видео.",
            )
            return

        with open(file_path, "rb") as f:
            # Не удаляем сообщение до успешной отправки!
            bot.send_audio(
                message.chat.id,
                f,
                caption=f"Аудио с YouTube ({size_mb:.1f} МБ)",
                timeout=60000,  # даём до 1000 минут на заливку
            )

        # Если всё прошло хорошо — теперь можно удалить "Качаю аудио..."
        bot.delete_message(wait.chat.id, wait.message_id)

    except Exception as e:
        # Если сообщение уже удалено или не найдено — просто пишем новое
        try:
            bot.edit_message_text(
                chat_id=wait.chat.id,
                message_id=wait.message_id,
                text=f"❌ Ошибка при скачивании аудио:\n{e}",
            )
        except Exception:
            bot.send_message(
                message.chat.id,
                f"❌ Ошибка при скачивании аудио:\n{e}",
            )

    finally:
        if tmp_dir and os.path.exists(tmp_dir):
            shutil.rmtree(tmp_dir)



# ---------- ЛОГИКА ВИДЕО ----------

def process_video(message: types.Message, url: str):
    wait = bot.send_message(message.chat.id, "📹 Качаю видео, подожди...")

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
        bot.edit_message_text(
            chat_id=wait.chat.id,
            message_id=wait.message_id,
            text=f"❌ Ошибка при обработке видео:\n{e}",
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
        
# --------------------------- RENDER KEEP-ALIVE SERVER ---------------------------
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
import os

class KeepAliveHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running")

def run_keepalive_server():
    port = int(os.environ.get("PORT", 5000))  # Render автоматически задаёт PORT
    server = HTTPServer(("", port), KeepAliveHandler)
    server.serve_forever()

# Запуск мини-сервера в отдельном потоке
threading.Thread(target=run_keepalive_server, daemon=True).start()

 # ---------- основной цикл polling с авто-перезапуском ----------
    while True:
        try:
            print("Starting polling...")
            bot.infinity_polling(skip_pending=True, timeout=60, long_polling_timeout=60)
        except apihelper.ApiTelegramException as e:
            # Игнорируем конфликт 409 и просто пробуем ещё раз
            if e.error_code == 409:
                print("Got 409 conflict from Telegram, retrying in 10s...")
                time.sleep(10)
                continue
            else:
                print(f"Telegram API error: {e}, retrying in 10s...")
                time.sleep(10)
        except Exception as e:
            print(f"Unexpected error: {e}, retrying in 10s...")
            time.sleep(10)

# ---------- ЗАПУСК ----------

if __name__ == "__main__":
    print("Бот запущен. Нажми Ctrl+C для остановки.")
    bot.infinity_polling(skip_pending=True)




