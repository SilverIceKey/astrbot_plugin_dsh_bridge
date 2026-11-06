# AstrBot Plugin: DSH Bridge

让 **AstrBot 的智能体可以直接操纵 DeepSeek Harness（DSH）的智能体**（同时兼容 **DSH 0.2.2-rc.1+ 新版**与**旧版无认证 DSH**）：

- AstrBot 主智能体获得两个函数工具 —— `dsh_ask`（把任务投递给 DSH 智能体并取回最终回复）和 `dsh_ping`（连通性检测）；
- 插件通过 `on_llm_request` 钩子向主智能体的系统提示词**自动注入使用说明**（可开关、可自定义）；
- 自动**扫描回环端口**发现 DSH Web 服务（DSH 0.2.x Desktop 使用随机临时端口，默认扫描 `32768-65535`），也可用 `dsh_url` 直接固定地址；
- **自动识别协议**：新版 DSH 走 Cookie 认证 + `session/*` 斜杠接口 + `session/wait`；旧版走无认证的点号接口 + `session.history` 轮询；
- 适配不同设备：Windows / Linux / macOS / Docker 通用。

```
┌──────────────┐   llm_tool 调用   ┌──────────────────────┐   HTTP/JSON（Cookie 认证）   ┌──────────────────────┐
│  AstrBot 智能体 │ ───────────────▶ │  dsh_bridge 插件      │ ─────────────────────────▶ │  DeepSeek Harness     │
│  (聊天/群聊)    │ ◀─────────────── │  端口扫描+会话投递    │ ◀───────────────────────── │  (127.0.0.1:<随机端口>) │
└──────────────┘   prompt 注入       └──────────────────────┘   session/* 接口            └──────────────────────┘
```

> **兼容性说明**：DSH 0.2.x 起把本地 API 从「无鉴权」改为「必须浏览器会话认证」，并把 RPC 方法名从 `session.create` 这类点号命名改为 `session/create` 斜杠命名、参数改为命名参数、完成判定改为 `session/wait` 事件等待。插件 1.1.1 会**自动识别新旧协议**：新版走认证 + 斜杠接口，旧版走无认证 + 点号接口，两种环境都能正常使用。
>
> 历史版本注意：1.1.0 只支持新版 DSH（旧版环境会出现「基础检测也失效」），1.0.0 只支持旧版；请使用 1.1.2+。

---

## 一、环境要求

