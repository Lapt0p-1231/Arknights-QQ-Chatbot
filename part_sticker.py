"""表情包全局捕获与重放模块。

所有私聊、群聊收到的表情（商城表情 mface、自定义收藏图片表情 image sub_type=1）
统一存储在本地公共目录 data/stickers/ 中，实现全场景互通共享。
"""

import asyncio
import base64
import hashlib
import json
import os
import random
import re
import time
import builtins
from typing import Any, Dict, List, Optional
import aiohttp


# 解决后台运行时 print() 不可见的问题：
# 劫持当前模块的 print，将其重定向到 QQ-BOT.py 的全局 log() 函数。
# 这样所有的 [表情] 和 [表情] 日志都会写入 QQ-BOT.log 并在 WebUI 实时显示。
def _log_print(*args, **kwargs):
    text = " ".join(str(a) for a in args)
    try:
        import __main__
        if hasattr(__main__, "log"):
            __main__.log(text)
            return
    except Exception:
        pass
    try:
        import QQ_BOT
        if hasattr(QQ_BOT, "log"):
            QQ_BOT.log(text)
            return
    except Exception:
        pass
    # 如果主程序还没加载或 log 不存在，退回原生 print
    builtins.print(*args, **kwargs)


print = _log_print

# 引用统一的 DATA_DIR
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("QQ_BOT_DATA_DIR", os.path.join(BASE_DIR, "data"))

STICKERS_DIR = os.path.join(DATA_DIR, "stickers")
STICKERS_CATEGORIES_DIR = os.path.join(STICKERS_DIR, "categories")
STICKERS_DB_FILE = os.path.join(STICKERS_DIR, "stickers_db.json")

# 情绪文件夹名常量（未分类图片直接存放在 categories 根目录下）
FOLDER_NAMES = {
    "happy": "开心_得意",
    "sad": "难过_委屈",
    "angry": "生气_暴躁",
    "annoyed": "傲娇_无奈",
    "shocked": "震惊_疑惑",
    "flirty": "害羞_心动",
}

# 确保公共目录和各情绪分类文件夹存在
os.makedirs(STICKERS_CATEGORIES_DIR, exist_ok=True)
for _folder in FOLDER_NAMES.values():
    os.makedirs(os.path.join(STICKERS_CATEGORIES_DIR, _folder), exist_ok=True)

_db_lock = asyncio.Lock()
# WebUI 与 QQ-BOT 主程序是两个独立进程，各自持有本模块的内存缓存。
# 因此缓存必须按文件 mtime+size 自动失效：主程序写入新表情后，
# WebUI 进程下次查询统计时会检测到文件变化并重新读盘，否则永远显示旧数据（0）。
_db_cache: Dict[str, Any] = {"mtime_ns": -1, "size": -1, "data": None}


def _init_empty_db() -> Dict[str, List[Dict[str, Any]]]:
    return {
        "mface": [],    # 商城表情元数据（免下载，持久有效）
        "images": [],   # 收藏图片/动图（本地持久化图片+标签）
    }


