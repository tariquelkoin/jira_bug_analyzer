# 🐛 MariaDB JIRA Bug Analyzer

A Python tool that fetches bugs from [MariaDB JIRA](https://jira.mariadb.org), extracts structured data from each ticket, classifies the bug area, assesses repro quality, and generates a runnable [MTR](https://mariadb.com/kb/en/mysql-test-run-overview/) (MySQL Test Run) test file — automatically.

Built to help developers, QA engineers, and contributors spend less time manually decoding bug reports and more time actually reproducing and fixing them.

---

## The Problem

MariaDB JIRA has thousands of open bugs. Reproducing them manually is tedious:

- Repro steps are scattered across descriptions, code blocks, log dumps, and comments
- Some are single SQL statements; others are multi-session concurrency scenarios
- Stack traces sit in plain text next to `general.log` dumps and prose descriptions
- Many tickets have incomplete, shaky, or prose-only repro information
- None of it is structured — every ticket is different

This tool handles the parsing, cleanup, and test generation so you don't have to.

---

## What It Does

For every JIRA ticket it processes, the tool:

- **Extracts repro SQL** from `{code}` blocks, `general.log` session dumps, and plain-text SQL in descriptions — three-tier extraction with automatic fallback
- **Detects the crash query** — the exact statement executing at crash time from MariaDB crash dumps
- **Extracts stack traces** — handles both explicit `Stack trace:` format and MariaDB's `Attempting backtrace...` crash format
- **Classifies the bug area** — Galera, InnoDB, Optimizer, Replication, DDL, Sequences, and 15+ other areas, rule-based with no LLM needed
- **Scores repro quality** — 0–100 score across 8 dimensions: SQL presence, schema, data setup, trigger query, table consistency, semicolons, error context, description length
- **Cleans up shaky repros** — for bugs scoring below the threshold, uses an LLM to reconstruct missing schema, fix SQL, synthesise from prose, with every inferred line clearly marked
- **Generates an MTR test file** — a runnable `.test` file with proper setup, teardown, multi-session handling, and `--error` directives
- **Saves a structured dataset** — every field in a `bug_dataset.jsonl` file, ready for analysis or ML training

---

## How It Works (Agent Pipeline)

```
JIRA ticket
    ↓
Fetch bug data (streaming, one page at a time)
    ↓
Extract SQL + stack trace + crash query   ← rule-based, 3-tier
    ↓
Classify area + score repro quality       ← rule-based, no LLM
    ↓
Quality < threshold?
    ├── yes → LLM cleanup & enrichment    ← adds missing schema, fixes SQL
    └── no  → use as-is
    ↓
Generate MTR test file                    ← LLM with pluggable backend
    ↓
Save structured output files
```

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
    ├── repro_cleaned.sql    # LLM-cleaned SQL (only when quality < threshold)
    ├── crash_query.sql      # query executing at crash time (if present)
    ├── stack_trace.txt
    ├── repro.test           # generated MTR test file ✨
    ├── repro_quality.json   # score, level, issues breakdown
    ├── training_text.txt    # combined text for ML/analysis
    └── ml_features.json     # full structured dataset record
bug_dataset.jsonl            # one record per bug, append-mode
```

**Repro quality levels:**

| Level | Score | Meaning |
|-------|-------|---------|
| `good` | 75–100 | Complete repro, ready for MTR generation |
| `partial` | 50–74 | Has SQL but missing schema or data — LLM cleans up |
| `shaky` | 25–49 | Minimal SQL or heavily incomplete — LLM cleans up |
| `prose_only` | 10–24 | No SQL, ops described in text — LLM synthesises |
| `none` | 0–9 | Nothing useful extracted |

---

## Setup & Installation

### Prerequisites

- Python 3.8+
- `pip install requests`
- An LLM backend for MTR generation (see below)

### Install

```bash
git clone https://github.com/tariquelkoin/jira_bug_analyzer.git
cd jira_bug_analyzer
pip install requests
```

---

## Choosing a Backend

The tool uses an LLM to generate MTR tests and clean up shaky repros. Pick one of the options below.

### Option A — Ollama (free, local, no API key)

Best for privacy, offline use, and bulk processing without cost.
**Note:** Slow on CPU-only machines — expect 3–8 minutes per bug. A GPU makes it fast.

```bash
# Install Ollama
curl -fsSL https://ollama.com/install.sh | sh

# Pull a model (llama3.2 is lighter, llama3.1 is better quality)
ollama pull llama3.2

# Ollama starts automatically as a system service — verify it's running:
curl http://localhost:11434/api/tags
```

Ollama on localhost is the default — no env vars needed to get started.

### Option B — Claude API (recommended)

Best for speed (3–5 seconds per bug) and MTR output quality.
Get your API key from [console.anthropic.com](https://console.anthropic.com).

```bash
export JIRA_MTR_BACKEND=claude
export CLAUDE_API_KEY=sk-ant-...
```

### Option C — OpenAI

```bash
export JIRA_MTR_BACKEND=openai
export OPENAI_API_KEY=sk-...
export OPENAI_MODEL=gpt-4o        # optional, gpt-4o is default
```

### Option D — No LLM (extraction and scoring only)

Runs the full extraction and quality assessment pipeline without any LLM calls. No MTR test or cleanup will be generated.

```bash
export JIRA_MTR_BACKEND=none
```

---

## Running the Script

### Fetch bugs updated today (default)

```bash
python3 jira_to_file.py
```

### Fetch bugs updated in the last N days

```bash
python3 jira_to_file.py --days=7
```

### Fetch all bugs ever (streaming + resume safe)

```bash
python3 jira_to_file.py --full
```

### Single bug

```bash
python3 jira_to_file.py MDEV-39152
```

### Full example with Claude backend

```bash
export JIRA_MTR_BACKEND=claude
export CLAUDE_API_KEY=sk-ant-...
python3 jira_to_file.py MDEV-39152
```

### What you'll see

```
Fetching bugs updated in the last 1 day(s)...
  Processed 3 new | skipped 0 already done | offset 100
Saved MDEV-39152 | area=Galera, InnoDB, Sequences | quality=82/100 (good) | repro.test generated
Saved MDEV-39151 | area=InnoDB, DDL | quality=85/100 (good) | repro.test generated
Saved MDEV-39150 | area=Optimizer | quality=38/100 (shaky) | repro.test generated | repro_cleaned.sql written
```

### Resume support

If a bulk run is interrupted, just run the script again — it reads `bug_dataset.jsonl` to find already-processed bugs and skips them automatically:

```
Resuming — 1150 bugs already in dataset, skipping them.
Fetching bugs updated in the last 1 day(s)...
```

### Run daily via cron

```bash
crontab -e
# Add this line to run every morning at 7am:
0 7 * * * cd /home/user/jira_bug_analyzer && python3 jira_to_file.py >> logs/daily.log 2>&1
```

---

## All Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `JIRA_MTR_BACKEND` | `ollama` | LLM backend: `ollama`, `claude`, `openai`, `none` |
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama server URL |
| `OLLAMA_MODEL` | `llama3.2` | Ollama model to use |
| `OLLAMA_TIMEOUT` | `600` | Ollama request timeout in seconds |
| `CLAUDE_API_KEY` | _(empty)_ | Required when using `claude` backend |
| `OPENAI_API_KEY` | _(empty)_ | Required when using `openai` backend |
| `OPENAI_MODEL` | `gpt-4o` | OpenAI model to use |
| `CLEANUP_THRESHOLD` | `60` | Quality score below which LLM cleanup is triggered (0–100) |
| `PROMPTS_DIR` | `./prompts` | Path to prompt `.txt` files |

---

## Customising Prompts

All LLM prompts live in the `prompts/` directory as plain text files. Edit them without touching the Python code:

```
prompts/
├── mtr_system.txt       # MTR generation — system prompt
├── mtr_user.txt         # MTR generation — user prompt template
├── cleanup_system.txt   # Repro cleanup — system prompt
└── cleanup_user.txt     # Repro cleanup — user prompt template
```

This is also where you can inject knowledge about plugins or storage engines the model may not know — add context directly to the relevant prompt file.

---

## Area Classification

Bugs are classified without needing an LLM — purely rule-based using JIRA component metadata and keyword detection across summary, description, and stack trace.

Supported areas: InnoDB, Galera, Optimizer, Replication, DDL, Partitioning, Full-text Search, JSON, Window Functions, CTE / Recursive Queries, Sequences, Locking / Deadlock, Crash Recovery, Stored Procedures / Triggers, MyISAM, Aria, RocksDB, Performance Schema, Backup, Security, Data Types, Character Sets / Collation.

A bug can belong to multiple areas (e.g. a Galera crash involving InnoDB sequences).

---

## Roadmap

- [ ] Fetch and parse JIRA comments (many repro steps live there, not in the description)
- [ ] RAG-based plugin context injection — load area-specific knowledge into prompts at runtime
- [ ] MTR test syntax validation — catch obvious errors before saving
- [ ] Fine-tuning pipeline — use accumulated MTR tests to fine-tune a local model
- [ ] Web UI for browsing the generated dataset locally

---

## Contributing

Contributions are welcome — bug reports, repro pattern improvements, new area keyword mappings, or prompt improvements.

If you find a MariaDB bug whose SQL isn't being extracted correctly, open an issue with the MDEV number and a test case will be added for it.

---

## License

MIT
