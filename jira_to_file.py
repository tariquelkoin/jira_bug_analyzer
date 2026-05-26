import requests
import re
import json
import os
import sys

# =========================
# CONFIG
# =========================

BASE_URL = "https://jira.mariadb.org/rest/api/2"
JQL = "project=MDEV AND updated >= -1d ORDER BY updated DESC"
MAX_RESULTS = 100
BASE_DIR = "bugs"
DATASET_FILE = "bug_dataset.jsonl"

# --- Repro Quality Cleanup ---
# LLM cleanup is triggered when quality score is below this threshold (0-100)
CLEANUP_QUALITY_THRESHOLD = int(os.environ.get("CLEANUP_THRESHOLD", "60"))

# --- MTR Generation Backend ---
# Options: "ollama", "claude", "openai", "none"
# Override via env: JIRA_MTR_BACKEND=claude
MTR_BACKEND = os.environ.get("JIRA_MTR_BACKEND", "ollama")

# Ollama settings (default)
OLLAMA_HOST    = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL   = os.environ.get("OLLAMA_MODEL", "llama3.2")
OLLAMA_TIMEOUT = int(os.environ.get("OLLAMA_TIMEOUT", "600"))  # 10 min for CPU-only

# Claude API settings
CLAUDE_API_KEY = os.environ.get("CLAUDE_API_KEY", "")
CLAUDE_MODEL = "claude-sonnet-4-20250514"

# OpenAI settings
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o")

# =========================
# PROMPT LOADER
# =========================

PROMPTS_DIR = os.environ.get("PROMPTS_DIR", os.path.join(os.path.dirname(__file__), "prompts"))


def _load_prompt(filename):
    path = os.path.join(PROMPTS_DIR, filename)
    if not os.path.exists(path):
        print(f"  [WARNING] Prompt file not found: {path}")
        return ""
    with open(path, "r") as f:
        return f.read().strip()


def load_prompts():
    global MTR_SYSTEM_PROMPT, MTR_USER_PROMPT_TEMPLATE
    global CLEANUP_SYSTEM_PROMPT, CLEANUP_USER_PROMPT_TEMPLATE
    MTR_SYSTEM_PROMPT          = _load_prompt("mtr_system.txt")
    MTR_USER_PROMPT_TEMPLATE   = _load_prompt("mtr_user.txt")
    CLEANUP_SYSTEM_PROMPT      = _load_prompt("cleanup_system.txt")
    CLEANUP_USER_PROMPT_TEMPLATE = _load_prompt("cleanup_user.txt")

# =========================
# KEYWORD MAPS
# =========================

ENGINE_KEYWORDS = {
    "InnoDB": ["innodb", "btr_cur", "trx_", "dict_table", "ibuf", "row_ins"],
    "MyISAM": ["myisam"],
    "Aria": ["aria"],
    "Galera": ["galera", "wsrep"],
    "RocksDB": ["rocksdb"],
}

SQL_KEYWORDS = [
    "ALTER TABLE", "CREATE TABLE", "CREATE SEQUENCE", "DROP TABLE", "DROP SEQUENCE",
    "INSERT", "UPDATE", "DELETE", "SELECT", "FULLTEXT", "PARTITION", "WINDOW",
    "JSON", "TRIGGER", "VIEW", "CTE", "RECURSIVE", "INDEX", "SAVEPOINT",
    "COMMIT", "ROLLBACK", "SET SESSION", "SET GLOBAL", "START TRANSACTION",
    "BEGIN", "CALL", "REPLACE", "TRUNCATE", "LOCK TABLES", "UNLOCK TABLES",
]

SQL_LINE_STARTERS = re.compile(
    r"^\s*(SELECT|INSERT\s+INTO|UPDATE|DELETE\s+FROM|CREATE\s+(TABLE|DATABASE|SEQUENCE|INDEX|VIEW|TRIGGER|PROCEDURE|FUNCTION)|"
    r"DROP\s+(TABLE|DATABASE|SEQUENCE|INDEX|VIEW)|ALTER\s+TABLE|TRUNCATE|REPLACE\s+INTO|CALL|"
    r"SET\s+(SESSION|GLOBAL|NAMES|CHARACTER)|START\s+TRANSACTION|BEGIN|COMMIT|ROLLBACK|"
    r"SAVEPOINT|LOCK\s+TABLES|UNLOCK\s+TABLES|WITH\s+\w+\s+AS)\b",
    re.IGNORECASE,
)

