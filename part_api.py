"""API 端点与模型路由管理（api.json）。

- profiles：可复用的端点配置（url/key/model）
- routes：main（聊天回复）/ sub（判断、总结、打分、发散）分别指向哪个 profile
- 带 mtime 缓存，WebUI 改完立即生效（无需重启 bot）
"""
import json
import os
import re
import time
from urllib.parse import urlparse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.environ.get(
    "QQ_BOT_API_CONFIG", os.path.join(BASE_DIR, "api.json")
)

ROLES = ("main", "sub")

# 仅在 api.json 不存在时用于初始化；密钥通过 WebUI 或 api.json 配置
DEFAULT_CONFIG = {
    "profiles": [
        {
            "id": "deepseek",
            "name": "DeepSeek",
            "url": "https://api.deepseek.com/chat/completions",
            "key": os.environ.get("QQ_BOT_API_KEY", ""),
            "model": "deepseek-chat",
        }
    ],
    "routes": {"main": "deepseek", "sub": "deepseek"},
}

_cache = {"mtime": None, "data": None}


def _normalize(data):
    if not isinstance(data, dict):
        data = {}
    profiles = []
    for p in data.get("profiles") or []:
        if not isinstance(p, dict) or not p.get("id"):
            continue
        profiles.append({
            "id": str(p["id"]),
            "name": str(p.get("name") or p["id"]),
            "url": str(p.get("url") or ""),
            "key": str(p.get("key") or ""),
            "model": str(p.get("model") or ""),
        })
    routes = data.get("routes") if isinstance(data.get("routes"), dict) else {}
    routes = {role: str(routes.get(role) or "") for role in ROLES}
    # 路由兜底：指向不存在的 profile 时回退到第一个
    fallback = profiles[0]["id"] if profiles else ""
    for role in ROLES:
        if routes[role] not in {p["id"] for p in profiles}:
            routes[role] = fallback
    return {"profiles": profiles, "routes": routes}


def load_config():
    try:
        mtime = os.path.getmtime(CONFIG_FILE)
    except OSError:
        mtime = 0
    if _cache["data"] is not None and _cache["mtime"] == mtime:
        return json.loads(json.dumps(_cache["data"]))
    if mtime:
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = DEFAULT_CONFIG
    else:
        data = DEFAULT_CONFIG
    data = _normalize(data)
    _cache["mtime"] = mtime
    _cache["data"] = json.loads(json.dumps(data))
    return data


def save_config(data):
    data = _normalize(data)
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, CONFIG_FILE)
    _cache["mtime"] = None
    _cache["data"] = None


def list_profiles():
    return [dict(p) for p in load_config()["profiles"]]


def get_routes():
    return dict(load_config()["routes"])


def get_profile(pid):
    for p in load_config()["profiles"]:
        if p["id"] == pid:
            return dict(p)
    return None


def get_route(role):
    """返回该角色实际使用的 {id,name,url,key,model}，没有配置时返回空字典。"""
    cfg = load_config()
    pid = cfg["routes"].get(role)
    for p in cfg["profiles"]:
        if p["id"] == pid:
            return dict(p)
    return {}


def set_route(role, pid):
    if role not in ROLES:
        raise ValueError("role 只能是 main 或 sub")
    cfg = load_config()
    if pid not in {p["id"] for p in cfg["profiles"]}:
        raise ValueError("没有这个端点")
    cfg["routes"][role] = pid
    save_config(cfg)


def _new_id(name, existing):
    base = re.sub(r"[^0-9a-zA-Z_-]+", "-", (name or "").strip().lower()).strip("-")
    if not base:
        base = "api"
    pid, n = base, 2
    while pid in existing:
        pid = f"{base}-{n}"
        n += 1
    return pid


def _normalize_url(url):
    """裸域名或 /v1 这类基地址自动补全 /chat/completions；已是完整端点或有自定义路径的不动。"""
    url = (url or "").strip().rstrip("/")
    if not url or url.endswith("/chat/completions"):
        return url
    path = urlparse(url).path.rstrip("/")
    if not path or re.fullmatch(r"/v\d+", path):
        return url + "/chat/completions"
    return url


def upsert_profile(pid, name, url, key, model):
    """pid 为空=新建；key 为空且是编辑=保留旧 key。返回 profile id。"""
    url = _normalize_url(url)
    cfg = load_config()
    existing = {p["id"] for p in cfg["profiles"]}
    if pid:
        for p in cfg["profiles"]:
            if p["id"] == pid:
                p["name"] = name or p["name"]
                p["url"] = url
                p["model"] = model
                if key:
                    p["key"] = key
                break
        else:
            raise ValueError("没有这个端点")
    else:
        pid = _new_id(name or url, existing)
        cfg["profiles"].append({
            "id": pid, "name": name or pid, "url": url, "key": key, "model": model,
        })
        # 第一个端点自动接管两路路由
        if len(cfg["profiles"]) == 1:
            cfg["routes"] = {"main": pid, "sub": pid}
    save_config(cfg)
    return pid


def delete_profile(pid):
    cfg = load_config()
    cfg["profiles"] = [p for p in cfg["profiles"] if p["id"] != pid]
    save_config(cfg)
