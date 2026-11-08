import json
import re
import part_record
import part_sticker


# ====== 指令列表 ======
# WebUI 指令速查展示用（完整版）
CMD_HELP_TEXT = (
    "[指令列表]\n"
    "/表情 - 查看公共表情库收录统计\n"
    "/rl - 重新生成上一条回复\n"
    "/char set <角色设定> - 设置专属人格 [仅私聊]\n"
    "/char clear - 清除专属人格 [仅私聊]\n"
    "/charfit set <0.0-1.0> - 设置人格拟合度\n"
    "/user set <用户人设> - 设置用户信息\n"
    "/user clear - 清除用户信息\n"
    "/mdchange - 切换回复模式 [群聊仅管理]\n"
    "/atswitch - 切换触发方式（群内切本群，私聊切全局默认）[仅管理]\n"
    "/cd set <秒数> - 设置自动回复冷却 [仅管理]\n"
    "/atcd set <秒数> - 设置被@回复冷却 [仅管理]）\n"
    "/faver set <QQ号> <数值> - 设置好感度 [仅管理]\n"
    "/name set <名字1> <名字2>... - 设置提名列表 [仅管理]\n"
    "/infer set <AI/PY> - 设置判回方式：AI/概率 [仅管理]\n"
    "/pro set <概率> - 设置掷骰概率 [仅管理]\n"
    "/smode set <AI/PY> - 设置表情触发方式 [仅管理]\n"
    "/spro set <概率> - 设置表情附带概率 [仅管理]\n"
    "/scd set <秒数> - 设置表情冷却时间 [仅管理]\n"
    "/斗图 - 开关斗图恶搞模式 [仅管理]\n"
    "/bot add <QQ> - 标注群内其他机器人QQ [仅管理]\n"
    "/bot del <QQ> - 移除已标注机器人 [仅管理]\n"
    "/bot list - 查看已标注机器人及衰减步长 [仅管理]\n"
    "/bot step <百分比> - 设置连续互动概率衰减步长(0-100) [仅管理]\n"
    "/poke - 立即触发一次主动搭讪/冒泡测试 [仅管理]\n"
    "/memory reset - 清空聊天记忆 [群聊中仅管理]\n"
    "/cmd help - 查看本指令列表"
)


def build_help(admin=False, in_group=False):
    """按权限和场景生成指令列表：普通用户只看到自己能用的。"""
    lines = [
        "[指令列表]",
        "/rl - 重新生成上一条回复",
        "/user set <用户信息> - 记录你的信息",
        "/user clear - 清除你的信息",
        "/charfit set <0.0-1.0> - 设置人设拟合度",
    ]
    if in_group:
        lines.insert(2, "/char - 专属人格请到私聊设置")
    else:
        lines += [
            "/char set <角色设定> - 设置专属人格",
            "/char clear - 清除专属人格",
            "/mdchange - 切换回复模式",
            "/memory reset - 清空聊天记忆",
        ]
    if admin:
        lines.append("—— 管理员 ——")
        if in_group:
            lines += [
                "/mdchange - 切换回复模式",
                "/memory reset - 清空聊天记忆",
            ]
        lines += [
            "/atswitch - 切换触发方式",
            "/cd set <秒数> - 设置自动回复冷却",
            "/atcd set <秒数> - 设置被@回复冷却",
            "/faver set <QQ号> <数值> - 设置好感度",
            "/name set <名字1> <名字2>... - 设置提名列表",
            "/infer set <AI/PY> - 设置判回方式：AI/概率",
            "/pro set <概率> - 设置掷骰概率",
            "/smode set <AI/PY> - 设置表情触发方式",
            "/spro set <概率> - 设置表情附带概率",
            "/scd set <秒数> - 设置表情冷却时间",
            "/斗图 - 开关斗图恶搞模式",
            "/bot add <QQ> - 标注群内其他机器人QQ",
            "/bot del <QQ> - 移除已标注机器人",
            "/bot list - 查看已标注机器人及衰减步长",
            "/bot step <百分比> - 设置连续互动概率衰减步长(0-100)",
            "/poke - 立即触发一次主动搭讪/冒泡测试",
        ]
    lines.append("/cmd help - 查看本指令列表")
    return "\n".join(lines)
# =====================


