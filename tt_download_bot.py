import asyncio
import logging
import os
import re
import uuid
import time
from dataclasses import dataclass
from typing import Optional, List, Dict

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    FSInputFile,
)
from dotenv import load_dotenv
import yt_dlp


# === Конфигурация ===
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "downloads")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN отсутствует в .env")

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("universal_media_bot")


# === Регулярные выражения для поддерживаемых платформ ===
URL_PATTERNS = {
    "tiktok": re.compile(r"(https?://(?:www\.)?(?:tiktok\.com|vm\.tiktok\.com)/[^\s]+)"),
    "youtube": re.compile(r"(https?://(?:www\.)?(?:youtube\.com|youtu\.be)/[^\s]+)"),
    "instagram": re.compile(r"(https?://(?:www\.)?instagram\.com/[^\s]+)"),
    "vk": re.compile(r"(https?://(?:www\.)?vk\.com/video[^\s]+)"),
    "pinterest": re.compile(r"(https?://(?:www\.)?pinterest\.[^\s]+)"),
}


# === Модель данных ===
@dataclass
class VideoInfo:
    url: str
    title: str
    formats: List[dict]


# === Временный кеш ссылок (id → url, с TTL) ===
URL_CACHE: Dict[str, dict] = {}
CACHE_TTL = 15 * 60  # 15 минут


async def cleanup_cache() -> None:
    """Периодически удаляет устаревшие записи из кеша."""
    while True:
        now = time.time()
        expired = [k for k, v in URL_CACHE.items() if now - v["time"] > CACHE_TTL]
        for k in expired:
            del URL_CACHE[k]
        await asyncio.sleep(300)  # каждые 5 минут


# === Вспомогательные функции ===
def detect_platform(url: str) -> str:
    for key in URL_PATTERNS.keys():
        if key in url:
            return key.capitalize()
    return "Unknown"


def extract_url(text: str) -> Optional[str]:
    for pattern in URL_PATTERNS.values():
        m = pattern.search(text)
        if m:
            return m.group(0)
    return None


async def get_video_info(url: str) -> Optional[VideoInfo]:
    """Извлекает метаданные видео без загрузки."""
    ydl_opts = {"quiet": True, "skip_download": True}
    loop = asyncio.get_event_loop()
    try:
        def _extract():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                return VideoInfo(
                    url=url,
                    title=info.get("title", "Без названия"),
                    formats=info.get("formats", []),
                )
        return await loop.run_in_executor(None, _extract)
    except Exception as e:
        logger.error(f"Не удалось получить информацию о видео: {e}")
        return None


async def download_media(url: str, fmt_id: Optional[str], media_type: str) -> Optional[str]:
    """Скачивает выбранный формат видео/аудио/превью."""
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "outtmpl": os.path.join(DOWNLOAD_DIR, "%(title)s.%(ext)s"),
        "noplaylist": True,
        "format": fmt_id or "best",
    }

    if media_type == "audio":
        ydl_opts.update({
            "format": "bestaudio/best",
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }]
        })
    elif media_type == "thumbnail":
        ydl_opts.update({"skip_download": True, "writethumbnail": True})

    loop = asyncio.get_event_loop()
    try:
        def _download():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                if media_type == "thumbnail":
                    return ydl.prepare_filename(info).rsplit(".", 1)[0] + ".jpg"
                return ydl.prepare_filename(info)
        return await loop.run_in_executor(None, _download)
    except Exception as e:
        logger.error(f"Ошибка загрузки: {e}")
        return None


# === Inline-клавиатуры ===
def build_type_keyboard(url: str) -> InlineKeyboardMarkup:
    uid = str(uuid.uuid4())[:8]
    URL_CACHE[uid] = {"url": url, "time": time.time()}
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🎞 Видео", callback_data=f"type|video|{uid}"),
                InlineKeyboardButton(text="🎧 Аудио", callback_data=f"type|audio|{uid}"),
                InlineKeyboardButton(text="🖼 Превью", callback_data=f"type|thumbnail|{uid}"),
            ]
        ]
    )