ERROR_PATTERNS = [
    r"ERROR\s+\d+", r"SIGSEGV", r"Assertion.*", r"AddressSanitizer",
    r"LeakSanitizer", r"runtime error:", r"Deadlock found",
    r"got signal \d+", r"WSREP.*FSM.*no such a transition",
]

ASSERTION_PATTERNS = [
    r"Assertion [`'\"]?(.*?)[`'\"]?( failed|$)",
]

# =========================
# AREA CLASSIFICATION
# =========================

# Map JIRA component names -> canonical area
COMPONENT_AREA_MAP = {
    "Optimizer":            "Optimizer",
    "Query optimizer":      "Optimizer",
    "InnoDB":               "InnoDB",
    "Galera":               "Galera",
    "Replication":          "Replication",
    "DDL":                  "DDL",
    "Partitioning":         "Partitioning",
    "Full-text search":     "Full-text Search",
    "JSON":                 "JSON",
    "Stored routines":      "Stored Procedures / Triggers",
    "Triggers":             "Stored Procedures / Triggers",
    "Locking":              "Locking / Deadlock",
    "Crash recovery":       "Crash Recovery",
    "Backup":               "Backup",
    "Security":             "Security",
    "Performance schema":   "Performance Schema",
    "MyISAM":               "MyISAM",
    "Aria":                 "Aria",
    "RocksDB":              "RocksDB",
    "Spider":               "Spider",
    "CONNECT":              "CONNECT Engine",
    "Data types":           "Data Types",
    "Character sets":       "Character Sets / Collation",
    "Window functions":     "Window Functions",
    "CTE":                  "CTE / Recursive Queries",
    "Sequences":            "Sequences",
}

# Keyword -> area, checked against summary + stack trace + description
KEYWORD_AREA_MAP = [
    (["wsrep", "galera", "sst", "ist"],                 "Galera"),
    (["innodb", "btr_cur", "row_ins", "dict_table"],     "InnoDB"),
    (["replication", "binlog", "slave", "relay log",
      "rpl_", "gtid"],                                   "Replication"),
    (["optimizer", "join_buffer", "range_check",
      "eq_ref", "derived", "subquery", "cost model"],    "Optimizer"),
    (["partition", "partitioning"],                      "Partitioning"),
    (["fulltext", "full-text", "ft_"],                   "Full-text Search"),
    (["json_", "json extract", "json_table"],            "JSON"),
    (["trigger", "stored procedure", "stored function",
      "sp_head", "sp_instr"],                            "Stored Procedures / Triggers"),
    (["deadlock", "lock wait", "lock_sys",
      "trx_lock", "waiting for lock"],                   "Locking / Deadlock"),
    (["crash recovery", "redo log", "ib_logfile",
      "doublewrite", "ibdata"],                          "Crash Recovery"),
    (["alter table", "create table", "drop table",
      "instant ddl", "online ddl"],                      "DDL"),
    (["window function", "over (", "rank()", "row_number()"],  "Window Functions"),
    (["with recursive", "cte", "common table"],          "CTE / Recursive Queries"),
    (["create sequence", "nextval", "seq_"],             "Sequences"),
    (["myisam"],                                         "MyISAM"),
    (["aria"],                                           "Aria"),
    (["rocksdb"],                                        "RocksDB"),
    (["performance_schema", "performance schema"],       "Performance Schema"),
    (["backup", "mariabackup", "xtrabackup"],            "Backup"),
    (["ssl", "tls", "privilege", "grant", "auth_"],      "Security"),
    (["character set", "collation", "charset", "utf8",
      "latin1"],                                         "Character Sets / Collation"),
    (["decimal", "float", "double", "timestamp",
      "datetime", "geometry", "spatial"],                "Data Types"),
]


def identify_area(summary, description, stack_trace, components, engines):
    """
    Classify the bug area. Priority order:
      1. JIRA components (most reliable)
      2. Keyword scan over summary + stack trace (fast signals)
      3. Storage engine detection
      4. Fallback: General
    Returns list of areas (primary first).
    """
    areas = []

    # 1. JIRA components
    for comp in components:
        for comp_key, area in COMPONENT_AREA_MAP.items():
            if comp_key.lower() in comp.lower() and area not in areas:
                areas.append(area)

    # 2. Keyword scan
    scan_text = f"{summary}\n{stack_trace or ''}\n{description[:2000]}".lower()
    for keywords, area in KEYWORD_AREA_MAP:
        if area not in areas and any(kw in scan_text for kw in keywords):
            areas.append(area)

    # 3. Storage engines as fallback areas
    engine_to_area = {
        "InnoDB": "InnoDB", "MyISAM": "MyISAM",
        "Aria": "Aria", "Galera": "Galera", "RocksDB": "RocksDB",
    }
    for engine in engines:
        area = engine_to_area.get(engine)
        if area and area not in areas:
            areas.append(area)

    return areas if areas else ["General"]



