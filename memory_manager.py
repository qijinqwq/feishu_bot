"""
飞书个人助手 — 持久记忆管理
跨会话记忆：每次 Claude 调用时注入历史记忆上下文，
也可以通过自然语言让用户管理记忆（"记住..."、"忘了..."、"你记得什么"）。
"""

import json
import os
import threading
import logging
from datetime import datetime
from typing import Optional

from config import MEMORY_FILE, MAX_MEMORY_ITEMS, TZ

log = logging.getLogger("agent.memory")

_memory_lock = threading.Lock()

# ============================================================
# 数据存取
# ============================================================


def _read_memories() -> dict:
    """读取记忆存储，返回完整 dict。"""
    with _memory_lock:
        try:
            with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if "memories" not in data:
                    data["memories"] = []
                return data
        except (FileNotFoundError, json.JSONDecodeError):
            return {"memories": [], "summary": ""}


def _write_memories(data: dict) -> None:
    """写入记忆存储。"""
    with _memory_lock:
        os.makedirs(os.path.dirname(MEMORY_FILE), exist_ok=True)
        with open(MEMORY_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)


def _prune_memories(memories: list) -> list:
    """裁剪记忆条目，保留最重要的（优先保留 user 类型 + 最近的）。"""
    if len(memories) <= MAX_MEMORY_ITEMS:
        return memories

    # 按优先级排序：user > preference > project > fact，同类型按时间倒序
    type_rank = {"user": 0, "preference": 1, "project": 2, "fact": 3}
    sorted_mems = sorted(
        memories,
        key=lambda m: (type_rank.get(m.get("type", "fact"), 9), -(m.get("id", 0))),
    )
    return sorted_mems[:MAX_MEMORY_ITEMS]


# ============================================================
# 公共 API
# ============================================================


def get_memory_context(max_chars: int = 2000) -> str:
    """
    返回可注入 Claude prompt 的记忆摘要字符串。
    用于每次 Claude 调用前拼接上下文。
    """
    data = _read_memories()
    memories = data.get("memories", [])

    if not memories:
        return ""

    # 用已有的 summary，或自动生成
    summary = data.get("summary", "")
    if summary:
        lines = [f"[持久记忆摘要]\n{summary}\n\n[最近记忆条目]"]
    else:
        lines = ["[持久记忆 — 关于用户及偏好的已知信息]"]

    for m in memories[-20:]:  # 最近 20 条
        type_label = {"user": "👤", "preference": "⭐", "project": "📁", "fact": "📝"}.get(
            m.get("type", "fact"), "📝"
        )
        lines.append(f"{type_label} {m['content']}")

    context = "\n".join(lines)

    # 截断到 max_chars，找最后一个换行断开
    if len(context) > max_chars:
        context = context[:max_chars].rsplit("\n", 1)[0]
        context += "\n…（记忆已截断）"

    return context


def add_memory(content: str, mem_type: str = "fact", source: str = "chat") -> dict:
    """
    添加一条持久记忆。

    Args:
        content:  记忆内容，如 "用户叫菲洛"
        mem_type: 记忆类型 — user / preference / project / fact
        source:   来源标识

    Returns:
        新创建的 memory dict
    """
    data = _read_memories()
    memories = data["memories"]

    # 去重：检查是否已有完全相同的记忆
    for m in memories:
        if m.get("content", "").strip() == content.strip():
            # 更新时间和类型，不重复添加
            m["updated_at"] = datetime.now(TZ).isoformat()
            m["type"] = mem_type
            _write_memories(data)
            log.debug("记忆已存在，更新: %s", content[:60])
            return m

    # 生成 ID
    new_id = max((m.get("id", 0) for m in memories), default=0) + 1

    now = datetime.now(TZ).isoformat()
    mem = {
        "id": new_id,
        "content": content.strip(),
        "type": mem_type,
        "source": source,
        "created_at": now,
        "updated_at": now,
    }
    memories.append(mem)

    # 裁剪
    data["memories"] = _prune_memories(memories)

    _write_memories(data)
    log.info("新增记忆 #%d [%s]: %s", new_id, mem_type, content[:80])
    return mem


def list_memories(mem_type: Optional[str] = None) -> list[dict]:
    """
    列出所有记忆。

    Args:
        mem_type: 按类型筛选，None 表示全部
    """
    data = _read_memories()
    memories = data["memories"]
    if mem_type:
        memories = [m for m in memories if m.get("type") == mem_type]
    memories.sort(key=lambda m: m.get("id", 0), reverse=True)
    return memories


def delete_memory(mem_id: int) -> Optional[dict]:
    """按 ID 删除记忆。"""
    data = _read_memories()
    memories = data["memories"]
    for i, m in enumerate(memories):
        if m.get("id") == mem_id:
            removed = memories.pop(i)
            data["memories"] = memories
            _write_memories(data)
            log.info("删除记忆 #%d: %s", mem_id, removed.get("content", "")[:60])
            return removed
    return None


def search_memories(query: str) -> list[dict]:
    """关键词搜索记忆（简单包含匹配）。"""
    data = _read_memories()
    results = []
    q_lower = query.lower()
    for m in data["memories"]:
        if q_lower in m.get("content", "").lower():
            results.append(m)
    results.sort(key=lambda m: m.get("id", 0), reverse=True)
    return results


def update_summary(new_summary: str) -> None:
    """手动更新记忆摘要。"""
    data = _read_memories()
    data["summary"] = new_summary.strip()
    _write_memories(data)
    log.info("记忆摘要已更新 (%d 字符)", len(new_summary))


def rebuild_summary() -> str:
    """
    基于现有记忆条目自动重建摘要。
    把所有记忆拼接后返回（可由 Claude 进一步压缩）。
    """
    data = _read_memories()
    memories = data["memories"]
    if not memories:
        return "暂无持久记忆。"

    lines = []
    for m in memories:
        lines.append(f"- {m['content']}")
    return "\n".join(lines)


def memory_count() -> int:
    """返回记忆总数。"""
    return len(_read_memories().get("memories", []))


def has_memory_command(text: str) -> Optional[str]:
    """
    检测用户消息是否为记忆管理命令。

    Returns:
        'remember' — 用户想保存记忆
        'forget'  — 用户想删除记忆
        'recall'  — 用户询问记忆内容
        None      — 非记忆命令
    """
    text_lower = text.strip().lower()

    remember_patterns = [
        "记住", "记下", "备忘", "保存记忆", "添加记忆",
        "remember", "save this", "store this",
    ]
    for p in remember_patterns:
        if text_lower.startswith(p) or p in text_lower[:20]:
            return "remember"

    forget_patterns = [
        "忘了", "删除记忆", "清除记忆", "忘记",
        "forget", "delete memory", "remove memory",
    ]
    for p in forget_patterns:
        if text_lower.startswith(p) or p in text_lower[:20]:
            return "forget"

    recall_patterns = [
        "你记得什么", "你记得我", "记忆有哪些", "查看记忆", "记忆列表",
        "你有什么记忆", "你知道我", "你回忆一下", "你回忆",
        "帮我回忆", "你还记得", "你了解我",
        "what do you remember", "what do you know about me",
        "list memories", "show memories", "recall",
    ]
    for p in recall_patterns:
        if p in text_lower:
            return "recall"

    return None
