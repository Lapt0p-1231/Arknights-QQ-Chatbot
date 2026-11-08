"""QQ-BOT WebUI 后端（FastAPI）。

启动：python part_web.py
默认地址：http://127.0.0.1:8090
"""
import asyncio
import base64
import json
import os
import socket
import struct
import subprocess
import sys
import time
import zlib

import aiohttp
import uvicorn
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel

import part_command
import part_prompt
import part_record
import part_api

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(BASE_DIR, "QQ-BOT.log")
INDEX_FILE = os.path.join(BASE_DIR, "webui", "index.html")

app = FastAPI(title="QQ-BOT WebUI", docs_url=None, redoc_url=None)


# ====== 请求模型 ======
class SettingsUpdate(BaseModel):
    at_only: bool | None = None
    reply_cooldown: float | None = None
    reply_cooldown_at: float | None = None
    infer_mode: str | None = None
    dive_probability: float | None = None
    name_keywords: list[str] | None = None
    default_mode: str | None = None
    hot_context_private: int | None = None
    hot_context_group: int | None = None
    recall_threshold: float | None = None
    vision_enabled: bool | None = None
    vision_admin_only: bool | None = None
    vision_probability: float | None = None
    vision_meme_probability: float | None = None
    search_enabled: bool | None = None
    search_cooldown: float | None = None
    tavily_api_key: str | None = None
    search_explicit_cmd_enabled: bool | None = None
    quote_threshold: int | None = None
    reply_delay_min: float | None = None
    reply_delay_max: float | None = None
    sticker_enabled: bool | None = None
    sticker_mode: str | None = None
    sticker_probability: float | None = None
    sticker_cooldown: float | None = None
    sticker_duel_mode: bool | None = None
    sticker_capture_only: bool | None = None
    sticker_capture_enabled: bool | None = None
    sticker_capture_admin_only: bool | None = None
    sticker_capture_probability: float | None = None
    known_bots: list[str | int] | None = None
    bot_decay_step: float | None = None


class MusingConfig(BaseModel):
    interval_minutes: float
    private_enabled: bool
    group_enabled: bool
    min_importance: float = 0.7


class MusingTargetUpdate(BaseModel):
    scope: str
    enabled: bool | None = None
    interval_minutes: float | None = None


class MusingTargetReset(BaseModel):
    scope: str


class MusingBatch(BaseModel):
    scopes: list[str]
    enabled: bool | None = None
    interval_minutes: float | None = None
    reset: bool = False


class BatchDelete(BaseModel):
    user_ids: list[str]


class HotEdit(BaseModel):
    user_id: str
    ts: float
    content: str


class HotDelete(BaseModel):
    user_id: str
    ts: float


class VectorEdit(BaseModel):
    id: str
    content: str | None = None
    importance: float | None = None


class VectorDelete(BaseModel):
    id: str


class FavorUpdate(BaseModel):
    user_id: str
    value: float


class TargetText(BaseModel):
    user_id: str
    text: str = ""


class MemoryClear(BaseModel):
    user_id: str
    group_id: str | None = None


class SearchRequest(BaseModel):
    query: str
    scopes: list[str]
    top_k: int = 5


class EmbedRequest(BaseModel):
    text: str


class PromptToggle(BaseModel):
    file: str
    enabled: bool


class PromptSave(BaseModel):
    file: str
    content: str


class PromptCreate(BaseModel):
    file: str
    content: str = ""
    enabled: bool = True
    character: bool = False
    format: bool = False
    chat: bool = False
    internal: bool = False


class PromptFile(BaseModel):
    file: str


class UserId(BaseModel):
    user_id: str


class PromptReorder(BaseModel):
    files: list[str]


class RouteUpdate(BaseModel):
    role: str
    profile: str


class ProfileSave(BaseModel):
    id: str | None = None
    name: str = ""
    url: str
    key: str = ""
    model: str


class ProfileDelete(BaseModel):
    id: str


class ProfileTest(BaseModel):
    id: str
    type: str = "all"


# ====== 工具 ======
def _safe_prompt_name(name: str) -> str:
    name = os.path.basename(name.strip())
    if not name.lower().endswith(".txt") or not name:
        raise HTTPException(400, "提示词文件名必须以 .txt 结尾")
    return name


