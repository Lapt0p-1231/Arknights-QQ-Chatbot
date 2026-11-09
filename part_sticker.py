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
from urllib.parse import parse_qs, urlparse
import aiohttp


# 解决后台运行时 print() 不可见的问题：
# 劫持当前模块的 print，将其重定向到 QQ-BOT.py 注入的全局 logger。
# 这样所有的 [表情] 日志都会写入 QQ-BOT.log 并在 WebUI 实时显示。
_logger = None


def set_logger(log_func):
    global _logger
    _logger = log_func


def _log_print(*args, **kwargs):
    text = " ".join(str(a) for a in args)
    if _logger is not None:
        try:
            _logger(text)
            return
        except Exception:
            pass
    try:
        import __main__
        if hasattr(__main__, "log"):
            __main__.log(text)
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

VALID_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}

# 确保公共目录和各情绪分类文件夹存在
os.makedirs(STICKERS_CATEGORIES_DIR, exist_ok=True)
for _folder in FOLDER_NAMES.values():
    os.makedirs(os.path.join(STICKERS_CATEGORIES_DIR, _folder), exist_ok=True)

# 旧版本曾使用 data/stickers/images，现已统一放在 categories 根目录/分类目录。
# 仅当它存在且为空时删除，避免留下一个“永远空着”的目录。
_legacy_images_dir = os.path.join(STICKERS_DIR, "images")
try:
    if os.path.isdir(_legacy_images_dir) and not os.listdir(_legacy_images_dir):
        os.rmdir(_legacy_images_dir)
except Exception:
    pass

_session = None


def _get_session():
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(limit=30, keepalive_timeout=30)
        )
    return _session


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


def _write_bytes(path: str, data: bytes) -> None:
    with open(path, "wb") as f:
        f.write(data)


def _image_dhash(data: bytes) -> str:
    """计算 64 位差值哈希，用于容忍转码/压缩的重复图片识别。"""
    try:
        import io
        from PIL import Image
        im = Image.open(io.BytesIO(data)).convert("L").resize((9, 8), Image.Resampling.LANCZOS)
        px = list(im.getdata())
        bits = 0
        for row in range(8):
            base = row * 9
            for col in range(8):
                bits = (bits << 1) | (1 if px[base + col] > px[base + col + 1] else 0)
        return f"{bits:016x}"
    except Exception:
        return ""


def _hamming_hex(a: str, b: str) -> int:
    try:
        return bin(int(a, 16) ^ int(b, 16)).count("1")
    except Exception:
        return 64


def _iter_all_image_files():
    """遍历分类文件夹与根目录下的所有图片文件路径。"""
    dirs = [STICKERS_CATEGORIES_DIR] + [
        os.path.join(STICKERS_CATEGORIES_DIR, f) for f in FOLDER_NAMES.values()
    ]
    for d in dirs:
        if not os.path.exists(d):
            continue
        try:
            for e in os.scandir(d):
                if e.is_file() and os.path.splitext(e.name)[1].lower() in VALID_IMAGE_EXTS:
                    yield e.path, e.name
        except OSError:
            continue


def _find_renamed_by_phash(phash: str, exclude_name: str = "") -> Optional[str]:
    """用户重命名/移动图片后，用感知哈希找回它，避免再次捕获时把旧名文件重新写出来。"""
    if not phash:
        return None
    for path, name in _iter_all_image_files():
        if name == exclude_name:
            continue
        try:
            with open(path, "rb") as f:
                if _image_dhash(f.read()) == phash:
                    return name
        except Exception:
            continue
    return None


def encode_image_for_vision(path: str, max_side: int = 640, quality: int = 85):
    """给视觉模型打标前压缩图片：表情包常见 2MB 动图，原图既费视觉 token 又容易超时。
    缩到 max_side 内并转 JPEG；失败时回退原图。返回 (base64 字符串, mime)。"""
    try:
        import io
        from PIL import Image
        im = Image.open(path)
        im.seek(0)
        im = im.convert("RGB")
        im.thumbnail((max_side, max_side))
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=quality)
        return base64.b64encode(buf.getvalue()).decode("ascii"), "image/jpeg"
    except Exception:
        with open(path, "rb") as f:
            data = f.read()
        ext = _detect_ext(data, path)
        mime = {".png": "image/png", ".gif": "image/gif", ".webp": "image/webp"}.get(ext, "image/jpeg")
        return base64.b64encode(data).decode("ascii"), mime


