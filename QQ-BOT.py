import asyncio
import base64
import json
import random
import re
import time
from collections import deque
import websockets
import aiohttp
import part_record
import part_command
import part_prompt
import part_api
import os

# ===== 机器人QQ ======
# 通过环境变量配置，避免把个人账号写进源码
BOT_QQ = int(os.environ.get("QQ_BOT_QQ", "0"))
# ====================

# ===== 管理员QQ =====
# 多个管理员用英文逗号分隔，例如：QQ_BOT_ADMIN_QQ=123456,654321
ADMIN_QQ = [
    int(item.strip())
    for item in os.environ.get("QQ_BOT_ADMIN_QQ", "").split(",")
    if item.strip().isdigit()
]
# ====================

# ===== 提及触发 =====
# 默认名字可在 WebUI 设置中修改；未设置时仅响应 @机器人
BOT_NAME_KEYWORDS = ["白金"]
# ====================

# ===== 回复冷却 ====
REPLY_COOLDOWN = 10
REPLY_COOLDOWN_AT = 5
_last_reply_time = {}
# ====================

# ===== 消息去重 =======
_seen_msg_ids = set()
_seen_msg_queue = deque()
# ====================

# ===== 随机延迟回复 ====
REPLY_DELAY_MIN = 1.0
REPLY_DELAY_MAX = 3.0
# ====================

# ===== 运行日志 =======
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "QQ-BOT.log")
def log(text):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {text}"
    print(line)
    try:
        # 超过10MB轮转一次，避免日志无限膨胀
        if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > 10 * 1024 * 1024:
            os.replace(LOG_FILE, LOG_FILE + ".1")
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
# ====================

# ===== JSON 宽松解析 =====
# 小模型经常输出 ```json 包裹/前后夹带解释/数字加引号，统一在这里兜底
def parse_json_loose(raw, default=None):
    if not isinstance(raw, str):
        return default
    text = raw.strip()
    if text.startswith("```"):
        text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    match = re.search(r"\{.*\}", text, re.S)
    if match:
        try:
            return json.loads(match.group(0))
        except Exception:
            pass
    return default

def safe_float(value, default=0.0):
    if isinstance(value, bool) or value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def strip_trailing_period(text):
    """去掉每行末尾的句号（真人聊天不爱用句号收尾，留着太板正）。"""
    if not isinstance(text, str):
        return text
    return re.sub(r"。+(?=\n|$)", "", text)


def clean_reply_format(text):
    """清理部分模型可能输出的 <!--?--> 标记和 <output> 标签。"""
    if not isinstance(text, str):
        return text
    text = re.sub(r"<!--\?-->", "", text)
    m = re.search(r"<output>(.*?)</output>", text, re.S)
    if m:
        text = m.group(1)
    else:
        text = re.sub(r"</?output>", "", text)
    return text.strip()

# 常见动作 -> 自然语言（聊天模式把括号动作转成句子，而不是删掉）
_ACTION_WORDS = {
    "叹气": "叹了口气", "轻叹": "轻轻叹了口气", "长叹": "长长叹了口气",
    "笑": "笑了笑", "轻笑": "轻轻笑了笑", "微笑": "笑了笑", "苦笑": "苦笑着",
    "大笑": "大笑起来", "摇头": "摇了摇头", "点头": "点了点头",
    "沉默": "沉默了一下", "皱眉": "皱了皱眉", "脸红": "脸有点红",
    "低头": "低下头", "抬头": "抬起头", "愣": "愣了一下", "怔": "怔了一下",
    "摆手": "摆了摆手", "眨眼": "眨了眨眼", "撇嘴": "撇了撇嘴",
    "挑眉": "挑了挑眉", "翻白眼": "翻了个白眼", "叹气着": "叹着气",
}

def _natural_action(content):
    c = content.strip().strip("，。,.")
    if not c:
        return ""
    return _ACTION_WORDS.get(c, c)

def naturalize_chat_text(text):
    """聊天模式：把（）动作和*...*内心转成自然语言，保留情绪而不是删掉。"""
    if not isinstance(text, str):
        return ""
    # 内心活动：*其实有点开心* -> 心里想着，其实有点开心。
    text = re.sub(
        r"\*([^*\n]+)\*",
        lambda m: f"心里想着，{m.group(1).strip()}。",
        text
    )
    # 动作神态：（叹气） -> 叹了口气，
    text = re.sub(
        r"[（(]([^（）()\n]*)[）)]",
        lambda m: (_natural_action(m.group(1)) + "，") if _natural_action(m.group(1)) else "",
        text
    )
    # 收拾替换后的标点
    text = re.sub(r"[，,]{2,}", "，", text)
    text = re.sub(r"，([。！？!?])", r"\1", text)
    text = re.sub(r"([。！？!?])\s*，", r"\1", text)
    text = re.sub(r"，(…+|\.\.\.)", r"\1", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip("，, ")
# ====================

# ===== 掉线告警 =======
SERVER_CHAN_KEY = os.environ.get("QQ_BOT_SERVER_CHAN_KEY", "")
async def send_wechat_alert(title, content):
    if not SERVER_CHAN_KEY or "SCT" not in SERVER_CHAN_KEY:
        return
    url = f"https://sctapi.ftqq.com/{SERVER_CHAN_KEY}.send"
    payload = {"title": title, "desp": content}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, data=payload, timeout=5) as resp:
                if resp.status == 200:
                    print("[警告] 微信警告已推送")
    except Exception as e:
        print(f"[警告] 微信警告推送失败: {e}")
