"""
PC Agent — Web 仪表盘
本地 HTTP 服务，显示实时状态 + 提供关闭入口。

访问: http://localhost:9528
"""

import json
import sys
import os
import time
import threading
import logging
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler

log = logging.getLogger("pc_agent.dashboard")

# ============================================================
# 共享状态（pc_agent 主线程写入，dashboard 线程读取）
# ============================================================

_state_lock = threading.Lock()

_state = {
    "connected": False,           # 是否已连接云端
    "cloud_host": "122.51.207.16:9527",
    "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    "last_heartbeat": None,       # 上次心跳时间
    "last_request": None,         # 最近一次文件操作请求
    "last_response": None,        # 最近一次响应结果（截断）
    "sync_count": 0,              # 已同步灵感数
    "last_sync": None,            # 上次同步时间
    "claude_ready": False,        # 本地 Claude 是否就绪
    "claude_mode": "unknown",     # "daemon" / "subprocess" / "none"
}

# 最近日志（环形缓冲区）
_log_lines: list[str] = []
_MAX_LOG_LINES = 100


def update_state(**kwargs):
    """线程安全地更新共享状态。"""
    with _state_lock:
        _state.update(kwargs)


def add_log_line(line: str):
    """追加日志行到环形缓冲区。"""
    global _log_lines
    timestamp = datetime.now().strftime("%H:%M:%S")
    entry = f"[{timestamp}] {line}"
    _log_lines.append(entry)
    if len(_log_lines) > _MAX_LOG_LINES:
        _log_lines = _log_lines[-_MAX_LOG_LINES:]


def get_state() -> dict:
    """获取当前状态快照。"""
    with _state_lock:
        snap = dict(_state)
    snap["recent_logs"] = list(_log_lines[-50:])  # 最近 50 条
    snap["log_count"] = len(_log_lines)
    return snap


# ============================================================
# 关闭信号
# ============================================================

_shutdown_event: threading.Event = None  # 由 pc_agent 设置


def set_shutdown_event(event: threading.Event):
    global _shutdown_event
    _shutdown_event = event


def trigger_shutdown():
    """触发关闭（由 HTTP handler 调用）。"""
    if _shutdown_event:
        _shutdown_event.set()
        return True
    return False


# ============================================================
# HTTP 请求处理器
# ============================================================

_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>PC Agent — 飞书助手物理机代理</title>
<style>
  :root {
    --bg: #0d1117;
    --card: #161b22;
    --border: #30363d;
    --text: #c9d1d9;
    --dim: #8b949e;
    --green: #3fb950;
    --red: #f85149;
    --yellow: #d2991d;
    --blue: #58a6ff;
    --accent: #1f6feb;
    --radius: 10px;
  }
  * { margin:0; padding:0; box-sizing:border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: var(--bg);
    color: var(--text);
    min-height: 100vh;
    padding: 24px;
  }
  .container { max-width: 720px; margin: 0 auto; }
  h1 {
    font-size: 1.5rem;
    font-weight: 600;
    margin-bottom: 4px;
  }
  .subtitle {
    color: var(--dim);
    font-size: 0.85rem;
    margin-bottom: 24px;
  }

  .status-bar {
    display: flex;
    gap: 12px;
    margin-bottom: 20px;
    flex-wrap: wrap;
  }
  .badge {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 6px 14px;
    border-radius: 20px;
    font-size: 0.85rem;
    font-weight: 500;
    background: var(--card);
    border: 1px solid var(--border);
  }
  .badge .dot {
    width: 10px; height: 10px;
    border-radius: 50%;
    background: var(--dim);
  }
  .badge .dot.online  { background: var(--green);  box-shadow: 0 0 6px var(--green); }
  .badge .dot.offline { background: var(--red);    box-shadow: 0 0 6px var(--red); }
  .badge .dot.warn    { background: var(--yellow); box-shadow: 0 0 6px var(--yellow); }

  .card {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 16px 20px;
    margin-bottom: 16px;
  }
  .card h3 {
    font-size: 0.8rem;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--dim);
    margin-bottom: 10px;
  }
  .kv { display: flex; justify-content: space-between; padding: 4px 0; font-size: 0.9rem; }
  .kv .key { color: var(--dim); }
  .kv .val { font-family: "SF Mono", "Cascadia Code", monospace; }

  .log-view {
    background: #0d1117;
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 12px 16px;
    max-height: 320px;
    overflow-y: auto;
    font-family: "SF Mono", "Cascadia Code", monospace;
    font-size: 0.78rem;
    line-height: 1.5;
    white-space: pre-wrap;
    word-break: break-all;
  }
  .log-view .dim { color: var(--dim); }

  .btn-row { display: flex; gap: 10px; margin-top: 20px; flex-wrap: wrap; }
  .btn {
    padding: 10px 22px;
    border-radius: 8px;
    border: 1px solid var(--border);
    font-size: 0.9rem;
    cursor: pointer;
    font-weight: 500;
    text-decoration: none;
    display: inline-flex;
    align-items: center;
    gap: 6px;
    transition: 0.15s;
  }
  .btn-danger {
    background: #da3633;
    border-color: #da3633;
    color: #fff;
  }
  .btn-danger:hover { background: #c62828; }
  .btn-secondary {
    background: var(--card);
    color: var(--text);
  }
  .btn-secondary:hover { background: #21262d; }

  .confirm-overlay {
    display: none;
    position: fixed;
    inset: 0;
    background: rgba(0,0,0,0.6);
    z-index: 100;
    align-items: center;
    justify-content: center;
  }
  .confirm-overlay.show { display: flex; }
  .confirm-box {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 28px;
    text-align: center;
    max-width: 360px;
  }
  .confirm-box p { margin-bottom: 20px; font-size: 1rem; }
  .confirm-box .btn-row { justify-content: center; }

  @keyframes pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.5; }
  }
  .updating { animation: pulse 1s ease-in-out; }
