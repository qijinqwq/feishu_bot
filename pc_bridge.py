"""
飞书个人助手 — PC 桥接层 (云端 WebSocket 服务端)
接受物理机 pc_agent.py 的 WebSocket 连接，管理 PC 在线状态，
转发文件操作请求到 PC → 本地 Claude 执行 → 回传结果。

协议（JSON 行）:
  PC → Cloud: {"type":"heartbeat"}
  Cloud → PC: {"type":"heartbeat_ack"}
  Cloud → PC: {"type":"request","request_id":"uuid","prompt":"..."}
  PC → Cloud: {"type":"response","request_id":"uuid","result":"..."}
  PC → Cloud: {"type":"sync_request"}   (PC请求同步灵感/记忆)
  Cloud → PC: {"type":"sync_data","inspirations":[...]}
"""

import asyncio
import json
import logging
import threading
import time
import uuid
from typing import Optional, Callable

import websockets
from websockets.asyncio.server import ServerConnection

from config import (
    PC_BRIDGE_HOST, PC_BRIDGE_PORT,
    PC_BRIDGE_HEARTBEAT_INTERVAL, PC_BRIDGE_HEARTBEAT_TIMEOUT,
)

log = logging.getLogger("agent.pc_bridge")

# ============================================================
# 连接状态
# ============================================================

_ws: Optional[ServerConnection] = None      # 当前唯一的 PC 连接
_last_heartbeat: float = 0.0                # 上次心跳时间 (monotonic seconds)
_lock = threading.Lock()

# 正在等待响应的请求
_pending_requests: dict[str, "asyncio.Future"] = {}
_loop: Optional[asyncio.AbstractEventLoop] = None   # bridge 的事件循环

# ============================================================
# 公共 API（从 message_handler / agent 调用）
# ============================================================

def is_pc_online() -> bool:
    """检查物理机是否在线。"""
    with _lock:
        if _ws is None:
            return False
        elapsed = time.monotonic() - _last_heartbeat
        return elapsed < PC_BRIDGE_HEARTBEAT_TIMEOUT


def send_to_pc(prompt: str, timeout: float = 180) -> Optional[str]:
    """
    向物理机发送文件操作请求，同步等待结果。

    Args:
        prompt:  发送给 PC 端 Claude 的指令
        timeout: 等待超时（秒）

    Returns:
        PC 端 Claude 的回复文本；PC 不在线或超时返回 None。
    """
    if not is_pc_online():
        return None

    req_id = uuid.uuid4().hex[:12]

    # 向 asyncio 事件循环提交任务
    loop = _get_loop()
    if loop is None:
        return None

    future = asyncio.run_coroutine_threadsafe(
        _send_request_async(req_id, prompt), loop
    )

    try:
        result = future.result(timeout=timeout)
        return result
    except TimeoutError:
        log.warning("PC 请求超时: req_id=%s", req_id)
        with _lock:
            _pending_requests.pop(req_id, None)
        return "⏱️ 物理机处理超时，请检查 PC 是否正常运行。"
    except Exception as e:
        log.error("PC 请求异常: %s", e)
        return f"❌ PC 通信异常: {e}"


async def _send_request_async(req_id: str, prompt: str) -> Optional[str]:
    """异步发送请求并等待响应。"""
    ws = _get_ws()
    if ws is None:
        return None

    # 创建 Future 等待响应
    loop = asyncio.get_event_loop()
    future = loop.create_future()
    with _lock:
        _pending_requests[req_id] = future

    try:
        msg = json.dumps({
            "type": "request",
            "request_id": req_id,
            "prompt": prompt,
        }, ensure_ascii=False)
        await ws.send(msg)
        log.debug("PC 请求已发送: req_id=%s prompt=%s", req_id, prompt[:80])
    except Exception as e:
        log.error("PC 请求发送失败: %s", e)
        with _lock:
            _pending_requests.pop(req_id, None)
        return None

    # 等待响应
    try:
        result = await asyncio.wait_for(future, timeout=180)
        return result
    except asyncio.TimeoutError:
        with _lock:
            _pending_requests.pop(req_id, None)
        return None


def get_sync_data() -> list[dict]:
    """获取灵感数据用于同步到 PC（由 inspiration_manager 提供）。"""
    try:
        from inspiration_manager import list_inspirations
        return list_inspirations()
    except ImportError:
        return []


# ============================================================
# 内部辅助
# ============================================================

def _get_ws() -> Optional[ServerConnection]:
    with _lock:
        return _ws


