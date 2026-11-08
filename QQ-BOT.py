import asyncio
import base64
import datetime
import json
import random
import re
import sys
import time
from collections import deque
import websockets
import aiohttp
import part_record
import part_command
import part_prompt
import part_api
import part_search
import part_sticker
import os

# Windows 控制台默认 GBK，打印 emoji 会直接 UnicodeEncodeError 干崩主循环
# 强制 UTF-8 + 容错替换，print 永远安全
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

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

# ===== 机器人连续互动防刷（概率步长衰减软保底） =====
_group_bot_turns = {}
BOT_INTERACTION_TIMEOUT = 300.0  # 超过5分钟无互动自动清零
_active_ws = None

def check_bot_interaction(group_id, user_id, settings):
    """
    针对已标注的已知机器人检查是否允许回复（固定步长概率衰减软保底）。
    返回: (allowed: bool, prob: float, current_turns: int)
    从 100% 开始，第1轮 100%，第2轮 100 - step%，第3轮 100 - 2*step%...
    """
    known_bots = set(str(b).strip() for b in settings.get("known_bots", []))
    if str(user_id) not in known_bots:
        return True, 100.0, 0

    step = float(settings.get("bot_decay_step", 25.0))
    now = time.time()
    state = _group_bot_turns.get(group_id, {"turns": 0, "last_time": 0.0})
    turns = state.get("turns", 0)
    if now - state.get("last_time", 0.0) > BOT_INTERACTION_TIMEOUT:
        turns = 0

    prob = max(0.0, 100.0 - (turns * step))
    roll = random.random() * 100.0
    allowed = (roll < prob)
    return allowed, prob, turns

def record_bot_reply(group_id, user_id, settings):
    """当真正对已知机器人发出回复时调用，递增连续互动轮数"""
    known_bots = set(str(b).strip() for b in settings.get("known_bots", []))
    if str(user_id) in known_bots:
        now = time.time()
        state = _group_bot_turns.get(group_id, {"turns": 0, "last_time": 0.0})
        turns = state.get("turns", 0)
        if now - state.get("last_time", 0.0) > BOT_INTERACTION_TIMEOUT:
            turns = 0
        _group_bot_turns[group_id] = {"turns": turns + 1, "last_time": now}

def reset_bot_turns_if_human(group_id, user_id, settings):
    """当收到真人群友消息时，立刻重置该群的机器人连续对话轮数"""
    known_bots = set(str(b).strip() for b in settings.get("known_bots", []))
    if str(user_id) not in known_bots and str(user_id) != str(BOT_QQ):
        if group_id in _group_bot_turns and _group_bot_turns[group_id].get("turns", 0) > 0:
            _group_bot_turns[group_id] = {"turns": 0, "last_time": 0.0}
# ===================================================

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


# 常见的推理/思考模型与各类反代包装的思维链/工具调用标签
_REASONING_TAGS = (
    "think|thought|thinking|reasoning|reason|reflection|reflect|"
    "scratchpad|scratch|inner_monologue|internal_monologue|chain_of_thought|"
    "cot|analysis|draft|plan|planning|internal|"
    "antthinking|antthought|anthinking|anthought|"
    "invoke|invoking|invocation|tool_call|tool_calls|function_call|call"
)

# 1. 如果文本头部就是思考/工具调用块，一次性切到【最后一个】闭合标签 </tag>
# 专门解决反代畸形的多闭合/嵌套结构（如：<invoke name="think">\n</invoke>...code...</invoke>）
_LEADING_REASONING_RE = re.compile(
    rf"^\s*<(?P<tag>{_REASONING_TAGS})\b[^>]*>[\s\S]*</(?:{_REASONING_TAGS})>\s*",
    re.IGNORECASE
)

# 2. 局部成对闭合标签剥离（非贪婪，处理中段标签）
_CLOSED_TAG_RE = re.compile(
    rf"<(?P<tag>{_REASONING_TAGS})\b[^>]*>[\s\S]*?</(?:{_REASONING_TAGS})>",
    re.IGNORECASE
)

# 3. 匹配方括号式 [reasoning]...[/reasoning]
_BRACKET_TAG_RE = re.compile(
    rf"\[(?P<tag>{_REASONING_TAGS})\b[^\]]*\][\s\S]*?\[/(?:{_REASONING_TAGS})\]",
    re.IGNORECASE
)

# 4. 匹配 markdown 代码块 ```reasoning ... ```
_CODEBLOCK_TAG_RE = re.compile(
    rf"```(?:{_REASONING_TAGS})\b[\s\S]*?```\s*",
    re.IGNORECASE
)

# 5. 中文思考标签
_CHINESE_THINK_RE = re.compile(
    r"【(?:思考过程|思维链|思考|推理)】[\s\S]*?【/(?:思考过程|思维链|思考|推理)】|"
    r"【(?:思考过程|思维链|思考|推理)】[\s\S]*?【(?:思考结束|推理结束)】",
    re.IGNORECASE
)

# 6. 未闭合截断标签（从标签开始直到文本末尾全部视为截断的思考/调用，彻底丢弃）
_UNCLOSED_TAG_RE = re.compile(
    rf"<(?P<tag>{_REASONING_TAGS})\b[^>]*>[\s\S]*$",
    re.IGNORECASE
)
_UNCLOSED_BRACKET_RE = re.compile(
    rf"\[(?P<tag>{_REASONING_TAGS})\b[^\]]*\][\s\S]*$",
    re.IGNORECASE
)
_UNCLOSED_CHINESE_RE = re.compile(
    r"【(?:思考过程|思维链|思考|推理)】[\s\S]*$",
    re.IGNORECASE
)

# 7. 残留孤立标签清理（含闭合标签遗留）
_ORPHAN_TAG_RE = re.compile(
    rf"</?(?:{_REASONING_TAGS})\b[^>]*>",
    re.IGNORECASE
)
_ORPHAN_BRACKET_RE = re.compile(
    rf"\[/?(?:{_REASONING_TAGS})\b[^\]]*\]",
    re.IGNORECASE
)


