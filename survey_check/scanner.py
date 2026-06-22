"""目录扫描引擎"""
import os
from datetime import datetime
from typing import List, Tuple

from .models import FileEntry, FileType
from .config import SurveyConfig


def scan_directory(directory: str, exts: List[str], file_type: FileType,
                 config: SurveyConfig) -> Tuple[List[FileEntry], List[str]]:
    """
    扫描指定目录下的文件

    Returns:
        (文件条目列表, 错误信息列表)
    """
    errors = []
    entries: List[FileEntry] = []

    if not directory:
        errors.append(f"目录路径为空")
        return entries, errors

    if not os.path.exists(directory):
        errors.append(f"目录不存在: {directory}")
        return entries, errors

    if not os.path.isdir(directory):
        errors.append(f"路径不是目录: {directory}")
        return entries, errors

    exts_lower = [ext.lower() for ext in exts]

    try:
        for filename in sorted(os.listdir(directory)):
            filepath = os.path.join(directory, filename)

            if os.path.isfile(filepath):
                _, ext = os.path.splitext(filename)
                if ext.lower() in exts_lower:
                    try:
                        stat = os.stat(filepath)
                        entry = FileEntry(
                            file_type=file_type,
                            path=filepath,
                            filename=filename,
                            size=stat.st_size,
                            modified=datetime.fromtimestamp(stat.st_mtime).isoformat(),
                        )
                        entries.append(entry)
                    except Exception as e:
                            errors.append(f"无法读取文件 {filepath}: {str(e)}")
    except Exception as e:
        errors.append(f"扫描目录 {directory} 时出错: {str(e)}")

    return entries, errors


def scan_all(config: SurveyConfig) -> Tuple[dict, List[str]]:
    """
    扫描所有配置的目录

    Returns:
        (扫描结果字典, 错误信息列表)
    """
    all_errors = []

    photos, photo_errors = scan_directory(
        config.photo_dir, config.photo_exts, FileType.PHOTO, config
    )
    all_errors.extend(photo_errors)

    tracks, track_errors = scan_directory(
        config.track_dir, config.track_exts, FileType.TRACK, config
    )
    all_errors.extend(track_errors)

    tables, table_errors = scan_directory(
        config.table_dir, config.table_exts, FileType.TABLE, config
    )
    all_errors.extend(table_errors)

    result = {
        "photos": photos,
        "tracks": tracks,
        "tables": tables,
    }

    return result, all_errors
