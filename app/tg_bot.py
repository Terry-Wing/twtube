import os
import re
import asyncio
import contextlib
import logging

import bg_tasks

from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters

log = logging.getLogger('tg_bot')

# 自愈/健康检查参数
_HEALTH_INTERVAL = 60        # 每隔多少秒检查一次
_HEALTH_TIMEOUT = 20         # 单次 Telegram API 探活超时（秒）
_HEALTH_MAX_FAILURES = 3     # 连续探活失败多少次就整体重建
_HEALTH_MAX_PENDING = 2      # 连续多少次发现「取不走的积压更新」就整体重建

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
            # 外层自愈循环。
            # 背景：PTB 的轮询跑在它自己内部的 task 里
            # （Updater.start_polling → network_retry_loop）。出站代理/长连接一旦
            # 抖动，它可能就此不再取件，而外层 await 收不到任何异常。
            # 2026-09-19 实测日志：代理 TLS 握手抛 NetworkError 之后，getUpdates
            # 彻底消失、消息积压在 Telegram（pending_update_count=1），
            # 表现为「机器人无声无息不再收消息，重启容器才好」。
            # 所以不依赖异常，改成每 60s 主动做三项健康检查，任一项判定异常
            # 就整体重建 Application（10s 后重试）。
            while True:
                app = ApplicationBuilder().token(self.token).build()
                app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_message))
                self.bot_app = app  # 保持引用，让 send_notification 用到的始终是当前实例
                try:
                    async with app:
                        await app.start()
                        await app.updater.start_polling()
                        log.info(
                            "Telegram Bot polling started successfully (health check every %ds).",
                            _HEALTH_INTERVAL,
                        )
                        probe_failures = 0
                        pending_strikes = 0
                        while True:
                            await asyncio.sleep(_HEALTH_INTERVAL)
                            # ① PTB 内部的轮询 task 是否还活着
                            # （updater.running 可能是陈旧值，所以直接查 task 本身）
                            polling_task = getattr(app.updater, "_Updater__polling_task", None)
                            if polling_task is not None and polling_task.done():
                                raise RuntimeError("updater polling task exited unexpectedly")
                            if not app.updater.running:
                                raise RuntimeError("updater stopped unexpectedly")
                            # ② 到 Telegram 的 API 是否还通（顺带逼 httpx 重建坏掉的连接）
                            try:
                                await asyncio.wait_for(app.bot.get_me(), timeout=_HEALTH_TIMEOUT)
                                probe_failures = 0
                            except Exception as exc:
                                probe_failures += 1
                                log.warning(
                                    "Telegram liveness probe failed (%d/%d): %s",
                                    probe_failures, _HEALTH_MAX_FAILURES, exc,
                                )
                                if probe_failures >= _HEALTH_MAX_FAILURES:
                                    raise RuntimeError("telegram API unreachable") from exc
                                continue
                            # ③ 最直接的信号：Telegram 上有没有「取不走的」积压更新。
                            # 健康的轮询几秒内就会取走；连续多次仍有积压 = 轮询已哑。
                            try:
                                info = await asyncio.wait_for(
                                    app.bot.get_webhook_info(), timeout=_HEALTH_TIMEOUT)
                                pending = int(getattr(info, "pending_update_count", 0) or 0)
                            except Exception as exc:
                                log.debug("pending_update_count probe failed: %s", exc)
                                pending = 0
                            if pending > 0:
                                pending_strikes += 1
                                log.warning(
                                    "Telegram has %d unfetched update(s) (%d/%d strikes).",
                                    pending, pending_strikes, _HEALTH_MAX_PENDING,
                                )
                                if pending_strikes >= _HEALTH_MAX_PENDING:
                                    raise RuntimeError(
                                        f"{pending} update(s) pending but never fetched")
                            else:
                                pending_strikes = 0
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.exception("Telegram Bot loop crashed; rebuilding in 10s: %s", e)
                finally:
                    with contextlib.suppress(Exception):
                        self.bot_app = None
                await asyncio.sleep(10)

        # 用 bg_tasks.create_task 保持强引用并记录意外失败（裸 asyncio.create_task
        # 只被事件循环弱引用，可能在运行中被 GC 回收导致轮询静默消失）。
        self._bot_task = bg_tasks.create_task(_run_bot(), name="telegram_bot")