def _prompt_items():
    items = []
    for item in part_prompt.get_prompt_files():
        path = os.path.join(part_prompt.PROMPT_DIR, item["file"])
        exists = os.path.isfile(path)
        items.append({
            **item,
            "exists": exists,
            "size": os.path.getsize(path) if exists else 0,
            "mtime": os.path.getmtime(path) if exists else 0,
        })
    return items


def _save_prompt_list(files):
    payload = []
    for item in files:
        entry = {"file": item["file"], "enabled": bool(item.get("enabled"))}
        for flag in ("character", "format", "chat", "internal"):
            if item.get(flag):
                entry[flag] = True
        payload.append(entry)
    part_prompt.save_config(payload)


# ====== 页面 ======
@app.get("/")
def index():
    if not os.path.isfile(INDEX_FILE):
        raise HTTPException(500, "webui/index.html 不存在")
    return FileResponse(INDEX_FILE, media_type="text/html; charset=utf-8")


# ====== 状态 / 设置 ======
@app.get("/api/status")
def status():
    settings = part_record.load_settings()
    log_mtime = os.path.getmtime(LOG_FILE) if os.path.isfile(LOG_FILE) else 0
    import time
    db_file = part_record.DB_FILE
    return {
        "ok": True,
        "settings": settings,
        "log_active": (time.time() - log_mtime) < 300,
        "log_mtime": log_mtime,
        "db_size": os.path.getsize(db_file) if os.path.isfile(db_file) else 0,
        "prompt_count": len(part_prompt.get_prompt_files()),
        "enabled_prompt_count": sum(
            1 for x in part_prompt.get_prompt_files() if x.get("enabled")
        ),
        "bot_qq": getattr(part_command, "BOT_QQ", None) or int(os.environ.get("QQ_BOT_QQ", "0")) or None,
    }


# ====== 机器人进程控制（关机/重启）======
BOT_PID_FILE = os.path.join(BASE_DIR, "data", "bot.pid")
BOT_SCRIPT = os.path.join(BASE_DIR, "QQ-BOT.py")


def _read_bot_pid():
    try:
        with open(BOT_PID_FILE, "r", encoding="utf-8") as f:
            pid = int(f.read().strip())
        return pid if pid > 0 else None
    except Exception:
        return None


def _pid_alive(pid):
    """用 tasklist 校验 PID 存活且确实是 python 进程，避免 PID 复用误判。"""
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=10,
            creationflags=0x08000000,
        ).stdout or ""
        if f'"{pid}"' not in out:
            return False
        low = out.lower()
        return "python" in low
    except Exception:
        return False


def _kill_bot():
    pid = _read_bot_pid()
    killed = False
    if pid and _pid_alive(pid):
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True, timeout=15,
            creationflags=0x08000000,
        )
        killed = True
    try:
        os.remove(BOT_PID_FILE)
    except OSError:
        pass
    return killed, pid


def _spawn_bot():
    # DETACHED_PROCESS：无控制台窗口，且 webui 退出/重启后机器人继续运行
    proc = subprocess.Popen(
        [sys.executable, os.path.basename(BOT_SCRIPT)],
        cwd=BASE_DIR,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=0x00000008 | 0x00000200,
        close_fds=True,
    )
    return proc


@app.get("/api/bot/status")
def bot_status():
    pid = _read_bot_pid()
    alive = bool(pid and _pid_alive(pid))
    return {"running": alive, "pid": pid if alive else None}


@app.post("/api/bot/shutdown")
def bot_shutdown():
    killed, pid = _kill_bot()
    return {"ok": True, "killed": killed, "pid": pid}


@app.post("/api/bot/restart")
def bot_restart():
    _kill_bot()
    time.sleep(0.8)
    _spawn_bot()
    start = time.time()
    # 等 PID 文件出现且 8080 端口真正在监听，避免误报启动成功
    while time.time() - start < 20:
        time.sleep(0.5)
        pid = _read_bot_pid()
        if pid and _pid_alive(pid):
            try:
                with socket.create_connection(("127.0.0.1", 8080), timeout=0.5):
                    return {"ok": True, "pid": pid}
            except OSError:
                continue
    raise HTTPException(500, "机器人启动超时，请查看日志")


@app.get("/api/settings")
def get_settings():
    return part_record.load_settings()


