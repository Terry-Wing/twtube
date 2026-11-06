try:
    import pytest
except ImportError:
    pytest = None
from unittest.mock import AsyncMock, patch, MagicMock
import twitter_media
from tg_bot import TelegramBotManager
from ytdl import _detect_platform_subfolder, _clean_and_normalize_url


def test_extract_tweet_id():
    # 标准 x.com 链接
    assert twitter_media.extract_tweet_id('https://x.com/elonmusk/status/1234567890123456789') == '1234567890123456789'
    # 带查询参数
    assert twitter_media.extract_tweet_id('https://x.com/user/status/987654321?s=20&t=abc') == '987654321'
    # twitter.com 域名
    assert twitter_media.extract_tweet_id('https://twitter.com/NASA/status/1122334455') == '1122334455'
    # 手机版子域名
    assert twitter_media.extract_tweet_id('https://mobile.twitter.com/test/status/99887766') == '99887766'
    # 纯分享文案中夹带
    text = 'Look at this awesome post: https://x.com/author/status/55667788 from Twitter!'
    assert twitter_media.extract_tweet_id(text) == '55667788'
    # 无效链接
    assert twitter_media.extract_tweet_id('https://youtube.com/watch?v=123') is None
    assert twitter_media.extract_tweet_id('') is None


def test_is_twitter_url():
    assert twitter_media.is_twitter_url('https://x.com/user/status/123') is True
    assert twitter_media.is_twitter_url('https://twitter.com/user/status/123') is True
    assert twitter_media.is_twitter_url('https://t.co/abcdef') is True
    assert twitter_media.is_twitter_url('https://bilibili.com/video/av1') is False
    assert twitter_media.is_twitter_url('') is False


def test_clean_tweet_text_and_sanitize():
    raw = 'Incredible launch today! 🚀 https://t.co/xyz123 \n Next generation rocket #SpaceX pic.twitter.com/abc'
    cleaned = twitter_media._clean_tweet_text(raw)
    assert 'https://t.co' not in cleaned
    assert 'pic.twitter.com' not in cleaned
    assert '\n' not in cleaned
    assert 'Incredible launch today! 🚀 Next generation rocket #SpaceX' in cleaned

    sanitized = twitter_media._sanitize_filename('A/B\\C:D*E?F"G<H>I|J')
    assert '/' not in sanitized and '\\' not in sanitized and ':' not in sanitized


def test_parse_fxtwitter_video():
    mock_status = {
        'id': '2118251808179323102',
        'text': 'Test video tweet https://t.co/test',
        'created_timestamp': 1793865560,
        'author': {
            'name': 'Elon Musk',
            'screen_name': 'elonmusk'
        },
        'media': {
            'videos': [
                {
                    'url': 'https://video.twimg.com/low.mp4',
                    'formats': [
                        {'container': 'mp4', 'bitrate': 500000, 'url': 'https://video.twimg.com/500k.mp4'},
                        {'container': 'mp4', 'bitrate': 2000000, 'url': 'https://video.twimg.com/2m.mp4'},
                    ]
                }
            ],
            'photos': []
        }
    }
    result = twitter_media._parse_fxtwitter_payload(mock_status, '2118251808179323102')
    assert result['id'] == '2118251808179323102'
    assert result['author'] == 'Elon Musk'
    assert result['type'] == 'video'
    assert len(result['videos']) == 1
    assert result['videos'][0] == 'https://video.twimg.com/2m.mp4'  # 最高码率
    assert len(result['images']) == 0
    assert result['upload_date'] != ''


def test_parse_fxtwitter_photos():
    mock_status = {
        'id': '112233',
        'text': 'Look at these photos',
        'created_timestamp': 1793865560,
        'author': {
            'name': 'Photographer',
            'screen_name': 'photo'
        },
        'media': {
            'videos': [],
            'photos': [
                {'url': 'https://pbs.twimg.com/media/pic1.jpg'},
                {'url': 'https://pbs.twimg.com/media/pic2.png?name=orig'}
            ]
        }
    }
    result = twitter_media._parse_fxtwitter_payload(mock_status, '112233')
    assert result['type'] == 'images'
    assert len(result['images']) == 2
    assert result['images'][0] == 'https://pbs.twimg.com/media/pic1.jpg?name=orig'
    assert result['images'][1] == 'https://pbs.twimg.com/media/pic2.png?name=orig'


def test_platform_subfolder_and_clean_url():
    # 测试 ytdl.py 的 _clean_and_normalize_url
    share_text = 'Check out https://x.com/elonmusk/status/12345?s=20 (shared via app)'
    assert _clean_and_normalize_url(share_text).startswith('https://x.com/elonmusk/status/12345')

    # 测试 ytdl.py 的 _detect_platform_subfolder
    assert _detect_platform_subfolder('https://x.com/user/status/1') == 'twitter'
    assert _detect_platform_subfolder('https://twitter.com/user/status/2') == 'twitter'
    assert _detect_platform_subfolder('https://t.co/abc') == 'twitter'


def test_tg_bot_platform_detection():
    mgr = TelegramBotManager(config=None, dqueue=None)
    assert mgr._detect_platform_folder('https://x.com/user/status/123') == 'twitter'
    assert mgr._detect_platform_folder('https://twitter.com/user/status/456') == 'twitter'
    assert mgr.PLATFORM_DISPLAY.get('twitter') == 'X (Twitter)'
