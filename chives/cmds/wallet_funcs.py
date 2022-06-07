import asyncio
import pathlib
import sys
import time
from datetime import datetime
from decimal import Decimal
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

import aiohttp

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


def print_transaction(tx: TransactionRecord, verbose: bool, name, address_prefix: str, mojo_per_unit: int) -> None:
    if verbose:
        print(tx)
    else:
        chives_amount = Decimal(int(tx.amount)) / mojo_per_unit
        to_address = encode_puzzle_hash(tx.to_puzzle_hash, address_prefix)
        print(f"Transaction {tx.name}")
        print(f"Status: {'Confirmed' if tx.confirmed else ('In mempool' if tx.is_in_mempool() else 'Pending')}")
        print(f"Amount {'sent' if tx.sent else 'received'}: {chives_amount} {name}")
        print(f"To address: {to_address}")
        print("Created at:", datetime.fromtimestamp(tx.created_at_time).strftime("%Y-%m-%d %H:%M:%S"))
        print("")


def get_mojo_per_unit(wallet_type: WalletType) -> int:
    mojo_per_unit: int
    if wallet_type == WalletType.STANDARD_WALLET or wallet_type == WalletType.POOLING_WALLET:
        mojo_per_unit = units["chives"]
    elif wallet_type == WalletType.CAT:
        mojo_per_unit = units["cat"]
    else:
        raise LookupError("Only standard wallet, Token Wallets, and Plot NFTs are supported")

    return mojo_per_unit


async def get_wallet_type(wallet_id: int, wallet_client: WalletRpcClient) -> WalletType:
    summaries_response = await wallet_client.get_wallets()
    for summary in summaries_response:
        summary_id: int = summary["id"]
        summary_type: int = summary["type"]
        if wallet_id == summary_id:
            return WalletType(summary_type)

    raise LookupError(f"Wallet ID not found: {wallet_id}")


async def get_name_for_wallet_id(
    config: Dict[str, Any],
    wallet_type: WalletType,
    wallet_id: int,
    wallet_client: WalletRpcClient,
):
    if wallet_type == WalletType.STANDARD_WALLET or wallet_type == WalletType.POOLING_WALLET:
        name = config["network_overrides"]["config"][config["selected_network"]]["address_prefix"].upper()
    elif wallet_type == WalletType.CAT:
        name = await wallet_client.get_cat_name(wallet_id=str(wallet_id))
    else:
        raise LookupError("Only standard wallet, Token Wallets, and Plot NFTs are supported")

    return name


async def get_transaction(args: dict, wallet_client: WalletRpcClient, fingerprint: int) -> None:
    transaction_id = bytes32.from_hexstr(args["tx_id"])
    config = load_config(DEFAULT_ROOT_PATH, "config.yaml", SERVICE_NAME)
    address_prefix = config["network_overrides"]["config"][config["selected_network"]]["address_prefix"]
    tx: TransactionRecord = await wallet_client.get_transaction("this is unused", transaction_id=transaction_id)

    try:
        wallet_type = await get_wallet_type(wallet_id=tx.wallet_id, wallet_client=wallet_client)
        mojo_per_unit = get_mojo_per_unit(wallet_type=wallet_type)
        name = await get_name_for_wallet_id(
            config=config,
            wallet_type=wallet_type,
            wallet_id=tx.wallet_id,
            wallet_client=wallet_client,
        )
    except LookupError as e:
        print(e.args[0])
        return

    print_transaction(
        tx,
        verbose=(args["verbose"] > 0),
        name=name,
        address_prefix=address_prefix,
        mojo_per_unit=mojo_per_unit,
    )


async def get_transactions(args: dict, wallet_client: WalletRpcClient, fingerprint: int) -> None:
    wallet_id = args["id"]
    paginate = args["paginate"]
    if paginate is None:
        paginate = sys.stdout.isatty()
    offset = args["offset"]
    limit = args["limit"]
    txs: List[TransactionRecord] = await wallet_client.get_transactions(
        wallet_id, start=offset, end=(offset + limit), reverse=True
    )
    config = load_config(DEFAULT_ROOT_PATH, "config.yaml", SERVICE_NAME)
    address_prefix = config["network_overrides"]["config"][config["selected_network"]]["address_prefix"]
    if len(txs) == 0:
        print("There are no transactions to this address")

    try:
        wallet_type = await get_wallet_type(wallet_id=wallet_id, wallet_client=wallet_client)
        mojo_per_unit = get_mojo_per_unit(wallet_type=wallet_type)
        name = await get_name_for_wallet_id(
            config=config,
            wallet_type=wallet_type,
            wallet_id=wallet_id,
            wallet_client=wallet_client,
        )
    except LookupError as e:
        print(e.args[0])
        return

    num_per_screen = 5 if paginate else len(txs)
    for i in range(0, len(txs), num_per_screen):
        for j in range(0, num_per_screen):
            if i + j >= len(txs):
                break
            print_transaction(
                txs[i + j],
                verbose=(args["verbose"] > 0),
                name=name,
                address_prefix=address_prefix,
                mojo_per_unit=mojo_per_unit,
            )
        if i + num_per_screen >= len(txs):
            return None
        print("Press q to quit, or c to continue")
        while True:
            entered_key = sys.stdin.read(1)
            if entered_key == "q":
                return None
            elif entered_key == "c":
                break


def check_unusual_transaction(amount: Decimal, fee: Decimal):
    return fee >= amount


async def send(args: dict, wallet_client: WalletRpcClient, fingerprint: int) -> None:
    wallet_id: int = args["id"]
    amount = Decimal(args["amount"])
    fee = Decimal(args["fee"])
    address = args["address"]
    override = args["override"]
    memo = args["memo"]
    if memo is None:
        memos = None
    else:
        memos = [memo]

    if not override and check_unusual_transaction(amount, fee):
        print(
            f"A transaction of amount {amount} and fee {fee} is unusual.\n"
            f"Pass in --override if you are sure you mean to do this."
        )
        return

    try:
        typ = await get_wallet_type(wallet_id=wallet_id, wallet_client=wallet_client)
    except LookupError:
        print(f"Wallet id: {wallet_id} not found.")
        return

    final_fee = uint64(int(fee * units["chives"]))
    final_amount: uint64
    if typ == WalletType.STANDARD_WALLET:
        final_amount = uint64(int(amount * units["chives"]))
        print("Submitting transaction...")
        res = await wallet_client.send_transaction(str(wallet_id), final_amount, address, final_fee, memos)
    elif typ == WalletType.CAT:
        final_amount = uint64(int(amount * units["cat"]))
        print("Submitting transaction...")
        res = await wallet_client.cat_spend(str(wallet_id), final_amount, address, final_fee, memos)
    else:
        print("Only standard wallet and Token Wallets are supported")
        return

    tx_id = res.name
    start = time.time()
    while time.time() - start < 10:
        await asyncio.sleep(0.1)
        tx = await wallet_client.get_transaction(str(wallet_id), tx_id)
        if len(tx.sent_to) > 0:
            print(f"Transaction submitted to nodes: {tx.sent_to}")
            print(f"Do chives wallet get_transaction -f {fingerprint} -tx 0x{tx_id} to get status")
            return None

    print("Transaction not yet submitted to nodes")
    print(f"Do 'chives wallet get_transaction -f {fingerprint} -tx 0x{tx_id}' to get status")

