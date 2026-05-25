"""Jupiter v1 — Solana deferred to Phase 5+."""
from typing import Optional
from .types import Quote


def quote(token_address: str, sell_usd: float, taker_address: str,
          direction: str = "buy", fill_price_usd: float = None) -> Optional[Quote]:
    raise NotImplementedError("Solana trading deferred to Phase 5+")