def clean_reply_format(text):
    """通杀剥离所有推理模型的思维链、伪工具调用、思考草稿及格式输出外壳：
    1. XML/HTML式标签（<think>, <reasoning>, <thought>, <thinking>, <reflection>, <invoke name="think"> 等）
    2. 方括号式标签（[reasoning]...[/reasoning] 等）与 markdown 代码块（```thinking ... ```）
    3. 中文思考标签（【思考过程】...【思考结束】）
    4. 未闭合截断的思考块（防止因 max_tokens 截断而把半截内心戏泄漏）
    5. 遗留的 <!--?--> 标记和 <output> 标签
    """
    if not isinstance(text, str):
        return text

    # 先提取 <output>...</output>（若存在）
    m = re.search(r"<output>(.*?)</output>", text, re.S)
    if m:
        text = m.group(1)
    else:
        text = re.sub(r"</?output>", "", text)

    # 剥掉多余标记
    text = re.sub(r"<!--\?-->", "", text)

    # 1. 核心：如果回复头部就是思考/调用块，一次性切到最后一个闭合标签
    text = _LEADING_REASONING_RE.sub("", text)

    # 2. 循环清洗中段和残余标签（多跑两轮防嵌套）
    for _ in range(3):
        prev = text
        text = _CLOSED_TAG_RE.sub("", text)
        text = _BRACKET_TAG_RE.sub("", text)
        text = _CODEBLOCK_TAG_RE.sub("", text)
        text = _CHINESE_THINK_RE.sub("", text)
        text = _UNCLOSED_TAG_RE.sub("", text)
        text = _UNCLOSED_BRACKET_RE.sub("", text)
        text = _UNCLOSED_CHINESE_RE.sub("", text)
        text = _ORPHAN_TAG_RE.sub("", text)
        text = _ORPHAN_BRACKET_RE.sub("", text)
        if text == prev:
            break

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

# ===== 拒答检测辅助 ======
# 词表保留备用，恢复时在 call_your_api 里加回 is_refusal_text 判断即可。
_REFUSAL_MARKERS = (
    "安全准则", "安全策略", "内容政策", "内容准则", "安全指南",
    "作为AI", "作为人工智能", "作为语言模型",
    "不能协助", "无法满足", "不适宜的内容",
    "safety guideline", "content policy", "safety constraints",
    "I cannot", "I can't", "cannot fulfill", "can't fulfill",
    "I'm unable", "I am unable", "unable to fulfill",
    "cannot assist", "can't assist", "must decline",
    "against my guidelines", "sexually explicit",
)

def is_refusal_text(text):
    if not isinstance(text, str):
        return False
    return any(marker in text for marker in _REFUSAL_MARKERS)
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


# 现实时间感知
def get_current_time_desc():
    """获取当前时间的拟人自然语言描述，赋予机器人精准的现实时间观念"""
    now = datetime.datetime.now()
    weekdays = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
    weekday = weekdays[now.weekday()]
    hour = now.hour
    if 5 <= hour < 9:
        period = "清晨 / 早晨"
    elif 9 <= hour < 12:
        period = "上午"
    elif 12 <= hour < 14:
        period = "中午 / 午休时段"
    elif 14 <= hour < 18:
        period = "下午 / 工作摸鱼时段"
    elif 18 <= hour < 20:
        period = "傍晚 / 晚饭时分"
    elif 20 <= hour < 23:
        period = "夜晚 / 休闲时段"
    else:
        period = "深夜 / 凌晨入眠时段"
    return f"{now.strftime('%Y年%m月%d日')} {weekday} {now.strftime('%H:%M')}（{period}）"


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

    # 现实时间感知
    active_prompt += (
        f"\n\n# 现实时间感知\n"
        f"当前时间：{get_current_time_desc()}\n"
        "（你明确感知当前的具体时刻、星期与时段作息，请让对话自然贴合当下时间，严禁机械报时）"
    )

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

    log(f"[系统] 用户 {user_id} 本次提示词长度: {len(active_prompt)} 字符")
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
            if "choices" not in data:
                # 上游报错（限流/参数不支持等）：带状态码和原文抛出，避免裸 KeyError
                raise RuntimeError(f"API返回异常 status={resp.status} body={json.dumps(data, ensure_ascii=False)[:300]}")
            choice = data["choices"][0]
            content = (choice.get("message") or {}).get("content")
            if content is None:
                # 安全拦截/空生成时 content 为 null，记下来方便排查拒答
                log(f"[API] 模型返回空内容 finish_reason={choice.get('finish_reason')}")
            return content


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
        caption = ((await asyncio.wait_for(
            _post_json(payload, api_url=route.get("url"), api_key=route.get("key")),
            timeout=30
        )) or "").strip()
        caption = re.sub(r"\s+", " ", caption)[:60]
        log(f"[识图] 图片描述: {caption}")
        return caption
    except Exception as e:
        log(f"[识图] 描述生成失败: {e}")
        return ""


async def prepare_vision(image_url, is_meme, settings, sender_is_admin=True):
    """返回 (record_tag, data_uri)。record_tag 是写进历史的文本替身，如 [图片：xxx]。"""
    if not settings.get("vision_enabled", True):
        return ("[表情包]" if is_meme else "[图片]"), None
    if settings.get("vision_admin_only", False) and not sender_is_admin:
        return ("[表情包]" if is_meme else "[图片]"), None
    prob = float(settings.get("vision_meme_probability", 30)) if is_meme \
        else float(settings.get("vision_probability", 100))
    if prob < 100:
        roll = random.random() * 100
        if roll >= prob:
            log(f"[识图] {'表情包' if is_meme else '图片'}掷出 {roll:.1f} >= {prob:g}%，跳过识别")
            return ("[表情包]" if is_meme else "[图片]"), None
    data_uri = await fetch_image_b64(image_url)
    if not data_uri:
        return ("[表情包]" if is_meme else "[图片]"), None
    caption = await vision_caption(data_uri)
    if caption:
        return f"[{'表情包' if is_meme else '图片'}：{caption}]", data_uri
    return ("[表情包]" if is_meme else "[图片]"), data_uri
# ====================


# ====== 后台任务安全包装 ======
# asyncio.create_task 默认会吞掉协程里的异常，导致表情包捕获失败时后台静默崩溃
def create_safe_task(coro, label="后台任务"):
    task = asyncio.create_task(coro)
    def _log_exception(t):
        try:
            t.result()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log(f"[{label}] 协程发生异常: {e}")
    task.add_done_callback(_log_exception)
    return task


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


