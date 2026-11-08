import json
import os
import re
import sqlite3
import threading
import time
import uuid
from collections import OrderedDict

# ====== 存储路径 ======
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("QQ_BOT_DATA_DIR", os.path.join(BASE_DIR, "data"))
MODEL_DIR = os.path.join(BASE_DIR, "models")
USER_LOG_DIR = os.path.join(BASE_DIR, "user_log")  # 旧JSON档案（迁移用）
DB_FILE = os.path.join(DATA_DIR, "memory.db")
CHROMA_DIR = os.path.join(DATA_DIR, "chroma")
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(USER_LOG_DIR, exist_ok=True)

# 热层对话默认加载轮数（进提示词的窗口，可在设置里按私聊/群聊分别调）
HOT_LIMIT = 30
# 数据库保留轮数（窗口可以调大，但最多只能看到这么多）
RETAIN_LIMIT = 200

# ====== 日志 ======
def log(text):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {text}"
    print(line)
    try:
        path = os.path.join(BASE_DIR, "QQ-BOT.log")
        if os.path.exists(path) and os.path.getsize(path) > 10 * 1024 * 1024:
            os.replace(path, path + ".1")
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ====== SQLite ======
def _db():
    # timeout 防止并发写入（发散/总结/消息处理）偶发 database is locked
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def _init_db():
    conn = _db()
    try:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS user_profile (
            user_id TEXT PRIMARY KEY,
            data TEXT
        );
        CREATE TABLE IF NOT EXISTS chat_history (
            user_id TEXT,
            ts REAL,
            role TEXT,
            content TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_history_user ON chat_history(user_id, ts);
        CREATE TABLE IF NOT EXISTS memory_meta (
            doc_id TEXT PRIMARY KEY,
            user_id TEXT,
            type TEXT,
            valence REAL,
            arousal REAL,
            importance REAL,
            ts REAL
        );
        CREATE INDEX IF NOT EXISTS idx_meta_user ON memory_meta(user_id, importance);
        """)
        # 轻量迁移：chat_history 增加 message_id，用于查引用回复的原文
        cols = [r[1] for r in conn.execute("PRAGMA table_info(chat_history)").fetchall()]
        if "message_id" not in cols:
            conn.execute("ALTER TABLE chat_history ADD COLUMN message_id TEXT")
            log("[记忆系统] chat_history 已迁移：新增 message_id 列")
        conn.commit()
    finally:
        conn.close()

_init_db()


# ====== 向量库（ChromaDB + bge-small-zh） ======
_chroma = None
_collection = None
_embedder = None
_embed_cache = OrderedDict()
_embed_cache_lock = threading.Lock()
_embed_infer_lock = threading.Lock()  # fastembed 推理串行化，避免多线程并发调用
EMBED_CACHE_SIZE = 256  # embedding 结果 LRU 缓存条数

def _get_collection():
    global _chroma, _collection
    if _collection is None:
        import chromadb
        _chroma = chromadb.PersistentClient(path=CHROMA_DIR)
        # HNSW 参数显式配置：兼顾召回率与写入性能
        _collection = _chroma.get_or_create_collection(
            "memories",
            metadata={
                "hnsw:space": "cosine",
                "hnsw:M": 32,
                "hnsw:construction_ef": 200,
                "hnsw:search_ef": 100,
            }
        )
    return _collection

def _get_embedder():
    global _embedder
    if _embedder is None:
        from fastembed import TextEmbedding
        log("[向量] 正在加载 bge-small-zh 模型...")
        _embedder = TextEmbedding("BAAI/bge-small-zh-v1.5", cache_dir=MODEL_DIR)
        log("[向量] 模型加载完成")
    return _embedder

def _embed(texts):
    # 文本级 LRU 缓存：重复查询/重复切片不重复推理
    results = [None] * len(texts)
    misses = []
    miss_idx = []
    with _embed_cache_lock:
        for i, text in enumerate(texts):
            cached = _embed_cache.get(text)
            if cached is not None:
                _embed_cache.move_to_end(text)
                results[i] = list(cached)
            else:
                misses.append(text)
                miss_idx.append(i)
    if misses:
        with _embed_infer_lock:
            vectors = [list(map(float, v)) for v in _get_embedder().embed(misses)]
        with _embed_cache_lock:
            for idx, text, vec in zip(miss_idx, misses, vectors):
                results[idx] = vec
                _embed_cache[text] = list(vec)
                _embed_cache.move_to_end(text)
            while len(_embed_cache) > EMBED_CACHE_SIZE:
                _embed_cache.popitem(last=False)
    return results


# 写入一条向量记忆，type: dialogue / musing / memory
def add_vector(user_id, content, type="dialogue", valence=0.0, arousal=0.0, importance=0.5,
               source_id=None, confidence=None):
    if not content or not content.strip():
        return None
    # 发散产物默认低置信度：它是梦，不是事实
    if confidence is None:
        confidence = 0.4 if type == "musing" else 1.0
    doc_id = uuid.uuid4().hex
    ts = time.time()
    try:
        vector = _embed([content])[0]
        _get_collection().add(
            ids=[doc_id],
            embeddings=[vector],
            documents=[content],
            metadatas=[{
                "user_id": str(user_id),
                "type": type,
                "source_id": str(source_id or ""),
                "confidence": float(confidence),
                "ts": float(ts),
                "valence": float(valence),
                "arousal": float(arousal),
                "importance": float(importance)
            }]
        )
        conn = _db()
        try:
            conn.execute(
                "INSERT INTO memory_meta VALUES (?,?,?,?,?,?,?)",
                (doc_id, str(user_id), type, valence, arousal, importance, ts)
            )
            conn.commit()
        finally:
            conn.close()
        log(f"[向量] 已存入 {user_id} 的 {type} 记忆（重要度 {importance}）")
        return doc_id
    except Exception as e:
        log(f"[向量] 存储失败 {user_id}: {e}")
        return None


# 语义召回：scopes 为档案名列表（私聊=[user_id]，群聊=[群档案, 发送者档案]）
# min_similarity：cosine 相似度下限（0-1），低于此值的记忆不召回（宁可没有，不要硬塞）
def recall(scopes, query, top_k=3, min_importance=0.0, min_similarity=0.0):
    if not query or not query.strip():
        return []
    try:
        vector = _embed([query])[0]
        where = {"user_id": {"$in": [str(s) for s in scopes]}}
        result = _get_collection().query(
            query_embeddings=[vector],
            n_results=top_k * 3,
            where=where,
            include=["documents", "metadatas", "distances"]
        )
        docs = result.get("documents", [[]])[0]
        if not docs:
            return []
        # cosine 距离转相似度
        distances = result.get("distances", [[]])[0]
        sims = {doc_id: 1.0 - d for doc_id, d in zip(result.get("ids", [[]])[0], distances)}
        # 用 meta 表的重要性过滤排序
        conn = _db()
        try:
            marks = ",".join("?" * len(scopes))
            rows = conn.execute(
                f"SELECT doc_id, importance, type FROM memory_meta WHERE user_id IN ({marks})",
                [str(s) for s in scopes]
            ).fetchall()
        finally:
            conn.close()
        # chroma 的 ids 与 documents 对齐
        ids = result.get("ids", [[]])[0]
        imp_map = {r[0]: (r[1], r[2]) for r in rows}
        items = []
        for doc_id, doc in zip(ids, docs):
            imp, mtype = imp_map.get(doc_id, (0.5, "dialogue"))
            sim = sims.get(doc_id, 0.0)
            if imp < min_importance:
                continue
            if sim < min_similarity:
                continue
            items.append((imp, sim, mtype, doc))
        # 相关性优先：相似度为主、重要度只加权微调（重要度不再盖过相似度）
        # 发散产物最多召回1条（梦境不能挤掉事实）
        items.sort(key=lambda x: (x[2] == "musing", -(x[1] * 0.7 + x[0] * 0.3)))
        kept, musing_seen = [], 0
        for item in items:
            if item[2] == "musing":
                musing_seen += 1
                if musing_seen > 1:
                    continue
            kept.append(item)
        return [(mtype, doc) for _, _, mtype, doc in kept[:top_k]]
    except Exception as e:
        log(f"[向量] 召回失败 {scopes}: {e}")
        return []


# 胡思乱想用：取高重要度记忆
def get_high_importance_memories(limit=3, min_importance=0.7, user_id=None):
    try:
        conn = _db()
        try:
            if user_id is None:
                rows = conn.execute(
                    "SELECT doc_id, user_id FROM memory_meta "
                    "WHERE importance >= ? AND type != 'musing' "
                    "ORDER BY importance DESC, ts DESC LIMIT ?",
                    (min_importance, limit * 5)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT doc_id, user_id FROM memory_meta "
                    "WHERE importance >= ? AND type != 'musing' AND user_id = ? "
                    "ORDER BY importance DESC, ts DESC LIMIT ?",
                    (min_importance, str(user_id), limit * 5)
                ).fetchall()
        finally:
            conn.close()
        if not rows:
            return []
        ids = [r[0] for r in rows]
        result = _get_collection().get(ids=ids, include=["documents"])
        doc_map = dict(zip(result.get("ids", []), result.get("documents", [])))
        out = []
        for doc_id, uid in rows:
            if doc_id in doc_map:
                out.append((uid, doc_id, doc_map[doc_id]))
            if len(out) >= limit:
                break
        return out
    except Exception as e:
        log(f"[向量] 读取高重要度记忆失败: {e}")
        return []


# ======== 档案汇总 ========
ARCHIVE_FILE = os.path.join(DATA_DIR, "archives.json")
_archive_sync_at = 0


def _archive_type(user_id):
    uid = str(user_id)
    if uid.startswith("group_"):
        rest = uid[len("group_"):]
        if "_" in rest:
            return "member", rest.split("_", 1)[0], rest.split("_", 1)[1]
        return "group", rest, None
    return "private", None, uid


def _touch_on(conn, uid, display_name=None):
    row = conn.execute("SELECT data FROM user_profile WHERE user_id=?", (uid,)).fetchone()
    if row is None:
        data = {"user_id": uid}
        if display_name:
            data["display_name"] = display_name
        conn.execute("INSERT INTO user_profile VALUES (?,?)",
                     (uid, json.dumps(data, ensure_ascii=False)))
        return True
    if display_name:
        try:
            data = json.loads(row[0]) or {}
        except Exception:
            data = {}
        if data.get("display_name") != display_name:
            data["display_name"] = display_name
            conn.execute("UPDATE user_profile SET data=? WHERE user_id=?",
                         (json.dumps(data, ensure_ascii=False), uid))
            return True
    return False


def touch_archive(user_id, display_name=None):
    """确保档案存在（用于记录所有说过话的群友/群聊/私聊）。"""
    try:
        conn = _db()
        try:
            changed = _touch_on(conn, str(user_id), display_name)
            if changed:
                conn.commit()
        finally:
            conn.close()
        sync_archive_index()
    except Exception as e:
        log(f"[档案] 记录 {user_id} 失败: {e}")


def touch_group_archives(group_id, user_id, nickname=None):
    """一条群消息同时留下：群档案 + 发送者个人档案（单次连接）。"""
    try:
        conn = _db()
        try:
            changed = _touch_on(conn, f"group_{group_id}")
            changed = _touch_on(conn, f"group_{group_id}_{user_id}", nickname) or changed
            if changed:
                conn.commit()
        finally:
            conn.close()
        sync_archive_index()
    except Exception as e:
        log(f"[档案] 记录群 {group_id} 失败: {e}")


def list_archives():
    """汇总所有档案：类型、显示名、历史/记忆条数、最后活跃时间。"""
    try:
        conn = _db()
        try:
            profiles = conn.execute("SELECT user_id, data FROM user_profile").fetchall()
            hist = {r[0]: (r[1], r[2]) for r in conn.execute(
                "SELECT user_id, COUNT(*), MAX(ts) FROM chat_history GROUP BY user_id"
            ).fetchall()}
            vecs = {r[0]: (r[1], r[2]) for r in conn.execute(
                "SELECT user_id, COUNT(*), MAX(ts) FROM memory_meta GROUP BY user_id"
            ).fetchall()}
        finally:
            conn.close()

        out = []
        for user_id, raw in profiles:
            try:
                data = json.loads(raw) if raw else {}
            except Exception:
                data = {}
            kind, group_id, qq = _archive_type(user_id)
            h_count, h_ts = hist.get(user_id, (0, 0))
            v_count, v_ts = vecs.get(user_id, (0, 0))
            out.append({
                "user_id": user_id,
                "type": kind,
                "group_id": group_id,
                "qq": qq,
                "display_name": data.get("display_name", ""),
                "mode": data.get("mode"),
                "fitness": data.get("fitness"),
                "favorability": data.get("favorability"),
                "history_count": h_count,
                "vector_count": v_count,
                "last_active": max(h_ts or 0, v_ts or 0),
            })
        out.sort(key=lambda x: x["last_active"], reverse=True)
        return out
    except Exception as e:
        log(f"[档案] 读取汇总失败: {e}")
        return []


def sync_archive_index(force=False, min_interval=30):
    """把档案汇总写入 data/archives.json（默认30秒内最多写一次）。"""
    global _archive_sync_at
    now = time.time()
    if not force and now - _archive_sync_at < min_interval:
        return None
    _archive_sync_at = now
    payload = {
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "count": 0,
        "archives": list_archives(),
    }
    payload["count"] = len(payload["archives"])
    tmp = ARCHIVE_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, ARCHIVE_FILE)
        return payload
    except Exception as e:
        log(f"[档案] 写入汇总失败: {e}")
        return None


def delete_archive(user_id):
    """彻底删除一个档案（人设、历史、向量、发散覆盖）。"""
    uid = str(user_id)
    try:
        conn = _db()
        try:
            rows = conn.execute(
                "SELECT doc_id FROM memory_meta WHERE user_id=?", (uid,)
            ).fetchall()
            conn.execute("DELETE FROM user_profile WHERE user_id=?", (uid,))
            conn.execute("DELETE FROM chat_history WHERE user_id=?", (uid,))
            conn.execute("DELETE FROM memory_meta WHERE user_id=?", (uid,))
            conn.commit()
        finally:
            conn.close()
        ids = [r[0] for r in rows]
        if ids:
            _get_collection().delete(ids=ids)
        settings = load_settings()
        overrides = settings.get("musing_overrides") or {}
        if uid in overrides:
            overrides.pop(uid)
            settings["musing_overrides"] = overrides
            save_settings(settings)
        sync_archive_index(force=True)
        log(f"[档案] 已删除 {uid}（历史/向量/人设）")
        return True
    except Exception as e:
        log(f"[档案] 删除 {uid} 失败: {e}")
        return False


# ======== 发散调度 ========
# 计算某个档案最终生效的发散策略：(是否开启, 间隔分钟)
def musing_policy(settings, scope):
    scope = str(scope)
    is_group = scope.startswith("group_")
    enabled = bool(settings.get(
        "musing_group_enabled" if is_group else "musing_private_enabled", True
    ))
    try:
        interval = float(settings.get("musing_interval_minutes", 120))
    except (TypeError, ValueError):
        interval = 120.0

    override = (settings.get("musing_overrides") or {}).get(scope) or {}
    if "enabled" in override:
        enabled = bool(override["enabled"])
    if override.get("interval_minutes") is not None:
        try:
            interval = float(override["interval_minutes"])
        except (TypeError, ValueError):
            pass

    interval = max(0.0, min(360.0, interval))
    if interval <= 0:
        enabled = False
    return enabled, interval


# 所有有记忆的档案 + 最近一次发散时间
def list_musing_scopes():
    try:
        conn = _db()
        try:
            rows = conn.execute("""
                SELECT u.user_id, MAX(m.ts)
                FROM (SELECT DISTINCT user_id FROM memory_meta WHERE type != 'musing') u
                LEFT JOIN memory_meta m
                    ON m.user_id = u.user_id AND m.type = 'musing'
                GROUP BY u.user_id
            """).fetchall()
        finally:
            conn.close()
        return [{"scope": r[0], "last_musing": float(r[1] or 0)} for r in rows]
    except Exception as e:
        log(f"[发散] 读取档案列表失败: {e}")
        return []


# ======== WebUI 查询接口 ========
# 列出某个档案的向量记忆（按时间倒序）
def list_memories(user_id, limit=50):
    try:
        conn = _db()
        try:
            rows = conn.execute(
                "SELECT doc_id, type, valence, arousal, importance, ts "
                "FROM memory_meta WHERE user_id = ? ORDER BY ts DESC LIMIT ?",
                (str(user_id), int(limit))
            ).fetchall()
        finally:
            conn.close()
        if not rows:
            return []
        got = _get_collection().get(ids=[r[0] for r in rows],
                                    include=["documents", "metadatas"])
        doc_map = dict(zip(got.get("ids", []), got.get("documents", [])))
        meta_map = dict(zip(got.get("ids", []), got.get("metadatas", [])))
        out = []
        for doc_id, mtype, valence, arousal, importance, ts in rows:
            if doc_id not in doc_map:
                continue
            meta = meta_map.get(doc_id) or {}
            out.append({
                "id": doc_id,
                "content": doc_map[doc_id],
                "type": mtype,
                "valence": valence,
                "arousal": arousal,
                "importance": importance,
                "confidence": meta.get("confidence", 1.0),
                "source_id": meta.get("source_id", ""),
                "ts": ts,
            })
        return out
    except Exception as e:
        log(f"[WebUI] 读取记忆列表失败 {user_id}: {e}")
        return []


# 语义检索（带相似度分数），WebUI 用
def search_vectors(scopes, query, top_k=5):
    if not query or not query.strip():
        return []
    try:
        vector = _embed([query])[0]
        result = _get_collection().query(
            query_embeddings=[vector],
            n_results=max(top_k * 2, top_k),
            where={"user_id": {"$in": [str(s) for s in scopes]}},
            include=["documents", "metadatas", "distances"]
        )
        ids = result.get("ids", [[]])[0]
        docs = result.get("documents", [[]])[0]
        metas = result.get("metadatas", [[]])[0]
        dists = result.get("distances", [[]])[0]
        if not ids:
            return []
        conn = _db()
        try:
            marks = ",".join("?" * len(scopes))
            rows = conn.execute(
                f"SELECT doc_id, importance FROM memory_meta WHERE user_id IN ({marks})",
                [str(s) for s in scopes]
            ).fetchall()
        finally:
            conn.close()
        imp_map = {r[0]: r[1] for r in rows}
        items = []
        for doc_id, doc, meta, dist in zip(ids, docs, metas, dists):
            meta = meta or {}
            items.append({
                "id": doc_id,
                "content": doc,
                "type": meta.get("type", "dialogue"),
                "score": round(1.0 - float(dist), 4),
                "importance": imp_map.get(doc_id, 0.5),
                "confidence": meta.get("confidence", 1.0),
                "source_id": meta.get("source_id", ""),
            })
        items.sort(key=lambda x: x["score"], reverse=True)
        return items[:top_k]
    except Exception as e:
        log(f"[WebUI] 语义检索失败 {scopes}: {e}")
        return []


# WebUI 用：带时间戳的热层列表（编辑/删除靠 ts 定位）
def list_hot_history(user_id, limit=None):
    try:
        conn = _db()
        try:
            rows = conn.execute(
                "SELECT ts, role, content FROM chat_history WHERE user_id=? ORDER BY ts DESC LIMIT ?",
                (str(user_id), int(limit) if limit else HOT_LIMIT)
            ).fetchall()
        finally:
            conn.close()
        return [{"ts": r[0], "role": r[1], "content": r[2]} for r in reversed(rows)]
    except Exception as e:
        log(f"[WebUI] 读取热层失败 {user_id}: {e}")
        return []


def update_hot_entry(user_id, ts, content):
    try:
        conn = _db()
        try:
            cur = conn.execute(
                "UPDATE chat_history SET content=? WHERE user_id=? AND ts=?",
                (content, str(user_id), float(ts))
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()
    except Exception as e:
        log(f"[WebUI] 修改热层失败 {user_id}: {e}")
        return False


def delete_hot_entry(user_id, ts):
    try:
        conn = _db()
        try:
            cur = conn.execute(
                "DELETE FROM chat_history WHERE user_id=? AND ts=?",
                (str(user_id), float(ts))
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()
    except Exception as e:
        log(f"[WebUI] 删除热层失败 {user_id}: {e}")
        return False


# 改内容会重新 embedding；只改重要度则不动向量
def update_vector(doc_id, content=None, importance=None):
    try:
        got = _get_collection().get(ids=[doc_id], include=["documents", "metadatas"])
        if not got.get("ids"):
            return False
        kwargs = {"ids": [doc_id]}
        if content is not None and content.strip():
            new_content = content.strip()
            kwargs["documents"] = [new_content]
            kwargs["embeddings"] = [_embed([new_content])[0]]
        if importance is not None:
            meta = dict((got.get("metadatas") or [{}])[0] or {})
            meta["importance"] = float(importance)
            kwargs["metadatas"] = [meta]
        if len(kwargs) > 1:
            _get_collection().update(**kwargs)
        if importance is not None:
            conn = _db()
            try:
                conn.execute("UPDATE memory_meta SET importance=? WHERE doc_id=?",
                             (float(importance), doc_id))
                conn.commit()
            finally:
                conn.close()
        log(f"[向量] 已更新记忆 {doc_id}")
        return True
    except Exception as e:
        log(f"[向量] 更新失败 {doc_id}: {e}")
        return False


def delete_vector(doc_id):
    try:
        _get_collection().delete(ids=[doc_id])
        conn = _db()
        try:
            conn.execute("DELETE FROM memory_meta WHERE doc_id=?", (doc_id,))
            conn.commit()
        finally:
            conn.close()
        log(f"[向量] 已删除记忆 {doc_id}")
        return True
    except Exception as e:
        log(f"[向量] 删除失败 {doc_id}: {e}")
        return False


# ======== 机器人设置（JSON 配置保留不动） ========
SETTINGS_FILE = os.environ.get("QQ_BOT_SETTINGS_FILE", os.path.join(BASE_DIR, "bot_settings.json"))

DEFAULT_SETTINGS = {
    "at_only": True,
    "reply_cooldown": 10,
    "reply_cooldown_at": 5,
    "infer_mode": "AI",
    "dive_probability": 10,
    # 全局回复模式（扮演/聊天）；档案自己的 mode（/mdchange）优先于它
    "default_mode": "扮演",
    # 发散：全局默认频率（分钟，0=关闭）
    "musing_interval_minutes": 120,
    "musing_private_enabled": True,
    "musing_group_enabled": True,
    # 发散素材的重要度门槛：低于此值的记忆不会被回忆（群聊闲聊普遍低分，可调低）
    "musing_min_importance": 0.7,
    # 进提示词的热上下文条数
    "hot_context_private": 30,
    "hot_context_group": 12,
    # RAG 召回相似度阈值（cosine，0-1，越高越挑剔；低于此值的"回忆"不注入）
    "recall_threshold": 0.55,
    # 识图：总开关 + 仅管理员识别 + 识别概率（%，普通图与表情包分开掷骰，省钱用）
    "vision_enabled": True,
    "vision_admin_only": False,
    "vision_probability": 100,
    "vision_meme_probability": 30,
    # 联网搜索：总开关 + 搜索冷却（秒，按群/私聊独立计；冷却内降级为无资料回复）
    # + Tavily API Key（LLM 友好搜索源，tavily.com 免费额度；留空走 Jina/必应免 Key 链）
    # + 显式指令开关（关闭后"搜一下/帮我查"等本地指令不再触发，仅 AI 判回提取搜索词）
    "search_enabled": True,
    "search_cooldown": 600,
    "tavily_api_key": "",
    "search_explicit_cmd_enabled": True,
    # 随机回复延迟（秒）：拟人+防风控，max=0 则关闭延迟
    "reply_delay_min": 1.0,
    "reply_delay_max": 3.0,
    # 引用回复：目标消息距今超过此条数就引用发送（0=总是引用）
    "quote_threshold": 3,
    # 单档案覆盖：{"QQ号": {"enabled": true, "interval_minutes": 30}, "group_xxx": {...}}
    "musing_overrides": {},
    # 表情包决策与附带设置
    "sticker_enabled": True,
    "sticker_mode": "AI",
    "sticker_probability": 25.0,
    "sticker_cooldown": 20.0,
    "sticker_duel_mode": False,
    "sticker_capture_only": False,
    "sticker_capture_enabled": True,
    "sticker_capture_admin_only": True,
    "sticker_capture_probability": 30.0,
}

def load_settings():
    if not os.path.exists(SETTINGS_FILE):
        return dict(DEFAULT_SETTINGS)
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        merged = dict(DEFAULT_SETTINGS)
        merged.update(data)
        return merged
    except Exception as e:
        print(f"[设置] 读取设置失败: {e}")
        return dict(DEFAULT_SETTINGS)

def save_settings(settings):
    tmp = SETTINGS_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(settings, f, ensure_ascii=False, indent=4)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, SETTINGS_FILE)
    except Exception as e:
        print(f"[设置] 保存设置失败: {e}")
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass

# ====== /atswitch ======
def toggle_at_only():
    settings = load_settings()
    settings["at_only"] = not settings.get("at_only", True)
    save_settings(settings)
    print(f"[设置] 群聊触发方式已切换为：{'仅@' if settings['at_only'] else '@或喊名字'}")
    return settings["at_only"]

# ====== /cd set 和 /atcd set ======
def set_reply_cooldown(seconds, key="reply_cooldown"):
    settings = load_settings()
    settings[key] = seconds
    save_settings(settings)
    print(f"[设置] {key} 已设置为 {seconds} 秒")
    return seconds

# ====== /faver set ======
def set_favorability(user_id, value):
    memory = load_user_memory(user_id)
    memory["favorability"] = max(-100, min(100, int(value)))
    save_user_memory(user_id, memory)
    print(f"[好感] 已设置 {user_id} 的好感度为 {memory['favorability']}")
    return memory["favorability"]

# ====== /name set ======
def set_name_keywords(names):
    settings = load_settings()
    settings["name_keywords"] = names
    save_settings(settings)
    print(f"[设置] 喊名字关键词已设置为：{'、'.join(names)}")
    return names

# ====== /infer set ======
def set_infer_mode(mode):
    settings = load_settings()
    settings["infer_mode"] = mode
    save_settings(settings)
    print(f"[设置] 潜水判断方式已切换为：{mode}")
    return mode

# ====== /pro set ======
def set_dive_probability(probability):
    settings = load_settings()
    settings["dive_probability"] = probability
    save_settings(settings)
    print(f"[设置] 潜水插嘴概率已设置为 {probability:g}%")
    return probability


# ====== 表情包设置 ======
def set_sticker_setting(key, value):
    settings = load_settings()
    settings[key] = value
    save_settings(settings)
    return settings


# ======== 档案读写（SQLite） ========
_DEFAULT_PROFILE = {
    "character_prompt": "",
    "user_persona": "",
    "fitness": None,
    "mode": None,
    "favorability": None,
    "state": {},
    "long_term": "",
    "summary_counter": 0,
}

def load_user_memory(user_id, hot_limit=None):
    user_id = str(user_id)
    conn = _db()
    try:
        row = conn.execute("SELECT data FROM user_profile WHERE user_id=?", (user_id,)).fetchone()
        if row:
            # 合并默认键：touch 建档的轻量档案只有 display_name，缺键会在后续 .strip() 处炸掉
            data = {**_DEFAULT_PROFILE, **json.loads(row[0])}
            data.setdefault("user_id", user_id)
        else:
            data = dict(_DEFAULT_PROFILE)
            data["user_id"] = user_id
            log(f"[记忆系统] 识别到新用户 {user_id}，已建档")

        # 热层：最近 hot_limit 轮（默认 HOT_LIMIT）
        rows = conn.execute(
            "SELECT role, content FROM chat_history WHERE user_id=? ORDER BY ts DESC LIMIT ?",
            (user_id, int(hot_limit) if hot_limit else HOT_LIMIT)
        ).fetchall()
        data["chat_history"] = [{"role": r, "content": c} for r, c in reversed(rows)]
        return data
    finally:
        conn.close()


def save_user_memory(user_id, memory_data):
    user_id = str(user_id)
    profile = {k: memory_data.get(k, v) for k, v in _DEFAULT_PROFILE.items()}
    profile["user_id"] = user_id
    # 档案元信息（display_name 等）不在默认键里，保存时必须保留，否则每次存档都会把昵称抹掉
    if memory_data.get("display_name"):
        profile["display_name"] = memory_data["display_name"]
    history = memory_data.get("chat_history", [])[-RETAIN_LIMIT:]

    conn = _db()
    try:
        if "display_name" not in profile:
            row = conn.execute("SELECT data FROM user_profile WHERE user_id=?", (user_id,)).fetchone()
            if row:
                try:
                    old = json.loads(row[0]) or {}
                except Exception:
                    old = {}
                if old.get("display_name"):
                    profile["display_name"] = old["display_name"]
        conn.execute(
            "INSERT INTO user_profile VALUES (?,?) ON CONFLICT(user_id) DO UPDATE SET data=excluded.data",
            (user_id, json.dumps(profile, ensure_ascii=False))
        )
        # 热层整体覆盖（按 角色+内容 对齐保留原 ts 和 message_id：
        # 之前每次存档都把全部时间戳刷成"刚刚"且丢掉 message_id，
        # 导致引用回复的"距今条数"恒等于全量历史、引用原文本地查询失效）
        old_rows = conn.execute(
            "SELECT role, content, ts, message_id FROM chat_history WHERE user_id=? ORDER BY ts",
            (user_id,)
        ).fetchall()
        old_meta = {}
        for o_role, o_content, o_ts, o_mid in old_rows:
            old_meta.setdefault((o_role, o_content), []).append((o_ts, o_mid))
        conn.execute("DELETE FROM chat_history WHERE user_id=?", (user_id,))
        now = time.time()
        rows_to_insert = []
        for i, m in enumerate(history):
            role = m.get("role", "user")
            content = m.get("content", "")
            matched = old_meta.get((role, content))
            if matched:
                ts, mid = matched.pop(0)
            else:
                ts, mid = now + i * 0.001, None
            rows_to_insert.append((user_id, ts, role, content, mid))
        conn.executemany(
            "INSERT INTO chat_history(user_id, ts, role, content, message_id) VALUES (?,?,?,?,?)",
            rows_to_insert
        )
        conn.commit()
    finally:
        conn.close()
    sync_archive_index()


# ======== 群共享上下文 ========
# 全群共用一个 group_{群号} 档案，每条消息带昵称前缀
def count_group_history_after(group_id, message_id):
    """群档案里目标消息之后又来了几条（引用回复的"距今条数"判断用）。
    按入库顺序（rowid）数，不受存档重写时间戳影响；查不到目标按已被刷上去处理。"""
    if not message_id:
        return 999
    try:
        conn = _db()
        try:
            row = conn.execute(
                "SELECT rowid FROM chat_history WHERE user_id=? AND message_id=? "
                "ORDER BY rowid DESC LIMIT 1",
                (f"group_{group_id}", str(message_id))
            ).fetchone()
            if not row:
                return 999
            row2 = conn.execute(
                "SELECT COUNT(*) FROM chat_history WHERE user_id=? AND rowid>?",
                (f"group_{group_id}", row[0])
            ).fetchone()
            return row2[0] if row2 else 0
        finally:
            conn.close()
    except Exception:
        return 999


def get_message_content(scope, message_id):
    """按消息 id 查历史原文（引用回复用），查不到返回空字符串。"""
    if not message_id:
        return ""
    try:
        conn = _db()
        try:
            row = conn.execute(
                "SELECT content FROM chat_history WHERE user_id=? AND message_id=? "
                "ORDER BY ts DESC LIMIT 1",
                (str(scope), str(message_id))
            ).fetchone()
            return (row[0] or "") if row else ""
        finally:
            conn.close()
    except Exception:
        return ""


def append_group_history(group_id, role, content, limit=RETAIN_LIMIT, message_id=None):
    mem_key = f"group_{group_id}"
    memory = load_user_memory(mem_key)
    history = memory.get("chat_history", [])
    history.append({"role": role, "content": content})
    # 群档案热层放宽到 limit，个人档案仍 HOT_LIMIT
    conn = _db()
    try:
        now = time.time()
        conn.execute("INSERT INTO chat_history(user_id, ts, role, content, message_id) VALUES (?,?,?,?,?)",
                     (mem_key, now, role, content, str(message_id) if message_id else None))
        # 裁剪到 limit 条
        conn.execute("""
            DELETE FROM chat_history WHERE user_id=? AND ts NOT IN (
                SELECT ts FROM chat_history WHERE user_id=? ORDER BY ts DESC LIMIT ?
            )
        """, (mem_key, mem_key, limit))
        conn.commit()
    finally:
        conn.close()
    sync_archive_index()


# ====== /char set 与 /char clear ======
def set_user_character(user_id, character_prompt):
    memory = load_user_memory(user_id)
    memory["character_prompt"] = character_prompt
    save_user_memory(user_id, memory)
    print(f"[人格] 已为用户 {user_id} 设置专属人格")

def clear_user_character(user_id):
    memory = load_user_memory(user_id)
    memory["character_prompt"] = ""
    save_user_memory(user_id, memory)
    print(f"[人格] 已清除用户 {user_id} 的专属人格")

# ====== /charfit set ======
def set_user_fitness(user_id, fitness):
    memory = load_user_memory(user_id)
    memory["fitness"] = fitness
    save_user_memory(user_id, memory)
    print(f"[人格] 已设置用户 {user_id} 的人设拟合度为 {fitness}")

# ====== /mdchange ======
def toggle_user_mode(user_id):
    memory = load_user_memory(user_id)
    mode = memory.get("mode")
    if mode == "聊天":
        mode = "扮演"
    else:
        mode = "聊天"

        # 切到聊天模式时清洗历史里的动作和心声，防止模型模仿旧格式
        for msg in memory.get("chat_history", []):
            if msg.get("role") == "assistant" and isinstance(msg.get("content"), str):
                cleaned = re.sub(r"[（(][^（）()]*[）)]", "", msg["content"])
                cleaned = re.sub(r"\*[^*]*\*", "", cleaned)
                msg["content"] = cleaned.strip()

    memory["mode"] = mode
    save_user_memory(user_id, memory)
    print(f"[模式] 已将用户 {user_id} 的回复模式切换为 {mode}模式")
    return mode

# ====== /memory reset ======
# 只清记忆（对话/状态/好感度），保留人设和参数
def clear_user_memory(user_id):
    memory = load_user_memory(user_id)
    memory["chat_history"] = []
    memory["state"] = {}
    memory["long_term"] = ""
    memory["summary_counter"] = 0
    memory["favorability"] = None
    save_user_memory(user_id, memory)

    # 向量记忆也清掉
    try:
        conn = _db()
        try:
            rows = conn.execute("SELECT doc_id FROM memory_meta WHERE user_id=?", (str(user_id),)).fetchall()
            conn.execute("DELETE FROM memory_meta WHERE user_id=?", (str(user_id),))
            conn.commit()
        finally:
            conn.close()
        ids = [r[0] for r in rows]
        if ids:
            _get_collection().delete(ids=ids)
    except Exception as e:
        log(f"[向量] 清空 {user_id} 的向量记忆失败: {e}")
    print(f"[记忆] 已清空用户 {user_id} 的记忆（保留人设与参数）")

# ====== /user set 与 /user clear ======
def set_user_persona(user_id, persona):
    memory = load_user_memory(user_id)
    memory["user_persona"] = persona
    save_user_memory(user_id, memory)
    print(f"[档案] 已为用户 {user_id} 设置用户人设")

def clear_user_persona(user_id):
    memory = load_user_memory(user_id)
    memory["user_persona"] = ""
    save_user_memory(user_id, memory)
    print(f"[档案] 已清除用户 {user_id} 的用户人设")

# ====== /rl ======
# 弹出最后一轮对话（用户+回复），返回用户消息用于重新生成
def pop_last_turn(user_id):
    memory = load_user_memory(user_id)
    history = memory.get("chat_history", [])
    if not history:
        return None
    last_msg = None
    if history and history[-1].get("role") == "assistant":
        history.pop()
    if history and history[-1].get("role") == "user":
        last_msg = history.pop().get("content")
    memory["chat_history"] = history
    save_user_memory(user_id, memory)
    return last_msg


# ====== 旧JSON档案迁移 ======
def migrate_from_json():
    if not os.path.isdir(USER_LOG_DIR):
        return
    files = [f for f in os.listdir(USER_LOG_DIR) if f.endswith(".json")]
    if not files:
        return
    log(f"[迁移] 发现 {len(files)} 份旧档案，开始迁移...")
    for fname in files:
        user_id = fname[:-5]
        try:
            with open(os.path.join(USER_LOG_DIR, fname), "r", encoding="utf-8") as f:
                old = json.load(f)
            memory = load_user_memory(user_id)
            for key in _DEFAULT_PROFILE:
                if old.get(key) not in (None, "", {}, []):
                    memory[key] = old[key]
            memory["chat_history"] = old.get("chat_history", [])[-HOT_LIMIT:]
            save_user_memory(user_id, memory)

            # 长期记忆灌入向量库
            long_term = str(old.get("long_term", "")).strip()
            if long_term:
                add_vector(user_id, f"长期记忆：{long_term}", type="memory", importance=0.8)
        except Exception as e:
            log(f"[迁移] {user_id} 迁移失败: {e}")
    # 迁移完成后把旧目录改名留档
    try:
        os.rename(USER_LOG_DIR, USER_LOG_DIR + "_已迁移")
        log(f"[迁移] 完成，旧档案已留档为 user_log_已迁移")
    except Exception as e:
        log(f"[迁移] 完成，但旧目录改名失败（不影响使用）: {e}")
