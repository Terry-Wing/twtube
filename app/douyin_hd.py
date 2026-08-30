import re
import json
import urllib.parse
import aiohttp
import logging

log = logging.getLogger('douyin_hd')

DOUYIN_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36',
    'Referer': 'https://www.douyin.com/',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
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
    """自动解析短链重定向"""
    if 'v.douyin.com' in url or 'iesdouyin.com' in url:
        try:
            async with aiohttp.ClientSession(headers=DOUYIN_HEADERS) as session:
                async with session.get(url, allow_redirects=True, timeout=10) as resp:
                    final_url = str(resp.url)
                    vid = extract_douyin_video_id(final_url)
                    if vid:
                        return vid
        except Exception as e:
            log.warning(f"解析抖音短链重定向失败: {e}")
    return extract_douyin_video_id(url)


async def fetch_douyin_hd_info(url: str) -> dict:
    """
    双重解析策略：
    1. 网页 HTML 内嵌高清数据直提 (最高画质)
    2. 移动端 API 备用兜底
    """
    video_id = await resolve_douyin_redirect(url)
    if not video_id:
        return None

    # 读取容器挂载的 cookies.txt（如果有）
    cookies_dict = {}
    try:
        cookie_path = "/config/cookies.txt"
        import os
        if os.path.exists(cookie_path):
            with open(cookie_path, 'r', encoding='utf-8', errors='ignore') as f:
                for line in f:
                    if line.startswith('#') or not line.strip():
                        continue
                    parts = line.strip().split('\t')
                    if len(parts) >= 7 and 'douyin.com' in parts[0]:
                        cookies_dict[parts[5]] = parts[6]
    except Exception as e:
        log.warning(f"读取 cookies.txt 失败: {e}")

    # 策略 1：直接抓取网页 HTML 提取渲染 JSON
    target_url = f"https://www.douyin.com/video/{video_id}"
    try:
        async with aiohttp.ClientSession(headers=DOUYIN_HEADERS, cookies=cookies_dict) as session:
            async with session.get(target_url, timeout=15) as resp:
                if resp.status == 200:
                    html_text = await resp.text()
                    
                    # 匹配 _ROUTER_DATA 或 RENDER_DATA
                    match = re.search(r'<script id="RENDER_DATA" type="application/json">([^<]+)</script>', html_text)
                    if match:
                        raw_data = urllib.parse.unquote(match.group(1))
                        json_data = json.loads(raw_data)
                        # 递归或直接获取 aweme 详情
                        for k, v in json_data.items():
                            if isinstance(v, dict) and 'aweme' in v:
                                aweme = v.get('aweme', {}).get('detailInfo', {}) or v.get('aweme', {})
                                res = parse_aweme_detail(aweme, video_id)
                                if res:
                                    return res

                    # 匹配第二种常见内嵌格式
                    match_router = re.search(r'window\._ROUTER_DATA\s*=\s*(\{.+?\});</script>', html_text)
                    if match_router:
                        json_data = json.loads(match_router.group(1))
                        loader_data = json_data.get('loaderData', {})
                        for k, v in loader_data.items():
                            if isinstance(v, dict) and 'videoInfoRes' in v:
                                item_list = v.get('videoInfoRes', {}).get('item_list', [])
                                if item_list:
                                    res = parse_aweme_detail(item_list[0], video_id)
                                    if res:
                                        return res
    except Exception as e:
        log.warning(f"网页端 HTML 高清解析尝试失败，切换 API: {e}")

    # 策略 2：移动端 API 兜底
    api_url = f"https://www.iesdouyin.com/aweme/v1/web/aweme/detail/?aweme_id={video_id}&aid=1128&version_code=190500"
    try:
        async with aiohttp.ClientSession(headers=DOUYIN_HEADERS, cookies=cookies_dict) as session:
            async with session.get(api_url, timeout=15) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    aweme_detail = data.get('aweme_detail')
                    if aweme_detail:
                        return parse_aweme_detail(aweme_detail, video_id)
    except Exception as e:
        log.error(f"移动端 API 解析失败: {e}")

    return None


def parse_aweme_detail(aweme_detail: dict, video_id: str) -> dict:
    if not aweme_detail:
        return None

    desc = aweme_detail.get('desc', video_id)
    author = aweme_detail.get('author', {}).get('nickname', 'douyin_user')
    author_id = aweme_detail.get('author', {}).get('unique_id') or aweme_detail.get('author', {}).get('short_id', '')
    create_time = aweme_detail.get('create_time', 0)

    video_obj = aweme_detail.get('video', {})
    bit_rate_list = video_obj.get('bit_rate', [])

    best_url = None
    width = 0
    height = 0

    # 优先从最高码率/最高分辨率列表提取
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

    upload_date = ''
    if create_time:
        import datetime
        upload_date = datetime.datetime.fromtimestamp(create_time).strftime('%Y%m%d')

    return {
        'id': video_id,
        'title': desc.strip() or f"抖音视频_{video_id}",
        'url': best_url,
        'webpage_url': f"https://www.douyin.com/video/{video_id}",
        'uploader': author,
        'uploader_id': author_id,
        'channel': author,
        'upload_date': upload_date,
        'ext': 'mp4',
        'width': width,
        'height': height,
        '_type': 'video',
        'direct': True,
    }