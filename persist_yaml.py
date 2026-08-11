#!/usr/bin/env python3
"""Patch app.py to add YAML persistence for admin actions."""
import sys, os

APP = "/home/ssahni/Developer/status-my-page/app.py"

with open(APP) as f:
    lines = f.readlines()

new_lines = []

i = 0
while i < len(lines):
    line = lines[i]

    # ── 1. Replace imports block (add threading, tempfile) ───────────
    if line.strip() == "import hashlib":
        new_lines.append("import hashlib\n")
        new_lines.append("import hmac\n")
        i += 1
        # Skip old hmac line
        new_lines.append("import tempfile\n")
        new_lines.append("import threading\n")
        continue

    # ── 2. Insert YAML persistence functions before "# ── App factory" ──
    if line.strip() == "# ── App factory":
        # Insert yaml helpers first
        new_lines.append("\n")
        new_lines.append("# ── YAML runtime persistence ────────────────────────────────────────\n")
        new_lines.append("_CONFIG_LOCK = threading.Lock()\n\n")
        new_lines.append("def _load_runtime():\n")
        new_lines.append('    """Load {status, notes, reorder} dict from config.yaml."""\n')
        new_lines.append("    try:\n")
        new_lines.append("        cfg_data = load_config()\n")
        new_lines.append('        return cfg_data.get("_runtime", {}) or {}\n')
        new_lines.append("    except Exception:\n")
        new_lines.append("        return {}\n\n")
        new_lines.append("def _save_runtime(data):\n")
        new_lines.append('    """Atomically persist data dict into config.yaml._runtime."""\n')
        new_lines.append("    path = CONFIG_PATH  # module-level\n")
        new_lines.append("    with _CONFIG_LOCK:\n")
        new_lines.append("        cfg_data = load_config()\n")
        new_lines.append('        if not isinstance(cfg_data, dict):\n')
        new_lines.append('            cfg_data = {"items": list(ITEM_NAMES), "_base": {}}\n')
        new_lines.append("        for section in (\"admin\", \"server\"):\n")
        new_lines.append('            if section in cfg_data and section not in str(cfg_data.get(\"_base\", {})):\n')
        new_lines.append('                cfg_data.setdefault(\"_base\", {})[section] = (\n')
        new_lines.append("                    cfg_data.pop(section, {})\n")
        new_lines.append("                )\n")
        new_lines.append('        cfg_data[\"_runtime\"] = data\n')
        new_lines.append("        fd, tmp_path = tempfile.mkstemp(\n")
        new_lines.append('            dir=path.parent, prefix=\".config_\", suffix=\".tmp\"\n')
        new_lines.append("        )\n")
        new_lines.append("        with os.fdopen(fd, 'w') as ff:\n")
        new_lines.append("            yaml.dump(cfg_data, ff, default_flow_style=False, sort_keys=False)\n")
        new_lines.append("        os.replace(tmp_path, path)\n\n\n")

    # ── 3. Patch init_db: insert restore logic before action print ────
    if line.strip().startswith('action = f"Rebuilt'):
        new_lines.append("\n")
        new_lines.append(
            "    # Restore persisted runtime overrides from yaml after seeding\n"
        )
        new_lines.append("    try:\n")
        new_lines.append("        rt_data = _load_runtime()\n")
        new_lines.append(
            '        status_map = rt_data.get("status", {})  # name -> degraded/red\n'
        )
        new_lines.append(
            '        notes_map  = rt_data.get("notes", {})    # name -> note text\n'
        )
        new_lines.append(
            '        reorder_list = rt_data.get("reorder", None)\n'
        )
        new_lines.append("\n")
        new_lines.append("        for item_name, new_state in status_map.items():\n")
        new_lines.append("            if item_name not in seed_set:\n")
        new_lines.append("                continue\n")
        new_lines.append(
            '            row = db.execute(\n'
        )
        new_lines.append(
            '                "SELECT id FROM status_items WHERE name = ?", [item_name]\n'
        )
        new_lines.append("            ).fetchone()\n")
        new_lines.append(
            '            if row and new_state not in ("green", ""):\n'
        )
        new_lines.append(
            '                db.execute(\n'
        )
        new_lines.append('                    "UPDATE status_items SET status=? WHERE id=?",\n')
        new_lines.append("                    (new_state, row['id']),\n")
        new_lines.append("                )\n")
        new_lines.append("\n")
        new_lines.append("        for item_name, note_text in notes_map.items():\n")
        new_lines.append("            if item_name not in seed_set or not note_text.strip():\n")
        new_lines.append("                continue\n")
        new_lines.append(
            '            row = db.execute(\n'
        )
        new_lines.append(
            '                "SELECT id FROM status_items WHERE name = ?", [item_name]\n'
        )
        new_lines.append("            ).fetchone()\n")
        new_lines.append(
            "            if row:\n")
        new_lines.append(
            '                db.execute(\n'
        )
        new_lines.append('                    "UPDATE status_items SET notes=? WHERE id=?",\n')
        new_lines.append("                    (note_text, row['id']),\n")
        new_lines.append("                )\n")
        new_lines.append("\n")
        new_lines.append("        if reorder_list and isinstance(reorder_list, list):\n")
        new_lines.append(
            '            all_rows = db.execute("SELECT id, name FROM status_items").fetchall()\n'
        )
        new_lines.append("            for i, item_name in enumerate(reorder_list):\n")
        new_lines.append(
                '                row = db.execute(\n')
        new_lines.append(
            '                    "SELECT id FROM status_items WHERE name = ?", [item_name]\n'
        )
        new_lines.append("                ).fetchone()\n")
        new_lines.append(
                "                if row:\n")
        new_lines.append(
            '                    db.execute(\n')
        new_lines.append('                        "UPDATE status_items SET position=? WHERE id=?",\n')
        new_lines.append("                        (i+1, row['id']),\n")
        new_lines.append("                    )\n")
        new_lines.append("\n")
        new_lines.append("    except Exception:\n")
        new_lines.append("        pass\n\n")

    # ── 4. Patch toggle_item → persist to yaml ────────────────────────
    if (line.strip() == "def toggle_item(item_id: int) -> str:" and 
        i+2 < len(lines) and 
        "Cycle:" in lines[i+1]):
        # Found it - capture and replace the whole function
        new_lines.append(line)  # def...
        i += 1
        new_lines.append(lines[i])  # docstring
        i += 1

        # Now capture existing body until return
        body_start = "" if i >= len(lines) else lines[i]
        
        new_toggle = """    \"\"\"Cycle: green → degraded → red → green (also persists to yaml).\"\"\"
    db = get_db()
    row = db.execute(
        "SELECT id, status FROM status_items WHERE id=?", (item_id,)
    ).fetchone()
    current = row["status"]
    next_idx = (STATUS_CYCLE.index(current) + 1) % len(STATUS_CYCLE)
    new_status = STATUS_CYCLE[next_idx]
    db.execute(
        "UPDATE status_items SET status=? WHERE id=?",
        (new_status, item_id),
    )

""")
    # ── 5. Patch set_notes to persist notes to yaml ───────────────────
    if line.strip().startswith("def set_notes(item_id: int, notes: str)"):
        new_set_notes = '''def set_notes(item_id: int, notes: str):
    db = get_db()
    # Persist config-item notes to yaml _runtime.notes (only user-added items)
    row = db.execute(
        "SELECT id, status FROM status_items WHERE id = ?", [item_id]
    ).fetchone()
''')
        new_toggle2 = """    # Persist if this is a config item (not user-added via the UI)
    row_name = db.execute(
        "SELECT name FROM status_items WHERE id=?", (item_id,)
    ).fetchone()
    if row_name:
        rt = _load_runtime()
        items_list = cfg.get("items", [])  # fresh load 
        item_name = row_name['name']
        if item_name in items_list and new_status != green:
            # This was a config-driven toggle → persist to yaml
            rt_status = rt.setdefault("status", {})
            
""")
    
    i += 1
    
return ''.join(new_lines)
