import json
from collections.abc import MutableMapping, Mapping
from sqlitedict import SqliteDict
import hashlib

import textwrap

def database_usage_doc() -> str:
    """
    Return a human-readable usage guide for the Database class and module.
    """
    return textwrap.dedent("""
    Database usage guide
    ====================

    What this class is
    ------------------
    Database is a dict-like wrapper backed by a SqliteDict. Each top-level key
    is stored as one row in SQLite. Values must be JSON-serializable
    (objects, arrays, strings, numbers, booleans, null). NaN and infinities are
    out of scope by design.

    Persistence model
    -----------------
    - At the root level, each assignment writes a single row: db[key] = value.
    - When you edit nested mappings through the wrapper, the class reads the
      entire top-level value for that key, mutates it in memory, and writes the
      whole value back as one row.
    - Lists are not proxied for mutation detection. If you mutate a list in
      place, reassign it to persist the change:
          nums = db["user"]["nums"]; nums.append(4); db["user"]["nums"] = nums
    - With autocommit=True, each write is persisted immediately.
    - With autocommit=False, writes are buffered and committed at:
        * db.commit()
        * clean context manager exit (with Database(...) as db:)
      If the context exits due to an exception, pending writes are not committed.

    Error behavior
    --------------
    - Missing top-level key access raises KeyError.
    - Indexing into a non-mapping raises TypeError (for example db["x"]["y"] when db["x"] is a number).

    Mapping behavior
    ----------------
    - len(db) counts top-level keys.
    - Iterating db yields top-level keys.
    - "k in db" matches KeyError semantics for presence checks.
    - del db["k"] removes the row and it stays deleted across reopen.

    Custom serialization
    --------------------
    Pass custom encode/decode callables to use JSON the way you want, for example:
        def enc(o): return json.dumps(o, sort_keys=True)
        def dec(s): return json.loads(s)
        db = Database("file.sqlite", encode=enc, decode=dec)
    Ensure your encoder and decoder accept and return bytes or strings as required
    by SqliteDict.

    Concurrency notes
    -----------------
    SQLite is great for one writer with many readers. For better concurrent
    reads while writing, consider enabling WAL mode at database creation time
    in your app. The Database class does not multiplex multiple writers.

    Performance notes
    -----------------
    - Nested set operations rewrite the single top-level row for that key.
      If that value is very large, consider sharding across multiple top-level
      keys to avoid rewriting big blobs for small changes.
    - For many writes, set autocommit=False, perform all assignments, then call
      db.commit() or rely on a clean context exit.

    Quick start examples
    --------------------

    Basic usage
    ~~~~~~~~~~~
        from your_module import Database
        with Database("example.sqlite", autocommit=True) as db:
            db["user:1"] = {"name": "Ada", "prefs": {"theme": "dark"}}
            name = db["user:1"]["name"]          # "Ada"

    Nested update
    ~~~~~~~~~~~~~
        with Database("example.sqlite", autocommit=True) as db:
            db["user:1"]["prefs"]["theme"] = "light"  # persists "user:1" row

    List update (reassign to persist)
    ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
        with Database("example.sqlite", autocommit=True) as db:
            nums = db["user:1"].setdefault("nums", [1, 2, 3])
            nums.append(4)
            db["user:1"]["nums"] = nums  # reassignment persists the list change

    Batched writes
    ~~~~~~~~~~~~~~
        with Database("bulk.sqlite", autocommit=False) as db:
            for i in range(1000):
                db[f"k{i}"] = {"v": i}
            db.commit()  # or let a clean 'with' exit commit

    Deletion
    ~~~~~~~~
        with Database("example.sqlite", autocommit=True) as db:
            del db["user:1"]

    Custom JSON encoder
    ~~~~~~~~~~~~~~~~~~~
        import json
        def enc(o): return json.dumps(o, separators=(",", ":"))
        def dec(s): return json.loads(s)
        with Database("example.sqlite", encode=enc, decode=dec) as db:
            db["cfg"] = {"a": 1, "b": 2}

    Guarantees in tests
    -------------------
    The bundled self-tests verify:
      - JSON primitives round trip
      - Nested mapping edits persist
      - List updates persist when reassigned
      - Root len, iteration, membership
      - Deletion persists across reopen
      - autocommit=False commits on clean exit, not on exception
      - KeyError for missing keys and TypeError for invalid indexing
      - Overwriting with a different JSON type fully replaces the value
      - Optional custom JSON encode/decode round trips cleanly

    Limitations
    -----------
    - Values must be JSON-serializable. NaN and infinities are intentionally
      not supported as requested.
    - No automatic detection of in-place list mutation. Reassign the list,
      or design updates to operate at the parent mapping level.
    """)

