from aiohttp import ClientSession
from loguru import logger
from json import dumps

from modules.retry import async_retry, have_json
from modules.database import DataBase
from .config import SOL_TOKEN_ADDRESSES


class Browser:
    def __init__(self, db: DataBase, proxy: str, sol_address: str):
        self.db = db
        self.sol_address = sol_address

        # Список шаблонов-подсказок и невалидных значений прокси
        invalid_proxies = [
            'https://log:pass@ip:port', 
            'http://log:pass@ip:port', 
            'log:pass@ip:port',
            'log:pass@ip:port1',  # Вариант с цифрой
            'http://log:pass@ip:port1',
            'https://log:pass@ip:port1',
            '', 
            '\n', 
            None
        ]
        
        # Также проверяем что прокси содержит валидный порт (не содержит 'port' как текст)
        is_template = proxy in invalid_proxies or (proxy and 'port' in proxy and not proxy.split(':')[-1].isdigit())
        
        if not is_template and proxy:
            self.proxy = "http://" + proxy.removeprefix("https://").removeprefix("http://")
            logger.opt(colors=True).debug(f'[•] Soft | <white>{sol_address}</white> | Got proxy <white>{self.proxy}</white>')
        else:
            self.proxy = None
            if proxy and proxy not in ['', '\n', None]:
                logger.opt(colors=True).warning(f'[•] Soft | <white>{sol_address}</white> | Invalid proxy template detected, working without proxy')
            else:
                logger.opt(colors=True).warning(f'[•] Soft | <white>{sol_address}</white> | You dont use proxies')

        self.session = self.get_new_session()
        self.session.headers.update({
            "Origin" : "https://www.app.ranger.finance",
            "Referer": "https://www.app.ranger.finance/",
        })


    def get_new_session(self):
        session = ClientSession(
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15"
            },
        )
        if self.proxy:
            session.proxy = self.proxy
        return session

    @have_json
    async def send_request(self, **kwargs):
        timed_session = False

        if kwargs.get("new_session") is True:
            session = self.get_new_session()
            timed_session = True
            del kwargs["new_session"]

        elif kwargs.get("session") is not None:
            session = kwargs["session"]
            del kwargs["session"]

        else: session = self.session

        if kwargs.get("params") and list(kwargs.get("params").keys()) == ["input"]:
            kwargs["params"]["input"] = dumps(kwargs["params"]["input"]).replace(' ', '')
        kwargs["method"] = kwargs["method"].upper()

        if self.proxy:
            kwargs["proxy"] = self.proxy

        try:
            return await session.request(**kwargs)
        finally:
            if timed_session:
                await session.close()


    @async_retry(source="Browser")
    async def get_market_order_quote(self, from_token: str, to_token: str, value: int):
        r = await self.send_request(
            method="GET",
            url="https://staging-spot-api-437363704888.asia-northeast1.run.app/api/v2/market/quote",
            params={
                "user_wallet_address": str(self.sol_address),
                "slippage_bps": 100,
                "input_mint": SOL_TOKEN_ADDRESSES[from_token],
                "output_mint": SOL_TOKEN_ADDRESSES[to_token],
                "input_amount": value,
            }
        )
        response = await r.json()
        if response.get('quotes') is None:
            if response.get('message') == 'Not Found':
                raise Exception(f'Not found quotes for swap')
            raise Exception(f'bad response: {response}')

        return response["quotes"]

    @async_retry(source="Browser")
    async def get_limit_order_quote(self, from_token: str, to_token: str, value: int, limit_price: float):
        """
        Получает котировку для лимитного ордера (prepare transaction)
        
        Args:
            from_token: Токен который продаем
            to_token: Токен который покупаем
            value: Количество from_token в минимальных единицах (input_token_amount)
            limit_price: Лимитная цена (цена to_token за 1 from_token)
        
        Returns:
            Котировка для лимитного ордера с транзакцией для подписи
        """
        # Рассчитываем output_token_amount из limit_price
        # Например: продаем 0.0001 WBTC @ $120,000 = получим $12 USDC
        # input: 0.0001 WBTC = 10000 в минимальных единицах (8 decimals)
        # output: $12 USDC = 12000000 в минимальных единицах (6 decimals)
        from .config import SOL_TOKEN_ADDRESSES
        
        # Получаем decimals для токенов
        input_decimals = 8 if from_token == "WBTC" else 6  # WBTC: 8, USDC: 6
        output_decimals = 6 if to_token == "USDC" else 8
        
        # Рассчитываем количество токена из value (input_token_amount)
        input_token_real = value / (10 ** input_decimals)
        
        # Рассчитываем ожидаемое количество USDC
        output_token_real = input_token_real * limit_price
        
        # Конвертируем в минимальные единицы
        output_token_amount = int(output_token_real * (10 ** output_decimals))
        
        # Подготовка тела запроса
        request_body = {
            "input_token_amount": value,
            "input_token_mint": SOL_TOKEN_ADDRESSES[from_token],
            "output_token_amount": output_token_amount,
            "output_token_mint": SOL_TOKEN_ADDRESSES[to_token],
            "user_wallet_address": str(self.sol_address)
        }
        
        # Используем production API endpoint (POST, не GET!)
        r = await self.send_request(
            method="POST",
            url="https://prod-spot-api-437363704888.asia-northeast1.run.app/api/v1/orders/limit",
            json=request_body
        )
        response = await r.json()
        
        # Ожидаем что API вернет transaction для подписи
        if not response.get('transaction'):
            raise Exception(f'No transaction in limit order quote response: {response}')
        
        return response

    @async_retry(source="Browser")
    async def register_limit_order(self, limit_order_account_address: str, user_signature: str):
        """
        Регистрирует лимитный ордер на бирже после подписания транзакции
        
        Args:
            limit_order_account_address: Адрес аккаунта лимитного ордера
            user_signature: Подпись пользователя (base58)
        
        Returns:
            Результат регистрации ордера
        """
        r = await self.send_request(
            method="POST",
            url="https://prod-spot-api-437363704888.asia-northeast1.run.app/api/v1/orders/limit/register",
            json={
                "limit_order_account_address": limit_order_account_address,
                "user_signature": user_signature
            }
        )
        response = await r.json()
        
        return response

    @async_retry(source="Browser")
    async def get_open_limit_orders(self):
        """
        Получает список открытых лимитных ордеров
        
        Returns:
            List[dict]: Список открытых лимитных ордеров
            [
                {
                    "order_id": "...",
                    "from_token": "WBTC",
                    "to_token": "USDC",
                    "from_amount": 0.000725,
                    "limit_price": 109846,
                    "status": "open",
                    "created_at": "2025-10-31T18:40:48Z"
                }
            ]
        """
        try:
            # Реальный endpoint из cURL пользователя
            r = await self.send_request(
                method="GET",
                url="https://prod-spot-api-437363704888.asia-northeast1.run.app/api/v1/orders/limit",
                params={
                    "user_wallet_address": str(self.sol_address),
                    "limit": 100  # Запрашиваем до 100 ордеров
                }
            )
            response = await r.json()
            
            if isinstance(response, list):
                return response
            elif response.get('orders'):
                orders = response['orders']
                return orders
            else:
                return []
                
        except Exception as e:
            logger.debug(f'Failed to get open limit orders: {e}')
            return []  # Возвращаем пустой список если endpoint не найден

    @async_retry(source="Browser")
    async def cancel_limit_order(self, order_id: str):
        """
        Отменяет лимитный ордер
        
        Args:
            order_id: ID ордера для отмены (limit_order_account_address)
        """
        try:
            r = await self.send_request(
                method="POST",
                url="https://prod-spot-api-437363704888.asia-northeast1.run.app/api/v1/orders/limit/cancel",
                json={
                    "limit_order_account_address": order_id,
                    "user_wallet_address": str(self.sol_address)
                }
            )
            response = await r.json()
            return response
            
        except Exception as e:
            logger.error(f'Failed to cancel limit order: {e}')
            raise


    @async_retry(source="Browser")
    async def fetch_ranger_cookies(self):
        r = await self.session.get("https://www.app.ranger.finance/perps", proxy=self.proxy)


    async def initialize_ranger_account(self, user_id: str, privy_cookies: dict):
        """
        Инициализирует аккаунт Ranger Finance
        """
        try:
            r = await self.send_request(
                method="POST",
                url="https://www.app.ranger.finance/api/referral/v2/initialize-ranger-account",
                json={"privy_id": user_id},
                cookies=privy_cookies,
            )
            
            # Читаем текст ответа
            text_response = await r.text()
            
            # Пытаемся распарсить как JSON
            try:
                import json
                response = json.loads(text_response)
                if response != {"is_success": True}:
                    raise Exception(f'Unexpected initialize ranger account response: {response}')
            except json.JSONDecodeError as e:
                logger.error(f"Initialize Ranger Account: Invalid JSON response (Status {r.status})")
                raise Exception(f"Initialize Ranger Account returned invalid JSON")
                
        except Exception as e:
            # Аккаунт уже инициализирован или другая ошибка - продолжаем работу
            logger.warning(f"⚠️ Initialize Ranger Account failed, skipping (account might be already initialized)")
            pass  # Не прерываем работу


    @async_retry(source="Browser")
    async def get_approve_builder_fee_quote(self, privy_eth_address: str):
        r = await self.send_request(
            method="POST",
            url="https://sor-evm-437363704888.asia-northeast1.run.app/api/v1/sor/hyperliquid/approve-builder-fee",
            json={"user_address": privy_eth_address},
        )
        response = await r.json()
        if response.get("execution_method") != "Hyperliquid" or response.get("hyperliquid_payload") is None:
            raise Exception(f'Unexpected get approve builder fee quote response: {response}')

        return response["hyperliquid_payload"]["place_order"]["action_payload"]


    @async_retry(source="Browser")
    async def approve_builder_fee(self, quote: dict, privy_cookies: dict):
        r = await self.send_request(
            method="POST",
            url="https://www.app.ranger.finance/api/hyperliquid/approve_builder_fee",
            json={"order": quote},
            headers={"Authorization": f"Bearer {privy_cookies['privy-token']}"},
            cookies=privy_cookies,
        )
        response = await r.json()
        if not response.get("message").startswith("Must deposit before performing actions. User: "):
            raise Exception(f'Unexpected approve builder fee response: {response}')

        return response


    async def use_ref_code(self, signature: str, user_id: str, privy_cookies: dict):
        """
        Применяет реферальный код для аккаунта
        """
        try:
            r = await self.send_request(
                method="POST",
                url="https://www.app.ranger.finance/api/referral/v2/post-referral",
                json={
                    "publicKey": self.sol_address,
                    "privy_id": user_id,
                    "code": "free",
                    "signature": signature
                },
                cookies=privy_cookies,
            )
            
            # Читаем текст ответа
            text_response = await r.text()
            
            # Пытаемся распарсить как JSON
            try:
                import json
                response = json.loads(text_response)
                if type(response) != list or (
                        response != [] and
                        any([a["referred_status"] != "Active" for a in response])
                ):
                    raise Exception(f'Use referral code unexpected response: {response}')
            except json.JSONDecodeError as e:
                logger.error(f"Use Ref Code: Invalid JSON response (Status {r.status})")
                raise Exception(f"Use Ref Code returned invalid JSON")
                
        except Exception as e:
            # Реферальный код уже применен или другая ошибка - продолжаем работу
            logger.warning(f"⚠️ Use Ref Code failed, skipping (code might be already applied)")
            pass  # Не прерываем работу


    async def get_token_price(self, token_symbol: str):
        """
        Получает текущую цену токена через Ranger Finance Pricing API
        Возвращает float цены для расчетов
        """
        try:
            # Используем Pricing API (быстро и просто)
            price = await self._get_price_from_ranger_pricing_api(token_symbol)
            
            if not price or price <= 0:
                raise Exception('Price is zero or invalid')
            
            # Не логируем цену каждый раз - только при событиях
            
            return float(price)
                
        except Exception as e:
            error_msg = f'Failed to get price for {token_symbol} from Ranger Finance API: {e}'
            logger.error(error_msg)
            raise Exception(error_msg)
    
    async def _get_price_from_ranger_pricing_api(self, token_symbol: str):
        """
        Получает цену через Ranger Finance Pricing API.
        Возвращает только среднюю цену (без BID/ASK, т.к. API их не предоставляет).
        """
        try:
            from .config import SOL_TOKEN_ADDRESSES
            
            token_address = SOL_TOKEN_ADDRESSES.get(token_symbol)
            if not token_address:
                raise Exception(f'Token {token_symbol} not found in SOL_TOKEN_ADDRESSES')
            
            r = await self.send_request(
                method="GET",
                url=f"https://prod-spot-pricing-api-437363704888.asia-northeast1.run.app/defi/multi_price",
                params={"list_address": token_address}
            )
            response = await r.json()
            
            if not response.get('success') or not response.get('data'):
                raise Exception('Invalid pricing API response')
            
            token_data = response['data'].get(token_address)
            if not token_data or not token_data.get('value'):
                raise Exception('Token price not found in response')
            
            price = float(token_data['value'])
            
            if price <= 0:
                raise Exception('Price is zero or negative')
            
            return price
            
        except Exception as e:
            logger.debug(f"Failed to get price from Pricing API: {e}")
            return None
    
    async def _get_price_from_ranger_quote(self, token_symbol: str):
        """
        Получает цену токена через Ranger Finance Quote API.
        Вычисляет среднюю цену (mid-price) между покупкой и продажей для точности.
        """
        try:
            from .config import SOL_TOKEN_ADDRESSES
            
            # Получаем decimals для токена
            token_decimals = 8 if token_symbol == "WBTC" else 6
            usdc_decimals = 6
            
            # 1. Получаем цену ПОКУПКИ (ASK): USDC -> Token
            test_amount_usdc = 1_000_000  # 1 USDC
            buy_quotes = await self.get_market_order_quote(
                from_token="USDC",
                to_token=token_symbol,
                value=test_amount_usdc
            )
            
            if not buy_quotes or len(buy_quotes) == 0:
                raise Exception('No buy quotes returned')
            
            # Количество токенов, которое получим за 1 USDC
            buy_output_raw = buy_quotes[0].get('output_token_info', {}).get('amount', 0)
            buy_output = buy_output_raw / (10 ** token_decimals)
            
            if buy_output <= 0:
                raise Exception('Buy output amount is zero')
            
            # Цена покупки (ASK) = сколько USDC платим за 1 токен
            ask_price = 1.0 / buy_output
            
            # 2. Получаем цену ПРОДАЖИ (BID): Token -> USDC
            # Используем фиксированное количество токенов для точности
            test_amount_token = int(0.00001 * (10 ** token_decimals))  # 0.00001 токена
            
            sell_quotes = await self.get_market_order_quote(
                from_token=token_symbol,
                to_token="USDC",
                value=test_amount_token
            )
            
            if not sell_quotes or len(sell_quotes) == 0:
                # Если не удалось получить котировку продажи, используем только покупку
                logger.debug(f"Could not get sell quote, using buy price: ${ask_price:.2f}")
                return {
                    'ask': ask_price,
                    'bid': ask_price * 0.999,  # Примерный BID (0.1% спред)
                    'mid': ask_price
                }
            
            # Получаем реальное количество токенов, которое мы запросили в sell quote
            sell_input_raw = sell_quotes[0].get('input_token_info', {}).get('amount', 0)
            sell_input_tokens = sell_input_raw / (10 ** token_decimals)
            
            # Количество USDC, которое получим за эти токены
            sell_output_raw = sell_quotes[0].get('output_token_info', {}).get('amount', 0)
            sell_output_usdc = sell_output_raw / (10 ** usdc_decimals)
            
            # Цена продажи (BID) = сколько USDC получим / сколько токенов продаем
            bid_price = sell_output_usdc / sell_input_tokens if sell_input_tokens > 0 else ask_price
            
            # 3. Средняя цена (Mid Price) - как на биржах
            mid_price = (ask_price + bid_price) / 2.0
            
            # Возвращаем все три цены
            return {
                'ask': ask_price,   # Цена покупки
                'bid': bid_price,   # Цена продажи
                'mid': mid_price    # Средняя цена
            }
                
        except Exception as e:
            raise Exception(f'Ranger quote price extraction failed: {e}')


    @async_retry(source="Browser")
    async def get_account_balances(self):
        """
        Получает балансы всех токенов в кошельке
        """
        # Эта функция должна использовать SolWallet для получения балансов
        # Возвращаем заглушку, реализация будет в SpotClient
        return {}

    async def get_trade_history(self, token_pair: str = None, limit: int = 50):
        """
        Получает историю сделок аккаунта (market + limit orders)
        
        Args:
            token_pair: Пара токенов (например "WBTC-USDC"), None = все
            limit: Максимальное количество сделок
            
        Returns:
            List[dict]: Список сделок с полем 'order_type': 'market' или 'limit'
        """
        all_trades = []
        
        try:
            # 1. Получаем market orders
            r_market = await self.send_request(
                method="GET",
                url="https://prod-spot-api-437363704888.asia-northeast1.run.app/api/v1/orders/market",
                params={
                    "user_wallet_address": str(self.sol_address)
                }
            )
            
            response_market = await r_market.json()
            
            if response_market and isinstance(response_market, list):
                # Парсим маркет ордера
                market_trades = self._parse_market_orders(response_market, token_pair)
                # Добавляем тип ордера
                for trade in market_trades:
                    trade['order_type'] = 'market'
                all_trades.extend(market_trades)
            
            # 2. Получаем limit orders (попытка получить историю)
            # Ranger Finance может хранить историю исполненных limit orders
            # Пробуем получить через тот же endpoint с параметром status
            try:
                r_limit = await self.send_request(
                    method="GET",
                    url="https://prod-spot-api-437363704888.asia-northeast1.run.app/api/v1/orders/limit",
                    params={
                        "user_wallet_address": str(self.sol_address),
                        "status": "filled"  # Пытаемся получить исполненные
                    }
                )
                
                response_limit = await r_limit.json()
                
                if response_limit and isinstance(response_limit, list):
                    # Парсим лимитные ордера (используем ту же логику)
                    limit_trades = self._parse_limit_orders(response_limit, token_pair)
                    # Добавляем тип ордера
                    for trade in limit_trades:
                        trade['order_type'] = 'limit'
                    all_trades.extend(limit_trades)
            except Exception as e:
                # Если endpoint не поддерживает историю лимитных ордеров - не страшно
                logger.debug(f"Limit order history unavailable: {e}")
            
            # Сортируем по времени (новые первые)
            all_trades.sort(key=lambda x: x.get('timestamp', 0), reverse=True)
            
            return all_trades[:limit]
            
        except Exception as e:
            logger.warning(f"⚠️ Failed to get trade history: {e}")
            return []
    
    def _parse_market_orders(self, orders: list, token_pair: str = None):
        """
        Парсит маркет-ордера из Ranger Finance API /api/v1/orders/market
        
        Фактическая структура ордера (из API):
        {
            "input_amount": 35869074,
            "input_mint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
            "input_mint_decimals": 6,
            "input_ui_amount": 35.869074,
            "output_amount": 32325,
            "output_mint": "3NZ9JMVBmGAqocybic2c7LQCJScmgsAZ6vQqTDzcqmJh",  # WBTC
            "output_mint_decimals": 8,
            "output_ui_amount": 0.00032325,
            "signature": "...",
            "created_at": 1762074543661,
            "is_via_ranger": 1
        }
        """
        from .config import SOL_TOKEN_ADDRESSES
        
        # Создаем обратный маппинг: address -> symbol
        address_to_symbol = {v: k for k, v in SOL_TOKEN_ADDRESSES.items()}
        
        parsed_trades = []
        
        for order in orders:
            # Получаем адреса токенов
            input_token_mint = order.get("input_mint")
            output_token_mint = order.get("output_mint")
            
            # Конвертируем адреса в символы
            from_token = address_to_symbol.get(input_token_mint, "UNKNOWN")
            to_token = address_to_symbol.get(output_token_mint, "UNKNOWN")
            
            # Пропускаем неизвестные токены
            if from_token == "UNKNOWN" or to_token == "UNKNOWN":
                continue
            
            # Фильтр по паре токенов
            if token_pair:
                pair = f"{from_token}-{to_token}"
                reverse_pair = f"{to_token}-{from_token}"
                if pair != token_pair and reverse_pair != token_pair:
                    continue
            
            # Используем готовые UI amounts (уже в правильных единицах!)
            from_amount = float(order.get("input_ui_amount", 0))
            to_amount = float(order.get("output_ui_amount", 0))
            
            # Пропускаем сделки с нулевым input или output (провалившиеся или некорректные)
            if from_amount <= 0 or to_amount <= 0:
                continue
            
            # Рассчитываем цену (для USDC -> WBTC это цена WBTC в USDC)
            if from_token == "USDC" and to_token == "WBTC":
                rate = from_amount / to_amount if to_amount > 0 else 0
            elif from_token == "WBTC" and to_token == "USDC":
                rate = to_amount / from_amount if from_amount > 0 else 0
            else:
                rate = to_amount / from_amount if from_amount > 0 else 0
            
            # Рассчитываем цену для удобства
            if from_token == "USDC":
                price = rate  # USDC -> Token: цена = rate
            else:
                price = rate  # Token -> USDC: цена = rate
            
            parsed_trades.append({
                "timestamp": order.get("created_at", 0) / 1000,  # ms -> seconds
                "from_token": from_token,
                "to_token": to_token,
                "from_amount": from_amount,
                "to_amount": to_amount,
                "rate": rate,
                "price": price,
                "platform": order.get("provider", "Ranger Finance"),
                "type": "MarketOrder",
                "tx_hash": order.get("signature", ""),
                "signature": order.get("signature", "")
            })
        
        return parsed_trades
    
    def _parse_limit_orders(self, orders: list, token_pair: str = None):
        """
        Парсит исполненные лимитные ордера из Ranger Finance API
        
        Структура похожа на открытые лимитные ордера, но это уже исполненные
        """
        from .config import SOL_TOKEN_ADDRESSES
        
        # Создаем обратный маппинг: address -> symbol
        address_to_symbol = {v: k for k, v in SOL_TOKEN_ADDRESSES.items()}
        
        parsed_trades = []
        
        for order in orders:
            # Получаем адреса токенов
            input_token_mint = order.get("input_mint")
            output_token_mint = order.get("output_mint")
            
            # Конвертируем адреса в символы
            from_token = address_to_symbol.get(input_token_mint, "UNKNOWN")
            to_token = address_to_symbol.get(output_token_mint, "UNKNOWN")
            
            # Пропускаем неизвестные токены
            if from_token == "UNKNOWN" or to_token == "UNKNOWN":
                continue
            
            # Фильтр по паре токенов
            if token_pair:
                pair = f"{from_token}-{to_token}"
                reverse_pair = f"{to_token}-{from_token}"
                if pair != token_pair and reverse_pair != token_pair:
                    continue
            
            # Парсим количества (используем initial_input_amount и expected_output_amount)
            input_decimals = order.get("input_mint_decimals", 8)
            output_decimals = order.get("output_mint_decimals", 6)
            
            from_amount = float(order.get("initial_input_amount", 0)) / (10 ** input_decimals)
            to_amount = float(order.get("expected_output_amount", 0)) / (10 ** output_decimals)
            
            # Пропускаем сделки с нулевым объемом
            if from_amount <= 0 or to_amount <= 0:
                continue
            
            # Рассчитываем цену (rate)
            if from_token == "USDC":
                # USDC -> Token: rate = USDC / Token
                rate = from_amount / to_amount if to_amount > 0 else 0
                price = rate  # Цена токена в USDC
            else:
                # Token -> USDC: rate = USDC / Token
                rate = to_amount / from_amount if from_amount > 0 else 0
                price = rate  # Цена токена в USDC
            
            parsed_trades.append({
                "timestamp": order.get("created_at", 0) / 1000,  # ms -> seconds
                "from_token": from_token,
                "to_token": to_token,
                "from_amount": from_amount,
                "to_amount": to_amount,
                "rate": rate,
                "price": price,  # Добавляем явно цену для удобства
                "platform": "Kamino",  # Лимитные ордера через Kamino
                "type": "SpotLimit",
                "tx_hash": order.get("signature", ""),
                "signature": order.get("signature", "")
            })
        
        return parsed_trades
    
    def _parse_ranger_trades(self, trades: list, token_pair: str = None):
        """
        Парсит сделки из Ranger Finance API (legacy, не используется)
        """
        parsed_trades = []
        
        for trade in trades:
            # Парсим формат Ranger Finance
            from_token = trade.get("from_token") or trade.get("input_token")
            to_token = trade.get("to_token") or trade.get("output_token")
            
            # Фильтр по паре токенов
            if token_pair:
                pair = f"{from_token}-{to_token}"
                reverse_pair = f"{to_token}-{from_token}"
                if pair != token_pair and reverse_pair != token_pair:
                    continue
            
            parsed_trades.append({
                "timestamp": trade.get("timestamp") or trade.get("time"),
                "from_token": from_token,
                "to_token": to_token,
                "from_amount": float(trade.get("from_amount", 0) or trade.get("input_amount", 0)),
                "to_amount": float(trade.get("to_amount", 0) or trade.get("output_amount", 0)),
                "rate": float(trade.get("rate", 0)),
                "platform": trade.get("platform", "Unknown"),
                "type": trade.get("type", "SpotMarket"),
                "tx_hash": trade.get("tx_hash") or trade.get("signature")
            })
        
        return parsed_trades
    
    async def _get_solana_transactions(self, limit: int = 50):
        """
        Получает транзакции кошелька из Solana RPC
        """
        try:
            import settings
            
            r = await self.send_request(
                method="POST",
                url=settings.RPCS.get("solana", "https://api.mainnet-beta.solana.com"),
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getSignaturesForAddress",
                    "params": [
                        self.sol_address,
                        {"limit": limit}
                    ]
                }
            )
            response = await r.json()
            
            if response.get("result"):
                return response["result"]
            
            return []
            
        except Exception as e:
            logger.debug(f"Failed to get Solana transactions: {e}")
            return []
    
    def _parse_solana_transactions(self, transactions: list, token_pair: str = None):
        """
        Парсит транзакции Solana в формат сделок
        """
        # Базовая реализация - возвращаем только метаданные
        # Для полного парсинга нужно получать детали каждой транзакции
        parsed_trades = []
        
        for tx in transactions:
            if tx.get("err"):  # Пропускаем ошибочные транзакции
                continue
            
            parsed_trades.append({
                "timestamp": tx.get("blockTime"),
                "tx_hash": tx.get("signature"),
                "type": "Unknown",  # Нужен детальный парсинг для определения типа
                "from_token": None,
                "to_token": None,
                "from_amount": 0,
                "to_amount": 0,
                "rate": 0,
                "platform": "Solana"
            })
        
        return parsed_trades