# 回复判断（返回 {"reply": bool, "search": 搜索词或None}，search 由分模型顺手提取）
async def should_reply(group_id):
    judge_prompt = load_internal_prompt("09_群聊判断.txt")
    if not judge_prompt:
        log("[判断] 判断提示词已被关闭，默认不回复")
        return {"reply": False, "search": None}
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
        # 兼容两种输出：JSON（{"reply": true, "emotion": "...", "search": "..."}）或纯文本 [回复]/[忽略]
        result = parse_json_loose(raw)
        if isinstance(result, dict):
            reply_flag = bool(result.get("reply"))
            emotion = str(result.get("emotion", "")).strip()
            reason = str(result.get("reason", "")).strip()
            search_q = str(result.get("search", "") or "").strip()
            search_reason = str(result.get("search_reason", "") or "").strip()
            # 搜索字段无论是否提取都完整落日志：能区分"没判回"和"判回了但填了无"，以及理由
            log(f"[判断] 群 {group_id} 待判断: {lines[-1][:40] if lines else ''} | 输出: {'回复' if reply_flag else '忽略'} 情绪={emotion} 理由={reason} 搜索={search_q or '无'} 搜索理由={search_reason or '无'}")
            if search_q.lower() in ("", "无", "none", "null", "不需要"):
                search_q = None
            return {"reply": reply_flag, "search": search_q}
        log(f"[判断] 群 {group_id} 待判断: {lines[-1][:40] if lines else ''} | 输出: {(raw or '')[:30]}")
        return {"reply": "[回复]" in (raw or ""), "search": None}
    except Exception as e:
        log(f"[判断] 群 {group_id} 判断失败（默认不回复）: {e}")
        return {"reply": False, "search": None}


# 私聊/群@搜索词提取（借用 09_群聊判断 的搜索准则，仅用于事实提问检出）
async def extract_search_query_private(user_id, msg_text, label="私聊"):
    if not msg_text or not msg_text.strip():
        return None
    judge_prompt = load_internal_prompt("09_群聊判断.txt")
    if not judge_prompt:
        return None
    try:
        user_mem = await asyncio.to_thread(
            part_record.load_user_memory, str(user_id)
        )
        history = user_mem.get("chat_history", [])[-5:]
        lines = []
        for msg in history:
            role_prefix = "我：" if msg.get("role") == "assistant" else "对方："
            lines.append(f"{role_prefix}{msg.get('content', '')}")
        if lines:
            judge_input = "【最近聊天记录】\n" + "\n".join(lines) + f"\n\n【新消息】\n对方：{msg_text}"
        else:
            judge_input = f"【新消息】\n对方：{msg_text}"

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
        result = parse_json_loose(raw)
        if isinstance(result, dict):
            search_q = str(result.get("search", "") or "").strip()
            search_reason = str(result.get("search_reason", "") or "").strip()
            log(f"[判断] {label} {user_id} 待判断: {msg_text[:40]} | 搜索={search_q or '无'} 搜索理由={search_reason or '无'}")
            if search_q.lower() in ("", "无", "none", "null", "不需要"):
                return None
            return search_q
        return None
    except Exception as e:
        log(f"[判断] 私聊 {user_id} 搜索提取异常: {e}")
        return None


# ====== 联网搜索 ======
# 显式触发词（本地零成本规则）：群友明确说"搜一下"时才命中，不搞泛匹配防误触
_SEARCH_CMD = re.compile(r"搜一下|搜索一下|帮我搜|帮我查|查一下|百度一下|谷歌一下|百度搜索|谷歌搜索")
# 搜索冷却：按作用域（群/私聊）各自计时，冷却内降级为无资料回复
_last_search_time = {}


def search_cooling(scope, settings):
    """搜索冷却预判：冷却中返回 True。在提取搜索词之前调用，避免冷却期内白调分模型。"""
    cooldown = float(settings.get("search_cooldown", 600))
    return (time.time() - _last_search_time.get(scope, 0)) < cooldown


# 事实问句预过滤：含疑问/时效特征才调分模型提取搜索词，避免每条私聊/@都白调一次
_FACTUAL_HINT = re.compile(
    r"[?？]|吗|嘛|呢|什么|啥|谁|哪|何时|多少|几|为什么|为啥|是不是|有没有|知不知道|"
    r"最新|最近|近期|今天|昨天|今年|现在|当前|什么时候|啥时候|发布|上线|公测|开服|涨价|降价|价格|时间|日期"
)


def looks_like_factual_query(text):
    """本地零成本预检：不像事实/时效问句就不走分模型提取（宁宽勿严，误放进来的由分模型判"无"）。"""
    return bool(_FACTUAL_HINT.search(text or ""))


def extract_explicit_query(text, enabled=True):
    """从消息里提取显式搜索指令后的关键词，没有指令或开关关闭时返回 None。"""
    if not enabled:
        return None
    m = _SEARCH_CMD.search(text or "")
    if not m:
        return None
    q = text[m.end():].strip(" ，。,.?？!！:：~～")
    return q if len(q) >= 2 else None


async def run_search(scope, query, settings):
    """执行联网检索。返回参考资料文本；冷却中/未启用/失败均返回 None（降级正常回复）。"""
    if not query or not settings.get("search_enabled", True):
        return None
    cooldown = float(settings.get("search_cooldown", 600))
    now = time.time()
    if now - _last_search_time.get(scope, 0) < cooldown:
        log(f"[搜索] {scope} 搜索冷却中（{cooldown:g}s），跳过联网降级回复")
        return None
    _last_search_time[scope] = now
    log(f"[搜索] {scope} 触发联网检索: {query}")
    try:
        result = await part_search.search_web(
            query, tavily_key=settings.get("tavily_api_key", "")
        )
    except Exception as e:
        log(f"[搜索] {scope} 检索异常: {e}")
        # 失败不给满冷却，60 秒后即可重试
        _last_search_time[scope] = now - cooldown + 60
        return None
    if result:
        log(f"[搜索] {scope} 检索完成（{len(result)} 字）: {result[:60]}")
    else:
        log(f"[搜索] {scope} 未搜到有效资料，按无资料回复")
        # 无结果说明资料本来就不存在，短冷却防连续重搜
        _last_search_time[scope] = now - cooldown + 120
    return result


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

