"""
飞书个人助手 — Claude CLI 桥接层
通过 claude_daemon 常驻进程实现热启动，零冷启动开销。
每次调用自动注入持久记忆上下文。
"""

import logging
import threading
from datetime import datetime

from config import CLAUDE_DAEMON_TIMEOUT, TZ
from claude_daemon import get_daemon
from memory_manager import (
    get_memory_context, has_memory_command,
    add_memory, search_memories, delete_memory,
)

log = logging.getLogger("agent.llm")


def call_claude(prompt: str, timeout: float = None) -> str:
    """
    通过 daemon 常驻进程调用 Claude（热启动，无进程创建开销）。

    Args:
        prompt:  发送给 Claude 的提示词
        timeout: 超时秒数，默认 CLAUDE_DAEMON_TIMEOUT

    Returns:
        Claude 的回复文本；若 daemon 未启动或超时则返回错误描述。
    """
    daemon = get_daemon()
    if daemon is None:
        return "❌ Claude 进程未启动，请联系管理员。"

    if timeout is None:
        timeout = CLAUDE_DAEMON_TIMEOUT

    # 注入当前时间 + 持久记忆（轻量前缀，不膨胀 prompt）
    now_str = datetime.now(TZ).strftime("%Y年%m月%d日 %H:%M:%S")
    memory_ctx = get_memory_context(max_chars=800)

    parts = []
    if memory_ctx:
        parts.append(memory_ctx)
    parts.append(
        f"[当前时间: {now_str}] "
        "[你是菲洛的个人助手，可管理待办、操作文件和回答问题。简洁中文回复。]"
    )
    parts.append(prompt)

    full_prompt = "\n".join(parts)

    result = daemon.send(full_prompt, timeout=timeout)

    # 轻量记忆钩子：用关键词检测，不额外调 LLM
    _maybe_extract_memory(prompt)

    return result


def call_claude_async(prompt: str, callback, timeout: float = None):
    """
    异步调用 Claude（后台线程），完成后调用 callback(result: str)。
    用于避免阻塞飞书 WebSocket 事件回调线程（3 秒 ACK 规则）。
    """
    if timeout is None:
        timeout = CLAUDE_DAEMON_TIMEOUT

    def _worker():
        result = call_claude(prompt, timeout)
        try:
            callback(result)
        except Exception as exc:
            log.exception("Async callback 出错: %s", exc)

    t = threading.Thread(target=_worker, daemon=True, name="claude-async")
    t.start()
    return t


def quick_intent(user_msg: str) -> str:
    """
    纯关键词意图判断（不调 LLM，毫秒级）。

    Returns:
        'todo_add' | 'todo_list' | 'todo_done' | 'todo_delete'
        | 'file_op' | 'chat' | 'help'
    """
    msg = user_msg

    # 待办相关
    if any(kw in msg for kw in ["待办", "todo", "提醒", "日程", "任务"]):
        if any(kw in msg for kw in ["添加", "加", "新建", "新增", "创建"]):
            return "todo_add"
        if any(kw in msg for kw in ["完成", "done", "做了", "搞定"]):
            return "todo_done"
        if any(kw in msg for kw in ["删除", "删掉", "移除"]):
            return "todo_delete"
        return "todo_list"

    # 帮助
    if any(kw in msg for kw in ["帮助", "help", "怎么用", "功能"]):
        return "help"

    # 文件操作
    if any(kw in msg.lower() for kw in [
        "文件", "文件夹", "目录", "写", "改", "查", "运行", "代码",
        "d:", "c:", "e:", "d:/", "c:/", "e:/",
        "d:\\", "c:\\", "e:\\", ".py", ".txt", ".md", ".json",
    ]):
        return "file_op"

    return "chat"


# ============================================================
# 内部工具
# ============================================================


def _maybe_extract_memory(user_msg: str) -> None:
    """
    从用户消息中提取记忆（仅关键词匹配，不调 LLM）。
    如果消息以"记住/记下/备忘"开头，提取后续内容存入持久记忆。
    """
    cmd = has_memory_command(user_msg)
    if cmd != "remember":
        return

    content = user_msg
    for prefix in ["记住", "记下", "备忘", "保存记忆", "添加记忆",
                    "remember", "save this", "store this"]:
        if content.lower().startswith(prefix.lower()):
            content = content[len(prefix):].strip().lstrip("，,：: ")
            break

    if content and len(content) > 1:
        mem_type = "fact"
        if any(kw in content for kw in ["喜欢", "偏好", "习惯", "不喜欢", "讨厌"]):
            mem_type = "preference"
        elif any(kw in content for kw in ["我叫", "我是", "我的名字", "称呼"]):
            mem_type = "user"
        mem = add_memory(content, mem_type=mem_type, source="feishu_chat")
        log.info("从消息提取记忆: %s", mem["content"][:80])
