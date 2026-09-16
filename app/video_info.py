"""下载（含合并）完成后，用 ffprobe 探测最终视频文件的真实分辨率与帧率，
生成 ``[2160x3840 60fps]`` 标签并追加到文件名里。

为什么不在 yt-dlp 输出模板里直接写 ``%(resolution)s`` / ``%(fps)s``：
模板只能拿到"下载前"的元数据，fps 常是 ``60.0`` 这样的浮点、缺失时渲染成 ``NA``，
而模板语法也没法做到"缺了就整段省略"，更拿不到合并后真实文件的属性。这里在文件
真正落地后用 ffprobe 读真实值，所有平台（含抖音直连下载）共用同一套命名逻辑。

所有失败路径都只记日志、原样返回原路径：文件名加标签绝不能让一次已完成的下载变成失败。
"""

import json
import logging
import os
import subprocess

log = logging.getLogger('video_info')

_FFPROBE_TIMEOUT = 30

_FFPROBE_ARGS = (
    'ffprobe', '-v', 'error',
    '-select_streams', 'v:0',
    '-show_entries',
    'stream=width,height,r_frame_rate,avg_frame_rate:stream_side_data=rotation',
    '-of', 'json',
)


def _parse_rate(value) -> float:
    """把 ffprobe 的 ``"30000/1001"`` / ``"60/1"`` 帧率字符串解析成浮点数。"""
    try:
        num, _, den = str(value).partition('/')
        num = float(num)
        den = float(den) if den else 1.0
    except (TypeError, ValueError):
        return 0.0
    return num / den if den else 0.0


def _fps_text(*rates) -> str:
    """把若干候选帧率归一化成文件名里显示的文本。

    整数帧率（60.0、30.0）显示为 ``60`` / ``30``；像 29.97 / 59.94 这类
    非整数帧率保留两位小数。全部取不到时返回空串。
    """
    fps = 0.0
    for rate in rates:
        fps = _parse_rate(rate)
        if fps:
            break
    if not fps:
        return ''
    # 60.0 / 30.0 这类真整数帧率写成整数；29.97 / 59.94 这种 NTSC 帧率保留两位
    # 小数（阈值取得很小，免得把 29.97 当成 30）。
    if abs(fps - round(fps)) < 0.001:
        return str(round(fps))
    return f'{fps:.2f}'.rstrip('0').rstrip('.')


def probe_video(path: str):
    """探测视频文件的 (宽, 高, 帧率文本)；任何失败都返回 (0, 0, '')。"""
    try:
        proc = subprocess.run(
            list(_FFPROBE_ARGS) + [path],
            capture_output=True, text=True, timeout=_FFPROBE_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning('ffprobe 调用失败: %s', exc)
        return 0, 0, ''
    if proc.returncode != 0:
        log.warning('ffprobe 读取失败 (%s): %s', path, (proc.stderr or '').strip())
        return 0, 0, ''
    try:
        streams = json.loads(proc.stdout or '{}').get('streams') or []
    except ValueError:
        log.warning('ffprobe 输出无法解析: %s', path)
        return 0, 0, ''
    if not streams:
        return 0, 0, ''  # 无视频流（例如纯音频）
    stream = streams[0]
    width = int(stream.get('width') or 0)
    height = int(stream.get('height') or 0)

    # 手机竖拍等带旋转元数据的视频，容器里的宽高与实际显示方向差 90 度，
    # 对调后才是用户看到的分辨率。
    rotation = 0
    for side in stream.get('side_data_list') or []:
        if isinstance(side, dict) and side.get('rotation') is not None:
            try:
                rotation = int(side['rotation'])
            except (TypeError, ValueError):
                rotation = 0
            break
    if abs(rotation) % 180 == 90:
        width, height = height, width

    # avg_frame_rate 表示实际平均帧率（例如 NTSC 的 29.97），比容器名义上的
    # r_frame_rate 更贴近肉眼看到的帧率；部分容器里 avg 为 0/0，这时回退到 r。
    fps = _fps_text(stream.get('avg_frame_rate'), stream.get('r_frame_rate'))
    return width, height, fps


def format_tag(width: int, height: int, fps: str = '') -> str:
    """组装文件名标签，如 ``[2160x3840 60fps]``；信息不足时返回空串。

    只有分辨率、没有帧率时退化为 ``[2160x3840]``；连分辨率都没有就不加标签。
    """
    if not width or not height:
        return ''
    if fps:
        return f'[{width}x{height} {fps}fps]'
    return f'[{width}x{height}]'


def _unique_path(path: str) -> str:
    """目标已存在时依次尝试 ``_1``、``_2``…… 避免覆盖同名文件。"""
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    counter = 1
    while os.path.exists(f'{stem}_{counter}{ext}'):
        counter += 1
    return f'{stem}_{counter}{ext}'


def append_tag(path: str, tag: str) -> str:
    """把标签插到扩展名前：``标题.mp4`` → ``标题 [2160x3840 60fps].mp4``。"""
    if not tag:
        return path
    stem, ext = os.path.splitext(path)
    return _unique_path(f'{stem} {tag}{ext}')


def tag_video_file(path: str) -> str:
    """探测并重命名，返回改名后的路径；失败或无需改名时原样返回 *path*。"""
    try:
        if not path or not os.path.isfile(path):
            return path
        stem = os.path.splitext(os.path.basename(path))[0]
        width, height, fps = probe_video(path)
        tag = format_tag(width, height, fps)
        if not tag or stem.endswith(tag):
            return path  # 探测不到，或名字里已经有标签了（重试场景）
        new_path = append_tag(path, tag)
        os.rename(path, new_path)
        log.info('已在文件名中追加分辨率/帧率标签: %s', os.path.basename(new_path))
        return new_path
    except Exception as exc:  # 改名失败不能影响这次已完成的下载
        log.warning('追加分辨率/帧率标签失败 (%s): %s', path, exc)
        return path
