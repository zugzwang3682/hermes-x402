"""Networks and assets the signer knows how to price and pay.

The signer only signs for assets listed here: an amount is meaningless without knowing the token and its
decimals, and a server that names an unknown asset gets no signature. Every listed asset is native USDC,
so one unit is a millionth of a dollar."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Asset:
    network: str        # CAIP-2 id
    address: str        # token contract (EVM) or mint (Solana)
    symbol: str
    decimals: int
    eip712_name: str    # EIP-712 domain name for EIP-3009 (EVM only)
    eip712_version: str
    testnet: bool
    explorer_tx: str    # prefix for a transaction link

    def units_to_usd(self, units: int) -> float:
        return units / 10 ** self.decimals


ASSETS = [
    Asset("eip155:8453", "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "USDC", 6, "USD Coin", "2", False,
          "https://basescan.org/tx/"),
    Asset("eip155:1", "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48", "USDC", 6, "USD Coin", "2", False,
          "https://etherscan.io/tx/"),
    Asset("eip155:84532", "0x036CbD53842c5426634e7929541eC2318f3dCF7e", "USDC", 6, "USDC", "2", True,
          "https://sepolia.basescan.org/tx/"),
    Asset("eip155:11155111", "0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238", "USDC", 6, "USDC", "2", True,
          "https://sepolia.etherscan.io/tx/"),
]

NAMES = {"eip155:8453": "Base", "eip155:1": "Ethereum", "eip155:84532": "Base Sepolia",
         "eip155:11155111": "Ethereum Sepolia"}

DEFAULT_RPC = {"eip155:8453": "https://mainnet.base.org", "eip155:1": "https://ethereum-rpc.publicnode.com",
               "eip155:84532": "https://sepolia.base.org", "eip155:11155111": "https://ethereum-sepolia-rpc.publicnode.com"}


def find_asset(network: str, address: str) -> Asset | None:
    for a in ASSETS:
        if a.network == network and a.address.lower() == str(address).lower():
            return a
    return None


def family(network: str) -> str:
    """'evm' for eip155:*, 'solana' for solana:*; wallets are per family, not per chain."""
    return network.split(":", 1)[0].replace("eip155", "evm")