# ====== 消息发送 ======
async def send_private(websocket, user_id, message):
    action = {
        "action": "send_private_msg",
        "params": {"user_id": user_id, "message": message},
        "echo": "send_msg"
    }
    await websocket.send(json.dumps(action))


async def send_group(websocket, group_id, message, reply_to=None):
    if reply_to:
        if isinstance(message, list):
            message = [{"type": "reply", "data": {"id": str(reply_to)}}] + message
        else:
            message = [
                {"type": "reply", "data": {"id": str(reply_to)}},
                {"type": "text", "data": {"text": str(message)}},
            ]
    action = {
        "action": "send_group_msg",
        "params": {"group_id": group_id, "message": message},
        "echo": "send_group_msg"
    }
    await websocket.send(json.dumps(action))
# =====================


# ====== 权限判断 ======
# 只认管理员QQ列表（ADMIN_QQ），群主/群管理员不算
def is_admin(event, admin_qq):
    return event.get("user_id") in admin_qq
# =====================


# ===== 冷却设置 ======
# /cd set 和 /atcd set 共用的解析逻辑，key 为设置项名，label 为回复里的中文名
def _parse_cdset(text, key, label):
    parts = text.split()
    if len(parts) == 3:
        try:
            value = float(parts[2])
            if 0 <= value <= 3600:
                part_record.set_reply_cooldown(value, key)
                return f"[指令] {label}已设置为 {value:g} 秒"
            return "[指令] 参数范围：0 - 3600 秒"
        except ValueError:
            pass
    return f"[指令] 请使用格式：{text.split()[0]} set <秒数>"


# ===== 好感度设置 ====
# /faver set 共用的解析逻辑，target 为档案名
def _parse_faverset(text, target, label):
    parts = text.split()
    if len(parts) == 4:
        try:
            value = float(parts[3])
            result = part_record.set_favorability(target, value)
            return f"[指令] 已将 {label} 的好感度设置为 {result}"
        except ValueError:
            pass
    return "[指令] 请使用格式：/faver set <QQ号> <数值>"


# ===== 提及触发 ====
def _parse_nameset(text):
    parts = text.split(maxsplit=2)
    if len(parts) == 3 and parts[2].strip():
        names = [n for n in re.split(r"[，,、\s]+", parts[2].strip()) if n]
        if names:
            part_record.set_name_keywords(names)
            return f"[指令] 提名列表已设置为：{'、'.join(names)}"
    return "[指令] 请使用格式：/name set <名字1> <名字2> ..."


# ===== 潜水判断方式 ====
def _parse_inferset(text):
    parts = text.split()
    if len(parts) == 3 and parts[2].upper() in ("AI", "PY"):
        mode = parts[2].upper()
        part_record.set_infer_mode(mode)
        label = "AI判断" if mode == "AI" else "概率掷骰"
        return f"[指令] 潜水判断方式已切换为：{label}"
    return "[指令] 请使用格式：/infer set <AI/PY>"


# ===== 潜水插嘴概率 =====
def _parse_proset(text):
    parts = text.split()
    if len(parts) == 3:
        try:
            value = float(parts[2])
            if 0 <= value <= 100:
                part_record.set_dive_probability(value)
                return f"[指令] 掷骰概率已设置为 {value:g}%"
            return "[指令] 参数范围：0 - 100"
        except ValueError:
            pass
    return "[指令] 请使用格式：/pro set <概率>"


# ===== 表情包指令解析 =====
def _parse_smodeset(text):
    parts = text.split()
    if len(parts) == 3 and parts[2].upper() in ("AI", "PY"):
        mode = parts[2].upper()
        part_record.set_sticker_setting("sticker_mode", mode)
        label = "AI自主标签" if mode == "AI" else "概率附带"
        return f"[指令] 表情触发模式已切换为：{label}"
    return "[指令] 请使用格式：/smode set <AI/PY>"


def _parse_sproset(text):
    parts = text.split()
    if len(parts) == 3:
        try:
            val = float(parts[2])
            if 0 <= val <= 100:
                part_record.set_sticker_setting("sticker_probability", val)
                return f"[指令] 表情附带概率已设置为 {val:g}%"
            return "[指令] 参数范围：0 - 100"
        except ValueError:
            pass
    return "[指令] 请使用格式：/spro set <百分比(0-100)>"


