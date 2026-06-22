"""
飞书个人助手 — 消息路由与处理 (云端版)
接收原始消息 → 意图分发 → 异步执行 → 回复。

核心原则：
- 3 秒 ACK 规则：耗时操作必须在异步线程中执行
- 显式命令同步返回，确保快速
- 文件操作走 pc_bridge → 物理机 Claude
- 通用对话走云端 Claude daemon
"""

import json
import logging
import threading
from typing import Optional

from todo_manager import (
    add_todo, list_todos, complete_todo, delete_todo,
    parse_todo_from_msg,
)
from llm_bridge import call_claude, quick_intent
from memory_manager import (
    has_memory_command, list_memories, add_memory, delete_memory,
    search_memories, memory_count,
)
from inspiration_manager import (
    is_inspiration_command, add_inspiration, list_inspirations,
    delete_inspiration, count as inspiration_count,
)
from pc_bridge import is_pc_online, send_to_pc
from config import TIMEZONE, TZ

log = logging.getLogger("agent.msg")

# ============================================================
# 异步工具
# ============================================================

_MAX_RESULT_CHARS = 4000


def _async_reply(send_fn, chat_id: str, task_fn):
    """
    先发送 ACK，再异步执行 task_fn，完成后回传结果。
    """

    def _worker():
        try:
            result = task_fn()
            if result:
                if len(result) > _MAX_RESULT_CHARS:
                    result = result[:_MAX_RESULT_CHARS] + "\n\n…（内容过长已截断）"
                send_fn(chat_id, result)
            else:
                send_fn(chat_id, "✅ 操作完成。")
        except Exception as exc:
            log.exception("异步任务执行失败")
            try:
                send_fn(chat_id, f"❌ 执行出错: {exc}")
            except Exception:
                pass

    send_fn(chat_id, "⏳ 处理中，请稍候…")

    t = threading.Thread(target=_worker, daemon=True, name="async-task")
    t.start()


# ============================================================
# 命令处理器
# ============================================================

def _cmd_help() -> str:
    return (
        "🤖 **飞书个人助手** 使用指南\n\n"
        "📋 **待办管理**\n"
        "`/待办` — 查看待办列表\n"
        "`/待办 添加 明天上午9点开会` — 添加待办（支持自然语言时间）\n"
        "`/待办 完成 3` — 标记第 3 条为已完成\n"
        "`/待办 删除 3` — 删除第 3 条\n\n"
        "💻 **文件操作**（需物理机开机 + pc_agent 运行）\n"
        "`/文件 把 D:/projects/readme.md 里的 xxx 改成 yyy`\n"
        "或直接发送含本地路径的自然语言指令\n\n"
        "💬 **通用对话**\n"
        "直接发送任意问题，由 Claude 回答\n\n"
        "🧠 **持久记忆**\n"
        "「记住 我喜欢喝咖啡」— 保存记忆\n"
        "「你记得什么」— 查看所有记忆\n"
        "「忘了 咖啡」— 删除匹配的记忆\n\n"
        "💡 **灵感记录**\n"
        "「灵感 写一个关于猫的养成游戏」— 保存灵感\n"
        "「灵感」— 查看最近的灵感\n"
        "「灵感 删除 3」— 删除第 3 条\n\n"
        "🔧 **其他**\n"
        "`/帮助` — 显示本信息\n"
        "`/状态` — 查看机器人运行状态"
    )


def _add_todo_with_claude(item_text: str, chat_id: str, send_fn) -> str:
    """用 Claude 解析待办时间 + 内容，然后存入待办列表。"""
    from todo_manager import parse_todo_from_msg as parse_fn
    parsed = parse_fn(item_text)
    if parsed:
        todo = add_todo(parsed["content"], parsed.get("remind_at"), chat_id)
        remind_info = f"\n⏰ 提醒时间: {parsed['remind_at']}" if parsed.get("remind_at") else ""
        return f"✅ 已添加待办 **#{todo['id']}**: {todo['content']}{remind_info}"
    else:
        todo = add_todo(item_text, None, chat_id)
        return f"✅ 已添加待办 **#{todo['id']}**: {todo['content']}\n（未识别到提醒时间，可后续修改）"


