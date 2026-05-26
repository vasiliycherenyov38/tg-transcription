import os
import logging
import asyncio
import tempfile
import httpx
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
GROQ_API_KEY = os.environ["GROQ_API_KEY"]

SUPPORTED_EXTENSIONS = [".mp3", ".mp4", ".wav", ".m4a", ".ogg", ".webm", ".mpeg", ".mpga"]

async def transcribe_file(file_path: str) -> str:
    url = "https://api.groq.com/openai/v1/audio/transcriptions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
    
    with open(file_path, "rb") as f:
        files = {
            "file": (os.path.basename(file_path), f, "audio/mpeg"),
            "model": (None, "whisper-large-v3"),
            "language": (None, "ru"),
            "response_format": (None, "verbose_json"),
        }
        async with httpx.AsyncClient(timeout=300) as client:
            response = await client.post(url, headers=headers, files=files)
    
    if response.status_code != 200:
        raise Exception(f"Groq error: {response.text}")
    
    data = response.json()
    segments = data.get("segments", [])
    
    if not segments:
        return data.get("text", "Текст не распознан")
    
    lines = []
    for seg in segments:
        start = int(seg["start"])
        minutes = start // 60
        seconds = start % 60
        timestamp = f"[{minutes:02d}:{seconds:02d}]"
        lines.append(f"{timestamp} {seg['text'].strip()}")
    
    return "\n".join(lines)

async def download_from_url(url: str) -> str:
    async with httpx.AsyncClient(timeout=300, follow_redirects=True) as client:
        response = await client.get(url)
    
    if response.status_code != 200:
        raise Exception(f"Не удалось скачать файл: {response.status_code}")
    
    content_type = response.headers.get("content-type", "")
    ext = ".mp3"
    if "mp4" in content_type or "video" in content_type:
        ext = ".mp4"
    elif "ogg" in content_type:
        ext = ".ogg"
    elif "wav" in content_type:
        ext = ".wav"
    
    with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
        tmp.write(response.content)
        return tmp.name

async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    await message.reply_text("⏳ Получил файл, начинаю транскрибацию...")
    
    try:
        audio = message.audio or message.voice or message.video or message.document
        
        if message.document:
            fname = message.document.file_name or ""
            if not any(fname.lower().endswith(ext) for ext in SUPPORTED_EXTENSIONS):
                await message.reply_text("Формат не поддерживается. Отправь mp3, mp4, wav, m4a, ogg.")
                return
        
        tg_file = await audio.get_file()
        
        with tempfile.NamedTemporaryFile(delete=False, suffix=".ogg") as tmp:
            tmp_path = tmp.name
        
        await tg_file.download_to_drive(tmp_path)
        
        text = await transcribe_file(tmp_path)
        os.unlink(tmp_path)
        
        await send_long_message(message, text)
    
    except Exception as e:
        logger.error(f"Error: {e}")
        await message.reply_text(f"Ошибка: {str(e)}")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    text = message.text.strip()
    
    if not text.startswith("http"):
        await message.reply_text(
            "Привет! Отправь мне:\n"
            "• Аудио или голосовое сообщение\n"
            "• Видео или файл\n"
            "• Ссылку на Google Drive или любой публичный файл"
        )
        return
    
    await message.reply_text("⏳ Скачиваю файл по ссылке...")
    
    try:
        # Конвертируем ссылку Google Drive в прямую
        if "drive.google.com" in text:
            if "/file/d/" in text:
                file_id = text.split("/file/d/")[1].split("/")[0]
                text = f"https://drive.google.com/uc?export=download&id={file_id}"
        
        tmp_path = await download_from_url(text)
        await message.reply_text("✅ Файл скачан, транскрибирую...")
        
        text_result = await transcribe_file(tmp_path)
        os.unlink(tmp_path)
        
        await send_long_message(message, text_result)
    
    except Exception as e:
        logger.error(f"Error: {e}")
        await message.reply_text(f"Ошибка: {str(e)}")

async def send_long_message(message, text: str):
    if len(text) <= 4000:
        await message.reply_text(text)
    else:
        parts = []
        while len(text) > 4000:
            split_at = text.rfind("\n", 0, 4000)
            if split_at == -1:
                split_at = 4000
            parts.append(text[:split_at])
            text = text[split_at:]
        parts.append(text)
        
        for i, part in enumerate(parts):
            await message.reply_text(f"Часть {i+1}/{len(parts)}:\n{part}")

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.AUDIO | filters.VOICE | filters.VIDEO | filters.Document.ALL, handle_audio))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.run_polling()

if __name__ == "__main__":
    main()
