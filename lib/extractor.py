import re
from lib.config import SQL_KEYWORDS, ERROR_PATTERNS, ASSERTION_PATTERNS, ENGINE_KEYWORDS

# Regex: lines that start a SQL statement
SQL_LINE_STARTERS = re.compile(
    r"^\s*(SELECT|INSERT\s+INTO|UPDATE|DELETE\s+FROM|"
    r"CREATE\s+(TABLE|DATABASE|SEQUENCE|INDEX|VIEW|TRIGGER|PROCEDURE|FUNCTION)|"
    r"DROP\s+(TABLE|DATABASE|SEQUENCE|INDEX|VIEW)|ALTER\s+TABLE|TRUNCATE|"
    r"REPLACE\s+INTO|CALL|SET\s+(SESSION|GLOBAL|NAMES|CHARACTER)|"
    r"START\s+TRANSACTION|BEGIN|COMMIT|ROLLBACK|SAVEPOINT|"
    r"LOCK\s+TABLES|UNLOCK\s+TABLES|WITH\s+\w+\s+AS)\b",
    re.IGNORECASE,
)

# Prose signals that imply SQL ops without actual code
PROSE_SQL_SIGNALS = re.compile(
    r"\b(run|execute|issue|perform|do|try|trigger)\s+(a\s+)?"
    r"(SELECT|INSERT|UPDATE|DELETE|ALTER|CREATE|DROP|COMMIT|ROLLBACK|query|statement)\b"
    r"|\bsession\s+\d+\b|\bthread\s+\d+\b|\bconcurrently\b|\bsimultaneously\b",
    re.IGNORECASE,
)

TABLE_REF_PATTERN = re.compile(
    r"(?:FROM|JOIN|INTO|UPDATE)\s+`?(\w+)`?",
    re.IGNORECASE,
)

TABLE_DEF_PATTERN = re.compile(
    r"CREATE\s+(?:TEMPORARY\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?`?(\w+)`?",
    re.IGNORECASE,
)


def normalize_text(text):
    if not text:
        return ""
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def get_description_text(desc):
    """Convert JIRA ADF or plain string to plain text."""
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
    lang    = block.get("language", "")
    content = block.get("content", "")
    cu      = content.upper()

    if lang in ("sql", "mysql", "mariadb"):   return "sql"
    if lang in ("bash", "sh", "shell"):       return "shell"
    if lang in ("python", "py"):              return "python"
    if lang in ("perl", "pl"):                return "perl"

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
    if any(kw in cu for kw in SQL_KEYWORDS):
        return "sql"
    if re.search(r"\[\s*(ERROR|WARNING|Note)\s*\]", content):
        return "log"
    return "unknown"


def extract_general_log_sessions(text):
    """Parse general.log **<thread_id>** session blocks from plain text."""
    sessions       = []
    header_pattern = re.compile(r"general\.log\s+\**(\d+)\**", re.IGNORECASE)
    lines          = text.splitlines()
    i = 0
    while i < len(lines):
        m = header_pattern.search(lines[i])
        if m:
            thread_id = m.group(1)
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
                sessions.append({
                    "thread_id": thread_id,
                    "statements": sql_lines,
                    "raw": "\n".join(sql_lines),
                })
        else:
            i += 1
    return sessions


def extract_plain_text_sql(text):
    """Fallback: collect consecutive SQL-looking lines from plain text."""
    lines   = text.splitlines()
    blocks  = []
    current = []
    for line in lines:
        if SQL_LINE_STARTERS.match(line):
            current.append(line.strip())
        else:
            if current:
                joined = "\n".join(current)
                if len(current) > 1 or joined.rstrip().endswith(";"):
                    blocks.append(joined)
                current = []
    if current:
        joined = "\n".join(current)
        if len(current) > 1 or joined.rstrip().endswith(";"):
            blocks.append(joined)
    return "\n\n".join(blocks) if blocks else None


def extract_repro_sql(text):
    """
    Three-tier extraction:
      1. {code} blocks with SQL / MTR content
      2. general.log session dumps
      3. Plain text SQL fallback
    """
    # Tier 1
    blocks     = extract_code_blocks(text)
    sql_blocks = []
    for block in blocks:
        btype   = classify_code_block(block)
        content = block["content"]
        if btype in ("sql", "mtr") or (
            btype == "unknown" and any(kw in content.upper() for kw in SQL_KEYWORDS)
        ):
            sql_blocks.append(content)
    if sql_blocks:
        return "\n\n".join(sql_blocks)

    # Tier 2
    sessions = extract_general_log_sessions(text)
    if sessions:
        return "\n\n".join(f"-- Thread {s['thread_id']}\n{s['raw']}" for s in sessions)

    # Tier 3
    return extract_plain_text_sql(text)


def extract_stack_trace(text):
    """Handle Stack trace: label and MariaDB crash Attempting backtrace format."""
    for pattern in [
        r"Stack trace:(.*?)(\n[A-Z][^\n]*:|\Z)",
        r"stack trace:(.*?)(\n[A-Z][^\n]*:|\Z)",
    ]:
        m = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
        if m:
            return m.group(1).strip()

    crash = re.search(
        r"Attempting backtrace\..*?\n(.*?)(?=\nOptimizer switch:|\nWriting a core|\nConnection ID|\Z)",
        text, re.DOTALL | re.IGNORECASE,
    )
    if crash:
        trace = crash.group(1).strip()
        if trace:
            return trace

    thread = re.search(r"Thread \d+ .*?(?=\n\n|\Z)", text, re.DOTALL)
    if thread:
        return thread.group(0).strip()

    return None


def extract_crash_query(text):
    m = re.search(r"Query\s+\(0x[0-9a-f]+\):\s*(.+)", text, re.IGNORECASE)
    return m.group(1).strip() if m else None


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
    return [
        engine for engine, keywords in ENGINE_KEYWORDS.items()
        if any(k.lower() in text_lower for k in keywords)
    ]


def detect_sql_keywords(sql):
    if not sql:
        return []
    su = sql.upper()
    return [kw for kw in SQL_KEYWORDS if kw in su]