async def call_your_api(user_id, message, group_id=None, image=None, search_context=None):
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

    # 联网检索资料：临时的 system 消息注入，不进历史不污染记忆
    if search_context:
        messages_with_prompt.append({"role": "system", "content":
            f"【实时检索资料】\n{search_context}\n"
            f"（以上是刚联网搜到的信息，时效性优先于你已有的记忆，回复时自然运用即可，不要暴露资料来源）"
        })

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
        if not reply:
            # 模型拒答/空回复：静默跳过，不入库不发送，避免 None 污染历史
            log(f"[回复] 模型返回空内容（可能被安全策略拦截），本条跳过 user={user_id}")
            return None

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

        # 随机延迟（拟人+防风控，可设 0 关闭）
        d_min = float(settings.get("reply_delay_min", REPLY_DELAY_MIN))
        d_max = float(settings.get("reply_delay_max", REPLY_DELAY_MAX))
        if d_max < d_min:
            d_min, d_max = d_max, d_min
        delay = random.uniform(d_min, d_max) if d_max > 0 else 0
        if delay > 0:
            log(f"[延迟] 本次回复延迟 {delay:.1f} 秒")
            await asyncio.sleep(delay)

        return reply

    except Exception as e:
        # API 异常静默跳过：错误文本绝不发进群/私聊，避免"发生错误"刷屏
        log(f"[错误] 调用API失败 user={user_id}: {e}")
        return None
# =====================


# ====== 后台回复 ======
async def send_sticker_message(websocket, sticker_seg, scope_label, group_id=None, user_id=None):
    if group_id is not None:
        action = "send_group_msg"
        params = {"group_id": group_id, "message": [sticker_seg]}
    else:
        action = "send_private_msg"
        params = {"user_id": user_id, "message": [sticker_seg]}
    resp = await call_onebot(websocket, action, params)
    if resp and resp.get("status") == "ok":
        log(f"[表情] 已单独发送表情到 {scope_label}")
        return True
    detail = json.dumps(resp, ensure_ascii=False)[:300] if resp else "无回执或超时"
    log(f"[表情] 发送失败 {scope_label}: {detail}")
    return False


async def reply_private(websocket, user_id, msg_text, image=None, search_context=None, is_duel=False):
    try:
        settings = await asyncio.to_thread(part_record.load_settings)
        memory = await asyncio.to_thread(part_record.load_user_memory, str(user_id))
        mood = (memory.get("state") or {}).get("mood", "")

        if is_duel:
            _, sticker_seg = await part_sticker.process_reply_stickers("", f"private_{user_id}", settings, is_duel=True, mood=mood)
            if sticker_seg:
                await send_sticker_message(websocket, sticker_seg, f"私聊 {user_id}", user_id=user_id)
            return

        reply = await call_your_api(user_id, msg_text, image=image, search_context=search_context)
        if reply is None or reply.startswith("[错误]"):
            return
        clean_text, sticker_seg = await part_sticker.process_reply_stickers(reply, f"private_{user_id}", settings, mood=mood)
        if clean_text.strip():
            await part_command.send_private(websocket, user_id, clean_text)
            log(f"[回复] 已回复私聊 {user_id}: {clean_text[:50]}")
        if sticker_seg:
            await send_sticker_message(websocket, sticker_seg, f"私聊 {user_id}", user_id=user_id)
        await summarize_memory(str(user_id))
    except Exception as e:
        log(f"[错误] 私聊回复任务异常 user={user_id}: {e}")


