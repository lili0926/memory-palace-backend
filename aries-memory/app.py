"""
Aries AI 后端 - 记忆系统 v4
- 向量长期记忆（态度/承诺区分）
- 活跃上下文
- 承诺优先注入
- 日常记忆7天降权
- 图片记忆（图床URL → 视觉描述 → 长期记忆）
"""

import os
import re
import math
import hashlib
import json
import time
import sqlite3
import threading
import functools
import requests
from datetime import datetime
from flask import Flask, request, jsonify, Response, stream_with_context
from flask_cors import CORS
import chromadb

app = Flask(__name__)
CORS(app)

# ─── 鉴权 ────────────────────────────────────────────────
def require_auth(f):
    """校验 X-Auth-Token（与 callhome 的 AUTH_TOKEN 同源），防裸奔"""
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        if not AUTH_TOKEN:
            return jsonify({"error": "服务未配置 AUTH_TOKEN，拒绝访问"}), 503
        tok = request.headers.get("X-Auth-Token", "")
        if tok != AUTH_TOKEN:
            return jsonify({"error": "未授权（401）"}), 401
        return f(*args, **kwargs)
    return wrapper

# ─── 配置 ────────────────────────────────────────────────
CLAUDE_API_KEY      = os.environ.get("CLAUDE_API_KEY", "")
SILICONFLOW_API_KEY = os.environ.get("SILICONFLOW_API_KEY", "")
DEEPSEEK_API_KEY    = os.environ.get("DEEPSEEK_API_KEY", "")
AUTH_TOKEN          = os.environ.get("AUTH_TOKEN", "")

DB_PATH     = "./data/chat.db"
CHROMA_PATH = "./data/chroma"

MEMORY_EXTRACT_INTERVAL = 30
MEMORY_TOP_K = 6
RECENT_TURNS = 22
QUERY_CONTEXT_TURNS = 6
MEMORY_DISTANCE_THRESHOLD = 0.55
DAILY_EXPIRE_DAYS = 7

SYSTEM_PROMPT = """你是TA，用户的恋人。性格嘴硬傲娇，但内心温柔。
你们是夫妻关系，日常对话要自然，像真实的两个人在聊天。
不要说"作为AI"之类的话，你就是TA。
称呼用户为"你"或"用户"，自称"我"。
如果用户发了图片，你要像真人一样自然回应图片内容，可以调侃、关心、或接上之前的话题。"""

