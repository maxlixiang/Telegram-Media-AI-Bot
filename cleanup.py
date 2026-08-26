import logging
import os
import re
import time


logger = logging.getLogger(__name__)

# 下载任务使用 media_<32位十六进制UUID> 作为前缀。严格限制文件名，避免误删
# telegram-bot-api 在共享目录中保存的其他工作文件。
MEDIA_FILE_PATTERN = re.compile(r"^media_[0-9a-f]{32}(?:$|[._-].*)", re.IGNORECASE)


def cleanup_task_files(filepath_base: str) -> tuple[int, int]:
    """删除单个下载任务产生的文件，返回（文件数，总字节数）。"""
    directory = os.path.dirname(filepath_base)
    prefix = os.path.basename(filepath_base)
    if not MEDIA_FILE_PATTERN.fullmatch(prefix):
        logger.warning("拒绝清理不符合任务命名规则的路径: %s", filepath_base)
        return 0, 0

    return _remove_matching_files(directory, prefix=prefix)


def cleanup_stale_media_files(directory: str, max_age_seconds: int) -> tuple[int, int]:
    """删除共享目录中超过保留时间的下载残留文件。"""
    if max_age_seconds < 0:
        raise ValueError("max_age_seconds 不能为负数")

    cutoff = time.time() - max_age_seconds
    return _remove_matching_files(directory, older_than=cutoff)


def _remove_matching_files(
    directory: str,
    *,
    prefix: str | None = None,
    older_than: float | None = None,
) -> tuple[int, int]:
    removed_count = 0
    removed_bytes = 0

    try:
        entries = os.scandir(directory)
    except FileNotFoundError:
        return 0, 0
    except OSError:
        logger.exception("无法扫描临时媒体目录: %s", directory)
        return 0, 0

    with entries:
        for entry in entries:
            if not MEDIA_FILE_PATTERN.fullmatch(entry.name):
                continue
            if prefix is not None and not (
                entry.name == prefix
                or entry.name.startswith(tuple(prefix + separator for separator in (".", "_", "-")))
            ):
                continue

            try:
                stat = entry.stat(follow_symlinks=False)
                if older_than is not None and stat.st_mtime > older_than:
                    continue
                if not entry.is_file(follow_symlinks=False) and not entry.is_symlink():
                    continue
                os.remove(entry.path)
                removed_count += 1
                removed_bytes += stat.st_size
            except FileNotFoundError:
                continue
            except OSError:
                logger.exception("临时媒体文件删除失败: %s", entry.path)

    return removed_count, removed_bytes