def _parse_scdset(text):
    parts = text.split()
    if len(parts) == 3:
        try:
            val = float(parts[2])
            if 0 <= val <= 3600:
                part_record.set_sticker_setting("sticker_cooldown", val)
                return f"[指令] 表情冷却时间已设置为 {val:g} 秒"
            return "[指令] 参数范围：0 - 3600 秒"
        except ValueError:
            pass
    return "[指令] 请使用格式：/scd set <秒数>"


def _toggle_duel():
    cur = part_record.load_settings()
    new_val = not cur.get("sticker_duel_mode", False)
    part_record.set_sticker_setting("sticker_duel_mode", new_val)
    return f"[指令] 斗图恶搞模式已{'开启（收到表情只回表情对线）' if new_val else '关闭'}"


def _format_sticker_stats(stats: dict) -> str:
    lines = [f"[表情库] 当前公共库共收录 {stats.get('total', 0)} 个表情：\n- 商城大表情：{stats.get('mface_count', 0)} 个\n- 收藏图片表情：{stats.get('image_count', 0)} 张"]
    cats = stats.get("categories", {})
    if cats:
        cat_items = [f"{k}: {v}" for k, v in cats.items() if v > 0]
        if cat_items:
            lines.append("【各分类图片】" + " | ".join(cat_items))
    return "\n".join(lines)


# ===== 机器人防刷管理指令 =====
def _handle_bot_cmd(text):
    parts = text.split()
    cur = part_record.load_settings()
    known_bots = [str(b).strip() for b in cur.get("known_bots", []) if str(b).strip()]
    decay_step = float(cur.get("bot_decay_step", 25.0))

    if len(parts) >= 2 and parts[1] == "list":
        if not known_bots:
            return f"[机器人防刷] 暂未标注任何机器人\n衰减步长: {decay_step:g}%\n提示: 可使用 /bot add <QQ> 标注"
        bots_str = "\n".join(f"- {b}" for b in known_bots)
        ladder = ["第1轮 100%"]
        cur_p = 100.0 - decay_step
        r = 2
        while cur_p > 0 and r <= 6:
            ladder.append(f"第{r}轮 {cur_p:g}%")
            cur_p -= decay_step
            r += 1
        if cur_p <= 0:
            ladder.append(f"第{r}轮起 0%(掐断)")
        return f"[机器人防刷列表]\n{bots_str}\n衰减步长: {decay_step:g}%\n衰减阶梯: {' -> '.join(ladder)}"

    if len(parts) == 3 and parts[1] == "add":
        qq = parts[2].strip()
        m = re.search(r"\d+", qq)
        if not m:
            return "[指令] 请输入有效的 QQ 号，格式：/bot add <QQ>"
        bot_qq = m.group(0)
        if bot_qq in known_bots:
            return f"[指令] 机器人 {bot_qq} 已在列表中"
        known_bots.append(bot_qq)
        cur["known_bots"] = known_bots
        part_record.save_settings(cur)
        return f"[指令] 已成功标注机器人 QQ：{bot_qq}"

    if len(parts) == 3 and parts[1] in ("del", "remove", "rm"):
        qq = parts[2].strip()
        m = re.search(r"\d+", qq)
        if not m:
            return "[指令] 请输入有效的 QQ 号，格式：/bot del <QQ>"
        bot_qq = m.group(0)
        if bot_qq not in known_bots:
            return f"[指令] 列表中未找到机器人 {bot_qq}"
        known_bots = [b for b in known_bots if b != bot_qq]
        cur["known_bots"] = known_bots
        part_record.save_settings(cur)
        return f"[指令] 已移除机器人 QQ：{bot_qq}"

    if len(parts) == 3 and parts[1] == "step":
        try:
            val = float(parts[2])
            if not 0 <= val <= 100:
                return "[指令] 衰减步长范围为 0 - 100（百分比）"
            cur["bot_decay_step"] = val
            part_record.save_settings(cur)
            return f"[指令] 机器人互动衰减步长已设置为 {val:g}%"
        except ValueError:
            return "[指令] 请输入有效的数字，格式：/bot step <百分比>"

    return "[指令] 机器人防刷指令格式：\n/bot list\n/bot add <QQ>\n/bot del <QQ>\n/bot step <百分比>"