# =========================
# REPRO QUALITY ASSESSMENT
# =========================

# Prose patterns that imply SQL operations without actual SQL
PROSE_SQL_SIGNALS = re.compile(
    r"\b(run|execute|issue|perform|do|try|trigger)\s+(a\s+)?"
    r"(SELECT|INSERT|UPDATE|DELETE|ALTER|CREATE|DROP|COMMIT|ROLLBACK|query|statement)\b"
    r"|\bsession\s+\d+\b|\bthread\s+\d+\b|\bconcurrently\b|\bsimultaneously\b",
    re.IGNORECASE,
)

# Detect table names referenced in DML (FROM / INTO / UPDATE / JOIN)
TABLE_REF_PATTERN = re.compile(
    r"(?:FROM|JOIN|INTO|UPDATE)\s+`?(\w+)`?",
    re.IGNORECASE,
)

# Detect table names defined via CREATE TABLE
TABLE_DEF_PATTERN = re.compile(
    r"CREATE\s+(?:TEMPORARY\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?`?(\w+)`?",
    re.IGNORECASE,
)


def assess_repro_quality(description, repro_sql, stack_trace, crash_query, summary):
    """
    Rule-based repro quality scorer. No LLM needed.

    Scoring dimensions (total 100):
      - Has SQL                    30 pts
      - Has schema (CREATE TABLE)  15 pts
      - Has data setup (INSERT)    10 pts
      - Has triggering query        10 pts
      - Table ref consistency      10 pts
      - Proper semicolons           5 pts
      - Has error context          15 pts  (stack trace / crash query / error message)
      - Description length          5 pts

    Returns dict:
      score       int 0-100
      level       str: good / partial / shaky / prose_only / none
      issues      list[str]: specific problems found
      details     dict: breakdown per dimension
    """
    issues = []
    details = {}
    score = 0

    sql = repro_sql or ""
    desc = description or ""
    desc_lower = desc.lower()

    # --- Dimension 1: Has SQL (30 pts) ---
    if sql.strip():
        score += 30
        details["has_sql"] = True
    else:
        details["has_sql"] = False
        if PROSE_SQL_SIGNALS.search(desc):
            issues.append("prose_only")
            details["prose_signals"] = True
        else:
            issues.append("no_sql")
        details["prose_signals"] = PROSE_SQL_SIGNALS.search(desc) is not None

    # --- Dimension 2: Has schema (15 pts) ---
    has_schema = bool(re.search(
        r"\bCREATE\s+(TEMPORARY\s+)?TABLE\b|\bCREATE\s+SEQUENCE\b",
        sql, re.IGNORECASE,
    ))
    details["has_schema"] = has_schema
    if has_schema:
        score += 15
    else:
        issues.append("no_schema")

    # --- Dimension 3: Has data setup (10 pts) ---
    has_data = bool(re.search(r"\bINSERT\s+INTO\b|\bREPLACE\s+INTO\b", sql, re.IGNORECASE))
    details["has_data_setup"] = has_data
    if has_data:
        score += 10
    else:
        issues.append("no_data_setup")

    # --- Dimension 4: Has a triggering DML/query (10 pts) ---
    # Something beyond just CREATE/INSERT — a SELECT, UPDATE, DELETE, or ALTER that is the trigger
    has_trigger_query = bool(re.search(
        r"\b(SELECT|UPDATE|DELETE\s+FROM|ALTER\s+TABLE|COMMIT|ROLLBACK)\b",
        sql, re.IGNORECASE,
    ))
    details["has_trigger_query"] = has_trigger_query
    if has_trigger_query:
        score += 10
    else:
        issues.append("no_trigger_query")

    # --- Dimension 5: Table reference consistency (10 pts) ---
    defined_tables = set(t.lower() for t in TABLE_DEF_PATTERN.findall(sql))
    referenced_tables = set(t.lower() for t in TABLE_REF_PATTERN.findall(sql))
    # Common pseudo-tables to ignore
    ignore = {"dual", "information_schema", "performance_schema", "mysql"}
    undefined = (referenced_tables - defined_tables) - ignore
    details["defined_tables"] = list(defined_tables)
    details["referenced_tables"] = list(referenced_tables)
    details["undefined_tables"] = list(undefined)
    if sql.strip() and undefined:
        # Partial credit: penalise per missing table but cap at -10
        penalty = min(len(undefined) * 3, 10)
        score += (10 - penalty)
        if undefined:
            issues.append(f"undefined_tables:{','.join(sorted(undefined))}")
    elif sql.strip():
        score += 10

    # --- Dimension 6: Semicolons (5 pts) ---
    if sql.strip():
        sql_lines = [l.strip() for l in sql.splitlines()
                     if l.strip() and not l.strip().startswith("--")
                     and not l.strip().startswith("#")]
        lines_with_semi = [l for l in sql_lines if l.endswith(";")]
        semi_ratio = len(lines_with_semi) / len(sql_lines) if sql_lines else 0
        details["semicolon_ratio"] = round(semi_ratio, 2)
        if semi_ratio >= 0.5:
            score += 5
        else:
            issues.append("missing_semicolons")
    else:
        details["semicolon_ratio"] = 0.0

    # --- Dimension 7: Error context (15 pts) ---
    has_stack   = bool(stack_trace and stack_trace.strip())
    has_crash_q = bool(crash_query and crash_query.strip())
    has_error_msg = bool(re.search(r"ERROR\s+\d+|SIGSEGV|Assertion|got signal", desc, re.IGNORECASE))
    context_pts = 0
    if has_stack:
        context_pts += 8
    if has_crash_q:
        context_pts += 4
    if has_error_msg:
        context_pts += 3
    score += min(context_pts, 15)
    details["has_stack_trace"] = has_stack
    details["has_crash_query"] = has_crash_q
    details["has_error_message"] = has_error_msg
    if not (has_stack or has_crash_q or has_error_msg):
        issues.append("no_error_context")

    # --- Dimension 8: Description length (5 pts) ---
    desc_len = len(desc.split())
    details["description_word_count"] = desc_len
    if desc_len >= 100:
        score += 5
    elif desc_len >= 30:
        score += 3
    else:
        issues.append("very_short_description")

    # --- Level ---
    if score >= 75:
        level = "good"
    elif score >= 50:
        level = "partial"
    elif score >= 25:
        level = "shaky"
    elif "prose_only" in issues:
        level = "prose_only"
    else:
        level = "none"

    return {
        "score": score,
        "level": level,
        "issues": issues,
        "details": details,
    }


