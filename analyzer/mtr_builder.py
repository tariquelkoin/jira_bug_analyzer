import re
from analyzer.config import MTR_BACKEND
from analyzer.extractor import (
    TABLE_DEF_PATTERN, TABLE_REF_PATTERN,
    extract_error_patterns,
)
from analyzer.llm import call_llm, strip_markdown_fences
import analyzer.prompts as P

# Blocking ops that need send/reap in multi-session context
BLOCKING_OPS = re.compile(
    r"^\s*(UPDATE|DELETE\s+FROM|SELECT\s+.*?(FOR\s+UPDATE|LOCK\s+IN\s+SHARE\s+MODE)|"
    r"LOCK\s+TABLES|ALTER\s+TABLE)\b",
    re.IGNORECASE,
)
TRANSACTION_BEGIN = re.compile(r"^\s*(BEGIN|START\s+TRANSACTION)\s*;?\s*$", re.IGNORECASE)
TRANSACTION_END   = re.compile(r"^\s*(COMMIT|ROLLBACK)\s*;?\s*$",           re.IGNORECASE)


# -------------------------------------------------------
# SCHEMA INFERENCE
# -------------------------------------------------------

def _infer_col_type(col_name):
    name = col_name.lower()
    if any(k in name for k in ("id", "num", "count", "seq", "age", "size")): return "INT"
    if any(k in name for k in ("date", "time", "created", "updated")):        return "DATETIME"
    if any(k in name for k in ("flag", "active", "enabled", "ind")):          return "TINYINT(1)"
    return "VARCHAR(255)"


def _infer_schema_for_table(table_name, sql_text):
    cols_seen = set()

    for m in re.finditer(
        rf"UPDATE\s+`?{re.escape(table_name)}`?\s+SET\s+([\w\s,=`'\"]+?)(?:\s+WHERE|\s*;|$)",
        sql_text, re.IGNORECASE,
    ):
        for part in m.group(1).split(","):
            col = part.strip().split("=")[0].strip().strip("`")
            if col and re.match(r"^\w+$", col):
                cols_seen.add(col)

    for m in re.finditer(
        rf"(?:FROM|UPDATE|JOIN)\s+`?{re.escape(table_name)}`?.*?WHERE\s+([\w\s,=<>!`'\"]+?)"
        r"(?:\s+(?:ORDER|LIMIT|GROUP|HAVING|JOIN|AND|OR)|\s*;|$)",
        sql_text, re.IGNORECASE,
    ):
        for part in re.split(r"\s+AND\s+|\s+OR\s+", m.group(1), flags=re.IGNORECASE):
            col = part.strip().split("=")[0].strip().strip("`")
            if col and re.match(r"^\w+$", col):
                cols_seen.add(col)

    for m in re.finditer(
        rf"INSERT\s+(?:IGNORE\s+)?INTO\s+`?{re.escape(table_name)}`?\s*\(([\w\s,`]+)\)",
        sql_text, re.IGNORECASE,
    ):
        for col in m.group(1).split(","):
            col = col.strip().strip("`")
            if col and re.match(r"^\w+$", col):
                cols_seen.add(col)

    if not cols_seen:
        return (
            f"-- INFERRED: minimal schema for {table_name}\n"
            f"CREATE TABLE IF NOT EXISTS `{table_name}` (\n"
            f"  id INT PRIMARY KEY AUTO_INCREMENT\n"
            f") ENGINE=InnoDB;\n"
        )

    col_defs = []
    has_pk   = False
    for col in sorted(cols_seen):
        col_type = _infer_col_type(col)
        if col.lower() in ("id", f"{table_name.lower()}_id") and not has_pk:
            col_defs.append(f"  `{col}` {col_type} PRIMARY KEY")
            has_pk = True
        else:
            col_defs.append(f"  `{col}` {col_type} DEFAULT NULL")

    if not has_pk:
        col_defs.insert(0, "  `id` INT PRIMARY KEY AUTO_INCREMENT")

    return (
        f"-- INFERRED: schema for {table_name} based on SQL usage\n"
        f"CREATE TABLE IF NOT EXISTS `{table_name}` (\n"
        + ",\n".join(col_defs)
        + f"\n) ENGINE=InnoDB;\n"
    )


# -------------------------------------------------------
# SESSION SPLITTING
# -------------------------------------------------------

