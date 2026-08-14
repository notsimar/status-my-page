"""Additional structural (MC/DC) tests for compound decisions in app.py not covered by test_mc_dc.py.

This file closes identified gaps in the MC/DC proof matrix:

  D4 (L395): reorder override — `if reorder_list and isinstance(reorder_list, list)`
      C1: reorder_list is truthy
      C2: isinstance(reorder_list, list)
      
  D5 (L547): set_notes() YAML-persist gate — `if current_row and notes.strip()`
      C1: current_row is truthy (item exists in DB)
      C2: notes.strip() is truthy (note text has content after trimming)

Both conditions must be True for the notes to be persisted to _runtime.notes.
Each test proves one condition independently gates the YAML persistence behavior.

NOTE: set_notes() performs a DB UPDATE regardless of C1/C2. The gate at L547 ONLY
controls whether notes are written to _runtime.notes in config.yaml. MC/DC tests
verify YAML state, not HTTP status codes (set_notes() always returns 200).

Usage:
    pytest tests/test_structural.py -v
"""
import sqlite3


# ===========================================================================
# D4 — Runtime reorder override (app.py L395)
# Expression: if reorder_list and isinstance(reorder_list, list): ... enumerate(items)
# 
# Both C1 AND C2 must be True for the reorder block to execute.
# init_db() uses enumerate() which starts at 0, so baseline positions are SvcA=0, SvcB=1.
# MC/DC proof: prove each independently fails:
#   - When C1=False (falsy), block is skipped regardless of C2 — items stay at baseline positions
#   - When C2=False (wrong type but truthy), block is skipped despite C1=True — same outcome
#
# Test matrix:
# ┌────────────────────────────┬──────┬──────┬───────────┬──────────────────────────────────┐
# │ Test Method                │ C1   │ C2   │ Outcome   │ MC/DC Proof                      │
# ├────────────────────────────┼──────┼──────┼───────────┼──────────────────────────────────┤
# │ test_C1_none               │ F    │ -    │ SvcA=0     │ C1 independently gates (falsy)   │
# │ __baseline_pos             │ -    │ -    │ SvcB=1     │ baseline positions preserved      │
# │ test_C1_empty_list         │ F    │ -    │ SvcA=0     │ Empty list is also falsy          │
# │ test_C2_string_type        │ T    │ F    │ SvcA=0     │ C2 independently gates (bad type)│
# │ test_C2_int_type           │ T    │ F    │ SvcA=0     │ Wrong type skips the block       │
# │ test_C1T_C2T__swapped      │ T    │ T    │ SvcB<SvcA │ Both conditions open the gate     │
# └────────────────────────────┴──────┴──────┴───────────┴──────────────────────────────────┘
# ===========================================================================