</style>
</head>
<body>
<div class="container">
  <h1>🖥️ PC Agent</h1>
  <p class="subtitle">飞书个人助手 · 物理机代理 · <span id="uptime">--</span></p>

  <div class="status-bar">
    <div class="badge" id="badge-cloud">
      <span class="dot" id="dot-cloud"></span>
      <span id="label-cloud">云端: --</span>
    </div>
    <div class="badge" id="badge-claude">
      <span class="dot" id="dot-claude"></span>
      <span>Claude: <span id="label-claude">--</span></span>
    </div>
    <div class="badge" id="badge-sync">
      <span>灵感同步: <span id="label-sync">--</span></span>
    </div>
  </div>

  <div class="card">
    <h3>📡 连接信息</h3>
    <div class="kv"><span class="key">云端地址</span><span class="val" id="info-host">--</span></div>
    <div class="kv"><span class="key">上次心跳</span><span class="val" id="info-heartbeat">--</span></div>
    <div class="kv"><span class="key">启动时间</span><span class="val" id="info-started">--</span></div>
    <div class="kv"><span class="key">最近请求</span><span class="val" id="info-request">--</span></div>
  </div>

  <div class="card">
    <h3>📋 最近日志</h3>
    <div class="log-view" id="log-view">
      <span class="dim">等待日志...</span>
    </div>
  </div>

  <div class="btn-row">
    <button class="btn btn-secondary" onclick="location.reload()">🔄 刷新</button>
    <button class="btn btn-danger" id="btn-shutdown" onclick="showConfirm()">⏹️ 关闭服务</button>
  </div>
</div>

<div class="confirm-overlay" id="confirm-overlay">
  <div class="confirm-box">
    <p>⚠️ 确认要关闭 PC Agent 吗？<br><small style="color:var(--dim)">关闭后物理机将不在线，文件操作等功能将不可用。</small></p>
    <div class="btn-row">
      <button class="btn btn-secondary" onclick="hideConfirm()">取消</button>
      <button class="btn btn-danger" onclick="doShutdown()">确认关闭</button>
    </div>
  </div>
</div>

