import requests
import re
import json
import os
import sys
from collections import Counter

# =========================
# CONFIG
# =========================

BASE_URL = "https://jira.mariadb.org/rest/api/2"

JQL = "project=MDEV ORDER BY updated DESC"

MAX_RESULTS = 100

BASE_DIR = "bugs"

DATASET_FILE = "bug_dataset.jsonl"

# =========================
# KEYWORD MAPS
# =========================

ENGINE_KEYWORDS = {
    "InnoDB": [
        "innodb",
        "btr_cur",
        "trx_",
        "dict_table",
        "ibuf",
        "row_ins",
    ],
    "MyISAM": ["myisam"],
    "Aria": ["aria"],
    "Galera": ["galera", "wsrep"],
    "RocksDB": ["rocksdb"],
}

SQL_KEYWORDS = [
    "ALTER TABLE",
    "CREATE TABLE",
    "CREATE SEQUENCE",
    "DROP TABLE",
    "DROP SEQUENCE",
    "INSERT",
    "UPDATE",
    "DELETE",
    "SELECT",
    "FULLTEXT",
    "PARTITION",
    "WINDOW",
    "JSON",
    "TRIGGER",
    "VIEW",
    "CTE",
    "RECURSIVE",
    "INDEX",
    "SAVEPOINT",
    "COMMIT",
    "ROLLBACK",
    "SET SESSION",
    "SET GLOBAL",
    "START TRANSACTION",
    "BEGIN",
    "CALL",
    "REPLACE",
    "TRUNCATE",
    "LOCK TABLES",
    "UNLOCK TABLES",
]

# Keywords that strongly indicate a line is SQL
SQL_LINE_STARTERS = re.compile(
    r"^\s*(SELECT|INSERT\s+INTO|UPDATE|DELETE\s+FROM|CREATE\s+(TABLE|DATABASE|SEQUENCE|INDEX|VIEW|TRIGGER|PROCEDURE|FUNCTION)|"
    r"DROP\s+(TABLE|DATABASE|SEQUENCE|INDEX|VIEW)|ALTER\s+TABLE|TRUNCATE|REPLACE\s+INTO|CALL|"
    r"SET\s+(SESSION|GLOBAL|NAMES|CHARACTER)|START\s+TRANSACTION|BEGIN|COMMIT|ROLLBACK|"
    r"SAVEPOINT|LOCK\s+TABLES|UNLOCK\s+TABLES|WITH\s+\w+\s+AS)\b",
    re.IGNORECASE,
)

ERROR_PATTERNS = [
    r"ERROR\s+\d+",
    r"SIGSEGV",
    r"Assertion.*",
    r"AddressSanitizer",
    r"LeakSanitizer",
    r"runtime error:",
    r"Deadlock found",
    r"got signal \d+",
    r"WSREP.*FSM.*no such a transition",
]

ASSERTION_PATTERNS = [
    r"Assertion [`'\"]?(.*?)[`'\"]?( failed|$)",
]

# =========================
# HELPERS
# =========================

def normalize_text(text):
    if not text:
        return ""
    return (
        text.replace("\r\n", "\n")
        .replace("\r", "\n")
        .strip()
    )


def get_description_text(desc):
    """
    Convert JIRA ADF or plain text description into a plain string.
    For ADF (dict/list), only extracts 'text' nodes to avoid duplicating
    content from non-text fields.
    """
    if isinstance(desc, str):
        return desc

    result = []

    def walk(node):
        if isinstance(node, dict):
            # Only grab the 'text' key if this looks like a text node
            node_type = node.get("type", "")
            if node_type == "text" and "text" in node:
                result.append(node["text"])
            elif "text" in node and node_type == "":
                # plain dict with text key
                result.append(node["text"])
            # Recurse into content/children, not all values (avoids duplication)
            for key in ("content", "children"):
                if key in node:
                    walk(node[key])
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(desc)
    return "\n".join(result)


def extract_code_blocks(text):
    """Extract all {code}...{code} blocks, returning list of (language, content) tuples."""
    matches = re.findall(
        r"\{code(?::([^\}]*))?\}(.*?)\{code\}",
        text,
        re.DOTALL | re.IGNORECASE,
    )
    results = []
    for lang, content in matches:
        results.append({
            "language": lang.strip().lower() if lang else "unknown",
            "content": content.strip(),
        })
    return results


