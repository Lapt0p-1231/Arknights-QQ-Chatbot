# 明日方舟QQ角色聊天机器人 插件开发指南 (Plugin SDK v1)

欢迎为 **明日方舟QQ角色聊天机器人** 编写插件！本指南旨在提供清晰、统一的插件开发标准与最佳实践，帮助插件作者高效开发出支持热重载、权限安全、消息指令与 WebUI 可视化交互的优质插件。

---

## 1. 核心架构与设计原则

- **热重载与隔离**：插件代码无需重启机器人，在修改代码或配置后数秒内自动重载。
- **配置与数据隔离**：插件配置统一由系统管理；本地数据必须存放在专属数据目录 `ctx.data_dir`，禁止污染项目根目录。
- **权限安全（最小特权）**：插件调用外部消息发送或模型推理等能力，需在 `manifest.json` 中声明对应权限。
- **UI 与引擎解耦**：WebUI 插件面板基于独立的沙箱 `iframe` 运行，通过安全桥接协议（`plugin-bridge.js`）与后端进行轻量 RPC 通信。

---

## 2. 插件目录与文件规范

所有插件存放在 `plugins/<plugin-id>/` 目录下。一个完整的插件目录结构如下：

```text
plugins/my-plugin/
├── manifest.json       # [必选] 插件元数据清单
├── plugin.py           # [条件可选] 机器人引擎端逻辑（指令/钩子）
├── main.py             # [条件可选] WebUI 专属 RPC 后端处理
├── index.html          # [条件可选] WebUI 可视化控制面板页面
└── ...                 # 其他多文件模块（支持相对导入如 from .helper import ...）
```
> **说明**：功能型插件至少需包含 `plugin.py`；纯前端/工具型插件可只包含 `index.html`；主题插件需包含 `theme.css`。

---

## 3. 清单文件规范 (`manifest.json`)

`manifest.json` 是插件的身份证，必须严格遵守 JSON 格式：

```json
{
  "id": "my-plugin",
  "name": "示例插件",
  "version": "1.0.0",
  "api_version": 1,
  "type": "plugin",
  "enabled": true,
  "priority": 100,
  "author": "YourName",
  "description": "这是插件的功能简要介绍，将显示在 WebUI 和指令帮助中",
  "permissions": [
    "send_message",
    "model",
    "settings.read"
  ],
  "config": {
    "default_title": "Hello"
  },
  "ui": {
    "panel": "index.html"
  }
}
```

### 字段说明：
| 字段 | 类型 | 必填 | 说明 |
| :--- | :--- | :---: | :--- |
| `id` | string | 是 | 唯一标识符，只能由字母开头的小写字母、数字和连字符组成（如 `roll-dice`） |
| `name` | string | 是 | 插件显示名称（如 `掷骰子工具`） |
| `version` | string | 否 | 语义化版本号，例如 `1.0.0` |
| `api_version`| int | 是 | 必须固定填 `1` |
| `type` | string | 否 | `plugin`（默认常规插件）或 `theme`（WebUI 主题插件） |
| `enabled` | bool | 否 | 默认启用状态（默认为 `false`） |
| `priority` | int | 否 | 执行优先级，数字越小优先级越高，默认 `100` |
| `permissions`| array | 否 | 插件声明的权限列表（见下表） |
| `config` | object | 否 | 插件的默认配置键值对（用户可在管理界面中自定义） |
| `ui.panel` | string | 否 | WebUI 面板入口相对路径（如 `index.html`） |
| `stylesheet`| string | 否 | 主题样式文件相对路径（仅 `type: "theme"` 时有效，如 `theme.css`） |

### 支持的权限声明 (`permissions`)：
- `"send_message"`：允许插件调用私聊/群聊主动发消息接口（`ctx.send_private`, `ctx.send_group`, `ctx.reply`）。
- `"model"`：允许插件调用机器人已配置的主/副大语言模型（`ctx.call_model`）。
- `"settings.read"`：允许插件读取机器人运行设置（`ctx.settings`）。

---

