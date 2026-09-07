from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def optional_secret(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    admin_token: str = "changeme"
    model_root: str = "/models"
    data_dir: str = "/data"
    hf_endpoint: str = "https://hf-mirror.com"
    hf_token: str | None = None
    modelscope_api_token: str | None = None
    aria2_rpc_url: str = "http://aria2:6800/jsonrpc"
    aria2_rpc_secret: str = "dlmodel"
    download_concurrency: int = 2
    aria2_connections: int = 16
    download_retries: int = 3
    ollama_base_url: str = "http://ollama:11434"
    vllm_base_url: str = "http://vllm:8000"
    vllm_model: str | None = None
    notify_dingtalk_webhook: str | None = None
    notify_feishu_webhook: str | None = None
    notify_wecom_webhook: str | None = None
    notify_on_completed: bool = True
    notify_on_failed: bool = True
    notify_on_started: bool = False
    notify_on_cancelled: bool = False
    host: str = "0.0.0.0"
    port: int = 8080

    @field_validator(
        "hf_token",
        "modelscope_api_token",
        "vllm_model",
        "notify_dingtalk_webhook",
        "notify_feishu_webhook",
        "notify_wecom_webhook",
        mode="before",
    )
    @classmethod
    def _blank_optional_to_none(cls, value):
        if value is None or isinstance(value, str):
            return optional_secret(value)
        return value


def get_settings() -> Settings:
    return Settings()