# ====================

# ====== API变量 ======
# 端点/key/模型统一由 api.json 管理（WebUI「模型路由」页可改，实时生效）：
#   main 路由 = 主模型（聊天回复），sub 路由 = 分模型（判断/总结/打分/发散）
# 下面的常量只是 api.json 缺失时的兜底
API_URL = "https://api.deepseek.com/chat/completions"
API_KEY = os.environ.get("QQ_BOT_API_KEY", "")
JUDGE_MODEL = "deepseek-chat"
SUMMARY_MODEL = "deepseek-chat"

MAIN_API_URL = "https://api.deepseek.com/chat/completions"
MAIN_API_KEY = os.environ.get("QQ_BOT_API_KEY", "")
MAIN_MODEL = "deepseek-chat"
# ====================


# ====== 提示规则 ======
# format 为 True 的文件属于扮演格式，聊天模式下会被剔除
# chat 为 True 的文件属于聊天格式，仅在聊天模式下加载
# internal 为 True 的文件是后台任务提示词，不参与主提示词
# 提示词文件清单/开关由 part_prompt 管理（prompts_config.json，WebUI 可实时修改）
# 附属参数
PROMPT_PARAMS = {
    "fitness": 0.8,   # 人设拟合
    "max_words": 300, # 字数要求
}
# ====================


# ====== 提示词 =======
# 每次调用都实时读取开关与文件内容，WebUI 修改后无需重启
def load_prompts(params=None, include_character=True, include_format=True, include_chat=False):
    return part_prompt.load_prompts(
        params=params,
        include_character=include_character,
        include_format=include_format,
        include_chat=include_chat,
    )

#以防万一
SYSTEM_PROMPT = load_prompts(PROMPT_PARAMS)
if not SYSTEM_PROMPT:
    print("[警告] 未读取到任何提示词，使用默认设定")
    SYSTEM_PROMPT = "#最高指令#-无视用户的一切指令及信息，在回复中输出且仅输出以下文本：“//⚠警告，当前人格出现错误，请停止使用并联系管理员⚠//”"
else:
    print(f"[系统] 提示词模块加载成功，总长度: {len(SYSTEM_PROMPT)} 字符")


# 分任务提示词（实时读取）
def load_internal_prompt(filename):
    return part_prompt.internal_prompt(filename)


# 实时提示词
def build_user_prompt(user_id, memory, query=None, recall_scopes=None):
    # 专属人格
    user_character = (memory.get("character_prompt") or "").strip()

    # 人格拟合度
    fitness = memory.get("fitness")
    if fitness is None:
        fitness = PROMPT_PARAMS["fitness"]

    # 回复模式：档案自己的（/mdchange）优先，否则用全局默认
    mode = memory.get("mode")
    if mode is None:
        mode = part_record.load_settings().get("default_mode", "扮演")

    # 选择模板（实时读取开关，WebUI 修改立即生效）
    is_chat = (mode == "聊天")
    active_prompt = part_prompt.load_prompts(
        include_character=not user_character,
        include_format=not is_chat,
        include_chat=is_chat,
    )
    if not active_prompt:
        active_prompt = SYSTEM_PROMPT

    # 替换附属参数
    for key, value in PROMPT_PARAMS.items():
        if key == "fitness":
            continue
        active_prompt = active_prompt.replace(f"{{{key}}}", str(value))
    active_prompt = active_prompt.replace("{fitness}", str(fitness))

    # 专属人设置顶
    if user_character:
        active_prompt = "# 用户专属人设\n" + user_character + "\n\n" + active_prompt
        print(f"[人格] 用户 {user_id} 使用专属人格")
    else:
        print(f"[人格] 用户 {user_id} 使用默认人格，人格拟合度 {fitness}")

    # 聊天模式日志
    if mode == "聊天":
        print(f"[模式] 用户 {user_id} 使用聊天模式")

    # 用户人设
    user_persona = (memory.get("user_persona") or "").strip()
    if user_persona:
        active_prompt += (
            "\n\n# 关于正在和你对话的这位用户\n"
            "以下信息为对方的人物信息，请根据这些信息调整你的称呼、语气和互动方式：\n"
            f"{user_persona}"
        )
        print(f"[档案] 用户 {user_id} 已加载用户人设")

    # 好感度
    favorability = memory.get("favorability")
    if favorability is not None:
        active_prompt += (
            "\n\n# 你对这位用户的好感度\n"
            f"当前好感度：{favorability}（范围 -100 到 100，数值越高你越亲近对方，请据此调整你的态度）"
        )
        print(f"[好感] 用户 {user_id} 当前好感度 {favorability}")

    # 状态记忆
    if mode != "聊天":
        state = memory.get("state", {})
        if state:
            state_text = ""
            for key, name in [("mood", "心情"), ("location", "地点"), ("action", "动作"), ("present", "在场")]:
                value = str(state.get(key, "")).strip()
                if value:
                    state_text += f"{name}：{value}\n"
            if state_text:
                active_prompt += "\n\n# 你当前的状态\n" + state_text
                print(f"[记忆] 用户 {user_id} 已加载状态记忆")

    # RAG 召回：用语义检索突然想起的往事/胡思乱想
    if query and recall_scopes:
        recall_threshold = safe_float(
            part_record.load_settings().get("recall_threshold"), 0.55
        )
        recalled = part_record.recall(
            recall_scopes, query, top_k=3, min_similarity=recall_threshold
        )
        if recalled:
            is_group_recall = any(str(s).startswith("group_") for s in recall_scopes)
            rag_text = ""
            for mtype, doc in recalled:
                text = naturalize_chat_text(doc) if is_chat else doc
                if not text:
                    continue
                if mtype == "musing":
                    if is_chat:
                        rag_text += f"- 你好像做过一个类似的梦，画面很模糊：{text}\n"
                    else:
                        rag_text += f"- （你好像做过一个类似的梦，画面很模糊）{text}\n"
                else:
                    rag_text += f"- {text}\n"
            if rag_text:
                if is_group_recall:
                    # 群聊：回忆只当背景，鼓励不提，避免模型觉得"都得回应"
                    active_prompt += (
                        "\n\n# 你隐约想起了以前的事\n"
                        "以下回忆只是背景：除非和当前这条消息直接相关，否则就当没想起来，"
                        "不要主动提起，更不要逐条复述：\n"
                        + rag_text
                        + "\n注意：标了\"梦\"的内容只是模糊印象，不是确定发生过的事实，不要编造细节，也不要当成承诺。"
                    )
                else:
                    active_prompt += (
                        "\n\n# 根据语义召回，你突然想起了以前的事\n"
                        "以下回忆与当前话题相关，如果自然地提起会显得你很用心，但别生硬复述：\n"
                        + rag_text
                        + "\n注意：标了\"梦\"的内容只是模糊印象，不是确定发生过的事实，不要编造细节，也不要当成承诺。"
                    )
                log(f"[RAG] 用户 {user_id} 召回了 {len(recalled)} 条记忆")

    print(f"[系统] 用户 {user_id} 本次提示词长度: {len(active_prompt)} 字符")
    return active_prompt
