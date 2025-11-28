from loguru import logger

from .sol_wallet import SolWallet
from .privy import Privy
from settings import TRADING_ASSET


class Ranger:
    def __init__(
            self,
            sol_wallet: SolWallet,
    ):
        self.sol_wallet = sol_wallet
        self.browser = sol_wallet.browser
        self.db = sol_wallet.db

        self.prefix = ""
        self.mode: int = 0


    async def start(self, mode: int):
        self.mode = mode
        await self.privy_login()

        if mode == 2:
            return await self.averaging_strategy()

        return True


    async def privy_login(self):
        privy_data = await Privy(
            sol_wallet=self.sol_wallet,
            url="www.app.ranger.finance",
            headers={
                "Privy-App-Id": "cmeiaw35f00dzjl0bztzhen22",
                "Privy-Client": "react-auth:2.21.1",
            },
            privy_url="auth.privy.io"
        ).login(embedded_sol_wallet=True, embedded_eth_wallet=True)

        await self.browser.fetch_ranger_cookies()
        privy_cookies = {
            "privy-session": "t",
            "privy-token": privy_data["tokens"]["raw"],
        }

        await self.browser.initialize_ranger_account(
            user_id=privy_data["user_id"],
            privy_cookies=privy_cookies,
        )

        approve_quote = await self.browser.get_approve_builder_fee_quote(privy_eth_address=privy_data["embedded_eth_address"])
        await self.browser.approve_builder_fee(
            quote=approve_quote,
            privy_cookies=privy_cookies,
        )

        ref_signature = self.sol_wallet.sign_message(
            text="Sign this message to verify your ownership of this wallet and accept the referral.",
            raw=True,
        )
        await self.browser.use_ref_code(
            signature=ref_signature,
            user_id=privy_data["user_id"],
            privy_cookies=privy_cookies,
        )


    async def averaging_strategy(self):
        """
        Запускает стратегию усреднения/пирамидинга
        """
        from .spot_client import SpotClient
        from .averaging_strategy import trade_averaging_strategy
        
        # Создаем SpotClient с активом из настроек
        spot_client = SpotClient(
            sol_wallet=self.sol_wallet,
            browser=self.browser,
            db=self.db,
            token_name=TRADING_ASSET
        )
        
        # Запускаем стратегию
        return await trade_averaging_strategy(
            client=spot_client,
            token_name=TRADING_ASSET
        )


    def log_message(
            self,
            text: str,
            smile: str = "•",
            level: str = "DEBUG",
            colors: bool = True
    ):
        label = f"<white>{self.sol_wallet.label}</white>" if colors else self.sol_wallet.label
        logger.opt(colors=colors).log(level.upper(), f'[{smile}] {label} | {text}')