def build_quality_keyboard(formats: List[dict], media_type: str, uid: str) -> InlineKeyboardMarkup:
    quality_buttons = []
    for f in formats:
        if f.get("vcodec") != "none" and f.get("ext") == "mp4" and f.get("height"):
            q = f.get("format_id")
            label = f"{f.get('height')}p"
            quality_buttons.append(
                InlineKeyboardButton(text=label, callback_data=f"dl|{media_type}|{q}|{uid}")
            )
    if not quality_buttons:
        quality_buttons.append(
            InlineKeyboardButton(text="Лучшее доступное", callback_data=f"dl|{media_type}|best|{uid}")
        )
    rows = [quality_buttons[i:i + 3] for i in range(0, len(quality_buttons), 3)]
    return InlineKeyboardMarkup(inline_keyboard=rows)


# === Обработчики ===
async def cmd_start(msg: Message):
    await msg.answer(
        "🎬 Я загружаю видео, аудио и превью из TikTok, YouTube Shorts, Instagram Reels, VK Видео и Pinterest.\n"
        "Отправь ссылку — выбери формат и качество.",
        parse_mode=ParseMode.HTML,
    )


async def handle_link(msg: Message):
    url = extract_url(msg.text or "")
    if not url:
        await msg.answer("❗ Не удалось найти ссылку.")
        return
    platform = detect_platform(url)
    await msg.answer(
        f"📦 Найдено: <b>{platform}</b>\nВыбери тип загрузки:",
        parse_mode=ParseMode.HTML,
        reply_markup=build_type_keyboard(url),
    )


async def cb_select_type(call: CallbackQuery):
    _, media_type, uid = call.data.split("|", 2)
    entry = URL_CACHE.get(uid)
    if not entry:
        await call.message.edit_text("⚠️ Сессия устарела. Отправь ссылку заново.")
        return
    url = entry["url"]

    await call.message.edit_text("⏳ Получаю список форматов...")
    info = await get_video_info(url)
    if not info:
        await call.message.edit_text("🚫 Не удалось получить информацию о видео.")
        return

    kb = build_quality_keyboard(info.formats, media_type, uid)
    await call.message.edit_text(
        f"🎥 <b>{info.title}</b>\nВыбери качество:",
        parse_mode=ParseMode.HTML,
        reply_markup=kb,
    )


async def cb_download(call: CallbackQuery):
    _, media_type, fmt_id, uid = call.data.split("|", 3)
    entry = URL_CACHE.get(uid)
    if not entry:
        await call.message.edit_text("⚠️ Сессия устарела. Отправь ссылку заново.")
        return
    url = entry["url"]

    await call.message.edit_text("⬇️ Загружаю файл, подожди немного...")
    path = await download_media(url, fmt_id, media_type)
    if not path or not os.path.exists(path):
        await call.message.edit_text("🚫 Ошибка загрузки.")
        return

    try:
        file = FSInputFile(path)
        if media_type == "audio":
            await call.message.answer_audio(file)
        elif media_type == "thumbnail":
            await call.message.answer_photo(file)
        else:
            await call.message.answer_video(file)
        await call.message.delete()
    except Exception as e:
        logger.error(f"Ошибка отправки файла: {e}")
        await call.message.edit_text("⚠️ Ошибка при отправке файла.")
    finally:
        try:
            os.remove(path)
        except Exception:
            pass


# === Запуск бота ===
async def main():
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.message.register(cmd_start, CommandStart())
    dp.message.register(handle_link)
    dp.callback_query.register(cb_select_type, lambda c: c.data.startswith("type|"))
    dp.callback_query.register(cb_download, lambda c: c.data.startswith("dl|"))

    asyncio.create_task(cleanup_cache())  # фоновая очистка кеша
    logger.info("Бот запущен.")
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Остановлен пользователем.")