async def masternode_merge(args: dict, wallet_client: WalletRpcClient, fingerprint: int) -> None:
    wallet_id: int = 1
    #amount = Decimal(args["amount"])
    #address = args["address"]
    wallet_type = await get_wallet_type(wallet_id=wallet_id, wallet_client=wallet_client)
    mojo_per_unit = get_mojo_per_unit(wallet_type=wallet_type)
    
    balances = await wallet_client.get_wallet_balance(wallet_id)
    max_send_amount = Decimal(balances["max_send_amount"]/mojo_per_unit)
    confirmed_wallet_balance = Decimal(balances["confirmed_wallet_balance"]/mojo_per_unit)
    fee = 1
    override = False
    memo = "Merge coin for MasterNode"
    get_staking_address_result = get_staking_address()
    address = get_staking_address_result['address']
    amount = max_send_amount;
    
    #Staking Amount
    stakingCoinAmount = 100000
    
    #print(balances)
    #print(get_staking_address_result)
    
    #To check is or not have this staking coin record
    isHaveStakingCoin = False
    StakingAccountAmount = 0
    get_target_xcc_coin_result = await get_target_xcc_coin(args,wallet_client,fingerprint,mojo_per_unit,address)
    #print(get_target_xcc_coin_result)
    if get_target_xcc_coin_result is not None:
        for target_xcc_coin in get_target_xcc_coin_result:
            StakingAccountAmount += target_xcc_coin.coin.amount
            if target_xcc_coin.coin.amount == stakingCoinAmount * mojo_per_unit:
                isHaveStakingCoin = True;
    #print(balances);
    print(f"")
    print(f"Wallet Balance:             {confirmed_wallet_balance}");
    print(f"Wallet Max Sent:            {max_send_amount} (Must more than {stakingCoinAmount} XCC)");
    print(f"Wallet Address:             {get_staking_address_result['first_address']}");
    print(f"")
    
    if isHaveStakingCoin is True:
        print("You have staking coins. Not need to merge coin again.");
        print("")
        return None
    
    #Wallet balance must more than 100000 XCC
    if confirmed_wallet_balance < (stakingCoinAmount+fee):
        print(f"Wallet confirmed balance must more than {(stakingCoinAmount+fee)} XCC. Need extra {fee} xcc as miner fee.");
        print("")
        return None
    
    #最大发送金额超过100000 XCC
    if max_send_amount >= (stakingCoinAmount+fee):
        print(f"Wallet Max Sent Amount have more than {(stakingCoinAmount+fee)} XCC, not need to merge coins");
        print("")
        return None
        
    #Merge small amount coins
    #print(f"max_send_amount:{max_send_amount}")
    if max_send_amount>fee and max_send_amount < (stakingCoinAmount+fee) and isHaveStakingCoin == False:
        amount = max_send_amount-fee;
        address = get_staking_address_result['first_address']
        memos = ["Merge coin for MasterNode"]
        if not override and check_unusual_transaction(amount, fee):
            print(
                f"A transaction of amount {amount} and fee {fee} is unusual.\n"
                f"Pass in --override if you are sure you mean to do this."
            )
            return
        try:
            typ = await get_wallet_type(wallet_id=wallet_id, wallet_client=wallet_client)
        except LookupError:
            print(f"Wallet id: {wallet_id} not found.")
            print("")
            return
        final_fee = uint64(int(fee * units["chives"]))
        final_amount: uint64
        if typ == WalletType.STANDARD_WALLET:
            final_amount = uint64(int(amount * units["chives"]))
            print("Merge coin for MasterNode Submitting transaction...")
            print("")
            res = await wallet_client.send_transaction(str(wallet_id), final_amount, address, final_fee, memos)
        else:
            print("Only standard wallet is supported")
            print("")
            return
        tx_id = res.name
        start = time.time()
        while time.time() - start < 10:
            await asyncio.sleep(0.1)
            tx = await wallet_client.get_transaction(str(wallet_id), tx_id)
            if len(tx.sent_to) > 0:
                print(f"Merge coin for MasterNode Transaction submitted to nodes: {tx.sent_to}")
                print(f"fingerprint {fingerprint} tx 0x{tx_id} to address: {address}")
                print("Waiting for block (180s). Do not quit.")
                await asyncio.sleep(180)
                print(f"finish to submit blockchain")
                print("")
                return None
        print("Merge coin for MasterNode not yet submitted to nodes")
        print(f"tx 0x{tx_id} ")
        print("")
    
    

async def masternode_staking(args: dict, wallet_client: WalletRpcClient, fingerprint: int) -> None:
    wallet_id: int = 1
    #amount = Decimal(args["amount"])
    #address = args["address"]
    wallet_type = await get_wallet_type(wallet_id=wallet_id, wallet_client=wallet_client)
    mojo_per_unit = get_mojo_per_unit(wallet_type=wallet_type)
    
    balances = await wallet_client.get_wallet_balance(wallet_id)
    max_send_amount = Decimal(balances["max_send_amount"]/mojo_per_unit)
    confirmed_wallet_balance = Decimal(balances["confirmed_wallet_balance"]/mojo_per_unit)
    fee = 1
    override = False
    memo = "Merge coin for MasterNode"
    get_staking_address_result = get_staking_address()
    address = get_staking_address_result['address']
    amount = max_send_amount;
    
    #Staking Amount
    stakingCoinAmount = 100000
    
    #print(balances)
    #print(get_staking_address_result)
    
    #To check is or not have this staking coin record
    isHaveStakingCoin = False
    StakingAccountAmount = 0
    get_target_xcc_coin_result = await get_target_xcc_coin(args,wallet_client,fingerprint,mojo_per_unit,address)
    #print(get_target_xcc_coin_result)
    if get_target_xcc_coin_result is not None:
        for target_xcc_coin in get_target_xcc_coin_result:
            StakingAccountAmount += target_xcc_coin.coin.amount
            if target_xcc_coin.coin.amount == stakingCoinAmount * mojo_per_unit:
                isHaveStakingCoin = True;
    #print(balances);
    print(f"")
    print(f"Wallet Balance:             {confirmed_wallet_balance}");
    print(f"Wallet Max Sent:            {max_send_amount}");
    print(f"Wallet Address:             {get_staking_address_result['first_address']}");
    print(f"")
    print(f"Staking Address:            {get_staking_address_result['address']}");
    print(f"Staking Account Balance:    {StakingAccountAmount/mojo_per_unit}");
    print(f"Staking Account Status:     {isHaveStakingCoin}");
    print(f"Staking Cancel Address:     {get_staking_address_result['first_address']}");
    print(f"")
    
    if isHaveStakingCoin is True:
        print("You have staking coins. Not need to stake coin again.");
        print("")
        return None
        
    #Wallet balance must more than 100000 XCC
    if confirmed_wallet_balance < (stakingCoinAmount+fee):
        print("Wallet confirmed balance must more than {(stakingCoinAmount+fee)} XCC");
        print("")
        return None
        
    #Merge small amount coins
    #print(f"max_send_amount:{max_send_amount}")
    if max_send_amount < (stakingCoinAmount+fee) and isHaveStakingCoin == False and 1:
        merge(args, wallet_client, fingerprint)
    
    #STAKING COIN
    if max_send_amount >= (stakingCoinAmount+fee) and isHaveStakingCoin == False and 1:
        amount = stakingCoinAmount;
        address = get_staking_address_result['address']
        memos = ["Staking coin for MasterNode"]
        if not override and check_unusual_transaction(amount, fee):
            print(
                f"A transaction of amount {amount} and fee {fee} is unusual.\n"
                f"Pass in --override if you are sure you mean to do this."
            )
            return
        try:
            typ = await get_wallet_type(wallet_id=wallet_id, wallet_client=wallet_client)
        except LookupError:
            print(f"Wallet id: {wallet_id} not found.")
            return
        final_fee = uint64(int(fee * units["chives"]))
        final_amount: uint64
        if typ == WalletType.STANDARD_WALLET:
            final_amount = uint64(int(amount * units["chives"]))
            print("Staking coin for MasterNode Submitting transaction...")
            res = await wallet_client.send_transaction(str(wallet_id), final_amount, address, final_fee, memos)
        else:
            print("Only standard wallet is supported")
            print("")
            return
        tx_id = res.name
        start = time.time()
        while time.time() - start < 10:
            await asyncio.sleep(0.1)
            tx = await wallet_client.get_transaction(str(wallet_id), tx_id)
            if len(tx.sent_to) > 0:
                print(f"Staking coin for MasterNode Transaction submitted to nodes: {tx.sent_to}")
                print(f"fingerprint {fingerprint} tx 0x{tx_id} to address: {address}")
                print("Waiting for block (180s)")
                await asyncio.sleep(180)
                print(f"finish to submit blockchain")
                print("")
                return None
        print("Staking coin for MasterNode not yet submitted to nodes")
        print(f"tx 0x{tx_id} ")
        print("")
    