- **AstrBot ≥ 4.16**（本插件基于 AstrBot 4.26 规范开发，`astrbot_version: ">=4.16.0"`）
- **DeepSeek Harness** 正在运行（DSH Desktop 桌面版，或 `dsh web` 命令行版；新版/旧版均可）
- 插件只用 Python 标准库 + `aiohttp`（AstrBot 核心自带）；读取 YAML 凭据时优先用 PyYAML，缺失时自动降级为内置解析
- **新版 DSH 的本机认证方式要求 AstrBot 与 DSH 以同一操作系统用户运行**（插件需要读取 DSH 数据目录下的 `.credentials.yaml`）；旧版 DSH 无需认证

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
git clone https://github.com/SilverIceKey/astrbot_plugin_dsh_bridge.git
```

## 三、认证说明（仅新版 DSH 需要）

插件会自动识别 DSH 版本：旧版（无认证、点号接口）直接使用兼容模式；新版（0.2.x）按以下顺序完成认证：

1. **本机凭据文件（默认，推荐）**
   插件读取 DSH 数据目录下的 `.credentials.yaml`，取
   `records["client-connection/browser-session"].payload.secret` 作为 HMAC 签名密钥，
   自行签发与 `host:port` 绑定的浏览器会话 Cookie 去调用 `/api`。
   - 数据目录解析顺序：`dsh_home` 配置 → 环境变量 `DSH_HOME` → `~/.dsh`
   - 该 Cookie 由 DSH 的持久密钥签名，**DSH 重启后依然有效**
   - 需要 AstrBot 与 DSH 同用户、同机器（或至少能读到该文件）

2. **启动令牌（跨设备 / 读不到凭据文件时）**
   把 `dsh web` 启动输出里带 `?token=...` 的 **完整 URL** 填到 `dsh_url`（或只把 token 填到 `dsh_token`），
   插件会调用 `GET /connect?token=...&interactions=none` 换取 API Cookie。
   - 令牌**每次 DSH 重启都会变化**，需要重新填写
   - 跨设备时 DSH 需以 `--trusted-host <本插件访问用的 host:port>` 启动，否则 `/api` 返回 403

3. **两条都不可用**
   `/dsh ping` 会明确提示缺失的凭据文件路径或跨设备配置方法。

> 安全提示：本机凭据文件里的签名密钥等价于本机 DSH 的完整控制权。请只在可信环境安装本插件，
> 不要把 AstrBot 或 DSH 的端口暴露到公网。

## 四、配置（插件设置页，均可改后即时生效）

| 配置项 | 默认 | 说明 |
| --- | --- | --- |
| `dsh_host` | `127.0.0.1` | DSH 所在主机；跨设备时填局域网 IP |
| `dsh_url` | 空 | 直接指定 DSH 地址（可带 `?token=`），跳过扫描；**最省事的固定方式** |
| `dsh_port` | `0` | 手动指定 DSH 端口；`0` = 自动扫描 |
| `port_ranges` | `32768-65535` | 自动扫描区间（覆盖 Windows/Linux 临时端口段；1.0.0 旧默认会自动迁移） |
| `scan_concurrency` | `256` | 扫描并发，弱机调低（如 64） |
| `port_probe_timeout_seconds` | `0.5` | 单端口 TCP 探测超时（太小会漏检） |
| `cache_ttl_seconds` | `300` | 端口发现结果缓存时长 |
| `dsh_home` | 空 | DSH 数据目录（含 `.credentials.yaml`），留空用 `DSH_HOME` 或 `~/.dsh` |
| `dsh_token` | 空 | `dsh web` 启动 URL 中的 token（跨设备/无凭据文件时使用） |
| `request_timeout_seconds` | `900` | 单个任务执行超时，超时自动取消 |
| `poll_interval_seconds` | `2.0` | 兼容项：`session/wait` 已事件驱动，仅用于投递瞬间的空闲误判重试 |
| `max_result_chars` | `6000` | 返回文本截断长度，防撑爆上下文 |
| `default_cwd` | 空 | 新建 DSH 会话的工作目录；空 = DSH 默认 |
| `session_mode` | `new` | `new` 每次新建会话；`persistent` 复用固定会话 |
| `persistent_session_id` | 空 | `persistent` 模式复用的会话 ID |
| `inject_prompt_enabled` | `true` | 是否向主智能体注入使用说明 |
| `inject_prompt_text` | 内置模板 | 注入的提示词内容，可完全自定义 |

## 五、使用

### 管理员命令（需在消息平台触发 AstrBot 唤醒后输入）

```
/dsh help                   查看帮助
/dsh ping                   检测 DSH 连接状态（含 DSH 版本、认证方式）
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

### 审批（approval）注意事项

DSH 默认权限预设会在执行写文件、跑命令等操作前**请求用户确认**。桥接插件无法替用户点「允许」，
此时 `/dsh ask` 会返回「DSH 正在等待用户确认/回答」，并保留该轮任务：

- 到 DSH 窗口处理审批后，可再次 `/dsh ask`，或直接用 `/dsh session <会话ID> <任务>` 继续该会话；
- 无人值守场景，可在 DSH 设置里把默认权限预设调整为不需要询问的模式（例如 `danger-full-access`，
  其审批策略为 `never`）；这相当于把整台电脑交给该智能体，请自行评估风险。

## 六、工作原理（给好奇的人）

