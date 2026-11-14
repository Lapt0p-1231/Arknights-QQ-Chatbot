import json
import os
import re
import sqlite3
import threading
import time
import uuid
from collections import OrderedDict
from contextlib import contextmanager

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
        CREATE TABLE IF NOT EXISTS memory_epoch (
            user_id TEXT PRIMARY KEY,
            epoch INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS musing_used (
            doc_id TEXT PRIMARY KEY,
            user_id TEXT,
            used_at REAL
        );
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


@contextmanager
def _transaction():
    """显式写事务，跨进程串行化读改写（WebUI 与机器人共用同一个 DB）。"""
    conn = _db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_epoch(user_id):
    conn = _db()
    try:
        row = conn.execute(
            "SELECT epoch FROM memory_epoch WHERE user_id=?", (str(user_id),)
        ).fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


def bump_epoch(conn, user_id):
    """在已开启的事务里把某个档案的纪元 +1。"""
    conn.execute(
        "INSERT INTO memory_epoch(user_id, epoch) VALUES (?, 1) "
        "ON CONFLICT(user_id) DO UPDATE SET epoch = epoch + 1",
        (str(user_id),)
    )
    row = conn.execute(
        "SELECT epoch FROM memory_epoch WHERE user_id=?", (str(user_id),)
    ).fetchone()
    return int(row[0]) if row else 1


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
                f"SELECT doc_id, importance, type, ts FROM memory_meta WHERE user_id IN ({marks})",
                [str(s) for s in scopes]
            ).fetchall()
        finally:
            conn.close()
        # chroma 的 ids 与 documents 对齐
        ids = result.get("ids", [[]])[0]
        meta_map = {r[0]: (r[1], r[2], r[3]) for r in rows}
        now = time.time()
        items = []
        for doc_id, doc in zip(ids, docs):
            imp, mtype, ts = meta_map.get(doc_id, (0.5, "dialogue", 0.0))
            sim = sims.get(doc_id, 0.0)
            if imp < min_importance:
                continue
            if sim < min_similarity:
                continue
            # 时间新鲜度衰减：30 天内接近 1，越久远越趋近 0
            age_days = max(0.0, (now - float(ts or 0)) / 86400.0)
            recency = 1.0 / (1.0 + age_days / 30.0)
            score = sim * 0.65 + imp * 0.25 + recency * 0.10
            items.append((imp, sim, mtype, doc, score))
        # 相关性优先：相似度为主、重要度与时间新鲜度加权微调
        # 发散产物最多召回1条（梦境不能挤掉事实）
        items.sort(key=lambda x: (x[2] == "musing", -x[4]))
        kept, musing_seen = [], 0
        for item in items:
            if item[2] == "musing":
                musing_seen += 1
                if musing_seen > 1:
                    continue
            kept.append(item)
        return [(mtype, doc) for _, _, mtype, doc, _ in kept[:top_k]]
    except Exception as e:
        log(f"[向量] 召回失败 {scopes}: {e}")
        return []


# 胡思乱想用：取高重要度记忆
def get_high_importance_memories(limit=3, min_importance=0.7, user_id=None, before_ts=None):
    try:
        conn = _db()
        try:
            params = [min_importance]
            sql = (
                "SELECT doc_id, user_id FROM memory_meta "
                "WHERE importance >= ? AND type != 'musing'"
            )
            if user_id is not None:
                sql += " AND user_id = ?"
                params.append(str(user_id))
            if before_ts is not None:
                sql += " AND ts <= ?"
                params.append(before_ts)
            sql += " ORDER BY importance DESC, ts DESC LIMIT ?"
            params.append(limit * 5)
            rows = conn.execute(sql, tuple(params)).fetchall()
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


