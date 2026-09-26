"""Hard spending limits, enforced where the keys are.

These hold no matter who asks for a signature: the Hermes plugin, the model calling the signer directly from a
terminal, or anything else on the network. A signature counts as spent the moment it is issued, because the
server holding it can settle it whether or not it delivers; the ledger is therefore conservative.

Two buckets. Payments to a top-up payee (a broker's receiving address, where the money becomes the payer's own prepaid
balance) use the top-up limits; everything else uses the standard limits. Each bucket has its own 24-hour total, so
top-ups cannot use up the standard allowance or the other way round."""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

DAY = 86400


def _csv(value: str) -> tuple[str, ...]:
    return tuple(v.strip() for v in (value or "").split(",") if v.strip())


@dataclass
class Limits:
    per_payment_usd: float
    per_day_usd: float
    networks: tuple[str, ...]
    topup_per_payment_usd: float = 20.0
    topup_per_day_usd: float = 20.0
    topup_payees: tuple[str, ...] = ()

    @classmethod
    def from_env(cls) -> "Limits":
        return cls(float(os.environ.get("X402_MAX_PER_PAYMENT_USD", "1.00")),
                   float(os.environ.get("X402_MAX_PER_DAY_USD", "5.00")),
                   _csv(os.environ.get("X402_NETWORKS", "eip155:8453")),
                   float(os.environ.get("X402_MAX_TOPUP_USD", "20.00")),
                   float(os.environ.get("X402_MAX_TOPUP_PER_DAY_USD", "20.00")),
                   _csv(os.environ.get("X402_TOPUP_PAYEES", "")))

    def is_topup(self, pay_to: str) -> bool:
        return str(pay_to).lower() in {p.lower() for p in self.topup_payees}

    def public(self) -> dict:
        return {"per_payment_usd": self.per_payment_usd, "per_day_usd": self.per_day_usd, "networks": list(self.networks),
                "topup_per_payment_usd": self.topup_per_payment_usd, "topup_per_day_usd": self.topup_per_day_usd,
                "topup_payees": list(self.topup_payees)}


class Ledger:
    def __init__(self, data_dir: str | os.PathLike):
        p = Path(data_dir)
        p.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(p / "ledger.db", check_same_thread=False)
        self.db.execute("""create table if not exists spends (
            id integer primary key, ts real not null, wallet text not null, address text not null,
            network text not null, asset text not null, pay_to text not null, units integer not null,
            usd real not null, resource text, nonce text not null)""")
        self.db.commit()
        self.lock = threading.Lock()

    def spent_usd(self, since: float, payees: tuple[str, ...] | None = None, exclude: tuple[str, ...] = ()) -> float:
        """USD signed since `since`: to `payees` only when given, never to `exclude` (addresses, case-insensitive)."""
        sql, args = "select coalesce(sum(usd), 0) from spends where ts >= ?", [since]
        if payees is not None:
            sql += f" and lower(pay_to) in ({','.join('?' * len(payees)) or 'null'})"
            args += [p.lower() for p in payees]
        if exclude:
            sql += f" and lower(pay_to) not in ({','.join('?' * len(exclude))})"
            args += [p.lower() for p in exclude]
        return float(self.db.execute(sql, args).fetchone()[0])

    def record(self, **row):
        self.db.execute("insert into spends (ts, wallet, address, network, asset, pay_to, units, usd, resource, nonce) "
                        "values (:ts, :wallet, :address, :network, :asset, :pay_to, :units, :usd, :resource, :nonce)", row)
        self.db.commit()

    def recent(self, limit: int = 20) -> list[dict]:
        cur = self.db.execute("select ts, wallet, network, pay_to, usd, resource from spends order by id desc limit ?",
                              (limit,))
        return [dict(zip(("ts", "wallet", "network", "pay_to", "usd", "resource"), r)) for r in cur.fetchall()]


class PolicyError(Exception):
    """A request the limits refuse. The message is safe to show the model and the user."""


def check(limits: Limits, ledger: Ledger, network: str, usd: float, pay_to: str = "", now: float | None = None) -> None:
    now = time.time() if now is None else now
    if network not in limits.networks:
        raise PolicyError(f"network {network} is not enabled on this signer (enabled: {', '.join(limits.networks)})")
    if usd <= 0:
        raise PolicyError("refusing a zero or negative amount")
    if limits.is_topup(pay_to):
        kind, per_payment, per_day = "top-up", limits.topup_per_payment_usd, limits.topup_per_day_usd
        spent = ledger.spent_usd(now - DAY, payees=limits.topup_payees)
    else:
        kind, per_payment, per_day = "per-payment", limits.per_payment_usd, limits.per_day_usd
        spent = ledger.spent_usd(now - DAY, exclude=limits.topup_payees)
    if usd > per_payment + 1e-9:
        raise PolicyError(f"${usd:.6f} is above the {kind} limit of ${per_payment:.2f}")
    if spent + usd > per_day + 1e-9:
        label = "daily top-up limit" if kind == "top-up" else "daily limit"
        raise PolicyError(f"${usd:.6f} would take the last 24 hours to ${spent + usd:.6f}, above the {label} of ${per_day:.2f}")
