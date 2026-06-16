"""
飞书个人助手 — 物理机代理 (pc_agent)
连接云端 PC Bridge，接收文件操作请求 → 调用本地 Claude → 回传结果。

功能：
  - WebSocket 长连接 ws://122.51.207.16:9527
  - 30s 心跳维持在线
  - 文件操作通过本地 Claude daemon 执行
  - 连接时自动从云端同步灵感 → 桌面 灵感记录.md
  - 断线自动重连
  - 本地 Web 仪表盘 http://localhost:9528 (实时状态 + 一键关闭)

启动: python pc_agent.py  或 双击 run_pc.bat
"""

import json
import sys
import os
import time
import threading
import hashlib
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

# === 添加本地 feishu-agent 到 Python 路径（复用 claude_daemon） ===
LOCAL_AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, LOCAL_AGENT_DIR)

try:
    import websockets
except ImportError:
    print("请先安装 websockets: pip install websockets")
    sys.exit(1)

import dashboard

TZ = ZoneInfo("Asia/Shanghai")

# ============================================================
# 配置
# ============================================================

CLOUD_HOST = "122.51.207.16"
CLOUD_PORT = 9527
DASHBOARD_PORT = 9528
HEARTBEAT_INTERVAL = 30
RECONNECT_DELAY = 10  # 断线重连间隔（秒）
INSPIRATION_FILE = r"C:\Users\25284\Desktop\灵感记录.md"

# ============================================================
# 关闭信号
# ============================================================

_shutdown_event = threading.Event()

# ============================================================
# 日志（同时输出到文件 + 仪表盘缓冲区）
# ============================================================


class DashboardLogHandler(logging.Handler):
    """将日志行推送到仪表盘的环形缓冲区。"""

    def emit(self, record):
        try:
            msg = self.format(record)
            dashboard.add_log_line(msg)
        except Exception:
            pass


logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)-7s] %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "pc_agent.log"),
            encoding="utf-8",
        ),
        DashboardLogHandler(),
    ],
)
log = logging.getLogger("pc_agent")

# ============================================================
# 灵感同步
# ============================================================


def sync_inspirations(inspirations: list[dict]):
    """将云端灵感写入桌面 灵感记录.md。"""
    if not inspirations:
        log.debug("无新灵感需要同步")
        return

    # 读取现有内容
    existing = ""
    try:
        with open(INSPIRATION_FILE, "r", encoding="utf-8") as f:
            existing = f.read()
    except FileNotFoundError:
        pass

    # 构建新条目
    now_str = datetime.now(TZ).strftime("%Y-%m-%d %H:%M")
    new_entries = []
    for insp in inspirations:
        type_icon = {"idea": "💡", "note": "📝", "link": "🔗", "task": "📋"}.get(
            insp.get("type", "idea"), "💡")
        new_entries.append(
            f"{type_icon} [{insp.get('created_at', now_str)[:16]}] {insp['content']}"
        )

    # 找到"云端同步"标记位置，在其后更新
    sync_marker = "<!-- CLOUD_SYNC -->"
    if sync_marker in existing:
        # 替换同步区域
        before = existing.split(sync_marker)[0]
        existing = before + sync_marker + "\n" + "\n".join(new_entries) + "\n"
    else:
        # 追加到文件末尾
        if existing and not existing.endswith("\n"):
            existing += "\n"
        existing += f"\n{sync_marker}\n## 云端灵感 (同步于 {now_str})\n"
        existing += "\n".join(new_entries) + "\n"

    with open(INSPIRATION_FILE, "w", encoding="utf-8") as f:
        f.write(existing)

    dashboard.update_state(sync_count=len(new_entries), last_sync=now_str)
    log.info("已同步 %d 条灵感到 %s", len(new_entries), INSPIRATION_FILE)


# ============================================================
# Claude 调用 (复用本地 claude_daemon)
# ============================================================

_claude_daemon = None


