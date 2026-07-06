"""
飞书个人助手 — 灵感记录管理
飞书端口述灵感 → 云端 JSON 存储 → PC 连接时自动同步到桌面 灵感记录.md
支持智能回顾提醒：录入灵感时自动分析是否需要回顾 + 定时推送。

灵感类型:
  - idea:    创意想法
  - note:    日常笔记
  - link:    链接/资源
  - task:    待办衍生（灵感转待办）
"""

import json
import os
import threading
import logging
from datetime import datetime
from typing import Optional

from config import INSPIRATION_FILE, TZ

log = logging.getLogger("agent.inspiration")

_lock = threading.Lock()


# ============================================================
# 数据存取
# ============================================================

def _read_all() -> list[dict]:
    """读取全部灵感记录。"""
    with _lock:
        try:
            with open(INSPIRATION_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, list) else []
        except (FileNotFoundError, json.JSONDecodeError):
            return []


def _write_all(items: list[dict]) -> None:
    """写入全部灵感记录。"""
    with _lock:
        os.makedirs(os.path.dirname(INSPIRATION_FILE), exist_ok=True)
        with open(INSPIRATION_FILE, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)


# ============================================================
# CRUD
# ============================================================

def add_inspiration(content: str, insp_type: str = "idea",
                    source: str = "feishu", chat_id: str = "") -> dict:
    """
    添加一条灵感。

    Args:
        content:   灵感内容
        insp_type: idea / note / link / task
        source:    来源
        chat_id:   飞书会话 ID（用于回顾提醒推送）

    Returns:
        新创建的灵感 dict
    """
    items = _read_all()

    # 生成 ID
    new_id = max((it.get("id", 0) for it in items), default=0) + 1

    insp = {
        "id": new_id,
        "content": content.strip(),
        "type": insp_type,
        "source": source,
        "created_at": datetime.now(TZ).isoformat(),
        "synced_to_pc": False,
        # 智能回顾提醒字段（后台 Claude 分析后填充）
        "review_at": None,
        "review_reason": None,
        "chat_id": chat_id,
    }
    items.append(insp)
    _write_all(items)
    log.info("灵感 #%d [%s]: %s", new_id, insp_type, content[:80])
    return insp


def list_inspirations(limit: int = 20, include_synced: bool = True) -> list[dict]:
    """列出灵感，按时间倒序。"""
    items = _read_all()
    if not include_synced:
        items = [it for it in items if not it.get("synced_to_pc")]
    items.sort(key=lambda it: it.get("id", 0), reverse=True)
    return items[:limit]


def delete_inspiration(insp_id: int) -> Optional[dict]:
    """删除指定灵感。"""
    items = _read_all()
    for i, it in enumerate(items):
        if it.get("id") == insp_id:
            removed = items.pop(i)
            _write_all(items)
            log.info("灵感删除 #%d: %s", insp_id, removed.get("content", "")[:60])
            return removed
    return None


def search_inspirations(query: str) -> list[dict]:
    """搜索灵感（关键词包含匹配）。"""
    items = _read_all()
    q = query.lower()
    return [it for it in items if q in it.get("content", "").lower()]


def mark_synced(insp_ids: list[int]) -> None:
    """标记灵感已同步到 PC。"""
    items = _read_all()
    for it in items:
        if it.get("id") in insp_ids:
            it["synced_to_pc"] = True
    _write_all(items)


def get_unsynced() -> list[dict]:
    """获取尚未同步到 PC 的灵感。"""
    items = _read_all()
    return [it for it in items if not it.get("synced_to_pc", False)]


def count() -> int:
    return len(_read_all())


# ============================================================
# 智能回顾提醒
# ============================================================

def set_inspiration_review(insp_id: int, review_info: dict) -> bool:
    """
    为指定灵感设置回顾提醒信息。

    Args:
        insp_id:     灵感 ID
        review_info: {"review_at": "2026-...", "review_reason": "...", "review_cycle": null}

    Returns:
        是否成功
    """
    items = _read_all()
    for it in items:
        if it.get("id") == insp_id:
            it["review_at"] = review_info.get("review_at")
            it["review_reason"] = review_info.get("reason") or review_info.get("review_reason")
            it["review_cycle"] = review_info.get("review_cycle")
            _write_all(items)
            log.info("灵感 #%d 已设置回顾提醒: %s", insp_id, it["review_at"])
            return True
    return False


def get_due_for_review() -> list[dict]:
    """
    获取所有 review_at 已到期（≤ 当前时间）且未标记完成的灵感。
    用于定时提醒检查。
    """
    items = _read_all()
    due = []
    for it in items:
        review_at = it.get("review_at")
        if not review_at:
            continue
        # 已 review 过的标记（review_at 被清除 = None）跳过
        if it.get("reviewed"):
            continue
        due.append(it)
    return due


def mark_reviewed(insp_id: int) -> bool:
    """
    标记灵感已回顾，清除 review_at 防止重复提醒。
    """
    items = _read_all()
    for it in items:
        if it.get("id") == insp_id:
            it["review_at"] = None
            it["reviewed"] = True
            _write_all(items)
            log.info("灵感 #%d 已标记回顾完成", insp_id)
            return True
    return False


# ============================================================
# 命令检测
# ============================================================

def is_inspiration_command(text: str) -> Optional[str]:
    """
    检测是否为灵感管理命令。

    Returns:
        'add'    — 添加灵感
        'list'   — 查看灵感列表
        'delete' — 删除灵感
        None     — 非灵感命令
    """
    t = text.strip()

    # 添加灵感
    add_prefixes = ["灵感", "想法", "创意", "灵光", "idea", "inspiration"]
    for p in add_prefixes:
        if t.startswith(p):
            body = t[len(p):].strip()
            if body and len(body) > 1:
                return "add"
            if not body:
                return "list"

    # 删除灵感
    if any(kw in t for kw in ["删除灵感", "灵感 删除", "灵感删除"]):
        return "delete"

    # 列表/整理
    if any(kw in t for kw in ["灵感列表", "我的灵感", "查看灵感", "整理灵感"]):
        return "list"

    return None
