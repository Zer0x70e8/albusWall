#!/usr/bin/env python3
"""
EXIF / 元数据解析工具 (函数式)
支持图片 (需 Pillow) 和视频 (需 ffprobe)
"""

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, TypedDict

# ---------- 依赖检查 ----------
try:
    # noinspection PyUnusedImports
    from PIL import Image, ExifTags
    HAS_PILLOW = True
except ImportError:
    HAS_PILLOW = False

def _has_ffprobe() -> bool:
    # noinspection PyBroadException
    try:
        subprocess.run(
            ["ffprobe", "-version"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True
        )
        return True
    except Exception:
        return False

HAS_FFPROBE = _has_ffprobe()

# ---------- 返回类型定义 ----------
class FileInfo(TypedDict):
    name: str
    size: int
    type: str          # "image" | "video" | "unknown"

class ParseResult(TypedDict):
    file: FileInfo
    metadata: Dict[str, Any]
    error: Optional[str]      # 明确标记可能为 None

# ---------- 工具函数 ----------
def _safe_value(val: Any) -> Any:
    if isinstance(val, bytes):
        # noinspection PyBroadException
        try:
            return val.decode("utf-8", errors="replace")
        except Exception:
            return val.hex()
    if isinstance(val, tuple):
        return list(val)
    return val

def _convert_gps(val: Any) -> Any:
    if isinstance(val, tuple) and len(val) == 2:
        return f"{val[0]} {val[1]}"
    return val

# ---------- 图片解析 ----------
def parse_image_exif(path: Path) -> Dict[str, Any]:
    if not HAS_PILLOW:
        raise RuntimeError("Pillow 未安装，无法解析图片。请执行: pip install Pillow")
    meta: Dict[str, Any] = {}
    with Image.open(path) as img:
        meta["format"] = img.format
        meta["mode"] = img.mode
        meta["width"] = img.width
        meta["height"] = img.height
        exif = img.getexif()
        if exif:
            for tid, val in exif.items():
                tag = ExifTags.TAGS.get(tid, tid)
                if tag == "GPSInfo":
                    gps: Dict[str, Any] = {}
                    for gid, gval in val.items():
                        gtag = ExifTags.GPSTAGS.get(gid, gid)
                        gps[gtag] = _convert_gps(gval)
                    meta["GPS"] = gps
                else:
                    meta[tag] = _safe_value(val)
    return meta

# ---------- 视频解析 ----------
def parse_video_meta(path: Path) -> Dict[str, Any]:
    if not HAS_FFPROBE:
        raise RuntimeError("未找到 ffprobe，请安装 FFmpeg 并确保可执行文件在 PATH 中")
    cmd = [
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-show_format", "-show_streams", str(path)
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe 错误: {proc.stderr.strip()}")
    try:
        probe = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise RuntimeError("ffprobe 返回无效 JSON")
    meta: Dict[str, Any] = {}
    fmt = probe.get("format", {})
    meta["format_name"] = fmt.get("format_name")
    meta["duration"] = fmt.get("duration")
    meta["bit_rate"] = fmt.get("bit_rate")
    meta["tags"] = fmt.get("tags", {})
    video = None
    audio: List[Dict[str, Any]] = []
    for st in probe.get("streams", []):
        ctype = st.get("codec_type")
        info: Dict[str, Any] = {
            "index": st.get("index"),
            "codec_name": st.get("codec_name"),
            "codec_long_name": st.get("codec_long_name"),
            "tags": st.get("tags", {})
        }
        if ctype == "video":
            info.update({
                "width": st.get("width"),
                "height": st.get("height"),
                "pix_fmt": st.get("pix_fmt"),
                "r_frame_rate": st.get("r_frame_rate"),
                "avg_frame_rate": st.get("avg_frame_rate")
            })
            video = info
        elif ctype == "audio":
            info.update({
                "sample_rate": st.get("sample_rate"),
                "channels": st.get("channels"),
                "channel_layout": st.get("channel_layout")
            })
            audio.append(info)
    if video:
        meta["video"] = video
    if audio:
        meta["audio"] = audio
    return meta

# ---------- 统一入口 ----------
SUPPORTED_IMAGE_EXT = {'.jpg', '.jpeg', '.png', '.tiff', '.bmp', '.gif', '.webp'}
SUPPORTED_VIDEO_EXT = {'.mp4', '.mov', '.avi', '.mkv', '.webm', '.flv', '.wmv', '.m4v'}

def parse_exif(file_path: str) -> ParseResult:
    """
    解析图片或视频的元数据。
    返回标准化的 ParseResult 字典，error 字段为 None 时表示成功。
    """
    path = Path(file_path)
    if not path.exists():
        return ParseResult(
            file=FileInfo(name=path.name, size=0, type="unknown"),
            metadata={},
            error="文件不存在"
        )

    fsize = path.stat().st_size
    suffix = path.suffix.lower()
    file_info = FileInfo(name=path.name, size=fsize, type="unknown")

    try:
        if suffix in SUPPORTED_IMAGE_EXT:
            file_info["type"] = "image"
            metadata = parse_image_exif(path)
        elif suffix in SUPPORTED_VIDEO_EXT:
            file_info["type"] = "video"
            metadata = parse_video_meta(path)
        else:
            return ParseResult(
                file=file_info,
                metadata={},
                error=f"不支持的文件类型: {suffix}"
            )
        return ParseResult(file=file_info, metadata=metadata, error=None)
    except Exception as e:
        return ParseResult(file=file_info, metadata={}, error=str(e))

# ---------- 命令行接口 ----------
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python exif_util.py <文件路径>")
        sys.exit(1)
    data = parse_exif(sys.argv[1])
    print(json.dumps(data, indent=2, ensure_ascii=False, default=str))
