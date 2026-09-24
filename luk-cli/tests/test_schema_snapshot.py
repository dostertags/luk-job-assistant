"""JSON-contract snapshot (spec §5.4): docs/schema/<kind>.json must equal what `luk schema` prints.

A failure means a model changed. That is a contract change: bump `models.SCHEMA_VERSION` (and the
`Literal` of `Envelope.schema_version`), then regenerate with
`python -m luk_cli schema --out luk-cli/docs/schema` and review the diff.

Regenerating alone is not enough (§5.4 "fails if a model changes without a schema_version bump"): each
version pins the SHA-256 of its contract shape (the schemas without their `title`/`description`
annotations), so a new shape needs a new SCHEMA_VERSION and its own SCHEMA_DIGESTS entry. Only a
wording change of a docstring or title regenerates without a bump.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from luk_cli import api
from luk_cli.models import KIND_MODELS, SCHEMA_VERSION

SNAPSHOT = Path(__file__).resolve().parents[1] / "docs" / "schema"
REGENERATE = "the JSON contract changed: bump SCHEMA_VERSION, then run `luk schema --out docs/schema`"
# schema_version → SHA-256 of contract_shape(api.schemas()); never edit an entry, add the next version's.
SCHEMA_DIGESTS = {
    1: "09481b029de80dd717ae94738c949b5c1b2fa8ee2eb2e86d4f92db86cb29c0af",
    2: "b538125314a529d74cdb750a513aef8e5f6ad10ad3cc4d9901b0e8273810c844",  # ListMeta: offset, next_offset
}
_ANNOTATIONS = frozenset({"title", "description", "examples"})


def contract_shape(node: Any) -> Any:
    """The schemas minus human annotations; property and $defs names are data, never dropped."""
    if isinstance(node, dict):
        return {key: ({name: contract_shape(sub) for name, sub in value.items()} if key in ("properties", "$defs")
                      else contract_shape(value))
                for key, value in node.items() if key not in _ANNOTATIONS}
    if isinstance(node, list):
        return [contract_shape(item) for item in node]
    return node


def digest(schemas: dict[str, Any]) -> str:
    canonical = json.dumps(contract_shape(schemas), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def test_one_snapshot_per_kind():
    assert sorted(p.stem for p in SNAPSHOT.glob("*.json")) == sorted([*KIND_MODELS, "error"])


@pytest.mark.parametrize("kind", [*KIND_MODELS, "error"])
def test_snapshot_matches_the_models(kind):
    snapshot = json.loads((SNAPSHOT / f"{kind}.json").read_text("utf-8"))
    assert snapshot == api.schemas()[kind], f"{kind}: {REGENERATE}"
    assert snapshot["properties"]["schema_version"]["const"] == SCHEMA_VERSION


def test_the_contract_shape_is_pinned_to_its_schema_version():
    """A changed model with regenerated snapshots still fails here until SCHEMA_VERSION is bumped."""
    current = digest(api.schemas())
    assert SCHEMA_DIGESTS.get(SCHEMA_VERSION) == current, (
        f"the contract of schema_version {SCHEMA_VERSION} changed: bump SCHEMA_VERSION to {SCHEMA_VERSION + 1}, "
        f"add SCHEMA_DIGESTS[{SCHEMA_VERSION + 1}] = {current!r} and run `luk schema --out docs/schema`"
    )


def test_the_digest_ignores_wording_but_not_shape():
    schemas = api.schemas()
    reworded = json.loads(json.dumps(schemas).replace("Provisional until the first capture", "Provisional"))
    assert reworded != schemas and digest(reworded) == digest(schemas)
    grown = json.loads(json.dumps(schemas))
    grown["company"]["$defs"]["Company"]["properties"]["employee_count_band"] = {"type": "string"}
    assert digest(grown) != digest(schemas)
    retyped = json.loads(json.dumps(schemas))
    retyped["job"]["properties"]["data"]["$ref"] = "#/$defs/Other"
    assert digest(retyped) != digest(schemas)
