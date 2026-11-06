"""Shared image-type rules for web, CLI and generation."""
import copy
import hashlib
from functools import lru_cache
from pathlib import Path

TYPES = {"character": "주인공/조연", "npc": "NPC", "monster": "몬스터"}
DEFAULT_TYPE_PROMPTS = {
    "character": {"positive": "game character, detailed character design, expressive face", "negative": ""},
    "npc": {"positive": "game character, clear silhouette, readable character design", "negative": ""},
    "monster": {
        "positive": "(monster:1.3), (non-human creature:1.3), creature design, distinct creature anatomy",
        "negative": "(1girl:1.4), (1boy:1.4), human, humanoid, monster girl, human face, human skin, breasts, school uniform, human clothes",
    },
}


def prompt_profiles(cfg):
    custom = cfg.get("type_prompts", {})
    return {kind: {**defaults, **custom.get(kind, {})} for kind, defaults in DEFAULT_TYPE_PROMPTS.items()}


def image_type(row):
    value = (row.get("image_type") or "character").strip()
    if value not in TYPES:
        raise ValueError(f"알 수 없는 이미지 유형: {value}")
    return value


def effective_config(cfg, row):
    result = copy.deepcopy(cfg)
    kind = image_type(row)
    if kind == "monster":
        result.setdefault("controlnet", {})["enabled"] = False
        result.setdefault("headroom", {})["enabled"] = False
    else:
        pose = cfg.get("type_poses", {}).get(kind)
        if pose:
            result.setdefault("controlnet", {}).update(pose=pose, enabled=True)
    return result


def fingerprint(path):
    path = Path(path)
    try:
        st = path.stat()
        return _digest(str(path.resolve()), st.st_mtime_ns, st.st_ctime_ns, st.st_size)
    except FileNotFoundError:
        return None


@lru_cache(maxsize=2048)
def _digest(path, mtime, ctime, size):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()