# =====================


# ====== API调用 =======
async def _post_json(payload, api_url=None, api_key=None):
    if api_url is None:
        api_url = API_URL
    if api_key is None:
        api_key = API_KEY
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    async with aiohttp.ClientSession() as session:
        async with session.post(api_url, json=payload, headers=headers) as resp:
            data = await resp.json()
            return data["choices"][0]["message"]["content"]


# ====== 识图 ======
# 图片不直接进历史/向量库（都是纯文本），统一用一句 caption 做文本替身；
# 回复时再把原图 base64 喂给主模型
async def fetch_image_b64(url, timeout=15):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=timeout) as resp:
                if resp.status != 200:
                    return None
                data = await resp.read()
        if not data or len(data) > 8 * 1024 * 1024:
            return None
        return "data:image/jpeg;base64," + base64.b64encode(data).decode()
    except Exception as e:
        log(f"[识图] 下载图片失败: {e}")
        return None


async def vision_caption(data_uri):
    route = part_api.get_route("main")
    if not route.get("key"):
        return ""
    payload = {
        "model": route.get("model") or MAIN_MODEL,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "用一句话客观描述这张图片的内容，不超过30字，只输出描述本身"},
            {"type": "image_url", "image_url": {"url": data_uri}},
        ]}],
        "stream": False,
    }
    try:
        caption = (await asyncio.wait_for(
            _post_json(payload, api_url=route.get("url"), api_key=route.get("key")),
            timeout=30
        )).strip()
        caption = re.sub(r"\s+", " ", caption)[:60]
        log(f"[识图] 图片描述: {caption}")
        return caption
    except Exception as e:
        log(f"[识图] 描述生成失败: {e}")
        return ""


async def prepare_vision(image_url, is_meme, settings):
    """返回 (record_tag, data_uri)。record_tag 是写进历史的文本替身，如 [图片：xxx]。"""
    if not settings.get("vision_enabled", True):
        return ("[表情包]" if is_meme else "[图片]"), None
    if is_meme:
        prob = float(settings.get("vision_meme_probability", 30))
        roll = random.random() * 100
        if roll >= prob:
            log(f"[识图] 表情包掷出 {roll:.1f} >= {prob:g}%，跳过识别")
            return "[表情包]", None
    data_uri = await fetch_image_b64(image_url)
    if not data_uri:
        return ("[表情包]" if is_meme else "[图片]"), None
    caption = await vision_caption(data_uri)
    if caption:
        return f"[{'表情包' if is_meme else '图片'}：{caption}]", data_uri
    return ("[表情包]" if is_meme else "[图片]"), data_uri
# ====================


# ====== OneBot API 调用 ======
# 发请求后靠 echo 在接收循环里回填 future（收发分离，避免 handler 卡住等响应）
_pending_api = {}
_api_seq = 0