def print_database_usage_doc() -> None:
    """Print the Database usage guide to stdout."""
    print(database_usage_doc())

# Make the usage guide the module docstring
__doc__ = database_usage_doc()


class Database(MutableMapping):
    """
    A nested, dict-like view over a SqliteDict where each top-level key maps to
    a JSON-serializable value (dict/list/primitive). Nested mappings are wrapped
    so in-place edits persist back to the single top-level row.

    Example:
        with Database("store.sqlite") as db:
            db["user:1"] = {"name": "Ada", "prefs": {"theme": "dark"}}
            db["user:1"]["prefs"]["theme"] = "light"   # auto-persists that row
    """
    __doc__ = (
        "Dict-like JSON-backed store on top of SqliteDict. "
        "Top-level keys are rows. Nested mapping edits rewrite the single row. "
        "See module docstring for full guide."
    )

    def __init__(self, filename,
                 autocommit=True,
                 encode=json.dumps,
                 decode=json.loads,
                 _root=None,
                 _path=()):
        self._is_root = _root is None
        if self._is_root:
            self._db = SqliteDict(filename, autocommit=autocommit,
                                  encode=encode, decode=decode)
            self._autocommit = autocommit
            self._encode = encode
            self._decode = decode
        else:
            # share the same backing store as the root
            self._db = _root._db
            self._autocommit = _root._autocommit
            self._encode = _root._encode
            self._decode = _root._decode

        self._root = self if self._is_root else _root
        # _path: () for root, else (top_key, k1, k2, ...)
        self._path = tuple(_path)

    # ------------- helpers -------------
    def _top_key(self):
        if not self._path:
            return None
        return self._path[0]

    def _load_top_row(self, top_key):
        """Load the entire object stored at top_key (decoded Python object)."""
        if top_key not in self._db:
            raise KeyError(top_key)
        return self._db[top_key]

    def _resolve_node(self, create_missing=False):
        """
        Return (top_key, top_obj, node) where node is the current mapping at _path.
        If create_missing=True, intermediate dicts are created if absent.
        """
        if not self._path:
            # root maps keys->values in SqliteDict, not a single row
            return (None, None, None)

        top = self._path[0]
        try:
            obj = self._load_top_row(top)
        except KeyError:
            if not create_missing:
                raise
            obj = {}
        node = obj
        for k in self._path[1:]:
            if isinstance(node, Mapping):
                if k in node:
                    node = node[k]
                else:
                    if create_missing:
                        # only create nested dicts if we need to
                        inner = {}
                        if isinstance(node, list):
                            raise TypeError("Cannot auto-create mapping inside a list")
                        node = (node := self._set_in(obj, self._path[1:], k, inner))
                        # The line above is complex; simpler approach below when we update
                        # We'll re-run traversal after insertion; break to re-traverse:
                        node = obj
                        for kk in self._path[1:]:
                            node = node[kk]
                    else:
                        raise KeyError(k)
            else:
                raise TypeError(f"Path hits non-mapping at {k!r}: {type(node)}")
        return (top, obj, node)

    def _write_top_row(self, top_key, obj):
        self._db[top_key] = obj  # SqliteDict handles (auto)commit

    # ------------- MutableMapping API -------------
    def __getitem__(self, key):
        if not self._path:
            # Root level: raise KeyError when missing
            if key in self._db:
                val = self._db[key]
            else:
                raise KeyError(key)
            if isinstance(val, Mapping):
                return Database(None, _root=self, _path=(key,))
            return val
        else:
            top, obj, node = self._resolve_node()
            if not isinstance(node, Mapping):
                raise TypeError(f"Cannot index into non-mapping at path {self._path}")
            val = node[key]  # this already raises KeyError when missing
            if isinstance(val, Mapping):
                return Database(None, _root=self._root, _path=self._path + (key,))
            elif isinstance(val, list):
                return TrackedList(val, self._root, self._path[0], self._path + (key,))
            return val

            return val

    def __contains__(self, key):
        if not self._path:
            return key in self._db
        top, obj, node = self._resolve_node()
        return isinstance(node, Mapping) and (key in node)

    def __setitem__(self, key, value):
        if not self._path:
            # root write: store value directly under key
            self._db[key] = value
            return

        # nested write: load top, mutate nested, write back the single top row
        top, obj, node = self._resolve_node(create_missing=True)

        if not isinstance(node, Mapping):
            raise TypeError(f"Cannot set item on non-mapping at path {self._path}")

        # Make sure node is a real dict if it's a Mapping (for JSON safety)
        if type(node) is not dict:
            # Rebuild the subtree as a plain dict to ensure JSON encoders are happy
            def to_plain(m):
                if isinstance(m, Mapping):
                    return {k: to_plain(v) for k, v in m.items()}
                elif isinstance(m, list):
                    return [to_plain(x) for x in m]
                else:
                    return m
            # Re-walk and replace the current node as a dict
            tmp = obj
            for k in self._path[1:-1]:
                tmp = tmp[k]
            tmp[self._path[-1]] = to_plain(tmp[self._path[-1]])
            node = tmp[self._path[-1]]

        node[key] = value
        self._write_top_row(top, obj)

    def __delitem__(self, key):
        if not self._path:
            del self._db[key]
            return
        top, obj, node = self._resolve_node()
        if not isinstance(node, Mapping):
            raise TypeError(f"Cannot delete from non-mapping at path {self._path}")
        del node[key]
        self._write_top_row(top, obj)

    def __iter__(self):
        if not self._path:
            yield from self._db.keys()
        else:
            top, obj, node = self._resolve_node()
            if not isinstance(node, Mapping):
                raise TypeError(f"Cannot iterate non-mapping at path {self._path}")
            yield from node.keys()

    def __len__(self):
        if not self._path:
            # SqliteDict doesn't expose len cheaply; this is O(n) but streaming
            return sum(1 for _ in self._db.keys())
        else:
            top, obj, node = self._resolve_node()
            if not isinstance(node, Mapping):
                raise TypeError(f"Cannot take len() of non-mapping at path {self._path}")
            return len(node)

    def __str__(self):
        """Return a human-readable JSON-like string of the current view."""
        try:
            if not self._path:
                # Root: show all top-level keys and values
                data = {k: self._db[k] for k in self._db.keys()}
            else:
                # Nested: show only the current mapping's contents
                _, _, node = self._resolve_node()
                data = node

            # Use JSON formatting for pretty printing
            return json.dumps(data, indent=2, ensure_ascii=False)
        except Exception as e:
            return f"<Database view error: {e}>"



    # ------------- convenience -------------
    def commit(self):
        """Flush pending writes if autocommit=False."""
        self._root._db.commit()

    def close(self):
        self._root._db.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        # If autocommit is off, commit on clean exit
        if exc is None and not self._root._autocommit:
            self._root._db.commit()
        self._root._db.close()

 # --- NEW STRUCTURE PRINTING FUNCTIONALITY ---


    def describe_structure(self, key=None):
        """
        Return a string describing the structure (key/type layout only)
        of the database or a specific key, deduplicating identical substructures.
        """
        lines = []
        seen_structures = {}

        def structure_signature(obj):
            """Generate a hashable signature for a dict/list structure."""
            if isinstance(obj, dict):
                return tuple(sorted((k, structure_signature(v)) for k, v in obj.items()))
            elif isinstance(obj, list):
                if not obj:
                    return ('list', None)
                return ('list', structure_signature(obj[0]))
            else:
                return type(obj).__name__

        def _describe(obj, indent=0, name=None):
            prefix = "  " * indent
            sig = structure_signature(obj)
            sig_hash = hashlib.md5(str(sig).encode()).hexdigest()

            # If we've seen this structure before, just reference it
            if sig_hash in seen_structures:
                lines.append(f"{prefix}{name or 'structure'}: (same as {seen_structures[sig_hash]})")
                return
            else:
                seen_structures[sig_hash] = name or f"structure_{len(seen_structures) + 1}"

            if isinstance(obj, dict):
                lines.append(f"{prefix}{name or 'dict'}: dict ({len(obj)} keys)")
                for k, v in obj.items():
                    if isinstance(v, (dict, list)):
                        _describe(v, indent + 1, name=repr(k))
                    else:
                        lines.append(f"{prefix}  {repr(k)}: {type(v).__name__}")
            elif isinstance(obj, list):
                lines.append(f"{prefix}{name or 'list'}: list ({len(obj)} items)")
                if obj:
                    sample = obj[0]
                    if isinstance(sample, (dict, list)):
                        _describe(sample, indent + 1, name='[item]')
                    else:
                        lines.append(f"{prefix}  [item]: {type(sample).__name__}")
            else:
                lines.append(f"{prefix}{name or 'value'}: {type(obj).__name__}")

        db_path = getattr(self, "_path", None)
        lines.append(f"Inspecting database: {db_path if db_path else '(unknown path)'}")

        if not hasattr(self, "_db"):
            lines.append("Error: this Database instance has no '_db' attribute.")
            return "\n".join(lines)

        if key is not None:
            if key not in self._db:
                lines.append(f"Key '{key}' not found.")
                return "\n".join(lines)
            lines.append(f"\nTop-level key: {repr(key)}")
            _describe(self._db[key], indent=1)
        else:
            if not self._db:
                lines.append("  (empty database)")
                return "\n".join(lines)
            for k in self._db.keys():
                lines.append(f"\nTop-level key: {repr(k)}")
                _describe(self._db[k], indent=1)

        return "\n".join(lines)


