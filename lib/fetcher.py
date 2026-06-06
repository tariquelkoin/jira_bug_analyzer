import json
import time
import requests
from lib.config import BASE_URL, MAX_RESULTS, DATASET_FILE


def fetch_single_issue(issue_key):
    url  = f"{BASE_URL}/issue/{issue_key}"
    resp = requests.get(url, timeout=30)
    if resp.status_code != 200:
        print(f"  Failed to fetch {issue_key}: {resp.status_code}")
        return None
    return resp.json()


def fetch_and_process_issues(jql, processed_keys):
    """
    Streaming page-by-page fetch. Yields each issue immediately.
    Skips keys already in processed_keys.
    Retries on transient errors.
    """
    url           = f"{BASE_URL}/search"
    startAt       = 0
    total_fetched = 0
    total_skipped = 0

    while True:
        params = {"jql": jql, "maxResults": MAX_RESULTS, "startAt": startAt}
        try:
            resp = requests.get(url, params=params, timeout=30)
            resp.raise_for_status()
        except Exception as e:
            print(f"  [fetch error at offset {startAt}] {e} — retrying in 5s...")
            time.sleep(5)
            continue

        issues = resp.json().get("issues", [])
        if not issues:
            break

        for issue in issues:
            key = issue.get("key", "")
            if key in processed_keys:
                total_skipped += 1
                continue
            total_fetched += 1
            yield issue

        startAt += len(issues)
        print(f"  Processed {total_fetched} new | skipped {total_skipped} already done | offset {startAt}")


def load_processed_keys():
    """Read bug_dataset.jsonl and return set of already-processed bug IDs."""
    keys = set()
    if not DATASET_FILE:
        return keys
    try:
        with open(DATASET_FILE) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        rec = json.loads(line)
                        if "bug_id" in rec:
                            keys.add(rec["bug_id"])
                    except json.JSONDecodeError:
                        pass
    except FileNotFoundError:
        pass
    return keys