async def masternode_cancel(args: dict, wallet_client: WalletRpcClient, fingerprint: int) -> None:
    wallet_id: int = 1
    #amount = Decimal(args["amount"])
    #address = args["address"]
    wallet_type = await get_wallet_type(wallet_id=wallet_id, wallet_client=wallet_client)
    mojo_per_unit = get_mojo_per_unit(wallet_type=wallet_type)
    
    balances = await wallet_client.get_wallet_balance(wallet_id)
    max_send_amount = Decimal(balances["max_send_amount"]/mojo_per_unit)
    confirmed_wallet_balance = Decimal(balances["confirmed_wallet_balance"]/mojo_per_unit)
    fee = 1
    override = False
    memo = "Merge coin for MasterNode"
    get_staking_address_result = get_staking_address()
    address = get_staking_address_result['address']
    amount = max_send_amount;
    
    #Staking Amount
    stakingCoinAmount = 100000
    
    #print(balances)
    #print(get_staking_address_result)
    
    #To check is or not have this staking coin record
    isHaveStakingCoin = False
    StakingAccountAmount = 0
    get_target_xcc_coin_result = await get_target_xcc_coin(args,wallet_client,fingerprint,mojo_per_unit,address)
    #print(get_target_xcc_coin_result)
    if get_target_xcc_coin_result is not None:
        for target_xcc_coin in get_target_xcc_coin_result:
            StakingAccountAmount += target_xcc_coin.coin.amount
            if target_xcc_coin.coin.amount == stakingCoinAmount * mojo_per_unit:
                isHaveStakingCoin = True;
    #print(balances);
    print(f"")
    print(f"Wallet Balance:             {confirmed_wallet_balance}");
    print(f"Wallet Max Sent:            {max_send_amount}");
    print(f"Wallet Address:             {get_staking_address_result['first_address']}");
    print(f"")
    print(f"Staking Address:            {get_staking_address_result['address']}");
    print(f"Staking Account Balance:    {StakingAccountAmount/mojo_per_unit}");
    print(f"Staking Account Status:     {isHaveStakingCoin}");
    print(f"Staking Cancel Address:     {get_staking_address_result['first_address']}");
    print(f"")
    
    #取消质押
    if isHaveStakingCoin is True:
        wt = WalletStaking(DEFAULT_CONSTANTS)
        print("Cancel staking coin for MasterNode Submitting transaction...")
        await wt.cancel_staking_coins(keypair=get_staking_address_result, coin_records=get_target_xcc_coin_result)
        print("Canncel staking coins for MasterNode have submitted to nodes")
        print("You have canncel staking coins. Waiting 1-3 minutes, will see your coins.");
        print("")
        return None
    else:
        print("You have not staking coins")
        print("")
        return None

async def masternode_show(args: dict, wallet_client: WalletRpcClient, fingerprint: int) -> None:
    wallet_id: int = 1
    #amount = Decimal(args["amount"])
    #address = args["address"]
    wallet_type = await get_wallet_type(wallet_id=wallet_id, wallet_client=wallet_client)
    mojo_per_unit = get_mojo_per_unit(wallet_type=wallet_type)
    
    balances = await wallet_client.get_wallet_balance(wallet_id)
    max_send_amount = Decimal(balances["max_send_amount"]/mojo_per_unit)
    confirmed_wallet_balance = Decimal(balances["confirmed_wallet_balance"]/mojo_per_unit)
    fee = 1
    override = False
    memo = "Merge coin for MasterNode"
    get_staking_address_result = get_staking_address()
    address = get_staking_address_result['address']
    amount = max_send_amount;
    
    #Staking Amount
    stakingCoinAmount = 100000
    
    #print(balances)
    #print(get_staking_address_result)
    
    #To check is or not have this staking coin record
    isHaveStakingCoin = False
    StakingAccountAmount = 0
    get_target_xcc_coin_result = await get_target_xcc_coin(args,wallet_client,fingerprint,mojo_per_unit,address)
    #print(get_target_xcc_coin_result)
    if get_target_xcc_coin_result is not None:
        for target_xcc_coin in get_target_xcc_coin_result:
            StakingAccountAmount += target_xcc_coin.coin.amount
            if target_xcc_coin.coin.amount == stakingCoinAmount * mojo_per_unit:
                isHaveStakingCoin = True;
    #print(balances);
    print(f"")
    print(f"Wallet Balance:             {confirmed_wallet_balance}");
    print(f"Wallet Max Sent:            {max_send_amount}");
    print(f"Wallet Address:             {get_staking_address_result['first_address']}");
    print(f"")
    print(f"Staking Address:            {get_staking_address_result['address']}");
    print(f"Staking Account Balance:    {StakingAccountAmount/mojo_per_unit}");
    print(f"Staking Account Status:     {isHaveStakingCoin}");
    print(f"Staking Cancel Address:     {get_staking_address_result['first_address']}");
    print(f"")


from chives.masternode.masternode_manager import MasterNodeManager

async def masternode_init(args: dict, wallet_client: WalletRpcClient, fingerprint: int) -> None:
    manager = MasterNodeManager()
    await manager.connect()
    await manager.sync()
    await manager.close()
    
