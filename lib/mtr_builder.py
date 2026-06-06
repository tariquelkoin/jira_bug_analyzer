import re
from lib.config import MTR_BACKEND
from lib.extractor import (
    TABLE_DEF_PATTERN, TABLE_REF_PATTERN,
    extract_error_patterns,
)
from lib.llm import call_llm, strip_markdown_fences
import lib.prompts as P

# Blocking ops that need send/reap in multi-session context
BLOCKING_OPS = re.compile(
    r"^\s*(UPDATE\b|DELETE\s+FROM\b|SELECT\b.*\b(FOR\s+UPDATE|LOCK\s+IN\s+SHARE\s+MODE)\b|"
    r"LOCK\s+TABLES\b|ALTER\s+TABLE\b)",
    re.IGNORECASE | re.DOTALL,
)

TRANSACTION_BEGIN = re.compile(r"^\s*(BEGIN|START\s+TRANSACTION)\s*;?\s*$", re.IGNORECASE)
TRANSACTION_END   = re.compile(r"^\s*(COMMIT|ROLLBACK)\s*;?\s*$",           re.IGNORECASE)
ISOLATION_SET     = re.compile(r"^\s*SET\s+SESSION\s+TRANSACTION\s+ISOLATION", re.IGNORECASE)
CREATE_STMT       = re.compile(r"^\s*CREATE\s+(TEMPORARY\s+)?(TABLE|SEQUENCE)", re.IGNORECASE)
INSERT_STMT       = re.compile(r"^\s*(INSERT|REPLACE)\s+", re.IGNORECASE)


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
            f"# INFERRED: minimal schema for {table_name}\n"
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
        f"# INFERRED: schema for {table_name} based on SQL usage\n"
        f"CREATE TABLE IF NOT EXISTS `{table_name}` (\n"
        + ",\n".join(col_defs)
        + f"\n) ENGINE=InnoDB;\n"
    )


# -------------------------------------------------------
# SESSION SPLITTING
# -------------------------------------------------------

def _split_into_sessions(repro_sql):
    """
    Split repro SQL into sessions. Handles three marker formats:
      -- Thread <id>       (from general.log extraction)
      /* s1 */ / /* s2 */  (common in MariaDB JIRA tickets)
      # Session 1          (prose-style markers)
    Returns list of (session_name, [sql_lines]).
    Falls back to single session if no markers found.
    """
    thread_pat  = re.compile(r"^--\s*Thread\s+(\S+)", re.IGNORECASE)
    session_pat = re.compile(r"/\*\s*(s|session|con)\s*(\d+)\s*\*/", re.IGNORECASE)
    prose_pat   = re.compile(r"^#\s*(session|connection|thread|con)\s*(\d+)", re.IGNORECASE)
    init_pat    = re.compile(r"/\*\s*init\s*\*/", re.IGNORECASE)

    lines     = repro_sql.splitlines()
    sessions  = {}   # session_num -> [lines]
    cur_num   = None

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        # Skip /* init */ lines — these are setup, already handled
        if init_pat.search(stripped):
            continue

        # Check for -- Thread marker
        m = thread_pat.match(stripped)
        if m:
            cur_num = len(sessions) + 1
            sessions.setdefault(cur_num, [])
            continue

        # Check for /* s1 */ /* s2 */ inline marker
        m = session_pat.search(stripped)
        if m:
            cur_num = int(m.group(2))
            sessions.setdefault(cur_num, [])
            # Strip the marker from the line
            clean = session_pat.sub("", stripped).strip()
            if clean:
                sessions[cur_num].append(clean)
            continue

        # Check for # Session 1 style marker
        m = prose_pat.match(stripped)
        if m:
            cur_num = int(m.group(2))
            sessions.setdefault(cur_num, [])
            continue

        # No marker — append to current session
        if cur_num is not None:
            sessions[cur_num].append(stripped)
        else:
            # No session detected yet — goes to session 1
            sessions.setdefault(1, [])
            sessions[1].append(stripped)

    if not sessions or (len(sessions) == 1):
        # Single session fallback
        all_lines = [l.strip() for l in lines if l.strip()
                     and not init_pat.search(l)]
        return [("con1", all_lines)]

    return [(f"con{num}", lines) for num, lines in sorted(sessions.items())]


