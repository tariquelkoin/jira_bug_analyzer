import re
import time
import requests
from lib.config import (
    MTR_BACKEND, OLLAMA_HOST, OLLAMA_MODEL, OLLAMA_TIMEOUT,
    CLAUDE_API_KEY, CLAUDE_MODEL, OPENAI_API_KEY, OPENAI_MODEL,
)


def call_ollama(prompt):
    try:
        resp = requests.post(
            f"{OLLAMA_HOST}/api/generate",
            json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False},
            timeout=OLLAMA_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json().get("response", "").strip()
    except Exception as e:
        print(f"  [Ollama error] {e}")
        return None


def call_claude(prompt, system_prompt=""):
    if not CLAUDE_API_KEY:
        print("  [Claude error] CLAUDE_API_KEY not set")
        return None
    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": CLAUDE_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": CLAUDE_MODEL,
                "max_tokens": 2048,
                "system": system_prompt,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json()["content"][0]["text"].strip()
    except Exception as e:
        print(f"  [Claude error] {e}")
        return None


def call_openai(prompt, system_prompt=""):
    if not OPENAI_API_KEY:
        print("  [OpenAI error] OPENAI_API_KEY not set")
        return None
    for attempt in range(3):
        try:
            resp = requests.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {OPENAI_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": OPENAI_MODEL,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user",   "content": prompt},
                    ],
                    "max_tokens": 2048,
                    "temperature": 0.2,
                },
                timeout=60,
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"].strip()
        except requests.exceptions.HTTPError as e:
            if resp.status_code == 429:
                wait = 20 * (attempt + 1)
                print(f"  [OpenAI] Rate limited — waiting {wait}s (retry {attempt+1}/3)...")
                time.sleep(wait)
            else:
                print(f"  [OpenAI error] {e}")
                return None
        except Exception as e:
            print(f"  [OpenAI error] {e}")
            return None
    print("  [OpenAI] All retries exhausted")
    return None


def call_llm(prompt, system_prompt=""):
    """Route to the configured backend."""
    if MTR_BACKEND == "ollama":
        return call_ollama(system_prompt + "\n\n" + prompt if system_prompt else prompt)
    elif MTR_BACKEND == "claude":
        return call_claude(prompt, system_prompt)
    elif MTR_BACKEND == "openai":
        return call_openai(prompt, system_prompt)
    return None


def strip_markdown_fences(text):
    if not text:
        return text
    text = re.sub(r"^```[a-z]*\n?", "", text, flags=re.MULTILINE)
    text = re.sub(r"\n?```$",       "", text, flags=re.MULTILINE)
    return text.strip()
