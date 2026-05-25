import requests
import re
import json
import os
import sys
import csv
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
    "DROP TABLE",
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
]

ERROR_PATTERNS = [
    r"ERROR\s+\d+",
    r"SIGSEGV",
    r"Assertion.*",
    r"AddressSanitizer",
    r"LeakSanitizer",
    r"runtime error:",
    r"Deadlock found",
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
    Convert JIRA ADF/plain text into text
    """

    if isinstance(desc, str):
        return desc

    result = []

    def walk(node):
        if isinstance(node, dict):

            if "text" in node:
                result.append(node["text"])

            for v in node.values():
                walk(v)

        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(desc)

    return "\n".join(result)


def extract_code_blocks(text):

    matches = re.findall(
        r"\{code(?::[^\}]*)?\}(.*?)\{code\}",
        text,
        re.DOTALL | re.IGNORECASE,
    )

    return [m.strip() for m in matches]


def extract_repro_sql(text):

    blocks = extract_code_blocks(text)

    sql_blocks = []

    for block in blocks:

        upper = block.upper()

        if any(keyword in upper for keyword in SQL_KEYWORDS):
            sql_blocks.append(block)

    return "\n\n".join(sql_blocks) if sql_blocks else None


def extract_stack_trace(text):

    stack_patterns = [
        r"Stack trace:(.*?)(\n[A-Z][^\n]*:|\Z)",
        r"stack trace:(.*?)(\n[A-Z][^\n]*:|\Z)",
        r"Thread \d+ .*?(?=\n\n|\Z)",
    ]

    for pattern in stack_patterns:

        match = re.search(
            pattern,
            text,
            re.DOTALL | re.IGNORECASE,
        )

        if match:
            return match.group(1).strip()

    return None


def extract_assertions(text):

    assertions = []

    for pattern in ASSERTION_PATTERNS:

        matches = re.findall(
            pattern,
            text,
            re.IGNORECASE,
        )

        for m in matches:

            if isinstance(m, tuple):
                assertions.append(m[0])
            else:
                assertions.append(m)

    return list(set(assertions))


def extract_error_patterns(text):

    found = []

    for pattern in ERROR_PATTERNS:

        matches = re.findall(
            pattern,
            text,
            re.IGNORECASE,
        )

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

    found = []

    for keyword in SQL_KEYWORDS:

        if keyword in sql_upper:
            found.append(keyword)

    return found


def build_training_text(
    summary,
    description,
    repro_sql,
    stack_trace,
    errors,
    assertions,
    engines,
):

    return normalize_text(f"""
SUMMARY:
{summary}

DESCRIPTION:
{description}

SQL:
{repro_sql or ""}

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

    summary = normalize_text(
        fields.get("summary", "")
    )

    desc_raw = fields.get("description", "")

    description = normalize_text(
        get_description_text(desc_raw)
    )

    # ------------------------------------
    # EXTRACTIONS
    # ------------------------------------

    repro_sql = extract_repro_sql(description)

    stack_trace = extract_stack_trace(description)

    full_text = f"""
{summary}

{description}

{repro_sql or ""}
"""

    errors = extract_error_patterns(full_text)

    assertions = extract_assertions(full_text)

    engines = detect_storage_engines(full_text)

    sql_keywords = detect_sql_keywords(
        repro_sql or ""
    )

    # ------------------------------------
    # LABELS
    # ------------------------------------

    components = []

    for comp in fields.get("components", []):

        if "name" in comp:
            components.append(comp["name"])

    labels = fields.get("labels", [])

    priority = (
        fields.get("priority", {})
        .get("name")
    )

    status = (
        fields.get("status", {})
        .get("name")
    )

    issue_type = (
        fields.get("issuetype", {})
        .get("name")
    )

    resolution = (
        fields.get("resolution", {}) or {}
    ).get("name")

    created = fields.get("created")

    updated = fields.get("updated")

    reporter = (
        fields.get("reporter", {}) or {}
    ).get("displayName")

    assignee = (
        fields.get("assignee", {}) or {}
    ).get("displayName")

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
    )

    # ------------------------------------
    # DATASET RECORD
    # ------------------------------------

    dataset_record = {
        "bug_id": key,
        "summary": summary,
        "description": description,
        "repro_sql": repro_sql,
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

    with open(
        os.path.join(issue_dir, "metadata.json"),
        "w",
    ) as f:
        json.dump(issue, f, indent=2)

    with open(
        os.path.join(issue_dir, "summary.txt"),
        "w",
    ) as f:
        f.write(summary)

    with open(
        os.path.join(issue_dir, "description.txt"),
        "w",
    ) as f:
        f.write(description)

    if repro_sql:

        with open(
            os.path.join(issue_dir, "repro.sql"),
            "w",
        ) as f:
            f.write(repro_sql + "\n")

    if stack_trace:

        with open(
            os.path.join(issue_dir, "stack_trace.txt"),
            "w",
        ) as f:
            f.write(stack_trace)

    with open(
        os.path.join(issue_dir, "training_text.txt"),
        "w",
    ) as f:
        f.write(training_text)

    with open(
        os.path.join(issue_dir, "ml_features.json"),
        "w",
    ) as f:
        json.dump(dataset_record, f, indent=2)

    # append dataset
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