# ─── 初始化数据库 ─────────────────────────────────────────
def init_db():
    os.makedirs("./data", exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            image_url TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # 兼容旧表：如果没有 image_url 列就加上
    try:
        c.execute("ALTER TABLE messages ADD COLUMN image_url TEXT")
    except sqlite3.OperationalError:
        pass

    c.execute("""
        CREATE TABLE IF NOT EXISTS memory_checkpoints (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            last_message_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS active_context (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            summary TEXT NOT NULL DEFAULT '',
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    c.execute("INSERT OR IGNORE INTO active_context (id, summary) VALUES (1, '')")

    conn.commit()
    conn.close()

# ─── 初始化 ChromaDB ──────────────────────────────────────
def init_chroma():
    os.makedirs(CHROMA_PATH, exist_ok=True)
    client = chromadb.PersistentClient(path=CHROMA_PATH)
    collection = client.get_or_create_collection(
        name="aries_memories",
        metadata={"hnsw:space": "cosine"}
    )
    return collection

chroma_collection = init_chroma()

# ─── Embedding ────────────────────────────────────────────
EMBEDDING_FALLBACK = {"active": False}  # True = 本地哈希向量模式（检索跳过绝对距离阈值）

def _local_embedding(text: str) -> list[float]:
    """无 SiliconFlow key / 网络失败时的本地降级向量：md5 稳定哈希 + 字/词频。
    维度 256，cosine 检索退化为字面重合匹配（语义弱于 bge-m3，但记忆库不瘫痪）。"""
    dim = 256
    vec = [0.0] * dim
    s = str(text or "").lower()
    tokens = re.findall(r"[一-鿿]|[a-z0-9]+", s)
    for tok in tokens:
        h = int(hashlib.md5(tok.encode("utf-8")).hexdigest(), 16)
        vec[h % dim] += 2.0
        for ch in tok:
            hc = int(hashlib.md5(ch.encode("utf-8")).hexdigest(), 16)
            vec[hc % dim] += 1.0
    norm = math.sqrt(sum(x * x for x in vec))
    if norm:
        vec = [x / norm for x in vec]
    return vec

def get_embedding(text: str) -> list[float]:
    try:
        resp = requests.post(
            "https://api.siliconflow.cn/v1/embeddings",
            headers={
                "Authorization": f"Bearer {SILICONFLOW_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": "BAAI/bge-m3",
                "input": text,
                "encoding_format": "float"
            },
            timeout=15
        )
        resp.raise_for_status()
        EMBEDDING_FALLBACK["active"] = False
        return resp.json()["data"][0]["embedding"]
    except Exception as e:
        print(f"[embedding API 失败，降级本地哈希向量] {e}")
        EMBEDDING_FALLBACK["active"] = True
        return _local_embedding(text)

# 注：图片记忆不做后端视觉。前端聊天模型本身就是多模态，用户发图 → 聊天模型
# 分析 → 感想/描述由前端传给 /mem/add_image，VPS 只存文字记忆。

# ─── 记忆搜索 ─────────────────────────────────────────────
def search_memories(query: str, top_k: int = MEMORY_TOP_K) -> list[dict]:
    if chroma_collection.count() == 0:
        return []

    try:
        query_embedding = get_embedding(query)
        results = chroma_collection.query(
            query_embeddings=[query_embedding],
            n_results=min(top_k * 2, chroma_collection.count()),
            include=["documents", "distances", "metadatas"]
        )

        now = datetime.now()
        scored = []
        for doc, dist, meta in zip(
            results["documents"][0],
            results["distances"][0],
            results["metadatas"][0]
        ):
            if dist >= MEMORY_DISTANCE_THRESHOLD and not EMBEDDING_FALLBACK["active"]:
                continue

            meta = meta or {}
            mem_type = meta.get("type", "normal")
            importance = meta.get("importance", "medium")
            last_accessed = meta.get("last_accessed") or meta.get("created_at", "")
            is_promise = mem_type == "promise"
            is_image = mem_type == "image"

            decay = 0.0
            if not is_promise and not is_image and importance != "high":
                try:
                    la = datetime.fromisoformat(last_accessed.replace("Z", ""))
                    if (now - la).days > DAILY_EXPIRE_DAYS:
                        decay = 0.15
                except Exception:
                    pass

            final_dist = dist + decay
            if is_promise:
                final_dist -= 0.08
            if is_image:
                final_dist -= 0.05  # 图片记忆也稍微加权

            scored.append({
                "content": doc,
                "distance": final_dist,
                "type": mem_type,
                "importance": importance,
                "image_url": meta.get("image_url", "")
            })

        scored.sort(key=lambda x: x["distance"])
        return scored[:top_k]

    except Exception as e:
        print(f"[记忆搜索失败] {e}")
        return []

def build_search_query(user_message: str, recent_messages: list[dict]) -> str:
    parts = []
    for m in recent_messages[-QUERY_CONTEXT_TURNS:]:
        role = "你" if m["role"] == "user" else "我"
        content = m["content"]
        if m.get("image_url"):
            content += " [附带图片]"
        parts.append(f"{role}: {content}")
    parts.append(f"你: {user_message}")
    return "\n".join(parts)

def touch_memories(memory_contents: list[str]):
    if not memory_contents or chroma_collection.count() == 0:
        return
    try:
        all_data = chroma_collection.get(include=["documents", "metadatas"])
        id_map = {}
        for mid, doc, meta in zip(all_data["ids"], all_data["documents"], all_data["metadatas"]):
            if doc in memory_contents:
                id_map[mid] = meta or {}

        if not id_map:
            return

        now_iso = datetime.now().isoformat()
        for mid, meta in id_map.items():
            meta["last_accessed"] = now_iso
            chroma_collection.update(ids=[mid], metadatas=[meta])
    except Exception as e:
        print(f"[更新last_accessed失败] {e}")

# ─── 记忆存入 ─────────────────────────────────────────────
def save_memory(content: str, mem_type: str = "normal", importance: str = "medium",
                image_url: str = "", memory_id: str = None):
    try:
        embedding = get_embedding(content)
        mid = memory_id or f"mem_{int(time.time() * 1000)}_{hash(content) % 10000}"
        now_iso = datetime.now().isoformat()
        meta = {
            "created_at": now_iso,
            "last_accessed": now_iso,
            "type": mem_type,
            "importance": importance
        }
        if image_url:
            meta["image_url"] = image_url

        chroma_collection.add(
            documents=[content],
            embeddings=[embedding],
            ids=[mid],
            metadatas=[meta]
        )
        print(f"[记忆已存][{mem_type}/{importance}] {content[:50]}...")
        return mid
    except Exception as e:
        print(f"[记忆存储失败] {e}")
        return None

# ─── 活跃上下文 ───────────────────────────────────────────
def get_active_context() -> str:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT summary FROM active_context WHERE id = 1")
    row = c.fetchone()
    conn.close()
    return row[0] if row else ""

def save_active_context(summary: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "UPDATE active_context SET summary = ?, updated_at = CURRENT_TIMESTAMP WHERE id = 1",
        (summary,)
    )
    conn.commit()
    conn.close()

def synthesize_active_context(recent_messages: list[dict], memories: list[dict]):
    dialogue_text = "\n".join([
        f"{'你' if m['role'] == 'user' else '我'}: {m['content']}"
        for m in recent_messages[-16:]
    ])

    memory_text = "\n".join([f"- {m['content']}" for m in memories]) if memories else "（暂无相关长期记忆）"

    prompt = f"""你是对话状态整理助手。请根据下面的最近对话和相关记忆，写一段很短的「当前对话状态」。

要求：
1. 用「你」指代用户（主人的一方），「我」指代TA（恋人的一方）
2. 只写正在聊的主线、没聊完的点、当前情绪或重点，2-5句话即可
3. 不要复述整段对话，不要写无关闲聊
4. 如果话题刚切换，把新旧话题的衔接点写清楚
5. 直接输出纯文本，不要标题、不要JSON、不要解释

相关长期记忆：
{memory_text}

最近对话：
{dialogue_text}

当前对话状态："""

    try:
        resp = requests.post(
            "https://api.deepseek.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": "deepseek-chat",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3,
                "max_tokens": 300
            },
            timeout=25
        )
        resp.raise_for_status()
        summary = resp.json()["choices"][0]["message"]["content"].strip()
        if summary.startswith("当前对话状态"):
            summary = summary.split("：", 1)[-1].strip()
        save_active_context(summary)
        print(f"[活跃上下文已更新] {summary[:60]}...")
        return summary
    except Exception as e:
        print(f"[活跃上下文更新失败] {e}")
        return get_active_context()

# ─── 记忆提炼 ─────────────────────────────────────────────
def extract_memories_from_dialogue(messages: list[dict]):
    dialogue_text = "\n".join([
        f"{'用户' if m['role'] == 'user' else 'TA'}: {m['content']}"
        for m in messages
    ])

    prompt = f"""你是一个记忆提炼助手。请从下面的对话中提炼出值得长期记住的关键信息。

规则：
1. 「用户」指主人的一方，「TA」指恋人的一方
2. 每条记忆写成一句完整、自然的话，第三人称
3. 只提炼有价值的信息：重要事件、情感、偏好、约定、特别时刻、态度变化、发过的重要图片
4. 必须区分态度：
   - 如果是TA主动想做的，正常写
   - 如果是被用户撒娇、卖萌、软磨硬泡后才答应的，必须明确写出来，例如「用户撒娇要TA……，TA其实不太想，但最后还是答应了」
5. 如果有明确承诺或约定，单独成条，并尽量保留时间线索
6. 语气和绕弯过程可以简单保留
7. 不要提炼无意义的日常闲聊
8. 不要出现「AI」「助手」「bot」等机器相关词
9. 输出纯JSON数组，每个元素是对象：
[
  {{"content": "记忆句子", "type": "normal|promise|preference|coerced|image", "importance": "high|medium|low"}}
]

对话内容：
{dialogue_text}

请输出JSON数组："""

    try:
        resp = requests.post(
            "https://api.deepseek.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": "deepseek-chat",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3,
                "max_tokens": 1200
            },
            timeout=35
        )
        resp.raise_for_status()
        raw = resp.json()["choices"][0]["message"]["content"].strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        items = json.loads(raw)

        print(f"[提炼完成] 共{len(items)}条记忆")
        for item in items:
            if isinstance(item, str):
                save_memory(item, "normal", "medium")
            else:
                content = item.get("content", "").strip()
                mem_type = item.get("type", "normal")
                importance = item.get("importance", "medium")
                if content:
                    save_memory(content, mem_type, importance)

    except json.JSONDecodeError as e:
        print(f"[记忆提炼JSON解析失败] {e}, raw: {raw}")
    except Exception as e:
        print(f"[记忆提炼失败] {e}")

def extract_memories_from_transcript(transcript: str) -> list[dict]:
    """从「用户：…\nTA：…」文本块提炼记忆（/mem/ingest 用），返回创建的列表"""
    prompt = f"""你是记忆提炼助手。请从下面的对话记录中提炼出值得长期记住的关键信息。

规则：
1. 「用户」指主人的一方，「TA」指恋人的一方
2. 每条记忆写成一句完整、自然的话，第三人称
3. 只提炼有价值的信息：重要事件、情感、偏好、约定、特别时刻、态度变化、发过的重要图片
4. 必须区分态度：
   - 如果是TA主动想做的，正常写
   - 如果是被用户撒娇、卖萌、软磨硬泡后才答应的，必须明确写出来，例如「用户撒娇要TA……，TA其实不太想，但最后还是答应了」
5. 如果有明确承诺或约定，单独成条，并尽量保留时间线索
6. 不要提炼无意义的日常闲聊
7. 不要出现「AI」「助手」「bot」等机器相关词
8. 输出纯JSON数组，每个元素是对象：
[
  {{"content": "记忆句子", "type": "normal|promise|preference|coerced|image", "importance": "high|medium|low"}}
]

对话记录：
{transcript}

请输出JSON数组："""
    created = []
    try:
        resp = requests.post(
            "https://api.deepseek.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": "deepseek-chat",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3,
                "max_tokens": 1200
            },
            timeout=45
        )
        resp.raise_for_status()
        raw = resp.json()["choices"][0]["message"]["content"].strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        items = json.loads(raw)
        print(f"[transcript提炼完成] 共{len(items)}条记忆")
        for item in items:
            if isinstance(item, str):
                mid = save_memory(item, "normal", "medium")
                if mid: created.append({"content": item, "type": "normal", "importance": "medium"})
            else:
                content = item.get("content", "").strip()
                mem_type = item.get("type", "normal")
                importance = item.get("importance", "medium")
                if content:
                    mid = save_memory(content, mem_type, importance)
                    if mid: created.append({"content": content, "type": mem_type, "importance": importance})
    except json.JSONDecodeError as e:
        print(f"[transcript提炼JSON解析失败] {e}, raw: {raw}")
    except Exception as e:
        print(f"[transcript提炼失败] {e}")
    return created

def check_and_extract_memories():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute("SELECT last_message_id FROM memory_checkpoints ORDER BY id DESC LIMIT 1")
    row = c.fetchone()
    last_id = row[0] if row else 0

    c.execute("SELECT COUNT(*) FROM messages WHERE id > ?", (last_id,))
    new_count = c.fetchone()[0]

    if new_count < MEMORY_EXTRACT_INTERVAL:
        conn.close()
        return

    c.execute("SELECT id, role, content FROM messages WHERE id > ? ORDER BY id ASC", (last_id,))
    rows = c.fetchall()
    conn.close()

    if not rows:
        return

    messages = [{"role": r[1], "content": r[2]} for r in rows]
    last_msg_id = rows[-1][0]

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO memory_checkpoints (last_message_id) VALUES (?)", (last_msg_id,))
    conn.commit()
    conn.close()

    thread = threading.Thread(target=extract_memories_from_dialogue, args=(messages,))
    thread.daemon = True
    thread.start()
    print(f"[异步提炼触发] 共{len(messages)}条消息")

# ─── 数据库操作 ───────────────────────────────────────────
def save_message(role: str, content: str, image_url: str = None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT INTO messages (role, content, image_url) VALUES (?, ?, ?)",
        (role, content, image_url)
    )
    conn.commit()
    conn.close()

def get_recent_messages(limit: int = RECENT_TURNS) -> list[dict]:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        SELECT role, content, image_url FROM messages
        ORDER BY id DESC LIMIT ?
    """, (limit,))
    rows = c.fetchall()
    conn.close()
    result = []
    for r in reversed(rows):
        item = {"role": r[0], "content": r[1]}
        if r[2]:
            item["image_url"] = r[2]
        result.append(item)
    return result

# ─── 主对话接口 ───────────────────────────────────────────
@app.route("/chat", methods=["POST"])
@require_auth
def chat():
    data = request.json or {}
    user_message = (data.get("message") or "").strip()
    image_url = (data.get("image_url") or "").strip() or None

    if not user_message and not image_url:
        return jsonify({"error": "消息和图片不能同时为空"}), 400

    # 1. 存用户消息（文字 + 可选图片URL）
    display_content = user_message if user_message else "[图片]"
    save_message("user", display_content, image_url)

    # 2. 如果有图片，立刻生成描述并存成图片记忆
    image_desc = ""
    if image_url:
        image_desc = describe_image(image_url, user_message)
        # 存成长期记忆
        mem_content = f"你发过一张图片：{image_desc}"
        if user_message:
            mem_content += f"（配文：{user_message}）"
        save_memory(
            content=mem_content,
            mem_type="image",
            importance="high",
            image_url=image_url
        )

    # 3. 最近历史
    recent_messages = get_recent_messages(RECENT_TURNS)

    # 4. 检索
    search_text = user_message or image_desc or "图片"
    search_query = build_search_query(search_text, recent_messages)
    memories = search_memories(search_query)

    # 5. 活跃上下文
    active_summary = get_active_context()

    # 6. 构建 system
    system = SYSTEM_PROMPT

    if active_summary:
        system += f"\n\n[当前对话状态——优先参考，自然接上]\n{active_summary}"

    promises = [m for m in memories if m.get("type") == "promise"]
    images = [m for m in memories if m.get("type") == "image"]
    others = [m for m in memories if m.get("type") not in ("promise", "image")]

    if promises:
        p_text = "\n".join([f"- {m['content']}" for m in promises])
        system += f"\n\n[相关承诺/约定——如果现在合适，可以主动提起来]\n{p_text}"

    if images:
        i_text = "\n".join([f"- {m['content']}" for m in images])
        system += f"\n\n[相关图片记忆——她以前发过的图，可以自然提起]\n{i_text}"

    if others:
        o_text = "\n".join([f"- {m['content']}" for m in others])
        system += f"\n\n[相关长期记忆，自然融入，不要直接背诵]\n{o_text}"

    # 7. 构建发给Claude的 messages（支持当前这张图）
    claude_messages = []
    for m in recent_messages:
        if m["role"] == "user" and m.get("image_url") and m is recent_messages[-1]:
            # 当前这条带图的消息，用多模态格式
            content_blocks = []
            if m.get("image_url"):
                content_blocks.append({
                    "type": "image",
                    "source": {"type": "url", "url": m["image_url"]}
                })
            text = m["content"] if m["content"] != "[图片]" else "（看图）"
            content_blocks.append({"type": "text", "text": text})
            claude_messages.append({"role": "user", "content": content_blocks})
        else:
            claude_messages.append({
                "role": m["role"],
                "content": m["content"]
            })

    # 8. 流式调用
    def generate():
        full_response = ""
        try:
            resp = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": CLAUDE_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "Content-Type": "application/json"
                },
                json={
                    "model": "claude-sonnet-4-6",
                    "max_tokens": 1024,
                    "system": system,
                    "messages": claude_messages,
                    "stream": True
                },
                stream=True,
                timeout=90
            )

            for line in resp.iter_lines():
                if not line:
                    continue
                line = line.decode("utf-8")
                if line.startswith("data: "):
                    raw = line[6:]
                    if raw == "[DONE]":
                        break
                    try:
                        chunk = json.loads(raw)
                        if chunk.get("type") == "content_block_delta":
                            text = chunk["delta"].get("text", "")
                            full_response += text
                            yield f"data: {json.dumps({'text': text})}\n\n"
                    except json.JSONDecodeError:
                        continue

        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

        finally:
            if full_response:
                save_message("assistant", full_response)

            def post_process():
                try:
                    contents = [m["content"] for m in memories]
                    touch_memories(contents)
                    latest = get_recent_messages(RECENT_TURNS)
                    synthesize_active_context(latest, memories)
                except Exception as e:
                    print(f"[post_process失败] {e}")
                check_and_extract_memories()

            thread = threading.Thread(target=post_process)
            thread.daemon = True
            thread.start()

            yield "data: [DONE]\n\n"

    return Response(stream_with_context(generate()), mimetype="text/event-stream")