def _backfill_phashes(db: Dict[str, Any]) -> bool:
    """给旧条目补算 phash（只算本地文件还在的），返回是否有改动。"""
    changed = False
    for item in db.get("images", []):
        if item.get("phash"):
            continue
        fn = item.get("filename")
        path = find_sticker_image_path(fn) if fn else None
        if not path:
            continue
        try:
            with open(path, "rb") as f:
                ph = _image_dhash(f.read())
            if ph:
                item["phash"] = ph
                changed = True
        except Exception:
            continue
    return changed


async def _download_image(url: str, timeout: int = 15) -> Optional[bytes]:
    """下载图片字节流"""
    try:
        async with _get_session().get(url, timeout=timeout) as resp:
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
    if not isinstance(raw_message, (list, tuple)):
        # 字符串格式或畸形消息（None）不解析，统一交给 segment 数组模式
        return

    # 先在锁外收集并完成图片下载（网络 IO 不持锁），最后在锁内统一读最新库去重落盘
    pending_mfaces = []
    pending_images = []
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
            pending_mfaces.append((emoji_id, pkg_id, key, summary))

        # ====== 2. 自定义/收藏图片表情 (image 且 sub_type=1) ======
        elif seg_type == "image":
            sub_type = str(seg_data.get("sub_type", seg_data.get("subType", "0")))
            url = seg_data.get("url")
            # 兼容 sub_type 为 "1", 1, 1.0 等情况
            is_sticker = sub_type in ("1", "1.0") or (sub_type.replace(".", "", 1).isdigit() and float(sub_type) == 1.0)

            if not is_sticker or not url:
                continue

            # 下载图片流（不持锁）
            img_bytes = await _download_image(url)
            if not img_bytes:
                continue
            img_hash = hashlib.sha256(img_bytes).hexdigest()
            ext = _detect_ext(img_bytes, url)
            # 稳定去重键：优先 md5 字段，其次 NapCat 的 file（通常是原图 MD5 文件名），
            # 再退回 URL 里的 fileid。QQ 多次下载同一张图可能因转码导致字节不同，
            # 只按下载内容哈希会重复入库，所以必须优先用这些稳定标识。
            file_name = str(seg_data.get("file") or "")
            file_key = os.path.splitext(os.path.basename(file_name))[0].strip().lower()
            md5_key = str(seg_data.get("md5") or seg_data.get("md5sum") or "").strip().lower()
            url_fileid = ""
            try:
                url_fileid = (parse_qs(urlparse(url).query).get("fileid") or [""])[0].strip()
            except Exception:
                url_fileid = ""
            dedup_id = md5_key or file_key or url_fileid
            phash = await asyncio.to_thread(_image_dhash, img_bytes)
            pending_images.append((img_hash, ext, img_bytes, dedup_id, phash))

    if not pending_mfaces and not pending_images:
        return

    async with _db_lock:
        # 下载期间别的任务可能已写入，锁内重新读最新库，避免脏写覆盖
        db = await asyncio.to_thread(_load_db_sync)
        db.setdefault("mface", [])
        db.setdefault("images", [])
        changed = await asyncio.to_thread(_backfill_phashes, db)

        for emoji_id, pkg_id, key, summary in pending_mfaces:
            exists = False
            for item in db["mface"]:
                # emoji_id 才是单表情的唯一标识；NapCat 的 key 在同一个表情包里
                # 可能是包级共享的，不能单独拿来去重，否则同包后续表情会被误判为已存在。
                same_id = bool(emoji_id) and item.get("id") == emoji_id
                same_key_only = (not emoji_id) and bool(key) and item.get("key") == key
                if same_id or same_key_only:
                    exists = True
                    # 如果原先没有标签但这次有了，补全标签
                    if not item.get("summary") and summary:
                        item["summary"] = summary
                        changed = True
                    break

            if not exists:
                db["mface"].append({
                    "type": "mface",
                    "id": emoji_id,
                    "package_id": pkg_id,
                    "key": key,
                    "summary": summary,
                    "caption": caption or (summary.strip("[]") if summary else ""),
                    "source": source,
                    "sender_id": user_id,
                    "created_at": int(time.time()),
                })
                changed = True
                print(f"[表情] 捕获新商城表情: {summary or emoji_id} (pkg={pkg_id}, 来源: {source})")

        for img_hash, ext, img_bytes, dedup_id, phash in pending_images:
            filename = f"{img_hash[:16]}{ext}"
            # 新捕获的未分类图片直接存放在 categories 根目录，方便用户随时拖拽整理
            uncat_file_path = os.path.join(STICKERS_CATEGORIES_DIR, filename)

            exists = False
            for item in db["images"]:
                same_stable = bool(dedup_id) and item.get("file_id") == dedup_id
                same_phash = bool(phash) and bool(item.get("phash")) and \
                    _hamming_hex(phash, item["phash"]) <= 2
                if same_stable or item.get("hash") == img_hash or same_phash:
                    exists = True
                    if dedup_id and not item.get("file_id"):
                        item["file_id"] = dedup_id
                        changed = True
                    if phash and not item.get("phash"):
                        item["phash"] = phash
                        changed = True
                    if caption and not item.get("caption"):
                        item["caption"] = caption
                        changed = True
                    # 旧文件被手动删掉时按原文件名补回；若是被重命名/移动过，则重链接到新文件名，避免生成重复文件
                    old_name = item.get("filename")
                    if old_name and not find_sticker_image_path(old_name):
                        renamed = await asyncio.to_thread(
                            _find_renamed_by_phash, item.get("phash") or phash, old_name
                        )
                        if renamed:
                            item["filename"] = renamed
                            changed = True
                            print(f"[表情] 检测到重命名，已重链接: {old_name} -> {renamed}")
                        else:
                            old_path = os.path.join(STICKERS_CATEGORIES_DIR, os.path.basename(old_name))
                            try:
                                await asyncio.to_thread(_write_bytes, old_path, img_bytes)
                            except Exception as e:
                                print(f"[表情] 补回图片文件失败 {old_path}: {e}")
                    break

            if not exists:
                if not os.path.exists(uncat_file_path):
                    try:
                        await asyncio.to_thread(_write_bytes, uncat_file_path, img_bytes)
                    except Exception as e:
                        print(f"[表情] 保存图片文件失败 {uncat_file_path}: {e}")
                        continue

                db["images"].append({
                    "type": "image",
                    "hash": img_hash,
                    "file_id": dedup_id,
                    "phash": phash,
                    "filename": filename,
                    "sub_type": "1",
                    "caption": caption or "",
                    "source": source,
                    "sender_id": user_id,
                    "created_at": int(time.time()),
                })
                changed = True
                print(f"[表情] 捕获新收藏图片表情 -> categories根目录: {filename} (描述: {caption or '无'}, 来源: {source})")

        if changed:
            await asyncio.to_thread(_save_db_sync, db)
            _db_cache["data"] = db
            _db_cache["mtime_ns"], _db_cache["size"] = _db_file_stat()


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
    valid_exts = VALID_IMAGE_EXTS
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


