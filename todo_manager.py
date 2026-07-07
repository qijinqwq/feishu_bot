"""
飞书个人助手 — 待办管理引擎
JSON 文件持久化 + APScheduler 定时提醒 + 智能重复周期。
"""

import json
import os
import threading
import time
import logging
from datetime import datetime, date, timedelta
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.date import DateTrigger

from config import TODO_FILE, TIMEZONE, TZ, REMINDER_CHECK_SECONDS
from llm_bridge import call_claude

log = logging.getLogger("agent.todo")

# ============================================================
# 重复周期常量
# ============================================================

VALID_REPEATS = {None, "daily", "weekly", "monthly", "yearly", "weekdays"}


def _advance_reminder(remind_at_str: str, repeat: str) -> Optional[str]:
    """根据重复周期计算下一次提醒时间。返回 ISO 字符串或 None。"""
    if repeat not in VALID_REPEATS or repeat is None:
        return None

    try:
        dt = datetime.fromisoformat(remind_at_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ)
    except (ValueError, TypeError):
        return None

    if repeat == "daily":
        return (dt + timedelta(days=1)).isoformat()
    elif repeat == "weekly":
        return (dt + timedelta(days=7)).isoformat()
    elif repeat == "monthly":
        # 安全跨月：保持同一天，溢出则取月末
        import calendar as _cal
        y, m = dt.year, dt.month
        if m == 12:
            y, m = y + 1, 1
        else:
            m += 1
        last_day = _cal.monthrange(y, m)[1]
        d = min(dt.day, last_day)
        return dt.replace(year=y, month=m, day=d).isoformat()
    elif repeat == "yearly":
        return dt.replace(year=dt.year + 1).isoformat()
    elif repeat == "weekdays":
        # 跳到下一个工作日（周一~周五），跳过周末
        next_dt = dt + timedelta(days=1)
        while next_dt.weekday() >= 5:  # 5=周六 6=周日
            next_dt += timedelta(days=1)
        return next_dt.isoformat()

    return None


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