def classify_code_block(block):
    """
    Classify a code block as: sql, mtr, shell, python, perl, log, or unknown.
    Returns the type string.
    """
    lang = block.get("language", "")
    content = block.get("content", "")
    content_upper = content.upper()

    # Explicit language hint
    if lang in ("sql", "mysql", "mariadb"):
        return "sql"
    if lang in ("bash", "sh", "shell"):
        return "shell"
    if lang in ("python", "py"):
        return "python"
    if lang in ("perl", "pl"):
        return "perl"

    # MTR format detection: has connection/connect directives or send/reap
    if re.search(r"^(--\s*)?(connect|connection|disconnect|send|reap|let\s+\$)\b", content, re.MULTILINE | re.IGNORECASE):
        return "mtr"

    # Shell: has mysql -e or mysqld invocations
    if re.search(r"mysql\s+(-\w+\s+)*-e\s+[\"']", content, re.IGNORECASE):
        return "shell"
    if re.search(r"^#\s*!/bin/(ba)?sh", content, re.MULTILINE):
        return "shell"

    # Python threading patterns
    if re.search(r"import\s+threading|Thread\(", content):
        return "python"

    # Perl
    if re.search(r"use\s+DBI;|use\s+threads;", content):
        return "perl"

    # SQL: has SQL statements
    if any(kw in content_upper for kw in SQL_KEYWORDS):
        return "sql"

    # Log output
    if re.search(r"\[\s*(ERROR|WARNING|Note)\s*\]", content):
        return "log"

    return "unknown"


def extract_general_log_sessions(text):
    """
    Detect and parse the MariaDB general.log dump format embedded in plain text:

        general.log  **<thread_id>**
        SET SESSION autocommit=0;
        SELECT ...
        COMMIT

    Returns a list of dicts: {thread_id, statements, raw}
    """
    sessions = []

    # Match "general.log **<id>**" or "general.log <id>" as a session header
    header_pattern = re.compile(
        r"general\.log\s+\**(\d+)\**",
        re.IGNORECASE,
    )

    lines = text.splitlines()
    i = 0
    while i < len(lines):
        header_match = header_pattern.search(lines[i])
        if header_match:
            thread_id = header_match.group(1)
            i += 1
            sql_lines = []
            # Collect lines until blank line or another general.log header or stack-trace-like content
            while i < len(lines):
                line = lines[i]
                # Stop at blank lines followed by non-SQL content
                if line.strip() == "":
                    # Peek ahead: if next non-empty line is SQL, keep going; else stop
                    j = i + 1
                    while j < len(lines) and lines[j].strip() == "":
                        j += 1
                    if j < len(lines) and SQL_LINE_STARTERS.match(lines[j]):
                        i = j
                        continue
                    else:
                        break
                # Stop if we hit another general.log header
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
    """
    Fall-back: extract SQL statements from plain text when no {code} blocks exist.
    Groups consecutive SQL lines into blocks.
    """
    lines = text.splitlines()
    blocks = []
    current_block = []

    for line in lines:
        if SQL_LINE_STARTERS.match(line):
            current_block.append(line.strip())
        else:
            if current_block:
                # End of a SQL run — save if meaningful (more than one line or ends with ;)
                joined = "\n".join(current_block)
                if len(current_block) > 1 or joined.rstrip().endswith(";"):
                    blocks.append(joined)
                current_block = []

    # Catch trailing block
    if current_block:
        joined = "\n".join(current_block)
        if len(current_block) > 1 or joined.rstrip().endswith(";"):
            blocks.append(joined)

    return "\n\n".join(blocks) if blocks else None