def get_category_images_sync(category_key: str, db: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """动态扫描某个情绪分类文件夹下的所有有效图片文件，实时反映用户的移动/删除/重命名。
    db 可由调用方传入复用，避免一次选图重复解析数据库。"""
    folder_name = FOLDER_NAMES.get(category_key)
    if not folder_name:
        return []
    folder_path = os.path.join(STICKERS_CATEGORIES_DIR, folder_name)
    if not os.path.exists(folder_path):
        return []

    # 查阅 db 中的 caption 和元数据做辅助（如果有的话）
    if db is None:
        db = _load_db_sync()
    meta_by_name = {it.get("filename"): it for it in db.get("images", []) if it.get("filename")}

    results = []
    valid_exts = VALID_IMAGE_EXTS
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
                "tags": meta.get("tags") or [],
                "intensity": meta.get("intensity"),
                "folder": folder_name,
            })
    except Exception as e:
        print(f"[表情] 扫描情绪文件夹 {folder_name} 失败: {e}")

    # 补充：仍留在根目录（未分类区）、但自动打标 emotion/tags 属于该桶的图片。
    # 已手动放进其他分类文件夹的图片以其所在文件夹为准，不跨桶重复出现。
    seen = {it["filename"] for it in results}
    fine_tags = set(STICKER_FINE_TAGS.get(category_key, []))
    root_dir = STICKERS_CATEGORIES_DIR
    for name, meta in meta_by_name.items():
        if name in seen:
            continue
        tags = meta.get("tags") or []
        if not (meta.get("emotion") == category_key or any(t in fine_tags for t in tags)):
            continue
        root_path = os.path.join(root_dir, name)
        if not os.path.exists(root_path):
            continue
        results.append({
            "type": "image",
            "filename": name,
            "file_path": os.path.abspath(root_path),
            "sub_type": "1",
            "caption": meta.get("caption", ""),
            "summary": meta.get("summary", ""),
            "tags": tags,
            "intensity": meta.get("intensity"),
            "folder": folder_name,
        })
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
            size = os.path.getsize(file_path)
            if size > 2 * 1024 * 1024:
                print(f"[表情] 素材较大 {filename} ({size/1024/1024:.1f}MB)，base64 发送较慢，建议压缩")
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
    valid_exts = VALID_IMAGE_EXTS
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

    # 图片总数以磁盘真实文件为准：用户手动删图/挪图后，db 里的历史记录不应再计入
    disk_image_count = sum(cat_counts.values())

    return {
        "mface_count": len(db.get("mface", [])),
        "image_count": disk_image_count,
        "db_image_count": len(db.get("images", [])),
        "total": len(db.get("mface", [])) + disk_image_count,
        "categories": cat_counts,
    }