<script>
  let startedAt = null;

  async function refresh() {
    try {
      const r = await fetch('/api/status');
      const s = await r.json();

      // 云端连接
      const dotCloud = document.getElementById('dot-cloud');
      const labelCloud = document.getElementById('label-cloud');
      if (s.connected) {
        dotCloud.className = 'dot online';
        labelCloud.textContent = '云端: 已连接';
      } else {
        dotCloud.className = 'dot offline';
        labelCloud.textContent = '云端: 未连接';
      }

      // Claude 状态
      const dotClaude = document.getElementById('dot-claude');
      const labelClaude = document.getElementById('label-claude');
      if (s.claude_ready) {
        dotClaude.className = 'dot online';
        labelClaude.textContent = s.claude_mode;
      } else {
        dotClaude.className = 'dot warn';
        labelClaude.textContent = s.claude_mode || '未就绪';
      }

      // 灵感同步
      document.getElementById('label-sync').textContent =
        s.sync_count > 0 ? `${s.sync_count} 条` : '无';

      // 连接信息
      document.getElementById('info-host').textContent = s.cloud_host;
      document.getElementById('info-heartbeat').textContent =
        s.last_heartbeat || '--';
      document.getElementById('info-started').textContent = s.started_at;
      document.getElementById('info-request').textContent =
        s.last_request || '无';

      // 日志
      const logView = document.getElementById('log-view');
      if (s.recent_logs && s.recent_logs.length > 0) {
        logView.innerHTML = s.recent_logs.map(l =>
          '<span class="dim">' + escapeHtml(l) + '</span>'
        ).join('\n');
      }

      // 运行时长
      if (s.started_at && window._lastStarted !== s.started_at) {
        window._lastStarted = s.started_at;
        startedAt = new Date(s.started_at);
      }
      if (startedAt) {
        const sec = Math.floor((Date.now() - startedAt) / 1000);
        const h = Math.floor(sec / 3600);
        const m = Math.floor((sec % 3600) / 60);
        const s2 = sec % 60;
        document.getElementById('uptime').textContent =
          `已运行 ${h}h ${m}m ${s2}s`;
      }
    } catch(e) {
      console.error('status fetch error:', e);
    }
  }

  function escapeHtml(s) {
    return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  }

  function showConfirm() {
    document.getElementById('confirm-overlay').classList.add('show');
  }
  function hideConfirm() {
    document.getElementById('confirm-overlay').classList.remove('show');
  }

  async function doShutdown() {
    try {
      await fetch('/api/shutdown', {method:'POST'});
      document.getElementById('btn-shutdown').textContent = '⏳ 正在关闭...';
      document.getElementById('btn-shutdown').disabled = true;
      setTimeout(() => location.reload(), 3000);
    } catch(e) {
      alert('关闭请求失败: ' + e);
    }
  }

  refresh();
  setInterval(refresh, 3000);
</script>
</body>
</html>"""


class DashboardHandler(BaseHTTPRequestHandler):
    """HTTP 请求处理器。"""

    def log_message(self, format, *args):
        """抑制默认日志，改用项目日志。"""
        log.debug("HTTP %s", args[0] if args else format)

    def _send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html, status=200):
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            self._send_html(_HTML_TEMPLATE)
        elif self.path == "/api/status":
            self._send_json(get_state())
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/api/shutdown":
            add_log_line("⚠️ 收到来自 Web 仪表盘的关闭请求")
            log.info("收到来自 Web 仪表盘的关闭请求")
            if trigger_shutdown():
                self._send_json({"ok": True, "message": "正在关闭..."})
            else:
                self._send_json({"ok": False, "message": "关闭信号未配置"}, 500)
        else:
            self.send_response(404)
            self.end_headers()


# ============================================================
# 服务管理
# ============================================================

_server: HTTPServer = None
_server_thread: threading.Thread = None


def start_server(host: str = "127.0.0.1", port: int = 9528) -> bool:
    """启动 HTTP 仪表盘服务（在后台线程中运行）。"""
    global _server, _server_thread

    if _server_thread is not None and _server_thread.is_alive():
        log.warning("仪表盘已在运行")
        return True

    try:
        _server = HTTPServer((host, port), DashboardHandler)
    except OSError as e:
        log.error("无法启动仪表盘 http://%s:%s — %s", host, port, e)
        return False

    _server_thread = threading.Thread(
        target=_server.serve_forever,
        daemon=True,
        name="dashboard",
    )
    _server_thread.start()
    log.info("仪表盘已启动: http://%s:%s", host, port)
    add_log_line(f"✅ 仪表盘已启动: http://{host}:{port}")
    return True


def stop_server():
    """停止 HTTP 仪表盘服务。"""
    global _server, _server_thread
    if _server:
        _server.shutdown()
        _server = None
    _server_thread = None
    log.info("仪表盘已停止")