def add_todo(content: str, remind_at: Optional[str], chat_id: str,
             repeat: Optional[str] = None) -> dict:
    """
    添加一条待办。

    Args:
        content:   待办内容
        remind_at: 提醒时间，ISO 格式如 "2026-06-16T09:00:00"，可为 None
        chat_id:   飞书会话 ID（用于回发提醒）
        repeat:    重复周期: None/daily/weekly/monthly/yearly/weekdays

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
        "repeat": repeat if repeat in VALID_REPEATS else None,
        "status": "pending",
        "created_at": datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"),
        "chat_id": chat_id,
    }
    todos.append(todo)
    _write_todos(todos)

    repeat_label = {"daily": " 🔁每天", "weekly": " 🔁每周", "monthly": " 🔁每月",
                    "yearly": " 🔁每年", "weekdays": " 🔁工作日"}
    label = repeat_label.get(repeat, "")
    log.info("新增待办 #%d: %s (提醒: %s%s)", new_id, content, remind_at or "无", label)
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
            t["repeat"] = None  # 完成后停止重复
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
# 自然语言解析（调用 Claude）— 含智能重复周期检测
# ============================================================

def parse_todo_from_msg(user_msg: str) -> Optional[dict]:
    """
    用 LLM 从自然语言中提取待办信息、提醒时间 和 重复周期。

    Returns:
        {"content": "...", "remind_at": "2026-06-16T09:00:00", "repeat": "daily"}
        或 None。若未指定时间则 remind_at 为 null，未指定周期则 repeat 为 null。
    """
    today_str = datetime.now(TZ).strftime("%Y-%m-%d")
    weekday_str = datetime.now(TZ).strftime("%A")

    prompt = (
        "你是一个智能待办提取器。从用户消息中提取待办信息，返回纯 JSON（不要 markdown 代码块）。\n"
        "\n"
        "规则:\n"
        "1. content — 待办内容（去除时间/频率描述后的纯事项）\n"
        "2. remind_at — 首次提醒时间，ISO 8601 如 \"2026-06-16T09:00:00\"；未指定则为 null\n"
        "3. repeat — 重复频率，只能是: null / \"daily\" / \"weekly\" / \"monthly\" / \"yearly\" / \"weekdays\"\n"
        "\n"
        "重复频率判断指南:\n"
        "- \"每天/每日/天天\" → repeat:\"daily\"\n"
        "- \"每周/每个星期\" + 具体星期几 → repeat:\"weekly\"（如\"每周五\"→每周五）\n"
        "- \"每月/每个月\" + 日期 → repeat:\"monthly\"（如\"每月15号交房租\"→每月15号）\n"
        "- \"每年/年年\" → repeat:\"yearly\"\n"
        "- \"工作日/上班日/周一到周五\" → repeat:\"weekdays\"\n"
        "- 没有周期词（如\"明天上午9点开会\"）→ repeat:null\n"
        "- \"每隔N天\" → repeat:\"daily\"\n"
        "- \"每隔N周\" → repeat:\"weekly\"\n"
        "\n"
        "时间推理:\n"
        "- 如果指定的时间在今天已经过去，则设为明天同一时间\n"
        "- \"下午3点\" 且没说明天 → 默认今天 15:00（如果已过则明天）\n"
        "- \"下周一下午2点\" → 推理出具体日期\n"
        "- 提醒时间统一用未来时间\n"
        "- 如果用户一次性说了多件事，只提取第一件\n"
        "\n"
        f"今天是 {today_str} ({weekday_str})。\n"
        "\n"
        f"用户消息: {user_msg}\n"
        "\n"
        '返回格式: {"content": "事项", "remind_at": "2026-...", "repeat": "daily"}\n'
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
        result = json.loads(raw)
        # 校验 repeat 值
        if result.get("repeat") not in VALID_REPEATS:
            result["repeat"] = None
        return result
    except (json.JSONDecodeError, IndexError) as e:
        log.warning("待办解析失败: %s | 原始输出: %s", e, raw[:200])
        return None


# ============================================================
# 灵感回顾分析（供 message_handler 调用）
# ============================================================

def analyze_inspiration_for_review(content: str, insp_type: str) -> Optional[dict]:
    """
    用 Claude 分析灵感是否需要设置回顾提醒。

    Returns:
        {"needs_review": bool, "review_at": "2026-...", "review_cycle": null|"weekly",
         "reason": "..."} 或 None（Claude 不可用时）
    """
    today_str = datetime.now(TZ).strftime("%Y-%m-%d")

    prompt = (
        "你是一个灵感分析助手。分析用户记录的灵感，判断是否需要设置回顾提醒。\n"
        "\n"
        "判断标准:\n"
        "- 创意/项目类灵感（游戏、应用、设计、写作）→ 需要回顾，1-7 天后\n"
        "- 纯备忘/信息记录（链接、笔记、已确定的事项）→ 通常不需要\n"
        "- 包含\"待办/任务/要做\"关键词 → 需要，1-2 天后\n"
        "- 灵感很简短/模糊 → 需要，3-5 天后以便用户充实\n"
        "\n"
        "规则:\n"
        "1. needs_review: true/false\n"
        "2. review_at: 建议回顾时间 ISO 8601，一般设晚上 20:00-21:00\n"
        "3. review_cycle: null（绝大多数灵感不需要周期回顾）\n"
        "4. reason: 一句话（≤30字）解释为什么这个时间\n"
        "\n"
        f"今天是 {today_str}。\n"
        "\n"
        f"灵感内容: {content}\n"
        f"灵感类型: {insp_type}\n"
        "\n"
        '返回格式: {"needs_review": true, "review_at": "2026-07-08T20:00:00", "review_cycle": null, "reason": "给这个创意2天沉淀时间再回顾"}\n'
        "JSON:"
    )

    try:
        raw = call_claude(prompt, timeout=20)
        log.debug("灵感回顾分析输出: %s", raw)

        if "```" in raw:
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()

        result = json.loads(raw)
        return result
    except Exception as e:
        log.warning("灵感回顾分析失败（非致命）: %s", e)
        return None


# ============================================================
# 定时提醒引擎
# ============================================================

# 全局调度器（由 agent.py 初始化）
_scheduler: Optional[BackgroundScheduler] = None

# 发送消息的回调（由 agent.py 注入）
_send_message_cb: Optional[callable] = None

# 灵感检查函数引用（延迟绑定，避免循环导入）
_inspiration_check_fn: Optional[callable] = None


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
    _scheduler.add_job(
        _check_inspiration_reviews,
        trigger="interval",
        seconds=REMINDER_CHECK_SECONDS * 2,  # 灵感回顾不用太频繁
        id="inspiration_review",
        name="灵感回顾检查",
        misfire_grace_time=60,
    )
    _scheduler.start()
    log.info("定时提醒引擎已启动（待办每 %ds + 灵感回顾每 %ds 检查）",
             REMINDER_CHECK_SECONDS, REMINDER_CHECK_SECONDS * 2)


def shutdown_reminder_engine():
    """停止定时提醒引擎。"""
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        log.info("定时提醒引擎已停止")


# 补发标记：首次检查时补发启动前 6 小时内错过的提醒
_catchup_done = False
_CATCHUP_WINDOW = 6 * 3600  # 6 小时内的错过提醒可补发


def _check_and_remind():
    """检查所有待办，发送到期提醒。对重复周期自动推进到下一次。

    首次运行时会补发最近 6 小时内因故障/重启错过的一次性提醒。
    """
    global _catchup_done

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
            if remind_dt.tzinfo is None:
                remind_dt = remind_dt.replace(tzinfo=TZ)
        except ValueError:
            continue

        delta = (remind_dt - now).total_seconds()
        repeat = t.get("repeat")

        # 正常窗口：±30 秒内触发
        in_window = abs(delta) <= REMINDER_CHECK_SECONDS / 2

        # 补发窗口：首次检查时，已过期且 6 小时内的一次性提醒
        is_catchup = (
            not _catchup_done
            and not repeat
            and delta < -REMINDER_CHECK_SECONDS / 2
            and -delta <= _CATCHUP_WINDOW
        )

        if not in_window and not is_catchup:
            continue

        chat_id = t.get("chat_id", "")

        # ── 构建提醒消息 ──
        repeat_labels = {
            "daily": "🔁 每天", "weekly": "🔁 每周", "monthly": "🔁 每月",
            "yearly": "🔁 每年", "weekdays": "🔁 工作日",
        }
        repeat_label = repeat_labels.get(repeat, "")

        overdue_tag = "⚠️ 错过提醒，补发: " if is_catchup else ""

        msg = (
            f"{overdue_tag}📌 **待办提醒**\n\n"
            f"📋 {t['content']}\n"
            f"🕐 原定时间: {remind_str}"
            + (f"\n{repeat_label}" if repeat_label else "")
            + f"\n📅 创建于: {t['created_at']}\n\n"
            f"回复「/待办 完成 {t['id']}」标记为已完成"
        )

        try:
            _send_message_cb(chat_id, msg)
            log.info("已发送提醒: #%d %s%s",
                     t["id"], "补发 " if is_catchup else "", t["content"])
        except Exception as exc:
            log.exception("发送提醒失败: #%d, %s", t["id"], exc)

        # ── 重复周期：推进到下一次 ──
        if repeat:
            next_time = _advance_reminder(remind_str, repeat)
            if next_time:
                t["remind_at"] = next_time
                log.info("重复待办 #%d 推进到 %s", t["id"], next_time)
            else:
                t["remind_at"] = None
        else:
            # 一次性提醒，触发后清除
            t["remind_at"] = None

    _write_todos(todos)

    if not _catchup_done:
        _catchup_done = True


def _check_inspiration_reviews():
    """检查灵感回顾提醒是否到期，发送推送。"""
    if _send_message_cb is None:
        return

    try:
        from inspiration_manager import get_due_for_review, mark_reviewed
    except ImportError:
        return

    now = datetime.now(TZ)
    due_items = get_due_for_review()

    for item in due_items:
        review_str = item.get("review_at", "")
        try:
            review_dt = datetime.fromisoformat(review_str)
            if review_dt.tzinfo is None:
                review_dt = review_dt.replace(tzinfo=TZ)
        except (ValueError, TypeError):
            continue

        # 到期即发（review_at ≤ now），不设精确窗口
        if review_dt > now:
            continue

        chat_id = item.get("chat_id", "")
        review_reason = item.get("review_reason", "该回顾一下这个灵感了~")

        msg = (
            f"💡 **灵感回顾提醒**\n\n"
            f"📝 {item['content'][:200]}\n"
            f"💬 {review_reason}\n"
            f"🕐 记录于: {item.get('created_at', '?')}\n"
            f"🏷️ 类型: {item.get('type', 'idea')}\n\n"
            f"回复「灵感 删除 {item['id']}」移除 | 继续记录新灵感~"
        )

        try:
            _send_message_cb(chat_id, msg)
            log.info("已发送灵感回顾提醒: #%d %s", item["id"], item["content"][:60])
        except Exception as exc:
            log.exception("发送灵感回顾提醒失败: #%d, %s", item["id"], exc)

        # 回顾后清除 review_at（不再重复提醒）
        mark_reviewed(item["id"])