# ====== 自动打标（是否调用模型由用户在 WebUI 决定） ======
def get_untagged_entries(limit: int = 1) -> List[Dict[str, Any]]:
    """取出还没打标、且失败次数未超限的条目；用于后台打标任务。
    文件已被手动删除的幽灵记录直接跳过，否则队列会被它永久卡住。"""
    db = _load_db_sync()
    out = []
    for kind in ("mface", "images"):
        for it in db.get(kind, []):
            if it.get("tagged_at"):
                continue
            if int(it.get("tag_attempts", 0) or 0) >= 3:
                continue
            if kind == "images" and not find_sticker_image_path(it.get("filename")):
                continue
            item = dict(it)
            item["_kind"] = kind
            out.append(item)
            if len(out) >= limit:
                return out
    return out


def cleanup_sticker_db(save_backup: bool = True) -> Dict[str, int]:
    """清理数据库里的幽灵记录：
    - 文件被改名/移动过的条目：用 phash 找回并重链接；
    - 文件确实不存在、且没有人工/自动打标投入（tags/emotion/intensity 全空）的条目：删除。
    安全阀：如果磁盘上一张图都看不到但库里有记录（例如数据目录被临时移走），整轮跳过，避免误删。
    """
    db = _load_db_sync()
    images = db.get("images", [])
    if not images:
        return {"removed": 0, "relinked": 0, "skipped": 0}
    disk_visible = sum(1 for _p, _n in _iter_all_image_files())
    if disk_visible == 0:
        return {"removed": 0, "relinked": 0, "skipped": len(images)}

    kept = []
    removed = relinked = skipped = 0
    for it in images:
        fn = it.get("filename")
        if fn and find_sticker_image_path(fn):
            kept.append(it)
            continue
        ph = it.get("phash")
        renamed = _find_renamed_by_phash(ph, fn or "") if ph else None
        if renamed:
            it["filename"] = renamed
            kept.append(it)
            relinked += 1
            continue
        if it.get("tags") or it.get("emotion") or it.get("intensity"):
            kept.append(it)
            skipped += 1
            continue
        removed += 1

    if removed or relinked:
        if save_backup and removed:
            try:
                import shutil
                shutil.copy2(STICKERS_DB_FILE, STICKERS_DB_FILE + ".bak")
            except Exception:
                pass
        db["images"] = kept
        _save_db_sync(db)
        _db_cache["data"] = db
        _db_cache["mtime_ns"], _db_cache["size"] = _db_file_stat()
        if removed:
            print(f"[表情] 已清理 {removed} 条文件不存在的历史记录" + (f"，重链接 {relinked} 条" if relinked else ""))
        elif relinked:
            print(f"[表情] 已重链接 {relinked} 条被改名/移动的表情记录")
    return {"removed": removed, "relinked": relinked, "skipped": skipped}


async def update_entry_tags(identifier: str, patch: Dict[str, Any]) -> bool:
    """按 filename（图片）或 id（mface）更新打标结果。"""
    async with _db_lock:
        db = await asyncio.to_thread(_load_db_sync)
        changed = False
        for kind in ("images", "mface"):
            for it in db.get(kind, []):
                key = it.get("filename") if kind == "images" else it.get("id")
                if key and str(key) == str(identifier):
                    it.update(patch)
                    changed = True
                    break
            if changed:
                break
        if changed:
            await asyncio.to_thread(_save_db_sync, db)
            _db_cache["data"] = db
            _db_cache["mtime_ns"], _db_cache["size"] = _db_file_stat()
        return changed