def _load_db_sync() -> Dict[str, List[Dict[str, Any]]]:
    """同步读取表情数据库"""
    if not os.path.exists(STICKERS_DB_FILE):
        db = _init_empty_db()
        _save_db_sync(db)
        return db
    try:
        with open(STICKERS_DB_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if not isinstance(data, dict):
                return _init_empty_db()
            data.setdefault("mface", [])
            data.setdefault("images", [])
            return data
    except Exception as e:
        print(f"[表情] 加载数据库失败: {e}")
        return _init_empty_db()


def _save_db_sync(data: Dict[str, Any]) -> None:
    """同步写入表情数据库"""
    try:
        temp_file = STICKERS_DB_FILE + ".tmp"
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(temp_file, STICKERS_DB_FILE)
    except Exception as e:
        print(f"[表情] 保存数据库失败: {e}")


def _db_file_stat() -> tuple[int, int]:
    """读取数据库文件的 mtime_ns 和 size，用于跨进程缓存失效判断"""
    try:
        st = os.stat(STICKERS_DB_FILE)
        return st.st_mtime_ns, st.st_size
    except OSError:
        return -1, -1


async def get_db() -> Dict[str, List[Dict[str, Any]]]:
    """获取表情数据库（内存缓存 + 文件变化自动重载 + 异步安全）"""
    async with _db_lock:
        mtime_ns, size = _db_file_stat()
        if _db_cache["data"] is None or (mtime_ns, size) != (_db_cache["mtime_ns"], _db_cache["size"]):
            _db_cache["data"] = await asyncio.to_thread(_load_db_sync)
            _db_cache["mtime_ns"], _db_cache["size"] = mtime_ns, size
        return _db_cache["data"]


async def save_db(db: Dict[str, Any]) -> None:
    """持久化表情数据库"""
    async with _db_lock:
        await asyncio.to_thread(_save_db_sync, db)
        _db_cache["data"] = db
        _db_cache["mtime_ns"], _db_cache["size"] = _db_file_stat()


def _detect_ext(data: bytes, url: str = "") -> str:
    """根据文件魔数或 URL 后缀识别图片格式"""
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return ".gif"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"RIFF") and len(data) > 12 and data[8:12] == b"WEBP":
        return ".webp"
    # 后备：从 URL 尝试解析
    clean_url = url.split("?")[0].lower()
    for ext in [".gif", ".png", ".jpg", ".jpeg", ".webp"]:
        if clean_url.endswith(ext):
            return ".jpg" if ext == ".jpeg" else ext
    return ".jpg"


async def _download_image(url: str, timeout: int = 15) -> Optional[bytes]:
    """下载图片字节流"""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=timeout) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    if data and len(data) <= 15 * 1024 * 1024:
                        return data
    except Exception as e:
        print(f"[表情] 下载图片失败 ({url[:60]}...): {e}")
    return None


