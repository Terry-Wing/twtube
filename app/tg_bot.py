import os
import re
import html
import asyncio
import contextlib
import logging
from urllib.parse import urlsplit, unquote

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

# MTProto（Telethon）部分
_TELETHON_RETRY_DELAY = 30       # Telethon 断开后重建前的等待
_TELETHON_HEALTH_INTERVAL = 30   # Telethon 健康检查间隔
# 单次媒体下载的兜底超时：正常完成会自行结束，这个只用于避免任务永久悬挂。
_TG_MEDIA_DOWNLOAD_TIMEOUT = 3600
# 进度上报节流：Telethon 回调很密集，每 1s 广播一次足够网页进度条平滑。
_TG_PROGRESS_INTERVAL = 1.0

import time


def _parse_proxy_url(raw: str):
    """把代理 URL 转成 Telethon 的 proxy 参数。

    Telethon 只支持 socks5/mtproxy，**不支持 http 代理**（http 代理仅用于
    python-telegram-bot 的 Bot API 请求）。因此这里只在识别到 socks5/socks4
    时才返回参数，其它协议返回 None 让调用方直连。

    返回 None 或 dict(proxy_type, addr, port, username, password, rdns)。
    """
    raw = (raw or '').strip()
    if not raw:
        return None
    try:
        parts = urlsplit(raw)
        scheme = (parts.scheme or '').lower()
    except ValueError:
        log.warning('无法解析 TG_PROXY_URL，将直连')
        return None
    if scheme not in ('socks5', 'socks5h', 'socks4', 'socks4a'):
        log.warning(
            'Telethon 不支持 "%s" 代理（仅 socks5/socks4），媒体采集将直连；'
            'Bot API 机器人仍照常走 HTTP_PROXY。',
            scheme or '未知协议',
        )
        return None
    if not parts.hostname or not parts.port:
        log.warning('TG_PROXY_URL 缺少主机或端口，将直连')
        return None
    proxy_type = 'socks5' if scheme.startswith('socks5') else 'socks4'
    # socks5h/socks4a 表示由代理端解析域名（rdns=True）；其余本地解析。
    rdns = scheme.endswith('h') or scheme.endswith('a')
    proxy = {
        'proxy_type': proxy_type,
        'addr': parts.hostname,
        'port': int(parts.port),
        'rdns': rdns,
    }
    if parts.username:
        proxy['username'] = unquote(parts.username)
    if parts.password:
        proxy['password'] = unquote(parts.password)
    return proxy