1. **端口发现**：DSH 0.2.x Desktop 以 `--port 0` 启动，Web 端口是系统随机临时端口。
   插件先尝试 `dsh_url` / `dsh_port` / `DSH_WEB_URL` / 历史默认端口 `59934`，再按 `32768-65535`
   并发扫描：对每个端口先做 TCP 连通预筛，再 `GET /` 检查指纹（自动跟随重定向）——
   新版 DSH 未认证时返回 401 文本 `dsh web authentication required; ...`；
   旧版 DSH 直接返回含 `DeepSeek Harness` / `__DSH_BOOT__` 的 index.html。
   指纹命中后：
   - 401 认证提示 → 直接认定为 DSH 0.2.x API；
   - 200 页面标记 → 再校验一次 `/api` 是否真的返回 JSON-RPC（跳过前端静态页/开发服务器/反向代理 SPA fallback 这类“长得像 DSH 但不是 API”的假候选）。
2. **协议识别**：先用新版的 `session/list` 探测一次；成功即新版，失败且旧版 `session.list` 成功即旧版。
   `dsh_ping` / `dsh_status` 会显示当前是「新版 0.2.x」还是「旧版兼容模式」。
3. **认证（仅新版）**：本机默认用 `.credentials.yaml` 中的 `browser-session` 签名密钥
   （32 字节 base64url）签发 `dsh-auth-<authority 哈希>` Cookie：
   `v1.<payload>.<HMAC-SHA256>`，payload 含 `authority` / `issuedAt` / `expiresAt`。
4. **调用**：
   - 新版：向 `/api/<method>` 发送 `{"type":"client-request",...,"payload":{"args":{...}}}`，
     流程 `session/create` → `session/state` → `session/prompt` → `session/wait`
     （超时 `session/cancel`）；
   - 旧版：发送扁平的 `payload`，流程 `session.create` → `session.prompt` →
     轮询 `session.history` 直到 `turn/end`（超时 `session.cancel`）。
5. **Prompt 注入**：`on_llm_request` 钩子在每次 LLM 请求的 `system_prompt` 末尾追加工具使用说明。

## 七、常见问题

| 现象 | 处理 |
| --- | --- |
| `/dsh ping` 提示未发现 DSH | 1) 确认 DSH Desktop 已打开；2) 把 DSH 窗口地址栏的完整 URL（含 `?token=`）填入 `dsh_url` 最省事；3) 或执行 `/dsh scan` 强制重扫；4) 看提示里的「本机」信息，确认 AstrBot 确实和 DSH 在同一台机器 |
| `/dsh ping` 显示已连接但提示会话查询失败 | 说明发现成功、只是 API 权限/认证没通；看提示里的认证方式与错误。旧版会显示「旧版无需认证」，新版需 `dsh_home` 可读或 `dsh_token` 有效 |
| 401 / 「DSH 要求认证」 | 本机：确认 `dsh_home` 指向 DSH 数据目录（默认 `~/.dsh`），且 AstrBot 与 DSH 同用户；跨设备：配置 `dsh_url`/`dsh_token` |
| 403 / 「来源地址不在信任列表」 | 跨设备访问需给 DSH 加 `--trusted-host <本插件访问用的 host:port>` 并重启 |
| 扫描很慢 | DSH 端口是随机的，全量扫描约 20-40 秒（默认 256 并发）；确定端口后填 `dsh_url` 或 `dsh_port` 可秒连 |
| 任务一直「等待用户确认」 | 到 DSH 窗口处理审批；或调整 DSH 默认权限预设 |
| 任务超时被取消 | 调大 `request_timeout_seconds`；长任务建议拆分成小任务 |
| 返回内容过长 | 调大 `max_result_chars`（注意上下文占用） |
| 工具报 `session/not-found` 等错误 | 多为会话已关闭/被归档，切回 `session_mode: new` 或更换会话 ID |
| 提示方法 404 | 该实例没有对应接口（版本过旧或能力未启用）；插件会自动尝试旧版兼容模式，若仍失败请升级 DSH |
| `DSH API 响应不是合法 JSON（session.create ...）` | 说明找到的地址是**前端页面/反向代理**，不是 DSH 的 `/api`：`/` 返回了带 DSH 标记的 HTML，但 `/api/*` 也回 HTML。1.1.2 起扫描会校验 `/api` 并跳过这类候选；仍出现时请把 `dsh_url` 指向真实 DSH API 地址（或给代理配置 `/api` 转发） |

## 八、安全说明