# ====== 触发与决策机制 ======
# 独立的作用域冷却计时器（按群或私聊区分，例如 "group_123456" 或 "private_123456"）
_last_sticker_time: Dict[str, float] = {}
_last_sticker_offer: Dict[str, float] = {}
_last_sticker_judge: Dict[str, float] = {}
_last_sticker_choice: Dict[str, str] = {}


# ====== 情绪聚类分类体系（方案二核心 + 方案一辅助） ======
EMOTION_CATEGORIES = {
    "happy": {
        "name": "开心/得意",
        "tags": ["开心", "雀跃", "得意", "兴奋", "期待", "高兴", "感动", "自豪", "欣喜", "好耶", "哈哈", "嘿嘿", "嘻嘻", "心心", "喜欢", "赞", "大拇指", "棒", "笑", "乐", "绝了", "太棒了", "庆祝", "爱心"],
    },
    "sad": {
        "name": "难过/委屈",
        "tags": ["难过", "低落", "委屈", "伤心", "悲伤", "失落", "伤感", "心碎", "破防", "emo", "哭", "哭泣", "眼泪", "泪目", "呜呜", "难受", "痛", "惨", "闭眼哭泣", "抹泪", "呜", "虐", "流泪", "自闭"],
    },
    "angry": {
        "name": "生气/暴躁",
        "tags": ["生气", "愤怒", "抓狂", "暴躁", "恼火", "不爽", "炸毛", "发火", "怒", "气愤", "操", "闭嘴", "滚", "打死", "咬牙", "不满", "气死", "怒火", "瞪", "烦躁", "烦死", "心烦", "火大"],
    },
    "annoyed": {
        "name": "傲娇/无语/嫌弃",
        "tags": ["傲娇", "嫌弃", "无奈", "无聊", "厌恶", "调侃", "冷淡", "鄙视", "尴尬", "无语", "白眼", "翻白眼", "懒得理", "没救了", "双手交叉", "哼", "叹气", "唉", "呵", "嗤", "汗", "擦汗", "敷衍", "吃醋", "疲惫", "心累", "好累", "没辙"],
    },
    "shocked": {
        "name": "震惊/疑惑",
        "tags": ["好奇", "惊讶", "震惊", "疑惑", "发呆", "纳闷", "迷茫", "懵", "问号", "？", "?", "奇怪", "探头", "真的假的", "怎么会", "哈？", "呆住", "愣住", "紧张", "不安", "焦虑", "害怕", "恐惧", "可怕", "吓"],
    },
    "flirty": {
        "name": "害羞/心动",
        "tags": ["害羞", "脸红", "心心", "给你心心", "喜欢", "爱心", "撒娇", "心动", "娇羞", "羞涩", "蹭", "抱", "亲", "比心", "讨厌", "娇嗔"],
    }
}

# 纯标点标签只用于 AI 关键词匹配，不参与回复文本打分（否则任何问句都会被判成"震惊/疑惑"）
EMOTION_TEXT_IGNORE_TAGS = {"？", "?"}


# 注入给主模型的细标签（比 6 个情绪桶更具体，但仍保持可控数量）
STICKER_FINE_TAGS = {
    "happy": ["开心笑", "得意", "嘲笑", "庆祝", "撒娇"],
    "sad": ["委屈", "哭泣", "失落", "求安慰"],
    "angry": ["生气", "炸毛", "威胁", "骂人"],
    "annoyed": ["无语", "白眼", "嫌弃", "扶额", "叹气"],
    "shocked": ["震惊", "疑惑", "问号", "呆住", "吃瓜"],
    "flirty": ["害羞", "心动", "比心", "亲亲", "抱抱"],
}

FINE_TAG_TO_BUCKET = {
    tag: bucket
    for bucket, tags in STICKER_FINE_TAGS.items()
    for tag in tags
}


def _bucket_has_images(bucket: str, db: Optional[Dict[str, Any]] = None) -> bool:
    """该情绪桶是否至少有一个可用表情（文件夹图片或同桶商城表情）。"""
    if get_category_images_sync(bucket, db):
        return True
    if db is None:
        db = _load_db_sync()
    return any(bucket in get_sticker_categories(mf) for mf in db.get("mface", []))


