from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    app_env: str = "local"
    aws_default_region: str = "eu-west-2"
    unblock_table: str = ""
    unblock_bucket: str = ""
    unblock_queue_url: str = ""
    unblock_user_pool_id: str = ""
    unblock_client_id: str = ""
    unblock_auth_domain: str = ""
    unblock_model_id: str = "amazon.nova-pro-v1:0"
    # Outbound mail stays disabled until a deployment sets both the switch and an allowlist.
    unblock_send_enabled: bool = False
    unblock_mail_from: str = ""
    unblock_inbound_domain: str = ""
    unblock_allowed_recipients: str = ""
    # Credentials for the shared read-only demo account. Empty disables the guest button.
    unblock_demo_username: str = ""
    unblock_demo_password: str = ""

    @property
    def allowed_recipients(self) -> list[str]:
        return [
            p.strip().casefold() for p in self.unblock_allowed_recipients.split(",") if p.strip()
        ]


@lru_cache
def settings() -> Settings:
    return Settings()
