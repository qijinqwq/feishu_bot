"""
飞书个人助手 — 待办管理引擎
JSON 文件持久化 + APScheduler 定时提醒。
"""

import json
import os
import threading
import time
import logging
from datetime import datetime, date
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.date import DateTrigger

from config import TODO_FILE, TIMEZONE, TZ, REMINDER_CHECK_SECONDS
from llm_bridge import call_claude

log = logging.getLogger("agent.todo")

# ============================================================
# 数据存取
# ============================================================

_todo_lock = threading.Lock()


def _read_todos() -> list[dict]:
    """读取全部待办（线程安全）。"""
    with _todo_lock:
        try:
            with open(TODO_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return []


def _write_todos(todos: list[dict]) -> None:
    """写入全部待办（线程安全）。"""
    with _todo_lock:
        os.makedirs(os.path.dirname(TODO_FILE), exist_ok=True)
        with open(TODO_FILE, "w", encoding="utf-8") as f:
            json.dump(todos, f, ensure_ascii=False, indent=2)


# ============================================================
# CRUD 操作
# ============================================================

def add_todo(content: str, remind_at: Optional[str], chat_id: str) -> dict:
    """
    添加一条待办。

    Args:
        content:   待办内容
        remind_at: 提醒时间，ISO 格式如 "2026-06-16T09:00:00"，可为 None
        chat_id:   飞书会话 ID（用于回发提醒）

    Returns:
        新创建的待办 dict
    """
    todos = _read_todos()

    # 生成 ID
    if todos:
        new_id = max(t["id"] for t in todos) + 1
    else:
        new_id = 1

    todo = {
        "id": new_id,
        "content": content,
        "remind_at": remind_at,
        "status": "pending",
        "created_at": datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"),
        "chat_id": chat_id,
    }
    todos.append(todo)
    _write_todos(todos)

    log.info("新增待办 #%d: %s (提醒: %s)", new_id, content, remind_at or "无")
    return todo


def list_todos(chat_id: Optional[str] = None) -> list[dict]:
    """
    列出待办。

    Args:
        chat_id: 若提供则只返回该会话下的待办；否则返回全部
    """
    todos = _read_todos()
    if chat_id:
        todos = [t for t in todos if t.get("chat_id") == chat_id]
    # 排序：未完成在前，按创建时间倒序
    todos.sort(key=lambda t: (t["status"] != "pending", -t["id"]))
    return todos


def complete_todo(todo_id: int) -> Optional[dict]:
    """将指定待办标记为完成。返回更新后的 dict，若不存在则返回 None。"""
    todos = _read_todos()
    for t in todos:
        if t["id"] == todo_id:
            t["status"] = "done"
            _write_todos(todos)
            log.info("完成待办 #%d: %s", todo_id, t["content"])
            return t
    return None


def delete_todo(todo_id: int) -> Optional[dict]:
    """删除指定待办。返回被删除的 dict，若不存在则返回 None。"""
    todos = _read_todos()
    for i, t in enumerate(todos):
        if t["id"] == todo_id:
            removed = todos.pop(i)
            _write_todos(todos)
            log.info("删除待办 #%d: %s", todo_id, removed["content"])
            return removed
    return None


# ============================================================
# 自然语言解析（调用 Claude）
# ============================================================

def parse_todo_from_msg(user_msg: str) -> Optional[dict]:
    """
    用 LLM 从自然语言中提取待办信息和提醒时间。

    Returns:
        {"content": "...", "remind_at": "2026-06-16T09:00:00"} 或 None
        若未指定时间则 remind_at 为 null。
    """
    today_str = datetime.now(TZ).strftime("%Y-%m-%d")

    prompt = (
        "你是一个待办提取器。从用户消息中提取待办事项，返回纯 JSON（不要 markdown 代码块）。\n"
        "\n"
        "规则:\n"
        "1. content 是待办内容\n"
        "2. remind_at 是提醒时间，ISO 8601 格式如 \"2026-06-16T09:00:00\"\n"
        "3. 如果用户没有指定时间，remind_at 为 null\n"
        "4. \"明天上午9点\" → \"{date}T09:00:00\"（date 需推理）\n"
        "5. \"下午3点\" 且没说明天 → 默认今天 \"{date}T15:00:00\"\n"
        "6. \"下周一下午2点\" → 推理出具体日期\n"
        "7. 如果用户一次性说了多件事，只提取第一件\n"
        "\n"
        f"今天的日期是 {today_str}。\n"
        "\n"
        f"用户消息: {user_msg}\n"
        "\n"
        '返回格式: {"content": "...", "remind_at": "..."}\n'
        "JSON:"
    )

    raw = call_claude(prompt, timeout=30)
    log.debug("LLM 待办解析原始输出: %s", raw)

    # 尝试解析 JSON
    try:
        # 剔除可能包裹的 ```json ... ``` 标记
        if "```" in raw:
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
        return json.loads(raw)
    except (json.JSONDecodeError, IndexError) as e:
        log.warning("待办解析失败: %s | 原始输出: %s", e, raw[:200])
        return None


# ============================================================
# 定时提醒引擎
# ============================================================

# 全局调度器（由 agent.py 初始化）
_scheduler: Optional[BackgroundScheduler] = None

# 发送消息的回调（由 agent.py 注入）
_send_message_cb: Optional[callable] = None


def init_reminder_engine(send_message_callback):
    """
    启动定时提醒引擎。

    Args:
        send_message_callback: 函数 (chat_id, text) → None，用于发送飞书消息
    """
    global _scheduler, _send_message_cb
    _send_message_cb = send_message_callback

    if _scheduler is not None:
        return

    _scheduler = BackgroundScheduler(timezone=TIMEZONE)
    _scheduler.add_job(
        _check_and_remind,
        trigger="interval",
        seconds=REMINDER_CHECK_SECONDS,
        id="todo_reminder",
        name="待办提醒检查",
        misfire_grace_time=30,
    )
    _scheduler.start()
    log.info("定时提醒引擎已启动（每 %ds 检查一次）", REMINDER_CHECK_SECONDS)


def shutdown_reminder_engine():
    """停止定时提醒引擎。"""
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        log.info("定时提醒引擎已停止")


def _check_and_remind():
    """检查所有待办，发送到期提醒。"""
    if _send_message_cb is None:
        return

    now = datetime.now(TZ)
    todos = _read_todos()

    for t in todos:
        if t["status"] != "pending":
            continue
        remind_str = t.get("remind_at")
        if not remind_str:
            continue

        try:
            remind_dt = datetime.fromisoformat(remind_str)
        except ValueError:
            continue

        # 在提醒时间 ±30 秒窗口内触发
        delta = abs((remind_dt - now).total_seconds())
        if delta <= REMINDER_CHECK_SECONDS / 2:
            # 避免重复提醒：提醒后把 remind_at 置空
            t["remind_at"] = None
            _write_todos(todos)

            chat_id = t.get("chat_id", "")
            msg = (
                f"📌 **待办提醒**\n\n"
                f"📋 {t['content']}\n"
                f"🕐 预定时间: {remind_str}\n"
                f"📅 创建于: {t['created_at']}\n\n"
                f"回复「/待办 完成 {t['id']}」标记为已完成"
            )
            try:
                _send_message_cb(chat_id, msg)
                log.info("已发送提醒: #%d %s", t["id"], t["content"])
            except Exception as exc:
                log.exception("发送提醒失败: #%d, %s", t["id"], exc)