# ======== 胡思乱想回放（主动搭讪复用已有 musing，避免临时再生成） ========
def get_recent_musings(user_id, limit=5, max_age_seconds=None, exclude_used=True):
    uid = str(user_id)
    conn = _db()
    try:
        sql = (
            "SELECT m.doc_id, m.ts FROM memory_meta m "
            "WHERE m.user_id=? AND m.type='musing'"
        )
        params = [uid]
        if max_age_seconds:
            sql += " AND m.ts >= ?"
            params.append(time.time() - float(max_age_seconds))
        if exclude_used:
            sql += " AND m.doc_id NOT IN (SELECT doc_id FROM musing_used)"
        sql += " ORDER BY m.ts DESC LIMIT ?"
        params.append(int(limit))
        rows = conn.execute(sql, tuple(params)).fetchall()
    finally:
        conn.close()
    if not rows:
        return []
    try:
        result = _get_collection().get(ids=[r[0] for r in rows], include=["documents"])
        doc_map = dict(zip(result.get("ids", []), result.get("documents", [])))
        return [
            {"doc_id": r[0], "ts": float(r[1]), "content": doc_map[r[0]]}
            for r in rows if doc_map.get(r[0])
        ]
    except Exception as e:
        log(f"[向量] 读取胡思乱想失败: {e}")
        return []


def mark_musing_used(doc_id, user_id):
    if not doc_id:
        return
    with _transaction() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO musing_used(doc_id, user_id, used_at) VALUES (?,?,?)",
            (str(doc_id), str(user_id), time.time())
        )


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
                "at_only": data.get("at_only"),
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
        with _transaction() as conn:
            bump_epoch(conn, uid)
            rows = conn.execute(
                "SELECT doc_id FROM memory_meta WHERE user_id=?", (uid,)
            ).fetchall()
            conn.execute("DELETE FROM user_profile WHERE user_id=?", (uid,))
            conn.execute("DELETE FROM chat_history WHERE user_id=?", (uid,))
            conn.execute("DELETE FROM memory_meta WHERE user_id=?", (uid,))
            conn.execute("DELETE FROM musing_used WHERE user_id=?", (uid,))
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
    # 回复字数上限（提示词 04_字数要求 的 {max_words} 占位符）
    "max_words": 20,
    # 向量入库重要度门槛：低于此值的对话切片不写入 ChromaDB
    "vector_min_importance": 0.35,
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
    # 表情包自动打标：默认关闭，是否调用由用户在 WebUI 决定；使用模型见 api.json 的 sticker_tag 路由
    "sticker_autotag_enabled": False,
    # 每轮打标张数（打标任务批量处理，积压时明显更快；1 张 ≈ 1 次视觉调用）
    "sticker_autotag_batch": 3,
    # 机器人防刷软保底：已知其他机器人 QQ 列表 + 连续互动概率步长衰减（从 100% 开始，每轮减去 step%）
    "known_bots": [],
    "bot_decay_step": 25.0,
    # 主动私聊搭讪系统（双轨：记忆发散睹物思人 + 时态日常碎碎念）
    "proactive_enabled": False,
    "proactive_target": "",
    "proactive_interval_minutes": 180.0,
    "proactive_probability": 50.0,
    "proactive_quiet_start": 23,
    "proactive_quiet_end": 8,
    "proactive_group_idle_minutes": 30.0,
    # 主动消息类型倾向：0=全类型B，50=各半，100=全类型A
    "proactive_type_a_ratio": 50.0,
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
    print(f"[设置] 群聊触发方式已切换为：{'仅提及（@/喊名字）' if settings['at_only'] else '自动插嘴 + 提及'}")
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
    "at_only": None,
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
    """只保存档案字段（人设/状态/好感度等），不再写 chat_history。

    历史统一走 append_history / replace_history / clear_user_memory，
    避免旧快照整段覆盖新消息。
    """
    user_id = str(user_id)
    profile = {k: memory_data.get(k, v) for k, v in _DEFAULT_PROFILE.items()}
    profile["user_id"] = user_id
    # 档案元信息（display_name 等）不在默认键里，保存时必须保留，否则每次存档都会把昵称抹掉
    if memory_data.get("display_name"):
        profile["display_name"] = memory_data["display_name"]

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
        conn.commit()
    finally:
        conn.close()


def update_profile(user_id, patch, expected_epoch=None):
    """事务内读改写档案字段；epoch 不一致时放弃（防止写回已清空的档案）。"""
    user_id = str(user_id)
    with _transaction() as conn:
        if expected_epoch is not None:
            row = conn.execute("SELECT epoch FROM memory_epoch WHERE user_id=?", (user_id,)).fetchone()
            cur = int(row[0]) if row else 0
            if cur != expected_epoch:
                return False
        row = conn.execute("SELECT data FROM user_profile WHERE user_id=?", (user_id,)).fetchone()
        try:
            data = json.loads(row[0]) if row else {}
        except Exception:
            data = {}
        data = {**_DEFAULT_PROFILE, **data}
        data["user_id"] = user_id
        data.update(patch or {})
        conn.execute(
            "INSERT INTO user_profile VALUES (?,?) ON CONFLICT(user_id) DO UPDATE SET data=excluded.data",
            (user_id, json.dumps(data, ensure_ascii=False))
        )
    return True


