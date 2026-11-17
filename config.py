"""Muse 注册全局默认配置。"""
from dataclasses import dataclass
from typing import Optional


@dataclass
class Config:
    proxy: Optional[str] = None
    impersonate: str = "chrome142"
    lang: str = "ja"
    otp_timeout: int = 45
    send_caa: bool = True
    send_falco: bool = True
    resolve_ip: bool = True