async def reply_group(websocket, group_id, mem_key, msg_text, image=None, reply_to=None, search_context=None, is_duel=False):
    try:
        settings = await asyncio.to_thread(part_record.load_settings)
        user_mem = await asyncio.to_thread(part_record.load_user_memory, mem_key)
        group_mem = await asyncio.to_thread(part_record.load_user_memory, f"group_{group_id}")
        mood = (user_mem.get("state") or {}).get("mood") or (group_mem.get("state") or {}).get("mood", "")

        if is_duel:
            _, sticker_seg = await part_sticker.process_reply_stickers("", f"group_{group_id}", settings, is_duel=True, mood=mood)
            if sticker_seg:
                await send_sticker_message(websocket, sticker_seg, f"群 {group_id}", group_id=group_id)
            return

        reply = await call_your_api(mem_key, msg_text, group_id=group_id, image=image, search_context=search_context)
        if reply is None or reply.startswith("[错误]"):
            return
        # 引用判断：目标消息距今超过阈值条才引用，避免每条都挂引用
        quote = None
        if reply_to is not None:
            threshold = int(settings.get("quote_threshold", 3))
            newer = await asyncio.to_thread(
                part_record.count_group_history_after, group_id, reply_to
            )
            if newer >= threshold:
                quote = reply_to
                log(f"[引用] 群 {group_id} 目标消息距今 {newer} 条（阈值 {threshold}），引用发送")

        clean_text, sticker_seg = await part_sticker.process_reply_stickers(reply, f"group_{group_id}", settings, mood=mood)
        if clean_text.strip():
            await part_command.send_group(websocket, group_id, clean_text, reply_to=quote)
            log(f"[回复] 已回复群 {group_id}: {clean_text[:50]}")
        if sticker_seg:
            await send_sticker_message(websocket, sticker_seg, f"群 {group_id}", group_id=group_id)
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
    global _active_ws
    _active_ws = websocket
    print("实时监控已连接！")
    queue = asyncio.Queue()
    recv_task = create_safe_task(_recv_loop(websocket, queue), "消息接收")
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

                # 捕获过滤：总开关 / 仅管理员 / 非管理员按概率抓取
                settings = await asyncio.to_thread(part_record.load_settings)
                _cap_enabled = settings.get("sticker_capture_enabled", True)
                _cap_admin_only = settings.get("sticker_capture_admin_only", True)
                _cap_prob = float(settings.get("sticker_capture_probability", 30.0))
                should_capture = False
                if _cap_enabled:
                    if _cap_admin_only:
                        should_capture = admin
                    else:
                        should_capture = admin or (random.random() * 100 < _cap_prob)
                if should_capture:
                    create_safe_task(part_sticker.capture_from_event(event), "表情捕获")

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
                        elif seg.get("type") == "mface":
                            seg_data = seg.get("data", {})
                            if seg_data.get("url") and image_url is None:
                                image_url = seg_data["url"]
                                image_is_meme = True
                            if not msg_text and seg_data.get("summary"):
                                msg_text = seg_data.get("summary")
                        elif image_url is None and seg.get("type") == "image":
                            seg_data = seg.get("data", {})
                            if seg_data.get("url"):
                                image_url = seg_data["url"]
                                image_is_meme = str(seg_data.get("sub_type")) == "1"

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
                        lambda m: create_safe_task(reply_private(websocket, user_id, m), "私聊回复"),
                        on_poke=lambda: create_safe_task(generate_and_send_proactive_message(force=True, specified_user_id=user_id), "主动搭讪测试")
                    ):
                        continue

                    print(f"收到私聊消息: {msg_text}")
                    if settings.get("sticker_capture_only", False):
                        log(f"[表情] 当前处于「纯捕获模式」，已捕获表情，跳过回复私聊 {user_id}")
                        continue

                    # 频率上限
                    now = time.time()
                    cooldown = settings.get("reply_cooldown", REPLY_COOLDOWN)
                    rate_key = f"private_{user_id}"
                    if now - _last_reply_time.get(rate_key, 0) < cooldown:
                        log(f"[频率] 私聊 {user_id} 回复间隔不足 {cooldown:g} 秒，已跳过")
                        continue
                    _last_reply_time[rate_key] = now
                    # 识图：被冷却跳过的消息不做识别，省调用
                    record_text, vision_image = msg_text.strip(), None
                    if image_url:
                        tag, vision_image = await prepare_vision(image_url, image_is_meme, settings, admin)
                        record_text = (record_text + tag).strip()
                        if should_capture and tag.startswith(("[表情包：", "[图片：")) and tag.endswith("]"):
                            cap = tag[5:-1]
                            create_safe_task(part_sticker.capture_from_event(event, caption=cap), "表情捕获")
                    if quoted_id:
                        quoted = await resolve_quoted_text(websocket, str(user_id), quoted_id)
                        if quoted:
                            record_text = f"（回复「{quoted}」）{record_text}"
                    # 私聊搜索：先看显式指令（搜一下xxx），未匹配则调分模型从问句中提取时效搜索词
                    # 冷却前置+问句预检：冷却期内或明显非事实问句时不调分模型
                    search_scope = f"private_{user_id}"
                    search_query = extract_explicit_query(
                        msg_text, settings.get("search_explicit_cmd_enabled", True)
                    )
                    if (
                        not search_query
                        and settings.get("search_enabled", True)
                        and not search_cooling(search_scope, settings)
                        and looks_like_factual_query(msg_text)
                    ):
                        search_query = await extract_search_query_private(user_id, msg_text)
                    search_ctx = await run_search(search_scope, search_query, settings)
                    is_duel = part_sticker.is_incoming_pure_sticker(event) and settings.get("sticker_duel_mode", False)
                    create_safe_task(reply_private(websocket, user_id, record_text, vision_image, search_ctx, is_duel=is_duel), "私聊回复")

                # ===== 群聊消息 ======
                elif event.get("message_type") == "group":
                    group_id = event["group_id"]
                    user_id = event["user_id"]

                    if str(user_id) == str(BOT_QQ):
                        continue

                    # 收到真人群友发言时，立即重置该群的机器人连续对话轮数
                    reset_bot_turns_if_human(group_id, user_id, settings)
                    known_bots = set(str(b).strip() for b in settings.get("known_bots", []))
                    is_from_bot = str(user_id) in known_bots
                    # 触发方式按群独立：群档案覆盖优先，否则跟随全局
                    at_only = await asyncio.to_thread(
                        part_record.effective_at_only, settings, f"group_{group_id}"
                    )

                    is_at_me = False
                    msg_text = ""
                    image_url = None
                    image_is_meme = False
                    quoted_id = None

                    for seg in event.get("message", []):
                        if seg.get("type") == "at":
                            # 判断@：@自己标记触发；@别人必须保留到文本里，否则模型会误以为在问自己
                            at_qq = str(seg.get("data", {}).get("qq", ""))
                            if at_qq == str(BOT_QQ):
                                is_at_me = True
                            else:
                                at_data = seg.get("data", {})
                                at_name = str(at_data.get("name") or "").strip()
                                if at_qq.lower() == "all":
                                    msg_text += "@全体成员 "
                                elif at_name:
                                    msg_text += f"@{at_name} "
                                elif at_qq:
                                    msg_text += f"@{at_qq} "
                        elif seg.get("type") == "text":
                            msg_text += seg.get("data", {}).get("text", "")
                        elif seg.get("type") == "reply":
                            quoted_id = seg.get("data", {}).get("id")
                        elif image_url is None and seg.get("type") in ("image", "mface"):
                            seg_data = seg.get("data", {})
                            if seg_data.get("url"):
                                image_url = seg_data["url"]
                                image_is_meme = seg.get("type") == "mface" or str(seg_data.get("sub_type")) == "1"
                        elif seg.get("type") == "mface":
                            seg_data = seg.get("data", {})
                            if not msg_text and seg_data.get("summary"):
                                msg_text = seg_data.get("summary")

                    if not msg_text.strip() and not image_url:
                        continue

                    sender = event.get("sender", {})
                    nickname = sender.get("card") or sender.get("nickname") or str(user_id)
                    # 留档：群聊本体 + 群友个人，两套档案分别记录
                    await asyncio.to_thread(
                        part_record.touch_group_archives,
                        group_id, user_id, nickname
                    )
                    name_keywords = settings.get("name_keywords", BOT_NAME_KEYWORDS)
                    # 喊名字与@同级触发、同冷却档；群聊一律使用默认人格
                    is_called = any(name in msg_text for name in name_keywords)
                    triggered = is_at_me or is_called

                    # 指令只在被触发时响应，且不进群共享档案（未@时不处理，防止群友使唤别家bot时乱插嘴）
                    if triggered and msg_text.strip().startswith("/"):
                        if is_from_bot:
                            log(f"[指令] 忽略来自已知机器人 {user_id} 的指令: {msg_text.strip()[:30]}")
                            continue
                        mem_key = f"group_{group_id}_{user_id}"
                        if await part_command.handle_group_command(
                            websocket, group_id, user_id, msg_text, admin,
                            lambda m: create_safe_task(reply_group(websocket, group_id, mem_key, m), "群聊回复"),
                            on_poke=lambda: create_safe_task(
                                generate_and_send_proactive_message(force=True, specified_group_id=group_id),
                                "群主动冒泡测试"
                            )
                        ):
                            continue

                    if settings.get("sticker_capture_only", False):
                        log(f"[表情] 当前处于「纯捕获模式」，已捕获表情，跳过回复群聊 {group_id}")
                        continue

                    # 频率上限前置：@/喊名字与自动插嘴分开计时，互不占用
                    # 两个冷却都置顶：冷却期内消息只入档，不做识图/判回/搜索；冷却外才走判回
                    now = time.time()
                    if triggered:
                        cooldown = settings.get("reply_cooldown_at", REPLY_COOLDOWN_AT)
                        rate_key = f"group_{group_id}_at"
                    else:
                        cooldown = settings.get("reply_cooldown", REPLY_COOLDOWN)
                        rate_key = f"group_{group_id}"
                    in_cooldown = now - _last_reply_time.get(rate_key, 0) < cooldown

                    # 闲聊消息写入群共享上下文（识图 caption / 引用原文一并写入，后续消息才能看懂语境）
                    # 冷却期内只入档案：不做识图（分模型调用），图片按纯文本入库
                    record_text, vision_image = msg_text.strip(), None
                    if image_url and not in_cooldown:
                        tag, vision_image = await prepare_vision(image_url, image_is_meme, settings, admin)
                        record_text = (record_text + tag).strip()
                        if should_capture and tag.startswith(("[表情包：", "[图片：")) and tag.endswith("]"):
                            cap = tag[5:-1]
                            create_safe_task(part_sticker.capture_from_event(event, caption=cap), "表情捕获")
                    if quoted_id:
                        quoted = await resolve_quoted_text(websocket, f"group_{group_id}", quoted_id)
                        if quoted:
                            record_text = f"（回复「{quoted}」）{record_text}"
                    await asyncio.to_thread(
                        part_record.append_group_history, group_id, "user",
                        f"{nickname}：{record_text}", message_id=mid
                    )

                    # 显式搜索指令（搜一下xxx）：必须在被触发（@或喊名字）或自主互动模式下才响应
                    # 仅提及模式下未被 @/喊名字 绝对不触发，防止群友使唤别家 bot（"搜一下/查一下"）时横插一嘴
                    search_query = None
                    if triggered or not at_only:
                        search_query = extract_explicit_query(
                            msg_text, settings.get("search_explicit_cmd_enabled", True)
                        )

                    # 未触发且仅提及模式：直接跳过，不进冷却也不判定
                    if not triggered and at_only:
                        log(f"[群聊] 群 {group_id} {nickname} 的消息未触发（本群为仅提及模式）")
                        continue

                    # 冷却期内直接跳过，连判定（分模型调用/掷骰）都不做，省算力
                    if in_cooldown:
                        log(f"[频率] 群 {group_id} 回复间隔不足 {cooldown:g} 秒，已跳过（仅入档案）")
                        continue

                    # 触发判定：@ 或喊名字直接触发；显式搜索指令同样直接响应，不过判回
                    # 其余自主潜水模式下按 infer 设置分流
                    if not triggered and not search_query:
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
                            # AI判断（分模型顺手提取搜索词，见 09_群聊判断.txt）
                            verdict = await should_reply(group_id)
                            if not verdict["reply"]:
                                log(f"[群聊] 群 {group_id} {nickname} 的消息判断为不回复")
                                continue
                            log(f"[判断] 群 {group_id} 决定参与互动")
                            if not search_query:
                                search_query = verdict.get("search")
                    elif search_query and not triggered:
                        log(f"[搜索] 群 {group_id} 收到显式搜索指令，直接响应: {search_query}")

                    # @提及无显式指令时与私聊对齐：分模型从自然问句提取时效搜索词
                    # 冷却前置+问句预检：冷却期内或明显非事实问句时不调分模型
                    if (
                        triggered
                        and not search_query
                        and settings.get("search_enabled", True)
                        and not search_cooling(f"group_{group_id}", settings)
                        and looks_like_factual_query(msg_text)
                    ):
                        search_query = await extract_search_query_private(
                            f"group_{group_id}_{user_id}", msg_text, label="群@"
                        )

                    log(f"群 {group_id} 收到 {nickname} 的消息: {msg_text}")
                    if is_from_bot:
                        allowed, prob, current_turns = check_bot_interaction(group_id, user_id, settings)
                        if not allowed:
                            log(f"[防刷] 群 {group_id} 对方为已知机器人 {user_id} (连续第{current_turns + 1}轮)，衰减概率 {prob:.1f}%，未通过软保底测试，掐断回复")
                            continue
                        log(f"[防刷] 群 {group_id} 对方为已知机器人 {user_id} (连续第{current_turns + 1}轮)，衰减概率 {prob:.1f}%，通过软保底测试，允许回复")
                        record_bot_reply(group_id, user_id, settings)

                    _last_reply_time[rate_key] = now
                    search_ctx = await run_search(f"group_{group_id}", search_query, settings)
                    is_duel = part_sticker.is_incoming_pure_sticker(event) and settings.get("sticker_duel_mode", False)
                    create_safe_task(reply_group(
                        websocket, group_id, f"group_{group_id}_{user_id}", record_text,
                        image=vision_image, reply_to=mid, search_context=search_ctx, is_duel=is_duel
                    ), "群聊回复")
    except Exception as e:
        log(f"[错误] 连接断开或发生异常: {e}")
        await send_wechat_alert("死老冯腾讯可能又风控了",f"处理消息时发生异常：{e}")
    finally:
        _active_ws = None
        recv_task.cancel()
