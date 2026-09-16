"""Tests for ``video_info`` — the resolution/framerate tag in finished filenames."""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

import video_info


def test_fps_text_normalizes_rates():
    assert video_info._fps_text('60/1') == '60'
    assert video_info._fps_text('60.0') == '60'
    assert video_info._fps_text('30') == '30'
    assert video_info._fps_text('30000/1001') == '29.97'
    assert video_info._fps_text('60000/1001') == '59.94'
    assert video_info._fps_text('') == ''
    assert video_info._fps_text(None) == ''
    assert video_info._fps_text('0/1') == ''
    assert video_info._fps_text('', '25/1') == '25'
    # avg_frame_rate 胜过名义上的 r_frame_rate（NTSC）
    assert video_info._fps_text('30000/1001', '30/1') == '29.97'


def test_format_tag():
    assert video_info.format_tag(2160, 3840, '60') == '[2160x3840 60fps]'
    assert video_info.format_tag(1920, 1080, '') == '[1920x1080]'
    assert video_info.format_tag(0, 0, '60') == ''
    assert video_info.format_tag(1080, 0, '60') == ''


def test_append_tag_goes_before_extension():
    assert video_info.append_tag('/tmp/a.mp4', '[1x2 30fps]') == '/tmp/a [1x2 30fps].mp4'
    assert video_info.append_tag('/tmp/a.mp4', '') == '/tmp/a.mp4'


def test_append_tag_avoids_overwriting(tmp_path):
    original = tmp_path / 'a.mp4'
    original.write_bytes(b'x')
    tagged = video_info.append_tag(str(original), '[1x2 30fps]')
    assert os.path.basename(tagged) == 'a [1x2 30fps].mp4'
    tagged_path = tmp_path / 'a [1x2 30fps].mp4'
    tagged_path.write_bytes(b'y')
    assert os.path.basename(
        video_info.append_tag(str(original), '[1x2 30fps]')) == 'a [1x2 30fps]_1.mp4'


def test_tag_video_file_renames_merged_file(tmp_path, monkeypatch):
    target = tmp_path / 'author - 2026-01-01 - title.mp4'
    target.write_bytes(b'x')
    monkeypatch.setattr(video_info, 'probe_video', lambda path: (2160, 3840, '60'))

    renamed = video_info.tag_video_file(str(target))

    assert os.path.basename(renamed) == 'author - 2026-01-01 - title [2160x3840 60fps].mp4'
    assert os.path.exists(renamed)
    assert not os.path.exists(str(target))


def test_tag_video_file_keeps_name_when_probe_finds_nothing(tmp_path, monkeypatch):
    target = tmp_path / 'audio.m4a'
    target.write_bytes(b'x')
    monkeypatch.setattr(video_info, 'probe_video', lambda path: (0, 0, ''))

    assert video_info.tag_video_file(str(target)) == str(target)
    assert os.path.exists(str(target))


def test_tag_video_file_missing_file_is_noop(tmp_path):
    missing = str(tmp_path / 'nope.mp4')
    assert video_info.tag_video_file(missing) == missing


def test_tag_video_file_never_raises(tmp_path, monkeypatch):
    target = tmp_path / 'a.mp4'
    target.write_bytes(b'x')

    def boom(path):
        raise RuntimeError('ffprobe exploded')

    monkeypatch.setattr(video_info, 'probe_video', boom)

    # A rename problem must never turn a finished download into a failure.
    assert video_info.tag_video_file(str(target)) == str(target)
    assert os.path.exists(str(target))


def test_tag_video_file_does_not_double_tag(tmp_path, monkeypatch):
    target = tmp_path / 'a [1x1 5fps].mp4'
    target.write_bytes(b'x')
    monkeypatch.setattr(video_info, 'probe_video', lambda path: (1, 1, '5'))

    assert video_info.tag_video_file(str(target)) == str(target)


@pytest.mark.skipif(
    shutil.which('ffprobe') is None or shutil.which('ffmpeg') is None,
    reason='ffmpeg/ffprobe not installed',
)
def test_probe_video_reads_a_real_file(tmp_path):
    clip = tmp_path / 'clip.mp4'
    subprocess.run(
        ['ffmpeg', '-y', '-v', 'error', '-f', 'lavfi',
         '-i', 'testsrc2=size=320x240:rate=25', '-t', '1',
         '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(clip)],
        check=True,
    )

    width, height, fps = video_info.probe_video(str(clip))

    assert (width, height) == (320, 240)
    assert fps == '25'
