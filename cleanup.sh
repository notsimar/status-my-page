#!/bin/bash
# cleanup.sh — Archive & cleanup utility for the status page
# Usage:
#   ./cleanup.sh [command]
#
# Commands:
#   list              List all archives (latest first)
#   show <file>       Pretty-print a specific archive JSON
#   prune [--keep N]  Delete archives older than the newest N snapshots
#   report            Summary of all outages across archived snapshots

set -euo pipefail
cd "$(dirname "$0")"

ARCHIVES_DIR="archives"
KEEP_DEFAULT=2  # keep last 2 snapshots by default

if [ ! -d "$ARCHIVES_DIR" ]; then
    echo "No archives directory found."
    exit 1
fi

archives=$(ls -1 "$ARCHIVES_DIR"/*.json 2>/dev/null)
count=$(echo "$archives" | grep -c . 2>/dev/null || echo 0)

if [ "$count" -eq 0 ]; then
    echo "No archives found."
    exit 0
fi

# ── list ────────────────────────────────────────────────
do_list() {
    printf "%-25s %s\n" "FILE" "SUMMARY"
    printf "%-25s %s\n" "----" "-------"
    ls -1t "$ARCHIVES_DIR"/*.json 2>/dev/null | sort -r | while IFS= read -r f; do
        ts=$(python3 -c "import json, sys; d=json.load(open(sys.argv[1])); print(d['timestamp'])" "$f" 2>/dev/null || echo "?")
        items=$(python3 -c "
import json, sys
d = json.load(open(sys.argv[1]))
items = d.get('items', [])
reds = sum(1 for i in items if i['status'] == 'red')
print(f'{len(items)} total, {reds} red')
" "$f" 2>/dev/null || echo "?")
        printf "%-25s %s\n" "$(basename "$f")" "$items  ($ts)"
    done
}

# ── show ────────────────────────────────────────────────
do_show() {
    local file="${1:-}"
    if [ -z "$file" ]; then
        echo "Usage: $0 show <filename_in_archives>"
        exit 1
    fi
    local path="$ARCHIVES_DIR/$file"
    if [ ! -f "$path" ]; then
        echo "Archive not found: $file"
        exit 1
    fi
    python3 -c "
import json, sys
d = json.load(open(sys.argv[1]))
print(f'Timestamp: {d[\"timestamp\"]}')
print()
print(f'{\"Name\":<22s} {\"Status\":>8s}  Notes')
print('-' * 60)
for i in d['items']:
    flag = '\U0001f534 red' if i['status']=='red' else '\U0001f7e2 green'
    print(f'{i[\"name\"]:<22s} {flag:>8s}  {i.get(\"notes\",\"\")}')
" "$path"
}

# ── prune ───────────────────────────────────────
do_prune() {
    local keep="$KEEP_DEFAULT"
    while [ $# -gt 0 ]; do
        case "$1" in
            --keep) keep="${2:-$KEEP_DEFAULT}"; shift 2 ;;
            *) echo "Unknown option: $1"; exit 1 ;;
        esac
    done

    total=$(ls -1 "$ARCHIVES_DIR"/*.json 2>/dev/null | wc -l)
    to_delete=$((total - keep))

    if [ "$to_delete" -le 0 ]; then
        echo "Nothing to prune — $total archives, keeping last $keep."
        exit 0
    fi

    echo "Pruning: $total total, keeping newest $keep, deleting $to_delete…"
    ls -1t "$ARCHIVES_DIR"/*.json | tail -n +"$((keep + 1))" | while read -r f; do
        rm -f "$f"
        echo "  Removed: $(basename "$f")"
    done

    remaining=$(ls -1 "$ARCHIVES_DIR"/*.json 2>/dev/null | wc -l)
    echo "Done — $remaining archives remaining."
}

# ── report ───────────────────────────────────────
do_report() {
    python3 << 'PYEOF'
import json, glob
from collections import defaultdict

files = sorted(glob.glob("archives/*.json"), reverse=True)
if not files:
    print("No archives found.")
    exit(0)

outage_log = defaultdict(list)  # service -> [(snapshot_name, notes)]
for f in files:
    data = json.load(open(f))
    name = f.replace("archives/", "").replace(".json", "")
    for item in data.get("items", []):
        if item["status"] == "red":
            outage_log[item["name"]].append((name, item.get("notes", "")))

print("Historical outage report")
print("=" * 60)
if not outage_log:
    print("No outages recorded in any archive snapshot.")
else:
    for svc in sorted(outage_log):
        incidents = outage_log[svc]
        print(f"\n  {svc} — {len(incidents)} incident(s)")
        for snap, note in incidents:
            note_str = f'  "{note}"' if note else ""
            print(f"    [{snap}] red{note_str}")

print("\n" + "=" * 60)
total_outages = sum(len(v) for v in outage_log.values())
services_with_outages = len(outage_log)
print(f"Total: {total_outages} outage(s) across {services_with_outages} service(s)")
PYEOF
}

# ── CLI dispatch ─────────────────────────────────────
case "${1:-help}" in
    list)   do_list ;;
    show)   do_show "$2" ;;
    prune)  shift; do_prune "$@" ;;
    report) do_report ;;
    *)
        echo "Cleanup utility for status page archives"
        echo ""
        echo "Usage: $0 {list|show|prune|report}"
        echo ""
        echo "Commands:"
        echo "  list              List all archives (latest first)"
        echo "  show <file>       Pretty-print a specific archive"
        echo "  prune [--keep N]  Delete old snapshots, keep newest N (default: $KEEP_DEFAULT)"
        echo "  report            Historical outage summary across all archives"
        ;;
esac
