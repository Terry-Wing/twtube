import os
import re
import asyncio
import logging

import bg_tasks

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

    # 平台显示名（与 web 端 getPlatformName 保持一致）
    PLATFORM_DISPLAY = {
        'douyin': '抖音',
        'tiktok': 'TikTok',
        'instagram': 'Instagram',
        'youtube': 'YouTube',
        'bilibili': 'B站',
        'default': '网页',
    }

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

        # 预先进行链接清洗，确保提示与平台识别完全准确
        if 'douyin.com' in extracted_url and 'modal_id=' in extracted_url:
            m = re.search(r'modal_id=(\d+)', extracted_url)
            if m:
                extracted_url = f"https://www.douyin.com/video/{m.group(1)}"

        target_folder = self._detect_platform_folder(extracted_url)
        await update.message.reply_text(
            f"📥 正在解析并加入队列...\n"
            f"🌐 平台识别: <b>{self.PLATFORM_DISPLAY.get(target_folder, '网页')}</b>\n"
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
                folder='',
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

        async def _run_bot():
            # 外层循环：轮询一旦崩溃（隔夜网络被 NAT/ISP 掐断、Telegram API
            # 报错等）就自动重建并恢复，而不是静默死掉直到重启容器。
            while True:
                app = ApplicationBuilder().token(self.token).build()
                app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_message))
                self.bot_app = app  # 保持引用，让 send_notification 用到的始终是当前实例
                try:
                    async with app:
                        await app.start()
                        await app.updater.start_polling()
                        log.info("Telegram Bot polling started successfully.")
                        while True:
                            await asyncio.sleep(3600)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.exception("Telegram Bot polling crashed; restarting in 10s: %s", e)
                    await asyncio.sleep(10)

        # 用 bg_tasks.create_task 保持强引用并记录意外失败（裸 asyncio.create_task
        # 只被事件循环弱引用，可能在运行中被 GC 回收导致轮询静默消失）。
        self._bot_task = bg_tasks.create_task(_run_bot(), name="telegram_bot")