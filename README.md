# AstrBot Plugin: DSH Bridge

让 **AstrBot 的智能体可以直接操纵 DeepSeek Harness（DSH）的智能体**：

- AstrBot 主智能体获得两个函数工具 —— `dsh_ask`（把任务投递给 DSH 智能体并取回最终回复）和 `dsh_ping`（连通性检测）；
- 插件通过 `on_llm_request` 钩子向主智能体的系统提示词**自动注入使用说明**（可开关、可自定义）；
- 自动**扫描 127.0.0.1 端口**发现 DSH Web 服务（默认端口 59934 优先，失败则全量扫描回环端口），无需在 DSH 侧安装任何东西；
- 适配不同设备：Windows / Linux / macOS / Docker 通用；AstrBot 与 DSH 不在同一台机器时，把 `dsh_host` 配成 DSH 所在设备的局域网 IP 即可。

```
┌──────────────┐   llm_tool 调用   ┌──────────────────────┐   HTTP/JSON (无鉴权)   ┌──────────────────────┐
│  AstrBot 智能体 │ ───────────────▶ │  dsh_bridge 插件      │ ─────────────────────▶ │  DeepSeek Harness     │
│  (聊天/群聊)    │ ◀─────────────── │  端口扫描+会话投递    │ ◀───────────────────── │  (127.0.0.1:59934)    │
└──────────────┘   prompt 注入       └──────────────────────┘   session.prompt 等    └──────────────────────┘
```

---

## 一、环境要求

- **AstrBot ≥ 4.16**（本插件基于 AstrBot 4.26 规范开发，`astrbot_version: ">=4.16.0"`）
- **DeepSeek Harness** 正在运行（DSH Desktop 桌面版，或 `dsh web` 命令行版）
- 零额外依赖：插件只用 Python 标准库 + `aiohttp`（AstrBot 核心自带）

## 二、安装

### 方式 A：通过 AstrBot Launcher（推荐）

1. 打开 **AstrBot Launcher**，进入对应实例的 **Dashboard（Web 控制台）**；
2. 左侧菜单进入 **插件市场 / 插件管理**；
3. 点击「**安装插件**」→ 选择「**从本地文件安装**」→ 选择随本插件分发的
   **`astrbot_plugin_dsh_bridge.zip`**（或本目录）；
4. 安装后点击 **启用**，在插件设置页确认配置（见第三节）。

### 方式 B：手动放置目录

把整个 `astrbot_plugin_dsh_bridge` 目录复制到 AstrBot 数据目录的插件目录下，然后重启 AstrBot：