# =========================
# REPRO CLEANUP & ENRICHMENT
# =========================

CLEANUP_SYSTEM_PROMPT = ""
CLEANUP_USER_PROMPT_TEMPLATE = ""


def cleanup_and_enrich_repro(bug_id, summary, description, repro_sql,
                               crash_query, stack_trace, area, quality):
    """
    Use the configured LLM backend to clean up and enrich a poor-quality repro.
    Only called when quality score is below CLEANUP_QUALITY_THRESHOLD.
    Returns cleaned SQL string or None.
    """
    if MTR_BACKEND == "none":
        return None

    prompt = CLEANUP_USER_PROMPT_TEMPLATE.format(
        bug_id=bug_id,
        summary=summary,
        area=", ".join(area) if isinstance(area, list) else area,
        score=quality["score"],
        level=quality["level"],
        issues=", ".join(quality["issues"]) if quality["issues"] else "none",
        description=(description or "")[:1200],
        repro_sql=repro_sql or "(none extracted)",
        crash_query=crash_query or "(none)",
        stack_trace=(stack_trace or "")[:600],
    )

    print(f"  Repro quality={quality['score']}/100 ({quality['level']}) — cleaning up via [{MTR_BACKEND}]...")

    if MTR_BACKEND == "ollama":
        result = call_ollama(CLEANUP_SYSTEM_PROMPT + "\n\n" + prompt)
    elif MTR_BACKEND == "claude":
        # Reuse call_claude but with cleanup system prompt
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
                    "system": CLEANUP_SYSTEM_PROMPT,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=60,
            )
            resp.raise_for_status()
            result = resp.json()["content"][0]["text"].strip()
        except Exception as e:
            print(f"  [Claude cleanup error] {e}")
            return None
    elif MTR_BACKEND == "openai":
        if not OPENAI_API_KEY:
            print("  [OpenAI error] OPENAI_API_KEY not set")
            return None
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
                        {"role": "system", "content": CLEANUP_SYSTEM_PROMPT},
                        {"role": "user", "content": prompt},
                    ],
                    "max_tokens": 2048,
                    "temperature": 0.2,
                },
                timeout=60,
            )
            resp.raise_for_status()
            result = resp.json()["choices"][0]["message"]["content"].strip()
        except Exception as e:
            print(f"  [OpenAI cleanup error] {e}")
            return None
    else:
        return None

    if result:
        result = re.sub(r"^```[a-z]*\n?", "", result, flags=re.MULTILINE)
        result = re.sub(r"\n?```$", "", result, flags=re.MULTILINE)
        return result.strip()

    return None