# ===== 私聊指令 ======
# on_rl: /rl 触发的重新生成回调
# on_poke: /poke 触发的主动搭讪测试回调
async def handle_private_command(websocket, user_id, msg_text, admin, on_rl, on_poke=None):
    text = msg_text.strip()

    # /poke /戳我 /搭讪
    if text in ("/poke", "/戳我", "/搭讪"):
        if not admin:
            reply = "[指令] 仅管理员可用"
            await send_private(websocket, user_id, reply)
            return True
        if on_poke:
            on_poke()
        else:
            await send_private(websocket, user_id, "[指令] 未配置主动搭讪回调")
        return True

    # 表情库查看
    if text in ("/sticker", "/表情"):
        stats = await part_sticker.get_sticker_stats()
        reply = _format_sticker_stats(stats)
        await send_private(websocket, user_id, reply)
        return True

    # /memory reset
    if text == "/memory reset":
        part_record.clear_user_memory(str(user_id))
        await send_private(websocket, user_id, "[指令] 记忆已全部清空")
        return True

    # /char set
    if text.startswith("/char set"):
        new_char = text[9:].strip()
        if not new_char:
            reply = "[指令] 请使用格式：/char set <角色设定>"
        else:
            part_record.set_user_character(str(user_id), new_char)
            reply = "[指令] 专属人设已设置"
        await send_private(websocket, user_id, reply)
        return True

    # /char clear
    if text == "/char clear":
        part_record.clear_user_character(str(user_id))
        await send_private(websocket, user_id, "[指令] 专属人设已清除")
        return True

    # /user set
    if text.startswith("/user set"):
        new_persona = text[9:].strip()
        if not new_persona:
            reply = "[指令] 请使用格式：/user set <你的信息，例如：我叫小明，男，25岁，程序员>"
        else:
            part_record.set_user_persona(str(user_id), new_persona)
            reply = "[指令] 你的个人信息已记录"
        await send_private(websocket, user_id, reply)
        return True

    # /user clear
    if text == "/user clear":
        part_record.clear_user_persona(str(user_id))
        await send_private(websocket, user_id, "[指令] 你的个人信息已清除")
        return True

    # /charfit set
    if text.startswith("/charfit"):
        parts = text.split()
        if len(parts) == 3 and parts[1] == "set":
            try:
                value = float(parts[2])
                if 0.0 <= value <= 1.0:
                    part_record.set_user_fitness(str(user_id), value)
                    reply = f"[指令] 人设拟合度已设置为 {value}"
                else:
                    reply = "[指令] 参数范围：0.0 - 1.0"
            except ValueError:
                reply = "[指令] 请使用格式：/charfit set <0.0-1.0>"
        else:
            reply = "[指令] 请使用格式：/charfit set <0.0-1.0>"
        await send_private(websocket, user_id, reply)
        return True

    # /mdchange
    if text == "/mdchange":
        new_mode = part_record.toggle_user_mode(str(user_id))
        await send_private(websocket, user_id, f"[指令] 回复模式已切换为：{new_mode}模式")
        return True

    # /rl
    if text == "/rl":
        last_msg = part_record.pop_last_turn(str(user_id))
        if not last_msg:
            await send_private(websocket, user_id, "[指令] 没有可以重新生成的对话")
        else:
            on_rl(last_msg)
        return True

    # /atswitch（仅管理员）—— 私聊里切换全局默认触发方式
    if text == "/atswitch":
        if not admin:
            reply = "[指令] 仅管理员可用"
        else:
            at_only = part_record.toggle_at_only()
            reply = f"[指令] 全局默认触发方式已切换为：{'仅提及' if at_only else '自主互动'}（群可单独覆盖）"
        await send_private(websocket, user_id, reply)
        return True

    # /cd set（仅管理员）
    if text.startswith("/cd set"):
        if not admin:
            reply = "[指令] 仅管理员可用"
        else:
            reply = _parse_cdset(text, "reply_cooldown", "自动回复冷却")
        await send_private(websocket, user_id, reply)
        return True

    # /atcd set（仅管理员）
    if text.startswith("/atcd set"):
        if not admin:
            reply = "[指令] 仅管理员可用"
        else:
            reply = _parse_cdset(text, "reply_cooldown_at", "被@回复冷却")
        await send_private(websocket, user_id, reply)
        return True

    # /faver set（仅管理员）
    if text.startswith("/faver set"):
        if not admin:
            reply = "[指令] 仅管理员可用"
        else:
            parts = text.split()
            target = parts[2] if len(parts) >= 3 else ""
            reply = _parse_faverset(text, target, target)
        await send_private(websocket, user_id, reply)
        return True

    # /name set（仅管理员）
    if text.startswith("/name set"):
        reply = "[指令] 仅管理员可用" if not admin else _parse_nameset(text)
        await send_private(websocket, user_id, reply)
        return True

    # /infer set（仅管理员）
    if text.startswith("/infer set"):
        reply = "[指令] 仅管理员可用" if not admin else _parse_inferset(text)
        await send_private(websocket, user_id, reply)
        return True

    # /pro set（仅管理员）
    if text.startswith("/pro set"):
        reply = "[指令] 仅管理员可用" if not admin else _parse_proset(text)
        await send_private(websocket, user_id, reply)
        return True

    # 表情包管理指令（仅管理员）
    if text.startswith("/smode set"):
        reply = "[指令] 仅管理员可用" if not admin else _parse_smodeset(text)
        await send_private(websocket, user_id, reply)
        return True

    if text.startswith("/spro set"):
        reply = "[指令] 仅管理员可用" if not admin else _parse_sproset(text)
        await send_private(websocket, user_id, reply)
        return True

    if text.startswith("/scd set"):
        reply = "[指令] 仅管理员可用" if not admin else _parse_scdset(text)
        await send_private(websocket, user_id, reply)
        return True

    if text in ("/斗图", "/duel"):
        reply = "[指令] 仅管理员可用" if not admin else _toggle_duel()
        await send_private(websocket, user_id, reply)
        return True

    # /bot（仅管理员）
    if text.startswith("/bot"):
        reply = "[指令] 仅管理员可用" if not admin else _handle_bot_cmd(text)
        await send_private(websocket, user_id, reply)
        return True

    # /cmd help
    if text == "/cmd help":
        await send_private(websocket, user_id, build_help(admin=admin, in_group=False))
        return True

    return False