async def capture_from_event(
    event: Dict[str, Any],
    caption: Optional[str] = None
) -> None:
    """从接收到的 OneBot 消息事件中捕获表情，保存到公共表情库。
    
    自动处理：
    1. mface (商城大表情): 提取 package_id, id, key, summary 等参数，永久可重放。
    2. image (普通表情图片/动图): 当 sub_type==1 或作为表情包时，下载至本地目录并去重。
    """
    if event.get("post_type") != "message":
        return

    msg_type = event.get("message_type")
    user_id = event.get("user_id")
    group_id = event.get("group_id")
    source = f"group_{group_id}" if msg_type == "group" else f"private_{user_id}"

    raw_message = event.get("message", [])
    if isinstance(raw_message, str):
        # 字符串格式暂不解析复杂结构，由 segment 数组模式为主
        return

    db = await get_db()
    changed = False

    for seg in raw_message:
        if not isinstance(seg, dict):
            continue
        seg_type = seg.get("type")
        seg_data = seg.get("data", {}) or {}

        # ====== 1. 商城大表情 (mface / 带emoji_id的image) ======
        # 注意：部分 NapCat 版本会把商城大表情包装成 type="image" 并附带 emoji_id/package_id
        is_mface = seg_type in ("mface", "market_face") or \
                   (seg_type == "image" and ("emoji_id" in seg_data or "emojiId" in seg_data or "key" in seg_data))

        if is_mface:
            emoji_id = str(seg_data.get("id") or seg_data.get("emoji_id") or seg_data.get("emojiId") or "")
            pkg_id = str(seg_data.get("package_id") or seg_data.get("emoji_package_id") or seg_data.get("packageId") or "")
            key = str(seg_data.get("key") or "")
            summary = str(seg_data.get("summary") or "").strip()

            if not emoji_id and not key:
                continue

            # 去重：按 (package_id, id) 或 key 匹配
            exists = False
            for item in db["mface"]:
                if (pkg_id and item.get("package_id") == pkg_id and item.get("id") == emoji_id) or \
                   (key and item.get("key") == key):
                    exists = True
                    # 如果原先没有标签但这次有了，补全标签
                    if not item.get("summary") and summary:
                        item["summary"] = summary
                        changed = True
                    break

            if not exists:
                new_item = {
                    "type": "mface",
                    "id": emoji_id,
                    "package_id": pkg_id,
                    "key": key,
                    "summary": summary,
                    "caption": caption or (summary.strip("[]") if summary else ""),
                    "source": source,
                    "sender_id": user_id,
                    "created_at": int(time.time()),
                }
                db["mface"].append(new_item)
                changed = True
                print(f"[表情] 捕获新商城表情: {summary or emoji_id} (pkg={pkg_id}, 来源: {source})")

        # ====== 2. 自定义/收藏图片表情 (image 且 sub_type=1) ======
        elif seg_type == "image":
            sub_type = str(seg_data.get("sub_type", seg_data.get("subType", "0")))
            url = seg_data.get("url")
            # 兼容 sub_type 为 "1", 1, 1.0 等情况
            is_sticker = sub_type in ("1", "1.0") or (sub_type.replace(".", "", 1).isdigit() and float(sub_type) == 1.0)

            if not is_sticker or not url:
                continue

            # 下载图片流
            img_bytes = await _download_image(url)
            if not img_bytes:
                continue

            # 计算图片 SHA256 哈希
            img_hash = hashlib.sha256(img_bytes).hexdigest()
            ext = _detect_ext(img_bytes, url)
            filename = f"{img_hash[:16]}{ext}"
            # 新捕获的未分类图片直接存放在 categories 根目录，方便用户随时拖拽整理
            uncat_file_path = os.path.join(STICKERS_CATEGORIES_DIR, filename)

            # 去重：判断是否已入库
            exists = False
            for item in db["images"]:
                if item.get("hash") == img_hash:
                    exists = True
                    # 如果有新 caption，更新已有条目
                    if caption and not item.get("caption"):
                        item["caption"] = caption
                        changed = True
                    break

            if not exists:
                # 写入 categories 根目录
                if not os.path.exists(uncat_file_path):
                    try:
                        with open(uncat_file_path, "wb") as f:
                            f.write(img_bytes)
                    except Exception as e:
                        print(f"[表情] 保存图片文件失败 {uncat_file_path}: {e}")
                        continue

                new_img_item = {
                    "type": "image",
                    "hash": img_hash,
                    "filename": filename,
                    "sub_type": "1",
                    "caption": caption or "",
                    "source": source,
                    "sender_id": user_id,
                    "created_at": int(time.time()),
                }
                db["images"].append(new_img_item)
                changed = True
                print(f"[表情] 捕获新收藏图片表情 -> categories根目录: {filename} (描述: {caption or '无'}, 来源: {source})")

    if changed:
        await save_db(db)


def find_sticker_image_path(filename: str) -> Optional[str]:
    """寻找本地表情图片的实际路径：优先在各分类子文件夹中找，找不到再去 categories 根目录找"""
    if not filename:
        return None
    clean_name = os.path.basename(filename)
    # 1. 在各个分类子目录中找
    for folder in FOLDER_NAMES.values():
        p = os.path.join(STICKERS_CATEGORIES_DIR, folder, clean_name)
        if os.path.exists(p):
            return os.path.abspath(p)
    # 2. 在 categories 根目录（未分类区）找
    root_p = os.path.join(STICKERS_CATEGORIES_DIR, clean_name)
    if os.path.exists(root_p):
        return os.path.abspath(root_p)
    return None


def get_root_images_sync() -> List[Dict[str, Any]]:
    """扫描 categories 根目录（未分类区）的所有图片"""
    if not os.path.exists(STICKERS_CATEGORIES_DIR):
        return []
    db = _load_db_sync()
    meta_by_name = {it.get("filename"): it for it in db.get("images", []) if it.get("filename")}
    valid_exts = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
    results = []
    try:
        for entry in os.scandir(STICKERS_CATEGORIES_DIR):
            if not entry.is_file():
                continue
            name = entry.name
            stem, ext = os.path.splitext(name)
            if ext.lower() not in valid_exts:
                continue
            meta = meta_by_name.get(name, {})
            is_hash = bool(re.fullmatch(r"[0-9a-fA-F]{12,64}", stem))
            results.append({
                "type": "image",
                "filename": name,
                "file_path": os.path.abspath(entry.path),
                "sub_type": "1",
                "caption": meta.get("caption", ""),
                "summary": "" if is_hash else stem,
                "folder": "未分类",
            })
    except Exception as e:
        print(f"[表情] 扫描未分类图片失败: {e}")
    return results