def extract_repro_sql(text):
    """
    Extract repro SQL using a three-tier approach:
      1. {code} blocks with SQL content (explicit, highest confidence)
      2. general.log session dumps embedded in plain text
      3. Plain text SQL statement extraction (fallback)

    Returns a string of SQL (possibly multi-session) or None.
    """
    # --- Tier 1: {code} blocks ---
    blocks = extract_code_blocks(text)
    sql_blocks = []
    for block in blocks:
        block_type = classify_code_block(block)
        content = block["content"]
        content_upper = content.upper()
        if block_type in ("sql", "mtr") or (
            block_type == "unknown" and any(kw in content_upper for kw in SQL_KEYWORDS)
        ):
            sql_blocks.append(content)

    if sql_blocks:
        return "\n\n".join(sql_blocks)

    # --- Tier 2: general.log session format ---
    sessions = extract_general_log_sessions(text)
    if sessions:
        parts = []
        for s in sessions:
            parts.append(f"-- Thread {s['thread_id']}\n{s['raw']}")
        return "\n\n".join(parts)

    # --- Tier 3: plain text SQL fallback ---
    return extract_plain_text_sql(text)


def extract_stack_trace(text):
    """
    Extract stack trace from several common formats:
      - "Stack trace: ..." (generic)
      - MariaDB crash format: "Attempting backtrace..." block
      - Thread-based format
    """
    # Format 1: explicit "Stack trace:" label
    for pattern in [
        r"Stack trace:(.*?)(\n[A-Z][^\n]*:|\Z)",
        r"stack trace:(.*?)(\n[A-Z][^\n]*:|\Z)",
    ]:
        match = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
        if match:
            return match.group(1).strip()

    # Format 2: MariaDB crash dump format
    # Starts at "Attempting backtrace", ends before "Optimizer switch:" or "Writing a core"
    crash_match = re.search(
        r"Attempting backtrace\..*?\n(.*?)(?=\nOptimizer switch:|\nWriting a core|\nConnection ID|\Z)",
        text,
        re.DOTALL | re.IGNORECASE,
    )
    if crash_match:
        trace = crash_match.group(1).strip()
        if trace:
            return trace

    # Format 3: thread-based
    thread_match = re.search(
        r"Thread \d+ .*?(?=\n\n|\Z)",
        text,
        re.DOTALL,
    )
    if thread_match:
        return thread_match.group(0).strip()

    return None