async def masternode_list(args: dict, wallet_client: WalletRpcClient, fingerprint: int) -> None:
    manager = MasterNodeManager()
    await manager.connect()
    nfts = await manager.get_all_masternodes()
    for nft in nfts:
        print_nft(nft)
    await manager.close()
    
async def masternode_register(args: dict, wallet_client: WalletRpcClient, fingerprint: int) -> None:
    manager = MasterNodeManager()
    await manager.connect()
    tx_id, launcher_id = await manager.launch_staking_storage()
    print(f"Transaction id: {tx_id}")
    nft = await manager.wait_for_confirmation(tx_id, launcher_id)
    print("\n\n stake NFT Launched!!")
    print_nft(nft)
    await manager.close()
      
def print_nft(nft):
    print("\n")
    print("-" * 64)
    print(f"MasterNode NFT:  {nft.launcher_id.hex()}")
    print(f"Chialisp:        {str(nft.data[0].decode('utf-8'))}")
    StakingJson = json.loads(nft.data[1].decode("utf-8"))
    print(f"StakingAddress:  {StakingJson['StakingAddress']}")
    print(f"StakingAmount:   {StakingJson['StakingAmount']}")
    print(f"ReceivedAddress: {StakingJson['ReceivedAddress']}")
    print(f"All Data:")
    print(StakingJson)
    print("-" * 64)
    print("\n")
  
import requests
import json
from chives.util.keychain import Keychain, bytes_to_mnemonic, generate_mnemonic, mnemonic_to_seed, unlocks_keyring
from chives.util.byte_types import hexstr_to_bytes
from chives.consensus.coinbase import create_puzzlehash_for_pk
from chives.wallet.derive_keys import (
    master_sk_to_farmer_sk,
    master_sk_to_pool_sk,
    master_sk_to_wallet_sk,
    master_sk_to_wallet_sk_unhardened,
    _derive_path,
    _derive_path_unhardened,
)
from chives.wallet.puzzles.p2_delegated_puzzle_or_hidden_puzzle import (
    DEFAULT_HIDDEN_PUZZLE_HASH,
    calculate_synthetic_secret_key,
    puzzle_for_pk,
    solution_for_conditions,
)
from blspy import AugSchemeMPL, PrivateKey, G1Element
@unlocks_keyring(use_passphrase_cache=True)
def get_staking_address():
    non_observer_derivation = False
    root_path = DEFAULT_ROOT_PATH
    config = load_config(root_path, "config.yaml")
    private_keys = Keychain().get_all_private_keys()
    selected = config["selected_network"]
    prefix = config["network_overrides"]["config"][selected]["address_prefix"]
    if len(private_keys) == 0:
        print("There are no saved private keys")
        return None
    result = {}
    for sk, seed in private_keys:   
        privateKey = _derive_path_unhardened(sk, [12381, 9699, 2, 0])
        publicKey = privateKey.get_g1()
        puzzle = puzzle_for_pk(bytes(publicKey))
        puzzle_hash = puzzle.get_tree_hash()
        #print(puzzle_hash)
        first_address = encode_puzzle_hash(puzzle_hash, prefix)
        result['first_address'] = first_address;      
        result['first_puzzle_hash'] = puzzle_hash;      
    for sk, seed in private_keys:   
        privateKey = _derive_path(sk, [12381, 9699, 99, 0])
        publicKey = privateKey.get_g1()
        puzzle = puzzle_for_pk(bytes(publicKey))
        puzzle_hash = puzzle.get_tree_hash()
        #print(puzzle_hash)
        address = encode_puzzle_hash(puzzle_hash, prefix)
        #print(address)        
        result['privateKey'] = privateKey;
        result['publicKey'] = publicKey;
        result['puzzle_hash'] = puzzle_hash;
        result['address'] = address;
        return result;
        
async def get_target_xcc_coin(args: dict, wallet_client: WalletRpcClient, fingerprint: int, mojo_per_unit: int, address: str) -> None:
    #Check the staking address is have or not coins
    config = load_config(DEFAULT_ROOT_PATH, "config.yaml")
    self_hostname = config["self_hostname"]
    rpc_port = config["full_node"]["rpc_port"]
    client_node = await FullNodeRpcClient.create(self_hostname, uint16(rpc_port), DEFAULT_ROOT_PATH, config)
    
    get_staking_address_result = get_staking_address()
    staking_coins = await client_node.get_coin_records_by_puzzle_hash(get_staking_address_result['puzzle_hash'], include_spent_coins=False)
    if staking_coins:
        return staking_coins
    else:
        return None
        
    client_node.close()
     
async def get_address(args: dict, wallet_client: WalletRpcClient, fingerprint: int) -> None:
    wallet_id = args["id"]
    new_address: bool = args.get("new_address", False)
    res = await wallet_client.get_next_address(wallet_id, new_address)
    print(res)


async def delete_unconfirmed_transactions(args: dict, wallet_client: WalletRpcClient, fingerprint: int) -> None:
    wallet_id = args["id"]
    await wallet_client.delete_unconfirmed_transactions(wallet_id)
    print(f"Successfully deleted all unconfirmed transactions for wallet id {wallet_id} on key {fingerprint}")


async def add_token(args: dict, wallet_client: WalletRpcClient, fingerprint: int) -> None:
    asset_id = args["asset_id"]
    token_name = args["token_name"]
    try:
        asset_id_bytes: bytes32 = bytes32.from_hexstr(asset_id)
        existing_info: Optional[Tuple[Optional[uint32], str]] = await wallet_client.cat_asset_id_to_name(asset_id_bytes)
        if existing_info is None or existing_info[0] is None:
            response = await wallet_client.create_wallet_for_existing_cat(asset_id_bytes)
            wallet_id = response["wallet_id"]
            await wallet_client.set_cat_name(wallet_id, token_name)
            print(f"Successfully added {token_name} with wallet id {wallet_id} on key {fingerprint}")
        else:
            wallet_id, old_name = existing_info
            await wallet_client.set_cat_name(wallet_id, token_name)
            print(f"Successfully renamed {old_name} with wallet_id {wallet_id} on key {fingerprint} to {token_name}")
    except ValueError as e:
        if "fromhex()" in str(e):
            print(f"{asset_id} is not a valid Asset ID")
        else:
            raise e