def _split_into_sessions(repro_sql):
    """
    Split repro SQL into sessions using -- Thread <id> markers.
    Falls back to single session if no markers found.
    """
    thread_pat = re.compile(r"^--\s*Thread\s+(\S+)", re.IGNORECASE)
    lines      = repro_sql.splitlines()
    sessions   = []
    cur_label  = None
    cur_lines  = []

    for line in lines:
        if thread_pat.match(line.strip()):
            if cur_lines:
                sessions.append((cur_label or "con1", cur_lines))
            cur_label = f"con{len(sessions) + 1}"
            cur_lines = []
        else:
            if line.strip():
                cur_lines.append(line.strip())

    if cur_lines:
        sessions.append((cur_label or "con1", cur_lines))

    return sessions if sessions else [("con1", [l.strip() for l in lines if l.strip()])]


# -------------------------------------------------------
# RULE-BASED MTR BUILDER
# -------------------------------------------------------

def build_mtr_from_rules(bug_id, summary, repro_sql, crash_query,
                          area, engines, description, errors):
    """
    Deterministic MTR skeleton builder.

    Returns:
      test      str  — .test file content
      gaps      list — things the rules couldn't handle
      complete  bool — True if no LLM needed
    """
    if not repro_sql and not crash_query:
        return {"test": None, "gaps": ["no_sql_available"], "complete": False}

    bug_id_safe = bug_id.replace("-", "_").lower()
    sql_text    = repro_sql or ""
    out         = []
    gaps        = []

    # Header
    out += [
        f"# {bug_id}: {summary}",
        f"# Area: {', '.join(area) if isinstance(area, list) else area}",
        f"# Engines: {', '.join(engines) if engines else 'unknown'}",
        "",
    ]

    # Setup
    out += [
        "--echo # Setup",
        f"CREATE DATABASE IF NOT EXISTS test_{bug_id_safe};",
        f"USE test_{bug_id_safe};",
        "",
    ]

    # Schema
    defined    = set(t.lower() for t in TABLE_DEF_PATTERN.findall(sql_text))
    referenced = set(t.lower() for t in TABLE_REF_PATTERN.findall(sql_text))
    ignore     = {"dual", "information_schema", "performance_schema", "mysql"}
    undefined  = (referenced - defined) - ignore

    creates = re.findall(
        r"(CREATE\s+(?:TEMPORARY\s+)?(?:TABLE|SEQUENCE)[^;]+;)",
        sql_text, re.IGNORECASE | re.DOTALL,
    )
    if creates:
        out.append("--echo # Schema")
        for stmt in creates:
            out += [stmt.strip(), ""]

    if undefined:
        out.append("--echo # Inferred schema for undefined tables")
        for tbl in sorted(undefined):
            out.append(_infer_schema_for_table(tbl, sql_text))
        gaps.append(f"inferred_schema:{','.join(sorted(undefined))}")

    # Error directive
    has_crash = bool(crash_query)
    error_dir = ""
    if has_crash or any(
        re.search(p, " ".join(errors), re.IGNORECASE)
        for p in [r"SIGSEGV", r"got signal", r"Assertion"]
    ):
        error_dir = "--error 0  # NOTE: replace with actual error code"
        gaps.append("error_code_unknown")

    # Detect sessions
    sessions = _split_into_sessions(sql_text)
    is_multi = (
        len(sessions) > 1
        or bool(re.search(
            r"\bsession\s+\d+\b|\bthread\s+\d+\b|\bconcurrent\b|\bsimultaneously\b",
            description or "", re.IGNORECASE,
        ))
    )

    # Single session
    if not is_multi:
        out.append("--echo # Reproduction")
        repro_lines = [
            l.strip() for l in sql_text.splitlines()
            if l.strip()
            and not re.match(r"CREATE\s+(TABLE|SEQUENCE|DATABASE)", l.strip(), re.IGNORECASE)
            and not re.match(r"USE\s+", l.strip(), re.IGNORECASE)
            and not l.strip().startswith("--")
        ]
        for stmt in repro_lines:
            if not stmt.endswith(";"): stmt += ";"
            if error_dir and crash_query and stmt.upper().startswith(crash_query.upper()[:10]):
                out.append(error_dir)
            out.append(stmt)

        if crash_query and crash_query.strip() not in sql_text:
            out += ["", "--echo # Crash trigger"]
            if error_dir:
                out.append(error_dir)
            cq = crash_query.strip()
            if not cq.endswith(";"): cq += ";"
            out.append(cq)

    # Multi session
    else:
        con_names = [s[0] for s in sessions]

        out.append("--echo # Open connections")
        for con in con_names:
            out.append(f"connect ({con},localhost,root,,test_{bug_id_safe});")
        out.append("")

        for con_name, session_lines in sessions:
            out += [f"--echo # Session {con_name}", f"connection {con_name};"]
            in_txn = False
            for stmt in session_lines:
                if not stmt.endswith(";"): stmt += ";"
                if TRANSACTION_BEGIN.match(stmt): in_txn = True
                elif TRANSACTION_END.match(stmt): in_txn = False

                if in_txn and BLOCKING_OPS.match(stmt):
                    out.append(f"send {stmt}")
                    gaps.append(f"send_reap_needed:{con_name}:{stmt[:40]}")
                else:
                    if error_dir and crash_query and stmt.upper().startswith(crash_query.upper()[:10]):
                        out.append(error_dir)
                    out.append(stmt)
            out.append("")

        if any("send_reap_needed" in g for g in gaps):
            out.append("--echo # Collect blocking results")
            for con_name, _ in sessions:
                out += [f"connection {con_name};", "reap;"]
            out.append("")

        out.append("--echo # Disconnect")
        for con in con_names:
            out.append(f"disconnect {con};")
        out += ["connection default;", ""]

    # Cleanup
    out.append("--echo # Cleanup")
    for tbl in sorted(defined | undefined):
        out.append(f"DROP TABLE IF EXISTS `{tbl}`;")
    out.append(f"DROP DATABASE IF EXISTS test_{bug_id_safe};")

    test_content = "\n".join(out)
    only_schema_gap = gaps == [f"inferred_schema:{','.join(sorted(undefined))}"]
    complete = len(gaps) == 0 or only_schema_gap

    return {"test": test_content, "gaps": gaps, "complete": complete}


