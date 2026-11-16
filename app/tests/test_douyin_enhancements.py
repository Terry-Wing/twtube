import pytest
from unittest.mock import AsyncMock, patch, MagicMock
import douyin_hd
from tg_bot import TelegramBotManager


def test_extract_images():
    aweme = {
        'images': [
            {'url_list': ['https://example.com/img1_thumb.jpg', 'https://example.com/img1.jpg'],
             'download_url_list': ['https://example.com/img1_hd.jpg']},
            {'url_list': ['https://example.com/img2.jpg']},
            {'invalid': 'entry'}
        ]
    }
    urls = douyin_hd.extract_images(aweme)
    assert urls == ['https://example.com/img1_hd.jpg', 'https://example.com/img2.jpg']


def test_extract_music():
    aweme = {
        'music': {
            'play_url': {
                'url_list': ['https://example.com/audio.mp3']
            }
        }
    }
    assert douyin_hd.extract_music(aweme) == 'https://example.com/audio.mp3'
    assert douyin_hd.extract_music({}) is None


def test_tg_bot_message_context():
    mgr = TelegramBotManager(config=None, dqueue=None)
    mgr._record_msg_context('https://v.douyin.com/abc/', '12345', 999)

    # Pop with exact url
    ctx = mgr._get_and_pop_msg_context('https://v.douyin.com/abc/')
    assert ctx is not None
    assert ctx['chat_id'] == '12345'
    assert ctx['message_id'] == 999

    # Should be removed now
    assert mgr._get_and_pop_msg_context('https://v.douyin.com/abc/') is None


def test_sanitize_filename_byte_limit():
    long_title = (
        '#纯欲天花板 害人之心我有 但我不敢 防人之心我也有 但是防不住  '
        '精打细算、我算了 没算明白  深思熟虑出来的 都是漏洞  '
        '权衡利弊选出来的全是弊  一怒之下站起来，两眼一黑又坐……版本过低，升级后可展示全部信息'
    )
    cleaned = douyin_hd._sanitize_filename(long_title, max_bytes=150)
    assert len(cleaned.encode('utf-8')) <= 150
    # 确保没有因切断多字节字符而损坏
    cleaned.encode('utf-8').decode('utf-8')


def test_unique_dest_file_within_name_max(tmp_path):
    from ytdl import _unique_dest_file
    import os

    long_name = (
        '大刀不乖 - 2026-05-22 - #纯欲天花板 害人之心我有 但我不敢 防人之心我也有 但是防不住  '
        '精打细算、我算了 没算明白  深思熟虑出来的 都是漏洞  '
        '权衡利弊选出来的全是弊  一怒之下站起来，两眼一黑又坐……版本过低，升级后可展示全部信息.mp4'
    )
    dest = _unique_dest_file(str(tmp_path), long_name, reserve_bytes=48)
    base = os.path.basename(dest)
    assert len(base.encode('utf-8')) <= 255 - 48
    # 加上 .part 也不超过 255 字节
    assert len((base + '.part').encode('utf-8')) <= 255
    # 验证能实际在文件系统创建文件
    with open(dest + '.part', 'w') as f:
        f.write('test')
    assert os.path.exists(dest + '.part')
