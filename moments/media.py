"""
moments/media.py
────────────────
Preparing photos and videos for School Moments.

Photos are turned upright, resized (1600 px on the long side, plus a 640 px
copy for the grid) and saved as JPEG — which also removes hidden data such as
the GPS position of where a phone took the picture.

Videos are saved straight away and prepared in the background with ffmpeg:
H.264 MP4 that every phone and browser plays (already-H.264 videos are only
repackaged, which takes seconds), at most 1280 px on the long side, metadata
(including location) removed, plus a cover picture. Until that is done the
post shows "Video processing…". Without ffmpeg the video is kept as uploaded.
"""
import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
from datetime import timedelta
from io import BytesIO

from django.core.files.base import ContentFile, File
from django.db import close_old_connections, transaction
from django.utils import timezone

from .models import Media

logger = logging.getLogger(__name__)

IMAGE_TYPES = {'.jpg', '.jpeg', '.png', '.webp', '.gif'}
VIDEO_TYPES = {'.mp4', '.mov', '.m4v', '.webm', '.3gp'}
MAX_FILES = 10
MAX_IMAGE = 20 * 1024 * 1024
MAX_VIDEO = 100 * 1024 * 1024
LONG_SIDE, THUMB_SIDE, VIDEO_SIDE = 1600, 640, 1280
STALE_MINUTES = 15


class MediaError(ValueError):
    pass


def kind_of(upload):
    ext = os.path.splitext(upload.name)[1].lower()
    if ext in IMAGE_TYPES:
        return Media.IMAGE
    if ext in VIDEO_TYPES:
        return Media.VIDEO
    raise MediaError(f'"{upload.name}" is not a photo or video we can use (JPG, PNG, WebP, GIF, MP4, MOV or WebM).')


def check(uploads):
    if not uploads:
        raise MediaError('Add at least one photo or video.')
    if len(uploads) > MAX_FILES:
        raise MediaError(f'A post can have up to {MAX_FILES} photos and videos.')
    for u in uploads:
        kind = kind_of(u)
        limit = MAX_VIDEO if kind == Media.VIDEO else MAX_IMAGE
        if u.size > limit:
            raise MediaError(f'"{u.name}" is too big — {"videos" if kind == Media.VIDEO else "photos"} '
                             f'can be up to {limit // (1024 * 1024)} MB.')


def _jpeg(img, side):
    copy = img.copy()
    copy.thumbnail((side, side))
    out = BytesIO()
    copy.save(out, 'JPEG', quality=85, optimize=True, progressive=True)
    return copy.size, out.getvalue()


def save_image(media, upload):
    from PIL import Image, ImageOps, UnidentifiedImageError
    try:
        img = Image.open(upload)
        img.load()
        img = ImageOps.exif_transpose(img)
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        raise MediaError(f'"{upload.name}" could not be opened as a picture.')
    if img.mode in ('RGBA', 'LA', 'P'):
        img = img.convert('RGBA')
        bg = Image.new('RGB', img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[-1])
        img = bg
    elif img.mode != 'RGB':
        img = img.convert('RGB')
    (media.width, media.height), full = _jpeg(img, LONG_SIDE)
    _, small = _jpeg(img, THUMB_SIDE)
    media.file.save('photo.jpg', ContentFile(full), save=False)
    media.thumb.save('thumb.jpg', ContentFile(small), save=False)
    media.status = Media.READY
    media.save()


def save_video(media, upload):
    """Keep the upload as it is for now and prepare it once the post is saved."""
    ext = os.path.splitext(upload.name)[1].lower()
    media.file.save(f'video{ext}', upload, save=False)
    media.status = Media.PROCESSING if shutil.which('ffmpeg') else Media.READY
    media.status_at = timezone.now()
    media.save()
    if media.status == Media.PROCESSING:
        transaction.on_commit(lambda: start(media.id))


def start(media_id):
    threading.Thread(target=_run, args=(media_id,), daemon=True, name=f'moments-video-{media_id}').start()


def _run(media_id):
    try:
        process_video(media_id)
    finally:
        close_old_connections()