@app.post("/api/settings")
def update_settings(body: SettingsUpdate):
    cur = part_record.load_settings()
    data = body.model_dump(exclude_none=True)
    if "at_only" in data:
        cur["at_only"] = bool(data["at_only"])
    for key in ("reply_cooldown", "reply_cooldown_at"):
        if key in data:
            value = float(data[key])
            if not 0 <= value <= 3600:
                raise HTTPException(400, "冷却范围 0-3600 秒")
            cur[key] = value
    if "infer_mode" in data:
        mode = str(data["infer_mode"]).upper()
        if mode not in ("AI", "PY"):
            raise HTTPException(400, "判断方式只能是 AI 或 PY")
        cur["infer_mode"] = mode
    if "default_mode" in data:
        dm = str(data["default_mode"])
        if dm not in ("扮演", "聊天"):
            raise HTTPException(400, "回复模式只能是 扮演 或 聊天")
        cur["default_mode"] = dm
    for key in ("hot_context_private", "hot_context_group"):
        if key in data:
            value = int(data[key])
            if not 1 <= value <= 200:
                raise HTTPException(400, "热上下文条数范围 1-200")
            cur[key] = value
    if "recall_threshold" in data:
        value = float(data["recall_threshold"])
        if not 0 <= value <= 1:
            raise HTTPException(400, "召回阈值范围 0-1")
        cur["recall_threshold"] = value
    if "vision_enabled" in data:
        cur["vision_enabled"] = bool(data["vision_enabled"])
    if "vision_admin_only" in data:
        cur["vision_admin_only"] = bool(data["vision_admin_only"])
    if "vision_probability" in data:
        value = float(data["vision_probability"])
        if not 0 <= value <= 100:
            raise HTTPException(400, "识图概率范围 0-100")
        cur["vision_probability"] = value
    if "vision_meme_probability" in data:
        value = float(data["vision_meme_probability"])
        if not 0 <= value <= 100:
            raise HTTPException(400, "表情包识别概率范围 0-100")
        cur["vision_meme_probability"] = value
    if "search_enabled" in data:
        cur["search_enabled"] = bool(data["search_enabled"])
    if "search_cooldown" in data:
        value = float(data["search_cooldown"])
        if not 0 <= value <= 86400:
            raise HTTPException(400, "搜索冷却范围 0-86400 秒")
        cur["search_cooldown"] = value
    if "tavily_api_key" in data:
        cur["tavily_api_key"] = str(data["tavily_api_key"]).strip()
    if "search_explicit_cmd_enabled" in data:
        cur["search_explicit_cmd_enabled"] = bool(data["search_explicit_cmd_enabled"])
    if "quote_threshold" in data:
        value = int(data["quote_threshold"])
        if not 0 <= value <= 200:
            raise HTTPException(400, "引用阈值范围 0-200 条")
        cur["quote_threshold"] = value
    for key in ("reply_delay_min", "reply_delay_max"):
        if key in data:
            value = float(data[key])
            if not 0 <= value <= 60:
                raise HTTPException(400, "回复延迟范围 0-60 秒")
            cur[key] = value
    if "dive_probability" in data:
        value = float(data["dive_probability"])
        if not 0 <= value <= 100:
            raise HTTPException(400, "概率范围 0-100")
        cur["dive_probability"] = value
    if "name_keywords" in data:
        names = [n.strip() for n in data["name_keywords"] if n.strip()]
        cur["name_keywords"] = names
    if "sticker_enabled" in data:
        cur["sticker_enabled"] = bool(data["sticker_enabled"])
    if "sticker_mode" in data:
        smode = str(data["sticker_mode"]).upper()
        if smode not in ("AI", "PY"):
            raise HTTPException(400, "表情触发模式只能是 AI 或 PY")
        cur["sticker_mode"] = smode
    if "sticker_probability" in data:
        value = float(data["sticker_probability"])
        if not 0 <= value <= 100:
            raise HTTPException(400, "表情附带概率范围 0-100")
        cur["sticker_probability"] = value
    if "sticker_cooldown" in data:
        value = float(data["sticker_cooldown"])
        if not 0 <= value <= 3600:
            raise HTTPException(400, "表情冷却范围 0-3600 秒")
        cur["sticker_cooldown"] = value
    if "sticker_duel_mode" in data:
        cur["sticker_duel_mode"] = bool(data["sticker_duel_mode"])
    if "sticker_capture_only" in data:
        cur["sticker_capture_only"] = bool(data["sticker_capture_only"])
    if "sticker_capture_enabled" in data:
        cur["sticker_capture_enabled"] = bool(data["sticker_capture_enabled"])
    if "sticker_capture_admin_only" in data:
        cur["sticker_capture_admin_only"] = bool(data["sticker_capture_admin_only"])
    if "sticker_capture_probability" in data:
        value = float(data["sticker_capture_probability"])
        if not 0 <= value <= 100:
            raise HTTPException(400, "抓取概率范围 0-100")
        cur["sticker_capture_probability"] = value
    if "known_bots" in data:
        bots = [str(b).strip() for b in data["known_bots"] if str(b).strip()]
        cur["known_bots"] = list(dict.fromkeys(bots))
    if "bot_decay_step" in data:
        value = float(data["bot_decay_step"])
        if not 0 <= value <= 100:
            raise HTTPException(400, "衰减步长范围 0-100")
        cur["bot_decay_step"] = value
    part_record.save_settings(cur)
    return cur


