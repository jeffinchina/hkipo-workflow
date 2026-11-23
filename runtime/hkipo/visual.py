"""Caller-controlled visual approvals, separate from model-generated bundles."""
import hashlib
import json
from pathlib import Path
from .evidence import load_visual_reviews


def trusted_visuals(registry_path, manifest):
    if registry_path is None:
        return [], [], {}
    path = Path(registry_path).resolve(strict=True)
    registry = json.loads(path.read_text())
    if registry.get("version") != 1 or not isinstance(registry.get("approved_reviews"), list):
        raise ValueError("Invalid independent visual approval registry")
    paths, hashes = [], []
    for row in registry["approved_reviews"]:
        sidecar = Path(row["path"]).resolve(strict=True)
        digest = hashlib.sha256(sidecar.read_bytes()).hexdigest()
        if digest != row["sha256"]:
            raise ValueError("Independent visual approval hash mismatch")
        paths.append(str(sidecar)); hashes.append(digest)
    docs = manifest["documents"] if isinstance(manifest, dict) else manifest
    accepted = load_visual_reviews(paths, hashes, docs)
    return paths, hashes, accepted
