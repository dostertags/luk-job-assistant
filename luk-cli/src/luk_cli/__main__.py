"""`python -m luk_cli` → `luk_cli.cli.main` (the MCP config from `luk doctor --print-mcp-json` uses this)."""

import sys

from luk_cli.cli import main

sys.exit(main())
