import re
import requests
from analyzer.config import (
    MTR_BACKEND, CLEANUP_QUALITY_THRESHOLD,
    CLAUDE_API_KEY, CLAUDE_MODEL, OPENAI_API_KEY, OPENAI_MODEL,
    OLLAMA_HOST, OLLAMA_MODEL, OLLAMA_TIMEOUT,
)
from analyzer.extractor import SQL_LINE_STARTERS, TABLE_DEF_PATTERN, TABLE_REF_PATTERN, PROSE_SQL_SIGNALS
from analyzer.llm import call_llm, strip_markdown_fences
import analyzer.prompts as P


def assess_repro_quality(description, repro_sql, stack_trace, crash_query, summary):
    """
    Rule-based repro quality scorer (0-100) across 8 dimensions.
    Returns dict: score, level, issues, details.
    """
    issues  = {}
    details = {}
    score   = 0
    sql     = repro_sql or ""
    desc    = description or ""

    # 1. Has SQL (30)
    if sql.strip():
        score += 30
        details["has_sql"] = True
    else:
        details["has_sql"] = False
        if PROSE_SQL_SIGNALS.search(desc):
            issues["prose_only"] = True
        else:
            issues["no_sql"] = True
        details["prose_signals"] = bool(PROSE_SQL_SIGNALS.search(desc))

    # 2. Has schema (15)
    has_schema = bool(re.search(
        r"\bCREATE\s+(TEMPORARY\s+)?TABLE\b|\bCREATE\s+SEQUENCE\b",
        sql, re.IGNORECASE,
    ))
    details["has_schema"] = has_schema
    if has_schema:
        score += 15
    else:
        issues["no_schema"] = True

    # 3. Has data setup (10)
    has_data = bool(re.search(r"\bINSERT\s+INTO\b|\bREPLACE\s+INTO\b", sql, re.IGNORECASE))
    details["has_data_setup"] = has_data
    if has_data:
        score += 10
    else:
        issues["no_data_setup"] = True

    # 4. Has trigger query (10)
    has_trigger = bool(re.search(
        r"\b(SELECT|UPDATE|DELETE\s+FROM|ALTER\s+TABLE|COMMIT|ROLLBACK)\b",
        sql, re.IGNORECASE,
    ))
    details["has_trigger_query"] = has_trigger
    if has_trigger:
        score += 10
    else:
        issues["no_trigger_query"] = True

    # 5. Table ref consistency (10)
    defined    = set(t.lower() for t in TABLE_DEF_PATTERN.findall(sql))
    referenced = set(t.lower() for t in TABLE_REF_PATTERN.findall(sql))
    ignore     = {"dual", "information_schema", "performance_schema", "mysql"}
    undefined  = (referenced - defined) - ignore
    details["defined_tables"]    = list(defined)
    details["referenced_tables"] = list(referenced)
    details["undefined_tables"]  = list(undefined)
    if sql.strip() and undefined:
        score += max(0, 10 - len(undefined) * 3)
        issues[f"undefined_tables"] = sorted(undefined)
    elif sql.strip():
        score += 10

    # 6. Semicolons (5)
    if sql.strip():
        sql_lines = [l.strip() for l in sql.splitlines()
                     if l.strip() and not l.strip().startswith(("--", "#"))]
        ratio = sum(1 for l in sql_lines if l.endswith(";")) / len(sql_lines) if sql_lines else 0
        details["semicolon_ratio"] = round(ratio, 2)
        if ratio >= 0.5:
            score += 5
        else:
            issues["missing_semicolons"] = True
    else:
        details["semicolon_ratio"] = 0.0

    # 7. Error context (15)
    has_stack  = bool(stack_trace and stack_trace.strip())
    has_crash  = bool(crash_query and crash_query.strip())
    has_errmsg = bool(re.search(r"ERROR\s+\d+|SIGSEGV|Assertion|got signal", desc, re.IGNORECASE))
    ctx = min((8 if has_stack else 0) + (4 if has_crash else 0) + (3 if has_errmsg else 0), 15)
    score += ctx
    details.update(has_stack_trace=has_stack, has_crash_query=has_crash, has_error_message=has_errmsg)
    if not (has_stack or has_crash or has_errmsg):
        issues["no_error_context"] = True

    # 8. Description length (5)
    wc = len(desc.split())
    details["description_word_count"] = wc
    if wc >= 100:
        score += 5
    elif wc >= 30:
        score += 3
    else:
        issues["very_short_description"] = True

    # Level
    if score >= 75:   level = "good"
    elif score >= 50: level = "partial"
    elif score >= 25: level = "shaky"
    elif "prose_only" in issues: level = "prose_only"
    else:             level = "none"

    return {
        "score":   score,
        "level":   level,
        "issues":  list(issues.keys()),
        "details": details,
    }


def cleanup_and_enrich_repro(bug_id, summary, description, repro_sql,
                              crash_query, stack_trace, area, quality):
    """
    LLM cleanup for shaky repros. Only called when quality score < threshold.
    Returns cleaned SQL string or None.
    """
    if MTR_BACKEND == "none":
        return None

    prompt = P.CLEANUP_USER_PROMPT_TEMPLATE.format(
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
    result = call_llm(prompt, P.CLEANUP_SYSTEM_PROMPT)
    return strip_markdown_fences(result) if result else None
