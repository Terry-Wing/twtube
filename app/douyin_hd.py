import os
import re
import json
import logging
import asyncio
import aiohttp

log = logging.getLogger('douyin_hd')

DOUYIN_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36',
    'Referer': 'https://www.douyin.com/',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
    'Accept-Language': 'zh-CN,zh;q=0.9',
}


def extract_douyin_video_id(url: str) -> str:
    """提取抖音视频 ID"""
    if not url:
        return ""
    m = re.search(r'/video/(\d+)', url)
    if m:
        return m.group(1)
    m = re.search(r'modal_id=(\d+)', url)
    if m:
        return m.group(1)
    return ""


async def resolve_douyin_redirect(url: str) -> str:
    """解析短链重定向"""
    if 'v.douyin.com' in url or 'iesdouyin.com' in url:
        try:
            async with aiohttp.ClientSession(headers=DOUYIN_HEADERS) as session:
                async with session.get(url, allow_redirects=True, timeout=10) as resp:
                    final_url = str(resp.url)
                    vid = extract_douyin_video_id(final_url)
                    if vid:
                        return vid
        except Exception as e:
            log.warning(f"解析短链重定向失败: {e}")
    return extract_douyin_video_id(url)


def _load_cookies_dict() -> dict:
    """从容器 cookies.txt 读取抖音 Cookie"""
    cookies_dict = {}
    cookie_path = "/config/cookies.txt"
    if os.path.exists(cookie_path):
        try:
            with open(cookie_path, 'r', encoding='utf-8', errors='ignore') as f:
                for line in f:
                    if line.startswith('#') or not line.strip():
                        continue
                    parts = line.strip().split('\t')
                    if len(parts) >= 7 and 'douyin.com' in parts[0]:
                        cookies_dict[parts[5]] = parts[6]
        except Exception as e:
            log.warning(f"加载 cookies 失败: {e}")
    return cookies_dict


async def get_douyin_video_detail(url: str) -> dict:
    """抓取抖音最高清视频直链元数据"""
    video_id = await resolve_douyin_redirect(url)
    if not video_id:
        return None

    cookies = _load_cookies_dict()
    target_url = f"https://www.douyin.com/video/{video_id}"

    # 1. 网页端 JSON 提取
    try:
        async with aiohttp.ClientSession(headers=DOUYIN_HEADERS, cookies=cookies) as session:
            async with session.get(target_url, timeout=15) as resp:
                if resp.status == 200:
                    html_text = await resp.text()
                    match_router = re.search(r'window\._ROUTER_DATA\s*=\s*(\{.+?\});</script>', html_text)
                    if match_router:
                        json_data = json.loads(match_router.group(1))
                        loader_data = json_data.get('loaderData', {})
                        for k, v in loader_data.items():
                            if isinstance(v, dict) and 'videoInfoRes' in v:
                                item_list = v.get('videoInfoRes', {}).get('item_list', [])
                                if item_list:
                                    res = _extract_stream_from_aweme(item_list[0], video_id)
                                    if res:
                                        return res
    except Exception as e:
        log.warning(f"网页端提取异常: {e}")

    # 2. 备用移动端 API
    api_url = f"https://www.iesdouyin.com/aweme/v1/web/aweme/detail/?aweme_id={video_id}&aid=1128&version_code=190500"
    try:
        async with aiohttp.ClientSession(headers=DOUYIN_HEADERS, cookies=cookies) as session:
            async with session.get(api_url, timeout=15) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    aweme_detail = data.get('aweme_detail')
                    if aweme_detail:
                        return _extract_stream_from_aweme(aweme_detail, video_id)
    except Exception as e:
        log.warning(f"API 提取异常: {e}")

    return None


def _extract_stream_from_aweme(aweme: dict, video_id: str) -> dict:
    desc = aweme.get('desc', video_id).strip() or f"抖音视频_{video_id}"
    safe_title = re.sub(r'[\\/:*?"<>|]', '_', desc)

    author = aweme.get('author', {}).get('nickname', 'douyin_user')
    video_obj = aweme.get('video', {})
    bit_rate_list = video_obj.get('bit_rate', [])

    best_url = None
    width = 0
    height = 0

    if bit_rate_list:
        sorted_rates = sorted(
            bit_rate_list,
            key=lambda x: (
                x.get('play_addr', {}).get('width', 0) * x.get('play_addr', {}).get('height', 0),
                x.get('bit_rate', 0)
            ),
            reverse=True
        )
        for item in sorted_rates:
            urls = item.get('play_addr', {}).get('url_list', [])
            if urls:
                best_url = urls[0]
                width = item.get('play_addr', {}).get('width', 0)
                height = item.get('play_addr', {}).get('height', 0)
                break

    if not best_url:
        play_addr_list = video_obj.get('play_addr', {}).get('url_list', [])
        if play_addr_list:
            best_url = play_addr_list[0].replace('playwm', 'play')
            width = video_obj.get('width', 0)
            height = video_obj.get('height', 0)

    if not best_url:
        return None

    return {
        'id': video_id,
        'title': safe_title,
        'author': author,
        'play_url': best_url,
        'width': width,
        'height': height,
    }


def _write_chunks_to_file(filepath: str, chunks_data: bytes):
    with open(filepath, 'ab') as f:
        f.write(chunks_data)


async def direct_download_douyin_video(detail: dict, download_dir: str) -> str:
    """直接流式下载超清 MP4（纯内置标准库，无外部依赖）"""
    target_folder = os.path.join(download_dir, 'douyin')
    os.makedirs(target_folder, exist_ok=True)

    filename = f"{detail['title']} - {detail['author']}.mp4"
    filepath = os.path.join(target_folder, filename)

    if os.path.exists(filepath):
        try:
            os.remove(filepath)
        except OSError:
            pass

    headers = {
        'User-Agent': DOUYIN_HEADERS['User-Agent'],
        'Referer': 'https://www.douyin.com/',
    }

    loop = asyncio.get_running_loop()
    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.get(detail['play_url'], timeout=120) as resp:
            if resp.status == 200:
                while True:
                    chunk = await resp.content.read(64 * 1024)
                    if not chunk:
                        break
                    await loop.run_in_executor(None, _write_chunks_to_file, filepath, chunk)
                log.info(f"抖音 1080P/4K 视频已成功直接落盘: {filepath}")
                return filepath
            else:
                raise RuntimeError(f"下载直链失败，HTTP 状态码: {resp.status}")