# =====================


# ====== 主动私聊搭讪系统（双轨：记忆发散睹物思人 + 时态日常碎碎念） ======
_last_proactive_time = 0.0

def is_in_quiet_hours(settings):
    try:
        start = int(settings.get("proactive_quiet_start", 23))
        end = int(settings.get("proactive_quiet_end", 8))
        now_hour = datetime.datetime.now().hour
        if start == end:
            return False
        if start > end:  # 跨午夜，如 23:00 到 08:00
            return now_hour >= start or now_hour < end
        else:            # 当天内，如 12:00 到 14:00
            return start <= now_hour < end
    except Exception:
        return False

def parse_proactive_targets(raw):
    """解析主动搭讪目标：逗号分隔；纯数字=私聊QQ，group_群号=群聊。"""
    targets = []
    seen = set()
    text = str(raw or "").replace("，", ",").replace("、", ",")
    for token in text.split(","):
        t = token.strip()
        if not t:
            continue
        if t.lower().startswith("group_"):
            gid = t[6:].strip()
            if gid.isdigit() and ("group", gid) not in seen:
                seen.add(("group", gid))
                targets.append(("group", gid))
        elif t.isdigit():
            if ("private", t) not in seen:
                seen.add(("private", t))
                targets.append(("private", t))
        else:
            log(f"[主动] 无法识别的目标格式已忽略: {t}（私聊填QQ，群聊填 group_群号）")
    return targets