def init_claude() -> bool:
    """初始化本地 Claude daemon。"""
    global _claude_daemon
    try:
        from claude_daemon import ClaudeDaemon
        _claude_daemon = ClaudeDaemon()
        ok = _claude_daemon.start()
        if ok:
            log.info("Claude daemon 已就绪")
            dashboard.update_state(claude_ready=True, claude_mode="daemon")
        else:
            log.error("Claude daemon 启动失败")
            dashboard.update_state(claude_ready=False, claude_mode="error")
        return ok
    except ImportError as e:
        log.error("无法导入 claude_daemon: %s (检查 %s)", e, LOCAL_AGENT_DIR)
        dashboard.update_state(claude_ready=False, claude_mode="subprocess(fallback)")
        return False
    except Exception as e:
        log.exception("Claude daemon 初始化异常")
        dashboard.update_state(claude_ready=False, claude_mode="error")
        return False


def call_claude_local(prompt: str, timeout: float = 180) -> str:
    """通过本地 daemon 调用 Claude。"""
    if _claude_daemon is None:
        # 回退：直接 subprocess 调用
        import subprocess
        claude_path = (
            r"C:\Users\25284\AppData\Roaming\npm"
            r"\node_modules\@anthropic-ai\claude-code\bin\claude.exe"
        )
        try:
            result = subprocess.run(
                [claude_path, "--dangerously-skip-permissions", "-p",
                 "--output-format", "text", prompt],
                capture_output=True, text=True,
                cwd=r"C:\Users\25284",
                timeout=timeout,
            )
            return result.stdout.strip() or result.stderr.strip()
        except subprocess.TimeoutExpired:
            return "⏱️ 本地 Claude 响应超时。请尝试简化指令。"
        except Exception as e:
            return f"❌ 本地 Claude 调用失败: {e}"

    try:
        result = _claude_daemon.send(prompt, timeout=timeout)
        return result
    except Exception as e:
        log.exception("Claude daemon 调用异常")
        return f"❌ 本地 Claude 异常: {e}"


def shutdown_claude():
    """关闭本地 Claude daemon。"""
    global _claude_daemon
    if _claude_daemon:
        _claude_daemon.stop()
        _claude_daemon = None


# ============================================================
# WebSocket 客户端
# ============================================================

async def connect_loop():
    """WebSocket 连接主循环（含断线重连）。"""
    import asyncio

    url = f"ws://{CLOUD_HOST}:{CLOUD_PORT}"

    try:
        while not _shutdown_event.is_set():
            log.info("正在连接云端 %s ...", url)
            dashboard.update_state(connected=False)
            try:
                async with websockets.connect(url, ping_interval=None) as ws:
                    log.info("已连接到云端 PC Bridge")
                    dashboard.update_state(connected=True)
                    dashboard.add_log_line("✅ 已连接到云端")

                    # 请求同步灵感
                    await ws.send(json.dumps({"type": "sync_request"}))
                    log.debug("已请求灵感同步")

                    # 启动心跳
                    heartbeat_task = asyncio.create_task(_heartbeat_sender(ws))

                    try:
                        while not _shutdown_event.is_set():
                            try:
                                raw_msg = await asyncio.wait_for(
                                    ws.recv(), timeout=1.0)
                            except asyncio.TimeoutError:
                                continue  # 超时后回到 while 检查关闭信号

                            try:
                                msg = json.loads(raw_msg)
                            except json.JSONDecodeError:
                                log.warning("收到非法 JSON: %s", raw_msg[:100])
                                continue

                            msg_type = msg.get("type", "")

                            if msg_type == "heartbeat_ack":
                                log.debug("心跳 ACK")
                                now_str = datetime.now(TZ).strftime("%H:%M:%S")
                                dashboard.update_state(last_heartbeat=now_str)

                            elif msg_type == "request":
                                req_id = msg.get("request_id", "")
                                prompt = msg.get("prompt", "")
                                log.info("收到文件操作请求 req_id=%s: %s", req_id, prompt[:100])
                                dashboard.update_state(last_request=prompt[:80])
                                dashboard.add_log_line(f"📥 收到请求: {prompt[:60]}...")

                                # 在后台线程中执行 Claude（不阻塞 WebSocket 接收）
                                def _execute():
                                    return call_claude_local(prompt, timeout=180)

                                loop = asyncio.get_event_loop()
                                result = await loop.run_in_executor(None, _execute)

                                # 回传结果
                                resp = json.dumps({
                                    "type": "response",
                                    "request_id": req_id,
                                    "result": result,
                                }, ensure_ascii=False)
                                await ws.send(resp)
                                log.info("已回传结果 req_id=%s len=%d", req_id, len(result))
                                dashboard.update_state(last_response=result[:100])
                                dashboard.add_log_line(
                                    f"📤 已回复 req_id={req_id} len={len(result)}")

                            elif msg_type == "sync_data":
                                inspirations = msg.get("inspirations", [])
                                if inspirations:
                                    def _sync():
                                        sync_inspirations(inspirations)
                                    loop = asyncio.get_event_loop()
                                    await loop.run_in_executor(None, _sync)

                            else:
                                log.debug("未知消息类型: %s", msg_type)

                    except websockets.exceptions.ConnectionClosed:
                        log.warning("连接已断开")
                        dashboard.update_state(connected=False)
                        dashboard.add_log_line("⚠️ 连接已断开")
                    finally:
                        heartbeat_task.cancel()
                        try:
                            await heartbeat_task
                        except asyncio.CancelledError:
                            pass

            except (websockets.exceptions.ConnectionClosed, OSError,
                    asyncio.TimeoutError) as e:
                log.warning("连接失败: %s，%ds 后重连...", e, RECONNECT_DELAY)
                dashboard.add_log_line(f"⚠️ 连接失败: {e}")
            except Exception:
                log.exception("连接异常，%ds 后重连...", RECONNECT_DELAY)
                dashboard.add_log_line(f"⚠️ 连接异常，{RECONNECT_DELAY}s 后重连")

            if _shutdown_event.is_set():
                break
            await asyncio.sleep(RECONNECT_DELAY)

    finally:
        log.info("PC Agent 主循环退出")


