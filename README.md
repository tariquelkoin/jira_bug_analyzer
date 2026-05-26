# 🐛 MariaDB JIRA Bug Analyzer

A Python tool that fetches bugs from [MariaDB JIRA](https://jira.mariadb.org), extracts structured data from each ticket, classifies the bug area, and generates a runnable [MTR](https://mariadb.com/kb/en/mysql-test-run-overview/) (MySQL Test Run) test file — automatically.

Built to help developers, QA engineers, and contributors spend less time manually decoding bug reports and more time actually reproducing and fixing them.

---

## The Problem

MariaDB JIRA has thousands of open bugs. Reproducing them manually is tedious:

- Repro steps are scattered across descriptions, code blocks, log dumps, and comments
- Some are single SQL statements; others are multi-session concurrency scenarios
- Stack traces sit in plain text next to `general.log` dumps and prose descriptions
- None of it is structured — every ticket is different

This tool handles the parsing so you don't have to.

---

## What It Does

For every JIRA ticket it processes, the tool:

- **Extracts repro SQL** from `{code}` blocks, `general.log` session dumps, and plain-text SQL in descriptions
- **Detects the crash query** — the exact statement executing at crash time (from MariaDB crash dumps)
- **Extracts stack traces** — handles both explicit `Stack trace:` format and MariaDB's `Attempting backtrace...` crash format
- **Classifies the bug area** — Galera, InnoDB, Optimizer, Replication, DDL, Sequences, and 15+ other areas
- **Generates an MTR test file** — a runnable `.test` file with proper setup, teardown, multi-session handling, and `--error` directives
- **Saves a structured dataset** — every field in a `bug_dataset.jsonl` file, ready for analysis or ML training

---

## Output Structure

Each bug gets its own directory:

```
bugs/
└── MDEV-39152/
    ├── metadata.json        # full raw JIRA API response
    ├── summary.txt
    ├── description.txt
    ├── area.txt             # classified areas (e.g. Galera, InnoDB)
    ├── repro.sql            # extracted SQL / session dump
    ├── crash_query.sql      # query executing at crash time (if present)
    ├── stack_trace.txt
    ├── repro.test           # generated MTR test file ✨
    ├── training_text.txt    # combined text for ML/analysis
    └── ml_features.json     # structured dataset record
bug_dataset.jsonl            # one record per bug, append-mode
```

---

## Getting Started

### Prerequisites

- Python 3.8+
- `pip install requests`
- For MTR generation: [Ollama](https://ollama.com) (free, runs locally) — or a Claude/OpenAI API key

### Install

```bash
git clone https://github.com/tariquelkoin/jira_bug_analyzer.git
cd jira_bug_analyzer
pip install requests
```

### Fetch a single bug

```bash
python jira_to_file.py MDEV-39152
```

### Fetch latest bugs in bulk

```bash
python jira_to_file.py
```

---

## MTR Generation

The tool generates MTR test files using a pluggable LLM backend. The default is **Ollama** — free, runs locally, no API key needed.

### Option 1: Ollama (default, recommended)

```bash
# Install Ollama from https://ollama.com, then:
ollama pull llama3.1
python jira_to_file.py MDEV-39152
```

### Option 2: Claude API

```bash
JIRA_MTR_BACKEND=claude CLAUDE_API_KEY=sk-ant-... python jira_to_file.py MDEV-39152
```

### Option 3: OpenAI

```bash
JIRA_MTR_BACKEND=openai OPENAI_API_KEY=sk-... python jira_to_file.py MDEV-39152
```

### Skip MTR generation

```bash
JIRA_MTR_BACKEND=none python jira_to_file.py MDEV-39152
```

The generated `repro.test` follows real MTR conventions — setup/teardown, `connect`/`disconnect` for multi-session scenarios, `send`/`reap` for concurrency, and `--error` directives for expected failures.

---

## Area Classification

Bugs are classified into areas without needing an LLM — purely rule-based using JIRA component metadata and keyword detection across the summary, description, and stack trace.

Supported areas include: InnoDB, Galera, Optimizer, Replication, DDL, Partitioning, Full-text Search, JSON, Window Functions, CTE / Recursive Queries, Sequences, Locking / Deadlock, Crash Recovery, Stored Procedures / Triggers, MyISAM, Aria, RocksDB, Performance Schema, Backup, Security, Data Types, Character Sets.

A bug can belong to multiple areas (e.g. a Galera crash involving InnoDB sequences).

---

## Roadmap

- [ ] Fetch and parse JIRA comments (many repro steps live there, not in the description)
- [ ] MTR test quality scoring — have the LLM rate its own confidence
- [ ] Detect multi-threaded repro patterns and model them as MTR concurrency
- [ ] `--resume` flag for incremental bulk fetch (don't re-process already-saved bugs)
- [ ] Web UI for browsing the generated dataset locally

---

## Contributing

This project is in active development. Contributions are welcome — bug reports, repro pattern improvements, new area keyword mappings, or MTR prompt improvements.

If you find a MariaDB bug whose SQL isn't being extracted correctly, open an issue with the MDEV number and I'll add a test case for it.

---

## License

MIT