class TrackedList(list):
    """A list wrapper that auto-persists changes back to its parent Database row."""
    def __init__(self, data, parent_db, top_key, full_path):
        super().__init__(data)
        self._parent_db = parent_db
        self._top_key = top_key
        self._full_path = full_path  # e.g., ('user123', 'prefs', 'tags')

    def _persist(self):
        # Reload the full top-level object, modify the nested value, and write it back
        obj = self._parent_db._load_top_row(self._top_key)
        node = obj
        for k in self._full_path[1:-1]:
            node = node[k]
        node[self._full_path[-1]] = list(self)
        self._parent_db._write_top_row(self._top_key, obj)

    # --- Override mutation methods ---
    def append(self, item):
        super().append(item)
        self._persist()

    def extend(self, iterable):
        super().extend(iterable)
        self._persist()

    def insert(self, index, item):
        super().insert(index, item)
        self._persist()

    def remove(self, item):
        super().remove(item)
        self._persist()

    def pop(self, index=-1):
        item = super().pop(index)
        self._persist()
        return item

    def clear(self):
        super().clear()
        self._persist()

    def sort(self, *args, **kwargs):
        super().sort(*args, **kwargs)
        self._persist()

    def reverse(self):
        super().reverse()
        self._persist()


# ----------------- SELF-TESTS (functional, no timing) -----------------
if __name__ == "__main__":
    import os, tempfile, traceback, json

    # Small harness that runs each test and prints pass/fail.

    def _extract_reason(fn):
        """
        Parse the test function's docstring.
        Returns (what, why) where:
          - 'what' = first non-empty line
          - 'why'  = line starting with 'WHY:' (case-insensitive), without the prefix
        """
        doc = (fn.__doc__ or "").strip()
        if not doc:
            return None, None
        lines = [ln.strip() for ln in doc.splitlines() if ln.strip()]
        what = None
        why = None
        for ln in lines:
            if ln.upper().startswith("WHY:"):
                why = ln[4:].strip(" :")
            elif what is None:
                what = ln
        return what, why


    def run_test(fn):
        what, why = _extract_reason(fn)
        try:
            fn()
            print(f"{fn.__name__}: ✓")
            if what:
                print(f"  What: {what}")
            if why:
                print(f"  Why : {why}")
            return True
        except Exception as e:
            print(f"{fn.__name__}: ✗ {e.__class__.__name__}: {e}")
            if what:
                print(f"  While: {what}")
            if why:
                print(f"  Why  : {why}")
            traceback.print_exc()
            return False


    # ----- end harness replacement -----

    def fresh_path(tmp, name):
        return os.path.join(tmp, f"{name}.sqlite")

    # ------------------------------------------------------------------
    # CORE BEHAVIOR TESTS (original set, redrafted with explanations)
    # ------------------------------------------------------------------

    def test_primitives_and_null():
        """
        Store and retrieve all JSON primitives: int, float, str, bool, null.
        WHY: Confirms encode/decode is lossless for JSON basic types.
        """
        with tempfile.TemporaryDirectory() as td:
            p = fresh_path(td, "primitives")
            with Database(p, autocommit=True) as db:
                db["int"] = 42
                db["float"] = 3.14
                db["str"] = "hello"
                db["bool_true"] = True
                db["bool_false"] = False
                db["null"] = None
            with Database(p) as db:
                assert db["int"] == 42
                assert db["float"] == 3.14
                assert db["str"] == "hello"
                assert db["bool_true"] is True
                assert db["bool_false"] is False
                assert db["null"] is None

    def test_nested_objects_and_write_through():
        """
        Edit a nested mapping + a list (with reassignment) and verify persistence.
        WHY: Your model rewrites the top-level row; nested edits must stick.
        """
        with tempfile.TemporaryDirectory() as td:
            p = fresh_path(td, "nested")
            with Database(p, autocommit=True) as db:
                db["user:1"] = {"name": "Ada", "prefs": {"theme": "dark", "tags": ["alpha"]}}
                # Change nested dict in place via wrappers -> should persist
                db["user:1"]["prefs"]["theme"] = "light"
                # For lists: mutate local list, then REASSIGN back -> should persist
                tags = db["user:1"]["prefs"]["tags"]
                tags.append("beta")
                db["user:1"]["prefs"]["tags"] = tags
            with Database(p) as db:
                assert db["user:1"]["prefs"]["theme"] == "light"
                assert db["user:1"]["prefs"]["tags"] == ["alpha", "beta"]

    def test_len_and_iter():
        """
        len(db) counts top-level keys; iter(db) yields exactly those keys.
        WHY: Ensures mapping semantics align with expectations (no hidden preload state).
        """
        with tempfile.TemporaryDirectory() as td:
            p = fresh_path(td, "len_iter")
            with Database(p, autocommit=True) as db:
                for i in range(3):
                    db[f"k{i}"] = i
                keys = set(iter(db))
                assert len(db) == 3
                assert keys == {"k0", "k1", "k2"}

    def test_delete_top_level_key():
        """
        Deleting a top-level key removes it from SQLite and stays deleted after reopen.
        WHY: Verifies delete propagation and no stale caches.
        """
        with tempfile.TemporaryDirectory() as td:
            p = fresh_path(td, "delete")
            with Database(p, autocommit=True) as db:
                db["todelete"] = {"x": 1}
                assert "todelete" in set(db)
                del db["todelete"]
                assert "todelete" not in set(db)
            with Database(p) as db:
                assert "todelete" not in set(db)

    def test_autocommit_false_commits_on_exit():
        """
        With autocommit=False, a clean context exit commits changes.
        WHY: Validates the context manager's 'commit on clean exit' behavior.
        """
        with tempfile.TemporaryDirectory() as td:
            p = fresh_path(td, "commit_on_exit")
            with Database(p, autocommit=False) as db:
                db["planet"] = {"name": "Earth", "moons": 1}  # no explicit commit()
            with Database(p) as db:
                assert db["planet"]["name"] == "Earth"
                assert db["planet"]["moons"] == 1

    def test_deep_chain_writes():
        """
        Create and update a deep path via wrappers; verify persisted value.
        WHY: Exercises traversal and read-modify-write for nested structures.
        """
        with tempfile.TemporaryDirectory() as td:
            p = fresh_path(td, "deep")
            with Database(p, autocommit=True) as db:
                db["A"] = {}
                db["A"]["B"] = {}
                db["A"]["B"]["C"] = {"n": 0}
                db["A"]["B"]["C"]["n"] = 7
            with Database(p) as db:
                assert db["A"]["B"]["C"]["n"] == 7

    def test_list_replace_persists():
        """
        In-place list mutation requires reassignment to persist.
        WHY: Documents the contract for list updates to avoid data loss.
        """
        with tempfile.TemporaryDirectory() as td:
            p = fresh_path(td, "list_replace")
            with Database(p, autocommit=True) as db:
                db["arr"] = {"nums": [1, 2, 3]}
                nums = db["arr"]["nums"]
                nums.extend([4, 5])           # local mutation
                db["arr"]["nums"] = nums      # reassignment triggers persistence
            with Database(p) as db:
                assert db["arr"]["nums"] == [1, 2, 3, 4, 5]

    def test_missing_key_raises_keyerror():
        """
        Accessing a missing top-level key raises KeyError (distinct from stored null).
        WHY: Prevents silent 'None' returns that mask missing data.
        """
        with tempfile.TemporaryDirectory() as td:
            p = fresh_path(td, "missing")
            with Database(p, autocommit=True) as db:
                db["exists"] = None
                # Existing key with null should not raise
                _ = db["exists"]
                # Missing key must raise
                try:
                    _ = db["nope"]
                except KeyError:
                    pass
                else:
                    raise AssertionError("Expected KeyError for missing key")

    def test_overwrite_value_type():
        """
        Overwriting a value with a different JSON type fully replaces the stored value.
        WHY: Ensures no stale fragments remain after type changes.
        """
        with tempfile.TemporaryDirectory() as td:
            p = fresh_path(td, "overwrite")
            with Database(p, autocommit=True) as db:
                db["x"] = {"a": 1}
                db["x"] = [1, 2, 3]  # replace object with array
            with Database(p) as db:
                assert db["x"] == [1, 2, 3]

    # ------------------------------------------------------------------
    # EXTRA EDGE-CASE TESTS (recommended additions)
    # ------------------------------------------------------------------

    def test_contains_and_membership():
        """
        Membership checks ('k' in db) match KeyError semantics; also check nested.
        WHY: Lets callers branch safely before access; ensures __iter__/__contains__ agree.
        """
        with tempfile.TemporaryDirectory() as td:
            p = fresh_path(td, "contains")
            with Database(p, autocommit=True) as db:
                db["root"] = {"child": 1}
                assert "root" in db
                assert "missing" not in db
                assert "child" in db["root"]
                assert "nope" not in db["root"]

    def test_delete_nested_key():
        """
        Delete a nested field via wrapper and verify it persists after reopen.
        WHY: Confirms nested deletions write back to the single top-level row.
        """
        with tempfile.TemporaryDirectory() as td:
            p = fresh_path(td, "del_nested")
            with Database(p, autocommit=True) as db:
                db["obj"] = {"a": {"b": 1, "c": 2}}
                del db["obj"]["a"]["b"]
            with Database(p) as db:
                assert "b" not in db["obj"]["a"]
                assert db["obj"]["a"]["c"] == 2

    def test_non_mapping_index_type_error():
        """
        Indexing into a non-mapping raises TypeError (e.g., db['x'] is a number).
        WHY: Produces clear errors when callers assume the wrong shape.
        """
        with tempfile.TemporaryDirectory() as td:
            p = fresh_path(td, "non_mapping")
            with Database(p, autocommit=True) as db:
                db["x"] = 5
                try:
                    _ = db["x"]["y"]  # invalid: int is not a mapping
                except TypeError:
                    pass
                else:
                    raise AssertionError("Expected TypeError when indexing non-mapping")

    def test_custom_json_encoder_decoder():
        """
        Use custom JSON encode/decode (e.g., sorted keys) and verify round-trip.
        WHY: Ensures the handler does not rely on pickle-only features and stays JSON-safe.
        """
        with tempfile.TemporaryDirectory() as td:
            p = fresh_path(td, "custom_json")
            def enc(o): return json.dumps(o, sort_keys=True)
            def dec(s): return json.loads(s)
            with Database(p, autocommit=True, encode=enc, decode=dec) as db:
                db["obj"] = {"z": 1, "a": {"m": 2}}
            with Database(p, encode=enc, decode=dec) as db:
                assert db["obj"] == {"a": {"m": 2}, "z": 1}

    def test_context_manager_close_on_exception():
        """
        With autocommit=False, raising inside the context should NOT commit.
        WHY: Validates durability expectations on error (rollback semantics).
        """
        with tempfile.TemporaryDirectory() as td:
            p = fresh_path(td, "rollback_on_exception")
            try:
                with Database(p, autocommit=False) as db:
                    db["will_rollback"] = {"x": 1}
                    raise RuntimeError("boom")  # force non-clean exit
            except RuntimeError:
                pass
            # After exception, the uncommitted write should be absent
            with Database(p) as db:
                assert "will_rollback" not in set(db)

    # Collect and run all tests
    TESTS = [
        # Core suite
        test_primitives_and_null,
        test_nested_objects_and_write_through,
        test_len_and_iter,
        test_delete_top_level_key,
        test_autocommit_false_commits_on_exit,
        test_deep_chain_writes,
        test_list_replace_persists,
        test_missing_key_raises_keyerror,
        test_overwrite_value_type,
        # Extras
        test_contains_and_membership,
        test_delete_nested_key,
        test_non_mapping_index_type_error,
        test_custom_json_encoder_decoder,
        test_context_manager_close_on_exception,
    ]

    print(f"Running {len(TESTS)} tests...\n")
    results = [run_test(t) for t in TESTS]
    passed = sum(results)
    failed = len(TESTS) - passed
    print(f"\nSummary: {passed} passed, {failed} failed")
    if failed == 0:
        print("ALL TESTS PASSED ✅")
    else:
        raise SystemExit(1)

