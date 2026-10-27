# Toolkit adapter interface (backup, cleanup, restore)

`quiettv-mcp` has **no backup, cleanup or restore transport of its own**. Those operations are reachable only
through an installed adapter from a validated toolkit. Until one is installed, `tv_capabilities` reports every
maintenance operation as unavailable, with the reason.

**Current status (2026-10-26).** `public-toolkit` validates fetched capsules (pinned hash, device binding) and
builds offline restore plans, but has **no live transport adapter**: its apply refuses and changes nothing, and
it registers no `quiettv_mcp.toolkit_adapters` entry point. Maintenance is therefore unavailable here even with
`--enable-maintenance`. When the toolkit ships an adapter, it should report `validated_transport: false` until
its live transport is proven on the supported profile; that allows dry-run planning only.

Coordination: each lane runs its own export scan. If the toolkit's tests carry deliberate fake LAN values or
planted secrets, approve those exact files by hash in `.github/public-fixture-approvals.txt` (see
`tools/public_source_guard.py --approve-fixtures`); do not widen the guard.

## Discovery

Exactly one Python entry point in the group `quiettv_mcp.toolkit_adapters`. It may be an object or a
zero-argument factory. Zero or several adapters means none is used.

```toml
# in the toolkit's pyproject.toml
[project.entry-points."quiettv_mcp.toolkit_adapters"]
public-toolkit = "quiet_toolkit.mcp_adapter:create_adapter"
```

## Interface (API version 1)

```python
class ToolkitAdapter(Protocol):
    api_version: int  # must be 1

    def describe(self) -> dict:
        # {"name": str, "version": str,
        #  "operations": {"backup"|"cleanup"|"restore":
        #      {"available": bool, "validated_transport": bool, "reason": str}}}
        # Read-only. "validated_transport" means the toolkit's own tests prove the
        # transport on the supported profile; false allows planning only.

    def plan(self, operation: str, capsule_ref: str | None) -> dict:
        # Read-only dry run: {"plan_id": str, "summary": str, "steps": [str], "mutates": bool}

    def verify_capsule(self, capsule_ref: str) -> dict:
        # Read-only: {"verified": bool, "sha256": str | None, "reason": str}

    def apply(self, operation: str, plan_id: str, capsule_ref: str | None) -> dict:
        # Mutation: {"applied": bool, "evidence": {...non-empty...}, "reason": str}
```

`capsule_ref` and `plan_id` are identifiers matching `[A-Za-z0-9._-]{1,128}`, never paths: the adapter resolves
them inside its own store, so the MCP tools cannot name arbitrary files.

## Gates the server enforces

1. Owner launch flag `--enable-maintenance` (otherwise the tools are not even registered).
2. Exactly one adapter, `api_version == 1`.
3. `describe()` says the operation is `available` (for planning) and has `validated_transport` (for applying).
4. `apply` needs a `plan_id` issued by `plan` **in the same server session** for the same operation and capsule;
   plan ids are single-use.
5. `restore` needs `verify_capsule` to return `verified: true` with a `sha256`.
6. The result is reported `applied` only if the adapter returns `applied: true` **and** non-empty `evidence`;
   otherwise `unverified`. The server never turns silence into success.

Adapter output is passed through the same redaction as diagnostics before it reaches the model.
`tests/test_maintenance.py` holds test doubles of this interface that exercise every gate; they are not shipped.