async def make_offer(args: dict, wallet_client: WalletRpcClient, fingerprint: int) -> None:
    offers: List[str] = args["offers"]
    requests: List[str] = args["requests"]
    filepath: str = args["filepath"]
    fee: int = int(Decimal(args["fee"]) * units["chives"])

    if [] in [offers, requests]:
        print("Not creating offer: Must be offering and requesting at least one asset")
    else:
        offer_dict: Dict[uint32, int] = {}
        printable_dict: Dict[str, Tuple[str, int, int]] = {}  # Dict[asset_name, Tuple[amount, unit, multiplier]]
        for item in [*offers, *requests]:
            wallet_id, amount = tuple(item.split(":")[0:2])
            if int(wallet_id) == 1:
                name: str = "XCC"
                unit: int = units["chives"]
            else:
                name = await wallet_client.get_cat_name(wallet_id)
                unit = units["cat"]
            multiplier: int = -1 if item in offers else 1
            printable_dict[name] = (amount, unit, multiplier)
            if uint32(int(wallet_id)) in offer_dict:
                print("Not creating offer: Cannot offer and request the same asset in a trade")
                break
            else:
                offer_dict[uint32(int(wallet_id))] = int(Decimal(amount) * unit) * multiplier
        else:
            print("Creating Offer")
            print("--------------")
            print()
            print("OFFERING:")
            for name, info in printable_dict.items():
                amount, unit, multiplier = info
                if multiplier < 0:
                    print(f"  - {amount} {name} ({int(Decimal(amount) * unit)} mojos)")
            print("REQUESTING:")
            for name, info in printable_dict.items():
                amount, unit, multiplier = info
                if multiplier > 0:
                    print(f"  - {amount} {name} ({int(Decimal(amount) * unit)} mojos)")

            confirmation = input("Confirm (y/n): ")
            if confirmation not in ["y", "yes"]:
                print("Not creating offer...")
            else:
                offer, trade_record = await wallet_client.create_offer_for_ids(offer_dict, fee=fee)
                if offer is not None:
                    with open(pathlib.Path(filepath), "w") as file:
                        file.write(offer.to_bech32())
                    print(f"Created offer with ID {trade_record.trade_id}")
                    print(f"Use chives wallet get_offers --id {trade_record.trade_id} -f {fingerprint} to view status")
                else:
                    print("Error creating offer")


def timestamp_to_time(timestamp):
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")


async def print_offer_summary(cat_name_resolver: CATNameResolver, sum_dict: Dict[str, int], has_fee: bool = False):
    for asset_id, amount in sum_dict.items():
        description: str = ""
        unit: int = units["chives"]
        wid: str = "1" if asset_id == "xcc" else ""
        mojo_amount: int = int(Decimal(amount))
        name: str = "XCC"
        if asset_id != "xcc":
            name = asset_id
            if asset_id == "unknown":
                name = "Unknown"
                unit = units["mojo"]
                if has_fee:
                    description = " [Typically represents change returned from the included fee]"
            else:
                unit = units["cat"]
                result = await cat_name_resolver(bytes32.from_hexstr(asset_id))
                if result is not None:
                    wid = str(result[0])
                    name = result[1]
        output: str = f"    - {name}"
        mojo_str: str = f"{mojo_amount} {'mojo' if mojo_amount == 1 else 'mojos'}"
        if len(wid) > 0:
            output += f" (Wallet ID: {wid})"
        if unit == units["mojo"]:
            output += f": {mojo_str}"
        else:
            output += f": {mojo_amount / unit} ({mojo_str})"
        if len(description) > 0:
            output += f" {description}"
        print(output)


async def print_trade_record(record, wallet_client: WalletRpcClient, summaries: bool = False) -> None:
    print()
    print(f"Record with id: {record.trade_id}")
    print("---------------")
    print(f"Created at: {timestamp_to_time(record.created_at_time)}")
    print(f"Confirmed at: {record.confirmed_at_index}")
    print(f"Accepted at: {timestamp_to_time(record.accepted_at_time) if record.accepted_at_time else 'N/A'}")
    print(f"Status: {TradeStatus(record.status).name}")
    if summaries:
        print("Summary:")
        offer = Offer.from_bytes(record.offer)
        offered, requested = offer.summary()
        outbound_balances: Dict[str, int] = offer.get_pending_amounts()
        fees: Decimal = Decimal(offer.bundle.fees())
        cat_name_resolver = wallet_client.cat_asset_id_to_name
        print("  OFFERED:")
        await print_offer_summary(cat_name_resolver, offered)
        print("  REQUESTED:")
        await print_offer_summary(cat_name_resolver, requested)
        print("Pending Outbound Balances:")
        await print_offer_summary(cat_name_resolver, outbound_balances, has_fee=(fees > 0))
        print(f"Included Fees: {fees / units['chives']}")
    print("---------------")


async def get_offers(args: dict, wallet_client: WalletRpcClient, fingerprint: int) -> None:
    id: Optional[str] = args.get("id", None)
    filepath: Optional[str] = args.get("filepath", None)
    exclude_my_offers: bool = args.get("exclude_my_offers", False)
    exclude_taken_offers: bool = args.get("exclude_taken_offers", False)
    include_completed: bool = args.get("include_completed", False)
    summaries: bool = args.get("summaries", False)
    reverse: bool = args.get("reverse", False)
    file_contents: bool = (filepath is not None) or summaries
    records: List[TradeRecord] = []
    if id is None:
        batch_size: int = 10
        start: int = 0
        end: int = start + batch_size

        # Traverse offers page by page
        while True:
            new_records: List[TradeRecord] = await wallet_client.get_all_offers(
                start,
                end,
                reverse=reverse,
                file_contents=file_contents,
                exclude_my_offers=exclude_my_offers,
                exclude_taken_offers=exclude_taken_offers,
                include_completed=include_completed,
            )
            records.extend(new_records)

            # If fewer records were returned than requested, we're done
            if len(new_records) < batch_size:
                break

            start = end
            end += batch_size
    else:
        records = [await wallet_client.get_offer(bytes32.from_hexstr(id), file_contents)]
        if filepath is not None:
            with open(pathlib.Path(filepath), "w") as file:
                file.write(Offer.from_bytes(records[0].offer).to_bech32())
                file.close()

    for record in records:
        await print_trade_record(record, wallet_client, summaries=summaries)


async def take_offer(args: dict, wallet_client: WalletRpcClient, fingerprint: int) -> None:
    if "." in args["file"]:
        filepath = pathlib.Path(args["file"])
        with open(filepath, "r") as file:
            offer_hex: str = file.read()
            file.close()
    else:
        offer_hex = args["file"]

    examine_only: bool = args["examine_only"]
    fee: int = int(Decimal(args["fee"]) * units["chives"])

    try:
        offer = Offer.from_bech32(offer_hex)
    except ValueError:
        print("Please enter a valid offer file or hex blob")
        return

    offered, requested = offer.summary()
    cat_name_resolver = wallet_client.cat_asset_id_to_name
    print("Summary:")
    print("  OFFERED:")
    await print_offer_summary(cat_name_resolver, offered)
    print("  REQUESTED:")
    await print_offer_summary(cat_name_resolver, requested)
    print(f"Included Fees: {Decimal(offer.bundle.fees()) / units['chives']}")

    if not examine_only:
        confirmation = input("Would you like to take this offer? (y/n): ")
        if confirmation in ["y", "yes"]:
            trade_record = await wallet_client.take_offer(offer, fee=fee)
            print(f"Accepted offer with ID {trade_record.trade_id}")
            print(f"Use chives wallet get_offers --id {trade_record.trade_id} -f {fingerprint} to view its status")


