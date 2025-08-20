import json
from sqlitedict import SqliteDict

class Database(dict):
    def __init__(self, filename, autocommit=True, encode=json.dumps, decode=json.loads,
                 _parent=None, _parent_key=None):
        super().__init__()
        self._db = SqliteDict(filename, autocommit=autocommit, encode=encode, decode=decode) if _parent is None else _parent._db
        self._parent = _parent
        self._parent_key = _parent_key

        # Load top-level keys if root
        if _parent is None:
            for key in self._db.keys():
                dict.__setitem__(self, key, self._wrap(self._db[key], key))

    # --- Internal wrapper for nested dicts ---
    def _wrap(self, value, key=None):
        if isinstance(value, dict):
            # Wrap nested dicts as Database, set parent reference
            return Database(None, _parent=self, _parent_key=key)._update_internal(value)
        return value

    def _update_internal(self, data):
        for k, v in data.items():
            dict.__setitem__(self, k, self._wrap(v, k))
        return self

    # --- Override __getitem__ ---
    def __getitem__(self, key):
        if key in self:
            return dict.__getitem__(self, key)
        val = self._db.get(key)
        if val is not None:
            dict.__setitem__(self, key, self._wrap(val, key))
            return dict.__getitem__(self, key)
        raise KeyError(key)

    # --- Override __setitem__ ---
    def __setitem__(self, key, value):
        dict.__setitem__(self, key, self._wrap(value, key) if isinstance(value, dict) else value)
        if self._parent is None:
            # Persist top-level key without calling __getitem__
            self._db[key] = self._serialize(dict.__getitem__(self, key))
        else:
            self._parent._persist_child(self._parent_key)

    # --- Persistence helpers ---
    def _persist_child(self, key):
        self._db[key] = self._serialize(dict.__getitem__(self, key))
        if self._parent:
            self._parent._persist_child(self._parent_key)

    def _serialize(self, obj):
        # Convert Database to plain dict for storage
        if isinstance(obj, Database):
            return {k: self._serialize(v) for k, v in obj.items()}
        return obj

    def __delitem__(self, key):
        dict.__delitem__(self, key)
        if self._parent is None:
            if key in self._db:
                del self._db[key]
        else:
            self._parent._persist_child(self._parent_key)

class Database2(dict):
    def __init__(self, filename, autocommit=True, encode=json.dumps, decode=json.loads, parent_keys=[]):
        super().__init__()
        if filename and '.' in filename:
            filename = filename.split('.')[0]
        if not parent_keys:
            parent_keys = [filename]
        filename = '_'.join(parent_keys) + '.db'
        print("DB FILENAME", filename)
        self._filename = filename
        self._db = SqliteDict(filename, autocommit=autocommit, encode=encode, decode=decode)
        self._parent_keys = parent_keys

    # --- Override __getitem__ ---
    def __getitem__(self, key):
        return super().__getitem__(key)

    # --- Override __setitem__ ---
    def __setitem__(self, key, value):
        print("SETTING", key, type(key), value, type(value))
        if isinstance(value, dict):
            new_value = Database2(None, parent_keys=self._parent_keys + [key])
            print("new_file", new_value._filename)
            for k, v in value.items():
                new_value[k] = new_value
        else:
            new_value = value
        super().__setitem__(key, new_value)

    def __delitem__(self, key):
        super().__delitem__(key)




if __name__ == "__main__":
    db = Database2('test.db')
    db['test'] = {'a': {'b': 1, 'c': 2}}
    print(db, type(db))
    print(db['test'], type(db['test']))
    print(db['test']['a'], type(db['test']['a']))
    print(db._db)
    db['test']['a'] = 3
    # db['test']['c'] = 3
    # print(db['test'])
    # db['test']['a'] = 4
    # db['test'] = db['test']
    # print(db['test'])