# ===== 群聊指令 ======
async def handle_group_command(websocket, group_id, user_id, msg_text, admin, on_rl, on_poke=None):
    text = msg_text.strip()
    mem_key = f"group_{group_id}_{user_id}"

    # /poke /戳我 /冒泡（仅管理员）
    if text in ("/poke", "/戳我", "/冒泡", "/搭讪"):
        if not admin:
            await send_group(websocket, group_id, "[指令] 仅管理员可用")
            return True
        if on_poke:
            on_poke()
        else:
            await send_group(websocket, group_id, "[指令] 未配置主动冒泡回调")
        return True

    # 表情库查看
    if text in ("/sticker", "/表情"):
        stats = await part_sticker.get_sticker_stats()
        reply = _format_sticker_stats(stats)
        await send_group(websocket, group_id, reply)
        return True

    # /rl
    if text == "/rl":
        last_msg = part_record.pop_last_turn(f"group_{group_id}")
        if not last_msg:
            await send_group(websocket, group_id, "[指令] 没有可以重新生成的对话")
        else:
            part_record.append_group_history(group_id, "user", last_msg)
            on_rl(last_msg)
        return True

    # /memory reset（仅管理员）
    if text == "/memory reset":
        if not admin:
            reply = "[指令] 仅管理员可用"
        else:
            part_record.clear_user_memory(mem_key)
            part_record.clear_user_memory(f"group_{group_id}")
            reply = "[指令] 记忆已全部清空"
        await send_group(websocket, group_id, reply)
        return True

    # /char set（群聊已统一使用默认人格，专属人设仅私聊生效）
    if text.startswith("/char set") or text == "/char clear":
        await send_group(websocket, group_id, "[指令] 群聊已统一使用默认人格，专属人设请在私聊里设置")
        return True

    # /user set
    if text.startswith("/user set"):
        new_persona = text[9:].strip()
        if not new_persona:
            reply = "[指令] 请使用格式：/user set <你的信息>"
        else:
            part_record.set_user_persona(mem_key, new_persona)
            reply = "[指令] 你在本群的个人信息已记录！"
        await send_group(websocket, group_id, reply)
        return True

    # /user clear
    if text == "/user clear":
        part_record.clear_user_persona(mem_key)
        await send_group(websocket, group_id, "[指令] 你在本群的个人信息已清除")
        return True

    # /charfit set
    if text.startswith("/charfit"):
        parts = text.split()
        if len(parts) == 3 and parts[1] == "set":
            try:
                value = float(parts[2])
                if 0.0 <= value <= 1.0:
                    part_record.set_user_fitness(mem_key, value)
                    reply = f"[指令] 人设拟合度已设置为 {value}"
                else:
                    reply = "[指令] 参数范围：0.0 - 1.0"
            except ValueError:
                reply = "[指令] 请使用格式：/charfit set <0.0-1.0>"
        else:
            reply = "[指令] 请使用格式：/charfit set <0.0-1.0>"
        await send_group(websocket, group_id, reply)
        return True

    # /mdchange（群内仅管理员；私聊不受限）
    if text == "/mdchange":
        if not admin:
            reply = "[指令] 群内切换回复模式仅管理员可用"
        else:
            new_mode = part_record.toggle_user_mode(f"group_{group_id}")
            reply = f"[指令] 本群回复模式已切换为：{new_mode}模式"
        await send_group(websocket, group_id, reply)
        return True

    # /atswitch（仅管理员）—— 群内切换本群的触发方式覆盖
    if text == "/atswitch":
        if not admin:
            reply = "[指令] 仅管理员可用"
        else:
            scope = f"group_{group_id}"
            settings = part_record.load_settings()
            cur = part_record.effective_at_only(settings, scope)
            new_val = not cur
            part_record.set_archive_at_only(scope, new_val)
            reply = f"[指令] 本群触发方式已切换为：{'仅提及' if new_val else '自主互动'}"
        await send_group(websocket, group_id, reply)
        return True

    # /cd set（仅管理员）
    if text.startswith("/cd set"):
        if not admin:
            reply = "[指令] 仅管理员可用"
        else:
            reply = _parse_cdset(text, "reply_cooldown", "自动回复冷却")
        await send_group(websocket, group_id, reply)
        return True

    # /atcd set（仅管理员）
    if text.startswith("/atcd set"):
        if not admin:
            reply = "[指令] 仅管理员可用"
        else:
            reply = _parse_cdset(text, "reply_cooldown_at", "被@回复冷却")
        await send_group(websocket, group_id, reply)
        return True

    # /faver set（仅管理员）
    if text.startswith("/faver set"):
        if not admin:
            reply = "[指令] 仅管理员可用"
        else:
            parts = text.split()
            target = f"group_{group_id}_{parts[2]}" if len(parts) >= 3 else ""
            reply = _parse_faverset(text, target, parts[2] if len(parts) >= 3 else "")
        await send_group(websocket, group_id, reply)
        return True

    # /name set（仅管理员）
    if text.startswith("/name set"):
        reply = "[指令] 仅管理员可用" if not admin else _parse_nameset(text)
        await send_group(websocket, group_id, reply)
        return True

    # /infer set（仅管理员）
    if text.startswith("/infer set"):
        reply = "[指令] 仅管理员可用" if not admin else _parse_inferset(text)
        await send_group(websocket, group_id, reply)
        return True

    # /pro set（仅管理员）
    if text.startswith("/pro set"):
        reply = "[指令] 仅管理员可用" if not admin else _parse_proset(text)
        await send_group(websocket, group_id, reply)
        return True

    # 表情包管理指令（仅管理员）
    if text.startswith("/smode set"):
        reply = "[指令] 仅管理员可用" if not admin else _parse_smodeset(text)
        await send_group(websocket, group_id, reply)
        return True

    if text.startswith("/spro set"):
        reply = "[指令] 仅管理员可用" if not admin else _parse_sproset(text)
        await send_group(websocket, group_id, reply)
        return True

    if text.startswith("/scd set"):
        reply = "[指令] 仅管理员可用" if not admin else _parse_scdset(text)
        await send_group(websocket, group_id, reply)
        return True

    if text in ("/斗图", "/duel"):
        reply = "[指令] 仅管理员可用" if not admin else _toggle_duel()
        await send_group(websocket, group_id, reply)
        return True

    # /bot（仅管理员）
    if text.startswith("/bot"):
        reply = "[指令] 仅管理员可用" if not admin else _handle_bot_cmd(text)
        await send_group(websocket, group_id, reply)
        return True

    # /cmd help
    if text == "/cmd help":
        await send_group(websocket, group_id, build_help(admin=admin, in_group=True))
        return True

    return False