# =========================

MTR_SYSTEM_PROMPT = ""
MTR_USER_PROMPT_TEMPLATE = ""


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


def call_claude(prompt):
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
                "system": MTR_SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["content"][0]["text"].strip()
    except Exception as e:
        print(f"  [Claude error] {e}")
        return None


def call_openai(prompt):
    if not OPENAI_API_KEY:
        print("  [OpenAI error] OPENAI_API_KEY not set")
        return None
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
                    {"role": "system", "content": MTR_SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                "max_tokens": 2048,
                "temperature": 0.2,
            },
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"  [OpenAI error] {e}")
        return None


def generate_mtr_test(bug_id, summary, description, repro_sql, crash_query, stack_trace, area, engines):
    """
    Generate an MTR .test file using the configured backend.
    Returns the test file content as a string, or None if generation fails.
    """
    if MTR_BACKEND == "none":
        return None

    bug_id_safe = bug_id.replace("-", "_").lower()

    # Trim context for CPU-based local models to keep generation time reasonable
    desc_limit  = 600  if MTR_BACKEND == "ollama" else 1500
    stack_limit = 300  if MTR_BACKEND == "ollama" else 800

    prompt = MTR_USER_PROMPT_TEMPLATE.format(
        bug_id=bug_id,
        bug_id_safe=bug_id_safe,
        summary=summary,
        area=", ".join(area) if isinstance(area, list) else area,
        engines=", ".join(engines) if engines else "unknown",
        description=(description or "")[:desc_limit],
        repro_sql=repro_sql or "(none extracted)",
        crash_query=crash_query or "(none)",
        stack_trace=(stack_trace or "")[:stack_limit],
    )

    print(f"  Generating MTR test via [{MTR_BACKEND}]...")

    if MTR_BACKEND == "ollama":
        result = call_ollama(MTR_SYSTEM_PROMPT + "\n\n" + prompt)
    elif MTR_BACKEND == "claude":
        result = call_claude(prompt)
    elif MTR_BACKEND == "openai":
        result = call_openai(prompt)
    else:
        print(f"  [MTR] Unknown backend: {MTR_BACKEND}")
        return None

    if result:
        result = re.sub(r"^```[a-z]*\n?", "", result, flags=re.MULTILINE)
        result = re.sub(r"\n?```$", "", result, flags=re.MULTILINE)
        return result.strip()

    return None


# =========================
# HELPERS
# =========================

def normalize_text(text):
    if not text:
        return ""
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def get_description_text(desc):
    if isinstance(desc, str):
        return desc
    result = []

    def walk(node):
        if isinstance(node, dict):
            node_type = node.get("type", "")
            if node_type == "text" and "text" in node:
                result.append(node["text"])
            elif "text" in node and node_type == "":
                result.append(node["text"])
            for key in ("content", "children"):
                if key in node:
                    walk(node[key])
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(desc)
    return "\n".join(result)


def extract_code_blocks(text):
    matches = re.findall(
        r"\{code(?::([^\}]*))?\}(.*?)\{code\}",
        text, re.DOTALL | re.IGNORECASE,
    )
    return [{"language": (lang or "").strip().lower(), "content": content.strip()}
            for lang, content in matches]


def classify_code_block(block):
    lang = block.get("language", "")
    content = block.get("content", "")
    content_upper = content.upper()

    if lang in ("sql", "mysql", "mariadb"):
        return "sql"
    if lang in ("bash", "sh", "shell"):
        return "shell"
    if lang in ("python", "py"):
        return "python"
    if lang in ("perl", "pl"):
        return "perl"

    if re.search(r"^(--\s*)?(connect|connection|disconnect|send|reap|let\s+\$)\b",
                 content, re.MULTILINE | re.IGNORECASE):
        return "mtr"
    if re.search(r"mysql\s+(-\w+\s+)*-e\s+[\"']", content, re.IGNORECASE):
        return "shell"
    if re.search(r"^#\s*!/bin/(ba)?sh", content, re.MULTILINE):
        return "shell"
    if re.search(r"import\s+threading|Thread\(", content):
        return "python"
    if re.search(r"use\s+DBI;|use\s+threads;", content):
        return "perl"
    if any(kw in content_upper for kw in SQL_KEYWORDS):
        return "sql"
    if re.search(r"\[\s*(ERROR|WARNING|Note)\s*\]", content):
        return "log"

    return "unknown"