@app.get("/api/stickers/stats")
async def sticker_stats():
    import part_sticker
    return await part_sticker.get_sticker_stats()


@app.get("/api/help")
def help_text():
    return {"text": getattr(part_command, "CMD_HELP_TEXT", "")}


# ====== 档案汇总 ======
@app.get("/api/archives")
def archives():
    part_record.sync_archive_index()
    items = part_record.list_archives()
    return {
        "path": part_record.ARCHIVE_FILE,
        "count": len(items),
        "archives": items,
    }


@app.post("/api/archives/sync")
def archives_sync():
    payload = part_record.sync_archive_index(force=True)
    return {
        "path": part_record.ARCHIVE_FILE,
        "updated_at": (payload or {}).get("updated_at"),
        "archives": part_record.list_archives(),
    }


@app.get("/api/archives/download")
def archives_download():
    part_record.sync_archive_index(force=True)
    if not os.path.isfile(part_record.ARCHIVE_FILE):
        raise HTTPException(404, "档案文件不存在")
    return FileResponse(part_record.ARCHIVE_FILE, media_type="application/json",
                        filename="archives.json")


@app.post("/api/memory/clear/batch")
def clear_batch(body: BatchDelete):
    cleared = []
    for uid in body.user_ids:
        uid = str(uid).strip()
        if not uid:
            continue
        part_record.clear_user_memory(uid)
        cleared.append(uid)
    part_record.sync_archive_index(force=True)
    return {"cleared": cleared}


@app.post("/api/memory/delete/batch")
def delete_batch(body: BatchDelete):
    deleted, failed = [], []
    for uid in body.user_ids:
        uid = str(uid).strip()
        if not uid:
            continue
        (deleted if part_record.delete_archive(uid) else failed).append(uid)
    return {"deleted": deleted, "failed": failed}


# ====== 发散调度 ======
def _musing_payload():
    settings = part_record.load_settings()
    overrides = settings.get("musing_overrides") or {}
    last_map = {x["scope"]: x["last_musing"]
                for x in part_record.list_musing_scopes()}
    scopes = []
    for item in part_record.list_archives():
        scope = item["user_id"]
        enabled, interval = part_record.musing_policy(settings, scope)
        scopes.append({
            "scope": scope,
            "type": item["type"],
            "display_name": item.get("display_name", ""),
            "last_musing": last_map.get(scope, 0),
            "effective_enabled": enabled,
            "effective_interval": interval,
            "override": overrides.get(scope),
        })
    return {
        "config": {
            "interval_minutes": float(settings.get("musing_interval_minutes", 120)),
            "private_enabled": bool(settings.get("musing_private_enabled", True)),
            "group_enabled": bool(settings.get("musing_group_enabled", True)),
            "min_importance": float(settings.get("musing_min_importance", 0.7)),
        },
        "overrides": overrides,
        "scopes": scopes,
    }


@app.get("/api/musing")
def get_musing():
    return _musing_payload()


@app.post("/api/musing/config")
def set_musing_config(body: MusingConfig):
    interval = float(body.interval_minutes)
    if not 0 <= interval <= 360:
        raise HTTPException(400, "发散间隔范围 0-360 分钟")
    min_imp = float(body.min_importance)
    if not 0 <= min_imp <= 1:
        raise HTTPException(400, "IMP 阈值范围 0-1")
    settings = part_record.load_settings()
    settings["musing_interval_minutes"] = interval
    settings["musing_private_enabled"] = bool(body.private_enabled)
    settings["musing_group_enabled"] = bool(body.group_enabled)
    settings["musing_min_importance"] = min_imp
    part_record.save_settings(settings)
    return _musing_payload()


