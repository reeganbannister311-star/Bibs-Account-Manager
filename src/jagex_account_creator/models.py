from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from functools import lru_cache

import pyotp
import wreq
from pydantic import BaseModel, ConfigDict, computed_field


class Proxy(BaseModel):
    model_config = ConfigDict(frozen=True)

    ip: str
    port: int
    username: str | None = None
    password: str | None = None

    def to_url(self) -> str:
        """Convert this proxy object to a url."""
        if self.username and self.password:
            proxy_url = f"http://{self.username}:{self.password}@{self.ip}:{self.port}"
        else:
            proxy_url = f"http://{self.ip}:{self.port}"
        return proxy_url

    def to_wreq(self) -> wreq.Proxy:
        """Convert this Proxy object to an wreq proxy object."""
        return wreq.Proxy.all(self.to_url())


class IMAPDetails(BaseModel):
    model_config = ConfigDict(frozen=True)

    ip: str
    port: int
    email: str
    password: str


class GmailWebDetails(BaseModel):
    model_config = ConfigDict(frozen=True)

    email: str
    password: str = ""


class Email(BaseModel):
    username: str
    domain: str

    @computed_field
    @property
    def Email(self) -> str:
        return f"{self.username}@{self.domain}"

    @property
    def address(self) -> str:
        return f"{self.username}@{self.domain}"


class Birthday(BaseModel):
    model_config = ConfigDict(frozen=True)

    day: int
    month: int
    year: int


class TwoFactorAuth(BaseModel):
    model_config = ConfigDict(frozen=True)

    setup_key: str
    backup_codes: list[str]

    def get_totp_code(self) -> str:
        return pyotp.TOTP(self.setup_key).now()


class JagexAccount(BaseModel):
    email: Email
    username: str
    password: str
    birthday: Birthday
    real_ip: str
    proxy: Proxy | None = None
    tfa: TwoFactorAuth | None = None


@dataclass(slots=True)
class TransferStats:
    bytes_sent: int = 0
    bytes_received: int = 0

    def __iadd__(self, other: "TransferStats") -> "TransferStats":
        self.bytes_sent += other.bytes_sent
        self.bytes_received += other.bytes_received
        return self


@dataclass(frozen=True)
class AccountRegistrationResult:
    jagex_account: JagexAccount
    transfer_stats: TransferStats
    duration: timedelta


class MailProvider(StrEnum):
    IMAP = "imap"
    GUERRILLA_MAIL = "guerrilla_mail"
    XITROO = "xitroo"
    GMAIL_WEB = "gmail_web"
