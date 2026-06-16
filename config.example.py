"""
飞书个人助手 — 配置文件（模板）
复制为 config.py 并填入真实凭证。

在飞书开放平台 https://open.feishu.cn/ 创建企业自建应用后，
将 App ID 和 App Secret 填入下方。
"""

# ============================================================
# 飞书应用凭证（必填！）
# ============================================================
APP_ID = "cli_xxxxxxxxxxxx"             # 替换为你的 App ID
APP_SECRET = "xxxxxxxxxxxxxxxx"         # 替换为你的 App Secret

# ============================================================
# WebSocket 长连接
# ============================================================
WS_ENDPOINT = "wss://open.feishu.cn/event"
HEARTBEAT_INTERVAL = 30                 # 心跳间隔（秒）

# ============================================================
# Claude Code CLI 路径
# ============================================================
# 云端 Linux 示例:
# CLAUDE_CLI_PATH = "/usr/bin/claude"
# CLAUDE_WORK_DIR = "/home/feishu"

# 本地 Windows 示例:
CLAUDE_CLI_PATH = (
    r"C:\Users\YourName\AppData\Roaming\npm"
    r"\node_modules\@anthropic-ai\claude-code\bin\claude.exe"
)
CLAUDE_TIMEOUT = 120                    # 单次调用超时（秒）
CLAUDE_WORK_DIR = r"C:\Users\YourName"

# ============================================================
# 文件路径
# ============================================================
import os
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))

# 云端: 数据放 /home/feishu/app_data/feishu-agent/
# 本地: 数据放项目目录
DATA_DIR = os.environ.get("FEISHU_AGENT_DATA_DIR", PROJECT_DIR)
TODO_FILE         = os.path.join(DATA_DIR, "todo_store.json")
CONVERSATION_FILE = os.path.join(DATA_DIR, "conversation_store.json")
MEMORY_FILE       = os.path.join(DATA_DIR, "memory_store.json")
INSPIRATION_FILE  = os.path.join(DATA_DIR, "inspiration_store.json")
LOG_FILE          = os.path.join(DATA_DIR, "agent.log")
LOG_MAX_BYTES = 5 * 1024 * 1024         # 日志单文件 5MB
LOG_BACKUP_COUNT = 3                    # 保留最近 3 个备份

# ============================================================
# PC Bridge（云端 WebSocket 服务端）
# ============================================================
PC_BRIDGE_HOST = "0.0.0.0"
PC_BRIDGE_PORT = 9527
PC_BRIDGE_HEARTBEAT_INTERVAL = 30
PC_BRIDGE_HEARTBEAT_TIMEOUT = 90

# ============================================================
# Claude Code CLI 行为
# ============================================================
CLAUDE_EXTRA_FLAGS = ["--dangerously-skip-permissions"]
MAX_CONVERSATION_HISTORY = 20
MAX_MEMORY_ITEMS = 50
CLAUDE_DAEMON_TIMEOUT = 180

# ============================================================
# 时区 & 提醒
# ============================================================
from zoneinfo import ZoneInfo
TIMEZONE = "Asia/Shanghai"
TZ = ZoneInfo("Asia/Shanghai")
REMINDER_CHECK_SECONDS = 60