def _probe(path):
    out = subprocess.run(['ffprobe', '-v', 'error', '-print_format', 'json', '-show_streams', '-show_format', path],
                         capture_output=True, timeout=60, check=True)
    data = json.loads(out.stdout or b'{}')
    video = next((s for s in data.get('streams', []) if s.get('codec_type') == 'video'), {})
    audio = next((s for s in data.get('streams', []) if s.get('codec_type') == 'audio'), {})
    rotate = 0
    for sd in video.get('side_data_list', []) or []:
        rotate = int(sd.get('rotation') or rotate)
    rotate = int((video.get('tags') or {}).get('rotate') or rotate)
    w, h = int(video.get('width') or 0), int(video.get('height') or 0)
    if abs(rotate) % 180 == 90:
        w, h = h, w
    return {'video': video.get('codec_name', ''), 'audio': audio.get('codec_name', ''), 'width': w, 'height': h,
            'duration': float((data.get('format') or {}).get('duration') or video.get('duration') or 0)}


def process_video(media_id):
    """Turn an uploaded video into a web-friendly MP4 with a cover picture. Safe to run again."""
    media = Media.objects.select_related('post').filter(pk=media_id, kind=Media.VIDEO).first()
    if media is None or not shutil.which('ffmpeg'):
        return None
    work = tempfile.mkdtemp(prefix='moments-')
    try:
        src = os.path.join(work, 'in' + os.path.splitext(media.file.name)[1].lower())
        with media.file.open('rb') as f, open(src, 'wb') as out:
            shutil.copyfileobj(f, out)
        info = _probe(src)
        if not info['video']:
            raise MediaError('no video stream')
        dst, poster = os.path.join(work, 'out.mp4'), os.path.join(work, 'poster.jpg')
        common = ['-map_metadata', '-1', '-movflags', '+faststart']
        if info['video'] == 'h264' and info['audio'] in ('', 'aac', 'mp3') \
                and max(info['width'], info['height']) <= VIDEO_SIDE * 1.5:
            cmd = ['ffmpeg', '-y', '-v', 'error', '-i', src, '-c', 'copy'] + common + [dst]
        else:
            scale = (f"scale='if(gte(iw,ih),min({VIDEO_SIDE},iw),-2)':'if(gte(iw,ih),-2,min({VIDEO_SIDE},ih))'")
            cmd = ['ffmpeg', '-y', '-v', 'error', '-i', src, '-vf', scale, '-c:v', 'libx264', '-preset', 'veryfast',
                   '-crf', '26', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '128k'] + common + [dst]
        subprocess.run(cmd, capture_output=True, timeout=900, check=True)
        at = f'{min(1.0, max(0.0, info["duration"] / 2)):.2f}'
        subprocess.run(['ffmpeg', '-y', '-v', 'error', '-ss', at, '-i', dst, '-frames:v', '1',
                        '-vf', f'scale={THUMB_SIDE}:-2', poster], capture_output=True, timeout=120, check=True)
        out = _probe(dst)
        old = media.file.name
        with open(dst, 'rb') as f:
            media.file.save('video.mp4', File(f), save=False)
        if os.path.exists(poster):
            if media.thumb:
                media.thumb.delete(save=False)
            with open(poster, 'rb') as f:
                media.thumb.save('poster.jpg', File(f), save=False)
        media.width, media.height, media.duration = out['width'], out['height'], out['duration']
        media.status, media.status_at = Media.READY, timezone.now()
        media.save()
        if old != media.file.name:
            media.file.storage.delete(old)
        return media
    except Exception as problem:   # a broken video must not break the feed
        logger.warning('Video %s could not be prepared: %s', media_id, problem)
        Media.objects.filter(pk=media_id).update(status=Media.FAILED, status_at=timezone.now())
        return None
    finally:
        shutil.rmtree(work, ignore_errors=True)


def requeue_stale(tenant):
    """Start again on videos whose preparation stopped (e.g. the server restarted half-way)."""
    stale = Media.objects.filter(post__tenant=tenant, status=Media.PROCESSING,
                                 status_at__lt=timezone.now() - timedelta(minutes=STALE_MINUTES))
    for media_id in list(stale.values_list('id', flat=True)[:5]):
        if Media.objects.filter(pk=media_id, status=Media.PROCESSING).update(status_at=timezone.now()):
            start(media_id)