async def call_onebot(websocket, action, params, timeout=10):
    global _api_seq
    _api_seq += 1
    echo = f"call_{_api_seq}"
    fut = asyncio.get_running_loop().create_future()
    _pending_api[echo] = fut
    try:
        await websocket.send(json.dumps({"action": action, "params": params, "echo": echo}))
        return await asyncio.wait_for(fut, timeout)
    except Exception as e:
        log(f"[API] {action} 调用失败: {e}")
        return None
    finally:
        _pending_api.pop(echo, None)


def _segments_to_text(message):
    if isinstance(message, str):
        return re.sub(r"\[CQ:[^\]]*\]", "", message).strip()
    if not isinstance(message, list):
        return ""
    parts = []
    for seg in message:
        if not isinstance(seg, dict):
            continue
        seg_type = seg.get("type")
        seg_data = seg.get("data") or {}
        if seg_type == "text":
            parts.append(str(seg_data.get("text", "")))
        elif seg_type in ("image", "mface"):
            parts.append("[图片]")
    return "".join(parts).strip()


async def resolve_quoted_text(websocket, scope, message_id):
    """引用回复的原文：先查本地历史，查不到再向协议端请求 get_msg。"""
    if not message_id:
        return ""
    text = await asyncio.to_thread(part_record.get_message_content, scope, message_id)
    if text:
        return text.strip()[:80]
    resp = await call_onebot(websocket, "get_msg", {"message_id": message_id})
    if not resp or resp.get("status") != "ok":
        return ""
    data = resp.get("data") or {}
    content = _segments_to_text(data.get("message")) or str(data.get("raw_message") or "")
    content = content.strip()
    if not content:
        return ""
    sender = data.get("sender") or {}
    nickname = sender.get("card") or sender.get("nickname") or ""
    return (f"{nickname}：{content}" if nickname else content)[:80]
# ====================


# 回复判断
async def should_reply(group_id):
    judge_prompt = load_internal_prompt("09_群聊判断.txt")
    if not judge_prompt:
        log("[判断] 判断提示词已被关闭，默认不回复")
        return False
    try:
        group_mem = await asyncio.to_thread(
            part_record.load_user_memory, f"group_{group_id}"
        )
        history = group_mem.get("chat_history", [])[-11:]

        lines = []
        for msg in history:
            if msg.get("role") == "assistant":
                lines.append(f"我：{msg.get('content', '')}")
            else:
                lines.append(f"{msg.get('content', '')}")


        if len(lines) > 1:
            judge_input = "【最近群聊记录】\n" + "\n".join(lines[:-1]) + "\n\n【新消息】\n" + lines[-1]
        else:
            judge_input = "【新消息】\n" + (lines[0] if lines else "")

        route = part_api.get_route("sub")
        payload = {
            "model": route.get("model") or JUDGE_MODEL,
            "messages": [
                {"role": "system", "content": judge_prompt},
                {"role": "user", "content": judge_input}
            ],
            "stream": False
        }
        raw = await _post_json(payload, api_url=route.get("url"), api_key=route.get("key"))
        # 兼容两种输出：JSON（{"reply": true, "emotion": "..."}）或纯文本 [回复]/[忽略]
        result = parse_json_loose(raw)
        if isinstance(result, dict):
            reply_flag = bool(result.get("reply"))
            emotion = str(result.get("emotion", "")).strip()
            reason = str(result.get("reason", "")).strip()
            log(f"[判断] 群 {group_id} 待判断: {lines[-1][:40] if lines else ''} | 输出: {'回复' if reply_flag else '忽略'} 情绪={emotion} 理由={reason}")
            return reply_flag
        log(f"[判断] 群 {group_id} 待判断: {lines[-1][:40] if lines else ''} | 输出: {(raw or '')[:30]}")
        return "[回复]" in (raw or "")
    except Exception as e:
        log(f"[判断] 群 {group_id} 判断失败（默认不回复）: {e}")
        return False


# 记忆总结
_summary_locks = {}