def _cmd_todo(chat_id: str, text: str, send_fn=None) -> Optional[str]:
    """处理 /待办 命令。"""
    body = text.strip()
    for prefix in ["/待办", "/todo", "待办", "todo"]:
        if body.lower().startswith(prefix.lower()):
            body = body[len(prefix):].strip()
            break

    if not body or body == "":
        todos = list_todos(chat_id)
        if not todos:
            return "📋 待办列表为空。试试 `/待办 添加 你的事项`"
        lines = ["📋 **待办列表**\n"]
        for t in todos:
            icon = "✅" if t["status"] == "done" else "⬜"
            remind = f" ⏰ {t['remind_at']}" if t.get("remind_at") else ""
            lines.append(f"{icon} **#{t['id']}** {t['content']}{remind}")
        return "\n".join(lines)

    elif body.startswith("添加") or body.startswith("add"):
        item_text = body.replace("添加", "", 1).replace("add", "", 1).strip()
        if not item_text:
            return "❓ 请写明待办内容。例如: `/待办 添加 明天上午9点开会`"

        time_keywords = ["明天", "后天", "下周", "下个", "今天", "下午", "上午",
                         "晚上", "早上", "中午", "明早", "明晚", "周", "星期",
                         "点半", "点", "分钟后", "小时后", "号", "日"]
        needs_time_parse = any(kw in item_text for kw in time_keywords)

        if needs_time_parse:
            _async_reply(send_fn, chat_id,
                         lambda t=item_text, c=chat_id, sfn=send_fn: _add_todo_with_claude(t, c, sfn))
            return None

        todo = add_todo(item_text, None, chat_id)
        return f"✅ 已添加待办 **#{todo['id']}**: {todo['content']}\n（未设置提醒时间）"

    elif any(kw in body for kw in ["完成", "完成", "done", "做了", "搞定"]):
        try:
            tid = int("".join(c for c in body if c.isdigit()))
            result = complete_todo(tid)
            if result:
                return f"🎉 待办 **#{tid}** 已完成: {result['content']}"
            else:
                return f"❌ 未找到待办 #{tid}"
        except ValueError:
            return "❓ 请告诉我完成哪一条。例如: `/待办 完成 3`"

    elif any(kw in body for kw in ["删除", "删掉", "移除", "delete", "remove"]):
        try:
            tid = int("".join(c for c in body if c.isdigit()))
            result = delete_todo(tid)
            if result:
                return f"🗑️ 已删除待办 **#{tid}**: {result['content']}"
            else:
                return f"❌ 未找到待办 #{tid}"
        except ValueError:
            return "❓ 请告诉我删除哪一条。例如: `/待办 删除 3`"

    else:
        time_keywords = ["明天", "后天", "下周", "下个", "今天", "下午", "上午",
                         "晚上", "早上", "中午", "明早", "明晚", "周", "星期",
                         "点半", "点", "分钟后", "小时后", "号", "日"]
        needs_time_parse = any(kw in body for kw in time_keywords)

        if needs_time_parse and send_fn:
            _async_reply(send_fn, chat_id,
                         lambda t=body, c=chat_id, sfn=send_fn: _add_todo_with_claude(t, c, sfn))
            return None

        todo = add_todo(body, None, chat_id)
        return f"✅ 已添加待办 **#{todo['id']}**: {todo['content']}"


def _cmd_status() -> str:
    """返回机器人运行状态。"""
    from datetime import datetime
    now = datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")
    import platform
    pc_status = "🟢 在线" if is_pc_online() else "⚫ 离线"
    return (
        "🟢 **机器人运行中** (云端)\n\n"
        f"🕐 当前时间: {now}\n"
        f"☁️ 服务器: {platform.node()}\n"
        f"🌐 时区: {TIMEZONE}\n"
        f"🧠 持久记忆: {memory_count()} 条\n"
        f"💡 灵感记录: {inspiration_count()} 条\n"
        f"📋 待办: {len(list_todos())} 条\n"
        f"💻 物理机: {pc_status}"
    )