def _get_loop() -> Optional[asyncio.AbstractEventLoop]:
    with _lock:
        return _loop


# ============================================================
# WebSocket 服务端
# ============================================================

async def _handle_connection(websocket: ServerConnection):
    """处理单个 PC 连接。"""
    global _ws, _last_heartbeat

    remote = websocket.remote_address
    log.info("PC 已连接: %s", remote)

    # 注册连接（同一时间只允许一个 PC）
    with _lock:
        old_ws = _ws
        _ws = websocket
        _last_heartbeat = time.monotonic()

    # 关闭旧连接
    if old_ws is not None:
        try:
            await old_ws.close()
        except Exception:
            pass

    try:
        async for raw_msg in websocket:
            try:
                msg = json.loads(raw_msg)
            except json.JSONDecodeError:
                log.warning("PC 收到非法 JSON: %s", raw_msg[:100])
                continue

            msg_type = msg.get("type", "")

            if msg_type == "heartbeat":
                with _lock:
                    _last_heartbeat = time.monotonic()
                try:
                    await websocket.send(json.dumps({"type": "heartbeat_ack"}))
                except Exception:
                    pass

            elif msg_type == "response":
                req_id = msg.get("request_id", "")
                result = msg.get("result", "")
                with _lock:
                    future = _pending_requests.pop(req_id, None)
                if future and not future.done():
                    future.set_result(result)
                    log.debug("PC 响应: req_id=%s len=%d", req_id, len(result))
                else:
                    log.debug("PC 响应未匹配: req_id=%s", req_id)

            elif msg_type == "sync_request":
                # PC 请求同步灵感数据
                inspirations = get_sync_data()
                sync_msg = json.dumps({
                    "type": "sync_data",
                    "inspirations": inspirations,
                }, ensure_ascii=False)
                try:
                    await websocket.send(sync_msg)
                    log.debug("已同步 %d 条灵感到 PC", len(inspirations))
                except Exception:
                    pass

            else:
                log.debug("PC 未知消息类型: %s", msg_type)

    except websockets.exceptions.ConnectionClosed:
        log.info("PC 连接已断开: %s", remote)
    except Exception:
        log.exception("PC 连接异常: %s", remote)
    finally:
        with _lock:
            if _ws is websocket:
                _ws = None
        # 清理该连接上的所有 pending 请求
        with _lock:
            for req_id, future in list(_pending_requests.items()):
                if not future.done():
                    future.set_result("⏸️ 物理机已断开连接。")
                del _pending_requests[req_id]


async def _heartbeat_checker():
    """后台任务：定期检查心跳超时，超时后主动断开。"""
    while True:
        await asyncio.sleep(PC_BRIDGE_HEARTBEAT_INTERVAL)
        with _lock:
            ws = _ws
            elapsed = time.monotonic() - _last_heartbeat if _last_heartbeat > 0 else 0
        if ws is not None and elapsed > PC_BRIDGE_HEARTBEAT_TIMEOUT:
            log.warning("PC 心跳超时 (%.0fs)，断开连接", elapsed)
            try:
                await ws.close()
            except Exception:
                pass


async def _serve():
    """启动 WebSocket 服务器。"""
    global _loop
    _loop = asyncio.get_event_loop()
    async with websockets.serve(_handle_connection, PC_BRIDGE_HOST, PC_BRIDGE_PORT):
        log.info("PC Bridge 监听 ws://%s:%s", PC_BRIDGE_HOST, PC_BRIDGE_PORT)
        # 同时启动心跳检查
        asyncio.create_task(_heartbeat_checker())
        await asyncio.Future()  # 永远运行


# ============================================================
# 线程桥接 — agent.py 是同步的，pc_bridge 是异步的
# ============================================================

_bridge_thread: Optional[threading.Thread] = None


def start_bridge():
    """在后台线程启动 PC Bridge 服务器。"""
    global _bridge_thread

    if _bridge_thread is not None and _bridge_thread.is_alive():
        return

    def _runner():
        asyncio.run(_serve())

    _bridge_thread = threading.Thread(
        target=_runner,
        daemon=True,
        name="pc-bridge",
    )
    _bridge_thread.start()
    log.info("PC Bridge 线程已启动")


def stop_bridge():
    """停止 PC Bridge（由 agent shutdown 调用）。"""
    global _bridge_thread
    # daemon 线程随主进程退出自动结束
    _bridge_thread = None
    log.info("PC Bridge 已停止")