async def _proactive_generate_text(settings, kind, memory, memory_scope):
    """生成一条主动消息文本，返回 (文本, 模式标签)。kind = private / group。"""
    time_desc = get_current_time_desc()
    state_mood = (memory.get("state") or {}).get("mood", "")
    min_imp = safe_float(settings.get("musing_min_importance"), 0.4)
    memories = await asyncio.to_thread(
        part_record.get_high_importance_memories, 1, min_imp, memory_scope
    )
    use_type_a = bool(memories) and random.random() < 0.5
    past_memory = memories[0][2] if use_type_a else ""

    memory = dict(memory)
    if kind == "group":
        memory["character_prompt"] = ""
    active_prompt = build_user_prompt(memory_scope, memory)

    if kind == "group":
        if use_type_a:
            mode_label = "类型A·群往事发散"
            instruction = (
                f"\n\n# 主动群聊冒泡指令（群往事发散型）\n"
                f"你现在闲着，当前现实时间是 {time_desc}，你突然想起这个群里以前聊过的这件往事：\n"
                f"【往事】：{past_memory}\n\n"
                "你决定在群里冒个泡，顺着这件往事自然开启一句（就像群友突然想起旧话题）。\n"
                "要求：\n"
                "1. 完全符合你的角色性格口吻，像真实的群聊发言。\n"
                "2. 严格控制在25字以内。\n"
                "3. 绝对不要复述往事原文，严禁客服腔，不要机械问好。\n"
                "4. 只输出要发到群里的那一句话，不要任何解释、标号或内心独白！"
            )
        else:
            mode_label = "类型B·群时态冒泡"
            instruction = (
                f"\n\n# 主动群聊冒泡指令（日常时态与情绪碎碎念）\n"
                f"你现在闲着，当前现实时间是 {time_desc}，群里最近有点冷清，你决定冒个泡。\n"
                + (f"你当前的心情/状态是：{state_mood}。\n" if state_mood else "")
                + "要求：\n"
                "1. 完全符合你的角色性格口吻，像真实群友随手发的一条群消息。\n"
                "2. 结合当下时间段（早晨、午后摸鱼、晚饭后、深夜等）或无聊发呆的状态，发一句简短的群聊消息（严格25字以内）。\n"
                "   例如：“这个点群里怎么这么安静”、“（冒泡）下午三点，都起来摸摸鱼啊”、“大半夜的还有夜猫子不睡？”\n"
                "3. 严禁客服腔，不要像AI助手，不要机械问好。\n"
                "4. 只输出要发到群里的那一句话，不要任何解释、标号或内心独白！"
            )
    else:
        if use_type_a:
            mode_label = "类型A·记忆发散"
            instruction = (
                f"\n\n# 主动发消息指令（记忆发散型）\n"
                f"你现在处于空闲状态，当前时间是 {time_desc}，突然回忆起了关于对方的这件往事：\n"
                f"【往事】：{past_memory}\n\n"
                "你决定主动给对方发一条私聊（就像朋友突然想起以前聊过的事找对方搭话）。\n"
                "要求：\n"
                "1. 必须完全符合你的角色性格口吻，语气自然随性，就像微信/QQ真实聊天。\n"
                "2. 顺着这件往事开启一句简短的新私聊（严格控制在25字以内）。\n"
                "   例如：“突然想起来，上次你提过的那家店叫什么来着？”、“今天听歌莫名想起来你之前推荐的那首了”。\n"
                "3. 绝对不要复述往事原文，严禁客服腔，不要机械问好。\n"
                "4. 只输出要发给对方的那句话，不要输出任何多余的解释、标号或内心独白！"
            )
        else:
            mode_label = "类型B·时态情绪"
            instruction = (
                f"\n\n# 主动发消息指令（日常时态与情绪碎碎念）\n"
                f"你现在闲下来了，当前现实时间是：{time_desc}。\n"
                + (f"你当前的心情/状态是：{state_mood}。\n" if state_mood else "")
                + "你决定主动在QQ私聊给对方发一句简短的搭讪、碎碎念或戳一戳（就像关系不错的朋友在无聊或特定时段随手发一句）。\n"
                "要求：\n"
                "1. 必须完全符合你的角色性格口吻（傲娇/慵懒/天才奸商等，自然流露）。\n"
                "2. 结合当下的真实时间段（早晨、午后摸鱼、晚饭后、深夜等）或当下无聊发呆的状态，发一句简短的私聊（严格控制在25字以内）。\n"
                "   例如：“（戳了戳）好无聊啊，你今天很忙吗？”、“下午三点……有点犯困，你在干嘛呢”、“这个点居然还在工作？别猝死了”。\n"
                "3. 严禁客服腔，不要像AI助手，就像真实QQ好友随手敲出的一句话。\n"
                "4. 只输出要发给对方的那句话，不要输出任何多余的解释、标号或内心独白！"
            )

    route = part_api.get_route("main")
    messages = [{"role": "system", "content": active_prompt + instruction}]
    for msg in memory.get("chat_history", [])[-12:]:
        if isinstance(msg, dict) and msg.get("content"):
            messages.append({"role": msg.get("role", "user"), "content": msg.get("content")})
    messages.append({"role": "user", "content": "请输出你要发出去的这一句话："})

    payload = {
        "model": route.get("model") or MAIN_MODEL,
        "messages": messages,
        "stream": False
    }
    raw = await _post_json(payload, api_url=route.get("url"), api_key=route.get("key"))
    text = strip_trailing_period(clean_reply_format(raw or ""))
    if len(text) > 60:
        text = text[:50]
    return text, mode_label, state_mood


