"""Profiles: named sets of gesture -> action mappings, stored as JSON."""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .actions import ACTIONS
from .gestures import Gesture

ROOT = Path(__file__).resolve().parent.parent
PROFILES_DIR = ROOT / "profiles"
SETTINGS_FILE = ROOT / "settings.json"


@dataclass
class Mapping:
    action: str
    gesture: Gesture
    hold_ms: int = 120
    image: str | None = None   # path (relative to ROOT) of a recorded snapshot

    def to_dict(self):
        return {"action": self.action, "gesture": self.gesture.to_dict(),
                "hold_ms": self.hold_ms, "image": self.image}

    @classmethod
    def from_dict(cls, d):
        return cls(d["action"], Gesture.from_dict(d["gesture"]),
                   int(d.get("hold_ms", 120)), d.get("image"))


@dataclass
class Profile:
    id: str
    name: str
    mappings: list[Mapping] = field(default_factory=list)
    bundled: bool = False

    def mapping_for(self, action_id: str) -> Mapping | None:
        return next((m for m in self.mappings if m.action == action_id), None)

    def set_mapping(self, mapping: Mapping):
        self.mappings = [m for m in self.mappings if m.action != mapping.action]
        self.mappings.append(mapping)

    def clear_mapping(self, action_id: str):
        self.mappings = [m for m in self.mappings if m.action != action_id]

    def conflict(self, gesture: Gesture, except_action: str) -> Mapping | None:
        return next((m for m in self.mappings
                     if m.gesture == gesture and m.action != except_action), None)

    def ordered_mappings(self) -> list[Mapping]:
        """Most specific gesture first; the engine takes the first match."""
        valid = [m for m in self.mappings if m.action in ACTIONS]
        return sorted(valid, key=lambda m: m.gesture.specificity(), reverse=True)

    def to_dict(self):
        return {"name": self.name, "bundled": self.bundled,
                "mappings": [m.to_dict() for m in self.mappings]}

    @classmethod
    def from_dict(cls, pid, d):
        return cls(pid, d.get("name", pid),
                   [Mapping.from_dict(m) for m in d.get("mappings", [])],
                   bool(d.get("bundled", False)))


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s or "profile"


class ProfileStore:
    def __init__(self, directory: Path = PROFILES_DIR, settings_file: Path = SETTINGS_FILE):
        self.dir = directory
        self.settings_file = settings_file
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "images").mkdir(exist_ok=True)

    def ensure_bundled(self, overwrite: bool = False):
        from .defaults import BUNDLED
        for profile in BUNDLED():
            path = self._path(profile.id)
            if overwrite or not path.exists():
                self.save(profile)

    def _path(self, pid: str) -> Path:
        return self.dir / f"{pid}.json"

    def list_ids(self) -> list[str]:
        return sorted(p.stem for p in self.dir.glob("*.json"))

    def load(self, pid: str) -> Profile:
        with open(self._path(pid), encoding="utf-8") as f:
            return Profile.from_dict(pid, json.load(f))

    def load_all(self) -> list[Profile]:
        out = []
        for pid in self.list_ids():
            try:
                out.append(self.load(pid))
            except (OSError, ValueError, KeyError):
                continue
        out.sort(key=lambda p: (not p.bundled, p.name.lower()))
        return out

    def save(self, profile: Profile):
        with open(self._path(profile.id), "w", encoding="utf-8") as f:
            json.dump(profile.to_dict(), f, indent=2)

    def delete(self, pid: str):
        path = self._path(pid)
        if path.exists():
            path.unlink()

    def new_id(self, name: str) -> str:
        base = slugify(name)
        pid, n = base, 2
        while self._path(pid).exists():
            pid = f"{base}-{n}"
            n += 1
        return pid

    def copy(self, source: Profile, name: str) -> Profile:
        pid = self.new_id(name)
        clone = Profile(pid, name, [Mapping(m.action, m.gesture, m.hold_ms, m.image)
                                    for m in source.mappings])
        self.save(clone)
        return clone

    # settings (small key/value file)
    def _settings(self) -> dict:
        try:
            with open(self.settings_file, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def get_setting(self, key: str, default=None):
        return self._settings().get(key, default)

    def set_setting(self, key: str, value):
        data = self._settings()
        data[key] = value
        with open(self.settings_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