@app.post("/api/musing/target")
def set_musing_target(body: MusingTargetUpdate):
    scope = body.scope.strip()
    if not scope:
        raise HTTPException(400, "档案名不能为空")
    if body.enabled is None and body.interval_minutes is None:
        raise HTTPException(400, "至少设置开关或频率")
    override = {}
    if body.enabled is not None:
        override["enabled"] = bool(body.enabled)
    if body.interval_minutes is not None:
        interval = float(body.interval_minutes)
        if not 0 <= interval <= 360:
            raise HTTPException(400, "发散间隔范围 0-360 分钟")
        override["interval_minutes"] = interval

    settings = part_record.load_settings()
    overrides = dict(settings.get("musing_overrides") or {})
    current = dict(overrides.get(scope) or {})
    current.update(override)
    overrides[scope] = current
    settings["musing_overrides"] = overrides
    part_record.save_settings(settings)
    return _musing_payload()


@app.post("/api/musing/target/reset")
def reset_musing_target(body: MusingTargetReset):
    settings = part_record.load_settings()
    overrides = dict(settings.get("musing_overrides") or {})
    overrides.pop(body.scope.strip(), None)
    settings["musing_overrides"] = overrides
    part_record.save_settings(settings)
    return _musing_payload()


@app.post("/api/musing/targets/batch")
def set_musing_batch(body: MusingBatch):
    scopes = [s.strip() for s in body.scopes if str(s).strip()]
    if not scopes:
        raise HTTPException(400, "没有选中档案")
    if not body.reset and body.enabled is None and body.interval_minutes is None:
        raise HTTPException(400, "至少设置开关或频率，或选择恢复默认")
    if body.interval_minutes is not None and not 0 <= float(body.interval_minutes) <= 360:
        raise HTTPException(400, "发散间隔范围 0-360 分钟")

    settings = part_record.load_settings()
    overrides = dict(settings.get("musing_overrides") or {})
    for scope in scopes:
        if body.reset:
            overrides.pop(scope, None)
            continue
        current = dict(overrides.get(scope) or {})
        if body.enabled is not None:
            current["enabled"] = bool(body.enabled)
        if body.interval_minutes is not None:
            current["interval_minutes"] = float(body.interval_minutes)
        overrides[scope] = current
    settings["musing_overrides"] = overrides
    part_record.save_settings(settings)
    return _musing_payload()


# ====== 目标操作 ======
@app.get("/api/profile")
def profile(user_id: str):
    memory = part_record.load_user_memory(user_id)
    return {
        "user_id": user_id,
        "mode": memory.get("mode", "扮演"),
        "favorability": memory.get("favorability"),
        "fitness": memory.get("fitness"),
        "state": memory.get("state", {}),
        "character_prompt": memory.get("character_prompt", ""),
        "user_persona": memory.get("user_persona", ""),
        "chat_history": memory.get("chat_history", []),
    }


@app.post("/api/favor")
def set_favor(body: FavorUpdate):
    return {"favorability": part_record.set_favorability(body.user_id, body.value)}


@app.post("/api/target/character")
def set_character(body: TargetText):
    if not body.text.strip():
        raise HTTPException(400, "人格内容不能为空")
    part_record.set_user_character(body.user_id, body.text.strip())
    return {"ok": True}


@app.post("/api/target/character/clear")
def clear_character(body: UserId):
    part_record.clear_user_character(body.user_id)
    return {"ok": True}


@app.post("/api/target/persona")
def set_persona(body: TargetText):
    part_record.set_user_persona(body.user_id, body.text.strip())
    return {"ok": True}


@app.post("/api/target/persona/clear")
def clear_persona(body: UserId):
    part_record.clear_user_persona(body.user_id)
    return {"ok": True}


@app.post("/api/memory/clear")
def clear_memory(body: MemoryClear):
    part_record.clear_user_memory(body.user_id)
    if body.group_id:
        part_record.clear_user_memory(f"group_{body.group_id}")
    return {"ok": True}


