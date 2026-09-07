from pydantic_settings import BaseSettings, SettingsConfigDict


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
    host: str = "0.0.0.0"
    port: int = 8080


def get_settings() -> Settings:
    return Settings()