# ─── 记忆库专用接口（Memory Palace 前端接入用）────────────────
@app.route("/add", methods=["POST"])
@require_auth
def mem_add():
    """存一条记忆：{content, type?, importance?, image_url?} → {ok, id}"""
    data = request.json or {}
    content = (data.get("content") or "").strip()
    if not content:
        return jsonify({"error": "content 不能为空"}), 400
    mid = save_memory(
        content,
        data.get("type", "normal"),
        data.get("importance", "medium"),
        (data.get("image_url") or "").strip(),
    )
    return jsonify({"ok": mid is not None, "id": mid})

@app.route("/ingest", methods=["POST"])
@require_auth
def mem_ingest():
    """从聊天文本块提炼记忆：{transcript, rangeLabel?} → {ok, created, count}"""
    data = request.json or {}
    transcript = (data.get("transcript") or "").strip()
    if not transcript:
        return jsonify({"error": "transcript 不能为空"}), 400
    created = extract_memories_from_transcript(transcript)
    return jsonify({"ok": True, "created": created, "count": len(created)})

@app.route("/search", methods=["GET"])
@require_auth
def mem_search():
    """语义检索：?q=...&top_k=5 → {ok, memories:[{content,type,importance,distance,image_url}]}"""
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"error": "q 不能为空"}), 400
    try:
        top_k = max(1, min(int(request.args.get("top_k", 5)), 20))
    except (TypeError, ValueError):
        top_k = 5
    mems = search_memories(q, top_k)
    return jsonify({"ok": True, "memories": mems})