@app.get("/api/memory/list")
def memory_list(user_id: str, limit: int = 50):
    return {
        "hot": part_record.list_hot_history(user_id),
        "vectors": part_record.list_memories(user_id, limit=limit),
    }


@app.post("/api/memory/hot/edit")
def hot_edit(body: HotEdit):
    if not body.content.strip():
        raise HTTPException(400, "内容不能为空")
    if not part_record.update_hot_entry(body.user_id, body.ts, body.content.strip()):
        raise HTTPException(404, "目标消息不存在（可能刚被新消息刷过，重新 LOAD 再试）")
    return {"ok": True}


@app.post("/api/memory/hot/delete")
def hot_delete(body: HotDelete):
    if not part_record.delete_hot_entry(body.user_id, body.ts):
        raise HTTPException(404, "目标消息不存在（可能刚被新消息刷过，重新 LOAD 再试）")
    return {"ok": True}


@app.post("/api/memory/vector/edit")
def vector_edit(body: VectorEdit):
    if body.content is None and body.importance is None:
        raise HTTPException(400, "没有要修改的内容")
    if body.importance is not None and not 0 <= body.importance <= 1:
        raise HTTPException(400, "重要度范围 0-1")
    if not part_record.update_vector(body.id, body.content, body.importance):
        raise HTTPException(404, "目标记忆不存在")
    return {"ok": True}


@app.post("/api/memory/vector/delete")
def vector_delete(body: VectorDelete):
    if not part_record.delete_vector(body.id):
        raise HTTPException(404, "目标记忆不存在")
    return {"ok": True}


@app.post("/api/memory/search")
def memory_search(body: SearchRequest):
    if not body.scopes:
        raise HTTPException(400, "至少提供一个检索范围")
    return {
        "results": part_record.search_vectors(
            body.scopes, body.query, top_k=max(1, min(20, body.top_k))
        )
    }


@app.post("/api/embed")
def embed_text(body: EmbedRequest):
    text = body.text.strip()
    if not text:
        raise HTTPException(400, "文本不能为空")
    vector = part_record._embed([text])[0]
    norm = sum(x * x for x in vector) ** 0.5
    return {
        "dim": len(vector),
        "norm": round(norm, 4),
        "preview": [round(x, 4) for x in vector[:32]],
    }


# ====== 提示词管理 ======
@app.get("/api/prompts")
def prompts():
    return {"dir": part_prompt.PROMPT_DIR, "items": _prompt_items()}


@app.get("/api/prompts/content")
def prompt_content(file: str):
    name = _safe_prompt_name(file)
    path = os.path.join(part_prompt.PROMPT_DIR, name)
    if not os.path.isfile(path):
        raise HTTPException(404, "文件不存在")
    with open(path, "r", encoding="utf-8") as f:
        return {"file": name, "content": f.read()}


@app.post("/api/prompts/toggle")
def prompt_toggle(body: PromptToggle):
    files = part_prompt.get_prompt_files()
    for item in files:
        if item["file"] == body.file:
            item["enabled"] = body.enabled
            break
    else:
        raise HTTPException(404, "没有这个提示词文件")
    _save_prompt_list(files)
    return {"ok": True, "items": _prompt_items()}


@app.post("/api/prompts/save")
def prompt_save(body: PromptSave):
    name = _safe_prompt_name(body.file)
    path = os.path.join(part_prompt.PROMPT_DIR, name)
    if not os.path.isfile(path):
        raise HTTPException(404, "文件不存在，请用新建")
    with open(path, "w", encoding="utf-8") as f:
        f.write(body.content)
    return {"ok": True}


@app.post("/api/prompts/create")
def prompt_create(body: PromptCreate):
    name = _safe_prompt_name(body.file)
    path = os.path.join(part_prompt.PROMPT_DIR, name)
    if os.path.exists(path):
        raise HTTPException(400, "同名文件已存在")
    with open(path, "w", encoding="utf-8") as f:
        f.write(body.content)
    files = part_prompt.get_prompt_files()
    entry = {"file": name, "enabled": body.enabled}
    for flag in ("character", "format", "chat", "internal"):
        if getattr(body, flag):
            entry[flag] = True
    files.append(entry)
    _save_prompt_list(files)
    return {"ok": True, "items": _prompt_items()}


