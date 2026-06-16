# 飞书个人助手 (Feishu Personal Agent)

7×24 运行的飞书机器人，云端常驻 + 物理机按需联动，实现待办管理 / 灵感记录 / 持久记忆 / AI 对话 / 远程文件操作。

> **当前版本：v2.1（云端常驻 + PC 桥接 + Web 仪表盘 + DeepSeek 驱动）**

---

## 架构

```
手机飞书 ──WebSocket──▶ 云端 Ubuntu 122.51.207.16 (7×24)
                            │
                            ├── agent.py (主入口)
                            ├── claude_daemon.py ──▶ DeepSeek v4-pro (Anthropic 兼容端点)
                            ├── message_handler.py (多层路由)
                            ├── todo_manager.py (待办 CRUD + APScheduler 提醒)
                            ├── memory_manager.py (跨会话持久记忆)
                            ├── inspiration_manager.py (灵感记录)
                            └── pc_bridge.py (WebSocket Server :9527)
                                  │
                    ┌─────────────┘
                    │ WebSocket (PC 主动连云端，30s 心跳)
                    │
              ┌─────┴─────┐
              │  你的物理机  │
              │  pc_agent.py │──▶ 本地 Claude ──▶ DeepSeek v4-pro
              │  (后台静默)  │    (文件操作专用)
              │              │
              │  dashboard.py│──▶ http://localhost:9528 (实时 Web 仪表盘)
              └─────────────┘
```

### 消息流向

```
用户消息
  │
  ├─ 显式命令 (/帮助 /状态 /待办)    → 同步返回（毫秒级）
  ├─ 记忆/灵感管理                    → 同步返回（毫秒级）
  ├─ 本地文件操作 (D:/ C:/ 等)       → pc_bridge → 物理机 Claude → 回传
  └─ 通用对话                        → 云端 Claude daemon (热启动，1~5s)
```

### 云端 vs 物理机职责

| 功能 | 运行位置 | 说明 |
|------|----------|------|
| 飞书 WebSocket | 云端 | 7×24，不依赖物理机 |
| Claude 通用对话 | 云端 | DeepSeek-v4-pro |
| 待办管理 | 云端 | JSON 文件操作 + APScheduler 提醒 |
| 持久记忆 | 云端 | 自动注入 Claude prompt |
| 灵感记录 | 云端 | 飞书口述→云端存储→PC 同步 |
| 本地文件操作 | 物理机 | D:/ C:/ E:/ 等本地路径 |
| 灵感同步到桌面 | 物理机 | 连接时自动拉取 |
| Web 仪表盘 | 物理机 | localhost:9528 实时状态 + 关闭入口 |

---

## 目录结构

### 云端 `/home/feishu/`

```
app/feishu-agent/          # 应用代码
├── agent.py               # 主入口：WebSocket + daemon + PC bridge
├── claude_daemon.py       # Claude 常驻进程（热启动核心）
├── config.py              # 凭证 + 路径 + DeepSeek 端点
├── inspiration_manager.py # 灵感 CRUD + PC 同步标记
├── llm_bridge.py          # Claude 桥接层（注入记忆 + 时间）
├── logger_setup.py        # RotatingFileHandler
├── memory_manager.py      # 持久记忆 CRUD + 自动裁剪
├── message_handler.py     # 多层消息路由
├── pc_bridge.py           # PC WebSocket 服务端 + 心跳管理
├── todo_manager.py        # 待办 CRUD + APScheduler
├── requirements.txt
├── run.sh                 # 启动脚本
└── .venv/                 # Python 虚拟环境

app_data/feishu-agent/     # 运行时数据
├── agent.log
├── todo_store.json
├── memory_store.json
└── inspiration_store.json
```

### 物理机 `D:\app\`