def append_history(user_id, role, content, message_id=None, expected_epoch=None,
                   retain_limit=RETAIN_LIMIT):
    """追加一条历史（事务内检查 epoch），返回是否写入成功。"""
    user_id = str(user_id)
    content = str(content or "")
    with _transaction() as conn:
        if expected_epoch is not None:
            row = conn.execute("SELECT epoch FROM memory_epoch WHERE user_id=?", (user_id,)).fetchone()
            cur = int(row[0]) if row else 0
            if cur != expected_epoch:
                return False
        now = time.time()
        conn.execute(
            "INSERT INTO chat_history(user_id, ts, role, content, message_id) VALUES (?,?,?,?,?)",
            (user_id, now, role, content, str(message_id) if message_id else None)
        )
        conn.execute("""
            DELETE FROM chat_history WHERE user_id=? AND rowid NOT IN (
                SELECT rowid FROM chat_history WHERE user_id=? ORDER BY ts DESC, rowid DESC LIMIT ?
            )
        """, (user_id, user_id, int(retain_limit)))
    return True


def replace_history(user_id, history):
    """仅迁移/特殊场景使用：整体替换历史。"""
    user_id = str(user_id)
    with _transaction() as conn:
        conn.execute("DELETE FROM chat_history WHERE user_id=?", (user_id,))
        now = time.time()
        rows = []
        for i, m in enumerate((history or [])[-RETAIN_LIMIT:]):
            if not isinstance(m, dict):
                continue
            rows.append((
                user_id, now + i * 0.001, m.get("role", "user"),
                m.get("content", ""), None
            ))
        if rows:
            conn.executemany(
                "INSERT INTO chat_history(user_id, ts, role, content, message_id) VALUES (?,?,?,?,?)",
                rows
            )
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


def append_group_history(group_id, role, content, limit=RETAIN_LIMIT, message_id=None,
                         expected_epoch=None):
    # 群档案热层放宽到 limit，个人档案仍 HOT_LIMIT
    return append_history(
        f"group_{group_id}", role, content,
        message_id=message_id, expected_epoch=expected_epoch, retain_limit=limit
    )


def get_last_history_info(user_id):
    """返回该档案最新一条历史消息的 (role, ts)；无记录返回 (None, 0.0)。"""
    conn = _db()
    try:
        row = conn.execute(
            "SELECT role, ts FROM chat_history WHERE user_id=? ORDER BY ts DESC LIMIT 1",
            (str(user_id),)
        ).fetchone()
        if row:
            return row[0], float(row[1])
        return None, 0.0
    finally:
        conn.close()


def get_last_history_ts(user_id, role=None):
    """返回该档案最新一条指定角色消息的时间戳；role=None 表示不限角色。"""
    conn = _db()
    try:
        if role is None:
            row = conn.execute(
                "SELECT ts FROM chat_history WHERE user_id=? ORDER BY ts DESC LIMIT 1",
                (str(user_id),)
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT ts FROM chat_history WHERE user_id=? AND role=? ORDER BY ts DESC LIMIT 1",
                (str(user_id), str(role))
            ).fetchone()
        return float(row[0]) if row else 0.0
    finally:
        conn.close()


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

    memory["mode"] = mode
    save_user_memory(user_id, memory)
    print(f"[模式] 已将用户 {user_id} 的回复模式切换为 {mode}模式")
    return mode


def set_user_mode(user_id, mode):
    """设置指定档案的回复模式；mode 为 None/空 表示跟随全局默认。"""
    memory = load_user_memory(user_id)
    if mode in ("扮演", "聊天"):
        memory["mode"] = mode
    else:
        memory["mode"] = None
    save_user_memory(user_id, memory)
    print(f"[模式] 档案 {user_id} 回复模式设为 {memory['mode'] or '跟随全局'}")
    return memory["mode"]


