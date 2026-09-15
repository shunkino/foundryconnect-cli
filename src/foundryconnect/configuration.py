import copy
import hashlib
import io
import json
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass
from pathlib import Path

from .models import FoundryError, Plan
from .storage import atomic_write, locked, reject_symlink


def parse(text: str, format: str) -> MutableMapping:
    try:
        if format == "toml":
            import tomlkit
            from tomlkit.exceptions import ParseError
            try:
                result = tomlkit.parse(text)
            except ParseError as exc:
                raise FoundryError("Invalid TOML configuration; no changes made.") from exc
        elif format == "yaml":
            from ruamel.yaml import YAML
            from ruamel.yaml.error import YAMLError
            try:
                result = YAML(typ="rt").load(text)
            except YAMLError as exc:
                raise FoundryError("Invalid YAML configuration; no changes made.") from exc
            if result is None:
                result = {}
            def reject_anchors(node):
                anchor = getattr(node, "anchor", None)
                if (anchor is not None and anchor.value) or getattr(node, "merge", None):
                    raise FoundryError("YAML anchors, aliases and merges are not edited by this MVP; "
                                       "expand them explicitly before configuring Hermes.")
                if isinstance(node, Mapping):
                    for value in node.values():
                        reject_anchors(value)
                elif isinstance(node, list):
                    for value in node:
                        reject_anchors(value)
            reject_anchors(result)
        elif format == "json":
            def unique(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise FoundryError("Duplicate JSON configuration keys; no changes made.")
                    result[key] = value
                return result
            result = json.loads(text, object_pairs_hook=unique) if text.strip() else {}
        else:
            raise FoundryError("Unknown configuration format.")
    except ImportError as exc:
        raise FoundryError("Missing configuration editor dependency; reinstall foundryconnect-cli.", "dependency") from exc
    except json.JSONDecodeError as exc:
        raise FoundryError("Invalid JSON configuration (JSONC is not edited by this MVP).") from exc
    if not isinstance(result, MutableMapping):
        raise FoundryError("Agent configuration must be a mapping/object.")
    return result


def render(document: Mapping, format: str) -> str:
    if format == "toml":
        import tomlkit
        return tomlkit.dumps(document)
    if format == "yaml":
        from ruamel.yaml import YAML
        output = io.StringIO()
        YAML(typ="rt").dump(document, output)
        return output.getvalue()
    return json.dumps(document, indent=2, ensure_ascii=True) + "\n"


def read(path: Path, format: str) -> tuple[str | None, MutableMapping]:
    reject_symlink(path)
    text = None
    if path.exists():
        with path.open("r", encoding="utf-8", newline="") as stream:
            text = stream.read()
    return text, parse(text or "", format)


def lookup(document: Mapping, path: tuple[str, ...]) -> dict:
    node = document
    for index, key in enumerate(path):
        if not isinstance(node, Mapping):
            raise FoundryError(f"Cannot edit {'.'.join(path)}: {'.'.join(path[:index])} is not an object.")
        if key not in node:
            return {"present": False}
        node = node[key]
    # Serialize round-trip editor scalar/container wrappers into receipt-safe JSON.
    try:
        return {"present": True, "value": json.loads(json.dumps(node))}
    except (TypeError, ValueError) as exc:
        raise FoundryError(f"Unsupported value at {'.'.join(path)}; no settings changed.") from exc


def assign(document: MutableMapping, path: tuple[str, ...], state: dict) -> None:
    node = document
    for key in path[:-1]:
        if key not in node:
            if not state["present"]:
                return
            node[key] = {}
        node = node[key]
        if not isinstance(node, MutableMapping):
            raise FoundryError(f"Cannot update {'.'.join(path)}: parent was changed.")
    if state["present"]:
        node[path[-1]] = state["value"]
    else:
        node.pop(path[-1], None)


def digest(text: str | None) -> str | None:
    return hashlib.sha256(text.encode()).hexdigest() if text is not None else None


@dataclass
class Prepared:
    plan: Plan
    before: str | None
    after: str
    changes: list[dict]
    created_parents: list[list[str]]


def prepare(plan: Plan) -> Prepared:
    before, document = read(plan.path, plan.format)
    changes = []
    parents = set()
    for path, value in plan.changes.items():
        old = lookup(document, path)
        for size in range(1, len(path)):
            if not lookup(document, path[:size])["present"]:
                parents.add(path[:size])
        changes.append({"path": list(path), "before": old, "after": {"present": True, "value": value}})
    edited = copy.deepcopy(document)
    for change in changes:
        assign(edited, tuple(change["path"]), change["after"])
    after = render(edited, plan.format)
    # Refuse a serialization that cannot reproduce the intended data.
    parsed = parse(after, plan.format)
    for change in changes:
        if lookup(parsed, tuple(change["path"])) != change["after"]:
            raise FoundryError("Configuration serialization did not preserve the requested settings.")
    return Prepared(plan, before, after, changes, [list(path) for path in sorted(parents)])


def preview(prepared: Prepared) -> str:
    lines = [f"Configuration: {prepared.plan.path}", f"Authentication: {prepared.plan.strategy}"]
    for change in prepared.changes:
        if change["before"] == change["after"]:
            continue
        key = ".".join(change["path"])
        if change["before"]["present"]:
            lines.append(f"- {key} = <existing value withheld>")
        lines.append(f"+ {key} = {json.dumps(change['after']['value'], ensure_ascii=True)}")
    return "\n".join(lines)


def receipt_path(root: Path, agent: str) -> Path:
    return root / "receipts" / f"{agent}.json"


def receipt(root: Path, agent: str) -> dict | None:
    path = receipt_path(root, agent)
    reject_symlink(path)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise FoundryError("Invalid ownership receipt; configuration will not be overwritten.") from exc
    if (not isinstance(data, dict) or data.get("version") != 1
            or data.get("agent") != agent
            or not isinstance(data.get("path"), str) or not Path(data["path"]).is_absolute()
            or data.get("format") not in ("toml", "yaml", "json")
            or not isinstance(data.get("changes"), list)
            or not isinstance(data.get("created_parents"), list)
            or not isinstance(data.get("profile"), str)
            or not isinstance(data.get("after_hash"), str)
            or not (data.get("before") is None or isinstance(data.get("before"), str))):
        raise FoundryError("Unsupported or malformed ownership receipt.")
    for change in data["changes"]:
        if not isinstance(change, dict) or not isinstance(change.get("path"), list) or not change["path"]:
            raise FoundryError("Malformed ownership receipt.")
        if not all(isinstance(key, str) for key in change["path"]):
            raise FoundryError("Malformed ownership receipt.")
        for side in ("before", "after"):
            state = change.get(side)
            if (not isinstance(state, dict) or not isinstance(state.get("present"), bool)
                    or (state["present"] and "value" not in state)):
                raise FoundryError("Malformed ownership receipt.")
    for parent in data["created_parents"]:
        if not isinstance(parent, list) or not parent or not all(isinstance(key, str) for key in parent):
            raise FoundryError("Malformed ownership receipt.")
    return data


def drift(data: dict) -> list[str]:
    _, document = read(Path(data["path"]), data["format"])
    return [".".join(change["path"]) for change in data["changes"]
            if lookup(document, tuple(change["path"])) != change["after"]]


def install(root: Path, prepared: Prepared, profile: str) -> bool:
    plan = prepared.plan
    with locked(root):
        existing = receipt(root, plan.agent)
        if existing:
            # Compare installed values, not old values captured by the fresh preview.
            if (existing["profile"] == profile and existing["path"] == str(plan.path)
                    and {tuple(c["path"]): c["after"] for c in existing["changes"]}
                    == {tuple(c["path"]): c["after"] for c in prepared.changes}
                    and not drift(existing)):
                return False
            raise FoundryError(f"{plan.agent} already has an ownership receipt. Run "
                               f"foundryconnect {plan.agent} off before changing profiles or settings.")
        current, _ = read(plan.path, plan.format)
        if current != prepared.before:
            raise FoundryError("Configuration changed after the preview; retry to review the new changes.")
        data = {"version": 1, "agent": plan.agent, "profile": profile, "path": str(plan.path),
                "format": plan.format, "before": prepared.before, "after_hash": digest(prepared.after),
                "changes": prepared.changes, "created_parents": prepared.created_parents}
        # Journal first: off can recover either side of an interrupted atomic config replacement.
        atomic_write(receipt_path(root, plan.agent), json.dumps(data, indent=2) + "\n")
        atomic_write(plan.path, prepared.after)
    return True


def removal(data: dict) -> tuple[str | None, str | None]:
    path = Path(data["path"])
    current, document = read(path, data["format"])
    if digest(current) == data["after_hash"] or current == data["before"]:
        return current, data["before"]
    conflicts = drift(data)
    if conflicts:
        raise FoundryError("Owned settings changed since installation: " + ", ".join(conflicts)
                           + ". Restore these settings before off; nothing was changed.")
    for change in reversed(data["changes"]):
        assign(document, tuple(change["path"]), change["before"])
    for path_parts in sorted(data["created_parents"], key=len, reverse=True):
        state = lookup(document, tuple(path_parts))
        if state["present"] and state["value"] == {}:
            assign(document, tuple(path_parts), {"present": False})
    return current, render(document, data["format"])


def uninstall(root: Path, agent: str, dry_run: bool = False) -> bool:
    if dry_run:
        data = receipt(root, agent)
        if data:
            removal(data)
        return data is not None
    with locked(root):
        data = receipt(root, agent)
        if data is None:
            return False
        before, after = removal(data)
        path = Path(data["path"])
        current, _ = read(path, data["format"])
        if current != before:
            raise FoundryError("Configuration changed during off; retry.")
        if after is None:
            path.unlink(missing_ok=True)
        else:
            atomic_write(path, after)
        receipt_path(root, agent).unlink()
    return True