async def cancel_offer(args: dict, wallet_client: WalletRpcClient, fingerprint: int) -> None:
    id = bytes32.from_hexstr(args["id"])
    secure: bool = not args["insecure"]
    fee: int = int(Decimal(args["fee"]) * units["chives"])

    trade_record = await wallet_client.get_offer(id, file_contents=True)
    await print_trade_record(trade_record, wallet_client, summaries=True)

    confirmation = input(f"Are you sure you wish to cancel offer with ID: {trade_record.trade_id}? (y/n): ")
    if confirmation in ["y", "yes"]:
        await wallet_client.cancel_offer(id, secure=secure, fee=fee)
        print(f"Cancelled offer with ID {trade_record.trade_id}")
        if secure:
            print(f"Use chives wallet get_offers --id {trade_record.trade_id} -f {fingerprint} to view cancel status")


def wallet_coin_unit(typ: WalletType, address_prefix: str) -> Tuple[str, int]:
    if typ == WalletType.CAT:
        return "", units["cat"]
    if typ in [WalletType.STANDARD_WALLET, WalletType.POOLING_WALLET, WalletType.MULTI_SIG, WalletType.RATE_LIMITED]:
        return address_prefix, units["chives"]
    return "", units["mojo"]


def print_balance(amount: int, scale: int, address_prefix: str) -> str:
    ret = f"{amount/scale} {address_prefix} "
    if scale > 1:
        ret += f"({amount} mojo)"
    return ret


async def print_balances(args: dict, wallet_client: WalletRpcClient, fingerprint: int) -> None:
    wallet_type: Optional[WalletType] = None
    if "type" in args:
        wallet_type = WalletType(args["type"])
    summaries_response = await wallet_client.get_wallets(wallet_type)
    config = load_config(DEFAULT_ROOT_PATH, "config.yaml")
    address_prefix = config["network_overrides"]["config"][config["selected_network"]]["address_prefix"]

    is_synced: bool = await wallet_client.get_synced()
    is_syncing: bool = await wallet_client.get_sync_status()

    print(f"Wallet height: {await wallet_client.get_height_info()}")
    if is_syncing:
        print("Sync status: Syncing...")
    elif is_synced:
        print("Sync status: Synced")
    else:
        print("Sync status: Not synced")

    if not is_syncing and is_synced:
        if len(summaries_response) == 0:
            type_hint = " " if wallet_type is None else f" from type {wallet_type.name} "
            print(f"\nNo wallets{type_hint}available for fingerprint: {fingerprint}")
        else:
            print(f"Balances, fingerprint: {fingerprint}")
        for summary in summaries_response:
            indent: str = "   "
            # asset_id currently contains both the asset ID and TAIL program bytes concatenated together.
            # A future RPC update may split them apart, but for now we'll show the first 32 bytes (64 chars)
            asset_id = summary["data"][:64]
            wallet_id = summary["id"]
            balances = await wallet_client.get_wallet_balance(wallet_id)
            typ = WalletType(int(summary["type"]))
            address_prefix, scale = wallet_coin_unit(typ, address_prefix)
            total_balance: str = print_balance(balances["confirmed_wallet_balance"], scale, address_prefix)
            unconfirmed_wallet_balance: str = print_balance(
                balances["unconfirmed_wallet_balance"], scale, address_prefix
            )
            spendable_balance: str = print_balance(balances["spendable_balance"], scale, address_prefix)
            max_send_amount: str = print_balance(balances["max_send_amount"], scale, address_prefix)
            print()
            print(f"{summary['name']}:")
            print(f"{indent}{'-Total Balance:'.ljust(23)} {total_balance}")
            print(f"{indent}{'-Pending Total Balance:'.ljust(23)} " f"{unconfirmed_wallet_balance}")
            print(f"{indent}{'-Spendable:'.ljust(23)} {spendable_balance}")
            print(f"{indent}{'-Max Send Amount:'.ljust(23)} {max_send_amount}")
            print(f"{indent}{'-Type:'.ljust(23)} {typ.name}")
            if len(asset_id) > 0:
                print(f"{indent}{'-Asset ID:'.ljust(23)} {asset_id}")
            print(f"{indent}{'-Wallet ID:'.ljust(23)} {wallet_id}")

    print(" ")
    trusted_peers: Dict = config["wallet"].get("trusted_peers", {})
    await print_connections(wallet_client, time, NodeType, trusted_peers)


async def get_wallet(wallet_client: WalletRpcClient, fingerprint: int = None) -> Optional[Tuple[WalletRpcClient, int]]:
    if fingerprint is not None:
        fingerprints = [fingerprint]
    else:
        fingerprints = await wallet_client.get_public_keys()
    if len(fingerprints) == 0:
        print("No keys loaded. Run 'chives keys generate' or import a key")
        return None
    if len(fingerprints) == 1:
        fingerprint = fingerprints[0]
    if fingerprint is not None:
        log_in_response = await wallet_client.log_in(fingerprint)
    else:
        logged_in_fingerprint: Optional[int] = await wallet_client.get_logged_in_fingerprint()
        spacing: str = "  " if logged_in_fingerprint is not None else ""
        current_sync_status: str = ""
        if logged_in_fingerprint is not None:
            if await wallet_client.get_synced():
                current_sync_status = "Synced"
            elif await wallet_client.get_sync_status():
                current_sync_status = "Syncing"
            else:
                current_sync_status = "Not Synced"
        print("Wallet keys:")
        for i, fp in enumerate(fingerprints):
            row: str = f"{i+1}) "
            row += "* " if fp == logged_in_fingerprint else spacing
            row += f"{fp}"
            if fp == logged_in_fingerprint and len(current_sync_status) > 0:
                row += f" ({current_sync_status})"
            print(row)
        val = None
        prompt: str = (
            f"Choose a wallet key [1-{len(fingerprints)}] ('q' to quit, or Enter to use {logged_in_fingerprint}): "
        )
        while val is None:
            val = input(prompt)
            if val == "q":
                return None
            elif val == "" and logged_in_fingerprint is not None:
                fingerprint = logged_in_fingerprint
                break
            elif not val.isdigit():
                val = None
            else:
                index = int(val) - 1
                if index < 0 or index >= len(fingerprints):
                    print("Invalid value")
                    val = None
                    continue
                else:
                    fingerprint = fingerprints[index]
        assert fingerprint is not None
        log_in_response = await wallet_client.log_in(fingerprint)

    if log_in_response["success"] is False:
        print(f"Login failed: {log_in_response}")
        return None
    return wallet_client, fingerprint


async def execute_with_wallet(
    wallet_rpc_port: Optional[int], fingerprint: int, extra_params: Dict, function: Callable
) -> None:
    try:
        config = load_config(DEFAULT_ROOT_PATH, "config.yaml")
        self_hostname = config["self_hostname"]
        if wallet_rpc_port is None:
            wallet_rpc_port = config["wallet"]["rpc_port"]
        wallet_client = await WalletRpcClient.create(self_hostname, uint16(wallet_rpc_port), DEFAULT_ROOT_PATH, config)
        wallet_client_f = await get_wallet(wallet_client, fingerprint=fingerprint)
        if wallet_client_f is None:
            wallet_client.close()
            await wallet_client.await_closed()
            return None
        wallet_client, fingerprint = wallet_client_f
        await function(extra_params, wallet_client, fingerprint)
    except KeyboardInterrupt:
        pass
    except Exception as e:
        if isinstance(e, aiohttp.ClientConnectorError):
            print(
                f"Connection error. Check if the wallet is running at {wallet_rpc_port}. "
                "You can run the wallet via:\n\tchives start wallet"
            )
        else:
            print(f"Exception from 'wallet' {e}")
    wallet_client.close()
    await wallet_client.await_closed()