async def _send_proactive_private(settings, target_qq, force):
    memory = await asyncio.to_thread(
        part_record.load_user_memory, str(target_qq), settings.get("hot_context_private", 30)
    )
    text, mode_label, mood = await _proactive_generate_text(settings, "private", memory, str(target_qq))
    if not text:
        return "生成内容为空"
    clean_text, sticker_seg = await part_sticker.process_reply_stickers(
        text, f"private_{target_qq}", settings, mood=mood
    )
    if clean_text.strip():
        await part_command.send_private(_active_ws, target_qq, clean_text)
        log(f"[主动] 已主动向私聊 {target_qq} 发送 ({mode_label}): {clean_text}")
    if sticker_seg:
        await send_sticker_message(_active_ws, sticker_seg, f"私聊 {target_qq}", user_id=target_qq)
    # 写入热记忆，保证对方回复时对话连贯
    fresh = await asyncio.to_thread(part_record.load_user_memory, str(target_qq))
    ctx = fresh.get("chat_history", [])
    ctx.append({"role": "assistant", "content": clean_text or text})
    fresh["chat_history"] = ctx[-200:]
    await asyncio.to_thread(part_record.save_user_memory, str(target_qq), fresh)
    log(f"[主动] 已将主动消息写入私聊 {target_qq} 的热记忆上下文")
    return f"已发送({mode_label}): {clean_text}"


async def _send_proactive_group(settings, group_id, force):
    if not force:
        last_role, last_ts = await asyncio.to_thread(
            part_record.get_last_history_info, f"group_{group_id}"
        )
        idle_min = float(settings.get("proactive_group_idle_minutes", 30.0))
        if last_ts and (time.time() - last_ts) < idle_min * 60:
            return f"群内仍活跃(需静默{idle_min:g}分钟)"
        if last_role == "assistant":
            return "上一条是自己发言，跳过防自言自语"
    memory = await asyncio.to_thread(
        part_record.load_user_memory, f"group_{group_id}", settings.get("hot_context_group", 12)
    )
    text, mode_label, mood = await _proactive_generate_text(settings, "group", memory, f"group_{group_id}")
    if not text:
        return "生成内容为空"
    clean_text, sticker_seg = await part_sticker.process_reply_stickers(
        text, f"group_{group_id}", settings, mood=mood
    )
    if clean_text.strip():
        await part_command.send_group(_active_ws, group_id, clean_text)
        log(f"[主动] 已主动向群 {group_id} 冒泡 ({mode_label}): {clean_text}")
    if sticker_seg:
        await send_sticker_message(_active_ws, sticker_seg, f"群 {group_id}", group_id=group_id)
    # 写入群共享历史，群友接话时可顺畅延续
    await asyncio.to_thread(
        part_record.append_group_history, group_id, "assistant", clean_text or text
    )
    log(f"[主动] 已将主动冒泡写入群 {group_id} 的共享历史")
    return f"已发送({mode_label}): {clean_text}"


async def generate_and_send_proactive_message(force=False, specified_user_id=None, specified_group_id=None):
    """
    主动搭讪系统（双轨：记忆发散 + 时态自发），支持私聊与群聊多目标。
    force=True 用于管理员指令/调试即时测试，绕过勿扰、频率与群活跃检查。
    """
    global _last_proactive_time
    if not _active_ws:
        return "WebSocket 客户端未连接，无法发送"

    settings = await asyncio.to_thread(part_record.load_settings)
    if not force and not settings.get("proactive_enabled", False):
        return "未开启"

    now = time.time()
    interval = float(settings.get("proactive_interval_minutes", 180.0)) * 60.0

    if not force:
        if (now - _last_proactive_time) < interval:
            return "冷却中"
        if is_in_quiet_hours(settings):
            return "勿扰时段"

    if specified_user_id:
        targets = [("private", str(specified_user_id))]
    elif specified_group_id:
        targets = [("group", str(specified_group_id))]
    else:
        targets = parse_proactive_targets(settings.get("proactive_target", ""))
        if not targets and ADMIN_QQ:
            targets = [("private", str(ADMIN_QQ[0]))]
    if not targets:
        return "未指定目标（私聊填QQ，群聊填 group_群号，逗号分隔）"

    if not force:
        prob = float(settings.get("proactive_probability", 50.0))
        roll = random.random() * 100.0
        if roll >= prob:
            _last_proactive_time = now
            log(f"[主动] 本轮掷出 {roll:.1f}% >= {prob:g}%，跳过主动搭讪")
            return "掷骰未命中"

    _last_proactive_time = now
    log(f"[主动] 触发主动搭讪流程，目标: {', '.join(f'{k}:{t}' for k, t in targets)}")

    results = []
    for kind, target in targets:
        try:
            if kind == "private":
                r = await _send_proactive_private(settings, target, force)
            else:
                r = await _send_proactive_group(settings, target, force)
            results.append(f"{target}->{r}")
        except Exception as e:
            log(f"[主动] 目标 {kind}:{target} 处理异常: {e}")
            results.append(f"{target}->异常:{e}")
    return "; ".join(results)



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

            # 周期性主动私聊搭讪检查
            try:
                await generate_and_send_proactive_message(force=False)
            except Exception as pe:
                log(f"[主动] 调度检查异常: {pe}")
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
                    musing = clean_reply_format((await asyncio.wait_for(
                        _post_json(payload, api_url=route.get("url"), api_key=route.get("key")), timeout=MUSING_TIMEOUT
                    )) or "")
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
                    create_safe_task(musing_task(), "胡思乱想")
                await asyncio.Future()
        except asyncio.CancelledError:
            print("[系统] 程序被手动终止。")
            break
        except Exception as e:
            print(f"[错误] 服务异常退出: {e}，5秒后自动重启...")
            await asyncio.sleep(5)


if __name__ == "__main__":
    # 写 PID 文件供 WebUI 一键关机/重启（隐藏窗口下进程不好手动找）
    _PID_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "bot.pid")
    try:
        os.makedirs(os.path.dirname(_PID_FILE), exist_ok=True)
        with open(_PID_FILE, "w", encoding="utf-8") as _f:
            _f.write(str(os.getpid()))
    except Exception:
        pass
    asyncio.run(main())