# -------------------------------------------------------
# HYBRID GENERATOR
# -------------------------------------------------------

def generate_mtr_test(bug_id, summary, description, repro_sql,
                      crash_query, stack_trace, area, engines):
    """
    Hybrid MTR generation:
      1. Rule-based builder always runs first
      2. Complete skeleton → return immediately, no LLM call
      3. Gaps exist + backend available → LLM fixes skeleton
      4. backend=none → return rule skeleton regardless
      5. LLM fails → return rule skeleton as fallback
    """
    from analyzer.config import VERBOSE
    errors = extract_error_patterns((description or "") + " " + (stack_trace or ""))

    rule = build_mtr_from_rules(
        bug_id, summary, repro_sql, crash_query,
        area, engines, description, errors,
    )

    if rule["complete"] or MTR_BACKEND == "none":
        if VERBOSE:
            status = "complete" if rule["complete"] else "rules-only (no LLM)"
            print(f"  MTR built by rules ({status}) | gaps: {rule['gaps'] or 'none'}")
        return rule["test"]

    if VERBOSE:
        print(f"  MTR rule skeleton built | gaps: {rule['gaps']}")

    # LLM fills the gaps
    bug_id_safe = bug_id.replace("-", "_").lower()
    desc_limit  = 600  if MTR_BACKEND == "ollama" else 1500
    stack_limit = 300  if MTR_BACKEND == "ollama" else 800

    skeleton_ctx = f"\nRule-based skeleton (fix and complete):\n{rule['test']}\n" if rule["test"] else ""
    gaps_ctx     = f"\nKnown gaps to fix: {', '.join(rule['gaps'])}\n"

    prompt = P.MTR_USER_PROMPT_TEMPLATE.format(
        bug_id=bug_id,
        bug_id_safe=bug_id_safe,
        summary=summary,
        area=", ".join(area) if isinstance(area, list) else area,
        engines=", ".join(engines) if engines else "unknown",
        description=(description or "")[:desc_limit],
        repro_sql=repro_sql or "(none extracted)",
        crash_query=crash_query or "(none)",
        stack_trace=(stack_trace or "")[:stack_limit],
    ) + skeleton_ctx + gaps_ctx

    print(f"  Completing MTR via [{MTR_BACKEND}] | gaps: {rule['gaps']}...")
    result = call_llm(prompt, P.MTR_SYSTEM_PROMPT)

    if result:
        return strip_markdown_fences(result)

    if VERBOSE:
        print("  LLM failed — returning rule-based skeleton")
    return rule["test"]