def get_available_buckets() -> List[str]:
    """返回当前至少有一张可用素材的情绪桶（一次读库 + 每桶一次目录扫描）。"""
    db = _load_db_sync()
    return [b for b in STICKER_FINE_TAGS if _bucket_has_images(b, db)]


def get_available_sticker_tags(buckets: Optional[List[str]] = None) -> str:
    """生成注入给主模型的可用细标签列表；没有素材的桶不展示。
    可传入 get_available_buckets() 的结果复用，避免重复扫描。"""
    if buckets is None:
        buckets = get_available_buckets()
    lines = []
    for bucket in buckets:
        tags = STICKER_FINE_TAGS.get(bucket) or []
        lines.append(
            f"- {bucket}（{EMOTION_CATEGORIES[bucket]['name']}）：" + "、".join(tags)
        )
    return "\n".join(lines)


def sticker_gate_open(settings: Dict[str, Any], scope: str) -> bool:
    """AI/PY 共用的冷却闸门：只检查开关和冷却，真正的发布判断由调用方决定。"""
    if not settings.get("sticker_enabled", True):
        return False
    now = time.time()
    cd = float(settings.get("sticker_cooldown", 20.0))
    last = max(
        _last_sticker_time.get(scope, 0.0),
        _last_sticker_offer.get(scope, 0.0),
        _last_sticker_judge.get(scope, 0.0),
    )
    return (now - last) >= cd


def mark_sticker_offer(scope: str) -> None:
    """本轮已经决定要发表情，记录 offer 时间，AI/PY 共用。"""
    _last_sticker_offer[scope] = time.time()


def mark_sticker_judge(scope: str) -> None:
    """AI 判断已经消耗过一次分模型调用：无论 send 与否都占用冷却，
    避免"判了不发"导致下一条回复继续白调分模型。"""
    _last_sticker_judge[scope] = time.time()


def pick_sticker_by_emotion(
    bucket: str,
    scope: str = "",
    tag: str = ""
) -> Optional[Dict[str, Any]]:
    """按情绪大类挑一张表情。
    - 同一作用域内自动排除上一张，避免连续重复。
    - 若 AI 判断给出了细分 tag，优先选打标命中该 tag 的素材；没有命中则回退全桶。"""
    if bucket not in EMOTION_CATEGORIES:
        return None
    db = _load_db_sync()
    candidates = get_category_images_sync(bucket, db)
    for mf in db.get("mface", []):
        if bucket in get_sticker_categories(mf):
            candidates.append(mf)
    if not candidates:
        return None

    if tag:
        tag_l = tag.strip().lower()
        tagged = []
        for it in candidates:
            tags = [str(t).lower() for t in (it.get("tags") or [])]
            cap = f"{it.get('summary') or ''} {it.get('caption') or ''}".lower()
            if tag_l in tags or (tag_l and tag_l in cap):
                tagged.append(it)
        if tagged:
            candidates = tagged

    last_key = _last_sticker_choice.get(scope, "")
    filtered = [
        it for it in candidates
        if (it.get("filename") or it.get("id") or "") != last_key
    ]
    chosen = random.choice(filtered or candidates)
    _last_sticker_choice[scope] = chosen.get("filename") or chosen.get("id") or ""
    return chosen


def get_sticker_categories(item: Dict[str, Any]) -> List[str]:
    """判断单个表情包归属于哪些情绪聚类桶（文件夹/自动打标/摘要都算）。"""
    tags = item.get("tags") or []
    text = f"{item.get('summary', '')} {item.get('caption', '')} {' '.join(str(t) for t in tags)}".lower()
    emotion = item.get("emotion")
    return [
        k for k, v in EMOTION_CATEGORIES.items()
        if emotion == k or any(t in text for t in v["tags"])
    ]


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
        scores = {
            k: sum(1 for t in v["tags"] if t not in EMOTION_TEXT_IGNORE_TAGS and t in tc)
            for k, v in EMOTION_CATEGORIES.items()
        }
        best_cat = max(scores, key=scores.get)
        if scores[best_cat] > 0:
            return best_cat, f"回复文本语气({scores[best_cat]}个词命中)"

    return None, ""