def extract_general_log_sessions(text):
    sessions = []
    header_pattern = re.compile(r"general\.log\s+\**(\d+)\**", re.IGNORECASE)
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        header_match = header_pattern.search(lines[i])
        if header_match:
            thread_id = header_match.group(1)
            i += 1
            sql_lines = []
            while i < len(lines):
                line = lines[i]
                if line.strip() == "":
                    j = i + 1
                    while j < len(lines) and lines[j].strip() == "":
                        j += 1
                    if j < len(lines) and SQL_LINE_STARTERS.match(lines[j]):
                        i = j
                        continue
                    else:
                        break
                if header_pattern.search(line):
                    break
                sql_lines.append(line.strip())
                i += 1
            if sql_lines:
                sessions.append({"thread_id": thread_id,
                                  "statements": sql_lines,
                                  "raw": "\n".join(sql_lines)})
        else:
            i += 1
    return sessions


def extract_plain_text_sql(text):
    lines = text.splitlines()
    blocks = []
    current_block = []
    for line in lines:
        if SQL_LINE_STARTERS.match(line):
            current_block.append(line.strip())
        else:
            if current_block:
                joined = "\n".join(current_block)
                if len(current_block) > 1 or joined.rstrip().endswith(";"):
                    blocks.append(joined)
                current_block = []
    if current_block:
        joined = "\n".join(current_block)
        if len(current_block) > 1 or joined.rstrip().endswith(";"):
            blocks.append(joined)
    return "\n\n".join(blocks) if blocks else None


def extract_repro_sql(text):
    # Tier 1: {code} blocks
    blocks = extract_code_blocks(text)
    sql_blocks = []
    for block in blocks:
        block_type = classify_code_block(block)
        content = block["content"]
        if block_type in ("sql", "mtr") or (
            block_type == "unknown" and any(kw in content.upper() for kw in SQL_KEYWORDS)
        ):
            sql_blocks.append(content)
    if sql_blocks:
        return "\n\n".join(sql_blocks)

    # Tier 2: general.log session dumps
    sessions = extract_general_log_sessions(text)
    if sessions:
        parts = [f"-- Thread {s['thread_id']}\n{s['raw']}" for s in sessions]
        return "\n\n".join(parts)

    # Tier 3: plain text SQL fallback
    return extract_plain_text_sql(text)


def extract_stack_trace(text):
    for pattern in [
        r"Stack trace:(.*?)(\n[A-Z][^\n]*:|\Z)",
        r"stack trace:(.*?)(\n[A-Z][^\n]*:|\Z)",
    ]:
        match = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
        if match:
            return match.group(1).strip()

    crash_match = re.search(
        r"Attempting backtrace\..*?\n(.*?)(?=\nOptimizer switch:|\nWriting a core|\nConnection ID|\Z)",
        text, re.DOTALL | re.IGNORECASE,
    )
    if crash_match:
        trace = crash_match.group(1).strip()
        if trace:
            return trace

    thread_match = re.search(r"Thread \d+ .*?(?=\n\n|\Z)", text, re.DOTALL)
    if thread_match:
        return thread_match.group(0).strip()

    return None


def extract_crash_query(text):
    match = re.search(r"Query\s+\(0x[0-9a-f]+\):\s*(.+)", text, re.IGNORECASE)
    return match.group(1).strip() if match else None


def extract_assertions(text):
    assertions = []
    for pattern in ASSERTION_PATTERNS:
        for m in re.findall(pattern, text, re.IGNORECASE):
            assertions.append(m[0] if isinstance(m, tuple) else m)
    return list(set(assertions))


def extract_error_patterns(text):
    found = []
    for pattern in ERROR_PATTERNS:
        found.extend(re.findall(pattern, text, re.IGNORECASE))
    return list(set(found))


def detect_storage_engines(text):
    text_lower = text.lower()
    return [engine for engine, keywords in ENGINE_KEYWORDS.items()
            if any(k.lower() in text_lower for k in keywords)]


def detect_sql_keywords(sql):
    if not sql:
        return []
    sql_upper = sql.upper()
    return [kw for kw in SQL_KEYWORDS if kw in sql_upper]