def _clean_llm_output(test_content):
    """
    Post-process LLM output to catch any remaining /* s1 */
    style markers and convert to proper MTR connection directives.
    """
    if not test_content:
        return test_content

    session_pat = re.compile(r"/\*\s*(s|session|con)\s*(\d+)\s*\*/\s*", re.IGNORECASE)
    init_pat    = re.compile(r"/\*\s*init\s*\*/\s*", re.IGNORECASE)

    lines     = test_content.splitlines()
    out       = []
    cur_con   = None

    for line in lines:
        # Remove /* init */ entirely
        if init_pat.search(line):
            clean = init_pat.sub("", line).strip()
            if clean:
                out.append(clean)
            continue

        m = session_pat.search(line)
        if m:
            con_num = int(m.group(2))
            con_name = f"con{con_num}"
            # Inject connection switch if changed
            if con_name != cur_con:
                out.append(f"connection {con_name};")
                cur_con = con_name
            # Strip marker and keep the rest of the line
            clean = session_pat.sub("", line).strip()
            if clean:
                out.append(clean)
        else:
            out.append(line)

    return "\n".join(out)


def _categorise_lines(session_lines):
    """
    Classify each SQL line as:
      setup    — SET SESSION, isolation level (runs directly, before BEGIN)
      begin    — BEGIN / START TRANSACTION
      blocking — UPDATE / DELETE / SELECT FOR UPDATE (needs send/reap)
      normal   — everything else inside transaction
      end      — COMMIT / ROLLBACK
    Skips CREATE TABLE / INSERT lines — already handled in schema/data sections.
    Returns list of (category, stmt).
    """
    result = []
    in_txn = False
    skip   = False   # for multi-line statements

    for stmt in session_lines:
        s = stmt.strip()
        if not s:
            continue

        # Skip schema and data lines — already emitted in setup sections
        if CREATE_STMT.match(s) or INSERT_STMT.match(s):
            skip = True
        if skip:
            if s.rstrip().endswith(";"):
                skip = False
            continue

        # Only add semicolon to single complete statements
        if not s.endswith(";") and not s.endswith(","):
            s += ";"

        if TRANSACTION_BEGIN.match(s):
            in_txn = True
            result.append(("begin", s))
        elif TRANSACTION_END.match(s):
            in_txn = False
            result.append(("end", s))
        elif ISOLATION_SET.match(s) or not in_txn:
            result.append(("setup", s))
        elif in_txn and BLOCKING_OPS.match(s):
            result.append(("blocking", s))
        else:
            result.append(("normal", s))

    return result


# -------------------------------------------------------
# STATEMENT COLLECTOR
# -------------------------------------------------------

def _collect_repro_statements(sql_text):
    """
    Return complete SQL statements from repro SQL, skipping CREATE/INSERT/USE/
    comment lines.  Multi-line statements (pretty-printed across many lines) are
    joined into a single string so only one trailing semicolon is added later.

    Statement boundaries are detected by: line ends with ';' AND cumulative paren
    depth == 0.  Unterminated trailing content is returned as-is (caller adds ';').
    """
    skip = (CREATE_STMT, INSERT_STMT, re.compile(r"^\s*USE\s+", re.IGNORECASE))
    result = []
    pending = []
    stmt_depth = 0
    skip_depth = 0
    skipping = False

    for line in sql_text.splitlines():
        s = line.strip()
        if not s or s.startswith(("--", "#")):
            continue

        if skipping:
            skip_depth += s.count("(") - s.count(")")
            if s.endswith(";") and skip_depth <= 0:
                skipping = False
                skip_depth = 0
            continue

        if any(p.match(s) for p in skip):
            if not s.endswith(";"):
                skipping = True
                skip_depth = s.count("(") - s.count(")")
            continue

        pending.append(line.rstrip())  # preserve indentation
        stmt_depth += s.count("(") - s.count(")")
        if s.endswith(";") and stmt_depth <= 0:
            result.append("\n".join(pending))
            pending = []
            stmt_depth = 0

    if pending:
        result.append("\n".join(pending))

    return result


# -------------------------------------------------------
# RULE-BASED MTR BUILDER
# -------------------------------------------------------