def _cmd_inspiration(chat_id: str, text: str) -> Optional[str]:
    """处理灵感管理命令。"""
    cmd = is_inspiration_command(text)

    if cmd == "list":
        items = list_inspirations(limit=15)
        if not items:
            return "💡 还没有灵感记录。\n对我说「灵感 你的想法」来记录！"
        lines = [f"💡 **灵感记录**（最近 {len(items)} 条）\n"]
        for it in items:
            type_icon = {"idea": "💡", "note": "📝", "link": "🔗", "task": "📋"}.get(
                it.get("type", "idea"), "💡")
            synced = " ✅" if it.get("synced_to_pc") else ""
            lines.append(f"{type_icon} **#{it['id']}** {it['content'][:100]}{synced}")
        return "\n".join(lines)

    if cmd == "add":
        # 提取灵感内容
        content = text
        for prefix in ["灵感", "想法", "创意", "灵光", "idea", "inspiration"]:
            if content.startswith(prefix):
                content = content[len(prefix):].strip().lstrip("，,：: ")
                break

        if content and len(content) > 1:
            # 判断类型
            insp_type = "idea"
            if any(kw in content for kw in ["http://", "https://", ".com", ".cn"]):
                insp_type = "link"
            elif any(kw in content for kw in ["待办", "任务", "要做", "完成"]):
                insp_type = "task"
            insp = add_inspiration(content, insp_type=insp_type)
            type_label = {"idea": "💡 灵感", "note": "📝 笔记", "link": "🔗 链接", "task": "📋 任务"}
            return f"{type_label.get(insp_type, '💡')} 已记录 **#{insp['id']}**: {insp['content'][:120]}"
        return "❓ 请告诉我具体要记录什么。例如: `灵感 写一个关于猫的养成游戏`"

    if cmd == "delete":
        try:
            tid = int("".join(c for c in text if c.isdigit()))
            result = delete_inspiration(tid)
            if result:
                return f"🗑️ 已删除灵感 **#{tid}**: {result['content'][:80]}"
            else:
                return f"❌ 未找到灵感 #{tid}"
        except ValueError:
            return "❓ 请告诉我要删除哪一条。例如: `灵感 删除 3`"

    return None


def _cmd_memory(chat_id: str, text: str, send_fn) -> Optional[str]:
    """处理记忆管理命令。"""
    cmd = has_memory_command(text)

    if cmd == "recall":
        memories = list_memories()
        if not memories:
            return "🧠 我还没有关于你的持久记忆。\n对我说「记住 我喜欢喝咖啡」来添加记忆！"
        lines = [f"🧠 **持久记忆**（共 {len(memories)} 条）\n"]
        for m in memories:
            type_icon = {"user": "👤", "preference": "⭐", "project": "📁", "fact": "📝"}.get(
                m.get("type", "fact"), "📝")
            lines.append(f"{type_icon} **#{m['id']}** {m['content']}")
        return "\n".join(lines)

    if cmd == "remember":
        content = text
        for prefix in ["记住", "记下", "备忘", "remember", "save this", "store this"]:
            if content.lower().startswith(prefix.lower()):
                content = content[len(prefix):].strip().lstrip("，,：: ")
                break
        if content and len(content) > 1:
            mem_type = "fact"
            if any(kw in content for kw in ["喜欢", "偏好", "习惯", "不喜欢", "讨厌"]):
                mem_type = "preference"
            elif any(kw in content for kw in ["我叫", "我是", "我的名字", "称呼", "名称"]):
                mem_type = "user"
            mem = add_memory(content, mem_type=mem_type, source=f"feishu:{chat_id[:12]}")
            return f"🧠 已记住: {mem['content']}"
        return "❓ 请告诉我具体要记住什么。例如: `记住 我喜欢喝咖啡`"

    if cmd == "forget":
        content = text
        for prefix in ["忘了", "删除记忆", "清除记忆", "忘记", "forget", "delete memory"]:
            if content.lower().startswith(prefix.lower()):
                content = content[len(prefix):].strip().lstrip("，,：: ")
                break
        if content:
            found = search_memories(content)
            if found:
                deleted = []
                for m in found[:3]:
                    dm = delete_memory(m["id"])
                    if dm:
                        deleted.append(dm["content"])
                if deleted:
                    return f"🗑️ 已删除记忆: {', '.join(deleted)}"
            return "❌ 未找到匹配的记忆。发送「你记得什么」查看所有记忆。"
        return "❓ 请告诉我具体要删除哪条记忆。例如: `忘了 咖啡`"

    return None