def build_training_text(summary, description, repro_sql, stack_trace,
                         errors, assertions, engines, area, crash_query=None):
    crash_section = f"\nCRASH QUERY:\n{crash_query}" if crash_query else ""
    return normalize_text(f"""
SUMMARY:
{summary}

AREA:
{", ".join(area) if isinstance(area, list) else area}

DESCRIPTION:
{description}

SQL:
{repro_sql or ""}
{crash_section}

STACK TRACE:
{stack_trace or ""}

ERRORS:
{", ".join(errors)}

ASSERTIONS:
{", ".join(assertions)}

STORAGE ENGINES:
{", ".join(engines)}
""")


# =========================
# FETCH
# =========================

def fetch_single_issue(issue_key):
    url = f"{BASE_URL}/issue/{issue_key}"
    response = requests.get(url)
    if response.status_code != 200:
        print(f"Failed to fetch {issue_key}: {response.status_code}")
        return None
    return response.json()


def fetch_and_process_issues(processed_keys):
    """
    Streaming fetch — fetches one page at a time and yields each issue
    immediately for processing. Never holds more than one page in memory.
    Skips keys already in processed_keys (resume support).
    """
    url = f"{BASE_URL}/search"
    startAt = 0
    total_fetched = 0
    total_skipped = 0

    while True:
        params = {"jql": JQL, "maxResults": MAX_RESULTS, "startAt": startAt}
        try:
            response = requests.get(url, params=params, timeout=30)
            response.raise_for_status()
        except Exception as e:
            print(f"  [fetch error at offset {startAt}] {e} — retrying in 5s...")
            import time; time.sleep(5)
            continue

        data = response.json()
        issues = data.get("issues", [])
        if not issues:
            break

        for issue in issues:
            key = issue.get("key", "")
            if key in processed_keys:
                total_skipped += 1
                continue
            total_fetched += 1
            yield issue

        startAt += len(issues)
        print(f"  Processed {total_fetched} new | skipped {total_skipped} already done | offset {startAt}")