async def _heartbeat_sender(ws):
    """每 HEARTBEAT_INTERVAL 秒发送心跳。"""
    import asyncio
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL)
        try:
            await ws.send(json.dumps({"type": "heartbeat"}))
            log.debug("发送心跳")
        except Exception:
            break


# ============================================================
# 主入口
# ============================================================

def main():
    print()
    print("=" * 50)
    print("  PC Agent - 飞书个人助手物理机代理")
    print("=" * 50)
    print(f"  云端: ws://{CLOUD_HOST}:{CLOUD_PORT}")
    print(f"  仪表盘: http://localhost:{DASHBOARD_PORT}")
    print(f"  灵感文件: {INSPIRATION_FILE}")
    print("  按 Ctrl+C 停止  |  或访问仪表盘「关闭服务」")
    print("=" * 50)
    print()

    # 初始化仪表盘（先于其他组件，确保日志捕获）
    dashboard.set_shutdown_event(_shutdown_event)
    dashboard.update_state(
        cloud_host=f"{CLOUD_HOST}:{CLOUD_PORT}",
        started_at=datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"),
    )
    if not dashboard.start_server(port=DASHBOARD_PORT):
        log.warning("仪表盘启动失败，继续运行（无 Web 界面）")
        print(f"⚠️ 仪表盘启动失败，可通过 http://localhost:{DASHBOARD_PORT} 检查是否端口被占用")

    # 初始化 Claude daemon
    log.info("正在启动本地 Claude daemon...")
    if not init_claude():
        log.warning("Claude daemon 启动失败，将使用 subprocess 回退模式")
        print("⚠️ Claude daemon 启动失败，性能会较慢。")
        print("   请确认 Claude Code 已安装且已登录。")

    log.info("PC Agent 启动中...")
    dashboard.add_log_line("🚀 PC Agent 启动中...")
    print(f"✅ 初始化完成，仪表盘: http://localhost:{DASHBOARD_PORT}")
    print("   等待云端连接...\n")

    try:
        import asyncio
        asyncio.run(connect_loop())
    except KeyboardInterrupt:
        log.info("收到 Ctrl+C 终止信号")
        dashboard.add_log_line("⏹️ 收到 Ctrl+C 终止信号")
    finally:
        log.info("正在关闭...")
        dashboard.add_log_line("⏹️ 正在关闭 PC Agent...")
        shutdown_claude()
        dashboard.stop_server()
        log.info("PC Agent 已停止")
        dashboard.add_log_line("⚫ PC Agent 已停止")


if __name__ == "__main__":
    main()
