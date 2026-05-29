import json
import os
from analyzer.config import BASE_DIR, DATASET_FILE, CLEANUP_QUALITY_THRESHOLD, MTR_BACKEND, VERBOSE
from analyzer.extractor import (
    normalize_text, get_description_text,
    extract_repro_sql, extract_stack_trace, extract_crash_query,
    extract_error_patterns, extract_assertions,
    detect_storage_engines, detect_sql_keywords,
)
from analyzer.classifier import identify_area
from analyzer.quality import assess_repro_quality, cleanup_and_enrich_repro
from analyzer.mtr_builder import generate_mtr_test


def build_training_text(summary, description, repro_sql, stack_trace,
                         errors, assertions, engines, area, crash_query=None):
    crash_sec = f"\nCRASH QUERY:\n{crash_query}" if crash_query else ""
    return normalize_text(f"""
SUMMARY:
{summary}

AREA:
{", ".join(area) if isinstance(area, list) else area}

DESCRIPTION:
{description}

SQL:
{repro_sql or ""}
{crash_sec}

STACK TRACE:
{stack_trace or ""}

ERRORS:
{", ".join(errors)}

ASSERTIONS:
{", ".join(assertions)}

STORAGE ENGINES:
{", ".join(engines)}
""")


def save_issue(issue):
    key    = issue["key"]
    fields = issue["fields"]
    issue_dir = os.path.join(BASE_DIR, key)
    os.makedirs(issue_dir, exist_ok=True)

    # Basic fields
    summary     = normalize_text(fields.get("summary", ""))
    description = normalize_text(get_description_text(fields.get("description", "")))

    # Extractions
    repro_sql   = extract_repro_sql(description)
    stack_trace = extract_stack_trace(description)
    crash_query = extract_crash_query(description)
    full_text   = f"{summary}\n\n{description}\n\n{repro_sql or ''}"
    errors      = extract_error_patterns(full_text)
    assertions  = extract_assertions(full_text)
    engines     = detect_storage_engines(full_text)
    sql_kw      = detect_sql_keywords(repro_sql or "")

    # Metadata
    components = [c["name"] for c in fields.get("components", []) if "name" in c]
    labels     = fields.get("labels", [])
    priority   = (fields.get("priority")   or {}).get("name")
    status     = (fields.get("status")     or {}).get("name")
    issue_type = (fields.get("issuetype")  or {}).get("name")
    resolution = (fields.get("resolution") or {}).get("name")
    created    = fields.get("created")
    updated    = fields.get("updated")
    reporter   = (fields.get("reporter")   or {}).get("displayName")
    assignee   = (fields.get("assignee")   or {}).get("displayName")

    # Area + quality
    area    = identify_area(summary, description, stack_trace, components, engines)
    quality = assess_repro_quality(description, repro_sql, stack_trace, crash_query, summary)

    # LLM cleanup for shaky repros
    repro_sql_cleaned = None
    if quality["score"] < CLEANUP_QUALITY_THRESHOLD:
        repro_sql_cleaned = cleanup_and_enrich_repro(
            bug_id=key, summary=summary, description=description,
            repro_sql=repro_sql, crash_query=crash_query,
            stack_trace=stack_trace, area=area, quality=quality,
        )

    repro_for_mtr = repro_sql_cleaned or repro_sql

    # MTR generation
    mtr_test = generate_mtr_test(
        bug_id=key, summary=summary, description=description,
        repro_sql=repro_for_mtr, crash_query=crash_query,
        stack_trace=stack_trace, area=area, engines=engines,
    )

    training_text = build_training_text(
        summary, description, repro_sql, stack_trace,
        errors, assertions, engines, area, crash_query,
    )

    dataset_record = {
        "bug_id": key, "summary": summary, "description": description,
        "repro_sql": repro_sql, "repro_sql_cleaned": repro_sql_cleaned,
        "crash_query": crash_query, "stack_trace": stack_trace,
        "training_text": training_text, "area": area,
        "components": components, "labels": labels,
        "priority": priority, "status": status, "issue_type": issue_type,
        "resolution": resolution, "created": created, "updated": updated,
        "reporter": reporter, "assignee": assignee,
        "storage_engines": engines, "sql_keywords": sql_kw,
        "errors": errors, "assertions": assertions,
        "repro_quality": quality,
        "mtr_generated": mtr_test is not None,
    }

    # Write files
    _w(issue_dir, "metadata.json",     json.dumps(issue, indent=2))
    _w(issue_dir, "summary.txt",       summary)
    _w(issue_dir, "description.txt",   description)
    _w(issue_dir, "area.txt",          "\n".join(area) + "\n")
    _w(issue_dir, "repro_quality.json",json.dumps(quality, indent=2))
    _w(issue_dir, "training_text.txt", training_text)
    _w(issue_dir, "ml_features.json",  json.dumps(dataset_record, indent=2))
    if repro_sql:        _w(issue_dir, "repro.sql",          repro_sql + "\n")
    if repro_sql_cleaned:_w(issue_dir, "repro_cleaned.sql",  repro_sql_cleaned + "\n")
    if crash_query:      _w(issue_dir, "crash_query.sql",    crash_query + "\n")
    if stack_trace:      _w(issue_dir, "stack_trace.txt",    stack_trace)
    if mtr_test:         _w(issue_dir, "repro.test",         mtr_test + "\n")

    with open(DATASET_FILE, "a") as f:
        f.write(json.dumps(dataset_record) + "\n")

    area_str    = ", ".join(area)
    quality_str = f"quality={quality['score']}/100 ({quality['level']})"
    mtr_str     = "repro.test generated" if mtr_test else f"no MTR (backend={MTR_BACKEND})"
    cleaned_str = " | repro_cleaned.sql written" if repro_sql_cleaned else ""
    print(f"Saved {key} | area={area_str} | {quality_str} | {mtr_str}{cleaned_str}")

    if VERBOSE:
        print(f"  issues        : {quality['issues'] or 'none'}")
        print(f"  sql extracted : {bool(repro_sql)} | crash_query: {bool(crash_query)} | stack: {bool(stack_trace)}")
        print(f"  engines       : {engines or 'none'}")
        print(f"  errors        : {errors or 'none'}")
        print(f"  components    : {components or 'none'}")


def _w(directory, filename, content):
    with open(os.path.join(directory, filename), "w") as f:
        f.write(content)
