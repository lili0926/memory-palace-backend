"""vigil 的 LLM 调用：DeepSeek（OpenAI 兼容 /chat/completions），复用 VPS 现有 key。"""
import os
import httpx

BASE_URL = os.getenv("VIGIL_LLM_BASE_URL", "https://api.deepseek.com/v1")
MODEL = os.getenv("VIGIL_LLM_MODEL", "deepseek-v4-flash")
API_KEY = os.getenv("VIGIL_LLM_API_KEY", os.getenv("CAPTIVITY_AI_API_KEY", ""))


def ask(messages, system, max_tokens=12000, temperature=0.9):
    """messages: [{role,content}...]; system: str -> 返回模型纯文本。"""
    if not API_KEY:
        raise RuntimeError("VIGIL_LLM_API_KEY 未设置（可复用 CAPTIVITY_AI_API_KEY）")
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.extend(messages or [])
    payload = {
        "model": MODEL,
        "messages": msgs,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    r = httpx.post(
        f"{BASE_URL.rstrip('/')}/chat/completions",
        headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
        json=payload,
        timeout=120,
    )
    r.raise_for_status()
    data = r.json()
    try:
        return data["choices"][0]["message"]["content"] or ""
    except Exception:
        return ""
