import os
import logging
import tempfile
import subprocess
import httpx
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
GROQ_API_KEY = os.environ["GROQ_API_KEY"]

SUPPORTED_EXTENSIONS = [".mp3", ".mp4", ".wav", ".m4a", ".ogg", ".webm", ".mpeg", ".mpga"]

def convert_to_audio(file_path: str) -> str:
    ext = os.path.splitext(file_path)[1].lower()
    audio_path = file_path + ".mp3"
    subprocess.run([
        "ffmpeg", "-i", file_path,
        "-vn", "-acodec", "mp3",
        "-ab", "64k", "-ar", "16000",
        "-y", audio_path
    ], capture_output=True)
    return audio_path

def split_audio(file_path: str, chunk_minutes: int = 10) -> list:
    chunk_seconds = chunk_minutes * 60
    duration_result = subprocess.run([
        "ffprobe", "-v", "quiet",
        "-show_entries", "format=duration",
        "-of", "csv=p=0", file_path
    ], capture_output=True, text=True)
    
    try:
        duration = float(duration_result.stdout.strip())
    except:
        return [file_path]
    
    if duration <= chunk_seconds:
        return [file_path]
    
    chunks = []
    start = 0
    index = 0
    while start < duration:
        chunk_path = file_path + f"_chunk{index}.mp3"
        subprocess.run([
            "ffmpeg", "-i", file_path,
            "-ss", str(start),
            "-t", str(chunk_seconds),
            "-acodec", "mp3", "-ab", "64k", "-ar", "16000",
            "-y", chunk_path
        ], capture_output=True)
        chunks.append((chunk_path, start))
        start += chunk_seconds
        index += 1
    
    return chunks

async def transcribe_chunk(file_path: str, offset_seconds: float = 0) -> str:
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
        return data.get("text", "")
    
    lines = []
    for seg in segments:
        start = int(seg["start"] + offset_seconds)
        minutes = start // 60
        seconds = start % 60
        timestamp = f"[{minutes:02d}:{seconds:02d}]"
        lines.append(f"{timestamp} {seg['text'].strip()}")
    
    return "\n".join(lines)

async def process_file(file_path: str, message) -> str:
    ext = os.path.splitext(file_path)[1].lower()
    
    # Конвертируем видео в аудио
    if ext in [".mp4", ".webm", ".mpeg", ".mov"]:
        await message.reply_text("🎬 Извлекаю аудио из видео...")
        audio_path = convert_to_audio(file_path)
        os.unlink(file_path)
    else:
        audio_path = file_path
    
    # Нарезаем на куски
    chunks = split_audio(audio_path)
    
    if isinstance(chunks[0], tuple):
        # Несколько кусков
        total = len(chunks)
        await message.reply_text(f"✂️ Файл большой, нарезал на {total} частей по 10 минут. Транскрибирую...")
        
        all_text = []
        for i, (chunk_path, offset) in enumerate(chunks):
            await message.reply_text(f"⏳ Обрабатываю часть {i+1}/{total}...")
            text = await transcribe_chunk(chunk_path, offset)
            all_text.append(text)
            os.unlink(chunk_path)
        
        os.unlink(audio_path)
        return "\n".join(all_text)
    else:
        # Один файл
        text = await transcribe_chunk(audio_path)
        os.unlink(audio_path)
        return text

async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    await message.reply_text("⏳ Получил файл, начинаю обработку...")
    
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
        result = await process_file(tmp_path, message)
        await send_long_message(message, result)
    
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
            "• Ссылку на Google Drive"
        )
        return
    
    await message.reply_text("⏳ Скачиваю файл...")
    
    try:
        if "drive.google.com" in text:
            if "/file/d/" in text:
                file_id = text.split("/file/d/")[1].split("/")[0]
                text = f"https://drive.google.com/uc?export=download&id={file_id}"
        
        async with httpx.AsyncClient(timeout=600, follow_redirects=True) as client:
            response = await client.get(text)
        
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
            tmp_path = tmp.name
        
        await message.reply_text("✅ Файл скачан, обрабатываю...")
        result = await process_file(tmp_path, message)
        await send_long_message(message, result)
    
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

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, format, *args):
        pass

def run_health_server():
    server = HTTPServer(("0.0.0.0", 8080), HealthHandler)
    server.serve_forever()

def main():
    threading.Thread(target=run_health_server, daemon=True).start()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.AUDIO | filters.VOICE | filters.VIDEO | filters.Document.ALL, handle_audio))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.run_polling()

if __name__ == "__main__":
    main()