async def pick_sticker_by_mood(
    keyword: Optional[str] = None,
    text: Optional[str] = None,
    mood: Optional[str] = None,
    is_duel: bool = False,
    scope: str = "",
    tag: str = ""
) -> Optional[Dict[str, Any]]:
    """斗图模式与外部调用的选图入口：
    - is_duel=True：从所有分类桶 + 商城表情里随机挑一张（分类桶全空时回退未分类根目录）。
    - 其余情况：先按 keyword/mood/text 判断情绪桶，再交给 pick_sticker_by_emotion 选图。
    """
    if is_duel:
        db = await get_db()
        duel_pool = []
        for cat_k in ("happy", "angry", "annoyed", "shocked", "flirty"):
            duel_pool.extend(get_category_images_sync(cat_k, db))
        for mf in db.get("mface", []):
            duel_pool.append(mf)
        if not duel_pool:
            # 兜底：如果分类文件夹都空着，取 categories 根目录下的未分类图片
            duel_pool.extend(get_root_images_sync())
        return random.choice(duel_pool) if duel_pool else None

    target_cat, cat_source = detect_reply_emotion(mood=mood, text=text, keyword=keyword)
    if not target_cat:
        print(f"[表情] 当前心情为「{mood or '平静'}」，无鲜明情绪波动，跳过发送表情")
        return None
    chosen = pick_sticker_by_emotion(target_cat, scope, tag)
    if not chosen:
        cat_name = EMOTION_CATEGORIES[target_cat]["name"]
        print(f"[表情] 情绪判定「{cat_name}」(来源={cat_source})，但该桶无素材，跳过发送（宁缺毋滥）")
        return None
    label = chosen.get("caption") or chosen.get("summary") or chosen.get("filename")
    print(f"[表情] 情绪「{EMOTION_CATEGORIES[target_cat]['name']}」(来源={cat_source}): {label[:40]}")
    return chosen


async def process_reply_stickers(
    reply_text: str,
    scope: str,
    settings: Dict[str, Any],
    is_duel: bool = False,
    mood: Optional[str] = None,
    pending: Optional[Dict[str, Any]] = None
) -> tuple[str, Optional[Dict[str, Any]]]:
    """处理回复中的表情包决策。

    pending 由生成前的 AI 判断/PY 掷骰产生：
      {"source": "AI", "emotion": "annoyed"}
      {"source": "PY", "emotion": ""}   # 情绪在回复文本生成后再推断
    没有 pending 时不发表情；模型自己写的 [表情:...] 也会被剥掉。
    """
    clean_text = reply_text

    # 白名单里没有表情标签这个概念，统一剥掉
    ai_keyword = None
    tag_match = re.search(r"\[表情[:：]([^\]]+)\]", clean_text)
    if tag_match:
        ai_keyword = tag_match.group(1).strip()
        clean_text = re.sub(r"\[表情[:：][^\]]+\]", "", clean_text).strip()

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

    if not pending:
        return clean_text, None

    bucket = str(pending.get("emotion") or "")
    if not bucket:
        # PY：用最终回复的语气/当前心情推断情绪大类
        bucket, _ = detect_reply_emotion(mood=mood, text=clean_text)
    if not bucket:
        return clean_text, None

    sticker = pick_sticker_by_emotion(bucket, scope, str(pending.get("tag") or ""))
    if sticker:
        seg = build_replay_segment(sticker)
        if seg:
            _last_sticker_time[scope] = time.time()
            return clean_text, seg

    return clean_text, None


def is_incoming_pure_sticker(event: Dict[str, Any]) -> bool:
    """判断收到的消息是否是纯表情包（对方发了 mface 或收藏图且几乎没有文字）"""
    raw_message = event.get("message", [])
    if not isinstance(raw_message, (list, tuple)):
        return False
    has_sticker = False
    text_content = ""
    for seg in raw_message:
        if not isinstance(seg, dict):
            continue
        st = seg.get("type")
        sd = seg.get("data", {}) or {}
        if not isinstance(sd, dict):
            continue
        is_mface_seg = st in ("mface", "market_face") or (st == "image" and ("emoji_id" in sd or "emojiId" in sd))
        is_sticker_img = st == "image" and str(sd.get("sub_type", "0")) in ("1", "1.0")
        if is_mface_seg or is_sticker_img:
            has_sticker = True
        elif st == "text":
            text_content += sd.get("text", "")
    # 只要包含表情，且附加文字小于等于2个字（如“？”“哈”），即视为纯表情/表情包投喂
    return has_sticker and len(text_content.strip()) <= 2

