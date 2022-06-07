import asyncio
import pathlib
import sys
import time
from datetime import datetime
from decimal import Decimal
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

import aiohttp

from typing import Dict, List, Optional, Tuple, Any

from blspy import AugSchemeMPL, G2Element, PrivateKey, G1Element
from clvm.casts import int_from_bytes, int_to_bytes

from chives.util.hash import std_hash
from chives.types.announcement import Announcement
from chives.types.blockchain_format.coin import Coin
from chives.types.blockchain_format.program import Program, SerializedProgram
from chives.types.blockchain_format.sized_bytes import bytes32
from chives.types.coin_spend import CoinSpend
from chives.types.condition_opcodes import ConditionOpcode
from chives.types.condition_with_args import ConditionWithArgs
from chives.types.spend_bundle import SpendBundle
from chives.util.condition_tools import conditions_by_opcode, conditions_for_solution
from chives.util.ints import uint32, uint64
from chives.wallet.derive_keys import master_sk_to_wallet_sk
from chives.wallet.puzzles.p2_delegated_puzzle_or_hidden_puzzle import (
    DEFAULT_HIDDEN_PUZZLE_HASH,
    calculate_synthetic_secret_key,
    puzzle_for_pk,
    solution_for_conditions,
)


from chives.cmds.show import print_connections
from chives.cmds.units import units
from chives.rpc.wallet_rpc_client import WalletRpcClient
from chives.server.outbound_message import NodeType
from chives.server.start_wallet import SERVICE_NAME
from chives.types.blockchain_format.sized_bytes import bytes32
from chives.util.bech32m import encode_puzzle_hash
from chives.util.config import load_config
from chives.util.default_root import DEFAULT_ROOT_PATH
from chives.util.ints import uint16, uint32, uint64
from chives.wallet.trade_record import TradeRecord
from chives.wallet.trading.offer import Offer
from chives.wallet.trading.trade_status import TradeStatus
from chives.wallet.transaction_record import TransactionRecord
from chives.wallet.util.wallet_types import WalletType

CATNameResolver = Callable[[bytes32], Awaitable[Optional[Tuple[Optional[uint32], str]]]]


if __name__ == "__main__":
    import asyncio
    from chives.cmds.wallet_funcs import execute_with_wallet, print_balances

    args: Dict[str, Any] = {}
    if wallet_type is not None:
        args["type"] = WalletType[wallet_type.upper()]
    asyncio.run(execute_with_wallet(wallet_rpc_port, fingerprint, args, print_balances))
    