## 4. 机器人逻辑开发 (`plugin.py`)

`plugin.py` 是运行在机器人主进程中的逻辑，负责注册指令和拦截消息钩子。

### 4.1 生命周期入口

```python
# plugins/my-plugin/plugin.py

def setup(ctx):
    """插件载入时由系统调用，用于初始化资源、注册指令与钩子"""
    ctx.logger.info("插件初始化中...")
    try:
        ctx.register_command(
            name="hello",
            handler=cmd_hello,
            description="向用户问好",
            admin_only=False,
            group_allowed=True,
            private_allowed=True,
            timeout=15.0
        )
    except TypeError:
        # 向下兼容：如果用户的宿主尚未重启（旧版无 description 参数）
        ctx.register_command("hello", cmd_hello)

def teardown(ctx):
    """[可选] 插件被禁用或热重载前由系统调用，用于释放后台资源、关闭数据库连接或定时任务"""
    ctx.logger.info("插件正在卸载...")
```

### 4.2 指令注册与处理函数 (`ctx.register_command`)

指令处理函数支持普通函数与异步协程函数（推荐 `async`）：

```python
async def cmd_hello(args: str, event: dict, ctx) -> dict:
    """
    入参说明:
      args: 用户在指令后附带的参数文本（例如用户输入 "/hello abc 123"，args 为 "abc 123"）
      event: OneBot 原始消息事件字典（包含 user_id, group_id, message_type 等）
      ctx: PluginContext 上下文对象
    """
    user_id = event.get("user_id")
    args_text = args.strip() or "世界"

    # 方式一：直接返回结果字典（系统自动回复）
    return {
        "action": "reply",
        "text": f"你好，{args_text}！(来自插件，调用者: {user_id})"
    }
```

#### 指令返回值协议：
- 返回 `str`：系统默认作为回复内容发送。
- 返回 `dict`：
  - `{"action": "reply", "text": "回复文本"}`：发送回复。
  - `{"action": "stop"}`：指令已处理完毕，不向用户发送任何消息。
  - `{"action": "continue"}`：当前插件不拦截，交由后续指令处理器继续处理。
- 返回 `None`：等同于 `{"action": "stop"}`。

### 4.3 钩子监听 (`ctx.on`)

当前支持的事件钩子：`on_reply`（在大模型生成文本后、入库与发送前触发，支持修改或拦截回复）。

```python
async def filter_sensitive_reply(payload: dict, ctx) -> dict:
    """
    payload: {"text": "模型原始回复", "user_id": "...", "group_id": "...", "message": "用户输入"}
    """
    text = payload.get("text", "")
    if "敏感词" in text:
        return {"action": "stop"}  # 拦截本条回复，机器人将静默不发送
    # 或者替换回复：
    return {"action": "replace", "text": text.replace("笨蛋", "天才")}

def setup(ctx):
    ctx.on("on_reply", filter_sensitive_reply, priority=50, timeout=5.0)
```

### 4.4 `PluginContext` 常用能力速查
- `ctx.plugin_id` (str)：当前插件的 ID。
- `ctx.data_dir` (str)：插件专属持久化数据目录路径（如 `data/plugin_data/my-plugin`），目录保证已存在。
- `ctx.config` (dict)：当前插件的合并配置（清单默认配置 + WebUI 运行时覆盖配置）。
- `ctx.settings` (dict)：机器人全局配置（需声明 `settings.read` 权限）。
- `ctx.logger`：带插件前缀的日志记录器，提供 `.info(msg)`、`.warning(msg)`、`.error(msg)`。
- `await ctx.reply(text)`：快捷回复当前消息（需 `send_message` 权限）。
- `await ctx.send_private(user_id, text)`：主动发私聊（需 `send_message` 权限）。
- `await ctx.send_group(group_id, text)`：主动发群聊（需 `send_message` 权限）。
- `await ctx.call_model(role, messages)`：调用大模型推理（`role` 支持 `"main"` 或 `"sub"`，需 `model` 权限）。

---

## 5. WebUI 面板开发 (`index.html` 与 `main.py`)

