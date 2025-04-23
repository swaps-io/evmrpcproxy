from pathlib import Path
from typing import Any

import orjson

HERE = Path(__file__).parent
CHAINS: list[dict[str, Any]] = list(orjson.loads((HERE / "blockchains.json").read_text()).values())
CHAIN_BY_ID = {int(chain_info["id"]): chain_info for chain_info in CHAINS}
CHAIN_BY_NAME = {chain_info["shortname"].lower(): chain_info for chain_info in CHAINS}