def set_archive_at_only(user_id, at_only):
    """设置指定群档案的触发方式；at_only 为 None 表示跟随全局。"""
    memory = load_user_memory(user_id)
    memory["at_only"] = None if at_only is None else bool(at_only)
    save_user_memory(user_id, memory)
    label = "跟随全局" if memory["at_only"] is None else ("仅提及" if memory["at_only"] else "自主互动")
    print(f"[触发] 档案 {user_id} 触发方式设为 {label}")
    return memory["at_only"]


def effective_at_only(settings, scope):
    """取某档案的有效触发方式：档案覆盖优先，否则用全局默认。"""
    try:
        override = load_user_memory(scope).get("at_only")
    except Exception:
        override = None
    return bool(settings.get("at_only", True)) if override is None else bool(override)

# ====== /memory reset ======
# 只清记忆（对话/状态/好感度），保留人设和参数
def clear_user_memory(user_id):
    uid = str(user_id)
    ids = []
    with _transaction() as conn:
        # 先递增纪元：在途任务提交时会发现 epoch 不一致并放弃，避免写回刚清空的记忆
        bump_epoch(conn, uid)
        rows = conn.execute(
            "SELECT doc_id FROM memory_meta WHERE user_id=?", (uid,)
        ).fetchall()
        ids = [r[0] for r in rows]
        conn.execute("DELETE FROM memory_meta WHERE user_id=?", (uid,))
        conn.execute("DELETE FROM musing_used WHERE user_id=?", (uid,))
        conn.execute("DELETE FROM chat_history WHERE user_id=?", (uid,))

        row = conn.execute("SELECT data FROM user_profile WHERE user_id=?", (uid,)).fetchone()
        try:
            data = json.loads(row[0]) if row else {}
        except Exception:
            data = {}
        data = {**_DEFAULT_PROFILE, **data}
        data["user_id"] = uid
        data.pop("chat_history", None)
        data["state"] = {}
        data["long_term"] = ""
        data["summary_counter"] = 0
        data["favorability"] = None
        conn.execute(
            "INSERT INTO user_profile VALUES (?,?) ON CONFLICT(user_id) DO UPDATE SET data=excluded.data",
            (uid, json.dumps(data, ensure_ascii=False))
        )

    # 向量记忆也清掉（Chroma 不在 SQLite 事务里，放在后面）
    try:
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
# 弹出最后一轮对话（用户+回复），返回 (用户消息, message_id) 用于重新生成
def pop_last_turn(user_id):
    """弹出最后一轮对话（用户+回复），返回 (用户消息, message_id)。"""
    uid = str(user_id)
    with _transaction() as conn:
        rows = conn.execute(
            "SELECT rowid, role, content, message_id FROM chat_history WHERE user_id=? "
            "ORDER BY ts DESC, rowid DESC LIMIT 2",
            (uid,)
        ).fetchall()
        if not rows:
            return None
        idx = 0
        if rows[0][1] == "assistant":
            conn.execute("DELETE FROM chat_history WHERE rowid=?", (rows[0][0],))
            idx = 1
        if idx < len(rows) and rows[idx][1] == "user":
            conn.execute("DELETE FROM chat_history WHERE rowid=?", (rows[idx][0],))
            return rows[idx][2], rows[idx][3]
        return None


def delete_last_vector(user_id):
    """删除该档案最近 10 分钟内最新一条 dialogue 向量，防止 /rl 重生成残留废回复。"""
    uid = str(user_id)
    cutoff = time.time() - 600
    try:
        conn = _db()
        try:
            row = conn.execute(
                "SELECT doc_id FROM memory_meta "
                "WHERE user_id=? AND type='dialogue' AND ts>=? "
                "ORDER BY ts DESC LIMIT 1",
                (uid, cutoff)
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return False
        doc_id = row[0]
        try:
            _get_collection().delete(ids=[doc_id])
        except Exception as e:
            log(f"[向量] 删除 /rl 残留向量失败 {doc_id}: {e}")
        conn = _db()
        try:
            conn.execute("DELETE FROM memory_meta WHERE doc_id=?", (doc_id,))
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception as e:
        log(f"[向量] 查询 /rl 残留向量失败 {uid}: {e}")
        return False


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
            save_user_memory(user_id, memory)
            replace_history(user_id, old.get("chat_history", [])[-HOT_LIMIT:])

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
