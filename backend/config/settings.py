from pydantic_settings import BaseSettings
from functools import lru_cache
from typing import List


class Settings(BaseSettings):
    # Database
    db_host: str = "timescaledb"
    db_port: int = 5432
    db_user: str = "crypto"
    db_password: str
    db_name: str = "crypto_dashboard"

    # Redis
    redis_url: str = "redis://redis:6379"

    # API Keys
    binance_api_key: str = ""
    binance_api_secret: str = ""
    coinglass_api_key: str = ""
    coingecko_api_key: str = ""
    deribit_client_id: str = ""
    deribit_client_secret: str = ""
    cryptoquant_api_key: str = ""

    # App
    environment: str = "production"
    secret_key: str = "change_me"

    # Trading
    instruments: List[str] = ["BTCUSDT", "ETHUSDT"]
    timeframes: List[str] = ["1h", "4h", "1d"]

    # Confluence weights (must sum to 10)
    weight_structure: float = 2.0
    weight_order_flow: float = 2.0
    weight_funding: float = 1.5
    weight_options: float = 1.5
    weight_onchain: float = 1.0
    weight_macro: float = 1.0
    weight_liquidation: float = 1.0

    # Sizing thresholds
    score_full_size: float = 7.0
    score_half_size: float = 5.0

    # History seed
    seed_days_ohlcv: int = 365       # 1 year
    seed_days_funding: int = 365
    seed_days_oi: int = 180          # OI history patchier before 2023

    @property
    def db_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )

    @property
    def db_url_sync(self) -> str:
        return (
            f"postgresql://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


@lru_cache()
def get_settings() -> Settings:
    return Settings()