```
feishu-agent-pc/           # PC 代理
├── pc_agent.py            # 主程序（WebSocket 客户端 + Claude 调用 + 灵感同步）
├── dashboard.py           # Web 仪表盘（HTTP 服务 + 共享状态 + 关闭信号）
├── run_pc.vbs             # 静默启动（双击，无终端窗口）
├── run_pc.bat             # 调试启动（保留终端输出，备用）
├── stop_pc.bat            # 关闭脚本（API 优雅关闭 → 强制终止回退）
├── create_shortcuts.ps1   # 桌面快捷方式生成
└── pc_agent.log           # 运行日志

feishu-agent/              # 本地备份 + Claude daemon 模块
└── ...
```

---

## 快速开始

### 1. 环境要求

- 云端：Ubuntu 24.04 + 4 核 + 4GB RAM
- 物理机：Windows 10/11 + Python 3.12 + Claude Code CLI
- 飞书企业自建应用（已配置）

### 2. 云端初始化

```bash
# SSH 登录
ssh root@122.51.207.16

# 安装依赖
apt-get install -y nodejs python3-pip python3.12-venv
useradd -m feishu

# 配置 DeepSeek（关键！）
cat > /home/feishu/.claude/settings.json << 'EOF'
{
  "env": {
    "ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic",
    "ANTHROPIC_AUTH_TOKEN": "sk-你的DeepSeek-API-Key",
    "ANTHROPIC_MODEL": "DeepSeek-v4-pro",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "DeepSeek-v4-pro",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "DeepSeek-v4-pro",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "DeepSeek-v4-pro"
  },
  "skipDangerousModePermissionPrompt": true
}
EOF
chown feishu:feishu /home/feishu/.claude/settings.json
```

### 3. 部署代码

```bash
# 从本地推送代码到云端
scp -r D:\app\feishu-agent\* root@122.51.207.16:/home/feishu/app/feishu-agent/
```

### 4. 启动云端服务

```bash
ssh root@122.51.207.16
su - feishu -c "nohup /home/feishu/app/feishu-agent/run.sh > /dev/null 2>&1 &"
```

启动日志示例：

```
[INFO] 飞书个人助手 启动中... (云端版)
[INFO] 待办提醒引擎就绪
[INFO] 正在启动 Claude 常驻进程…
[INFO] daemon init: model=DeepSeek-v4-pro
[INFO] Claude daemon 已就绪 (pid=17839, model=DeepSeek-v4-pro)
[INFO] PC Bridge 监听 ws://0.0.0.0:9527
[INFO] WebSocket 连接中…
[INFO] 功能状态: 待办✅ 记忆✅ 灵感✅ Claude✅ PC桥接✅
```

### 5. 启动物理机代理

双击桌面 **PC Agent** 快捷方式（静默后台启动），然后浏览器打开 `http://localhost:9528` 查看实时状态。

或者调试模式（带终端输出）：

```bat
D:\app\feishu-agent-pc\run_pc.bat
```

启动后飞书发 `/状态`，物理机状态会从 `⚫ 离线` 变为 `🟢 在线`。

---

## PC Agent Web 仪表盘

访问 `http://localhost:9528`，实时展示：

| 模块 | 内容 |
|------|------|
| 连接状态 | 🟢/🔴 云端 WebSocket 连接、心跳时间 |
| Claude 状态 | daemon / subprocess 回退模式 |
| 灵感同步 | 已同步数量 + 上次同步时间 |
| 最近请求 | 云端发来的文件操作请求（截断显示） |
| 实时日志 | 最近 50 条日志，自动滚动 |
| 关闭服务 | 按钮确认后优雅关闭（HTTP POST → 断开连接 → 退出进程） |

页面每 3 秒自动刷新，无需手动操作。

### 关闭 PC Agent

三种等价方式：

1. 仪表盘网页点 **⏹️ 关闭服务**
2. 桌面双击 **Stop PC Agent**
3. 终端 `Ctrl+C`（调试模式下）