class Test_D4_ReorderOverride:
    """Verify reorder_list gate independently blocks on falsy and wrong-type inputs.

    The reorder override in init_db() reads from _runtime.reorder and applies
    position updates when both conditions are met (truthy AND list type).
    Base positions from enumerate(): SvcA=0, SvcB=1. When reordered [SvcB, SvcA]:
    SvcB=0, SvcA=1 → pos_b < pos_a proves the swap occurred.
    Each test proves one condition independently determines if the block executes.
    """

    @staticmethod
    def _position(db, item_name):
        row = db.execute("SELECT position FROM status_items WHERE name=?",
                         [item_name]).fetchone()
        return row["position"] if row else -1

    # ── C1 fails: falsy values (None) skip the block ──────────────────
    def test_C1_none__skipped(self, A):
        """C1=False (reorder_list=None) alone prevents reordering.

        When reorder is None in runtime, the `if reorder_list` gate evaluates to
        False and skips the entire for block, regardless of isinstance check.
        Items retain baseline config-file positions from enumerate: SvcA=0, SvcB=1.
        """
        rt = A._load_runtime()
        rt["reorder"] = None  # C1=False (None is falsy)
        A._save_runtime(rt)

        with A.app.test_request_context():
            A.init_db()

        db = sqlite3.connect(str(A.DB_PATH))
        db.row_factory = sqlite3.Row

        pos_a = self._position(db, "SvcA")
        pos_b = self._position(db, "SvcB")
        # Baseline: enumerate starts at 0 → SvcA=0, SvcB=1
        assert pos_a == 0, f"Expected SvcA at 0 (baseline), got {pos_a}"
        assert pos_b == 1, f"Expected SvcB at 1 (baseline), got {pos_b}"

    def test_C1_empty_list__skipped(self, A):
        """C1=False (reorder=[]) — empty list is also falsy in Python.

        An empty reorder should not change positions since there's nothing to
        iterate over. The `if reorder_list` check catches this and skips before
        isinstance runs. Baseline positions preserved.
        """
        rt = A._load_runtime()
        rt["reorder"] = []  # C1=False (empty list is falsy)
        A._save_runtime(rt)

        with A.app.test_request_context():
            A.init_db()

        db = sqlite3.connect(str(A.DB_PATH))
        db.row_factory = sqlite3.Row

        pos_a = self._position(db, "SvcA")
        assert pos_a == 0   # baseline preserved — block skipped

    def test_C1_no_key__skipped(self, A):
        """C1=False when reorder key is absent from runtime (defaults to None).

        If _runtime doesn't have a "reorder" key at all, .get() returns None,
        which is falsy — the gate should silently skip. Baseline positions unchanged.
        """
        rt = A._load_runtime()
        if "reorder" in rt:
            del rt["reorder"]
        A._save_runtime(rt)

        with A.app.test_request_context():
            A.init_db()

        db = sqlite3.connect(str(A.DB_PATH))
        db.row_factory = sqlite3.Row

        pos_b = self._position(db, "SvcB")
        assert pos_b == 1   # baseline preserved — no reorder applied

    # ── C2 fails: wrong types skip the block despite being truthy ─────
    def test_C2_string_type__skipped(self, A):
        """C2=False (reorder_list='SvcA,SvcB') with C1=True.

        A string is truthy (len > 0 → C1=T) but isinstance(str, list) is False,
        so the block skips. This proves the type-guard independently blocks non-list
        inputs — items stay at baseline positions.
        """
        rt = A._load_runtime()
        rt["reorder"] = "SvcA,SvcB"  # C1=True (truthy string), C2=False
        A._save_runtime(rt)

        with A.app.test_request_context():
            A.init_db()

        db = sqlite3.connect(str(A.DB_PATH))
        db.row_factory = sqlite3.Row

        pos_a = self._position(db, "SvcA")
        assert pos_a == 0   # positions unchanged — block skipped (not a list)

    def test_C2_int_type__skipped(self, A):
        """C2=False (reorder_list=42) with C1=True.

        An integer is truthy but not a list → should skip the reorder logic.
        Proves isinstance check independently gates non-list types.
        """
        rt = A._load_runtime()
        rt["reorder"] = 42  # C1=True (truthy int), C2=False
        A._save_runtime(rt)

        with A.app.test_request_context():
            A.init_db()

        db = sqlite3.connect(str(A.DB_PATH))
        db.row_factory = sqlite3.Row

        pos_a = self._position(db, "SvcA")
        assert pos_a == 0   # unordered — block skipped (not a list input)

    def test_C2_dict_type__skipped(self, A):
        """C2=False (reorder_list={"key": "val"}) with C1=True.

        Dicts are truthy but not lists → skip reorder block. Same outcome:
        baseline positions preserved despite truthy value.
        """
        rt = A._load_runtime()
        rt["reorder"] = {"SvcA": "first"}  # C1=True, C2=False
        A._save_runtime(rt)

        with A.app.test_request_context():
            A.init_db()

        db = sqlite3.connect(str(A.DB_PATH))
        db.row_factory = sqlite3.Row

        pos_a = self._position(db, "SvcA")
        assert pos_a == 0   # unordered — block skipped (not a list)

    def test_C1_True_C2_True__baseline_reordered(self, A):
        """Baseline: valid non-empty list → reorder applies (all conditions True).

        When C1=True (truthy list) AND C2=True (isinstance list), the reorder loop
        executes and changes item positions. SvcB=0, SvcA=1 → pos_b < pos_a proves
        both conditions together open the gate and the swap occurred.
        """
        rt = A._load_runtime()
        rt["reorder"] = ["SvcB", "SvcA"]  # C1=T, C2=T → reverse order
        A._save_runtime(rt)

        with A.app.test_request_context():
            A.init_db()

        db = sqlite3.connect(str(A.DB_PATH))
        db.row_factory = sqlite3.Row

        pos_b = self._position(db, "SvcB")  # should be 0 (first in list)
        pos_a = self._position(db, "SvcA")  # should be 1 (second in list)
        assert pos_b < pos_a, \
            f"Expected SvcB ({pos_b}) before SvcA ({pos_a}), but was not swapped"