为插件开发专属管理面板非常简单，无需复杂前端脚手架，直接使用原生 HTML/JS 或 Vue/React 构建均可。

### 5.1 面板前端页面 (`index.html`)

页面会自动注入 `/plugin-bridge.js`（也可以主动通过 `<script src="/plugin-bridge.js"></script>` 引入）。

```html
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>插件面板</title>
  <script src="/plugin-bridge.js"></script>
  <style>
    body { background: #0d1015; color: #d8dee9; font-family: sans-serif; padding: 16px; }
    button { background: #d6b36a; color: #000; border: 0; padding: 6px 12px; cursor: pointer; border-radius: 3px; }
  </style>
</head>
<body>
  <h2>我的插件控制台</h2>
  <button id="btn-ping">测试 RPC 通信</button>
  <pre id="output"></pre>

  <script>
    ArkPlugin.onReady(function(cfg) {
      console.log("宿主注入配置:", cfg);
      // 动态调整 iframe 高度
      ArkPlugin.setHeight(320);
    });

    document.getElementById('btn-ping').onclick = async function() {
      try {
        // 调用后端 main.py 中的 handle(method, data, ctx)
        const res = await ArkPlugin.request('ping', { timestamp: Date.now() });
        document.getElementById('output').textContent = JSON.stringify(res, null, 2);
      } catch (err) {
        alert("调用失败: " + err.message);
      }
    };
  </script>
</body>
</html>
```

### 5.2 面板后端 RPC (`main.py`)

`main.py` 用于处理来自 WebUI `ArkPlugin.request(method, data)` 的请求：

```python
# plugins/my-plugin/main.py

def handle(method: str, data: any, ctx: dict) -> dict:
    """
    method: 前端传递的方法名（如 'ping', 'save_data'）
    data: 前端传递的 JSON 参数字典或基础类型
    ctx: 包含 id, data_dir, config 等字段的环境字典
    """
    data_dir = ctx.get("data_dir")

    if method == "ping":
        return {
            "ok": True,
            "message": "pong",
            "received": data,
            "plugin": ctx.get("id")
        }

    elif method == "save_config":
        # 在 ctx['data_dir'] 下安全保存业务数据
        return {"ok": True}

    return {"ok": False, "error": f"未知方法: {method}"}
```

---

## 6. 打包、分发与安装

### 6.1 打包规范
将插件目录打包为标准 `.zip` 文件。既可以直接在根目录打包文件，也可以在外层包含一个顶层文件夹打包。例如：

```text
my-plugin.zip
├── manifest.json
├── plugin.py
├── main.py
└── index.html
```

### 6.2 安装方式
1. **WebUI 拖拽安装**：在机器人 Web 控制台打开「插件管理」，直接将 `.zip` 文件拖入上传区或点击“选择 ZIP”即可完成安装。
2. **手动安装**：直接将解压后的文件夹放入 `plugins/<plugin-id>/` 目录，系统将在数秒内自动热重载识别。

---

## 7. 开发者避坑与最佳实践清单

1. **永远使用 `ctx.data_dir` 存放文件**：
   - 严禁使用相对路径 `./` 或访问项目根目录写临时文件。所有需要持久化的配置、缓存、数据库（SQLite）请统一保存在 `os.path.join(ctx.data_dir, "your_data.db")` 中。
2. **多文件拆分规范**：
   - 支持同一插件目录下多文件组织，例如 `from .service import DataService`。
3. **避免常驻阻塞调用**：
   - 指令处理函数若包含耗时网络请求或复杂计算，务必使用 `async` 并设定合理超时，避免挂起事件循环。
4. **后台常驻协程必须在 `teardown` 中清理**：
   - 若在 `setup` 中调用了 `asyncio.create_task`，请将任务句柄保存，并在 `teardown(ctx)` 中执行 `task.cancel()`，以防插件热重载或禁用后旧任务残留。
5. **严禁硬编码端口或全局网络设置**：
   - 不得在插件中直接劫持或修改全局环境变量与系统设置。