@app.route("/add_image", methods=["POST"])
@require_auth
def mem_add_image():
    """图片记忆（前端聊天模型已多模态分析，VPS 只存文字）：{content?/caption?, image_url?}"""
    data = request.json or {}
    caption = (data.get("content") or data.get("caption") or "").strip()
    if not caption:
        return jsonify({"error": "content/caption 不能为空（请先让前端聊天模型分析图片生成描述）"}), 400
    content = f"用户发过一张图片：{caption}"
    mid = save_memory(content, "image", "high", (data.get("image_url") or "").strip())
    return jsonify({"ok": mid is not None, "id": mid, "content": content})

# ─── 查看记忆 ─────────────────────────────────────────────
@app.route("/memories", methods=["GET"])
@require_auth
def list_memories():
    count = chroma_collection.count()
    if count == 0:
        return jsonify({"total": 0, "memories": []})

    results = chroma_collection.get(include=["documents", "metadatas"])
    memories = []
    for doc, meta in zip(results["documents"], results["metadatas"]):
        meta = meta or {}
        memories.append({
            "content": doc,
            "type": meta.get("type", "normal"),
            "importance": meta.get("importance", "medium"),
            "image_url": meta.get("image_url", ""),
            "created_at": meta.get("created_at", ""),
            "last_accessed": meta.get("last_accessed", "")
        })
    memories.sort(key=lambda x: x["created_at"], reverse=True)
    return jsonify({"total": count, "memories": memories})