def build_mtr_from_rules(bug_id, summary, repro_sql, crash_query,
                          area, engines, description, errors):
    """
    Deterministic MTR skeleton builder following real MTR conventions:
      - connect/connection/disconnect for sessions
      - send/reap for blocking ops (one send at a time per connection)
      - Sessions properly interleaved: setup all → blocking ops → reap
      - --disable_warnings around CREATE TABLE
      - Full cleanup: DROP TABLE for each created table + DROP DATABASE

    Returns: {test, gaps, complete}
    """
    if not repro_sql and not crash_query:
        return {"test": None, "gaps": ["no_sql_available"], "complete": False}

    bug_id_safe = bug_id.replace("-", "_").lower()
    sql_text    = repro_sql or ""
    out         = []
    gaps        = []

    # --------------------------------------------------
    # HEADER
    # --------------------------------------------------
    out += [
        f"# {bug_id}: {summary}",
        f"# Area: {', '.join(area) if isinstance(area, list) else area}",
        f"# Engines: {', '.join(engines) if engines else 'unknown'}",
        "",
    ]

    # --------------------------------------------------
    # ENGINE INCLUDE
    # --------------------------------------------------
    engine_includes = {
        "InnoDB":  "include/have_innodb.inc",
        "Galera":  "include/have_wsrep.inc",
        "RocksDB": "include/have_rocksdb.inc",
    }
    for eng in (engines or []):
        if eng in engine_includes:
            out.append(f"--source {engine_includes[eng]}")
    if any(eng in engine_includes for eng in (engines or [])):
        out.append("")

    # --------------------------------------------------
    # SETUP — database
    # --------------------------------------------------
    out += [
        "--echo # Setup",
        f"CREATE DATABASE IF NOT EXISTS test_{bug_id_safe};",
        f"USE test_{bug_id_safe};",
        "",
    ]

    # --------------------------------------------------
    # SCHEMA — existing CREATE statements
    # --------------------------------------------------
    defined    = set(t.lower() for t in TABLE_DEF_PATTERN.findall(sql_text))
    referenced = set(t.lower() for t in TABLE_REF_PATTERN.findall(sql_text))
    ignore_tbl = {"dual", "information_schema", "performance_schema", "mysql"}
    undefined  = (referenced - defined) - ignore_tbl
    all_tables = defined | undefined   # everything we need to clean up

    creates = re.findall(
        r"(CREATE\s+(?:TEMPORARY\s+)?(?:TABLE|SEQUENCE)[^;]+;)",
        sql_text, re.IGNORECASE | re.DOTALL,
    )
    if creates:
        out += ["--echo # Schema", "--disable_warnings"]
        for stmt in creates:
            out += [stmt.strip(), ""]
        out.append("--enable_warnings")
        out.append("")

    # Infer schema for undefined tables
    if undefined:
        out += ["--echo # Inferred schema", "--disable_warnings"]
        for tbl in sorted(undefined):
            out.append(_infer_schema_for_table(tbl, sql_text))
        out += ["--enable_warnings", ""]
        gaps.append(f"inferred_schema:{','.join(sorted(undefined))}")

    # --------------------------------------------------
    # DATA SETUP — extract INSERT statements
    # --------------------------------------------------
    insert_lines = [
        l.strip() for l in sql_text.splitlines()
        if INSERT_STMT.match(l.strip())
    ]
    if insert_lines:
        out.append("--echo # Test data")
        for ins in insert_lines:
            if not ins.endswith(";"): ins += ";"
            out.append(ins)
        out.append("")

    # --------------------------------------------------
    # DETECT SESSIONS
    # --------------------------------------------------
    sessions   = _split_into_sessions(sql_text)
    is_multi   = (
        len(sessions) > 1
        or bool(re.search(
            r"\bsession\s+\d+\b|\bthread\s+\d+\b|\bconcurrent\b|\bsimultaneously\b",
            description or "", re.IGNORECASE,
        ))
    )

    # --------------------------------------------------
    # ERROR DIRECTIVE
    # --------------------------------------------------
    has_crash = bool(crash_query)
    error_dir = ""
    if has_crash or any(
        re.search(p, " ".join(errors), re.IGNORECASE)
        for p in [r"SIGSEGV", r"got signal", r"Assertion"]
    ):
        error_dir = "--error 0  # NOTE: replace with actual ER_ error code"
        gaps.append("error_code_unknown")

    # --------------------------------------------------
    # SINGLE SESSION
    # --------------------------------------------------
    if not is_multi:
        out.append("--echo # Reproduction")
        for stmt in _collect_repro_statements(sql_text):
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

    # --------------------------------------------------
    # MULTI SESSION — proper interleaving
    #
    # Pattern:
    #   1. Open all connections
    #   2. Each connection: run setup stmts (SET SESSION etc) + BEGIN
    #   3. Interleave: con1 send blocking → con2 runs → con1 reap → COMMIT
    #   4. Disconnect all, connection default
    # --------------------------------------------------
    else:
        con_names = [s[0] for s in sessions]

        # Open connections
        out += ["--echo # Open connections"]
        for con in con_names:
            out.append(f"connect ({con},localhost,root,,test_{bug_id_safe});")
        out.append("")

        # Categorise each session's lines
        categorised = []
        for con_name, lines in sessions:
            categorised.append((con_name, _categorise_lines(lines)))

        # Phase 1: setup + BEGIN for all sessions
        out.append("--echo # Session setup")
        for con_name, cats in categorised:
            setup_stmts = [s for cat, s in cats if cat == "setup"]
            begin_stmts = [s for cat, s in cats if cat == "begin"]
            if setup_stmts or begin_stmts:
                out.append(f"connection {con_name};")
                for s in setup_stmts:
                    out.append(s)
                for s in begin_stmts:
                    out.append(s)
        out.append("")

        # Phase 2: blocking ops interleaved across sessions
        # Strategy: for each session that has blocking ops, send them one at a time,
        # let other sessions run, then reap
        has_blocking = any(
            any(cat == "blocking" for cat, _ in cats)
            for _, cats in categorised
        )

        if has_blocking:
            out.append("--echo # Concurrent operations")

            # Collect blocking and normal ops per session (post-BEGIN, pre-COMMIT)
            session_ops = []
            for con_name, cats in categorised:
                ops = [(cat, s) for cat, s in cats if cat in ("blocking", "normal")]
                session_ops.append((con_name, ops))

            # Interleave: send from con1, run con2, reap con1
            # Simple two-session interleave — generalises for N sessions
            pending_sends = {}  # con_name -> stmt

            # First pass: send all blocking ops from first session
            for con_name, ops in session_ops:
                for cat, stmt in ops:
                    if cat == "blocking":
                        out.append(f"connection {con_name};")
                        out.append(f"send {stmt}")
                        pending_sends[con_name] = stmt
                        gaps.append(f"send_reap:{con_name}:{stmt[:50]}")
                        # Let other sessions run
                        for other_name, other_ops in session_ops:
                            if other_name != con_name:
                                out.append(f"connection {other_name};")
                                for ocat, ostmt in other_ops:
                                    if ocat == "normal":
                                        out.append(ostmt)
                        # Reap
                        out.append(f"connection {con_name};")
                        out.append("reap;")
                        del pending_sends[con_name]
                    elif cat == "normal" and con_name not in pending_sends:
                        out.append(f"connection {con_name};")
                        out.append(stmt)
            out.append("")

        else:
            # No blocking ops — just run each session sequentially
            out.append("--echo # Operations")
            for con_name, cats in categorised:
                ops = [(cat, s) for cat, s in cats if cat in ("normal",)]
                if ops:
                    out.append(f"connection {con_name};")
                    for _, stmt in ops:
                        out.append(stmt)
            out.append("")

        # Phase 3: COMMIT all sessions
        out.append("--echo # Commit all sessions")
        for con_name, cats in categorised:
            commits = [s for cat, s in cats if cat == "end"]
            if commits:
                out.append(f"connection {con_name};")
                for s in commits:
                    out.append(s)
        out.append("")

        # Phase 4: Disconnect + return to default
        out += ["--echo # Disconnect"]
        for con in con_names:
            out.append(f"disconnect {con};")
        out += ["connection default;", ""]

    # --------------------------------------------------
    # CLEANUP — drop all tables then database
    # --------------------------------------------------
    out.append("--echo # Cleanup")
    for tbl in sorted(all_tables):
        out.append(f"DROP TABLE IF EXISTS `{tbl}`;")
    out.append(f"DROP DATABASE IF EXISTS test_{bug_id_safe};")

    test_content = "\n".join(out)
    complete     = len(gaps) == 0 or gaps == [f"inferred_schema:{','.join(sorted(undefined))}"]

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
    from lib.config import VERBOSE
    errors = extract_error_patterns((description or "") + " " + (stack_trace or ""))

    rule = build_mtr_from_rules(
        bug_id, summary, repro_sql, crash_query,
        area, engines, description, errors,
    )

    if rule["complete"] or MTR_BACKEND == "none":
        if VERBOSE:
            status = "complete" if rule["complete"] else "rules-only (backend=none)"
            print(f"  MTR built by rules ({status}) | gaps: {rule['gaps'] or 'none'}")
        return rule["test"]

    if VERBOSE:
        print(f"  MTR rule skeleton built | gaps: {rule['gaps']}")

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
        return _clean_llm_output(strip_markdown_fences(result))

    if VERBOSE:
        print("  LLM failed — returning rule-based skeleton")
    return rule["test"]