def get_category_images_sync(category_key: str) -> List[Dict[str, Any]]:
    """动态扫描某个情绪分类文件夹下的所有有效图片文件，实时反映用户的移动/删除/重命名"""
    folder_name = FOLDER_NAMES.get(category_key)
    if not folder_name:
        return []
    folder_path = os.path.join(STICKERS_CATEGORIES_DIR, folder_name)
    if not os.path.exists(folder_path):
        return []

    # 查阅 db 中的 caption 和元数据做辅助（如果有的话）
    db = _load_db_sync()
    meta_by_name = {it.get("filename"): it for it in db.get("images", []) if it.get("filename")}

    results = []
    valid_exts = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
    try:
        for entry in os.scandir(folder_path):
            if not entry.is_file():
                continue
            name = entry.name
            stem, ext = os.path.splitext(name)
            if ext.lower() not in valid_exts:
                continue
            meta = meta_by_name.get(name, {})
            is_hash = bool(re.fullmatch(r"[0-9a-fA-F]{12,64}", stem))
            title = "" if is_hash else stem
            results.append({
                "type": "image",
                "filename": name,
                "file_path": os.path.abspath(entry.path),
                "sub_type": "1",
                "caption": meta.get("caption", ""),
                "summary": title or meta.get("summary", ""),
                "folder": folder_name,
            })
    except Exception as e:
        print(f"[表情] 扫描情绪文件夹 {folder_name} 失败: {e}")
    return results


