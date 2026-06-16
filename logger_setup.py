"""
飞书个人助手 — 日志模块
同时输出到控制台和文件（自动轮转）。
"""

import logging
import logging.handlers
import os
import sys
from config import LOG_FILE, LOG_MAX_BYTES, LOG_BACKUP_COUNT

_log_initialized = False


def setup_logging(name: str = "agent") -> logging.Logger:
    """配置并返回 logger 实例，幂等（多次调用只初始化一次）。"""
    global _log_initialized

    logger = logging.getLogger(name)

    if _log_initialized:
        return logger

    logger.setLevel(logging.DEBUG)

    # --- 格式 ---
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)-7s] %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # --- 控制台 ---
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(fmt)
    logger.addHandler(console)

    # --- 文件（轮转） ---
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        LOG_FILE,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    _log_initialized = True
    return logger