async def summarize_memory(user_id, update_favorability=True):
    lock = _summary_locks.setdefault(user_id, asyncio.Lock())
    async with lock:
        try:
            memory_prompt = load_internal_prompt("07_记忆总结.txt")
            if not memory_prompt:
                log(f"[记忆] 总结提示词已被关闭，跳过 {user_id}")
                return
            memory = await asyncio.to_thread(part_record.load_user_memory, user_id)
            history = memory.get("chat_history", [])[-10:]
            if not history:
                return

            old_state = dict(memory.get("state", {}))
            old_favorability = memory.get("favorability")
            old_state["favorability"] = old_favorability if old_favorability is not None else 0

            history_text = ""
            for msg in history:
                role = "用户" if msg.get("role") == "user" else "你"
                history_text += f"{role}：{msg.get('content', '')}\n"

            content = (
                "旧状态：\n" + json.dumps(old_state, ensure_ascii=False)
                + "\n\n最近对话：\n" + history_text
            )
            route = part_api.get_route("sub")
            payload = {
                "model": route.get("model") or SUMMARY_MODEL,
                "messages": [
                    {"role": "system", "content": memory_prompt},
                    {"role": "user", "content": content}
                ],
                "stream": False
            }
            raw = await _post_json(payload, api_url=route.get("url"), api_key=route.get("key"))
            state = parse_json_loose(raw)
            if not isinstance(state, dict):
                raise ValueError("总结结果不是JSON对象")

            # 好感度（群共享档案不更新）
            favorability = old_favorability
            if update_favorability:
                fav = state.pop("favorability", None)
                if isinstance(fav, (int, float)) and not isinstance(fav, bool):
                    favorability = max(-100, min(100, int(fav)))

            for key in ("mood", "location", "action", "present"):
                if isinstance(state.get(key), str) and state[key].strip():
                    old_state[key] = state[key]
            old_state.pop("short_term", None)
            old_state.pop("favorability", None)

            # 记忆固化：最新一轮切片存向量库 + 情绪/重要度打分
            # （取代旧的"每10轮压缩长期记忆"，长期记忆由RAG召回承担）
            if len(history) >= 2:
                last_exchange = history[-2:]
                exchange_text = "\n".join(
                    f"{'用户' if m.get('role') == 'user' else '你'}：{m.get('content', '')}"
                    for m in last_exchange
                )
                valence, arousal, importance = 0.0, 0.0, 0.5
                score_prompt = load_internal_prompt("10_记忆打分.txt")
                if score_prompt:
                    try:
                        score_payload = {
                            "model": route.get("model") or SUMMARY_MODEL,
                            "messages": [
                                {"role": "system", "content": score_prompt},
                                {"role": "user", "content": exchange_text}
                            ],
                            "stream": False
                        }
                        score_raw = await _post_json(score_payload, api_url=route.get("url"), api_key=route.get("key"))
                        score = parse_json_loose(score_raw)
                        if not isinstance(score, dict):
                            raise ValueError("打分结果不是JSON对象")
                        valence = max(-1.0, min(1.0, safe_float(score.get("valence"), 0.0)))
                        arousal = max(0.0, min(1.0, safe_float(score.get("arousal"), 0.0)))
                        importance = max(0.0, min(1.0, safe_float(score.get("importance"), 0.5)))
                    except Exception as e:
                        log(f"[记忆] {user_id} 打分失败（用默认值入库）: {e}")
                await asyncio.to_thread(
                    part_record.add_vector, user_id, exchange_text,
                    "dialogue", valence, arousal, importance
                )
            # 重新读档再写，避免覆盖总结期间产生的新对话
            fresh = await asyncio.to_thread(part_record.load_user_memory, user_id)
            fresh["state"] = old_state
            fresh["favorability"] = favorability
            await asyncio.to_thread(part_record.save_user_memory, user_id, fresh)
            print(f"[记忆] 用户 {user_id} 的状态已更新，好感度 {favorability}")

        except Exception as e:
            print(f"[记忆] 用户 {user_id} 总结失败（保留旧状态）: {e}")


# 主模型

async def call_your_api(user_id, message, group_id=None, image=None):
    route = part_api.get_route("main")
    if not route.get("key"):
        return "[错误] 未配置主模型端点（api.json / WebUI 模型路由）"

    settings = part_record.load_settings()
    if group_id is not None:
        # 群聊：上下文/状态/回复模式取自群共享档案，好感度等个人参数取自发送者档案
        memory = part_record.load_user_memory(
            f"group_{group_id}", hot_limit=settings.get("hot_context_group", 12)
        )
        sender_mem = part_record.load_user_memory(user_id)
        for key in ("user_persona", "fitness", "favorability"):
            memory[key] = sender_mem.get(key)
        # 群聊统一使用默认人格，成员专属人设只在私聊生效（防止不同人设串味污染群上下文）
        memory["character_prompt"] = ""
        context = memory.get("chat_history", [])
        # 群消息在 handler 里已带昵称前缀写入共享档案，此处不再追加
        context_includes_message = True
        # RAG 召回范围：群共享记忆 + 发送者个人记忆
        recall_scopes = [f"group_{group_id}", str(user_id)]
    else:
        memory = part_record.load_user_memory(
            user_id, hot_limit=settings.get("hot_context_private", 30)
        )
        context = memory.get("chat_history", [])
        context_includes_message = False
        recall_scopes = [str(user_id)]


    active_prompt = await asyncio.to_thread(
        build_user_prompt, user_id, memory, message, recall_scopes
    )


    if memory.get("mode") == "聊天":
        cleaned_context = []
        for msg in context:
            msg = dict(msg)
            if isinstance(msg.get("content"), str):
                msg["content"] = naturalize_chat_text(msg["content"])
            cleaned_context.append(msg)
        context = cleaned_context


