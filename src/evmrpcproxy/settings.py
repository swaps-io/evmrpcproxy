import hashlib
import os
from pathlib import Path
from typing import Literal

import pydantic
import pydantic_settings
from hyapp.pydsettings import (
    YAMLedDotEnvSettingsSource,
    YAMLedEnvSettingsSource,
    YAMLedSecretsSettingsSource,
)

from .evmrpc.evmrpc_config_model import EVMRPCConfig, EVMRPCSecrets

TEnvName = Literal["dev", "tests", "devrun", "staging", "prod"]
CONFIG_ROOT = Path.home() / ".config/evmrpcproxy"
ENV_FILE_PATH = CONFIG_ROOT / "env"
SECRETS_DIR_PATH_ENV = os.environ.get("ERPX_SECRETS_DIR")
SECRETS_DIR_PATH = CONFIG_ROOT / "secrets" if not SECRETS_DIR_PATH_ENV else Path(SECRETS_DIR_PATH_ENV)


class SettingsOptsBase(pydantic_settings.BaseSettings):
    """Overridable key-value settings, base version that only reads init arguments"""

    model_config = pydantic_settings.SettingsConfigDict(
        env_prefix="ERP_",
        env_file=ENV_FILE_PATH,
        secrets_dir=SECRETS_DIR_PATH,
        frozen=True,
    )

    def __repr__(self) -> str:
        hash_str = hashlib.sha256(self.model_dump_json().encode()).hexdigest()[:16]
        return f"{self.__class__.__name__}(env={self.env}, hash={hash_str}, ...)"

    def __str__(self) -> str:
        return self.__repr__()

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[pydantic_settings.BaseSettings],
        init_settings: pydantic_settings.PydanticBaseSettingsSource,
        env_settings: pydantic_settings.PydanticBaseSettingsSource,
        dotenv_settings: pydantic_settings.PydanticBaseSettingsSource,
        file_secret_settings: pydantic_settings.PydanticBaseSettingsSource,
    ) -> tuple[pydantic_settings.PydanticBaseSettingsSource, ...]:
        return (init_settings,)

    env: TEnvName = "dev"  # `ERP_ENV`

    api_bind: str = "0.0.0.0"
    api_port: int = 13431
    api_deploy_workers: int = 4
    api_dev_reload: bool = True

    sentry_dsn: str = ""

    # config override is primarily for the tests.
    evmrpc_config: EVMRPCConfig | None = None  # `ERP_EVMRPC_CONFIG`
    evmrpc_secrets: EVMRPCSecrets | None = None  # `ERP_EVMRPC_SECRETS`
    evmrpc_fallback_to_public: bool = True
    # token -> title
    evmrpc_auth_tokens: dict[str, str] = {"xlocalonlyauthtoken": "xlocalonly"}
    evmrpc_do_upstream_debug: bool = False


class SettingsOptsEnv(SettingsOptsBase):
    """Overridable settings class that also loads values from `os.environ`"""

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[pydantic_settings.BaseSettings],
        init_settings: pydantic_settings.PydanticBaseSettingsSource,
        env_settings: pydantic_settings.PydanticBaseSettingsSource,
        dotenv_settings: pydantic_settings.PydanticBaseSettingsSource,
        file_secret_settings: pydantic_settings.PydanticBaseSettingsSource,
    ) -> tuple[pydantic_settings.PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            YAMLedDotEnvSettingsSource(settings_cls),
            YAMLedEnvSettingsSource(settings_cls),
            YAMLedSecretsSettingsSource(settings_cls),
        )


class Settings(pydantic.BaseModel):
    opts: SettingsOptsBase = pydantic.Field(default_factory=SettingsOptsEnv)
