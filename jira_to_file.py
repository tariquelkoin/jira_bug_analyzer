import os
import sys
import analyzer.config as config
from analyzer.prompts import load_prompts
from analyzer.fetcher import fetch_single_issue, fetch_and_process_issues, load_processed_keys
from analyzer.dataset import save_issue

HELP_TEXT = """
MariaDB JIRA Bug Analyzer
=========================
Fetches bugs from jira.mariadb.org, extracts repro SQL, classifies areas,
scores repro quality, and generates runnable MTR test files.

Usage:
  python3 main.py [options] [MDEV-XXXXX ...]

Modes:
  (no args)                     Fetch bugs updated in the last 1 day (default)
  MDEV-XXXXX                    Fetch and process a single bug
  MDEV-XXXXX MDEV-YYYYY         Space-separated list of bugs
  MDEV-XXXXX,MDEV-YYYYY         Comma-separated list of bugs
  --file=bugs.txt               Bugs listed in a file (one per line or comma-separated)
  --days=N                      Fetch bugs updated in the last N days (default: 1)
  --full                        Fetch all bugs ever (streaming + resume safe)

Options:
  --verbose                     Detailed output per bug
  --help                        Show this help message

Backend (env vars):
  JIRA_MTR_BACKEND              ollama (default) | claude | openai | none
  CLAUDE_API_KEY                Required for claude backend
  OPENAI_API_KEY                Required for openai backend
  OPENAI_MODEL                  OpenAI model (default: gpt-4o)
  OLLAMA_MODEL                  Ollama model (default: llama3.2)
  OLLAMA_TIMEOUT                Ollama timeout seconds (default: 600)

Tuning:
  CLEANUP_THRESHOLD             Quality score below which LLM cleanup runs (default: 60)
  PROMPTS_DIR                   Path to prompt .txt files (default: ./prompts)
  VERBOSE                       Set to 1 for detailed output

MTR generation:
  Rule-based builder always runs first — generates proper connect/send/reap
  structure deterministically. LLM only called to fill remaining gaps.
  Use JIRA_MTR_BACKEND=none to use rules only (no LLM calls at all).

Examples:
  python3 main.py MDEV-39152
  python3 main.py MDEV-39152,MDEV-39151,MDEV-39150
  python3 main.py --file=bugs.txt --verbose
  python3 main.py --days=7
  python3 main.py --full
  JIRA_MTR_BACKEND=none python3 main.py MDEV-39152
  JIRA_MTR_BACKEND=claude CLAUDE_API_KEY=sk-ant-... python3 main.py MDEV-39152

Output per bug (bugs/<MDEV-KEY>/):
  metadata.json, summary.txt, description.txt, area.txt,
  repro.sql, repro_cleaned.sql, crash_query.sql, stack_trace.txt,
  repro.test, repro_quality.json, training_text.txt, ml_features.json
"""


def main():
    load_prompts()
    os.makedirs(config.BASE_DIR, exist_ok=True)

    args = sys.argv[1:]

    if "--help" in args or "-h" in args:
        print(HELP_TEXT)
        sys.exit(0)

    if "--verbose" in args:
        config.VERBOSE = True
        args = [a for a in args if a != "--verbose"]

    full_fetch = "--full" in args

    days = 1
    for arg in args:
        if arg.startswith("--days="):
            try:
                days = int(arg.split("=")[1])
            except ValueError:
                print(f"Invalid --days value: {arg}")
                sys.exit(1)

    file_path = None
    for arg in args:
        if arg.startswith("--file="):
            file_path = arg.split("=", 1)[1]

    # Collect issue keys — support space and comma separation
    raw_keys   = [a for a in args if not a.startswith("--")]
    issue_keys = []
    for k in raw_keys:
        issue_keys.extend([x.strip() for x in k.split(",") if x.strip()])

    # File mode
    if file_path:
        if not os.path.exists(file_path):
            print(f"Error: file not found: {file_path}")
            sys.exit(1)
        with open(file_path) as f:
            issue_keys = []
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                issue_keys.extend([x.strip() for x in line.split(",") if x.strip()])
        print(f"Loaded {len(issue_keys)} bug keys from {file_path}")

    # Single / multi issue mode
    if issue_keys:
        for key in issue_keys:
            if config.VERBOSE:
                print(f"\n{'='*50}")
            print(f"Fetching: {key}")
            issue = fetch_single_issue(key)
            if issue:
                save_issue(issue)
            else:
                print(f"  Skipping {key} — could not fetch")

    # Bulk / daily mode
    else:
        jql = (
            "project=MDEV ORDER BY updated DESC"
            if full_fetch
            else f"project=MDEV AND updated >= -{days}d ORDER BY updated DESC"
        )
        label = "all bugs" if full_fetch else f"bugs updated in last {days} day(s)"
        print(f"Fetching {label}...")

        processed_keys = load_processed_keys()
        if processed_keys:
            print(f"Resuming — {len(processed_keys)} bugs already in dataset, skipping them.")

        for issue in fetch_and_process_issues(jql, processed_keys):
            if config.VERBOSE:
                print(f"\n{'='*50}")
            save_issue(issue)


if __name__ == "__main__":
    main()
