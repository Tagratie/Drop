"""Multi-library data model. Persists to ~/.drop/library.json."""
import os
import re
import json
import shutil
from pathlib import Path

from .theme import LIBRARIES_DIR, script_dir
from .utils import safe_dirname, unique_path


def migrate_libraries_dir(library_file):
    """One-time relocation of any old Libraries/ folder into ~/.drop/Libraries/,
    plus path rewrites in library.json so existing entries stay playable.

    Checks both:
      * <script_dir>/Libraries        (dev runs from project root)
      * <script_dir>/dist/Libraries   (older frozen Drop.exe builds)

    Idempotent: silent no-op once nothing is left to move. Safe to call on
    every launch."""
    new = LIBRARIES_DIR
    sd = script_dir()
    candidates = [sd / "Libraries", sd / "dist" / "Libraries"]

    moved_pairs = []  # (old_prefix, new_prefix) for path rewriting

    for old in candidates:
        try:
            if not old.exists():
                continue
            if old.resolve() == new.resolve():
                continue
        except Exception:
            continue

        try:
            new.parent.mkdir(parents=True, exist_ok=True)
            new.mkdir(exist_ok=True)
            # Merge each per-library subfolder into the corresponding one
            # under new/. shutil.move on a dir into an existing dir would
            # nest it (creating new/Edits/Edits/), so iterate one level deeper.
            for sub in list(old.iterdir()):
                if sub.is_dir():
                    target_dir = new / sub.name
                    target_dir.mkdir(exist_ok=True)
                    for child in list(sub.iterdir()):
                        target = target_dir / child.name
                        if target.exists():
                            continue  # don't clobber existing
                        shutil.move(str(child), str(target))
                    try: sub.rmdir()
                    except OSError: pass
                else:
                    target = new / sub.name
                    if target.exists():
                        continue
                    shutil.move(str(sub), str(target))
            try:
                old.rmdir()
            except OSError:
                pass
            moved_pairs.append((str(old), str(new)))
        except Exception:
            # Move failed — skip path rewriting for this candidate so
            # library.json keeps pointing at the (still-present) files.
            continue

    if not moved_pairs:
        return

    try:
        data = json.loads(library_file.read_text(encoding="utf-8"))
    except Exception:
        return

    def _rewrite(items):
        changed = False
        for it in items:
            p = it.get("path") or ""
            for old_prefix, new_prefix in moved_pairs:
                if p.startswith(old_prefix):
                    candidate = new_prefix + p[len(old_prefix):]
                    if Path(candidate).exists():
                        it["path"] = candidate
                        changed = True
                    break
        return changed

    changed = False
    if isinstance(data, dict) and "items" in data:
        for lib_items in data["items"].values():
            changed = _rewrite(lib_items) or changed
    elif isinstance(data, list):
        changed = _rewrite(data)

    if changed:
        try:
            library_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception:
            pass