import json
import time
import asyncio
import aiosqlite
import sqlite3
import logging

from typing import Dict, List, Optional
from blspy import AugSchemeMPL, G2Element, PrivateKey
from chives.consensus.constants import ConsensusConstants
from chives.util.hash import std_hash
from chives.types.announcement import Announcement
from chives.types.blockchain_format.coin import Coin
from chives.types.blockchain_format.program import Program
from chives.types.blockchain_format.sized_bytes import bytes32
from chives.types.coin_spend import CoinSpend
from chives.types.condition_opcodes import ConditionOpcode
from chives.types.condition_with_args import ConditionWithArgs
from chives.types.spend_bundle import SpendBundle
from clvm.casts import int_from_bytes, int_to_bytes
from chives.util.condition_tools import conditions_by_opcode, conditions_for_solution, pkm_pairs_for_conditions_dict
from chives.util.ints import uint32, uint64
from chives.util.byte_types import hexstr_to_bytes
from chives.types.blockchain_format.classgroup import ClassgroupElement
from chives.types.blockchain_format.coin import Coin
from chives.types.blockchain_format.foliage import TransactionsInfo
from chives.types.blockchain_format.program import SerializedProgram
from chives.types.blockchain_format.sized_bytes import bytes32
from chives.types.blockchain_format.slots import InfusedChallengeChainSubSlot
from chives.types.blockchain_format.vdf import VDFInfo, VDFProof
from chives.types.end_of_slot_bundle import EndOfSubSlotBundle
from chives.types.full_block import FullBlock
from chives.types.unfinished_block import UnfinishedBlock
from chives.wallet.derive_keys import master_sk_to_wallet_sk
from chives.wallet.puzzles.p2_delegated_puzzle_or_hidden_puzzle import (
    DEFAULT_HIDDEN_PUZZLE_HASH,
    calculate_synthetic_secret_key,
    puzzle_for_pk,
    solution_for_conditions,
)
from chives.wallet.puzzles.puzzle_utils import (
    make_assert_aggsig_condition,
    make_assert_coin_announcement,
    make_assert_puzzle_announcement,
    make_assert_relative_height_exceeds_condition,
    make_assert_absolute_height_exceeds_condition,
    make_assert_my_coin_id_condition,
    make_assert_absolute_seconds_exceeds_condition,
    make_assert_relative_seconds_exceeds_condition,
    make_create_coin_announcement,
    make_create_puzzle_announcement,
    make_create_coin_condition,
    make_reserve_fee_condition,
    make_assert_my_parent_id,
    make_assert_my_puzzlehash,
    make_assert_my_amount,
)
from chives.util.keychain import Keychain, bytes_from_mnemonic, bytes_to_mnemonic, generate_mnemonic, mnemonic_to_seed
from chives.consensus.default_constants import DEFAULT_CONSTANTS
from chives.rpc.full_node_rpc_api import FullNodeRpcApi
from chives.rpc.full_node_rpc_client import FullNodeRpcClient
from chives.util.default_root import DEFAULT_ROOT_PATH
from chives.util.config import load_config
from chives.util.ints import uint16
from chives.util.misc import format_bytes

#使用指定的私钥把COIN转到指定的账户. 因为标准钱包客户端只能处理标准的COIN,当需要撤回质押的COIN时,需要一个单独的转账程序来执行转账操作.