# ============================================================
# 辅助判断函数（不调 LLM，毫秒级）
# ============================================================

def _looks_like_local_file_op(text: str) -> bool:
    """判断是否为需要物理机执行的本地文件操作。"""
    text_lower = text.lower()
    local_paths = [
        "d:", "d:/", "d:\\",
        "c:", "c:/", "c:\\",
        "e:", "e:/", "e:\\",
    ]
    return any(kw in text_lower for kw in local_paths)


def _looks_like_file_op(text: str) -> bool:
    """快速判断是否为文件操作（关键词匹配，不调 LLM）。"""
    text_lower = text.lower()
    keywords = [
        "文件", "文件夹", "目录", "帮我写", "帮我改", "帮我查", "帮我做",
        "d:", "d:/", "d:\\", "c:", "c:/", "c:\\", "e:", "e:/", "e:\\",
        ".py", ".txt", ".md", ".json", ".js", ".ts", ".html", ".css",
        "运行", "执行", "代码", "命令",
    ]
    return any(kw in text_lower for kw in keywords)


def _looks_like_todo(text: str) -> bool:
    """快速判断是否为待办操作。"""
    return any(kw in text for kw in ["待办", "提醒我", "提醒", "日程", "/todo"])


def _is_greeting(text: str) -> bool:
    """检查是否为简短问候。"""
    greetings = {"你好", "在吗", "hello", "hi", "嗨", "哈喽", "hey", "早上好",
                 "下午好", "晚上好", "good morning", "good afternoon"}
    return text.strip().lower() in greetings or len(text.strip()) <= 2


def _quick_greeting(text: str) -> str:
    """对简短问候返回友好快速应答。"""
    t = text.strip().lower()
    if t in ("你好", "hello", "hi", "嗨", "哈喽", "hey"):
        return "👋 你好！有什么可以帮你的？\n发送 `/帮助` 查看功能列表。"
    if t in ("在吗",):
        return "🟢 在的！有什么需要？"
    if t in ("早上好", "下午好", "晚上好", "good morning", "good afternoon"):
        return f"😊 {text.strip()}！需要我做什么吗？"
    if len(text.strip()) <= 2:
        return f"👋 收到！想让我帮你做什么？发送 `/帮助` 查看功能。"
    return f"👋 你好！发送 `/帮助` 查看功能列表。"


# ============================================================
# 主路由
# ============================================================

