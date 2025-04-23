import urllib.parse
from pathlib import Path

import yaml

from ..blockchains import CHAIN_BY_NAME
from .evmrpc_config_model import EVMRPCConfig, EVMRPCSecrets

HERE = Path(__file__).parent
# To consider: load lazily, validate in tests.
EVMRPC_CONFIG_RAW = yaml.safe_load((HERE / "evmrpc_config.yaml").read_text())
EVMRPC_CONFIG = EVMRPCConfig.model_validate(EVMRPC_CONFIG_RAW)
EVMRPC_CONFIG.validate_templates(EVMRPCSecrets())

EVMRPC_CONFIG_PUBLIC_RAW = {
    chain_name: {
        "x_chain_id": chain_info["id"],
        **{
            urllib.parse.urlparse(rpc_url).hostname: {"url": rpc_url, "max_blocks_distance": 100}
            for rpc_url in [chain_info["rpc_url"], *chain_info.get("rpc_extra_urls", [])]
        },
    }
    for chain_name, chain_info in CHAIN_BY_NAME.items()
}
EVMRPC_CONFIG_PUBLIC = EVMRPCConfig.model_validate(EVMRPC_CONFIG_PUBLIC_RAW)
