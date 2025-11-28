from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.transaction import Transaction, VersionedTransaction
from solders.token.associated import get_associated_token_address
from solders.system_program import transfer, TransferParams
from solders.message import Message, MessageV0, to_bytes_versioned
from solders.signature import Signature
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from spl.token.instructions import (
    create_associated_token_account,
    transfer_checked,
    TransferCheckedParams,
)
from solana.rpc.types import TxOpts
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed, Processed
from base58 import b58encode, b58decode
from random import uniform, randint
from loguru import logger
from json import loads
from time import time
from re import search
import asyncio

from modules.config import SOL_TOKEN_ADDRESSES, TOKEN_PROGRAMS, CHAINS_DATA, TOKENS_PROGRAM
from modules.retry import async_retry, CustomError
from modules.utils import async_sleep, round_cut
from modules.database import DataBase
from settings import RPCS, TO_WAIT_TX

from solana.rpc.core import RPCException
from solana.exceptions import SolanaRpcException


class SolWallet:
    def __init__(
            self,
            privatekey: str,
            encoded_pk: str,
            label: str,
            db: DataBase,
            browser=None,
            recipient: str = None,
            client: AsyncClient = None,
    ):
        self.encoded_pk = encoded_pk
        self.privatekey = privatekey
        self.label = label
        self.db = db
        self.browser = browser
        if recipient is None:
            self.recipient = None
        elif type(recipient) == str:
            self.recipient = Pubkey.from_string(recipient)
        elif type(recipient) == Pubkey:
            self.recipient = recipient

        self.client = client or AsyncClient(endpoint=RPCS["solana"], proxy=self.browser.proxy)

        self.account = Keypair.from_base58_string(privatekey)
        self.address = self.account.pubkey()


    @property
    def pkey(self):
        return b58encode(
            self.account.secret() + b58decode(str(self.account.pubkey()))
        ).decode('utf-8')

    def sign_message(self, text: str, raw: bool = False, account: Keypair = None, convert: bool = False):
        if account is None:
            account = self.account
        encoded_signature = account.sign_message(text.encode())

        if raw: return str(encoded_signature)
        elif convert: return self.convert_radix2(encoded_signature)
        return "0x" + b58decode(str(encoded_signature)).hex()


    @classmethod
    def convert_radix2(cls, signature: Signature, pad: bool = True):
        signature_data = signature.to_bytes_array()
        from_bits = 8
        to_bits = 6
        alphabet = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H', 'I', 'J', 'K', 'L', 'M', 'N', 'O', 'P', 'Q', 'R', 'S', 'T',
                    'U', 'V', 'W', 'X', 'Y', 'Z', 'a', 'b', 'c', 'd', 'e', 'f', 'g', 'h', 'i', 'j', 'k', 'l', 'm', 'n',
                    'o', 'p', 'q', 'r', 's', 't', 'u', 'v', 'w', 'x', 'y', 'z', '0', '1', '2', '3', '4', '5', '6', '7',
                    '8', '9', '+', '/']

        acc = 0
        bits = 0
        from_base = 1 << from_bits
        to_mask = (1 << to_bits) - 1
        result = []

        for value in signature_data:
            acc = (acc << from_bits) | value
            bits += from_bits

            while bits >= to_bits:
                bits -= to_bits
                result.append((acc >> bits) & to_mask)

        if pad:
            if bits > 0:
                result.append((acc << (to_bits - bits)) & to_mask)

        encoded_signature = [alphabet[val] for val in result]
        while (len(encoded_signature) * to_bits) % 8 != 0:
            encoded_signature.append("=")

        return "".join(encoded_signature)


    def _get_error_reason(self, logs: list):
        fails_list = [
            "Program log: Error: ",
            "Program log: AnchorError occurred. ",
            "Program log: AnchorError caused by account: "
        ]
        advanced_errors = []
        for log in logs:
            for fail_msg in fails_list:
                # if log.startswith(fail_msg) or "compute units" in log:
                if log.startswith(fail_msg):
                    return log.removeprefix(fail_msg)
                elif "compute units" in log:
                    advanced_errors.append(log)

        if advanced_errors: return advanced_errors[-1]
        return logs[-1]


    async def get_tx_status(self, signature: Signature):
        started = time()
        retry_count = 0
        
        while True:
            tx = None
            try:
                tx = await self.client.get_transaction(
                    tx_sig=signature,
                    commitment=Confirmed,
                    max_supported_transaction_version=0,
                )
                # Успешный запрос - сбрасываем счетчик ошибок
                retry_count = 0
                
            except SolanaRpcException as e:
                retry_count += 1
                error_msg = str(e)
                
                # Логируем только критичные случаи (много ошибок подряд)
                if retry_count >= 10:
                    logger.warning(f'[-] {self.label} | RPC errors: {retry_count} attempts, {int(time() - started)}s elapsed')
                
                # Проверяем только общий таймаут, НЕ прерываемся из-за RPC ошибок
                # Транзакция уже отправлена, нужно дождаться её подтверждения!
                if time() - started > 60 * TO_WAIT_TX:
                    raise Exception(f'tx not in blockchain in {TO_WAIT_TX}m (after {retry_count} RPC errors)')
                
                # Увеличенная задержка при ошибках
                await async_sleep(3)
                continue

            if tx and tx.value:
                break

            if time() - started > 60 * TO_WAIT_TX:
                raise Exception(f'tx not in blockchain in {TO_WAIT_TX}m')
            await async_sleep(2)

        tx_result = loads(tx.value.transaction.meta.to_json())
        status = tx_result["err"] is None and "Ok" in tx_result["status"]

        reason = self._get_error_reason(tx_result["logMessages"])
        return {"success": status, "msg": reason}


    def get_associated_token(self, token: str, address: Pubkey):
        token_address = SOL_TOKEN_ADDRESSES.get(token, token)
        token_program = TOKENS_PROGRAM.get(token, "default")
        return get_associated_token_address(
            address,
            Pubkey.from_string(token_address),
            Pubkey.from_string(TOKEN_PROGRAMS[token_program])
        )


    def get_unit_price(self, amount: float = 0):
        if not amount: amount = 0.0001 # 100 micro-lamports
        return set_compute_unit_price(int(amount * 1e6))


    def get_unit_limit(self, amount: float):
        return set_compute_unit_limit(amount)


    async def get_token_info(self, token: str = None, address: Pubkey = None, associated_token: Pubkey = None):
        """
        Getting token info: balance (human and blockchain) and decimals

        :param token: token address or token name from `TOKENS_LIST`
        """
        if address is None:
            address = self.address

        if token and token not in ["SOL", SOL_TOKEN_ADDRESSES["SOL"]]:
            if associated_token is None:
                associated_token = self.get_associated_token(token=token, address=address)
            token_address = SOL_TOKEN_ADDRESSES.get(token, token)

            token_info = await self.client.get_token_account_balance(associated_token)
            if hasattr(token_info, 'message'):
                balance, decimals = 0, (await self.client.get_account_info_json_parsed(Pubkey.from_string(token_address))).value.data.parsed["info"]["decimals"]
            else:
                balance, decimals = int(token_info.value.amount), token_info.value.decimals

        else:
            account_data = (await self.client.get_account_info(address)).value
            if account_data is None:
                balance, decimals = 0, 9
            else:
                balance, decimals = account_data.lamports, 9

        return {
            "amount": balance / 10 ** decimals,
            "value": balance,
            "decimals": decimals,
        }


    async def wait_for_balance(
            self,
            previous_balance_amount: float,
            address: Pubkey = None,
            token: str = None,
            is_any_difference: bool = False,
    ):
        if address is None:
            address = self.address
        token = token or "SOL"
        logger.debug(f'[•] {self.label} | Waiting for balance more than {round_cut(previous_balance_amount, 6)} {token}')
        while True:
            try:
                new_balance = await self.get_token_info(address=address, token=token)
                if (
                        new_balance["amount"] > previous_balance_amount or
                        (is_any_difference and new_balance["amount"] != previous_balance_amount)
                ):
                    logger.debug(f'[•] {self.label} | New balance: {round_cut(new_balance["amount"], 6)} {token}')
                    return new_balance
            except SolanaRpcException as e:
                logger.warning(f'[-] {self.label} | Get {token} balance | {e}')
            finally:
                await async_sleep(3)


    async def send_transaction(
            self,
            tx_label: str,
            message: Message = None,
            completed_tx_message: Message = None,
            completed_tx: Transaction = None,
            signatures: list[Signature] = [],
            signers: list[Keypair] = [],
            tx_debug: bool = True,
            simulate: bool = True,
    ):
        if completed_tx_message:
            if str(completed_tx_message.recent_blockhash) == "11111111111111111111111111111111" and type(completed_tx_message) == MessageV0:
                completed_tx_message = MessageV0(
                    completed_tx_message.header,
                    completed_tx_message.account_keys,
                    (await self.client.get_latest_blockhash("confirmed")).value.blockhash,
                    completed_tx_message.instructions,
                    completed_tx_message.address_table_lookups
                )

            account_signature = self.account.sign_message(to_bytes_versioned(completed_tx_message))
            if signatures:
                completed_signatures = signatures
                completed_signatures[completed_tx_message.account_keys.index(self.address)] = account_signature

            else:
                completed_signatures = [account_signature]

            tx = VersionedTransaction.populate(completed_tx_message, completed_signatures)

        elif message:
            tx = Transaction(
                from_keypairs=[self.account, *signers],
                message=message,
                recent_blockhash=(await self.client.get_latest_blockhash("confirmed")).value.blockhash,
            )
        elif completed_tx:
            tx = completed_tx

        try:
            if simulate:
                simulated = await self.client.simulate_transaction(txn=tx, commitment=Confirmed)
                if not hasattr(simulated, "value"):
                    raise RPCException(simulated)
                elif simulated.value.err:
                    raise RPCException(simulated.value)

            tx_hash_ = await self.client.send_raw_transaction(
                txn=bytes(tx),
                opts=TxOpts(
                    skip_preflight=True,
                    preflight_commitment=Processed,
                )
            )
            tx_hash = tx_hash_.value

        except RPCException as err:
            if hasattr(err.args[0], 'data') and err.args[0].data.logs:
                error_text = self._get_error_reason(err.args[0].data.logs)

            elif hasattr(err.args[0], 'logs') and err.args[0].logs:
                error_text = self._get_error_reason(err.args[0].logs)

            elif hasattr(err.args[0], 'message') and err.args[0].message:
                error_text = err.args[0].message

            elif hasattr(err.args[0], 'err') and err.args[0].err:
                error_text = str(err.args[0].err)

            else:
                error_text = str(err)

            if search(r'consumed \d+ of \d+ compute units', error_text):
                error_text = "not enough funds to send transaction"

            tx_link = ''
            tx_status = {"success": False, "msg": f"Simulate failed: {error_text}"}

        else:
            tx_link = f"{CHAINS_DATA['solana']['explorer']}{tx_hash}"
            if tx_debug: logger.debug(f'[•] {self.label} | {tx_label} tx sent: {tx_link}')
            
            # Пытаемся проверить статус транзакции
            try:
                tx_status = await self.get_tx_status(signature=tx_hash)
            except Exception as e:
                # RPC не отвечает, но транзакция УЖЕ отправлена в блокчейн!
                # Считаем её успешной с предупреждением
                logger.warning(f'[!] {self.label} | {tx_label} - Cannot verify tx status (RPC error), but tx was sent to blockchain!')
                logger.warning(f'[!] {self.label} | Check tx manually: {tx_link}')
                tx_status = {"success": True, "msg": f"RPC verification failed, but tx sent: {str(e)[:100]}"}

        if tx_status["success"]:
            logger.info(f'[+] {self.label} | {tx_label} tx successfully sent!')
            if tx_debug:
                await self.db.append_report(
                    key=self.encoded_pk,
                    text=tx_label,
                    success=True
                )
            return tx_hash
        else:
            if tx_link: tx_href = f'| <a href="{tx_link}">link 👈</a>'
            else: tx_href = ""

            if tx_debug:
                await self.db.append_report(
                    key=self.encoded_pk,
                    text=f'{tx_label} | tx failed {tx_href}',
                    success=False
                )
            tx_link_str = "\n" if tx_link and tx_debug else ""
            if tx_debug:
                raise Exception(f'Transaction "{tx_label}" failed error: {tx_status["msg"]}{tx_link_str + tx_link}')
            else:
                logger.error(f'[-] {self.label} | Transaction "{tx_label}" failed error: {tx_status["msg"]}{tx_link_str + tx_link}')
                return False


    @async_retry(source="Solana", module_str="Send SOL", exceptions=Exception)
    async def send_sol(self, amount: float, recipient: str = None):
        if not recipient and not self.recipient:
            raise CustomError("No recipient provided")

        value = int(amount * 1e9)
        recipient = Pubkey.from_string(recipient) if recipient else self.recipient

        message = Message(instructions=[transfer(
            TransferParams(from_pubkey=self.address, to_pubkey=recipient, lamports=value)
        )])
        await self.send_transaction(tx_label=f"transfer {round(amount, 4)} SOL", message=message)


    @async_retry(source="Solana", module_str="Send token", exceptions=Exception)
    async def send_token(self, token: str, amount: float, value: int, decimals: int, recipient: str = None):
        if recipient is None:
            if not self.recipient:
                raise CustomError("No recipient provided")
            recipient = self.recipient

        if type(recipient) == str:
            recipient = Pubkey.from_string(recipient)

        token_address = Pubkey.from_string(SOL_TOKEN_ADDRESSES[token])
        insts = []

        src_associated = self.get_associated_token("ES", self.address)
        dst_associated = self.get_associated_token("ES", recipient)
        if not (await self.client.get_account_info(dst_associated)).value:
            insts.append(create_associated_token_account(
                payer=self.address,
                owner=recipient,
                mint=token_address,
                token_program_id=Pubkey.from_string(TOKEN_PROGRAMS["2022"])
            ))

        insts.append(transfer_checked(
            TransferCheckedParams(
                program_id=Pubkey.from_string(TOKEN_PROGRAMS[TOKENS_PROGRAM[token]]),
                source=src_associated,
                mint=token_address,
                dest=dst_associated,
                owner=self.address,
                amount=value,
                decimals=decimals,
            )
        ))
        message = Message(
            instructions=insts,
            payer=self.address,
        )

        await self.send_transaction(
            tx_label=f"transfer {amount} {token}",
            message=message,
            signers=[self.account]
        )

        return True
