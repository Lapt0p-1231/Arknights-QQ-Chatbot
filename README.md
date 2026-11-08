# 在QQ上的《明日方舟》角色聊天机器人

当前版本：v1.3.2 · [更新日志](CHANGELOG.md)

基于 SnowLuma（Docker）+ OpenAI 兼容接口的 QQ 聊天机器人，内置冷热双层记忆（该方案要求一定电脑配置，详见https://github.com/Lapt0p-1231/bionic-memory ）、语义召回、角色扮演、群聊插话和本地 WebUI。

# 能让你的干员和你QQ聊天！！

该版本不包含个人 API Key、QQ 账号、聊天记录、向量数据、模型缓存、备份或本机 Docker 环境。

## 功能

- 私聊与群聊：@触发、名字触发、冷却控制、引用回复、图片识别
- 冷热记忆：SQLite 保存近期对话，ChromaDB 保存可语义召回的长期记忆
- 记忆发散：定时回顾高重要度记忆，生成低置信度的“梦境”式内心独白
- 角色系统：支持全局人设、用户专属人设、人设拟合度和用户档案
- 群聊插话：AI 判断或概率掷骰，避免逐条回复
- WebUI：模型路由、提示词开关、记忆检索、档案与发散任务管理
- 指令系统：重新生成、清空记忆、好感度、回复模式和管理员设置

## 运行要求

- Windows 10/11（其他系统可使用 Python 命令手动启动）
- Python 3.11+
- 一个 QQ 机器人账号
- Docker Desktop 与 SnowLuma 协议端容器（镜像：`motricseven7/snowluma:latest`）
- 一个 OpenAI 兼容的聊天模型接口，例如 DeepSeek

嵌入模型使用 `bge-small-zh`，首次运行会自动下载约 100MB 模型，之后可离线进行向量检索。无需 GPU。

## 安装

第一次部署请先阅读根目录的 **`环境配置教程.txt`**，其中包含 Python、Docker Desktop、SnowLuma、模型接口、机器人身份和启动检查的完整步骤。

```powershell
git clone https://github.com/Lapt0p-1231/Arknights-QQ-Chatbot.git
cd Arknights-QQ-Chatbot
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

复制示例模型配置：

```powershell
Copy-Item api.example.json api.json
```

编辑 `api.json`，在 `key` 中填入你的 API Key。也可以不手动编辑，启动后在 WebUI 的“模型路由”页面添加端点。

路由含义：

- `main`：生成聊天回复的主模型
- `sub`：负责群聊判断、记忆总结、记忆打分和发散的副模型

## 配置机器人身份

机器人 QQ 与管理员 QQ 通过环境变量设置，不在源码中保存。

PowerShell 临时设置（仅当前窗口有效）：

```powershell
$env:QQ_BOT_QQ = "你的机器人QQ号"
$env:QQ_BOT_ADMIN_QQ = "你的QQ号"
```

多个管理员使用英文逗号分隔：

```powershell
$env:QQ_BOT_ADMIN_QQ = "123456789,987654321"
```

如需永久保存，可改用 `setx QQ_BOT_QQ "你的机器人QQ号"`，设置后重新打开终端。

可选环境变量：

| 变量 | 用途 | 默认值 |
|---|---|---|
| `QQ_BOT_QQ` | 机器人自身 QQ，用于识别 @与过滤自身消息 | `0`，必须配置 |
| `QQ_BOT_ADMIN_QQ` | 管理员 QQ 列表 | 空 |
| `QQ_BOT_API_KEY` | `api.json` 未配置时的接口密钥兜底 | 空 |
| `QQ_BOT_SERVER_CHAN_KEY` | Server酱 SendKey，用于异常微信告警 | 空，不开启 |
| `QQ_BOT_WEB_HOST` | WebUI 监听地址 | `127.0.0.1` |
| `QQ_BOT_WEB_PORT` | WebUI 端口 | `8090` |

## 配置 SnowLuma

1. 安装并启动 Docker Desktop。
2. 按 `环境配置教程.txt` 创建并启动 `snowluma` 容器。
3. 打开 SnowLuma 控制台：http://127.0.0.1:5099，登录机器人 QQ。
4. 在 SnowLuma 网络配置中添加“反向 WebSocket 客户端”，地址填写 `ws://127.0.0.1:8080`。
5. 保存并启用连接。

`QQ-BOT.py` 会在本机 `8080` 端口启动 WebSocket 服务，等待 SnowLuma 接入。请保持该端口未被其他程序占用，不要将其直接暴露到公网。

## 启动

确保 Docker Desktop 与 SnowLuma 已运行，然后双击 `一键启动.bat`，或分别执行：

```powershell
.\.venv\Scripts\python.exe part_web.py
```

另开一个终端：

```powershell
.\.venv\Scripts\python.exe QQ-BOT.py
```

WebUI 地址：http://127.0.0.1:8090

WebUI 默认没有登录认证，请不要将监听地址改为公网地址。

## 人设与提示词

`prompts/` 中保留了一套通用提示词和一个白金角色模板。白金人设默认关闭，可在 WebUI 的提示词管理中启用，也可以直接修改或新增 `.txt` 提示词文件。

## ！欣特莱雅世界第一 ！

提示词开关会保存到本地 `prompts_config.json`，该文件属于个人配置，不纳入标准仓库。

## 常用指令

| 指令 | 功能 |
|---|---|
| `/cmd help` | 按当前权限与场景查看指令 |
| `/rl` | 重新生成上一条回复 |
| `/char set <角色设定>` | 私聊设置专属人格 |
| `/char clear` | 清除专属人格 |
| `/user set <用户信息>` | 记录用户信息 |
| `/user clear` | 清除用户信息 |
| `/mdchange` | 切换扮演/聊天模式 |
| `/memory reset` | 清空当前档案记忆 |
| `/charfit set <0.0-1.0>` | 设置人设拟合度 |
| `/name set <名字...>` | 管理员设置名字触发词 |

完整管理员指令可在 QQ 中发送 `/cmd help` 查看。

## 数据与隐私

首次运行后会在本地生成：

- `data/`：SQLite、ChromaDB 与档案数据
- `models/`：本地嵌入模型缓存
- `user_log/`：旧版档案迁移目录
- `api.json`：模型端点与密钥
- `bot_settings.json`：个人运行参数
- `QQ-BOT.log`：运行日志

这些内容均已加入 `.gitignore`。公开分享或提交代码前，请勿强行添加这些文件。

## 项目结构

```text
Arknights-QQ-Chatbot/
├── QQ-BOT.py             # 消息处理、模型调用与机器人主循环
├── part_record.py        # 冷热记忆、向量召回与档案存储
├── part_command.py       # 消息发送与指令处理
├── part_prompt.py        # 提示词加载与开关管理
├── part_api.py           # OpenAI 兼容端点与模型路由
├── part_search.py        # 联网搜索模块（Tavily/Jina/必应三级降级）
├── part_sticker.py       # 表情包捕获、情绪优选与发送模块
├── part_web.py           # FastAPI WebUI 后端
├── prompts/              # 通用提示词与可选白金人设
├── webui/index.html      # WebUI 前端
├── api.example.json      # 模型配置模板
├── 环境配置教程.txt       # 首次部署的完整环境配置步骤
├── CHANGELOG.md          # 版本更新日志
└── requirements.txt
```

## 相关项目

记忆系统的独立解耦版本：https://github.com/Lapt0p-1231/bionic-memory
