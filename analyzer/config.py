import os

# =========================
# JIRA
# =========================
BASE_URL    = "https://jira.mariadb.org/rest/api/2"
JQL         = "project=MDEV AND updated >= -1d ORDER BY updated DESC"
MAX_RESULTS = 100
BASE_DIR    = "bugs"
DATASET_FILE = "bug_dataset.jsonl"

# =========================
# LLM BACKENDS
# =========================
MTR_BACKEND  = os.environ.get("JIRA_MTR_BACKEND", "ollama")

OLLAMA_HOST    = os.environ.get("OLLAMA_HOST",    "http://localhost:11434")
OLLAMA_MODEL   = os.environ.get("OLLAMA_MODEL",   "llama3.2")
OLLAMA_TIMEOUT = int(os.environ.get("OLLAMA_TIMEOUT", "600"))

CLAUDE_API_KEY = os.environ.get("CLAUDE_API_KEY", "")
CLAUDE_MODEL   = "claude-sonnet-4-20250514"

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL   = os.environ.get("OPENAI_MODEL",   "gpt-4o")

# =========================
# QUALITY / CLEANUP
# =========================
CLEANUP_QUALITY_THRESHOLD = int(os.environ.get("CLEANUP_THRESHOLD", "60"))

# =========================
# PROMPTS
# =========================
PROMPTS_DIR = os.environ.get(
    "PROMPTS_DIR",
    os.path.join(os.path.dirname(__file__), "..", "prompts"),
)

# =========================
# VERBOSE
# =========================
VERBOSE = os.environ.get("VERBOSE", "0") == "1"

# =========================
# KEYWORD MAPS
# =========================
ENGINE_KEYWORDS = {
    "InnoDB":  ["innodb", "btr_cur", "trx_", "dict_table", "ibuf", "row_ins"],
    "MyISAM":  ["myisam"],
    "Aria":    ["aria"],
    "Galera":  ["galera", "wsrep"],
    "RocksDB": ["rocksdb"],
}

SQL_KEYWORDS = [
    "ALTER TABLE", "CREATE TABLE", "CREATE SEQUENCE", "DROP TABLE", "DROP SEQUENCE",
    "INSERT", "UPDATE", "DELETE", "SELECT", "FULLTEXT", "PARTITION", "WINDOW",
    "JSON", "TRIGGER", "VIEW", "CTE", "RECURSIVE", "INDEX", "SAVEPOINT",
    "COMMIT", "ROLLBACK", "SET SESSION", "SET GLOBAL", "START TRANSACTION",
    "BEGIN", "CALL", "REPLACE", "TRUNCATE", "LOCK TABLES", "UNLOCK TABLES",
]

ERROR_PATTERNS = [
    r"ERROR\s+\d+", r"SIGSEGV", r"Assertion.*", r"AddressSanitizer",
    r"LeakSanitizer", r"runtime error:", r"Deadlock found",
    r"got signal \d+", r"WSREP.*FSM.*no such a transition",
]

ASSERTION_PATTERNS = [
    r"Assertion [`'\"]?(.*?)[`'\"]?( failed|$)",
]
