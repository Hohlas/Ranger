from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.backends import default_backend
from datetime import datetime, timezone
from base64 import b64decode, b64encode
from solders.keypair import Keypair
from curl_cffi import AsyncSession
from hashlib import sha256
from loguru import logger
from uuid import uuid4
from os import urandom
import asyncio

from .sol_wallet import SolWallet


PRIVY_SITE_TEXT: str = "{} wants you to sign in with your Solana account:\n" \
                       "{}\n\n" \
                       "By signing, you are proving you own this wallet and logging in. This does not initiate a transaction or cost any fees.\n\n" \
                       "URI: https://{}\n" \
                       "Version: 1\n" \
                       "Chain ID: {}\n" \
                       "Nonce: {}\n" \
                       "Issued At: {}\n" \
                       "Resources:\n- https://privy.io"
PRIVY_OG_TEXT: str = "{} wants you to sign in with your Solana account:\n" \
                     "{}\n\n" \
                     "You are proving you own {}.\n\n" \
                     "URI: https://{}\n" \
                     "Version: 1\n" \
                     "Chain ID: {}\n" \
                     "Nonce: {}\n" \
                     "Issued At: {}\n" \
                     "Resources:\n- https://privy.io"


class Privy:

    def __init__(
            self,
            sol_wallet: SolWallet,
            url: str,
            headers: dict,
            privy_url: str = "auth.privy.io",
            chain_id: str | int = 1
    ):
        self.sol_wallet = sol_wallet
        self.session = AsyncSession(
            impersonate="chrome136",
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
            },
            proxy=sol_wallet.browser.proxy
        )
        self.url = url
        self.privy_url = privy_url
        self.chain_id = str(chain_id)

        self.headers = {
            "Privy-Ca-Id": str(uuid4()),
            "Origin": f"https://{self.url}",
            "Referer": f"https://{self.url}/",
            **headers,
        }


    async def login(
            self,
            embedded_sol_wallet: bool = False,
            embedded_eth_wallet: bool = False,
            accept_rules: bool = False,
            captcha_type: str | None = None,
    ):
        tokens_resp = await self.privy_connect_wallet(
            account=self.sol_wallet.account,
            main_account=True,
            captcha_type=captcha_type
        )

        result = {
            "tokens": {
                "privy_access_token": tokens_resp["privy_access_token"],
                "refresh_token": tokens_resp["refresh_token"],
                "identity_token": tokens_resp.get("identity_token"),
                "token": self.headers["Authorization"],
                "raw": tokens_resp["token"],
            },
            "user_id": tokens_resp["user"]["id"],
        }

        if accept_rules and tokens_resp["user"].get("has_accepted_terms") is False:
            await self.privy_accept_terms(
                headers={
                    "Authorization": "Bearer " + (tokens_resp.get("privy_access_token") or
                                                  tokens_resp.get("token"))
                }
            )

        if embedded_eth_wallet:
            embedded_eth_address = self._get_embedded_wallet(tokens_resp["user"]["linked_accounts"], "ethereum")
            if embedded_eth_address is None:
                embedded_eth_address = await self.privy_create_wallet(chain_type="ethereum")
                tokens_resp = await self.privy_update_session(
                    refresh_token=tokens_resp["refresh_token"],
                    headers={"Authorization": "Bearer " + tokens_resp["privy_access_token"]}
                )

            result["embedded_eth_address"] = embedded_eth_address

        if embedded_sol_wallet:
            embedded_sol_address = self._get_embedded_wallet(tokens_resp["user"]["linked_accounts"], "solana")
            if embedded_sol_address is None:
                embedded_sol_address = await self.privy_create_wallet(chain_type="solana")
                tokens_resp = await self.privy_update_session(
                    refresh_token=tokens_resp["refresh_token"],
                    headers={"Authorization": "Bearer " + tokens_resp["privy_access_token"]}
                )

            result["embedded_sol_account"] = embedded_sol_address

        return result


    async def privy_connect_wallet(self, account: Keypair, main_account: bool, captcha_type: str | None = None):
        sign_nonce = await self.privy_init(
            address=str(account.pubkey()),
            captcha_type=captcha_type,
            chain_type=None,
            headers=self.headers,
        )
        issued_at = datetime.now(tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + "Z"

        sign_text = PRIVY_OG_TEXT.format(
            self.url,
            str(account.pubkey()),
            str(account.pubkey()),
            self.url,
            "mainnet",
            sign_nonce,
            issued_at,
        )
        signature = self.sol_wallet.sign_message(sign_text, account=account, convert=True)

        if main_account:
            response = await self.privy_auth(
                sign_text=sign_text,
                signature=signature,
                headers=self.headers
            )
            self.headers["Authorization"] = "Bearer " + response["token"]
            return response

        else:
            return {
                "sign_text": sign_text,
                "signature": signature,
            }


    def _get_embedded_wallet(self, linked_accounts: list, chain_type: str):
        return next((
            account["address"] for account in linked_accounts
            if (
                account.get("recovery_method") in ["privy", "privy-v2"] and
                account["connector_type"] == "embedded") and
                account.get("chain_type") == chain_type
        ), None)


    async def privy_init(
            self,
            address: str,
            captcha_type: str | None,
            chain_type: str | None,
            headers: dict,
    ):
        payload = {"address": address}
        # if captcha_type:
        #     payload["token"] = CaptchaSolver(self.proxy).solve(captcha_type)
        if chain_type:
            payload["chain_type"] = chain_type

        r = await self.session.request(
            method="POST",
            url=f"https://{self.privy_url}/api/v1/siws/init",
            json=payload,
            headers=headers,
        )
        resp = r.json()
        if not resp.get("nonce"):
            if resp.get("error", {}).get("message") == 'Too Many Requests':
                logger.warning(f'[-] {address} | Privy Init | Too many requests...')
                await asyncio.sleep(10)
                return await self.privy_init(address, captcha_type, chain_type, headers)

            raise Exception(f'Get Privy nonce unexpected response: {resp}')

        return resp["nonce"]


    async def privy_auth(self, sign_text: str, signature: str, headers: dict):
        payload = {
            "message": sign_text,
            "signature": signature,
            "walletClientType": "backpack",
            "connectorType": "solana_adapter",
            "mode": "login-or-sign-up",
            "message_type": "plain"
        }
        r = await self.session.request(
            method="POST",
            url=f"https://{self.privy_url}/api/v1/siws/authenticate",
            json=payload,
            headers=headers,
        )
        resp = r.json()
        if not resp.get("user"):
            if resp.get("error", {}).get("message") == 'Too Many Requests':
                logger.warning(f'[-] {self.sol_wallet.address} | Privy Auth | Too many requests...')
                await asyncio.sleep(10)
                return await self.privy_auth(sign_text, signature, headers)
            raise Exception(f'Privy Auth unexpected response: {resp}')

        return resp

    async def privy_accept_terms(self, headers: dict):
        r = await self.session.request(
            method="POST",
            url=f"https://{self.privy_url}/api/v1/users/me/accept_terms",
            json={},
            headers={**self.headers, **headers},
        )
        resp = r.json()
        if resp.get("has_accepted_terms") is not True:
            if resp.get("error", {}).get("message") == 'Too Many Requests':
                logger.warning(f'[-] {self.sol_wallet.address} | Privy Terms | Too many requests...')
                await asyncio.sleep(10)
                return await self.privy_accept_terms(headers)

            raise Exception(f'Privy Accept terms unexpected response: {resp}')

        return resp

    async def privy_embed_wallet(
            self,
            address: str,
            sign_text: str,
            signature: str,
            device_id: str,
            device_auth_share: str,
            recovery_auth_share: str,
            encrypted_recovery_share: str,
            encrypted_recovery_share_iv: str,
            recovery_key_hash: str,
            recovery_key: str,
            headers: dict,
    ):
        payload = {
            "entropy_key": address,
            "entropy_key_verifier": "ethereum-address-verifier",
            "chain_type": "ethereum",
            "message": sign_text,
            "signature": signature,
            "device_id": device_id,
            "device_auth_share": device_auth_share,
            "recovery_auth_share": recovery_auth_share,
            "encrypted_recovery_share": encrypted_recovery_share,
            "encrypted_recovery_share_iv": encrypted_recovery_share_iv,
            "recovery_type": "privy_generated_recovery_key",
            "recovery_key_hash": recovery_key_hash,
            "imported": False,
            "recovery_key": recovery_key
        }
        r = await self.session.request(
            method="POST",
            url=f"https://{self.privy_url}/api/v1/embedded_wallets",
            json=payload,
            headers={
                **headers,
                "Origin": "https://auth.privy.io",
                "Referer": "https://auth.privy.io/",
            },
        )
        resp = r.json()
        if not resp.get("linked_accounts"):
            raise Exception(f'Privy embed wallet unexpected response: {resp}')

        return resp

    async def privy_create_wallet(
            self,
            chain_type: str,
    ):
        r = await self.session.request(
            method="POST",
            url=f"https://{self.privy_url}/api/v1/wallets",
            json={"chain_type": chain_type},
            headers=self.headers,
        )
        resp = r.json()
        if not resp.get("address"):
            raise Exception(f'Privy create wallet unexpected response: {resp}')

        return resp["address"]


    async def privy_update_session(self, refresh_token: str, headers: dict):
        r = await self.session.request(
            method="POST",
            url=f"https://{self.privy_url}/api/v1/sessions",
            json={"refresh_token": refresh_token},
            headers={**self.headers, **headers},
        )
        resp = r.json()
        if not resp.get("user"):
            raise Exception(f'Privy Update Session unexpected response: {resp}')

        return resp


    async def privy_get_key_material(self, embedded_address: str, headers: dict):
        r = await self.session.request(
            method="POST",
            url=f"https://{self.privy_url}/api/v1/embedded_wallets/{embedded_address}/recovery/key_material",
            json={"chain_type": "ethereum"},
            headers={
                **headers,
                "Origin": "https://auth.privy.io",
                "Referer": "https://auth.privy.io/",
            },
        )
        resp = r.json()
        if not resp.get("recovery_key") or not resp.get("recovery_type"):
            raise Exception(f'Privy Get material unexpected response: {resp}')

        return resp

    async def privy_get_auth_share(self, embedded_address: str, headers: dict):
        r = await self.session.request(
            method="POST",
            url=f"https://{self.privy_url}/api/v1/embedded_wallets/{embedded_address}/recovery/auth_share",
            json={"chain_type": "ethereum"},
            headers={
                **headers,
                "Origin": "https://auth.privy.io",
                "Referer": "https://auth.privy.io/",
            },
        )
        resp = r.json()
        if not resp.get("share"):
            raise Exception(f'Privy Get auth share unexpected response: {resp}')

        return resp["share"]

    async def privy_get_shares(self, embedded_address: str, recovery_key_hash: str, headers: dict):
        r = await self.session.request(
            method="POST",
            url=f"https://{self.privy_url}/api/v1/embedded_wallets/{embedded_address}/recovery/shares",
            json={
                "recovery_key_hash": recovery_key_hash,
                "chain_type": "ethereum"
            },
            headers={
                **headers,
                "Origin": "https://auth.privy.io",
                "Referer": "https://auth.privy.io/",
            },
        )
        resp = r.json()
        if (
                not resp.get("encrypted_recovery_share") or
                not resp.get("encrypted_recovery_share_iv") or
                "imported" not in resp
        ):
            raise Exception(f'Privy Get shares unexpected response: {resp}')

        return resp

    async def privy_recovery_device(self, embedded_address: str, device_id: str, device_auth_share: str, headers: dict):
        r = await self.session.request(
            method="POST",
            url=f"https://{self.privy_url}/api/v1/embedded_wallets/{embedded_address}/recovery/device",
            json={
                "device_id": device_id,
                "device_auth_share": device_auth_share,
                "chain_type": "ethereum"
            },
            headers={
                **headers,
                "Origin": "https://auth.privy.io",
                "Referer": "https://auth.privy.io/",
            },
        )
        resp = r.json()
        if resp.get("success") is not True:
            raise Exception(f'Privy Get recovery device unexpected response: {resp}')


n: list = list(bytes.fromhex(
    "00ffc8089110d0365a3ed8439977fe1823200770a16c0c7f628b4046c74be00eeb16e8adcfcd39536a273593d44e48c32b79542809780f"
    "219087142aa99cd674b47cdeedb18676a498e2968f02321cc133eeef81fd305c139d2917c411448c80f373421e1db5f012d15b41a2d72c"
    "e9d559cb50a8dcfcf25672a6652f9f9b3dba7dc24582a757b6a37a754fae3f376d4761beabd35fb058afca5efa85e44d8a05fb60b77bb8"
    "264a67c61af86925b3dbbd66ddf1d2df038d34d9920d6355aa49ecbc953c840bf5e6e7e5ac7e6eb9f9da8e9ac924e10a156b3aa051f4ea"
    "b2979e5d228894ce1901714ca5e3c531bbcc1f2d3b526ff62e89f7c0681b640406bf8338"
))
a: list = list(bytes.fromhex(
    "01e54cb5fb9ffc120334d4c416ba1f36055c67573ad5215a0fe4a9f94e6463ee1137e010d2aca52933593b306deff47b55eb4d50b72a07"
    "8dff26d7f0c27e098c1a6a620b5d821b8f2ebea61de79d2d8a72d9f12732bc77859670086956df9994a19018bbfa7ab0a7f8ab28d6158e"
    "cbf213e678613f89460d353188a34180ca175f5383fec39b4539e1f59e195eb6cf4b3804b92be2c14add480cd07d3d58de7cd8146b8747"
    "e87984733cbd92c9238b979544dcad406586a2a4cc7fecc0af91fdf74f812f5beaa81c02d19871ed25e3240668b3932c6f3e6c0ab8ceae"
    "74b142b41ed349e99cc8c6c7226edb20bf43515266b27660dac5f3f6aacd9aa075540e01"
))


def s(e, t):
    return 0 if e == 0 or t == 0 else a[(n[e] + n[t]) % 255]


def shamir_split(e: bytes, t: int = 2, r: int = 2):
    nn = []
    aa = len(e)
    h = [tt + 1 for tt in range(255)]
    tt = list(urandom(255))
    for rr in range(255):
        i = tt[rr] % 255
        h[rr], h[i] = h[i], h[rr]
    for ee in range(t):
        tt = [0] * (aa + 1)
        tt[aa] = h[ee]
        nn.append(tt)
    c = r - 1
    for rr in range(aa):
        aaa = [0] * (c + 1)
        aaa[0] = e[rr]
        for ee in range(1, c + 1):
            if ee == c:
                while True:
                    eee = urandom(1)[0]
                    if eee > 0:
                        aaa[ee] = eee
                        break
            else:
                aaa[ee] = urandom(1)[0]
        for ee in range(t):
            if h[ee] == 0:
                tt = aaa[0]
            else:
                tt = aaa[c]
                for nnn in range(c - 1, -1, -1):
                    tt = s(tt, h[ee]) ^ aaa[nnn]
            nn[ee][rr] = tt
    return [bytes(v) for v in nn]


def encrypt_share(data: bytes, key: bytes):
    iv = urandom(12)
    cipher = Cipher(algorithms.AES(key), modes.GCM(iv), backend=default_backend())
    e = cipher.encryptor()
    encrypted_data = e.update(data) + e.finalize()
    tag = e.tag
    enc_data = b64encode(encrypted_data + tag).decode('utf-8')
    enc_iv = b64encode(iv).decode('utf-8')
    return enc_data, enc_iv


def get_key_hash(b64_key: str) :
    key = b64decode(b64_key)
    if len(key) not in [16, 24, 32]:
        raise ValueError("Invalid key size for AES-GCM")
    aes = algorithms.AES(key)
    digest = sha256(aes.key).digest()
    return b64encode(digest).decode('utf-8')


def decrypt_share(b64_enc_data: str, b64_enc_iv: str, b64_key: str) -> bytes:
    key = b64decode(b64_key)
    enc_iv = b64decode(b64_enc_iv)
    enc_data = b64decode(b64_enc_data)
    data = enc_data[:-16]
    tag = enc_data[-16:]
    cipher = Cipher(algorithms.AES(key), modes.GCM(enc_iv, tag), backend=default_backend())
    d = cipher.decryptor()
    decrypted_data = d.update(data) + d.finalize()
    return decrypted_data


def shamir_combine(e: list[bytes]) -> bytes:
    if not isinstance(e, list):
        raise ValueError("shares must be an Array")
    if not (2 <= len(e) <= 255):
        raise ValueError("shares must have at least 2 and at most 255 elements")
    t = e[0]
    for share in e:
        if not isinstance(share, (bytes, bytearray)):
            raise ValueError("each share must be a Uint8Array")
        if len(share) < 2:
            raise ValueError("each share must be at least 2 bytes")
        if len(share) != len(t):
            raise ValueError("all shares must have the same byte length")
    r = len(e)
    i = len(t)
    h = i - 1
    d = set()
    ll = [0] * r
    for tt in range(r):
        rr = e[tt][i - 1]
        if rr in d:
            raise ValueError("shares must contain unique values but a duplicate was found")
        d.add(rr)
        ll[tt] = rr
    c = [0] * h
    for tt in range(h):
        f = [e[ii][tt] for ii in range(r)]
        if len(ll) != len(f):
            raise ValueError("sample length mistmatch")
        ii, result = len(ll), 0
        for rr in range(ii):
            o = 1
            for ttt in range(ii):
                if rr != ttt:
                    x, y = 0 ^ ll[ttt], ll[rr] ^ ll[ttt]
                    if y == 0:
                        raise ValueError("cannot divide by zero")
                    rrr = a[(n[x] - n[y] + 255) % 255]
                    inter = 0 if x == 0 else rrr
                    o = s(o, inter)
            result = result ^ s(f[rr], o)
        c[tt] = result
    return bytes(c)
