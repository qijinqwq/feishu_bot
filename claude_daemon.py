"""
飞书个人助手 — Claude 常驻进程管理器 (claude_daemon)

维护一个 claude.exe 持久子进程，通过 stream-json over stdin/stdout
实现消息收发。一次冷启动，后续全部热复用。

协议：
  stdin  ← {"type":"user","message":{"role":"user","content":"..."}}\n
  stdout → JSONL 流，读到 {"type":"result",...} 为止，取 result 字段

特性：
  - 串行锁：同一时刻只处理一条消息
  - 自动重启：进程 crash 后下一条消息触发重建
  - 空闲保活：长驻不退出
"""

import subprocess
import json
import threading
import logging
import time
import os
from typing import Optional

from config import CLAUDE_CLI_PATH, CLAUDE_DAEMON_TIMEOUT, CLAUDE_WORK_DIR

log = logging.getLogger("agent.daemon")

# Claude 输出中的 "好的。" 最短约 3 个 UTF-8 中文字符
MIN_RESULT_BYTES = 6


class ClaudeDaemon:
    """Claude 常驻进程包装。"""

    def __init__(self):
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._shutdown = threading.Event()
        self._reader_thread: Optional[threading.Thread] = None

        # 当前请求的同步设施
        self._result: Optional[str] = None
        self._result_ready = threading.Event()

    # ================================================================
    # 生命周期
    # ================================================================

    def start(self) -> bool:
        """启动 Claude 子进程，等待 init 事件。失败返回 False。"""
        with self._lock:
            return self._start_locked()

    def stop(self):
        """优雅关闭子进程。"""
        self._shutdown.set()
        with self._lock:
            self._stop_locked()

    def _start_locked(self) -> bool:
        """在持有锁的情况下启动进程。"""
        if self._proc is not None:
            log.warning("daemon 已经在运行，先停止再启动")
            self._stop_locked()

        if not os.path.exists(CLAUDE_CLI_PATH):
            log.error("Claude CLI 不存在: %s", CLAUDE_CLI_PATH)
            return False

        self._ready.clear()
        self._shutdown.clear()

        cmd = [
            CLAUDE_CLI_PATH,
            "--dangerously-skip-permissions",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--verbose",
            "-p",
            "--no-session-persistence",
        ]

        log.info("启动 Claude daemon: %s", " ".join(cmd[:1] + ["[...flags...]"]))

        try:
            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=CLAUDE_WORK_DIR,
                # 二进制模式避免 Windows 行尾转换和编码问题
            )
        except Exception:
            log.exception("无法启动 Claude 进程")
            return False

        # 启动 stdout 消费线程
        self._reader_thread = threading.Thread(
            target=self._reader_loop,
            daemon=True,
            name="claude-reader",
        )
        self._reader_thread.start()

        # 异步 drain stderr
        threading.Thread(
            target=self._drain_stderr,
            daemon=True,
            name="claude-stderr",
        ).start()

        # Claude stream-json: 必须先写一条消息到 stdin 才会输出 init 事件。
        # 发送一个轻量 ping 来触发 init + 验证管道通畅。
        if not self._ping_locked():
            log.error("Claude daemon ping 失败")
            self._stop_locked()
            return False

        log.info("Claude daemon 已就绪 (pid=%s)", self._proc.pid)
        return True

    def _ping_locked(self) -> bool:
        """
        发送一条极简消息触发 Claude 输出 init 事件并验证响应通畅。
        必须在 _lock 持有下调用。
        """
        ping_msg = json.dumps({
            "type": "user",
            "message": {"role": "user", "content": "回复一个 1。"},
        }, ensure_ascii=False)

        self._result = None
        self._result_ready.clear()

        try:
            self._proc.stdin.write(ping_msg.encode("utf-8") + b"\n")
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            log.error("ping 写入失败: %s", e)
            return False

        if not self._result_ready.wait(timeout=30):
            log.error("ping 响应超时")
            return False

        # 检查是否收到 init（reader 线程会在看到 init 时 set _ready）
        if not self._ready.is_set():
            log.error("未收到 init 事件")
            return False

        log.debug("ping 成功: %s", self._result[:80] if self._result else "(空)")
        return True

    def _stop_locked(self):
        """在持有锁的情况下停止进程。"""
        proc = self._proc
        self._proc = None
        self._ready.clear()

        if proc is None:
            return

        try:
            if proc.stdin and not proc.stdin.closed:
                proc.stdin.close()
        except Exception:
            pass

        try:
            proc.terminate()
        except Exception:
            pass

        try:
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
                proc.wait(timeout=3)
            except Exception:
                pass

        log.info("Claude daemon 已停止 (pid=%s, rc=%s)", proc.pid, proc.returncode)

    # ================================================================
    # 消息收发
    # ================================================================

    def send(self, prompt: str, timeout: float = None) -> str:
        """
        向 Claude 发送一条消息，阻塞等待回复。

        Args:
            prompt:  用户消息文本
            timeout: 超时秒数，默认 DAEMON_TIMEOUT

        Returns:
            Claude 的回复文本；超时或出错返回错误描述。
        """
        if timeout is None:
            timeout = CLAUDE_DAEMON_TIMEOUT

        # 准备
        self._result = None
        self._result_ready.clear()

        msg = json.dumps({
            "type": "user",
            "message": {"role": "user", "content": prompt},
        }, ensure_ascii=False)

        # 获取锁，确保串行
        acquired = self._lock.acquire(timeout=timeout + 120)
        if not acquired:
            return "❌ 内部错误：获取 daemon 锁超时，可能有消息堆积。"

        try:
            # 确保进程存活
            if self._proc is None or self._proc.poll() is not None:
                log.warning("daemon 进程已退出，尝试重建…")
                self._stop_locked()
                if not self._start_locked():
                    return "❌ Claude 进程启动失败，请检查是否已安装 Claude Code。"

            # 写入 stdin
            try:
                raw = msg.encode("utf-8") + b"\n"
                self._proc.stdin.write(raw)
                self._proc.stdin.flush()
            except (BrokenPipeError, OSError) as e:
                log.error("写入 daemon stdin 失败: %s", e)
                return f"❌ Claude 进程通信中断: {e}"

            # 等待结果
            if not self._result_ready.wait(timeout=timeout):
                log.warning("daemon 回复超时 (%.0fs)，将重启进程", timeout)
                self._stop_locked()
                return f"⏱️ Claude 响应超时（{int(timeout)}秒），请尝试简化指令。"

            result = self._result
            if result is None:
                return "❌ Claude 未返回任何内容。"

            # 飞书消息上限约 5000 字符
            if len(result) > 4000:
                result = result[:4000] + "\n\n…（内容过长已截断）"
            return result

        finally:
            self._lock.release()

    # ================================================================
    # 内部线程
    # ================================================================

    def _reader_loop(self):
        """
        stdout 消费线程 — 持续读取 JSONL 直到进程退出。
        每看到一个 type:"result" 就把结果交给等待的 send() 调用。
        """
        buf = b""
        try:
            while not self._shutdown.is_set():
                proc = self._proc
                if proc is None or proc.stdout is None:
                    break

                # 读取一块数据
                try:
                    chunk = proc.stdout.read1(4096)
                except (ValueError, OSError):
                    break

                if not chunk:
                    # stdout 关闭 → 进程退出
                    break

                buf += chunk

                # 按行解析
                while b"\n" in buf:
                    line_bytes, buf = buf.split(b"\n", 1)
                    self._handle_line(line_bytes)

        except Exception:
            log.exception("reader_loop 异常退出")
        finally:
            log.debug("reader_loop 线程退出")
            self._ready.clear()

    def _handle_line(self, line_bytes: bytes):
        """处理一行 stdout JSON。"""
        if not line_bytes:
            return

        try:
            line = line_bytes.decode("utf-8", errors="replace")
            obj = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            # 不是 JSON，忽略（可能是启动 banner）
            return

        msg_type = obj.get("type", "")
        subtype = obj.get("subtype", "")

        # init 事件 → 标记就绪
        if msg_type == "system" and subtype == "init":
            self._ready.set()
            log.debug("daemon init: session=%s model=%s",
                      obj.get("session_id", "")[:20],
                      obj.get("model", "?"))
            return

        # result 事件 → 提取最终文本
        if msg_type == "result":
            if not obj.get("is_error", False):
                text = obj.get("result", "")
                self._result = text
                log.debug("daemon result: %d chars, session=%s",
                          len(text), obj.get("session_id", "")[:20])
            else:
                self._result = f"❌ Claude 调用失败: {obj.get('result', '未知错误')}"
                log.error("daemon error: %s", self._result[:200])
            self._result_ready.set()
            return

    def _drain_stderr(self):
        """消费 stderr，防止管道阻塞。"""
        try:
            while not self._shutdown.is_set():
                proc = self._proc
                if proc is None or proc.stderr is None:
                    break
                chunk = proc.stderr.read1(8192)
                if not chunk:
                    break
                # 记录为 debug 级别
                text = chunk.decode("utf-8", errors="replace").strip()
                if text:
                    for line in text.split("\n"):
                        if line.strip():
                            log.debug("claude stderr: %s", line[:200])
        except Exception:
            pass


# ================================================================
# 模块单例
# ================================================================

_daemon: Optional[ClaudeDaemon] = None


def get_daemon() -> Optional[ClaudeDaemon]:
    return _daemon


def init_daemon() -> bool:
    """初始化全局 daemon 单例。"""
    global _daemon
    if _daemon is not None:
        return True
    _daemon = ClaudeDaemon()
    return _daemon.start()


def shutdown_daemon():
    """关闭全局 daemon。"""
    global _daemon
    if _daemon is not None:
        _daemon.stop()
        _daemon = None