class Library:
    """Holds multiple named libraries of downloaded items."""
    MAX_PER = 200

    def __init__(self, path):
        self.path = path
        self.data = self._load()
        if not self.data["libraries"]:
            self.data["libraries"] = ["Default"]
            self.data["items"]["Default"] = []
        for n in self.data["libraries"]:
            self.data["items"].setdefault(n, [])
        if self.data.get("active") not in self.data["libraries"]:
            self.data["active"] = self.data["libraries"][0]

    def _load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                # legacy single-list format → migrate
                return {"libraries": ["Default"], "items": {"Default": data}, "active": "Default"}
            if isinstance(data, dict) and "libraries" in data:
                return data
        except Exception:
            pass
        return {"libraries": ["Default"], "items": {"Default": []}, "active": "Default"}

    def save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        except Exception:
            pass

    @property
    def names(self):  return list(self.data["libraries"])
    @property
    def active(self): return self.data["active"]

    def items_in(self, name):
        return self.data["items"].get(name, [])

    def set_active(self, name):
        if name in self.data["libraries"]:
            self.data["active"] = name
            self.save()

    def create(self, name):
        name = (name or "").strip()
        if not name or name in self.data["libraries"]:
            return False
        self.data["libraries"].append(name)
        self.data["items"][name] = []
        self.save()
        return True

    def rename(self, old, new):
        new = (new or "").strip()
        if old not in self.data["libraries"]: return False
        if not new or new == old or new in self.data["libraries"]: return False

        # Rename folder on disk if it exists
        old_dir = self.dir_for(old)
        new_dir = self.dir_for(new)
        if old_dir.exists() and not new_dir.exists():
            try:
                shutil.move(str(old_dir), str(new_dir))
                # Update any item paths that pointed inside the old folder
                old_prefix = str(old_dir)
                new_prefix = str(new_dir)
                for it in self.data["items"].get(old, []):
                    p = it.get("path") or ""
                    if p.startswith(old_prefix):
                        it["path"] = new_prefix + p[len(old_prefix):]
            except Exception:
                pass

        i = self.data["libraries"].index(old)
        self.data["libraries"][i] = new
        self.data["items"][new] = self.data["items"].pop(old, [])
        if self.data["active"] == old:
            self.data["active"] = new
        self.save()
        return True

    def delete_lib(self, name):
        if name not in self.data["libraries"]: return False
        if len(self.data["libraries"]) <= 1:   return False
        self.data["libraries"].remove(name)
        self.data["items"].pop(name, None)
        if self.data["active"] == name:
            self.data["active"] = self.data["libraries"][0]
        self.save()
        return True

    def add(self, lib, item):
        if lib not in self.data["libraries"]: return
        items = self.data["items"].setdefault(lib, [])
        items[:] = [x for x in items if x.get("path") != item.get("path")]
        items.insert(0, item)
        del items[self.MAX_PER:]
        self.save()

    def remove(self, lib, idx):
        items = self.data["items"].get(lib, [])
        if 0 <= idx < len(items):
            del items[idx]
            self.save()

    def toggle_favorite(self, lib, idx):
        """Flip the `favorite` flag on items[lib][idx]. When favoriting, also
        moves the item to the head of the list so a newly-pinned item lands
        at the very top of the favorites section (the visible sort is stable,
        so otherwise it'd appear *after* any existing favorites). Returns
        (new_flag: bool, new_idx: int)."""
        items = self.data["items"].get(lib, [])
        if not (0 <= idx < len(items)):
            return False, idx
        new = not bool(items[idx].get("favorite", False))
        items[idx]["favorite"] = new
        new_idx = idx
        if new:
            item = items.pop(idx)
            items.insert(0, item)
            new_idx = 0
        self.save()
        return new, new_idx

    def reorder(self, lib, from_idx, to_idx):
        """Move an item within the same library from one position to another."""
        items = self.data["items"].get(lib, [])
        n = len(items)
        if not (0 <= from_idx < n): return
        to_idx = max(0, min(to_idx, n - 1))
        if from_idx == to_idx: return
        item = items.pop(from_idx)
        items.insert(to_idx, item)
        self.save()

    def move(self, from_lib, idx, to_lib):
        if from_lib == to_lib: return
        items = self.data["items"].get(from_lib, [])
        if not (0 <= idx < len(items)): return
        if to_lib not in self.data["libraries"]: return
        item = items.pop(idx)
        target = self.data["items"].setdefault(to_lib, [])
        target[:] = [x for x in target if x.get("path") != item.get("path")]
        target.insert(0, item)
        del target[self.MAX_PER:]
        self.save()

    def clear(self, lib):
        if lib in self.data["items"]:
            self.data["items"][lib] = []
            self.save()

    def find_by_path(self, path):
        for lib in self.data["libraries"]:
            for i, it in enumerate(self.data["items"].get(lib, [])):
                if it.get("path") == path:
                    return lib, i
        return None, None

    # ── on-disk folder management ────────────────────────────────────────────
    def dir_for(self, name):
        return LIBRARIES_DIR / safe_dirname(name)

    def ensure_dir(self, name):
        d = self.dir_for(name)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def place_in(self, name, src_path):
        """Copy a file into Libraries/<name>/. Returns the new path or None."""
        if not src_path:
            return None
        src = Path(src_path)
        if not src.exists() or not src.is_file():
            return None
        dst_dir = self.ensure_dir(name)
        if src.parent == dst_dir:
            return str(src)  # already in the right place
        dst = unique_path(dst_dir / src.name)
        try:
            shutil.copy2(src, dst)
            return str(dst)
        except Exception:
            return None

    def move_file(self, from_lib, idx, to_lib):
        """Physically move a row's file between library folders, then update path."""
        items = self.data["items"].get(from_lib, [])
        if not (0 <= idx < len(items)):
            return False
        item = items[idx]
        src_str = item.get("path")
        if not src_str:
            return False
        src = Path(src_str)
        dst_dir = self.ensure_dir(to_lib)

        if src.exists():
            # If the file already lives inside the target folder, no move needed.
            if src.parent == dst_dir:
                return True
            dst = unique_path(dst_dir / src.name)
            try:
                shutil.move(str(src), str(dst))
                item["path"] = str(dst)
                return True
            except Exception:
                # Fall back to copy if move fails (cross-device etc.)
                try:
                    shutil.copy2(src, dst)
                    item["path"] = str(dst)
                    return True
                except Exception:
                    return False
        # File is missing — bring the entry along anyway, but don't update path.
        return True


# ── downloader ───────────────────────────────────────────────────────────────

