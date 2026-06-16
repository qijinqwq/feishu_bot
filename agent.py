"""
飞书个人助手 — 主入口 (云端版)
WebSocket 长连接 + 心跳 + 消息分发 + 待办提醒 + PC 桥接。
启动: python agent.py  (或 ./run.sh)
"""

import json
import sys
import os
import hashlib
import time

# 确保项目目录在 Python 路径中
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_DIR)

import lark_oapi as lark
from lark_oapi.ws import Client as WSClient
from lark_oapi.event.dispatcher_handler import EventDispatcherHandlerBuilder
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
)

from config import APP_ID, APP_SECRET
from logger_setup import setup_logging
from message_handler import route_message
from todo_manager import init_reminder_engine, shutdown_reminder_engine
from memory_manager import memory_count, list_memories
from inspiration_manager import count as inspiration_count
from claude_daemon import init_daemon, shutdown_daemon
from pc_bridge import start_bridge, stop_bridge, is_pc_online

log = setup_logging("agent")

# ============================================================
# 消息去重（防止飞书重推 + 3 秒 ACK 超时导致的重复处理）
# ============================================================

_seen_msg_ids: dict[str, float] = {}
_seen_fingerprints: dict[str, float] = {}

MSG_ID_TTL = 300
FINGERPRINT_TTL = 60
CLEANUP_INTERVAL = 100
_msg_counter = 0


def _msg_fingerprint(chat_id: str, text: str) -> str:
    raw = f"{chat_id}:{text[:200]}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _is_duplicate(msg_id: str, chat_id: str, text: str) -> bool:
    global _msg_counter
    now = time.time()

    if msg_id in _seen_msg_ids:
        if _seen_msg_ids[msg_id] > now:
            log.debug("重复消息（message_id）: %s", msg_id[:20])
            return True

    fp = _msg_fingerprint(chat_id, text)
    if fp in _seen_fingerprints:
        if _seen_fingerprints[fp] > now:
            log.debug("重复消息（内容指纹）: %s", fp)
            return True

    _seen_msg_ids[msg_id] = now + MSG_ID_TTL
    _seen_fingerprints[fp] = now + FINGERPRINT_TTL

    _msg_counter += 1
    if _msg_counter >= CLEANUP_INTERVAL:
        _cleanup_seen(now)
        _msg_counter = 0

    return False


def _cleanup_seen(now: float) -> None:
    expired_ids = [mid for mid, exp in _seen_msg_ids.items() if exp <= now]
    for mid in expired_ids:
        del _seen_msg_ids[mid]

    expired_fps = [fp for fp, exp in _seen_fingerprints.items() if exp <= now]
    for fp in expired_fps:
        del _seen_fingerprints[fp]

    if expired_ids or expired_fps:
        log.debug("去重清理: %d message_ids + %d fingerprints",
                  len(expired_ids), len(expired_fps))


# ============================================================
# 飞书 API 客户端
# ============================================================

api_client = (
    lark.Client.builder()
    .app_id(APP_ID)
    .app_secret(APP_SECRET)
    .build()
)


def send_message(chat_id: str, text: str):
    """通过飞书 API 发送文本消息。"""
    body = (
        CreateMessageRequestBody.builder()
        .receive_id(chat_id)
        .msg_type("text")
        .content(json.dumps({"text": text}))
        .build()
    )

    request = (
        CreateMessageRequest.builder()
        .receive_id_type("chat_id")
        .request_body(body)
        .build()
    )

    resp = api_client.im.v1.message.create(request)
    if not resp.success():
        log.error("消息发送失败: code=%s msg=%s", resp.code, resp.msg)
    else:
        log.debug("消息已发送 [%s]: %s", chat_id[:12], text[:80])


# ============================================================
# 事件处理
# ============================================================

def on_receive_message(event_data):
    """收到 im.message.receive_v1 事件。"""
    try:
        msg = event_data.event.message
        msg_id = msg.message_id or ""

        chat_id = msg.chat_id
        msg_type = msg.message_type

        if msg_type != "text":
            log.debug("忽略非文本消息 type=%s", msg_type)
            return

        text = msg.content
        if not text:
            return

        try:
            parsed = json.loads(text)
            text = parsed.get("text", "")
        except (json.JSONDecodeError, TypeError):
            pass

        if not text.strip():
            return

        if _is_duplicate(msg_id, chat_id, text):
            return

        reply = route_message(chat_id, text, send_message)

        if reply is not None:
            send_message(chat_id, reply)

    except Exception:
        log.exception("消息处理异常")


# ============================================================
# 主入口
# ============================================================

def main():
    log.info("=" * 50)
    log.info("飞书个人助手 启动中... (云端版)")
    log.info("项目目录: %s", PROJECT_DIR)

    if APP_ID == "cli_xxxxxxxxxxxx" or APP_SECRET == "xxxxxxxxxxxxxxxx":
        log.error("请先在 config.py 中填入飞书 App ID 和 App Secret！")
        sys.exit(1)

    if APP_ID.startswith("cli_"):
        log.info("App ID: %s...", APP_ID[:12])

    # 1. 启动定时提醒引擎
    init_reminder_engine(send_message)
    log.info("待办提醒引擎就绪")

    # 2. 加载持久记忆统计
    mem_count = memory_count()
    if mem_count > 0:
        log.info("已加载 %d 条持久记忆", mem_count)
        latest_mems = list_memories()[:3]
        for m in latest_mems:
            log.debug("  记忆: %s", m.get("content", "")[:80])
    else:
        log.info("持久记忆为空（对我说「记住 xxx」来创建记忆）")

    # 3. 灵感记录统计
    insp_count = inspiration_count()
    if insp_count > 0:
        log.info("已加载 %d 条灵感记录", insp_count)

    # 4. 启动 Claude daemon（云端常驻进程）
    log.info("正在启动 Claude 常驻进程…")
    if not init_daemon():
        log.error("Claude daemon 启动失败！AI 对话功能将不可用。")
        log.error("   请确认: 1) Claude Code 已安装  2) claude 已登录")
        log.error("   手动测试: su - feishu -c 'claude -p \"hello\"'")
    else:
        log.info("Claude daemon 就绪（热启动模式）")

    # 5. 启动 PC Bridge（接受物理机 pc_agent 连接）
    log.info("正在启动 PC Bridge (ws://0.0.0.0:9527)…")
    start_bridge()
    log.info("PC Bridge 已启动，等待物理机连接…")

    # 6. 构建事件处理器
    event_handler = (
        EventDispatcherHandlerBuilder(
            encrypt_key="",
            verification_token="",
        )
        .register_p2_im_message_receive_v1(on_receive_message)
        .build()
    )

    # 7. 初始化 WebSocket 客户端
    ws_client = WSClient(
        app_id=APP_ID,
        app_secret=APP_SECRET,
        event_handler=event_handler,
    )

    log.info("WebSocket 连接中…（启动后请去飞书后台保存事件订阅配置）")
    log.info("功能状态: 待办✅ 记忆✅ 灵感✅ Claude✅ PC桥接✅")
    try:
        ws_client.start()
    except KeyboardInterrupt:
        log.info("收到终止信号，关闭中...")
    except Exception:
        log.exception("WebSocket 异常退出")
    finally:
        shutdown_reminder_engine()
        shutdown_daemon()
        stop_bridge()
        log.info("飞书个人助手已停止")


if __name__ == "__main__":
    main()