def load_processed_keys():
    """
    Read bug_dataset.jsonl and return the set of already-processed bug IDs.
    Used for resume support — avoids reprocessing bugs from a previous run.
    """
    keys = set()
    if not os.path.exists(DATASET_FILE):
        return keys
    with open(DATASET_FILE, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                if "bug_id" in record:
                    keys.add(record["bug_id"])
            except json.JSONDecodeError:
                continue
    return keys


# =========================
# SAVE
# =========================

def save_issue(issue):
    key = issue["key"]
    fields = issue["fields"]
    issue_dir = os.path.join(BASE_DIR, key)
    os.makedirs(issue_dir, exist_ok=True)

    # Basic fields
    summary = normalize_text(fields.get("summary", ""))
    description = normalize_text(get_description_text(fields.get("description", "")))

    # Extractions
    repro_sql    = extract_repro_sql(description)
    stack_trace  = extract_stack_trace(description)
    crash_query  = extract_crash_query(description)
    full_text    = f"{summary}\n\n{description}\n\n{repro_sql or ''}"
    errors       = extract_error_patterns(full_text)
    assertions   = extract_assertions(full_text)
    engines      = detect_storage_engines(full_text)
    sql_keywords = detect_sql_keywords(repro_sql or "")

    # Metadata
    components = [c["name"] for c in fields.get("components", []) if "name" in c]
    labels     = fields.get("labels", [])
    priority   = (fields.get("priority") or {}).get("name")
    status     = (fields.get("status") or {}).get("name")
    issue_type = (fields.get("issuetype") or {}).get("name")
    resolution = (fields.get("resolution") or {}).get("name")
    created    = fields.get("created")
    updated    = fields.get("updated")
    reporter   = (fields.get("reporter") or {}).get("displayName")
    assignee   = (fields.get("assignee") or {}).get("displayName")

    # Area identification
    area = identify_area(summary, description, stack_trace, components, engines)

    # Repro quality assessment (rule-based, always runs)
    quality = assess_repro_quality(description, repro_sql, stack_trace, crash_query, summary)

    # Cleanup low-quality repros via LLM
    repro_sql_cleaned = None
    if quality["score"] < CLEANUP_QUALITY_THRESHOLD:
        repro_sql_cleaned = cleanup_and_enrich_repro(
            bug_id=key, summary=summary, description=description,
            repro_sql=repro_sql, crash_query=crash_query,
            stack_trace=stack_trace, area=area, quality=quality,
        )

    # Use cleaned SQL for MTR generation if available, otherwise original
    repro_for_mtr = repro_sql_cleaned or repro_sql

    # MTR test generation
    mtr_test = generate_mtr_test(
        bug_id=key, summary=summary, description=description,
        repro_sql=repro_for_mtr, crash_query=crash_query,
        stack_trace=stack_trace, area=area, engines=engines,
    )

    # Training text
    training_text = build_training_text(
        summary, description, repro_sql, stack_trace,
        errors, assertions, engines, area, crash_query,
    )

    # Dataset record
    dataset_record = {
        "bug_id": key, "summary": summary, "description": description,
        "repro_sql": repro_sql, "repro_sql_cleaned": repro_sql_cleaned,
        "crash_query": crash_query,
        "stack_trace": stack_trace, "training_text": training_text,
        "area": area, "components": components, "labels": labels,
        "priority": priority, "status": status, "issue_type": issue_type,
        "resolution": resolution, "created": created, "updated": updated,
        "reporter": reporter, "assignee": assignee,
        "storage_engines": engines, "sql_keywords": sql_keywords,
        "errors": errors, "assertions": assertions,
        "repro_quality": quality,
        "mtr_generated": mtr_test is not None,
    }

    # Write files
    with open(os.path.join(issue_dir, "metadata.json"), "w") as f:
        json.dump(issue, f, indent=2)
    with open(os.path.join(issue_dir, "summary.txt"), "w") as f:
        f.write(summary)
    with open(os.path.join(issue_dir, "description.txt"), "w") as f:
        f.write(description)
    with open(os.path.join(issue_dir, "area.txt"), "w") as f:
        f.write("\n".join(area) + "\n")
    if repro_sql:
        with open(os.path.join(issue_dir, "repro.sql"), "w") as f:
            f.write(repro_sql + "\n")
    if crash_query:
        with open(os.path.join(issue_dir, "crash_query.sql"), "w") as f:
            f.write(crash_query + "\n")
    if stack_trace:
        with open(os.path.join(issue_dir, "stack_trace.txt"), "w") as f:
            f.write(stack_trace)
    if mtr_test:
        with open(os.path.join(issue_dir, "repro.test"), "w") as f:
            f.write(mtr_test + "\n")
    with open(os.path.join(issue_dir, "repro_quality.json"), "w") as f:
        json.dump(quality, f, indent=2)
    if repro_sql_cleaned:
        with open(os.path.join(issue_dir, "repro_cleaned.sql"), "w") as f:
            f.write(repro_sql_cleaned + "\n")
    with open(os.path.join(issue_dir, "training_text.txt"), "w") as f:
        f.write(training_text)
    with open(os.path.join(issue_dir, "ml_features.json"), "w") as f:
        json.dump(dataset_record, f, indent=2)

    with open(DATASET_FILE, "a") as f:
        f.write(json.dumps(dataset_record) + "\n")

    area_str    = ", ".join(area)
    quality_str = f"quality={quality['score']}/100 ({quality['level']})"
    mtr_str     = "repro.test generated" if mtr_test else f"no MTR (backend={MTR_BACKEND})"
    cleaned_str = " | repro_cleaned.sql written" if repro_sql_cleaned else ""
    print(f"Saved {key} | area={area_str} | {quality_str} | {mtr_str}{cleaned_str}")


# =========================
# MAIN
# =========================

def main():
    load_prompts()
    os.makedirs(BASE_DIR, exist_ok=True)

    # Parse simple flags: MDEV-XXXXX | --full | --days=N
    args = sys.argv[1:]
    full_fetch = "--full" in args
    days = 1
    for arg in args:
        if arg.startswith("--days="):
            try:
                days = int(arg.split("=")[1])
            except ValueError:
                pass
    issue_key = next((a for a in args if not a.startswith("--")), None)

    if issue_key:
        # --- Single issue mode ---
        if os.path.exists(DATASET_FILE):
            os.remove(DATASET_FILE)
        print(f"Fetching single issue: {issue_key}")
        issue = fetch_single_issue(issue_key)
        if issue:
            save_issue(issue)
    else:
        # --- Bulk mode ---
        if full_fetch:
            jql = "project=MDEV ORDER BY updated DESC"
            print("Full fetch mode — fetching all MDEV bugs...")
        else:
            jql = f"project=MDEV AND updated >= -{days}d ORDER BY updated DESC"
            print(f"Fetching bugs updated in the last {days} day(s)...")

        # Patch JQL for this run
        global JQL
        JQL = jql

        processed_keys = load_processed_keys()
        if processed_keys:
            print(f"Resuming — {len(processed_keys)} bugs already in dataset, skipping them.")

        for issue in fetch_and_process_issues(processed_keys):
            save_issue(issue)


if __name__ == "__main__":
    main()