关闭流程：API 请求 → threading.Event 信号 → WebSocket 循环退出 → Claude daemon 停止 → HTTP 服务停止 → 进程退出。2 秒内完成。

---

## 命令参考

| 输入 | 说明 | 响应速度 |
|------|------|----------|
| `/帮助` | 使用指南 | 毫秒级 |
| `/状态` | 运行状态 + 记忆/待办/灵感/PC 在线状态 | 毫秒级 |
| `/待办` | 查看待办列表 | 毫秒级 |
| `/待办 添加 明天上午9点开会` | 添加待办（自然语言时间） | 3~6s |
| `/待办 完成 3` | 标记完成 | 毫秒级 |
| `/待办 删除 3` | 删除 | 毫秒级 |
| `/文件 查看 D:/projects/readme.md` | 文件操作（需 PC 在线） | 2~8s |
| `记住 我喜欢咖啡` | 保存持久记忆 | 毫秒级 |
| `你记得什么` | 查看所有记忆 | 毫秒级 |
| `忘了 咖啡` | 删除匹配记忆 | 毫秒级 |
| `灵感 写一个关于猫的游戏` | 保存灵感 | 毫秒级 |
| `灵感` | 查看最近灵感 | 毫秒级 |
| `整理灵感` | Claude 总结归类灵感 | 3~5s |
| `今天天气怎么样` | 通用对话 | 1~5s |
| `你好` | 问候 | 毫秒级 |

---

## 消息路由流程

```
飞书消息到达
  │
  ├─ 第 1 层：显式命令（同步返回，< 10ms）
  │   /帮助 /状态 /待办(CRUD)  /文件
  │
  ├─ 第 2 层：关键词快速判断（同步返回，< 1ms）
  │   灵感添加/查看/删除 → inspiration_manager
  │   记忆保存/删除/召回 → memory_manager
  │   本地路径 (D:/ C:/) → pc_bridge → 物理机
  │   文件关键词（无本地路径）→ 云端 Claude
  │   待办关键词 → 同步处理
  │   简短问候 → 同步友好回复
  │
  └─ 第 3 层：兜底 → 云端 Claude daemon（异步，1~5s）
      任何未被上层捕获的消息
      → call_claude() → daemon.send()
        ├── 注入持久记忆 + 当前时间
        └── 写入 daemon stdin → 等待 stdout result
```

---

## PC 桥接协议

云端 `pc_bridge.py` 与物理机 `pc_agent.py` 之间的通信：

```
PC → Cloud (30s)
  {"type":"heartbeat"}

Cloud → PC
  {"type":"heartbeat_ack"}

Cloud → PC (文件操作请求)
  {"type":"request","request_id":"uuid","prompt":"查看 D:/test.py"}

PC → Cloud (结果回传)
  {"type":"response","request_id":"uuid","result":"文件内容..."}

PC → Cloud (首次连接)
  {"type":"sync_request"}  →  {"type":"sync_data","inspirations":[...]}
```

- 心跳：30s 间隔，90s 超时判定离线
- 开销：每分钟 ~4 字节流量，CPU 忽略不计
- 关闭信号：`threading.Event` 跨线程通知，`asyncio.wait_for(ws.recv(), timeout=1.0)` 保证 1 秒内响应关闭

---

## 关键设计决策

### DeepSeek 驱动（v2.0）
Claude Code 原生只支持 Anthropic API，但通过设置 `ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic` 将请求路由到 DeepSeek 的 Anthropic 兼容端点。无需 Codex/桥接层，一套环境变量即可全村吃饭。

### 云端常驻（v2.0）
飞书 WebSocket + Claude daemon 部署在云服务器，7×24 运行。待办、记忆、灵感、通用对话不依赖物理机。物理机关机不影响基础功能。

### PC 按需联动（v2.0）
涉及本地文件路径（D:/ C:/ 等）的操作通过 pc_bridge 转发到物理机执行。物理机未开机时返回友好提示，不影响其他功能。