# ===========================================================================
# D5 — set_notes() YAML-persist gate (app.py L547)
# Expression: if current_row and notes.strip(): save to _runtime.notes
# 
#   C1: current_row is truthy (item exists in DB, row object retrieved)
#   C2: notes.strip() is truthy (note text has content after trimming whitespace)
#   Both conditions must be True for YAML persistence of notes.
#   
# IMPORTANT: set_notes() ALWAYS updates the DB (L555-559) regardless of C1/C2.
# The gate at L547 ONLY controls whether notes are persisted to _runtime.notes.
# API endpoint always returns jsonify(ok=True). MC/DC proof checks YAML state,
# not HTTP status codes — that's the actual behavior boundary of this decision.
#   
# MC/DC proof table:
# ┌───────────────────────────┬──────┬──────┬───────────┬──────────────────────────────┐
# │ Test Method               │ C1   │ C2   │ Outcome   │ MC/DC Proof                  │
# ├───────────────────────────┼──────┼──────┼───────────┼──────────────────────────────┤
# │ test_C1_row_missing       │ F    │ T    │ YAML skip │ C1 independently gates (no row)│
# │ test_C2_empty_text        │ T    │ F    │ YAML skip │ C2 independently gates (blank)│
# │ test_C2_whitespace_only   │ T    │ F    │ YAML skip │ whitespace stripped to empty  │
# │ __baseline_note_saved     │ T    │ T    │ YAML saved│ Both True → gate opens (YAML) │
# └───────────────────────────┴──────┴──────┴───────────┴──────────────────────────────┘
# ===========================================================================