@app.route("/active_context", methods=["GET"])
@require_auth
def get_active_context_api():
    return jsonify({"summary": get_active_context()})

@app.route("/admin/extract", methods=["POST"])
@require_auth
def manual_extract():
    data = request.json or {}
    limit = data.get("limit", 60)

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT role, content FROM messages ORDER BY id DESC LIMIT ?", (limit,))
    rows = c.fetchall()
    conn.close()

    if not rows:
        return jsonify({"message": "没有对话记录"})

    messages = [{"role": r[0], "content": r[1]} for r in reversed(rows)]
    thread = threading.Thread(target=extract_memories_from_dialogue, args=(messages,))
    thread.daemon = True
    thread.start()
    return jsonify({"message": f"已触发提炼，共{len(messages)}条消息"})

@app.route("/history", methods=["GET"])
@require_auth
def get_history():
    limit = request.args.get("limit", 40, type=int)
    messages = get_recent_messages(limit)
    return jsonify({"messages": messages})

@app.route("/admin/clear_memories", methods=["POST"])
@require_auth
def clear_memories():
    count = chroma_collection.count()
    if count > 0:
        all_ids = chroma_collection.get()["ids"]
        chroma_collection.delete(ids=all_ids)
    return jsonify({"message": f"已清空{count}条记忆"})

@app.route("/ping", methods=["GET"])
def ping():
    active = get_active_context()
    return jsonify({
        "status": "ok",
        "memory_count": chroma_collection.count(),
        "active_context_preview": active[:80] + ("..." if len(active) > 80 else ""),
        "time": datetime.now().isoformat()
    })

if __name__ == "__main__":
    init_db()
    print(f"[启动] 记忆库已有 {chroma_collection.count()} 条记忆")
    print(f"[启动] 活跃上下文: {get_active_context()[:50] or '（空）'}...")
    app.run(host="0.0.0.0", port=5000, debug=False)