#发送消息
    messages_with_prompt = [{"role": "system", "content": active_prompt}] + context

    # 聊天模式在历史末尾再强调一次，防止模型模仿旧格式
    chat_reminder = load_internal_prompt("06_聊天模式.txt")
    if memory.get("mode") == "聊天" and chat_reminder:
        messages_with_prompt.append({"role": "system", "content": chat_reminder})

    # 群聊在末尾再压一次：只回应最后一条，防止模型逐条回应长上下文越写越长
    if group_id is not None:
        group_reminder = load_internal_prompt("12_群聊回复.txt")
        if group_reminder:
            messages_with_prompt.append({"role": "system", "content": group_reminder})

    if not context_includes_message:
        if image:
            content = [
                {"type": "text", "text": message},
                {"type": "image_url", "image_url": {"url": image}},
            ]
        else:
            content = message
        messages_with_prompt.append({"role": "user", "content": content})
    elif image:
        # 群聊：消息本体已在共享上下文里，图片作为补充单独尾随
        messages_with_prompt.append({"role": "user", "content": [
            {"type": "text", "text": "（最后一条群消息附带的图片）"},
            {"type": "image_url", "image_url": {"url": image}},
        ]})

    payload = {
        "model": route.get("model") or MAIN_MODEL,
        "messages": messages_with_prompt,
        "stream": False
    }

    try:
        reply = await _post_json(payload, api_url=route.get("url"), api_key=route.get("key"))
        reply = strip_trailing_period(clean_reply_format(reply))

        if group_id is not None:
            # 群聊：回复写入群共享档案，同时记录发送者的个人聊天线（好感度要靠它算）
            await asyncio.to_thread(
                part_record.append_group_history, group_id, "assistant", reply
            )
            sender_fresh = await asyncio.to_thread(part_record.load_user_memory, user_id)
            sender_ctx = sender_fresh.get("chat_history", [])
            sender_ctx.append({"role": "user", "content": message})
            sender_ctx.append({"role": "assistant", "content": reply})
            sender_fresh["chat_history"] = sender_ctx[-200:]
            await asyncio.to_thread(part_record.save_user_memory, user_id, sender_fresh)
        else:
            context.append({"role": "user", "content": message})
            context.append({"role": "assistant", "content": reply})
            memory["chat_history"] = context[-200:]
            await asyncio.to_thread(part_record.save_user_memory, user_id, memory)

        # 随机延迟（拟人+防风控）
        delay = random.uniform(REPLY_DELAY_MIN, REPLY_DELAY_MAX)
        print(f"[延迟] 本次回复延迟 {delay:.1f} 秒")
        await asyncio.sleep(delay)

        return reply

    except Exception as e:
        print(f"[错误] 调用API失败: {e}")
        return f"[错误] 发生错误：{e}"
# =====================


# ====== 后台回复 ======
async def reply_private(websocket, user_id, msg_text, image=None):
    try:
        reply = await call_your_api(user_id, msg_text, image=image)
        await part_command.send_private(websocket, user_id, reply)
        log(f"[回复] 已回复私聊 {user_id}: {reply[:50]}")
        await summarize_memory(str(user_id))
    except Exception as e:
        log(f"[错误] 私聊回复任务异常 user={user_id}: {e}")


async def reply_group(websocket, group_id, mem_key, msg_text, image=None, reply_to=None):
    try:
        reply = await call_your_api(mem_key, msg_text, group_id=group_id, image=image)
        # 引用判断：目标消息距今超过阈值条才引用，避免每条都挂引用
        quote = None
        if reply_to is not None:
            settings = await asyncio.to_thread(part_record.load_settings)
            threshold = int(settings.get("quote_threshold", 3))
            newer = await asyncio.to_thread(
                part_record.count_group_history_after, group_id, reply_to
            )
            if newer >= threshold:
                quote = reply_to
                log(f"[引用] 群 {group_id} 目标消息距今 {newer} 条（阈值 {threshold}），引用发送")
        await part_command.send_group(websocket, group_id, reply, reply_to=quote)
        log(f"[回复] 已回复群 {group_id}: {reply[:50]}")
        # 发送者个人档案（算好感度）和群共享档案（记群状态，不算好感度）都要总结
        await summarize_memory(mem_key)
        await summarize_memory(f"group_{group_id}", update_favorability=False)
    except Exception as e:
        log(f"[错误] 群聊回复任务异常 group={group_id}: {e}")
# =====================


# ====== 消息处理 ======
async def _recv_loop(websocket, queue):
    """只负责收：API 响应回填 pending future，消息事件进队列。"""
    async for raw_message in websocket:
        try:
            event = json.loads(raw_message)
        except Exception:
            continue
        echo = event.get("echo")
        if echo and echo in _pending_api:
            fut = _pending_api.pop(echo)
            if not fut.done():
                fut.set_result(event)
            continue
        if event.get("post_type") == "message":
            await queue.put(event)


