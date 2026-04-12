"""
bone_mapper_bridge.py
---------------------
Shared helpers that let the WoW Tools importer/exporter auto-apply bone maps
from the Bone-Mapper add-on (if it is installed and has matching maps).

Both io_import_wow_m2i.py and io_export_wow_m2i.py import from here so the
logic lives in exactly one place.
"""

from __future__ import annotations
import importlib
import importlib.util
from pathlib import Path


# ---------------------------------------------------------------------------
# Bone-map discovery
# ---------------------------------------------------------------------------

def _find_bone_mapper_dir() -> Path | None:
    """Return the bone_maps/ directory inside the Bone-Mapper add-on, or None."""
    try:
        import addon_utils
        for mod in addon_utils.modules():
            if getattr(mod, "bl_info", {}).get("name") == "Bone-Mapper":
                return Path(mod.__file__).parent / "bone_maps"
    except Exception:
        pass
    return None


def _iter_bone_map_files(directory: Path):
    """Yield every .py bone-map file found recursively under *directory*."""
    for item in sorted(directory.rglob("*.py")):
        if item.stem != "__init__":
            yield item


def _load_module_from_path(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def find_bone_map_for_m2(m2_filename: str) -> dict | None:
    """
    Given an M2 filename (e.g. ``"alleria3.m2"``), scan all bone-map files for
    one that lists that filename in its ``M2`` attribute.

    Returns a bone-map dict with keys ``id``, ``label``, ``bones``, ``type``,
    ``subtype``, ``mod``  — or ``None`` if no match is found.

    Matching is case-insensitive and extension-agnostic, so ``"alleria3"``,
    ``"alleria3.m2"``, and ``"alleria3.m2i"`` all match each other.
    """
    bone_maps_dir = _find_bone_mapper_dir()
    if bone_maps_dir is None or not bone_maps_dir.is_dir():
        return None

    # Strip any extension so "alleria3.m2i", "alleria3.m2", "alleria3" all match
    target = Path(m2_filename).stem.lower()

    for path in _iter_bone_map_files(bone_maps_dir):
        module_name = "bone_mapper_bridge_tmp." + path.stem
        try:
            mod = _load_module_from_path(path, module_name)
        except Exception as e:
            print(f"[BoneMapperBridge] Could not load '{path}': {e}")
            continue

        raw_m2 = getattr(mod, "M2", None)
        if raw_m2 is None:
            continue

        # M2 may be a single string or a list of strings.
        # Strip extensions from entries too so "alleria3", "alleria3.m2" etc. all work.
        if isinstance(raw_m2, str):
            raw_m2 = [raw_m2]

        if any(Path(entry).stem.lower() == target for entry in raw_m2):
            label   = getattr(mod, "LABEL",   path.stem.replace("_", " ").title())
            bones   = getattr(mod, "BONES",   [])
            a_type  = getattr(mod, "TYPE",    None)
            subtype = getattr(mod, "SUBTYPE", None)
            # Build a stable id the same way the Bone-Mapper does
            rel = path.relative_to(bone_maps_dir.parent)
            mod_id  = str(rel.with_suffix("")).replace("/", ".").replace("\\", ".")

            return {
                "id":      mod_id,
                "label":   label,
                "bones":   bones,
                "type":    a_type,
                "subtype": subtype,
                "mod":     mod,
            }

    return None


# ---------------------------------------------------------------------------
# Rename helpers (mirror of the Bone-Mapper's own helpers)
# ---------------------------------------------------------------------------

def rename_bones(armature_obj, bones: list[tuple[str, str]], to_mirrored: bool) -> int:
    """
    Rename bones on *armature_obj* in either direction.

    :param to_mirrored: ``True``  → indexed  names  →  mirrored names  (post-import)
                        ``False`` → mirrored names  →  indexed  names  (pre-export)
    :returns: number of bones actually renamed.
    """
    bone_data = armature_obj.data.bones
    renamed = 0
    for indexed, mirrored in bones:
        src = indexed  if to_mirrored else mirrored
        dst = mirrored if to_mirrored else indexed
        if src in bone_data:
            bone_data[src].name = dst
            renamed += 1
    return renamed


def apply_bone_map_post_import(armature_obj, m2_filename: str) -> str | None:
    """
    Called right after a successful M2I import.
    Finds a matching bone map, renames bones to mirrored names, and stamps
    ``bone_mapper_active`` on the armature so the exporter can reverse it.

    :returns: the bone-map label if one was applied, else ``None``.
    """
    bone_map = find_bone_map_for_m2(m2_filename)
    if bone_map is None:
        return None

    count = rename_bones(armature_obj, bone_map["bones"], to_mirrored=True)
    armature_obj["bone_mapper_active"] = bone_map["id"]
    armature_obj["bone_mapper_m2"]     = m2_filename.lower()

    print(
        f"[BoneMapperBridge] Auto-applied '{bone_map['label']}' "
        f"({count} bones renamed) for '{m2_filename}'"
    )
    return bone_map["label"]


def apply_bone_map_pre_export(armature_obj) -> dict | None:
    """
    Called just before exporting.  If the armature carries a
    ``bone_mapper_active`` stamp we reverse the rename so the exported
    .m2i contains the original indexed names that M2Mod expects.

    :returns: the bone-map dict that was reversed (caller must pass it to
              ``restore_bone_map_post_export`` afterwards), or ``None``.
    """
    active_id = armature_obj.get("bone_mapper_active")
    if not active_id:
        return None

    m2_filename = armature_obj.get("bone_mapper_m2", "")
    bone_map = find_bone_map_for_m2(m2_filename) if m2_filename else None

    # Fall back to scanning by id if m2 lookup fails
    if bone_map is None:
        bone_map = _find_bone_map_by_id(active_id)

    if bone_map is None:
        print(
            f"[BoneMapperBridge] Active bone map '{active_id}' not found — "
            "exporting with current bone names."
        )
        return None

    count = rename_bones(armature_obj, bone_map["bones"], to_mirrored=False)
    print(
        f"[BoneMapperBridge] Pre-export: reversed '{bone_map['label']}' "
        f"({count} bones renamed back to indexed)."
    )
    return bone_map


def restore_bone_map_post_export(armature_obj, bone_map: dict) -> None:
    """
    After the file has been written, re-apply the mirrored names so the
    Blender scene stays in its friendly mirrored-name state.
    """
    count = rename_bones(armature_obj, bone_map["bones"], to_mirrored=True)
    print(
        f"[BoneMapperBridge] Post-export: re-applied '{bone_map['label']}' "
        f"({count} bones renamed back to mirrored)."
    )


# ---------------------------------------------------------------------------
# Internal fallback: find a bone map by its dotted id string
# ---------------------------------------------------------------------------

def _find_bone_map_by_id(bone_map_id: str) -> dict | None:
    bone_maps_dir = _find_bone_mapper_dir()
    if bone_maps_dir is None or not bone_maps_dir.is_dir():
        return None

    for path in _iter_bone_map_files(bone_maps_dir):
        rel     = path.relative_to(bone_maps_dir.parent)
        mod_id  = str(rel.with_suffix("")).replace("/", ".").replace("\\", ".")
        if mod_id != bone_map_id:
            continue
        try:
            mod = _load_module_from_path(path, "bone_mapper_bridge_tmp." + path.stem)
            return {
                "id":      mod_id,
                "label":   getattr(mod, "LABEL",   path.stem.replace("_", " ").title()),
                "bones":   getattr(mod, "BONES",   []),
                "type":    getattr(mod, "TYPE",    None),
                "subtype": getattr(mod, "SUBTYPE", None),
                "mod":     mod,
            }
        except Exception as e:
            print(f"[BoneMapperBridge] Could not load '{path}': {e}")

    return None
