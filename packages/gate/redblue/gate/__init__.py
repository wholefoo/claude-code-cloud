"""RedBlue security gate: Red/Blue agents, scanners and the release gate.

The gate runs standalone (GitHub Action, CI) and depends only on pydantic, pyyaml and httpx.
It never imports ``redblue.core``.

Hard rule: the Red agent only scans previews it launched itself. There is no target-URL
input anywhere in this package (config, CLI or API).
"""

__version__ = "0.1.0"