def extract_crash_query(text):
    """
    Extract the query that was executing at crash time.
    MariaDB crash dumps include: Query (0x...): <SQL>
    """
    match = re.search(r"Query\s+\(0x[0-9a-f]+\):\s*(.+)", text, re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return None


def extract_assertions(text):
    assertions = []
    for pattern in ASSERTION_PATTERNS:
        matches = re.findall(pattern, text, re.IGNORECASE)
        for m in matches:
            if isinstance(m, tuple):
                assertions.append(m[0])
            else:
                assertions.append(m)
    return list(set(assertions))


def extract_error_patterns(text):
    found = []
    for pattern in ERROR_PATTERNS:
        matches = re.findall(pattern, text, re.IGNORECASE)
        found.extend(matches)
    return list(set(found))


def detect_storage_engines(text):
    text_lower = text.lower()
    engines = []
    for engine, keywords in ENGINE_KEYWORDS.items():
        if any(k.lower() in text_lower for k in keywords):
            engines.append(engine)
    return engines


def detect_sql_keywords(sql):
    if not sql:
        return []
    sql_upper = sql.upper()
    return [kw for kw in SQL_KEYWORDS if kw in sql_upper]


def build_training_text(
    summary,
    description,
    repro_sql,
    stack_trace,
    errors,
    assertions,
    engines,
    crash_query=None,
):
    crash_section = f"\nCRASH QUERY:\n{crash_query}" if crash_query else ""
    return normalize_text(f"""
SUMMARY:
{summary}

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


def fetch_multiple_issues():
    url = f"{BASE_URL}/search"
    startAt = 0
    all_issues = []

    while True:
        params = {
            "jql": JQL,
            "maxResults": MAX_RESULTS,
            "startAt": startAt,
        }
        response = requests.get(url, params=params)
        data = response.json()
        issues = data.get("issues", [])
        if not issues:
            break
        all_issues.extend(issues)
        startAt += len(issues)
        print(f"Fetched {len(all_issues)} issues...")

    return all_issues


# =========================
# SAVE
# =========================

def save_issue(issue):
    key = issue["key"]
    fields = issue["fields"]
    issue_dir = os.path.join(BASE_DIR, key)
    os.makedirs(issue_dir, exist_ok=True)

    # ------------------------------------
    # BASIC FIELDS
    # ------------------------------------

    summary = normalize_text(fields.get("summary", ""))
    desc_raw = fields.get("description", "")
    description = normalize_text(get_description_text(desc_raw))

    # ------------------------------------
    # EXTRACTIONS
    # ------------------------------------

    repro_sql = extract_repro_sql(description)
    stack_trace = extract_stack_trace(description)
    crash_query = extract_crash_query(description)

    full_text = f"{summary}\n\n{description}\n\n{repro_sql or ''}"

    errors = extract_error_patterns(full_text)
    assertions = extract_assertions(full_text)
    engines = detect_storage_engines(full_text)
    sql_keywords = detect_sql_keywords(repro_sql or "")

    # ------------------------------------
    # LABELS / METADATA
    # ------------------------------------

    components = [
        comp["name"]
        for comp in fields.get("components", [])
        if "name" in comp
    ]
    labels = fields.get("labels", [])
    priority = (fields.get("priority") or {}).get("name")
    status = (fields.get("status") or {}).get("name")
    issue_type = (fields.get("issuetype") or {}).get("name")
    resolution = (fields.get("resolution") or {}).get("name")
    created = fields.get("created")
    updated = fields.get("updated")
    reporter = (fields.get("reporter") or {}).get("displayName")
    assignee = (fields.get("assignee") or {}).get("displayName")

    # ------------------------------------
    # TRAINING TEXT
    # ------------------------------------

    training_text = build_training_text(
        summary,
        description,
        repro_sql,
        stack_trace,
        errors,
        assertions,
        engines,
        crash_query,
    )

    # ------------------------------------
    # DATASET RECORD
    # ------------------------------------

    dataset_record = {
        "bug_id": key,
        "summary": summary,
        "description": description,
        "repro_sql": repro_sql,
        "crash_query": crash_query,
        "stack_trace": stack_trace,
        "training_text": training_text,
        "components": components,
        "labels": labels,
        "priority": priority,
        "status": status,
        "issue_type": issue_type,
        "resolution": resolution,
        "created": created,
        "updated": updated,
        "reporter": reporter,
        "assignee": assignee,
        "storage_engines": engines,
        "sql_keywords": sql_keywords,
        "errors": errors,
        "assertions": assertions,
    }

    # ------------------------------------
    # WRITE FILES
    # ------------------------------------

    with open(os.path.join(issue_dir, "metadata.json"), "w") as f:
        json.dump(issue, f, indent=2)

    with open(os.path.join(issue_dir, "summary.txt"), "w") as f:
        f.write(summary)

    with open(os.path.join(issue_dir, "description.txt"), "w") as f:
        f.write(description)

    if repro_sql:
        with open(os.path.join(issue_dir, "repro.sql"), "w") as f:
            f.write(repro_sql + "\n")

    if crash_query:
        with open(os.path.join(issue_dir, "crash_query.sql"), "w") as f:
            f.write(crash_query + "\n")

    if stack_trace:
        with open(os.path.join(issue_dir, "stack_trace.txt"), "w") as f:
            f.write(stack_trace)

    with open(os.path.join(issue_dir, "training_text.txt"), "w") as f:
        f.write(training_text)

    with open(os.path.join(issue_dir, "ml_features.json"), "w") as f:
        json.dump(dataset_record, f, indent=2)

    # append to dataset
    with open(DATASET_FILE, "a") as f:
        f.write(json.dumps(dataset_record) + "\n")

    print(f"Saved {key}")


# =========================
# MAIN
# =========================

def main():
    os.makedirs(BASE_DIR, exist_ok=True)

    # clear old dataset
    if os.path.exists(DATASET_FILE):
        os.remove(DATASET_FILE)

    # ------------------------------------
    # SINGLE ISSUE
    # ------------------------------------

    if len(sys.argv) > 1:
        issue_key = sys.argv[1].strip()
        print(f"Fetching single issue: {issue_key}")
        issue = fetch_single_issue(issue_key)
        if issue:
            save_issue(issue)

    # ------------------------------------
    # MULTIPLE ISSUES
    # ------------------------------------

    else:
        print("Fetching multiple issues...")
        issues = fetch_multiple_issues()
        for issue in issues:
            save_issue(issue)


if __name__ == "__main__":
    main()