@app.post("/api/prompts/delete")
def prompt_delete(body: PromptFile):
    name = _safe_prompt_name(body.file)
    path = os.path.join(part_prompt.PROMPT_DIR, name)
    if os.path.isfile(path):
        os.remove(path)
    files = [x for x in part_prompt.get_prompt_files() if x["file"] != name]
    _save_prompt_list(files)
    return {"ok": True, "items": _prompt_items()}


@app.post("/api/prompts/character")
def prompt_character(body: PromptFile):
    name = _safe_prompt_name(body.file)
    files = part_prompt.get_prompt_files()
    for item in files:
        item["character"] = item["file"] == name
        if item["file"] == name:
            item["enabled"] = True
    _save_prompt_list(files)
    return {"ok": True, "items": _prompt_items()}


@app.post("/api/prompts/reorder")
def prompt_reorder(body: PromptReorder):
    files = part_prompt.get_prompt_files()
    by_name = {item["file"]: item for item in files}
    ordered = [by_name[name] for name in body.files if name in by_name]
    ordered += [item for item in files if item["file"] not in body.files]
    _save_prompt_list(ordered)
    return {"ok": True, "items": _prompt_items()}


# ====== 模型路由 ======
def _mask_key(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return "****"
    return key[:3] + "****" + key[-4:]


def _models_payload():
    return {
        "profiles": [
            {**p, "key_masked": _mask_key(p.get("key", "")), "key": ""}
            for p in part_api.list_profiles()
        ],
        "routes": part_api.get_routes(),
    }


@app.get("/api/models")
def models():
    return _models_payload()


@app.post("/api/models/route")
def model_route(body: RouteUpdate):
    try:
        part_api.set_route(body.role, body.profile)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return _models_payload()


@app.post("/api/models/profile")
def model_profile_save(body: ProfileSave):
    if not body.url.strip():
        raise HTTPException(400, "端点 URL 不能为空")
    if not body.model.strip():
        raise HTTPException(400, "模型名不能为空")
    if not body.id and not body.key.strip():
        raise HTTPException(400, "新建端点必须填写 API KEY")
    try:
        part_api.upsert_profile(
            body.id, body.name.strip(), body.url.strip(),
            body.key.strip(), body.model.strip(),
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return _models_payload()


@app.post("/api/models/profile/delete")
def model_profile_delete(body: ProfileDelete):
    part_api.delete_profile(body.id)
    return _models_payload()


_TEST_IMG_URI = None


def _test_image_uri():
    """运行时生成一张 32x32 纯红 PNG（stdlib 手搓，无第三方依赖），用于探测端点视觉能力。"""
    global _TEST_IMG_URI
    if _TEST_IMG_URI is None:
        w = h = 32
        raw = b"".join(b"\x00" + b"\xff\x00\x00" * w for _ in range(h))

        def chunk(tag, data):
            return (struct.pack(">I", len(data)) + tag + data
                    + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff))

        png = (b"\x89PNG\r\n\x1a\n"
               + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
               + chunk(b"IDAT", zlib.compress(raw))
               + chunk(b"IEND", b""))
        _TEST_IMG_URI = "data:image/png;base64," + base64.b64encode(png).decode()
    return _TEST_IMG_URI


@app.post("/api/models/test")
async def model_profile_test(body: ProfileTest):
    """连通性 / 文本延迟 / 视觉能力测试，结果只读不写配置。"""
    profile = part_api.get_profile(body.id)
    if not profile:
        raise HTTPException(404, "没有这个端点")
    url, key, model = profile.get("url"), profile.get("key"), profile.get("model")
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"

    async def _probe(messages, timeout):
        payload = {"model": model, "messages": messages, "stream": False, "max_tokens": 64}
        start = time.monotonic()
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, json=payload, headers=headers,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as resp:
                    raw = await resp.text()
                    latency = round((time.monotonic() - start) * 1000)
                    try:
                        data = json.loads(raw)
                    except Exception:
                        return {"ok": False, "latency_ms": latency,
                                "error": f"HTTP {resp.status}（返回非 JSON，疑似网关错误页）"}
                    choices = data.get("choices") if isinstance(data, dict) else None
                    if not choices:
                        err = data.get("error") if isinstance(data, dict) else None
                        msg = err.get("message") if isinstance(err, dict) else str(data)[:200]
                        return {"ok": False, "latency_ms": latency, "error": f"HTTP {resp.status}: {msg}"}
                    reply = ((choices[0].get("message") or {}).get("content") or "").strip()
                    return {"ok": True, "latency_ms": latency, "reply": reply[:100]}
        except (asyncio.TimeoutError, aiohttp.ServerTimeoutError):
            return {"ok": False, "latency_ms": round((time.monotonic() - start) * 1000),
                    "error": f"超时（{timeout}s 内未响应）"}
        except Exception as e:
            return {"ok": False, "latency_ms": round((time.monotonic() - start) * 1000),
                    "error": str(e)[:200]}

    async def _probe_text():
        text_result = await _probe([{"role": "user", "content": "ping，只回复 pong"}], 45)
        if text_result["ok"] and not text_result.get("reply"):
            text_result["note"] = "端点响应正常但内容为空（可能为推理模型，token 耗尽于思考）"
        return text_result

    async def _probe_vision():
        vision = await _probe([{"role": "user", "content": [
            {"type": "text", "text": "这张图片是什么颜色？只回答颜色名称"},
            {"type": "image_url", "image_url": {"url": _test_image_uri()}},
        ]}], 60)
        if vision["ok"]:
            reply = vision.get("reply", "")
            if not reply:
                vision["supported"] = False
                vision["note"] = "返回内容为空，无法确认视觉能力"
            else:
                negative = any(k in reply for k in ("无法", "不能", "看不到", "没有图", "抱歉", "不支持")) \
                    or any(k in reply.lower() for k in ("cannot", "unable", "no image", "can't"))
                vision["supported"] = not negative
                if not negative and "红" not in reply and "red" not in reply.lower():
                    vision["note"] = "端点接受了图片但描述与测试图不符，视觉能力存疑"
        else:
            vision["supported"] = False
        return vision

    test_type = getattr(body, "type", "all") or "all"
    if test_type in ("text", "conn"):
        return {"text": await _probe_text(), "vision": None}
    elif test_type == "vision":
        return {"text": None, "vision": await _probe_vision()}
    else:
        text_result = await _probe_text()
        vision_result = None
        if text_result["ok"]:
            vision_result = await _probe_vision()
        return {"text": text_result, "vision": vision_result}


# ====== 日志 ======
@app.get("/api/logs/tail")
def logs_tail(lines: int = 200):
    if not os.path.isfile(LOG_FILE):
        return {"lines": []}
    with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
        content = f.read()
    return {"lines": content.splitlines()[-max(1, min(2000, lines)):]}


@app.websocket("/ws/logs")
async def ws_logs(websocket: WebSocket):
    await websocket.accept()
    try:
        offset = 0
        buffer = b""
        if os.path.isfile(LOG_FILE):
            size = os.path.getsize(LOG_FILE)
            start = max(0, size - 64 * 1024)
            with open(LOG_FILE, "rb") as f:
                f.seek(start)
                data = f.read()
            offset = size
            lines = data.decode("utf-8", "replace").splitlines()
            if start > 0 and lines:
                lines = lines[1:]
            await websocket.send_json({"type": "init", "lines": lines[-400:]})

        last_ping = time.time()
        while True:
            await asyncio.sleep(0.4)
            # 心跳：日志长时间无新增时也持续发帧，客户端据此判断连接是否半开
            now = time.time()
            if now - last_ping >= 10:
                try:
                    await websocket.send_json({"type": "ping"})
                except Exception:
                    return
                last_ping = now
            if not os.path.isfile(LOG_FILE):
                continue
            size = os.path.getsize(LOG_FILE)
            if size < offset:
                offset = 0
                buffer = b""
            if size == offset:
                continue
            with open(LOG_FILE, "rb") as f:
                f.seek(offset)
                chunk = f.read(size - offset)
            offset += len(chunk)
            buffer += chunk
            if len(buffer) > 512 * 1024:
                buffer = buffer[-4096:]
            parts = buffer.split(b"\n")
            buffer = parts.pop()
            lines = [p.decode("utf-8", "replace") for p in parts if p]
            if lines:
                await websocket.send_json({"type": "append", "lines": lines})
    except (WebSocketDisconnect, asyncio.CancelledError):
        return
    except Exception:
        return


if __name__ == "__main__":
    host = os.environ.get("QQ_BOT_WEB_HOST", "127.0.0.1")
    port = int(os.environ.get("QQ_BOT_WEB_PORT", "8090"))
    print(f"QQ-BOT WebUI: http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="warning")