- DSH 的本地 API 现在需要会话认证，但仍**只应在本机/可信网络使用**；请勿把 DSH 端口或 AstrBot 暴露到公网。
- 本机凭据文件中的签名密钥、`dsh_token` 等价于 DSH 的完整控制权；插件不会打印它们，请妥善保管配置文件。
- `/dsh` 系列命令仅 **管理员** 可用（`permission_type(ADMIN)`）；`dsh_ask` 工具仅 AstrBot 智能体
  能触发，且每个任务默认是独立 DSH 会话，任务间互不干扰。
- DSH 智能体拥有本机工具（命令执行、文件读写等），本质上等同于授权 AstrBot 代为操作电脑，
  请仅在可信的聊天环境启用本插件。

## 九、文件清单

```
astrbot_plugin_dsh_bridge/
├── metadata.yaml        # 插件元数据（name/desc/author/version/astrbot_version）
├── main.py              # Star 主类：/dsh 命令 + dsh_ask/dsh_ping 工具 + prompt 注入
├── dsh_client.py        # DSH API 客户端：端口发现、Cookie 认证、RPC、任务投递与事件等待
├── _conf_schema.json    # 插件配置面板 schema
├── tests/
│   └── test_dsh_client.py  # 纯函数单元测试（python -m unittest discover -s tests -v）
└── README.md            # 本文档
```

## 十、Changelog

- **1.1.2** 修复“找到页面但不是 API”导致的非 JSON 报错：
  - 扫描时对 200 页面候选追加 `/api` 校验（modern `session/list` / legacy `session.list`），跳过前端静态页、开发服务器、反向代理 SPA fallback 等假候选，继续寻找真正的 DSH API 端口；
  - JSON 解析失败的错误信息补充 `方法 @ 地址`、HTTP 状态、`Content-Type` 与响应前 200 字符，便于定位是代理/前端页面还是协议不匹配；
  - 新增“页面型假候选”与“错误信息含响应详情”的集成测试（共 22 个单测）。
- **1.1.1** 回归修复 + 双协议兼容：
  - 恢复对**旧版 DSH** 的支持：自动识别新版（Cookie 认证 + `session/*` + `session/wait`）与旧版（无认证 + 点号方法 + `session.history` 轮询），`/dsh ask` 在新旧两种 DSH 上都能创建会话并取回结果；
  - `dsh_ping` 恢复容错：只要端口指纹发现成功就显示「已连接」，API 认证/查询失败只作为警告，不再把基础检测判死；
  - 指纹探测恢复跟随 HTTP 重定向，放宽探测超时（TCP 0.5s / HTTP 2.5s），并自动迁移 1.0.0 保存的旧端口区间（避免漏扫 61000-65535）；
  - 失败提示会带上 AstrBot 所在机器、`dsh_host`/`dsh_port`/`dsh_url`/扫描区间/DSH 数据目录，便于定位是跨机器还是端口问题；
  - 新增旧版协议的本地 mock 集成测试（含重定向、ping、`dsh_ask` 轮询）。
- **1.1.0** 适配 DSH 0.2.2-rc.1（仅新版）：
  - 新增 DSH 0.2.x API 认证（本机 `.credentials.yaml` 自签浏览器 Cookie / `dsh web` 启动令牌换取 API Cookie）；
  - RPC 改为 `session/create`、`session/prompt`、`session/state`、`session/wait` 等斜杠方法名与命名参数信封；
  - 完成判定改用 `session/wait` 事件等待，去掉高频轮询；`session/wait` 报告 `input-required` 时保留任务并提示审批；
  - 端口发现默认扫描 `32768-65535`（DSH Desktop 0.2.x 随机临时端口），支持 `dsh_url` 直接固定地址，优化并发扫描速度；
  - `/dsh ping` 显示 DSH 版本与认证方式；新增 `dsh_home`、`dsh_url`、`dsh_token` 配置；
  - 新增 `tests/` 单元测试。
- **1.0.0** 首个版本：端口自动发现、`/dsh` 命令组、`dsh_ask`/`dsh_ping` LLM 工具、prompt 注入、
  跨设备 host 配置、任务超时自动取消、结果截断。
