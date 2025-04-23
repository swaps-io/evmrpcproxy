from typing import Any, Self

import pydantic
import yaml


class EVMRPCSecrets(pydantic.BaseModel, frozen=True):
    onerpc_token: str = ""
    alchemy_token: str = ""
    ankr_token: str = ""
    ankr_token_02: str = ""
    blastapi_token: str = ""
    # blockpi
    blockpi_arbitrum: str = ""
    blockpi_avalanche: str = ""
    blockpi_base: str = ""
    blockpi_berachain: str = ""
    blockpi_blast: str = ""
    blockpi_bsc: str = ""
    blockpi_gnosis: str = ""
    blockpi_linea: str = ""
    blockpi_mainnet: str = ""
    blockpi_mantle: str = ""
    blockpi_merlin: str = ""
    blockpi_optimism: str = ""
    blockpi_polygon: str = ""
    blockpi_polygonzkevm: str = ""
    blockpi_scroll: str = ""
    blockpi_sonic: str = ""
    # ...
    blockvision_token: str = ""
    drpc_token: str = ""
    drpc_token_02: str = ""
    infura_token: str = ""
    # quiknode
    quiknode_arbitrum: str = ""
    quiknode_avalanche: str = ""
    quiknode_base: str = ""
    quiknode_berachain: str = ""
    quiknode_bitcoin: str = ""
    quiknode_blast: str = ""
    quiknode_bsc: str = ""
    quiknode_fantom: str = ""
    quiknode_gnosis: str = ""
    quiknode_linea: str = ""
    quiknode_mainnet: str = ""
    quiknode_mantle: str = ""
    quiknode_mode: str = ""
    quiknode_optimism: str = ""
    quiknode_polygon: str = ""
    quiknode_polygonzkevm: str = ""
    quiknode_scroll: str = ""
    quiknode_solana: str = ""
    quiknode_zksync: str = ""


class EVMRPCNodeConfig(pydantic.BaseModel, frozen=True):
    chain_name: str
    node_name: str
    url: str

    # Limited to 10k on quicknode:
    # https://support.quicknode.com/hc/en-us/articles/10258449939473-Understanding-the-10-000-Block-Range-Limit-for-querying-Logs-and-Events
    # Limited to 10k **results** on infura:
    # https://docs.infura.io/api/networks/ethereum/json-rpc-methods/eth_getlogs#constraints
    # Limited to 1.5k on bouncebit.
    # Many other RPC providers have no limit (but it might be a bad idea to query too much at once).
    max_blocks_distance: int | None = 3000
    headers: tuple[tuple[str, str], ...] = ()

    supports_batch: bool = True

    # See: https://www.quicknode.com/docs/ethereum/bb_getAddress
    supports_blockbook: bool = False

    @classmethod
    def load_from_config(cls, chain_name: str, node_name: str, obj: Any, **kwargs: Any):
        obj_norm = {"url": obj} if isinstance(obj, str) else obj
        return super().model_validate({**obj_norm, "chain_name": chain_name, "node_name": node_name}, **kwargs)

    def get_url(self, secrets: EVMRPCSecrets) -> str:
        return self.url.format(**secrets.model_dump())


# chain_name -> node_name -> config
TEVMRPCChains = dict[str, dict[str, EVMRPCNodeConfig]]


class EVMRPCConfig(pydantic.BaseModel, frozen=True):
    chains: TEVMRPCChains

    @staticmethod
    def _is_extra_key(key: str) -> bool:
        return key.startswith("x_")

    @classmethod
    def prepare_chains_config(cls, value: Any) -> TEVMRPCChains:
        if not value:
            return {}

        raw_data = yaml.safe_load(value) if isinstance(value, str) else value
        return {
            chain_name: {
                node_name: EVMRPCNodeConfig.load_from_config(chain_name, node_name, node_config)
                for node_name, node_config in chain_config.items()
                if not cls._is_extra_key(node_name)
            }
            for chain_name, chain_config in raw_data.items()
            if not cls._is_extra_key(chain_name)
        }

    def __init__(self, **data):
        if "chains" not in data:
            chains_config = self.prepare_chains_config(data)
            data = {"chains": chains_config}
        super().__init__(**data)

    @classmethod
    def model_validate(cls, obj: Any, **kwargs: Any):
        chains_config = cls.prepare_chains_config(obj)
        return super().model_validate({"chains": chains_config})

    def validate_templates(self, secrets: EVMRPCSecrets) -> None:
        errors: list[tuple[str, str, Exception]] = []
        for chain_name, chain_cfg in self.chains.items():
            for node_name, node_config in chain_cfg.items():
                try:
                    node_config.get_url(secrets)
                except Exception as exc:
                    errors.append((chain_name, node_name, exc))
        if errors:
            raise Exception("EVMRPC Config template errors", errors)

    def replace(self, **kwargs: Any) -> Self:
        return self.model_copy(update=kwargs)
