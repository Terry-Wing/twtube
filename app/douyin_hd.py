import re
import urllib.parse
import aiohttp
import logging

log = logging.getLogger('douyin_hd')

DOUYIN_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1',
    'Referer': 'https://www.douyin.com/',
    'Accept': 'application/json, text/plain, */*'
}


def extract_douyin_video_id(url: str) -> str:
    """提取抖音视频 ID"""
    if not url:
        return ""
    # 匹配 /video/xxxxxxxxxxxx 格式
    m = re.search(r'/video/(\d+)', url)
    if m:
        return m.group(1)
    # 匹配 modal_id=xxxxxxxxxxxx 格式
    m = re.search(r'modal_id=(\d+)', url)
    if m:
        return m.group(1)
    return ""


async def resolve_douyin_redirect(url: str) -> str:
    """自动解析短链重定向（如 v.douyin.com）获取真实视频 ID"""
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
    通过抖音移动端 API 直接抓取 1080P/2K/4K 无水印视频信息
    返回符合 yt-dlp 规范的 info_dict 结构，直接无缝兼容 TwTube
    """
    video_id = await resolve_douyin_redirect(url)
    if not video_id:
        return None

    api_url = f"https://www.iesdouyin.com/aweme/v1/web/aweme/detail/?aweme_id={video_id}&aid=1128&version_code=190500"

    try:
        async with aiohttp.ClientSession(headers=DOUYIN_HEADERS) as session:
            async with session.get(api_url, timeout=15) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json(content_type=None)
                
                aweme_detail = data.get('aweme_detail')
                if not aweme_detail:
                    return None

                desc = aweme_detail.get('desc', video_id)
                author = aweme_detail.get('author', {}).get('nickname', 'douyin_user')
                author_id = aweme_detail.get('author', {}).get('unique_id') or aweme_detail.get('author', {}).get('short_id', '')
                create_time = aweme_detail.get('create_time', 0)
                
                # 寻找最高清晰度的播放流
                video_obj = aweme_detail.get('video', {})
                bit_rate_list = video_obj.get('bit_rate', [])
                
                best_url = None
                width = 0
                height = 0

                # 优先从 bit_rate 列表中按分辨率/码率选取最高画质
                if bit_rate_list:
                    # 按分辨率乘积和码率排序
                    sorted_rates = sorted(
                        bit_rate_list,
                        key=lambda x: (
                            x.get('play_addr', {}).get('width', 0) * x.get('play_addr', {}).get('height', 0),
                            x.get('bit_rate', 0)
                        ),
                        reverse=True
                    )
                    best_stream = sorted_rates[0]
                    url_list = best_stream.get('play_addr', {}).get('url_list', [])
                    if url_list:
                        best_url = url_list[0]
                        width = best_stream.get('play_addr', {}).get('width', 0)
                        height = best_stream.get('play_addr', {}).get('height', 0)

                # 降级备用：直接取 play_addr
                if not best_url:
                    play_addr_list = video_obj.get('play_addr', {}).get('url_list', [])
                    if play_addr_list:
                        best_url = play_addr_list[0].replace('playwm', 'play')
                        width = video_obj.get('width', 0)
                        height = video_obj.get('height', 0)

                if not best_url:
                    return None

                # 格式化日期 YYYYMMDD
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
    except Exception as e:
        log.error(f"抖音超清直链解析异常: {e}")
        return None