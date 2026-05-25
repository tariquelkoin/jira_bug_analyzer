#!/bin/bash

# ==========================================
# 🧪 MariaDB AI QA Agent (Stable Version)
# ==========================================

print_help() {
  cat <<EOF

Usage:
  ./repro_ai.sh \\
    --repro_file=<file.sql> \\
    --prompt_file=<prompt.txt> \\
    --container=<name> \\
    [--docker_image=<image>] \\
    [--port=<port>] \\
    [--user=<user>] \\
    [--max_iter=<n>]

EOF
}

IMAGE="mariadb:latest"
PORT=3307
MYSQL_USER="root"
MAX_ITER=10

# Parse args
for arg in "$@"; do
  case $arg in
    --help) print_help; exit 0 ;;
    --repro_file=*) QUERY_FILE="${arg#*=}" ;;
    --prompt_file=*) PROMPT_FILE="${arg#*=}" ;;
    --container=*) CONTAINER="${arg#*=}" ;;
    --docker_image=*) IMAGE="${arg#*=}" ;;
    --port=*) PORT="${arg#*=}" ;;
    --user=*) MYSQL_USER="${arg#*=}" ;;
    --max_iter=*) MAX_ITER="${arg#*=}" ;;
    *) echo "❌ Unknown arg: $arg"; exit 1 ;;
  esac
done

# Validation
[ -z "$QUERY_FILE" ] && { echo "❌ Missing repro_file"; exit 1; }
[ -z "$PROMPT_FILE" ] && { echo "❌ Missing prompt_file"; exit 1; }
[ -z "$CONTAINER" ] && { echo "❌ Missing container"; exit 1; }

[ ! -f "$QUERY_FILE" ] && { echo "❌ SQL file not found"; exit 1; }
[ ! -f "$PROMPT_FILE" ] && { echo "❌ Prompt file not found"; exit 1; }
[ -z "$OPENAI_API_KEY" ] && { echo "❌ OPENAI_API_KEY not set"; exit 1; }

PROMPT_CONTENT=$(cat "$PROMPT_FILE")
SAFE_PROMPT=$(printf "%s" "$PROMPT_CONTENT" | jq -Rs .)

# Wait for MySQL readiness
wait_for_mysql() {
  for i in {1..30}; do
    mysql -u$MYSQL_USER -h127.0.0.1 -P$PORT -e "SELECT 1;" &>/dev/null && return 0
    sleep 1
  done
  echo "❌ MySQL did not start"
  exit 1
}

# Start container
start_container() {
  echo "🚀 Starting container: $CONTAINER ($IMAGE)"

  docker rm -f $CONTAINER 2>/dev/null

  docker run -d \
    --name $CONTAINER \
    -e MARIADB_ALLOW_EMPTY_ROOT_PASSWORD=yes \
    -e MARIADB_ROOT_HOST=% \
    -p $PORT:3306 \
    $IMAGE

  wait_for_mysql

  mysql -u$MYSQL_USER -h127.0.0.1 -P$PORT -e "CREATE DATABASE IF NOT EXISTS test;"
}

start_container

CURRENT_SQL="current.sql"
cp "$QUERY_FILE" "$CURRENT_SQL"

PREV_HASH=""

echo "🚀 Starting AI loop (max iterations: $MAX_ITER)"

for ((i=1; i<=MAX_ITER; i++)); do
  echo "================ Iteration $i ================="

  OUTPUT=$(mysql -u$MYSQL_USER -h127.0.0.1 -P$PORT test < "$CURRENT_SQL" 2>&1)
  echo "$OUTPUT"

  # Crash detection
  if echo "$OUTPUT" | grep -q -E "ERROR 2002|ERROR 2013|Can't connect"; then
    echo "💥 MariaDB crash detected"
    docker logs $CONTAINER > crash.log 2>&1
    start_container
  fi

  SAFE_OUTPUT=$(printf "%s" "$OUTPUT" | jq -Rs .)
  SAFE_SQL=$(printf "%s" "$(cat "$CURRENT_SQL")" | jq -Rs .)

  RESPONSE=$(curl https://api.openai.com/v1/responses \
    -s \
    -H "Authorization: Bearer $OPENAI_API_KEY" \
    -H "Content-type: application/json" \
    -d "{
      \"model\": \"gpt-4.1-mini\",
      \"input\": [
        {
          \"role\": \"user\",
          \"content\": [
            {\"type\": \"text\", \"text\": $SAFE_PROMPT},
            {\"type\": \"text\", \"text\": \"CURRENT SQL:\"},
            {\"type\": \"text\", \"text\": $SAFE_SQL},
            {\"type\": \"text\", \"text\": \"OUTPUT:\"},
            {\"type\": \"text\", \"text\": $SAFE_OUTPUT}
          ]
        }
      ]
    }")

  # Extract SQL robustly
  NEW_SQL=$(echo "$RESPONSE" | jq -r '
    .output[]?.content[]? 
    | select(.type=="output_text") 
    | .text
  ' | sed 's/^```sql//;s/^```//;s/```$//' | head -n 1)

  echo "-------- AI Suggested SQL --------"
  echo "$NEW_SQL"

  # Retry once if invalid
  if [ -z "$NEW_SQL" ] || [ "$NEW_SQL" == "null" ]; then
    echo "⚠️ Invalid AI response, retrying once..."
    sleep 2
    continue
  fi

  CUR_HASH=$(echo "$NEW_SQL" | md5sum)

  if [ "$CUR_HASH" == "$PREV_HASH" ]; then
    echo "🛑 No change detected, stopping loop"
    break
  fi

  PREV_HASH=$CUR_HASH

  echo "$NEW_SQL" > "$CURRENT_SQL"

done

echo "✅ Finished AI loop"