### PC Agent 后台静默 + Web 仪表盘（v2.1）
物理机代理通过 `pythonw.exe` 启动，无终端窗口。所有状态信息通过本地 Web 仪表盘 (`localhost:9528`) 呈现，支持一键关闭。关闭信号通过 `threading.Event` 跨线程传递，1 秒内优雅退出。

### 灵感记录 + 自动同步（v2.0）
灵感通过飞书口述保存到云端，物理机 pc_agent 连接时自动拉取同步到桌面 `灵感记录.md`。

### 热启动 daemon（v1.0，沿用至今）
云端和物理机各维护一个 Claude 常驻进程（stream-json over stdin/stdout）。首条消息后上下文由进程原生维护，后续消息为零进程开销，延迟 = 纯推理时间。

### 3 秒 ACK 规则
飞书要求事件处理器 3 秒内确认。所有 Claude 调用在后台线程执行，主线程即刻返回。

### 三重消息去重
message_id（5min TTL）+ 内容指纹 SHA256（60s 窗口）+ 快速 ACK，防止飞书重推导致重复回复。

### 持久记忆 + 自动注入
跨会话记忆存储在 memory_store.json，每次 Claude 调用自动注入到 prompt。用户类型自动分类（偏好/身份/项目/事实），50 条上限自动裁剪。

### 纯关键词意图判断
不调 LLM，全部基于关键词匹配（毫秒级）。意图判断零延迟。

---

## 维护

### 查看云端日志

```bash
ssh root@122.51.207.16
tail -f /home/feishu/app_data/feishu-agent/agent.log
```

### 重启云端

```bash
ssh root@122.51.207.16
pkill -f "python3 agent.py"
su - feishu -c "nohup /home/feishu/app/feishu-agent/run.sh > /dev/null 2>&1 &"
```

### 查看 PC 代理状态

浏览器打开 `http://localhost:9528`，实时状态 + 最近日志一目了然。

或直接查看日志文件：`D:\app\feishu-agent-pc\pc_agent.log`

---

## FAQ

**Q: 飞书收不到消息**
A: 确认云端正运行 (`ps aux | grep agent.py`)，飞书开放平台事件订阅配置已保存。

**Q: /状态 物理机始终离线**
A: PC 未启动（双击桌面 PC Agent），或云防火墙 9527 端口未开放（腾讯云安全组需放行 TCP 9527）。

**Q: "Claude 进程未启动"**
A: DeepSeek API Key 未配置或余额不足。检查 `/home/feishu/.claude/settings.json` 中的 `ANTHROPIC_AUTH_TOKEN`。

**Q: 能不能在物理机不开机时也能文件操作？**
A: 不能，本地磁盘只有物理机能访问。但待办、记忆、灵感、通用对话不受影响。

**Q: 灵感记录什么时候同步到桌面？**
A: pc_agent 每次连接云端时自动同步。云端灵感新增 / 桌面上次同步时间戳之前的都会拉取。

**Q: 如何确认 PC Agent 是否在运行？**
A: 浏览器打开 `http://localhost:9528`，仪表盘显示实时状态。或者检查任务管理器 `pythonw.exe` 进程。

**Q: 云端费用多少？**
A: 腾讯云轻量应用服务器 ~50 元/月。DeepSeek API 按量计费，个人使用量很低（日常对话一个月几块钱）。

---

## 版本历史

| 版本 | 日期 | 变更 |
|------|------|------|
| v2.1 | 2026-06-16 | PC Agent 后台静默启动，Web 仪表盘 (localhost:9528)，一键关闭，关闭脚本 |
| v2.0 | 2026-06-16 | 云端常驻架构，PC 桥接，灵感记录，DeepSeek 驱动 |
| v1.0 | 2026-06-15 | 热启动 claude daemon，三重去重，持久记忆，权限跳过 |
| v0.1 | 2026-06 初 | 冷启动 subprocess，基础 WebSocket 连接 |
