"""The registry's own check: every published manifest parses, and says what the index says.

A rule without a check is a suggestion. The index is the thing a deployment
reads first, so an entry pointing at a manifest that does not exist — or at one
declaring a different plugin — is the failure most likely to reach a customer,
and the cheapest to catch here.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SLUG = set("abcdefghijklmnopqrstuvwxyz0123456789-_")
#: Keys a manifest must never carry. A credential belongs in the deployment's
#: secret store, and which model an agent runs on is its administrator's call.
FORBIDDEN = {"model", "model_name", "model_config_id", "llm", "provider",
             "api_key", "token", "secret", "password", "credential"}


def fail(problems: list[str]) -> None:
    """Report every problem and stop. Returns only when there were none, so a
    pass is stated rather than inferred from silence."""
    if not problems:
        return
    for problem in problems:
        print(f"FAIL {problem}")
    sys.exit(1)


def slug(value: object) -> bool:
    return isinstance(value, str) and bool(value) and set(value) <= SLUG


def walk_for_forbidden(node: object, where: str, problems: list[str]) -> None:
    """Any forbidden key, at any depth. A key nested inside an agent is still a key."""
    if isinstance(node, dict):
        for key, value in node.items():
            if str(key).lower() in FORBIDDEN:
                problems.append(f"{where}: carries '{key}', which a manifest may never declare")
            walk_for_forbidden(value, f"{where}.{key}", problems)
    elif isinstance(node, list):
        for i, value in enumerate(node):
            walk_for_forbidden(value, f"{where}[{i}]", problems)


def main() -> None:
    problems: list[str] = []
    index_path = ROOT / "index.yaml"
    if not index_path.exists():
        fail(["index.yaml is missing"])

    index = yaml.safe_load(index_path.read_text())
    if not isinstance(index, dict):
        fail(["index.yaml is not a mapping"])
    if index.get("registry_version") != 1:
        problems.append(f"index.yaml: registry_version {index.get('registry_version')!r} is not 1")

    connector_ids = {c.get("connector_id") for c in index.get("connectors") or []}
    for entry in index.get("plugins") or []:
        plugin_id = entry.get("plugin_id")
        where = f"index.yaml plugins[{plugin_id!r}]"
        if not slug(plugin_id):
            problems.append(f"{where}: plugin_id is not a slug")
        rel = entry.get("manifest")
        if not rel:
            problems.append(f"{where}: no manifest path")
            continue
        path = ROOT / rel
        if not path.is_file():
            problems.append(f"{where}: manifest {rel} does not exist")
            continue

        manifest = yaml.safe_load(path.read_text())
        if not isinstance(manifest, dict):
            problems.append(f"{rel}: not a mapping")
            continue
        if manifest.get("plugin_id") != plugin_id:
            problems.append(
                f"{rel}: declares plugin_id {manifest.get('plugin_id')!r}, "
                f"but the index lists it as {plugin_id!r}"
            )
        if manifest.get("manifest_version") != 1:
            problems.append(f"{rel}: manifest_version is not 1")
        walk_for_forbidden(manifest, rel, problems)

        declared = {c.get("connector_id") for c in manifest.get("connectors") or []}
        for cid in sorted(declared - connector_ids):
            problems.append(f"{rel}: names connector {cid!r}, which the index does not publish")
        required = set((entry.get("requires") or {}).get("connectors") or [])
        if required != declared:
            problems.append(
                f"{where}: requires {sorted(required)} but the manifest declares {sorted(declared)}"
            )
        for skill in manifest.get("skills") or []:
            if not slug(skill.get("name")):
                problems.append(f"{rel}: skill name {skill.get('name')!r} is not a slug")
            if not (skill.get("description") or "").strip():
                problems.append(f"{rel}: skill {skill.get('name')!r} has no description")
            for tool in skill.get("tools") or []:
                if str(tool).startswith("skill:"):
                    problems.append(
                        f"{rel}: skill {skill.get('name')!r} names a skill ({tool}); "
                        "a skill may not call another"
                    )

    fail(problems)
    print("registry ok")


if __name__ == "__main__":
    main()