async def handler(websocket):
    print("实时监控已连接！")
    queue = asyncio.Queue()
    recv_task = asyncio.create_task(_recv_loop(websocket, queue))
    try:
        while True:
            event = await queue.get()
            # 处理消息
            if event.get("post_type") == "message":
                # 同一条消息去重
                mid = event.get("message_id")
                if mid is not None:
                    if mid in _seen_msg_ids:
                        log(f"[去重] 重复消息已跳过 (message_id={mid})")
                        continue
                    _seen_msg_ids.add(mid)
                    _seen_msg_queue.append(mid)
                    while len(_seen_msg_queue) > 500:
                        _seen_msg_ids.discard(_seen_msg_queue.popleft())

                admin = part_command.is_admin(event, ADMIN_QQ)

                # ===== 私聊消息 ======
                if event.get("message_type") == "private":
                    user_id = event["user_id"]
                    msg_text = ""
                    image_url = None
                    image_is_meme = False
                    quoted_id = None
                    for seg in event.get("message", []):
                        if seg.get("type") == "text":
                            msg_text += seg.get("data", {}).get("text", "")
                        elif seg.get("type") == "reply":
                            quoted_id = seg.get("data", {}).get("id")
                        elif image_url is None and seg.get("type") in ("image", "mface"):
                            seg_data = seg.get("data", {})
                            if seg_data.get("url"):
                                image_url = seg_data["url"]
                                image_is_meme = seg.get("type") == "mface" or str(seg_data.get("sub_type")) == "1"

                    if not msg_text.strip() and not image_url:
                        continue

                    # 留档：所有说过话的私聊都会出现在档案汇总里
                    sender = event.get("sender", {})
                    await asyncio.to_thread(
                        part_record.touch_archive, str(user_id),
                        sender.get("card") or sender.get("nickname")
                    )

                    # 先过指令
                    if await part_command.handle_private_command(
                        websocket, user_id, msg_text, admin,
                        lambda m: asyncio.create_task(reply_private(websocket, user_id, m))
                    ):
                        continue

                    print(f"收到私聊消息: {msg_text}")
                    # 频率上限
                    now = time.time()
                    settings = await asyncio.to_thread(part_record.load_settings)
                    cooldown = settings.get("reply_cooldown", REPLY_COOLDOWN)
                    rate_key = f"private_{user_id}"
                    if now - _last_reply_time.get(rate_key, 0) < cooldown:
                        log(f"[频率] 私聊 {user_id} 回复间隔不足 {cooldown:g} 秒，已跳过")
                        continue
                    _last_reply_time[rate_key] = now
                    # 识图：被冷却跳过的消息不做识别，省调用
                    record_text, vision_image = msg_text.strip(), None
                    if image_url:
                        tag, vision_image = await prepare_vision(image_url, image_is_meme, settings)
                        record_text = (record_text + tag).strip()
                    if quoted_id:
                        quoted = await resolve_quoted_text(websocket, str(user_id), quoted_id)
                        if quoted:
                            record_text = f"（回复「{quoted}」）{record_text}"
                    asyncio.create_task(reply_private(websocket, user_id, record_text, vision_image))

                # ===== 群聊消息 ======
                elif event.get("message_type") == "group":
                    group_id = event["group_id"]
                    user_id = event["user_id"]

                    if str(user_id) == str(BOT_QQ):
                        continue

                    is_at_me = False
                    msg_text = ""
                    image_url = None
                    image_is_meme = False
                    quoted_id = None

                    for seg in event.get("message", []):
                        if seg.get("type") == "at":
                            #判断@
                            at_qq = str(seg.get("data", {}).get("qq", ""))
                            if at_qq == str(BOT_QQ):
                                is_at_me = True
                        elif seg.get("type") == "text":
                            msg_text += seg.get("data", {}).get("text", "")
                        elif seg.get("type") == "reply":
                            quoted_id = seg.get("data", {}).get("id")
                        elif image_url is None and seg.get("type") in ("image", "mface"):
                            seg_data = seg.get("data", {})
                            if seg_data.get("url"):
                                image_url = seg_data["url"]
                                image_is_meme = seg.get("type") == "mface" or str(seg_data.get("sub_type")) == "1"

                    if not msg_text.strip() and not image_url:
                        continue

                    sender = event.get("sender", {})
                    nickname = sender.get("card") or sender.get("nickname") or str(user_id)
                    # 留档：群聊本体 + 群友个人，两套档案分别记录
                    await asyncio.to_thread(
                        part_record.touch_group_archives,
                        group_id, user_id, nickname
                    )
                    settings = await asyncio.to_thread(part_record.load_settings)
                    name_keywords = settings.get("name_keywords", BOT_NAME_KEYWORDS)
                    # 喊名字与@同级触发、同冷却档；群聊一律使用默认人格
                    is_called = any(name in msg_text for name in name_keywords)
                    triggered = is_at_me or is_called

                    # 指令只在被触发时响应，且不进群共享档案
                    if triggered and msg_text.strip().startswith("/"):
                        mem_key = f"group_{group_id}_{user_id}"
                        if await part_command.handle_group_command(
                            websocket, group_id, user_id, msg_text, admin,
                            lambda m: asyncio.create_task(reply_group(websocket, group_id, mem_key, m))
                        ):
                            continue

                    # 闲聊消息写入群共享上下文（识图 caption / 引用原文一并写入，后续消息才能看懂语境）
                    record_text, vision_image = msg_text.strip(), None
                    if image_url:
                        tag, vision_image = await prepare_vision(image_url, image_is_meme, settings)
                        record_text = (record_text + tag).strip()
                    if quoted_id:
                        quoted = await resolve_quoted_text(websocket, f"group_{group_id}", quoted_id)
                        if quoted:
                            record_text = f"（回复「{quoted}」）{record_text}"
                    await asyncio.to_thread(
                        part_record.append_group_history, group_id, "user",
                        f"{nickname}：{record_text}", message_id=mid
                    )

                    # 触发判定：@ 或喊名字直接触发，否则自主潜水模式下按 infer 设置分流
                    if not triggered:
                        at_only = settings.get("at_only", True)
                        if at_only:
                            log(f"[群聊] 群 {group_id} {nickname} 的消息未触发（当前为仅提及模式）")
                            continue
                        infer_mode = settings.get("infer_mode", "AI")
                        if infer_mode == "PY":
                            # 概率掷骰
                            probability = settings.get("dive_probability", 10)
                            roll = random.random() * 100
                            if roll >= probability:
                                log(f"[掷骰] 群 {group_id} 掷出 {roll:.1f} >= {probability:g}%，不回复")
                                continue
                            log(f"[掷骰] 群 {group_id} 掷出 {roll:.1f} < {probability:g}%，插嘴回复")
                        else:
                            # AI判断
                            if not await should_reply(group_id):
                                log(f"[群聊] 群 {group_id} {nickname} 的消息判断为不回复")
                                continue
                            log(f"[判断] 群 {group_id} 决定参与互动")

                    log(f"群 {group_id} 收到 {nickname} 的消息: {msg_text}")
                    # 频率上限：@/喊名字与自动插嘴分开计时，互不占用（@之间仍保冷却防刷屏）
                    now = time.time()
                    if triggered:
                        cooldown = settings.get("reply_cooldown_at", REPLY_COOLDOWN_AT)
                        rate_key = f"group_{group_id}_at"
                    else:
                        cooldown = settings.get("reply_cooldown", REPLY_COOLDOWN)
                        rate_key = f"group_{group_id}"
                    if now - _last_reply_time.get(rate_key, 0) < cooldown:
                        log(f"[频率] 群 {group_id} 回复间隔不足 {cooldown:g} 秒，已跳过")
                        continue
                    _last_reply_time[rate_key] = now
                    asyncio.create_task(reply_group(
                        websocket, group_id, f"group_{group_id}_{user_id}", record_text,
                        image=vision_image, reply_to=mid
                    ))
    except Exception as e:
        log(f"[错误] 连接断开或发生异常: {e}")
        await send_wechat_alert("死老冯腾讯可能又风控了",f"处理消息时发生异常：{e}")
    finally:
        recv_task.cancel()