class WalletStaking:
    def __init__(self, DEFAULT_CONSTANTS: ConsensusConstants):
        config = load_config(DEFAULT_ROOT_PATH, "config.yaml")
        network_agg_sig_data = config["network_overrides"]["constants"][config['selected_network']]["AGG_SIG_ME_ADDITIONAL_DATA"]
        DEFAULT_CONSTANTS = DEFAULT_CONSTANTS.replace_str_to_bytes(**{"AGG_SIG_ME_ADDITIONAL_DATA": network_agg_sig_data})       
        self.constants = DEFAULT_CONSTANTS
        self.config = config
        self.keypair: Dict = {}     
    async def  cancel_staking_coins(self, keypair: dict, coin_records: List):           
        #keypair['privateKey'] = privateKey;
        #keypair['publicKey'] = publicKey;
        #keypair['puzzle_hash'] = puzzle_hash;
        #keypair['address'] = address;
        self.keypair = keypair;        
        fee = uint64(0)
        SendToPuzzleHash = str(keypair['first_puzzle_hash'])        
        coinList = []
        SendToAmount = 0
        for row in coin_records:
            coin = row.coin
            SendToAmount += uint64(row.coin.amount)
            coinList.append(coin)
        if(len(coinList)==0):
            return ''        
        #coinList里面是一个数组,里面包含有的COIN对像. 这个函数可以传入多个COIN,可以实现多个输入,对应两个输出的结构.
        generate_signed_transaction = self.generate_signed_transaction_multiple_coins(
            SendToAmount,
            SendToPuzzleHash,
            coinList,
            {},
            fee,
        )        
        #提交交易记录到区块链网络
        try:
            self_hostname = self.config["self_hostname"]
            rpc_port = self.config["full_node"]["rpc_port"]
            client_node = await FullNodeRpcClient.create(self_hostname, uint16(rpc_port), DEFAULT_ROOT_PATH, self.config)
            push_tx = await client_node.push_tx(generate_signed_transaction)
            print(f"Begin to Cancel Staking Coin for MasterNode.")
            if push_tx['status']=="SUCCESS" and push_tx['success']==True:
                #print(f"fingerprint {fingerprint} tx 0x{tx_id} to address: {address}")
                print("Waiting for block (180s). Do not quit.")
                print(f"tx_id:{generate_signed_transaction.name()}") 
                await asyncio.sleep(180)
                print(f"finish to Cancel Staking Coin")            
        except Exception as e:
            print(f"Exception {e}")
        finally:
            client_node.close()
            await client_node.await_closed()
            
    def make_solution(self, condition_dic: Dict[ConditionOpcode, List[ConditionWithArgs]]) -> Program:
        ret = []
        for con_list in condition_dic.values():
            for cvp in con_list:
                if cvp.opcode == ConditionOpcode.CREATE_COIN:
                    ret.append(make_create_coin_condition(cvp.vars[0], cvp.vars[1], None))
                if cvp.opcode == ConditionOpcode.CREATE_COIN_ANNOUNCEMENT:
                    ret.append(make_create_coin_announcement(cvp.vars[0]))
                if cvp.opcode == ConditionOpcode.CREATE_PUZZLE_ANNOUNCEMENT:
                    ret.append(make_create_puzzle_announcement(cvp.vars[0]))
                if cvp.opcode == ConditionOpcode.AGG_SIG_UNSAFE:
                    ret.append(make_assert_aggsig_condition(cvp.vars[0]))
                if cvp.opcode == ConditionOpcode.ASSERT_COIN_ANNOUNCEMENT:
                    ret.append(make_assert_coin_announcement(cvp.vars[0]))
                if cvp.opcode == ConditionOpcode.ASSERT_PUZZLE_ANNOUNCEMENT:
                    ret.append(make_assert_puzzle_announcement(cvp.vars[0]))
                if cvp.opcode == ConditionOpcode.ASSERT_SECONDS_ABSOLUTE:
                    ret.append(make_assert_absolute_seconds_exceeds_condition(cvp.vars[0]))
                if cvp.opcode == ConditionOpcode.ASSERT_SECONDS_RELATIVE:
                    ret.append(make_assert_relative_seconds_exceeds_condition(cvp.vars[0]))
                if cvp.opcode == ConditionOpcode.ASSERT_MY_COIN_ID:
                    ret.append(make_assert_my_coin_id_condition(cvp.vars[0]))
                if cvp.opcode == ConditionOpcode.ASSERT_HEIGHT_ABSOLUTE:
                    ret.append(make_assert_absolute_height_exceeds_condition(cvp.vars[0]))
                if cvp.opcode == ConditionOpcode.ASSERT_HEIGHT_RELATIVE:
                    ret.append(make_assert_relative_height_exceeds_condition(cvp.vars[0]))
                if cvp.opcode == ConditionOpcode.RESERVE_FEE:
                    ret.append(make_reserve_fee_condition(cvp.vars[0]))
                if cvp.opcode == ConditionOpcode.ASSERT_MY_PARENT_ID:
                    ret.append(make_assert_my_parent_id(cvp.vars[0]))
                if cvp.opcode == ConditionOpcode.ASSERT_MY_PUZZLEHASH:
                    ret.append(make_assert_my_puzzlehash(cvp.vars[0]))
                if cvp.opcode == ConditionOpcode.ASSERT_MY_AMOUNT:
                    ret.append(make_assert_my_amount(cvp.vars[0]))
        return solution_for_conditions(Program.to(ret))

    def generate_unsigned_transaction(
        self,
        amount: uint64,
        new_puzzle_hash: bytes32,
        coins: List[Coin],
        condition_dic: Dict[ConditionOpcode, List[ConditionWithArgs]],
        fee: int = 0,
        secret_key: Optional[PrivateKey] = None,
    ) -> List[CoinSpend]:
        spends = []        
        spend_value = sum([c.amount for c in coins])
        if ConditionOpcode.CREATE_COIN not in condition_dic:
            condition_dic[ConditionOpcode.CREATE_COIN] = []
        if ConditionOpcode.CREATE_COIN_ANNOUNCEMENT not in condition_dic:
            condition_dic[ConditionOpcode.CREATE_COIN_ANNOUNCEMENT] = []
        output = ConditionWithArgs(ConditionOpcode.CREATE_COIN, [hexstr_to_bytes(new_puzzle_hash), int_to_bytes(amount)])
        condition_dic[output.opcode].append(output)
        amount_total = sum(int_from_bytes(cvp.vars[1]) for cvp in condition_dic[ConditionOpcode.CREATE_COIN])
        change = spend_value - amount_total - fee
        #会把全部的COIN转移走
        #if change > 0:
        #    change_puzzle_hash = self.get_new_puzzlehash()
        #    change_output = ConditionWithArgs(ConditionOpcode.CREATE_COIN, [change_puzzle_hash, int_to_bytes(change)])
        #    condition_dic[output.opcode].append(change_output)
        secondary_coins_cond_dic: Dict[ConditionOpcode, List[ConditionWithArgs]] = dict()
        secondary_coins_cond_dic[ConditionOpcode.ASSERT_COIN_ANNOUNCEMENT] = []        
        for n, coin in enumerate(coins):
            puzzle_hash = coin.puzzle_hash
            pubkey = self.keypair['publicKey']
            puzzle = puzzle_for_pk(bytes(pubkey))
            if n == 0:
                message_list = [c.name() for c in coins]
                for outputs in condition_dic[ConditionOpcode.CREATE_COIN]:
                    message_list.append(Coin(coin.name(), outputs.vars[0], int_from_bytes(outputs.vars[1])).name())
                message = std_hash(b"".join(message_list))
                condition_dic[ConditionOpcode.CREATE_COIN_ANNOUNCEMENT].append(
                    ConditionWithArgs(ConditionOpcode.CREATE_COIN_ANNOUNCEMENT, [message])
                )
                primary_announcement_hash = Announcement(coin.name(), message).name()
                secondary_coins_cond_dic[ConditionOpcode.ASSERT_COIN_ANNOUNCEMENT].append(
                    ConditionWithArgs(ConditionOpcode.ASSERT_COIN_ANNOUNCEMENT, [primary_announcement_hash])
                )
                main_solution = self.make_solution(condition_dic)
                spends.append(CoinSpend(coin, puzzle, main_solution))
            else:
                spends.append(CoinSpend(coin, puzzle, self.make_solution(secondary_coins_cond_dic)))
        return spends

    def sign_transaction(self, coin_solutions: List[CoinSpend]) -> SpendBundle:
        signatures = []
        solution: Program
        puzzle: Program
        for coin_solution in coin_solutions:  # type: ignore # noqa
            secret_key = self.keypair['privateKey']
            synthetic_secret_key = calculate_synthetic_secret_key(secret_key, DEFAULT_HIDDEN_PUZZLE_HASH)
            err, con, cost = conditions_for_solution(
                coin_solution.puzzle_reveal, coin_solution.solution, self.constants.MAX_BLOCK_COST_CLVM
            )
            if not con:
                raise ValueError(err)
            conditions_dict = conditions_by_opcode(con)
            for _, msg in pkm_pairs_for_conditions_dict(
                conditions_dict, bytes(coin_solution.coin.name()), self.constants.AGG_SIG_ME_ADDITIONAL_DATA
            ):
                signature = AugSchemeMPL.sign(synthetic_secret_key, msg)
                signatures.append(signature)
        aggsig = AugSchemeMPL.aggregate(signatures)
        spend_bundle = SpendBundle(coin_solutions, aggsig)
        return spend_bundle

    def generate_signed_transaction(
        self,
        amount: uint64,
        new_puzzle_hash: bytes32,
        coin: Coin,
        condition_dic: Dict[ConditionOpcode, List[ConditionWithArgs]] = None,
        fee: int = 0,
    ) -> SpendBundle:
        if condition_dic is None:
            condition_dic = {}
        transaction = self.generate_unsigned_transaction(amount, new_puzzle_hash, [coin], condition_dic, fee)
        assert transaction is not None
        return self.sign_transaction(transaction)

    def generate_signed_transaction_multiple_coins(
        self,
        amount: uint64,
        new_puzzle_hash: bytes32,
        coins: List[Coin],
        condition_dic: Dict[ConditionOpcode, List[ConditionWithArgs]] = None,
        fee: int = 0,
    ) -> SpendBundle:
        if condition_dic is None:
            condition_dic = {}
        transaction = self.generate_unsigned_transaction(amount, new_puzzle_hash, coins, condition_dic, fee)
        assert transaction is not None
        return self.sign_transaction(transaction)
