"""Twitter / X 媒体解析与下载模块。

支持解析 x.com / twitter.com / t.co 推文中的视频与高清图片：
1. 提取推文 ID 并支持短链重定向追踪；
2. 优先调用 FxTwitter API (v2) 提取完整媒体元数据（最高码率视频、原画质量图片、推文正文与作者）；
3. 若首选接口不可用，自动回退到 VxTwitter API；
4. 支持单视频下载（触发真实参数探测）、单图下载以及多图/混合媒体专属目录归档。
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import re
import urllib.parse
from zoneinfo import ZoneInfo
import aiohttp

log = logging.getLogger('twitter_media')

DEFAULT_UA = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 TwTube/1.0'
)

# 匹配 twitter.com / x.com 推文 ID 的正则
TWEET_URL_REGEX = re.compile(
    r'https?://(?:www\.|mobile\.)?(?:twitter\.com|x\.com)/(?:[^/\s]+/status/|i/web/status/|i/status/)(\d+)',
    re.IGNORECASE,
)

TCO_REGEX = re.compile(r'https?://t\.co/[a-zA-Z0-9]+', re.IGNORECASE)


def extract_tweet_id(url: str) -> str | None:
    """从推文 URL 或分享文本中提取推文 Snowflake ID。"""
    if not url:
        return None
    match = TWEET_URL_REGEX.search(url)
    if match:
        return match.group(1)
    return None


def is_twitter_url(url: str) -> bool:
    """判断是否为 Twitter / X 相关链接。"""
    if not url:
        return False
    u = url.lower()
    return 'twitter.com' in u or 'x.com' in u or 't.co/' in u


def _sanitize_filename(name: str) -> str:
    """过滤文件名非法字符并去除多余空白。"""
    if not name:
        return ''
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]', '_', name)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip(' ._')
    return cleaned[:100] or 'tweet'


def _format_beijing_date(epoch_seconds: int | float | None) -> str:
    """将 UTC 时间戳转为北京时间 YYYY-MM-DD。"""
    if not epoch_seconds:
        return ''
    try:
        dt = datetime.datetime.fromtimestamp(epoch_seconds, tz=ZoneInfo('Asia/Shanghai'))
        return dt.strftime('%Y-%m-%d')
    except Exception:
        return ''


def _clean_tweet_text(text: str) -> str:
    """清洗推文文本作为文件/目录标题。"""
    if not text:
        return ''
    # 去除末尾/夹带的 t.co / pic.twitter.com / pic.x.com 链接（无论是否带 https://）
    cleaned = re.sub(r'(?:https?://)?(?:t\.co|pic\.twitter\.com|pic\.x\.com)/\S+', '', text)
    # 替换换行与多余空白
    cleaned = re.sub(r'[\r\n\t]+', ' ', cleaned)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned


def _get_proxy() -> str | None:
    """获取代理 URL（支持 HTTP_PROXY 与 HTTPS_PROXY）。"""
    return os.environ.get('HTTPS_PROXY') or os.environ.get('HTTP_PROXY') or None


async def resolve_short_url(url: str) -> str:
    """如果为 t.co 等短链，解析重定向目标。"""
    if 't.co/' not in url.lower():
        return url
    proxy = _get_proxy()
    headers = {'User-Agent': DEFAULT_UA}
    try:
        async with aiohttp.ClientSession(trust_env=True, headers=headers) as session:
            async with session.head(url, proxy=proxy, allow_redirects=True,
                                    timeout=aiohttp.ClientTimeout(total=10)) as resp:
                return str(resp.url)
    except Exception as e:
        log.warning('解析 Twitter 短链接失败 (%s): %s', url, e)
        return url


async def fetch_fxtwitter_detail(tweet_id: str) -> dict | None:
    """从 FxTwitter v2 API 拉取推文元数据。"""
    api_url = f'https://api.fxtwitter.com/2/status/{tweet_id}'
    proxy = _get_proxy()
    headers = {'User-Agent': DEFAULT_UA, 'Accept': 'application/json'}
    try:
        async with aiohttp.ClientSession(trust_env=True, headers=headers) as session:
            async with session.get(api_url, proxy=proxy,
                                   timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    log.warning('FxTwitter 返回状态码 %s for tweet %s', resp.status, tweet_id)
                    return None
                data = await resp.json()
                code = data.get('code')
                if code not in (200, None):
                    return None
                # status or tweet payload
                status = data.get('status') or data.get('tweet')
                if not status or not isinstance(status, dict):
                    return None
                return _parse_fxtwitter_payload(status, tweet_id)
    except Exception as e:
        log.warning('请求 FxTwitter API 失败 (id=%s): %s', tweet_id, e)
        return None


def _parse_fxtwitter_payload(status: dict, tweet_id: str) -> dict:
    author_obj = status.get('author') or {}
    author_name = author_obj.get('name') or author_obj.get('screen_name') or 'TwitterUser'
    screen_name = author_obj.get('screen_name') or ''
    created_ts = status.get('created_timestamp')

    text = status.get('text') or ''
    raw_text = text
    title = _clean_tweet_text(text) or f'tweet_{tweet_id}'

    # 提取媒体：先看自身 media，若无再检查 quote
    media = status.get('media') or {}
    if not (media.get('videos') or media.get('photos')) and status.get('quote'):
        quote = status.get('quote')
        if isinstance(quote, dict) and quote.get('media'):
            media = quote.get('media') or {}

    videos = []
    for v in (media.get('videos') or []):
        best_url = None
        highest_bitrate = -1
        for fmt in (v.get('formats') or []):
            if fmt.get('container') == 'mp4':
                br = fmt.get('bitrate', 0)
                if br > highest_bitrate:
                    highest_bitrate = br
                    best_url = fmt.get('url')
        if not best_url:
            best_url = v.get('url')
        if best_url:
            videos.append(best_url)

    photos = []
    for p in (media.get('photos') or []):
        p_url = p.get('url')
        if p_url:
            # 确保获取原图 ?name=orig
            if 'twimg.com' in p_url and 'name=' not in p_url:
                delim = '&' if '?' in p_url else '?'
                p_url = f'{p_url}{delim}name=orig'
            photos.append(p_url)

    media_type = 'none'
    if videos and photos:
        media_type = 'mixed'
    elif videos:
        media_type = 'video'
    elif photos:
        media_type = 'images'

    return {
        'id': tweet_id,
        'author': author_name,
        'screen_name': screen_name,
        'upload_date': _format_beijing_date(created_ts),
        'title': title,
        'raw_text': raw_text,
        'type': media_type,
        'videos': videos,
        'images': photos,
        'total_count': len(videos) + len(photos),
    }


async def fetch_vxtwitter_detail(tweet_id: str) -> dict | None:
    """从 VxTwitter API (备用) 拉取推文元数据。"""
    api_url = f'https://api.vxtwitter.com/Twitter/status/{tweet_id}'
    proxy = _get_proxy()
    headers = {'User-Agent': DEFAULT_UA, 'Accept': 'application/json'}
    try:
        async with aiohttp.ClientSession(trust_env=True, headers=headers) as session:
            async with session.get(api_url, proxy=proxy,
                                   timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                if not data or not isinstance(data, dict):
                    return None
                return _parse_vxtwitter_payload(data, tweet_id)
    except Exception as e:
        log.warning('请求 VxTwitter API 失败 (id=%s): %s', tweet_id, e)
        return None


def _parse_vxtwitter_payload(data: dict, tweet_id: str) -> dict:
    author_name = data.get('user_name') or data.get('user_screen_name') or 'TwitterUser'
    screen_name = data.get('user_screen_name') or ''
    created_ts = data.get('date_epoch')

    text = data.get('text') or ''
    raw_text = text
    title = _clean_tweet_text(text) or f'tweet_{tweet_id}'

    media_ext = data.get('media_extended') or []
    videos = []
    photos = []
    for item in media_ext:
        mtype = item.get('type')
        murl = item.get('url')
        if not murl:
            continue
        if mtype in ('video', 'gif'):
            videos.append(murl)
        elif mtype == 'image':
            if 'twimg.com' in murl and 'name=' not in murl:
                delim = '&' if '?' in murl else '?'
                murl = f'{murl}{delim}name=orig'
            photos.append(murl)

    media_type = 'none'
    if videos and photos:
        media_type = 'mixed'
    elif videos:
        media_type = 'video'
    elif photos:
        media_type = 'images'

    return {
        'id': tweet_id,
        'author': author_name,
        'screen_name': screen_name,
        'upload_date': _format_beijing_date(created_ts),
        'title': title,
        'raw_text': raw_text,
        'type': media_type,
        'videos': videos,
        'images': photos,
        'total_count': len(videos) + len(photos),
    }


async def resolve_twitter_media(url: str) -> dict | None:
    """解析 Twitter / X 推文的媒体信息（支持视频与图片）。

    自动识别短链、首选 FxTwitter，若失败自动降级至 VxTwitter。
    返回解析结果字典；若无媒体或无法解析则返回 None。
    """
    cleaned_url = url.strip()
    if 't.co/' in cleaned_url.lower():
        cleaned_url = await resolve_short_url(cleaned_url)

    tweet_id = extract_tweet_id(cleaned_url)
    if not tweet_id:
        log.info('未能从 URL 解析出 Twitter 推文 ID: %s', url)
        return None

    # 1. 首选 FxTwitter v2
    detail = await fetch_fxtwitter_detail(tweet_id)
    if not detail or detail['total_count'] == 0:
        # 2. 备选 VxTwitter
        detail = await fetch_vxtwitter_detail(tweet_id)

    if not detail:
        log.info('未能通过 API 解析到推文 %s 的媒体元数据', tweet_id)
        return None

    if detail['total_count'] == 0:
        log.info('推文 %s 未包含任何可下载的媒体文件', tweet_id)
        return None

    return detail


async def download_stream(video_url: str, dest_path: str,
                          progress_cb=None, is_canceled=None) -> str:
    """流式下载单个视频文件到 dest_path。"""
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    tmp_path = dest_path + '.part'
    proxy = _get_proxy()
    headers = {'User-Agent': DEFAULT_UA, 'Referer': 'https://x.com/'}

    downloaded = 0
    if os.path.exists(tmp_path):
        downloaded = os.path.getsize(tmp_path)

    req_headers = dict(headers)
    if downloaded > 0:
        req_headers['Range'] = f'bytes={downloaded}-'

    async with aiohttp.ClientSession(trust_env=True, headers=headers) as session:
        async with session.get(
            video_url,
            headers=req_headers,
            proxy=proxy,
            timeout=aiohttp.ClientTimeout(total=1800, sock_read=60),
        ) as resp:
            if resp.status not in (200, 206):
                if downloaded > 0 and resp.status == 416:
                    # 范围请求超出，重新从头下载
                    downloaded = 0
                    req_headers.pop('Range', None)
                    async with session.get(video_url, headers=req_headers, proxy=proxy) as r2:
                        return await _write_response(r2, tmp_path, dest_path, 0, progress_cb, is_canceled)
                raise RuntimeError(f'下载视频 HTTP 错误: {resp.status}')

            mode = 'ab' if resp.status == 206 else 'wb'
            if resp.status == 200:
                downloaded = 0

            content_len = resp.headers.get('Content-Length')
            total = (int(content_len) + downloaded) if content_len else None

            return await _write_response(resp, tmp_path, dest_path, downloaded, total, progress_cb, is_canceled, mode=mode)


async def _write_response(resp, tmp_path, dest_path, initial_downloaded, total, progress_cb, is_canceled, mode='wb') -> str:
    downloaded = initial_downloaded
    with open(tmp_path, mode) as f:
        buf = bytearray()
        buf_limit = 256 * 1024
        async for chunk in resp.content.iter_chunked(64 * 1024):
            if is_canceled and is_canceled():
                raise asyncio.CancelledError()
            buf.extend(chunk)
            downloaded += len(chunk)
            if len(buf) >= buf_limit:
                to_write = bytes(buf)
                buf.clear()
                await asyncio.to_thread(f.write, to_write)
            if progress_cb:
                await progress_cb(downloaded, total)
        if buf:
            await asyncio.to_thread(f.write, bytes(buf))
            buf.clear()
        await asyncio.to_thread(f.flush)

    os.replace(tmp_path, dest_path)
    return dest_path


async def download_images_and_media(detail: dict, dest_dir: str,
                                   progress_cb=None, is_canceled=None) -> list[str]:
    """批量下载推文中的图片与混合媒体到 dest_dir，并保存推文说明文本。"""
    os.makedirs(dest_dir, exist_ok=True)
    proxy = _get_proxy()
    headers = {'User-Agent': DEFAULT_UA, 'Referer': 'https://x.com/'}
    saved_files = []

    images = detail.get('images') or []
    videos = detail.get('videos') or []
    total_items = len(images) + len(videos)
    completed_items = 0

    async with aiohttp.ClientSession(trust_env=True, headers=headers,
                                     timeout=aiohttp.ClientTimeout(total=600)) as session:
        # 1. 下载所有图片
        for idx, img_url in enumerate(images, start=1):
            if is_canceled and is_canceled():
                raise asyncio.CancelledError()

            # 推断扩展名
            ext = '.jpg'
            parsed = urllib.parse.urlparse(img_url)
            path_lower = parsed.path.lower()
            if '.png' in path_lower or 'format=png' in parsed.query:
                ext = '.png'
            elif '.webp' in path_lower or 'format=webp' in parsed.query:
                ext = '.webp'

            img_name = f'{idx:02d}{ext}'
            img_path = os.path.join(dest_dir, img_name)
            tmp_path = img_path + '.part'
            try:
                async with session.get(img_url, proxy=proxy) as resp:
                    if resp.status == 200:
                        data = await resp.read()
                        await asyncio.to_thread(lambda: open(tmp_path, 'wb').write(data))
                        os.replace(tmp_path, img_path)
                        saved_files.append(img_path)
                    else:
                        log.warning('下载图片 %s 状态异常: %s', img_url, resp.status)
            except Exception as e:
                log.warning('下载图片 %s 失败: %s', img_url, e)

            completed_items += 1
            if progress_cb:
                await progress_cb(completed_items, total_items)

        # 2. 如果包含视频（多媒体混合推文），下载视频文件
        for v_idx, v_url in enumerate(videos, start=1):
            if is_canceled and is_canceled():
                raise asyncio.CancelledError()
            v_name = f'video_{v_idx:02d}.mp4'
            v_path = os.path.join(dest_dir, v_name)
            try:
                await download_stream(v_url, v_path, is_canceled=is_canceled)
                saved_files.append(v_path)
            except Exception as e:
                log.warning('下载推文混合视频 %s 失败: %s', v_url, e)

            completed_items += 1
            if progress_cb:
                await progress_cb(completed_items, total_items)

        # 3. 保存推文正文文本备查
        raw_text = detail.get('raw_text')
        if raw_text:
            text_file = os.path.join(dest_dir, 'tweet.txt')
            try:
                content = (
                    f"Author: {detail.get('author')} (@{detail.get('screen_name')})\n"
                    f"Date: {detail.get('upload_date')}\n"
                    f"Tweet ID: {detail.get('id')}\n"
                    f"Text:\n{raw_text}\n"
                )
                await asyncio.to_thread(lambda: open(text_file, 'w', encoding='utf-8').write(content))
                saved_files.append(text_file)
            except Exception as e:
                log.warning('保存 tweet.txt 失败: %s', e)

    return saved_files
