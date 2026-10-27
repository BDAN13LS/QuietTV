# Third-party notices

`quiettv-mcp` vendors no third-party code. At install time pip fetches its dependencies under their own
licences, chiefly:

| Package | Use | Licence |
|---|---|---|
| `mcp` (Model Context Protocol Python SDK, 2.3.x), with `mcp-types` | MCP server and client | MIT |
| `pydantic`, `anyio` and other transitive dependencies of `mcp` | pulled in by `mcp` | their own (MIT/BSD-style) |
| `pytest` (tests only) | test runner | MIT |

Run `pip-licenses` (or `pip show <name>`) in the project venv for the exact installed versions and texts.
Samsung, Tizen and app names are trademarks of their owners; app IDs appear only as identifiers.