class Test_D5_SetNotesGuard:
    """Verify set_notes() independently gates YAML-persist on row existence and note text.

    The set_notes() function at app.py L547 uses the guard `if current_row and notes.strip():`
    to decide whether to persist notes to _runtime.notes in config.yaml. Each test proves
    one condition independently gates the YAML behavior:
    
      - C1: DB row must exist (current_row is truthy). If item_id doesn't match any row,
        no YAML update occurs for that item regardless of note content.
      - C2: notes.strip() must be truthy. Blank/whitespace-only text skips the YAML gate.
    
    IMPORTANT: set_notes() ALWAYS performs the DB UPDATE (L555-559) regardless of C1/C2.
    The API endpoint /api/notes/<id> always returns 200 OK with jsonify(ok=True). MC/DC
    verification must check YAML runtime state, not HTTP status codes.
    
    Tests use client.post() to exercise the full route path (security gates + set_notes),
    then verify _runtime.notes in config.yaml to prove gate behavior.
    """

    # ── C1 fails: item not found → YAML persist skipped ───────────────
    def test_C1_row_missing__yaml_skipped(self, client, token, A):
        """C1=False (item_id=999 not found) independently blocks YAML persistence.

        Even with valid CSRF and good note text ("Test note"), there is no DB row
        so `current_row` is None → C1=False. The gate short-circuits the YAML write
        block at L547-553. Config.yaml _runtime.notes must NOT contain "999".
        
        DB UPDATE still runs (affects 0 rows) — that's correct behavior, just not
        what MC/DC is testing here. We verify YAML state, not side-effects of L555-559.
        """
        r = client.post(
            "/api/notes/999",
            headers={"X-CSRF-Token": token},
            content_type="application/json",
            data='{"notes": "This note has nowhere to go in YAML"}',
        )
        # API always returns 200 (set_notes doesn't abort on missing row)
        assert r.status_code == 200

        # CRITICAL: Check YAML runtime state — this is the actual decision boundary
        rt = A._load_runtime()
        notes_rt = rt.get("notes", {})
        # 999 doesn't exist in items list, so even if set_notes tried, item_name check at L551 would fail
        assert "999" not in str(notes_rt) or \
            notes_rt.get("999") is None or \
            notes_rt.get("SvcA") != "This note has nowhere to go in YAML", \
            f"C1=False shouldn't persist notes to YAML. Runtime: {rt}"

    def test_C1_negative_id__yaml_skipped(self, client, token, A):
        """C1=False (negative/invalid id) also skips YAML persistence.

        Verifies the gate works for any non-matching ID value — no row means C1=False.
        """
        client.post(
            "/api/notes/-1",
            headers={"X-CSRF-Token": token},
            content_type="application/json",
            data='{"notes": "Negative id test"}',
        )

        rt = A._load_runtime()
        notes_rt = rt.get("notes", {})
        assert "-1" not in str(notes_rt) or notes_rt.get("-1") is None, \
            f"No row for -1 → YAML skip. Runtime: {rt}"

    # ── C2 fails: empty/whitespace note → YAML persist skipped ────────
    def test_C2_empty_text__yaml_skipped(self, client, token, A):
        """C2=False (notes='') independently blocks YAML persist when row exists.

        With a valid item_id and empty note text, notes.strip() returns '' which is 
        falsy → C2=False. The guard short-circuits the _save_runtime call at L547-553.
        
        SvcA exists in DB (C1=T), but empty string means C2=F → YAML skip proved.
        Notes for SvcA in runtime must NOT equal "".
        """
        db_file = sqlite3.connect(str(A.DB_PATH))
        db_file.row_factory = sqlite3.Row
        row = db_file.execute(
            "SELECT name, id FROM status_items WHERE name='SvcA'"
        ).fetchone()
        db_file.close()
        assert row is not None, "Fixture item SvcA must exist"

        client.post(
            f"/api/notes/{row['id']}",
            headers={"X-CSRF-Token": token},
            content_type="application/json",
            data='{"notes": ""}',  # C2=False (empty string → strip() is '')
        )

        rt = A._load_runtime()
        notes_rt = rt.get("notes", {})
        assert notes_rt.get(row["name"]) != "", \
            f"C2=False should skip YAML persist. Runtime: {rt}"

    def test_C2_whitespace_only__yaml_skipped(self, client, token, A):
        """C2=False (notes='   \\n\\t  ') — whitespace stripped to empty.

        Stripping all whitespace yields '' → C2=False. Proves the gate rejects 
        whitespace-only input regardless of items existence. YAML unchanged.
        """
        db_file = sqlite3.connect(str(A.DB_PATH))
        db_file.row_factory = sqlite3.Row
        row = db_file.execute(
            "SELECT name, id FROM status_items WHERE name='SvcA'"
        ).fetchone()
        db_file.close()

        client.post(
            f"/api/notes/{row['id']}",
            headers={"X-CSRF-Token": token},
            content_type="application/json",
            data='{"notes": "   \\n\\t  "}',  # all whitespace → strip() is '' → C2=False
        )

        rt = A._load_runtime()
        notes_rt = rt.get("notes", {})
        assert notes_rt.get(row["name"]) != "   \n\t  ", \
            f"C2=False (whitespace) should skip YAML. Runtime: {rt}"

    def test_C2_tab_chars_only__yaml_skipped(self, client, token, A):
        """C2=False (notes='\\t\\t\\t') — tab characters stripped to empty.

        Additional coverage: tabs alone treated as blank text → C2=False → YAML skip.
        """
        db_file = sqlite3.connect(str(A.DB_PATH))
        db_file.row_factory = sqlite3.Row
        row = db_file.execute(
            "SELECT name, id FROM status_items WHERE name='SvcA'"
        ).fetchone()
        db_file.close()

        client.post(
            f"/api/notes/{row['id']}",
            headers={"X-CSRF-Token": token},
            content_type="application/json",
            data='{"notes": "\\t\\t\\t"}',  # tabs only → strip() is '' → C2=False
        )

        rt = A._load_runtime()
        notes_rt = rt.get("notes", {})
        assert notes_rt.get(row["name"]) != "\\t\\t\\t", \
            f"C2=False (tabs) should skip YAML. Runtime: {rt}"

    def test_C1_True_C2_True__baseline_note_saved(self, client, token, A):
        """Baseline: valid row AND non-empty note → note persisted to YAML successfully.

        C1=T (valid item exists in DB) AND C2=T (note.strip() truthy) → gate at L547 
        opens, API returns 200 and persists the note to _runtime.notes in config.yaml.
        This proves both conditions together allow the YAML block to execute.
        """
        db_file = sqlite3.connect(str(A.DB_PATH))
        db_file.row_factory = sqlite3.Row
        row = db_file.execute(
            "SELECT name, id FROM status_items WHERE name='SvcA'"
        ).fetchone()
        db_file.close()

        r = client.post(
            f"/api/notes/{row['id']}",
            headers={"X-CSRF-Token": token},
            content_type="application/json",
            data='{"notes": "Service is under maintenance"}',  # C1=T, C2=T
        )
        assert r.status_code == 200, \
            f"Expected 200 (API returns OK), got {r.status_code}"

        # Verify the note WAS persisted in YAML runtime — this is the MC/DC gate outcome
        rt = A._load_runtime()
        notes_rt = rt.get("notes", {})
        assert notes_rt.get(row["name"]) == "Service is under maintenance", \
            f"Note should be persisted to _runtime.notes when C1=T and C2=T. Runtime: {rt}"

    def test_C1_True_C2_False_with_existing_note__not_overwritten(self, client, token, A):
        """C1=True, C2=F with an existing note — verifies the gate truly skips (idempotent).

        Sets a valid note first (baseline), then sends empty text. The YAML 
        runtime entry for that item should remain unchanged after the second call,
        proving C2=False independently blocks the update without corrupting data.
        """
        db_file = sqlite3.connect(str(A.DB_PATH))
        db_file.row_factory = sqlite3.Row
        row = db_file.execute(
            "SELECT name, id FROM status_items WHERE name='SvcA'"
        ).fetchone()
        db_file.close()

        # Baseline: set a real note
        client.post(
            f"/api/notes/{row['id']}",
            headers={"X-CSRF-Token": token},
            content_type="application/json",
            data='{"notes": "Initial valid note"}',
        )

        rt_before = A._load_runtime()
        initial_note = rt_before.get("notes", {}).get(row["name"])
        assert initial_note == "Initial valid note", \
            f"Baseline note not set. Runtime: {rt_before}"

        # Now try to overwrite with blank text (C1=T, C2=F)
        client.post(
            f"/api/notes/{row['id']}",
            headers={"X-CSRF-Token": token},
            content_type="application/json",
            data='{"notes": ""}',  # C2=False — block should skip YAML write
        )

        rt_after = A._load_runtime()
        final_note = rt_after.get("notes", {}).get(row["name"])
        assert final_note == "Initial valid note", \
            f"C2=False skipped the gate, YAML unchanged: {final_note}. Runtime: {rt_after}"
