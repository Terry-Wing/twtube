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
