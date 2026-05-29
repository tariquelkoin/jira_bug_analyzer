import os
from analyzer.config import PROMPTS_DIR

MTR_SYSTEM_PROMPT            = ""
MTR_USER_PROMPT_TEMPLATE     = ""
CLEANUP_SYSTEM_PROMPT        = ""
CLEANUP_USER_PROMPT_TEMPLATE = ""


def _load(filename):
    path = os.path.join(PROMPTS_DIR, filename)
    if not os.path.exists(path):
        print(f"  [WARNING] Prompt file not found: {path}")
        return ""
    with open(path) as f:
        return f.read().strip()


def load_prompts():
    global MTR_SYSTEM_PROMPT, MTR_USER_PROMPT_TEMPLATE
    global CLEANUP_SYSTEM_PROMPT, CLEANUP_USER_PROMPT_TEMPLATE
    MTR_SYSTEM_PROMPT            = _load("mtr_system.txt")
    MTR_USER_PROMPT_TEMPLATE     = _load("mtr_user.txt")
    CLEANUP_SYSTEM_PROMPT        = _load("cleanup_system.txt")
    CLEANUP_USER_PROMPT_TEMPLATE = _load("cleanup_user.txt")