class TelegramBotManager:
    def __init__(self, config, dqueue):
        self.config = config
        self.dqueue = dqueue
        self.token = os.environ.get('TG_BOT_TOKEN', '').strip()
        self.allowed_chat_id = os.environ.get('TG_CHAT_ID', '').strip()
        self.bot_app = None
        # MTProto 凭据（用于下载媒体文件；缺失时媒体采集功能自动禁用）
        self.api_id = os.environ.get('TG_API_ID', '').strip()
        self.api_hash = os.environ.get('TG_API_HASH', '').strip()
        self.proxy = _parse_proxy_url(os.environ.get('TG_PROXY_URL', '').strip())
        self.tg_client = None
        # 记录 URL / 视频 ID 对应的 Telegram 消息上下文（用于下载完成后精准引用回复）
        self._msg_contexts: dict[str, dict] = {}
        self._max_msg_contexts = 500
        # 相册（media group）聚合：Telegram 会把一组图片拆成多条消息推送，
        # 这里把同组附件收集起来一次性登记，避免相册被拆成多条完成记录。
        self._albums: dict[str, dict] = {}
        # 在途下载计数：传输期间健康检查不得断开连接（详见 _run_mtproto）。
        self._active_downloads = 0

    def _record_msg_context(self, url: str, chat_id: str, message_id: int):
        if not url:
            return
        if len(self._msg_contexts) >= self._max_msg_contexts:
            # 超过容量上限时淘汰最早的一半记录
            old_keys = list(self._msg_contexts.keys())[:len(self._msg_contexts) // 2]
            for k in old_keys:
                self._msg_contexts.pop(k, None)
        ctx = {'chat_id': chat_id, 'message_id': message_id, 'time': time.time()}
        self._msg_contexts[url] = ctx
        clean_url = self._extract_url(url) or url
        self._msg_contexts[clean_url] = ctx

    def _get_and_pop_msg_context(self, url: str | None, dl_id: str | None = None) -> dict | None:
        candidates = [c for c in (url, dl_id) if c]
        for c in candidates:
            if c in self._msg_contexts:
                ctx = self._msg_contexts.pop(c)
                # 清理关联的重复项
                for k, v in list(self._msg_contexts.items()):
                    if v.get('message_id') == ctx.get('message_id') and v.get('chat_id') == ctx.get('chat_id'):
                        self._msg_contexts.pop(k, None)
                return ctx
        return None

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

    # ===================== 权限校验 =====================

    def _is_authorized(self, chat_id, user_id) -> bool:
        if not self.allowed_chat_id:
            return True
        return str(chat_id) == self.allowed_chat_id or str(user_id) == self.allowed_chat_id

    # ===================== 文本链接（Bot API 路径） =====================

    async def _handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.message:
            return

        user_id = str(update.effective_user.id)
        chat_id = str(update.effective_chat.id)
        message_id = update.message.message_id

        # 权限校验：如果配置了 TG_CHAT_ID，则仅允许指定用户使用
        if not self._is_authorized(chat_id, user_id):
            log.warning(f"Unauthorized TG message from user_id: {user_id}, chat_id: {chat_id}")
            return

        # 媒体文件/图片走跟文本不同的处理分支（纯文本消息这里直接返回）
        if update.message.video or update.message.document or update.message.photo:
            await self._handle_media(update)
            return

        text = update.message.text or update.message.caption
        if not text:
            return

        raw_text = text.strip()
        extracted_url = self._extract_url(raw_text)

        if not extracted_url:
            await update.message.reply_text("❌ 未检测到有效的视频链接，请重新发送。")
            return

        # 预先进行链接清洗，确保提示与平台识别完全准确
        if 'douyin.com' in extracted_url and 'modal_id=' in extracted_url:
            m = re.search(r'modal_id=(\d+)', extracted_url)
            if m:
                extracted_url = f"https://www.douyin.com/video/{m.group(1)}"

        # 记录消息上下文，用于后续下载完成时精准引用回复原消息
        self._record_msg_context(extracted_url, chat_id, message_id)

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

    # ===================== 媒体文件（MTProto 路径） =====================

    async def _handle_media(self, update: Update):
        """处理机器人收到的视频/图片/文件：落到本地并登记进完成列表。"""
        message = update.message
        chat_id = str(update.effective_chat.id)
        user_id = str(update.effective_user.id)
        if not self._is_authorized(chat_id, user_id):
            log.warning(f"Unauthorized TG media from user_id: {user_id}, chat_id: {chat_id}")
            return

        if self.tg_client is None:
            await message.reply_text(
                "⚠️ 媒体采集未启用或 MTProto 未连接。\n"
                "请检查 TG_API_ID / TG_API_HASH / TG_PROXY_URL 是否配置正确，并查看容器日志。"
            )
            return

        # 相册（media_group_id 相同的多条消息）：等整组到齐后统一登记，
        # 避免一次相册被拆成多条完成记录。
        media_group_id = getattr(message, 'media_group_id', None)
        if media_group_id:
            self._buffer_album_member(update, str(media_group_id))
            return

        ok, detail = await self._fetch_and_register(
            message_id=message.message_id,
            chat_id=chat_id,
            is_image=bool(message.photo),
        )
        if ok:
            reply = detail
        else:
            reply = f"❌ 媒体处理失败：<code>{html.escape(str(detail))[:300]}</code>"
        with contextlib.suppress(Exception):
            await message.reply_text(reply, parse_mode="HTML")

    def _buffer_album_member(self, update: Update, media_group_id: str):
        """相册成员到达：入缓冲；每个成员到达会重置静默计时，静默 1.5s 后整组处理。"""
        bucket = self._albums.get(media_group_id)
        if bucket is None:
            bucket = {
                'chat_id': str(update.effective_chat.id),
                'members': [],          # [(message_id, is_image, name_hint)]
                'timer': None,
                'reply_to': None,
            }
            self._albums[media_group_id] = bucket

        message = update.message
        bucket['members'].append((
            message.message_id,
            bool(message.photo),
            self._message_name_hint(message),
        ))
        if bucket['reply_to'] is None:
            bucket['reply_to'] = message

        if bucket['timer'] is not None:
            bucket['timer'].cancel()
        loop = asyncio.get_running_loop()
        bucket['timer'] = loop.call_later(
            1.5, lambda: bg_tasks.create_task(
                self._flush_album(media_group_id), name='tg_album_flush')
        )
        # 异常情况下（如中途崩溃未触发 flush）防止 _albums 无限增长：只清理旧桶。
        if len(self._albums) > 200:
            for k in [k for k, v in self._albums.items() if v is not bucket][:100]:
                stale = self._albums.pop(k, None)
                if stale and stale.get('timer') is not None:
                    stale['timer'].cancel()

    async def _flush_album(self, media_group_id: str):
        bucket = self._albums.pop(media_group_id, None)
        if not bucket:
            return
        if bucket.get('timer') is not None:
            bucket['timer'].cancel()

        saved, failures = 0, []
        for message_id, is_image, name_hint in bucket['members']:
            try:
                ok, detail = await self._fetch_and_register(
                    message_id=message_id,
                    chat_id=bucket['chat_id'],
                    is_image=is_image,
                    name_hint=name_hint,
                )
            except Exception as exc:
                ok, detail = False, str(exc)
                log.exception('相册附件处理失败 msg=%s: %s', message_id, exc)
            if ok:
                saved += 1
            else:
                failures.append((name_hint, str(detail)))

        reply_to = bucket.get('reply_to')
        if reply_to is not None:
            lines = [f"✅ 相册已归档 {saved} 项"]
            if failures:
                lines.append(f"❌ 失败 {len(failures)} 项：")
                for name_hint, reason in failures:
                    lines.append(
                        f"• <code>{html.escape(str(name_hint))[:60]}</code>\n"
                        f"   {html.escape(reason)[:200]}"
                    )
            with contextlib.suppress(Exception):
                await reply_to.reply_text("\n".join(lines), parse_mode="HTML")

    async def _fetch_and_register(self, *, message_id, chat_id, is_image: bool,
                                  name_hint: str | None = None) -> tuple[bool, str]:
        """下载一个附件并登记；成功返回 (True, 成功文案)，失败返回 (False, 失败原因)。

        失败时同样往完成列表写一条 error 记录，使它和 yt-dlp 的失败一样在网页可见，
        而不是只在聊天里回一句就消失。
        """
        info = None
        abs_path = None
        try:
            message = await self._resolve_message(message_id, chat_id)
            if name_hint is None:
                name_hint = self._media_name_hint(message)
            info, abs_path = await self.dqueue.begin_tg_media(
                file_name=name_hint,
                source_message_id=message_id,
                chat_id=chat_id,
                is_image=is_image,
            )
            # 传输期间置位：健康检查看到在途下载就不做断开判定。
            self._active_downloads += 1
            try:
                await self._download_media(message, info, abs_path)
            finally:
                self._active_downloads -= 1
            await self.dqueue.finish_tg_media(info, abs_path)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception('TG 媒体处理失败 msg=%s: %s', message_id, exc)
            # 丢掉半截文件：留着会既占空间、又可能被误当成完整文件。
            self._discard_partial(abs_path)
            if info is not None:
                with contextlib.suppress(Exception):
                    await self.dqueue.fail_tg_media(info, exc)
            return False, str(exc)

        size_txt = ''
        if info.size:
            size_txt = f"\n💾 大小: <code>{self._human_size(info.size)}</code>"
        return True, (
            f"✅ 已归档到 TwTube\n"
            f"📁 目录: <code>{html.escape(str(info.folder))}</code>\n"
            f"🎬 文件: <code>{html.escape(str(info.title))}</code>{size_txt}"
        )

    async def _resolve_message(self, message_id, chat_id):
        """按 chat/message id 取出那条消息（用于取文件名并下载）。"""
        if self.tg_client is None:
            raise RuntimeError('MTProto 客户端未就绪')
        entity = await self.tg_client.get_entity(int(chat_id))
        message = await self.tg_client.get_messages(entity, ids=int(message_id))
        if message is None or message.media is None:
            raise RuntimeError('消息中没有可下载的媒体')
        return message

    async def _download_media(self, message, info, abs_path) -> str:
        """把媒体下载到已分配的具体路径，期间上报进度；失败即抛异常。

        注意三条实测踩过的坑（2026-09-26）：
        1. `download_media` **没有** `timeout` 参数，传了会直接 TypeError；
        2. bot 不能用聊天历史接口（get_messages 不带 ids 会抛 BotMethodInvalidError），
           但 `get_messages(peer, ids=<单条>)` 是允许的，且能拿到真实 file_reference。
           不要改走 Bot API 的 file_id —— Telethon 的 resolve_bot_file_id 对现行
           file_id 格式（version 4）会返回 None；
        3. 传输中途若连接被断开，download_media 会**静默返回 None** 并留下半截文件，
           既不报错也不重试。所以这里必须把「返回假值」当成失败，并校验最终字节数。
        """
        if self.tg_client is None:
            raise RuntimeError('MTProto 客户端未就绪')

        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        doc = getattr(message, 'document', None)
        expected = getattr(doc, 'size', None) if doc is not None else None

        last_update = [0.0]

        async def _progress(current, total_bytes):
            now = time.monotonic()
            if now - last_update[0] < _TG_PROGRESS_INTERVAL:
                return
            last_update[0] = now
            total = total_bytes or expected
            info.status = 'downloading'
            if total:
                info.percent = round(current * 100.0 / total, 1)
                info.size = total
                info.msg = f'已下载 {self._human_size(current)} / {self._human_size(total)}'
            else:
                info.msg = f'已下载 {self._human_size(current)}'
            # 进度上报失败不能拖垮下载本身。
            with contextlib.suppress(Exception):
                await self.dqueue.update_tg_media(info)

        try:
            path = await asyncio.wait_for(
                self.tg_client.download_media(
                    message, file=abs_path, progress_callback=_progress),
                timeout=_TG_MEDIA_DOWNLOAD_TIMEOUT,
            )
        except asyncio.TimeoutError:
            raise RuntimeError(f'下载超时（超过 {_TG_MEDIA_DOWNLOAD_TIMEOUT // 60} 分钟）')

        # 以磁盘上的真实字节数为准，而不是 download_media 的返回值：连接恰好在
        # 文件落盘那一刻被断开时，它会返回 None，但文件其实是完整的——只看返回
        # 值会把这种「其实成功了」误报成失败，并把好文件删掉。
        got = os.path.getsize(abs_path) if os.path.exists(abs_path) else 0
        if expected:
            if got != expected:
                raise RuntimeError(
                    f'文件不完整（{self._human_size(got)}/{self._human_size(expected)}）')
        elif not path or not got:
            # 拿不到预期大小时无法核对字节数，退回到「返回了路径且确有内容」。
            raise RuntimeError('下载中断，未收到完整文件（连接可能被断开）')
        log.info('TG 媒体已落盘: %s', abs_path)
        return os.path.basename(abs_path)

    @staticmethod
    def _discard_partial(abs_path):
        """下载失败后清掉半截文件（不存在则忽略）。"""
        if not abs_path:
            return
        with contextlib.suppress(OSError):
            os.remove(abs_path)

    @staticmethod
    def _message_name_hint(message) -> str:
        """取 Telegram 文件原名的入口（供相册提前记录名字）。"""
        return TelegramBotManager._media_name_hint(message)

    @staticmethod
    def _media_name_hint(message) -> str:
        """取 Telegram 文件的原名：文档用 file_name，视频/图片按属性兜底命名。"""
        doc = getattr(message, 'document', None)
        if doc is not None and getattr(doc, 'attributes', None):
            for attr in doc.attributes:
                name = getattr(attr, 'file_name', None)
                if name:
                    return name
            for attr in doc.attributes:
                if getattr(attr, 'duration', None) is not None:
                    return f'tg_video_{getattr(message, "id", "x")}.mp4'
        if getattr(message, 'photo', None) is not None:
            return f'tg_photo_{getattr(message, "id", "x")}.jpg'
        return f'tg_media_{getattr(message, "id", "x")}'

    @staticmethod
    def _human_size(num) -> str:
        try:
            num = float(num)
        except (TypeError, ValueError):
            return str(num)
        for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
            if num < 1024 or unit == 'TB':
                return f"{int(num)} B" if unit == 'B' else f"{num:.1f} {unit}"
            num /= 1024
        return f"{num:.1f} TB"

    async def send_notification(self, text: str, dl=None):
        """用于下载完成后的回传通知，支持引用触发下载的原消息"""
        if not self.bot_app:
            return

        chat_id = self.allowed_chat_id
        reply_to_message_id = None

        if dl:
            url = getattr(dl, 'url', None)
            dl_id = getattr(dl, 'id', None)
            ctx = self._get_and_pop_msg_context(url, dl_id)
            if ctx:
                chat_id = ctx.get('chat_id') or chat_id
                reply_to_message_id = ctx.get('message_id')
            elif isinstance(url, str) and url.startswith('tg://'):
                # Telegram 采集的媒体：不回传完成通知，避免用户每发一个文件
                # 就收到一条多余消息（归档结果已由 _handle_media 直接回复）。
                return

        if not chat_id:
            return

        try:
            if reply_to_message_id:
                try:
                    await self.bot_app.bot.send_message(
                        chat_id=chat_id,
                        text=text,
                        parse_mode="HTML",
                        reply_to_message_id=reply_to_message_id,
                    )
                    return
                except Exception as exc:
                    log.warning(f"Failed to reply to message {reply_to_message_id} (message may be deleted): {exc}, sending without reply")

            await self.bot_app.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="HTML"
            )
        except Exception as e:
            log.warning(f"Failed to send TG notification: {e}")

    # ===================== 启动与自愈 =====================

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
                app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, self._handle_message))
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

        # MTProto 媒体采集（可选，缺凭据时自动跳过）
        if self.api_id and self.api_hash:
            self._mtproto_task = bg_tasks.create_task(
                self._run_mtproto(), name="telegram_mtproto")
        else:
            log.info(
                "TG_API_ID / TG_API_HASH not configured. "
                "Telegram media (video/photo) capture disabled; link downloads still work."
            )

    async def _run_mtproto(self):
        """Telethon 客户端自愈循环：断线自动重连，失败后延迟重建。"""
        try:
            from telethon import TelegramClient
        except Exception as exc:
            log.error("telethon 未安装，媒体采集功能不可用: %s", exc)
            return

        session_path = os.path.join(getattr(self.config, 'STATE_DIR', '.'), 'tg_media')
        while True:
            client = None
            try:
                client = TelegramClient(
                    session_path,
                    int(self.api_id),
                    self.api_hash,
                    proxy=self.proxy,
                )
                await client.start(bot_token=self.token)
                self.tg_client = client
                log.info("Telegram MTProto client started (media capture enabled).")
                while True:
                    await asyncio.sleep(_TELETHON_HEALTH_INTERVAL)
                    # 有下载在途时不判定断线：download_media 传输期间若被
                    # disconnect()，它会静默返回 None 并留下半截文件，既不报错
                    # 也不重试——大文件（图片的万倍）正好落在多个检查窗口里，
                    # 这就是「相册图片全成功、视频失败」的成因。等传输结束再查。
                    if self._active_downloads > 0:
                        continue
                    if not client.is_connected():
                        raise RuntimeError('MTProto 连接已断开')
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("Telegram MTProto client error; rebuilding in %ds: %s",
                              _TELETHON_RETRY_DELAY, exc)
            finally:
                self.tg_client = None
                if client is not None:
                    with contextlib.suppress(Exception):
                        await client.disconnect()
            await asyncio.sleep(_TELETHON_RETRY_DELAY)