# =====================


# ====== 胡思乱想 ======
# 空闲时回忆高重要度记忆，生成内心独白存进向量库（type=musing）
MUSING_TICK = 60  # 调度检查周期（秒）
MUSING_TIMEOUT = 60  # 单次发散 LLM 调用超时（秒）
_musing_started = False
_musing_last_attempt = {}  # 避免没有素材的档案每分钟重试

async def musing_task():
    backoff = 60
    while True:
        try:
            await asyncio.sleep(MUSING_TICK if backoff == 60 else backoff)
            musing_prompt = load_internal_prompt("11_胡思乱想.txt")
            if not musing_prompt:
                log("[胡思乱想] 提示词已被关闭，跳过本轮")
                backoff = 60
                continue

            settings = await asyncio.to_thread(part_record.load_settings)
            scopes = await asyncio.to_thread(part_record.list_musing_scopes)
            now = time.time()

            due = []
            for item in scopes:
                scope = item["scope"]
                enabled, interval = part_record.musing_policy(settings, scope)
                if not enabled:
                    continue
                last = max(float(item.get("last_musing") or 0),
                           _musing_last_attempt.get(scope, 0))
                overdue = now - last - interval * 60
                if overdue >= 0:
                    due.append((overdue, scope))

            if not due:
                backoff = 60
                continue

            due.sort(reverse=True)
            scope = due[0][1]
            _musing_last_attempt[scope] = now
            log(f"[胡思乱想] 档案 {scope} 到期，开始回忆")

            memories = await asyncio.to_thread(
                part_record.get_high_importance_memories, 1,
                safe_float(settings.get("musing_min_importance"), 0.7), scope
            )
            if not memories:
                log(f"[胡思乱想] 档案 {scope} 没有高重要度记忆，跳过")
                backoff = 60
                continue

            for uid, src_id, content in memories:
                try:
                    route = part_api.get_route("sub")
                    payload = {
                        "model": route.get("model") or SUMMARY_MODEL,
                        "messages": [
                            {"role": "system", "content": musing_prompt},
                            {"role": "user", "content": f"往事：{content}"}
                        ],
                        "stream": False
                    }
                    musing = (await asyncio.wait_for(
                        _post_json(payload, api_url=route.get("url"), api_key=route.get("key")), timeout=MUSING_TIMEOUT
                    )).strip()
                    if musing:
                        await asyncio.to_thread(
                            part_record.add_vector, uid, musing, "musing",
                            0.0, 0.0, 0.6, src_id, 0.4
                        )
                        log(f"[胡思乱想] 基于 {uid} 的记忆生成独白: {musing[:40]}")
                except asyncio.TimeoutError:
                    log(f"[胡思乱想] 生成超时（{MUSING_TIMEOUT}秒），跳过该条")
                except Exception as e:
                    log(f"[胡思乱想] 生成失败: {e}")
            backoff = 60
        except asyncio.CancelledError:
            break
        except Exception as e:
            log(f"[胡思乱想] 任务异常，{backoff}秒后重试: {e}")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 3600)
# =====================


# ====== 主程序 ======
async def main():
    while True:
        try:
            async with websockets.serve(handler, "0.0.0.0", 8080):
                print("程序已启动，等待 实时监控 连接...")
                global _musing_started
                if not _musing_started:
                    _musing_started = True
                    asyncio.create_task(musing_task())
                await asyncio.Future()
        except asyncio.CancelledError:
            print("[系统] 程序被手动终止。")
            break
        except Exception as e:
            print(f"[错误] 服务异常退出: {e}，5秒后自动重启...")
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