| 环境 | 插件目录 |
| --- | --- |
| 本机（AstrBot Launcher 实例） | `C:\Users\Admin\.astrbot_launcher\instances\<实例ID>\core\data\plugins\` |
| 常规安装 | `<AstrBot 工作目录>/data/plugins/` |
| Docker | 容器内 `/AstrBot/data/plugins/`（或映射卷对应目录） |

### 方式 C：源码安装（开发者）

```bash
cd data/plugins
git clone https://github.com/Fantasality/astrbot_plugin_dsh_bridge.git
```

## 三、配置（插件设置页，均可改后即时生效）

| 配置项 | 默认 | 说明 |
| --- | --- | --- |
| `dsh_host` | `127.0.0.1` | DSH 所在主机；跨设备时填局域网 IP |
| `dsh_port` | `0` | 手动指定 DSH 端口；`0` = 自动扫描 |
| `port_ranges` | `59900-60100,49152-60000,32768-60999` | 自动扫描区间（默认端口 59934 总是优先） |
| `scan_concurrency` | `256` | 扫描并发，弱机调低 |
| `cache_ttl_seconds` | `300` | 端口发现结果缓存时长 |
| `request_timeout_seconds` | `900` | 单个任务执行超时，超时自动取消 |
| `poll_interval_seconds` | `2.0` | 结果轮询间隔 |
| `max_result_chars` | `6000` | 返回文本截断长度，防撑爆上下文 |
| `default_cwd` | 空 | 新建 DSH 会话的工作目录；空 = DSH 默认 |
| `session_mode` | `new` | `new` 每次新建会话；`persistent` 复用固定会话 |
| `persistent_session_id` | 空 | `persistent` 模式复用的会话 ID |
| `inject_prompt_enabled` | `true` | 是否向主智能体注入使用说明 |
| `inject_prompt_text` | 内置模板 | 注入的提示词内容，可完全自定义 |

## 四、使用

### 管理员命令（需在消息平台触发 AstrBot 唤醒后输入）

```
/dsh help                   查看帮助
/dsh ping                   检测 DSH 连接状态
/dsh status                 连接状态 + 配置摘要
/dsh scan                   强制重新扫描端口（改过配置后可用）
/dsh ask <任务描述>          把任务交给 DSH 智能体执行，返回最终回复
/dsh session <会话ID> <任务> 在指定 DSH 会话中执行任务
```

示例：
```
/dsh ask 帮我在电脑上查一下 C 盘剩余空间，并生成一份磁盘使用报告
```
插件会先回复「📡 已接入 DSH 会话 …」，DSH 智能体执行完后返回其最终回复。

### 让 AstrBot 主智能体自动调用

插件启用后，主智能体每次 LLM 请求都会自动携带注入的提示词，并拥有 `dsh_ask` / `dsh_ping`
两个工具。你只需正常说话，例如：

- 「问一下我电脑上的智能体，帮我整理桌面文件」
- 「让 DSH 跑一个脚本，统计这个项目代码行数」
- 「电脑上的智能体在吗？」（触发 `dsh_ping`）

主智能体会自行决定何时调用工具，并把 DSH 的回复转述给你。

## 五、工作原理（给好奇的人）

1. **端口发现**：DSH Desktop 默认把 Web 服务绑在 `127.0.0.1:59934`（被占用则回退到系统随机端口）。
   插件先探测 59934，再按配置区间并发扫描回环端口；对每个端口先做 TCP 连通预筛，再 GET `/`
   检查页面标题是否含 `DeepSeek Harness` / 是否含 `__DSH_BOOT__` 指纹标记。
2. **调用**：向 DSH 的 `/api/<method>` 发送 JSON 信封
   `{"type":"client-request","rpcId":"<uuid>","method":"...","payload":{...}}`：
   - `session.create` → 新建会话（默认每次任务一个独立会话）
   - `session.prompt` → 投递任务（`mode: "queue"`）
   - `session.history` 轮询 → 直到出现 `turn/end` 事件，从 `assistant/message` 事件中取最终文本
   - 超时 → `session.cancel` 取消并报错
3. **Prompt 注入**：`on_llm_request` 钩子在每次 LLM 请求的 `system_prompt` 末尾追加工具使用说明。

> 该 API 仅绑定回环地址且无鉴权，因此插件无需任何密钥即可驱动本机 DSH。

## 六、常见问题

| 现象 | 处理 |
| --- | --- |
| `/dsh ping` 提示未发现 DSH | 1) 确认 DSH Desktop 已打开；2) 看 DSH 窗口地址栏端口，填入 `dsh_port`；3) 或执行 `/dsh scan` 强制重扫 |
| 扫描很慢 | 调小 `port_ranges`（只留 `59900-60100`），或直接填 `dsh_port` 跳过扫描 |
| AstrBot 在服务器/Docker，DSH 在台式机 | `dsh_host` 填台式机局域网 IP；确认防火墙放行该端口 |
| 任务超时被取消 | 调大 `request_timeout_seconds`；长任务建议拆分成小任务 |
| 返回内容过长 | 调大 `max_result_chars`（注意上下文占用） |
| 工具报「DSH 拒绝了 session.prompt」 | 多为会话已关闭，切回 `session_mode: new` 或更换会话 ID |

## 七、安全说明

- DSH 的本地 API 无鉴权但**仅绑定 127.0.0.1**；请勿把 `dsh_host` 暴露到公网。
- `/dsh` 系列命令仅 **管理员** 可用（`permission_type(ADMIN)`）；`dsh_ask` 工具仅 AstrBot 智能体
  能触发，且每个任务都是独立 DSH 会话，任务间互不干扰。
- DSH 智能体拥有本机工具（命令执行、文件读写等），本质上等同于授权 AstrBot 代为操作电脑，
  请仅在可信的聊天环境启用本插件。

## 八、文件清单

```
astrbot_plugin_dsh_bridge/
├── metadata.yaml        # 插件元数据（name/desc/author/version/astrbot_version）
├── main.py              # Star 主类：/dsh 命令 + dsh_ask/dsh_ping 工具 + prompt 注入
├── dsh_client.py        # DSH API 客户端：端口发现、RPC、任务投递与轮询
├── _conf_schema.json    # 插件配置面板 schema
└── README.md            # 本文档
```

## 九、Changelog

- **1.0.0** 首个版本：端口自动发现、`/dsh` 命令组、`dsh_ask`/`dsh_ping` LLM 工具、prompt 注入、
  跨设备 host 配置、任务超时自动取消、结果截断。
