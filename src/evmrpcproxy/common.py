import logging
from typing import Any

from .blockchains import CHAIN_BY_NAME
from .evmrpc.evmrpc_check import SimpleChainInfo
from .evmrpc.evmrpc_client import EVMRPCClient
from .evmrpc.evmrpc_config import EVMRPC_CONFIG, EVMRPC_CONFIG_PUBLIC
from .evmrpc.evmrpc_config_model import EVMRPCConfig, EVMRPCSecrets
from .settings import Settings

LOGGER = logging.getLogger(__name__)

SIMPLE_CHAIN_INFOS = {
    name: SimpleChainInfo(
        id=chain_config["id"],
        shortname=name,
        non_evm=chain_config.get("non_evm", False),
        multicall3_address=chain_config.get("addresses", {}).get("Multicall3"),
    )
    for name, chain_config in CHAIN_BY_NAME.items()
}


# Hack to detect empty (missing) secrets without logic duplication
_PLACEHOLDER = "__ERP_SECRET_PLACEHOLDER__"


def combine_config_with_public(
    config: EVMRPCConfig, secrets: EVMRPCSecrets, public_config: EVMRPCConfig = EVMRPC_CONFIG_PUBLIC
) -> EVMRPCConfig:
    secrets_with_placeholder = EVMRPCSecrets.model_validate(
        {key: val or _PLACEHOLDER for key, val in secrets.model_dump().items()}
    )
    chains = {
        chain_name: {
            node_name: node_config
            for node_name, node_config in chain_config.items()
            if _PLACEHOLDER not in node_config.get_url(secrets_with_placeholder)
        }
        # If no private nodes are available, fall back to public nodes.
        or public_config.chains.get(chain_name)
        or {}
        for chain_name, chain_config in config.chains.items()
    }
    return config.replace(chains=chains)


def make_evmrpc_cli(settings: Settings, **kwargs: Any) -> EVMRPCClient:
    config = settings.opts.evmrpc_config or EVMRPC_CONFIG
    secrets = settings.opts.evmrpc_secrets
    if not secrets:
        LOGGER.error("No EVMRPC secrets configured")
        secrets = EVMRPCSecrets()

    if settings.opts.evmrpc_fallback_to_public:
        config = combine_config_with_public(config, secrets)

    return EVMRPCClient(
        evmrpc_config=config, evmrpc_secrets=secrets, do_upstream_debug=settings.opts.evmrpc_do_upstream_debug, **kwargs
    )


def dump_rendered_config() -> dict:
    settings = Settings()
    evmrpc_cli = make_evmrpc_cli(settings)
    return {
        f"{node.chain_name}__{node.node_name}": node.get_url(evmrpc_cli.evmrpc_secrets)
        for nodes in evmrpc_cli.evmrpc_config.chains.values()
        for node in nodes.values()
    }
