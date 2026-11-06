"""提示词运行时管理。

- 提示词文件从 prompts/ 目录读取
- 开关/新增/排序配置保存在 prompts_config.json
- 每次读取都会检查配置与文件 mtime，WebUI 改完立即生效（无需重启 bot）
"""
import json
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROMPT_DIR = os.path.join(BASE_DIR, "prompts")
CONFIG_FILE = os.environ.get(
    "QQ_BOT_PROMPT_CONFIG", os.path.join(BASE_DIR, "prompts_config.json")
)

# 默认配置：白金人设作为可选模板，默认关闭，可在 WebUI 中启用
DEFAULT_FILES = [
    {"file": "01_前置破甲.txt", "enabled": True},
    {"file": "02_角色人设（白金）.txt", "enabled": False, "character": True},
    {"file": "03_符号格式.txt", "enabled": True, "format": True},
    {"file": "04_字数要求.txt", "enabled": True, "format": True},
    {"file": "05_人设拟合.txt", "enabled": True},
    {"file": "06_聊天模式.txt", "enabled": True, "chat": True},
    {"file": "07_记忆总结.txt", "enabled": True, "internal": True},
    {"file": "09_群聊判断.txt", "enabled": True, "internal": True},
    {"file": "10_记忆打分.txt", "enabled": True, "internal": True},
    {"file": "11_胡思乱想.txt", "enabled": True, "internal": True},
    {"file": "12_群聊回复.txt", "enabled": True, "internal": True},
]

# mtime 缓存：配置文件、提示词内容
_config_cache = {"mtime": None, "files": None}
_file_cache = {}


def _infer_flags(filename):
    """根据文件名推断用途标记，方便手动丢进 prompts/ 的新文件自动识别。"""
    flags = {}
    if filename.startswith("02_"):
        flags["character"] = False
    elif filename.startswith(("03_", "04_")):
        flags["format"] = True
    elif filename.startswith("06_"):
        flags["chat"] = True
    elif filename.startswith(("07_", "09_", "10_", "11_", "12_")):
        flags["internal"] = True
    return flags


def _scan_dir():
    names = []
    if os.path.isdir(PROMPT_DIR):
        for name in sorted(os.listdir(PROMPT_DIR)):
            if name.lower().endswith(".txt"):
                names.append(name)
    return names


def load_config():
    """读取配置；不存在则用默认配置。文件增删会自动合并进来。"""
    try:
        mtime = os.path.getmtime(CONFIG_FILE)
    except OSError:
        mtime = 0

    if _config_cache["files"] is not None and _config_cache["mtime"] == mtime:
        files = [dict(item) for item in _config_cache["files"]]
    else:
        if mtime:
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                raw = data.get("files", []) if isinstance(data, dict) else data
                files = [item for item in raw
                         if isinstance(item, dict) and item.get("file")]
            except Exception:
                files = [dict(item) for item in DEFAULT_FILES]
        else:
            files = [dict(item) for item in DEFAULT_FILES]
        _config_cache["mtime"] = mtime
        _config_cache["files"] = [dict(item) for item in files]

    known = {item["file"] for item in files}
    scanned = _scan_dir()
    scanned_set = set(scanned)
    # 配置里残留的已删除文件直接丢弃
    files = [item for item in files if item["file"] in scanned_set]
    for name in scanned:
        if name not in known:
            item = {"file": name, "enabled": False}
            item.update(_infer_flags(name))
            files.append(item)

    for item in files:
        item["enabled"] = bool(item.get("enabled", False))
        for flag in ("character", "format", "chat", "internal"):
            if flag in item:
                item[flag] = bool(item[flag])
    return files


def save_config(files):
    payload = {"files": files}
    tmp = CONFIG_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, CONFIG_FILE)
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        raise
    # 立即失效缓存
    _config_cache["mtime"] = None
    _config_cache["files"] = None


def get_prompt_files():
    return load_config()


def _read_file(filename):
    path = os.path.join(PROMPT_DIR, filename)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return ""
    cached = _file_cache.get(filename)
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read().strip()
    except Exception as e:
        print(f"[提示词] 读取 {filename} 失败: {e}")
        return ""
    _file_cache[filename] = (mtime, content)
    return content


def load_prompts(params=None, include_character=True,
                 include_format=True, include_chat=False):
    """按当前开关拼装系统提示词（与原 load_prompts 语义一致）。"""
    full_prompt = ""
    for item in load_config():
        if not item.get("enabled", False):
            continue
        if item.get("internal", False):
            continue
        if not include_character and item.get("character", False):
            continue
        if not include_format and item.get("format", False):
            continue
        if not include_chat and item.get("chat", False):
            continue
        content = _read_file(item["file"])
        if not content:
            continue
        if params:
            for key, value in params.items():
                content = content.replace(f"{{{key}}}", str(value))
        full_prompt += content + "\n"
    return full_prompt


def internal_prompt(filename):
    """读取分任务提示词（每次调用都走 mtime 缓存，改完即生效）。

    该文件在配置里被关闭时返回空字符串，调用方应据此跳过对应任务。
    """
    for item in load_config():
        if item["file"] == filename and not item.get("enabled", True):
            return ""
    return _read_file(filename)