def build_replay_segment(sticker_entry: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """将表情库里的条目构造成 NapCat / OneBot v11 发送消息段"""
    st_type = sticker_entry.get("type")

    if st_type == "mface":
        # NapCat 接收/发送商城表情使用 emoji_id / emoji_package_id 字段名
        seg_data = {
            "emoji_id": str(sticker_entry.get("id", "")),
            "emoji_package_id": str(sticker_entry.get("package_id", "")),
            "key": str(sticker_entry.get("key", "")),
        }
        if sticker_entry.get("summary"):
            seg_data["summary"] = str(sticker_entry.get("summary"))
        return {"type": "mface", "data": seg_data}

    elif st_type == "image":
        file_path = sticker_entry.get("file_path")
        filename = sticker_entry.get("filename")
        if not file_path:
            file_path = find_sticker_image_path(filename)
        if not file_path or not os.path.exists(file_path):
            print(f"[表情] 本地图片缺失: {filename or file_path}")
            return None
        try:
            # NapCat 跑在 Docker 容器中，无法访问 Windows 宿主路径 file:///E:/...，
            # 必须内嵌 base64，容器才能直接解码图片发送。
            with open(file_path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("ascii")
        except Exception as e:
            print(f"[表情] 读取本地图片失败 {filename}: {e}")
            return None
        return {
            "type": "image",
            "data": {
                "file": f"base64://{b64}",
                "sub_type": 1
            }
        }

    return None


async def get_sticker_stats() -> Dict[str, Any]:
    """获取当前本地公共表情库的统计信息，包含各个情绪文件夹中的实时图片数"""
    db = await get_db()
    cat_counts = {}
    valid_exts = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
    for k, folder in FOLDER_NAMES.items():
        p = os.path.join(STICKERS_CATEGORIES_DIR, folder)
        cnt = 0
        if os.path.exists(p):
            try:
                cnt = sum(1 for e in os.scandir(p) if e.is_file() and os.path.splitext(e.name)[1].lower() in valid_exts)
            except Exception:
                cnt = 0
        cat_counts[folder] = cnt

    # 统计 categories 根目录（未分类）的图片数量
    uncategorized_cnt = 0
    if os.path.exists(STICKERS_CATEGORIES_DIR):
        try:
            uncategorized_cnt = sum(
                1 for e in os.scandir(STICKERS_CATEGORIES_DIR)
                if e.is_file() and os.path.splitext(e.name)[1].lower() in valid_exts
            )
        except Exception:
            pass
    cat_counts["未分类(根目录)"] = uncategorized_cnt

    return {
        "mface_count": len(db.get("mface", [])),
        "image_count": len(db.get("images", [])),
        "total": len(db.get("mface", [])) + len(db.get("images", [])),
        "categories": cat_counts,
    }


# ====== 触发与决策机制 ======
# 独立的作用域冷却计时器（按群或私聊区分，例如 "group_123456" 或 "private_123456"）
_last_sticker_time: Dict[str, float] = {}


# ====== 情绪聚类分类体系（方案二核心 + 方案一辅助） ======
EMOTION_CATEGORIES = {
    "happy": {
        "name": "开心/得意",
        "tags": ["开心", "雀跃", "得意", "兴奋", "期待", "高兴", "感动", "自豪", "欣喜", "好耶", "哈哈", "嘿嘿", "嘻嘻", "心心", "喜欢", "赞", "大拇指", "棒", "笑", "乐", "绝了", "太棒了", "庆祝", "爱心"],
    },
    "sad": {
        "name": "难过/委屈",
        "tags": ["难过", "低落", "委屈", "伤心", "悲伤", "失落", "哭", "哭泣", "眼泪", "泪目", "呜呜", "难受", "痛", "惨", "闭眼哭泣", "抹泪", "呜", "虐", "流泪", "自闭"],
    },
    "angry": {
        "name": "生气/暴躁",
        "tags": ["生气", "愤怒", "抓狂", "暴躁", "恼火", "不爽", "炸毛", "发火", "怒", "气愤", "操", "闭嘴", "滚", "打死", "咬牙", "不满", "气死", "怒火", "瞪"],
    },
    "annoyed": {
        "name": "傲娇/无语/嫌弃",
        "tags": ["傲娇", "嫌弃", "无奈", "无聊", "厌恶", "调侃", "冷淡", "鄙视", "尴尬", "无语", "白眼", "翻白眼", "懒得理", "没救了", "双手交叉", "哼", "叹气", "呵", "嗤", "汗", "擦汗", "敷衍"],
    },
    "shocked": {
        "name": "震惊/疑惑",
        "tags": ["好奇", "惊讶", "震惊", "疑惑", "发呆", "纳闷", "迷茫", "懵", "问号", "？", "?", "奇怪", "探头", "真的假的", "怎么会", "哈？", "呆住", "愣住"],
    },
    "flirty": {
        "name": "害羞/心动",
        "tags": ["害羞", "脸红", "心心", "给你心心", "喜欢", "爱心", "撒娇", "心动", "娇羞", "羞涩", "蹭", "抱", "亲", "比心"],
    }
}


def get_sticker_categories(item: Dict[str, Any]) -> List[str]:
    """判断单个表情包归属于哪些情绪聚类桶"""
    text = f"{item.get('summary', '')} {item.get('caption', '')}".lower()
    return [k for k, v in EMOTION_CATEGORIES.items() if any(t in text for t in v["tags"])]


def detect_reply_emotion(
    mood: Optional[str] = None,
    text: Optional[str] = None,
    keyword: Optional[str] = None
) -> tuple[Optional[str], str]:
    """根据 AI 显式标签、实时档案心情(mood)、以及回复文本中的动作词，推断当前情绪大类。
    返回 (情绪桶key 或 None, 判定来源说明)"""
    # 1. 优先：AI 在句末明确写了 [表情:xxx]
    if keyword:
        kw = keyword.strip("[] ").lower()
        for k, v in EMOTION_CATEGORIES.items():
            if any(t in kw or kw in t for t in v["tags"]):
                return k, f"AI标签[{keyword}]"

    # 2. 核心（方案二）：读取档案中实时更新的心情 state["mood"]
    if mood:
        m = mood.strip().lower()
        for k, v in EMOTION_CATEGORIES.items():
            if any(t in m or m in t for t in v["tags"]):
                return k, f"档案心情[{mood}]"

    # 3. 辅助：分析回复文本本身的语气词和动作描述（如 （冷笑）、（叹气）、哈哈）
    if text:
        tc = text.lower()
        scores = {k: sum(1 for t in v["tags"] if t in tc) for k, v in EMOTION_CATEGORIES.items()}
        best_cat = max(scores, key=scores.get)
        if scores[best_cat] > 0:
            return best_cat, f"回复文本语气({scores[best_cat]}个词命中)"

    return None, ""


async def pick_sticker_by_mood(
    keyword: Optional[str] = None,
    text: Optional[str] = None,
    mood: Optional[str] = None,
    is_duel: bool = False
) -> Optional[Dict[str, Any]]:
    """情绪驱动的表情挑选器：
    - 绝不再纯随机乱发，情绪不对时宁可不发。
    - 文件夹优先：动态扫描用户放置在对应情绪文件夹 (categories/xxx) 里的图片！用户拖到哪就认哪。
    - 方案二优先：按角色当前档案心情与回复内容，锁定对应情绪桶中的候选表情。
    - 方案一辅助：当情绪桶中有多个表情时，使用向量相似度挑出与当句话语义最贴合的一张。
    """
    db = await get_db()

    # ====== 斗图模式 ======
    if is_duel:
        # 斗图模式：汇总所有分类文件夹里的图片 + 库中表情，挑鲜明情绪的
        duel_pool = []
        for cat_k in ("happy", "angry", "annoyed", "shocked", "flirty"):
            duel_pool.extend(get_category_images_sync(cat_k))
        for mf in db.get("mface", []):
            duel_pool.append(mf)
        if not duel_pool:
            # 兜底：如果分类文件夹都空着，取 categories 根目录下的未分类图片
            duel_pool.extend(get_root_images_sync())
        return random.choice(duel_pool) if duel_pool else None

    # 1. 判断当次回话的目标情绪
    target_cat, cat_source = detect_reply_emotion(mood=mood, text=text, keyword=keyword)
    if not target_cat:
        print(f"[表情] 当前心情为「{mood or '平静'}」，无鲜明情绪波动，跳过发送表情")
        return None

    cat_name = EMOTION_CATEGORIES[target_cat]["name"]
    folder_name = FOLDER_NAMES.get(target_cat, "")

    # 2. 核心优先：动态读取对应情绪文件夹中的图片！用户随时把图放进/挪出该文件夹即刻生效
    candidates = get_category_images_sync(target_cat)

    # 同时也把属于该分类的商城大表情(mface)纳入候选
    for mf in db.get("mface", []):
        if target_cat in get_sticker_categories(mf):
            candidates.append(mf)

    if not candidates:
        print(f"[表情] 情绪判定「{cat_name}」(来源={cat_source}, 文件夹={folder_name})，但文件夹中无图片，跳过发送（宁缺毋滥）")
        return None

    # 3. 方案一辅助：如果候选池有多张表情，用向量相似度挑选最贴合的一张
    if len(candidates) > 1:
        try:
            import part_record
            query_str = f"{cat_name} {mood or ''} {text[:60] if text else ''}".strip()
            cand_strs = [
                f"{it.get('summary') or ''} {it.get('caption') or ''}".strip() or it.get("folder", "") or cat_name
                for it in candidates
            ]
            # 异步线程池调用向量模型，避免阻塞事件循环
            vecs = await asyncio.to_thread(part_record._embed, cand_strs + [query_str])
            query_vec = vecs[-1]
            cand_vecs = vecs[:-1]
            sims = [sum(a * b for a, b in zip(query_vec, cv)) for cv in cand_vecs]
            max_sim = max(sims)
            # 找到所有与最高分差距在 0.02 以内的候选（分数基本相同的同类表情随机挑选，避免每次都发同一张图）
            top_indices = [i for i, s in enumerate(sims) if (max_sim - s) < 0.02]
            best_idx = random.choice(top_indices) if top_indices else max(range(len(sims)), key=lambda i: sims[i])
            chosen = candidates[best_idx]
            label = chosen.get("caption") or chosen.get("summary") or chosen.get("filename")
            print(f"[表情] 情绪「{cat_name}」(来源={cat_source}, 文件夹={folder_name}, 语义相似度 {sims[best_idx]:.2f}): {label[:40]}")
            return chosen
        except Exception as e:
            print(f"[表情] 向量排序降级: {e}")

    # 单张候选或降级
    chosen = random.choice(candidates)
    label = chosen.get("caption") or chosen.get("summary") or chosen.get("filename")
    print(f"[表情] 情绪「{cat_name}」(来源={cat_source}, 文件夹={folder_name}, 候选{len(candidates)}张随机): {label[:40]}")
    return chosen


async def process_reply_stickers(
    reply_text: str,
    scope: str,
    settings: Dict[str, Any],
    is_duel: bool = False,
    mood: Optional[str] = None
) -> tuple[str, Optional[Dict[str, Any]]]:
    """处理回复中的表情包决策。
    
    返回 (处理后的文本, 表情segment或None)。
    """
    clean_text = reply_text

    # 提取并剥除 AI 标签：[表情:xxx]
    ai_keyword = None
    tag_match = re.search(r"\[表情:([^\]]+)\]", clean_text)
    if tag_match:
        ai_keyword = tag_match.group(1).strip()
        clean_text = re.sub(r"\[表情:[^\]]+\]", "", clean_text).strip()

    if not settings.get("sticker_enabled", True):
        return clean_text, None

    # ====== 斗图模式 ======
    if is_duel:
        sticker = await pick_sticker_by_mood(keyword=ai_keyword, text=clean_text, mood=mood, is_duel=True)
        if sticker:
            seg = build_replay_segment(sticker)
            if seg:
                _last_sticker_time[scope] = time.time()
                return "", seg
        return clean_text, None

    # ====== 频率限制 / 冷却检查 ======
    now = time.time()
    cd = float(settings.get("sticker_cooldown", 20.0))
    last_sent = _last_sticker_time.get(scope, 0)
    in_cooldown = (now - last_sent) < cd

    mode = str(settings.get("sticker_mode", "AI")).upper()
    should_send = False
    target_kw = ai_keyword

    if mode == "AI":
        # AI 决策模式：AI 明确附带表情标签且未在冷却期内
        if ai_keyword and not in_cooldown:
            should_send = True
    elif mode == "PY":
        # 概率决策模式：掷骰命中且未在冷却期内
        if not in_cooldown:
            prob = float(settings.get("sticker_probability", 25.0))
            if random.random() * 100 < prob:
                should_send = True

    if should_send:
        sticker = await pick_sticker_by_mood(keyword=target_kw, text=clean_text, mood=mood)
        if sticker:
            seg = build_replay_segment(sticker)
            if seg:
                _last_sticker_time[scope] = now
                return clean_text, seg

    return clean_text, None


def is_incoming_pure_sticker(event: Dict[str, Any]) -> bool:
    """判断收到的消息是否是纯表情包（对方发了 mface 或收藏图且几乎没有文字）"""
    raw_message = event.get("message", [])
    if isinstance(raw_message, str):
        return False
    has_sticker = False
    text_content = ""
    for seg in raw_message:
        st = seg.get("type")
        sd = seg.get("data", {}) or {}
        is_mface_seg = st in ("mface", "market_face") or (st == "image" and ("emoji_id" in sd or "emojiId" in sd))
        is_sticker_img = st == "image" and str(sd.get("sub_type", "0")) in ("1", "1.0")
        if is_mface_seg or is_sticker_img:
            has_sticker = True
        elif st == "text":
            text_content += sd.get("text", "")
    # 只要包含表情，且附加文字小于等于2个字（如“？”“哈”），即视为纯表情/表情包投喂
    return has_sticker and len(text_content.strip()) <= 2

