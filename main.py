import asyncio
import logging
import os
from aiohttp import web
from aiogram import Bot, Dispatcher
import bot as bot_module
import storage  # ⚠️ SHU QATOR QO'SHILDI
from config import BOT_TOKEN

# Render.com uchun soxta veb-sahifa
async def handle(request):
    return web.Response(text="Bot va Testlar muvaffaqiyatli ishlamoqda! 🚀")

async def main():
    # ⚠️ ENG MUHIM QISM: Testlarni papkalardan o'qib, botning xotirasiga yuklaymiz
    bot_module.memory_db = storage.init_storage()

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(bot_module.router)

    # Veb-serverni fonda ishga tushirish (Render portga ulanishi uchun)
    app = web.Application()
    app.router.add_get('/', handle)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()

    logging.info("Bot ishga tushdi...")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Bot to'xtatildi.")
