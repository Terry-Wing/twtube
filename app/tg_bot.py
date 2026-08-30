import os
import re
import asyncio
import logging
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters

log = logging.getLogger('tg_bot')

# 正则提取文本中的任何 http/https 链接
URL_REGEX = re.compile(r'https?://[^\s<>"]+|www\.[^\s<>"]+')

class TelegramBotManager:
    def __init__(self, config, dqueue):
        self.config = config
        self.dqueue = dqueue
        self.token = os.environ.get('TG_BOT_TOKEN', '').strip()
        self.allowed_chat_id = os.environ.get('TG_CHAT_ID', '').strip()
        self.bot_app = None

    def _extract_url(self, text: str) -> str | None:
        if not text:
            return None
        match = URL_REGEX.search(text)
        return match.group(0) if match else None

    def _detect_platform_folder(self, url: str) -> str:
        url_lower = url.lower()
        if 'douyin.com' in url_lower or 'iesdouyin.com' in url_lower:
            return 'douyin'
        elif 'tiktok.com' in url_lower:
            return 'tiktok'
        elif 'instagram.com' in url_lower or 'instagr.am' in url_lower:
            return 'instagram'
        elif 'youtube.com' in url_lower or 'youtu.be' in url_lower:
            return 'youtube'
        elif 'bilibili.com' in url_lower or 'b23.tv' in url_lower:
            return 'bilibili'
        return 'default'

    async def _handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.message or not update.message.text:
            return

        user_id = str(update.effective_user.id)
        chat_id = str(update.effective_chat.id)

        # 权限校验：如果配置了 TG_CHAT_ID，则仅允许指定用户使用
        if self.allowed_chat_id and chat_id != self.allowed_chat_id and user_id != self.allowed_chat_id:
            log.warning(f"Unauthorized TG message from user_id: {user_id}, chat_id: {chat_id}")
            return

        raw_text = update.message.text.strip()
        extracted_url = self._extract_url(raw_text)

        if not extracted_url:
            await update.message.reply_text("❌ 未检测到有效的视频链接，请重新发送。")
            return

        target_folder = self._detect_platform_folder(extracted_url)
        await update.message.reply_text(
            f"📥 正在解析并加入队列...\n"
            f"🌐 平台识别: <b>{target_folder.upper()}</b>\n"
            f"📁 保存目录: <code>{target_folder}</code>\n"
            f"🔗 链接: {extracted_url}",
            parse_mode="HTML"
        )

        try:
            # 加入 MeTube 下载队列，默认下载最高画质 + 任意格式 (由后台封装为 MP4)
            status = await self.dqueue.add(
                url=extracted_url,
                download_type='video',
                codec='auto',
                format='any',
                quality='best',
                folder=target_folder,
                custom_name_prefix='',
                playlist_item_limit=0,
                auto_start=True,
                split_by_chapters=False,
                chapter_template=self.config.OUTPUT_TEMPLATE_CHAPTER,
                subtitle_language='zh-Hans',
                subtitle_mode='prefer_manual',
                ytdl_options_presets=[],
                ytdl_options_overrides={},
                clip_start=None,
                clip_end=None,
                sponsorblock=False
            )

            if status.get('status') == 'error':
                await update.message.reply_text(f"⚠️ 添加下载失败: {status.get('msg')}")
            else:
                log.info(f"Successfully added download via TG: {extracted_url} -> {target_folder}")
        except Exception as e:
            log.exception(f"Error handling TG download: {e}")
            await update.message.reply_text(f"❌ 系统处理异常: {str(e)}")

    async def send_notification(self, text: str):
        """用于下载完成后的回传通知"""
        if self.bot_app and self.allowed_chat_id:
            try:
                await self.bot_app.bot.send_message(
                    chat_id=self.allowed_chat_id,
                    text=text,
                    parse_mode="HTML"
                )
            except Exception as e:
                log.warning(f"Failed to send TG notification: {e}")

    def start(self):
        if not self.token:
            log.info("TG_BOT_TOKEN not configured. Telegram bot service disabled.")
            return

        log.info("Starting Telegram Bot service...")
        self.bot_app = ApplicationBuilder().token(self.token).build()
        self.bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_message))

        async def _run_bot():
            async with self.bot_app:
                await self.bot_app.start()
                await self.bot_app.updater.start_polling()
                log.info("Telegram Bot polling started successfully.")
                while True:
                    await asyncio.sleep(3600)

        asyncio.create_task(_run_bot())