def route_message(chat_id: str, text: str, send_fn) -> Optional[str]:
    """
    路由用户消息到对应处理器。

    设计原则（飞书 3 秒 ACK 规则）：
    - 显式命令 / 快速判断 → 同步返回（< 500ms）
    - 需要 LLM 判断 / Claude 调用 → 异步处理（先返回 None）

    Returns:
        同步可回复时返回 str；异步处理时返回 None。
    """
    text = text.strip()
    if not text:
        return None

    log.info("收到消息 [%s]: %s", chat_id[:12], text[:200])

    # ================================================================
    # 第一层：显式命令（同步，快速）
    # ================================================================

    if text.startswith("/帮助") or text.startswith("/help"):
        return _cmd_help()

    if text.startswith("/状态") or text.startswith("/status"):
        return _cmd_status()

    # 待办命令
    if (text.startswith("/待办") or text.startswith("/todo") or
            text.startswith("待办") or text.startswith("todo")):
        return _cmd_todo(chat_id, text, send_fn)

    # 文件命令 → 异步（需 PC 或云端 Claude）
    if text.startswith("/文件") or text.startswith("/file"):
        prompt = text.replace("/文件", "", 1).replace("/file", "", 1).strip()
        if not prompt:
            return "❓ 请描述文件操作内容。例如: `/文件 查看 D:/projects 下有哪些文件`"

        if _looks_like_local_file_op(prompt):
            # 需要物理机
            if is_pc_online():
                _async_reply(send_fn, chat_id, lambda p=prompt: send_to_pc(p) or "❌ PC 处理失败")
            else:
                return "⏸️ 你的电脑当前未开机或 pc_agent 未启动，文件操作暂不可用。\n待办、记忆、灵感、通用对话功能正常。"
        else:
            _async_reply(send_fn, chat_id, lambda p=prompt: call_claude(p))
        return None

    # 灵感命令 → 同步（必须在记忆检测之前，因为"灵感"前缀意图明确，
    # 避免被记忆模块的关键词（如"回忆"）误拦截）
    insp_cmd = is_inspiration_command(text)
    if insp_cmd in ("list", "add", "delete"):
        return _cmd_inspiration(chat_id, text)

    # 记忆召回 → 同步
    memory_cmd = has_memory_command(text)
    if memory_cmd == "recall":
        return _cmd_memory(chat_id, text, send_fn)

    # "整理灵感" → 异步 Claude
    if "整理灵感" in text or "总结灵感" in text:
        items = list_inspirations(limit=30)
        if not items:
            return "💡 还没有灵感记录。"
        insp_text = "\n".join(f"- #{it['id']} [{it['type']}] {it['content']}" for it in items)
        prompt = f"以下是用户的灵感记录，请用中文做一个简洁的总结归类（按主题分组，3-5 组），然后给出一些建议：\n\n{insp_text}"
        _async_reply(send_fn, chat_id, lambda p=prompt: call_claude(p))
        return None

    # ================================================================
    # 第二层：快速关键词判断（同步，保证 3 秒内 ACK）
    # ================================================================

    # 灵感和记忆的 add/delete → 同步
    if insp_cmd is not None:
        return _cmd_inspiration(chat_id, text)

    # 记忆保存/删除 → 同步
    if memory_cmd in ("remember", "forget"):
        return _cmd_memory(chat_id, text, send_fn)

    # 本地文件操作关键词 → 路由到物理机
    if _looks_like_local_file_op(text):
        if is_pc_online():
            _async_reply(send_fn, chat_id, lambda t=text: send_to_pc(t) or "❌ PC 处理失败，请重试。")
        else:
            _async_reply(send_fn, chat_id,
                         lambda t=text: "⏸️ 你的电脑当前未开机或 pc_agent 未启动。\n\n"
                                        "涉及本地文件（D:/、C:/ 等）的操作需要物理机在线。\n"
                                        "待办、记忆、灵感、通用对话功能不受影响。")
        return None

    # 文件操作关键词（无本地路径）→ 云端 Claude
    if _looks_like_file_op(text):
        _async_reply(send_fn, chat_id, lambda t=text: call_claude(t))
        return None

    # 待办关键词 → 同步处理
    if _looks_like_todo(text):
        return _cmd_todo(chat_id, text, send_fn)

    # 简短问候 → 同步友好回复
    if _is_greeting(text):
        return _quick_greeting(text)

    # ================================================================
    # 第三层：兜底 → 云端 Claude daemon（热启动）
    # ================================================================
    _async_reply(send_fn, chat_id, lambda t=text: call_claude(t))
    return None
