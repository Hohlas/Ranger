"""
Spot Client для стратегии усреднения/пирамидинга
Адаптирован для работы с Ranger Finance API на Solana
"""

from decimal import Decimal
from loguru import logger
from typing import Dict, Optional
from datetime import datetime
import asyncio

from .utils import round_cut, async_sleep
from .utils.tg_report import TgReport
from .sol_wallet import SolWallet
from .browser import Browser

# Кэш для ограничения повторяющихся логов
_log_cooldown_cache = {}


def can_log_repeated(account_label: str, message_type: str, cooldown_minutes: int = 5) -> bool:
    """
    Проверяет, можно ли выводить повторяющееся сообщение.
    Ограничивает вывод: не чаще, чем раз в cooldown_minutes минут.
    """
    global _log_cooldown_cache
    from time import time
    
    current_time = time()
    cache_key = f"{account_label}_{message_type}"
    last_log_time = _log_cooldown_cache.get(cache_key, 0)
    cooldown_seconds = cooldown_minutes * 60
    
    if current_time - last_log_time >= cooldown_seconds:
        _log_cooldown_cache[cache_key] = current_time
        return True
    
    return False


class SpotClient:
    """
    Клиент для Spot торговли с поддержкой стратегии усреднения/пирамидинга
    """

    def __init__(self, sol_wallet: SolWallet, browser: Browser, db, token_name: str = "WBTC"):
        self.sol_wallet = sol_wallet
        self.browser = browser
        self.db = db
        self.token_name = token_name  # По умолчанию WBTC (Wrapped Bitcoin)
        self.label = sol_wallet.label
        
        # TP ордера (синхронизируются с биржей)
        self.tp_orders = []  # Список TP ордеров на бирже
        
    async def get_token_balance(self, token: str) -> Decimal:
        """
        Получает баланс токена в кошельке с retry-логикой и кэшированием
        """
        max_attempts = 5
        delay = 1.0  # Начальная задержка 1 секунда
        
        # Инициализируем кэш если нужно
        if not hasattr(self, '_balance_cache'):
            self._balance_cache = {}
        
        for attempt in range(1, max_attempts + 1):
            try:
                token_info = await self.sol_wallet.get_token_info(token)
                balance = Decimal(str(token_info.get("amount", 0)))
                
                # Сохраняем в кэш
                self._balance_cache[token] = balance
                return balance
                
            except Exception as e:
                if attempt < max_attempts:
                    # Не логируем промежуточные ошибки
                    await asyncio.sleep(delay)
                    delay *= 2  # Exponential backoff: 1s → 2s → 4s → 8s → 16s
                else:
                    # Все попытки провалились
                    # Используем кэшированное значение если есть
                    if token in self._balance_cache:
                        logger.warning(
                            f'{self.sol_wallet.label}: Failed to get {token} balance after {max_attempts} attempts, '
                            f'using cached value: {self._balance_cache[token]}'
                        )
                        return self._balance_cache[token]
                    else:
                        logger.error(
                            f'{self.sol_wallet.label}: Failed to get {token} balance after {max_attempts} attempts, '
                            f'no cached value available'
                        )
                        return Decimal('0')

    async def get_usdc_balance(self) -> Decimal:
        """
        Получает баланс USDC
        """
        return await self.get_token_balance("USDC")

    async def get_current_price(self, token: str) -> Decimal:
        """
        Получает текущую цену токена
        """
        try:
            price = await self.browser.get_token_price(token)
            return Decimal(str(price))
        except Exception as e:
            logger.error(f'Failed to get price for {token}: {e}')
            raise

    async def calculate_position_size(self) -> Decimal:
        """
        Рассчитывает размер позиции на основе POSITION_SIZE_PERCENT
        """
        import settings
        
        usdc_balance = await self.get_usdc_balance()
        position_size = usdc_balance * Decimal(str(settings.POSITION_SIZE_PERCENT / 100))
        
        return position_size

    async def place_market_order(self, from_token: str, to_token: str, amount: Decimal):
        """
        Размещает маркет ордер (swap через Ranger)
        """
        try:
            # Получаем информацию о токенах
            from_token_info = await self.sol_wallet.get_token_info(from_token)
            to_token_info = await self.sol_wallet.get_token_info(to_token)

            # Проверяем минимальный размер
            import settings
            current_price = await self.get_current_price(self.token_name)
            current_price_decimal = Decimal(str(current_price))
            
            # Определяем направление свапа
            if from_token == "USDC":
                # Покупка: USDC → Token
                token_amount = amount / current_price_decimal
                notional_value = amount
            else:
                # Продажа: Token → USDC
                token_amount = amount
                notional_value = amount * current_price_decimal
            
            if token_amount < Decimal(str(settings.MIN_ORDER_SIZE_BTC)):
                # Ограничиваем вывод: не чаще раза в 5 минут
                if can_log_repeated(self.label, "order_size_below_minimum"):
                    self.log_message(
                        f'Order size {token_amount:.6f} {self.token_name} is below minimum {settings.MIN_ORDER_SIZE_BTC}',
                        level="WARNING"
                    )
                return None

            if notional_value < Decimal(str(settings.MIN_ORDER_NOTIONAL)):
                # Ограничиваем вывод: не чаще раза в 5 минут
                if can_log_repeated(self.label, "order_notional_below_minimum"):
                    self.log_message(
                        f'Order notional ${notional_value:.2f} is below minimum ${settings.MIN_ORDER_NOTIONAL}',
                        level="WARNING"
                    )
                return None

            # Получаем котировки для свапа
            value = int(amount * 10 ** from_token_info["decimals"])
            quotes = await self.browser.get_market_order_quote(
                from_token=from_token,
                to_token=to_token,
                value=value
            )

            if not quotes:
                raise Exception(f'Failed to get quotes for {amount} {from_token} → {to_token}')

            # Выбираем лучшую котировку
            quote = self._find_best_quote(quotes)
            if not quote:
                raise Exception(f'No suitable quote found for swap')

            amount_out = round_cut(
                quote["output_token_info"]["amount"] / 10 ** to_token_info["decimals"],
                7
            )
            
            swap_provider = quote['provider'].replace("_", " ").title()
            
            # Логируем операцию
            self.log_message(
                f'Market order <green>{amount} {from_token}</green> → <green>{amount_out} {to_token}</green> ({swap_provider})',
                level="INFO"
            )

            # Выполняем swap через SolWallet
            from solders.transaction import VersionedTransaction
            from base64 import b64decode
            
            tx = VersionedTransaction.from_bytes(b64decode(quote["transaction"]))
            old_balance = (await self.sol_wallet.get_token_info(to_token))["amount"]
            
            await self.sol_wallet.send_transaction(
                tx_label=f"ranger market order {amount} {from_token} → {amount_out} {to_token}",
                completed_tx_message=tx.message,
                signatures=tx.signatures,
            )

            new_balance = await self.sol_wallet.wait_for_balance(
                previous_balance_amount=old_balance,
                token=to_token,
            )

            actual_amount = new_balance["amount"] - old_balance
            
            # Рассчитываем реальную цену исполнения
            # Цена всегда = USDC / Token (цена токена в долларах)
            if from_token == "USDC":
                # Покупка: USDC → Token
                # price = сколько USDC заплатили / сколько Token получили
                execution_price = float(Decimal(str(amount)) / Decimal(str(actual_amount))) if actual_amount > 0 else 0
            else:
                # Продажа: Token → USDC
                # price = сколько USDC получили / сколько Token продали
                execution_price = float(Decimal(str(actual_amount)) / Decimal(str(amount))) if amount > 0 else 0
            
            return {
                "from_token": from_token,
                "to_token": to_token,
                "from_amount": float(amount),
                "to_amount": float(actual_amount),
                "price": execution_price,
                "provider": swap_provider
            }

        except Exception as e:
            self.log_message(f'Failed to place market order: {e}', level="ERROR")
            raise

    async def place_limit_order(self, from_token: str, to_token: str, amount: Decimal, limit_price: float):
        """
        Размещает лимитный ордер на бирже через Kamino
        
        Args:
            from_token: Токен который продаем (например "WBTC")
            to_token: Токен который покупаем (например "USDC")
            amount: Количество from_token для продажи
            limit_price: Лимитная цена (сколько to_token получим за 1 from_token)
        
        Returns:
            dict: Информация о созданном ордере или None если не удалось создать
        """
        try:
            from_token_info = await self.sol_wallet.get_token_info(from_token)
            to_token_info = await self.sol_wallet.get_token_info(to_token)
            
            import settings
            
            # Проверка минимального размера
            if amount < Decimal(str(settings.MIN_ORDER_SIZE_BTC)):
                self.log_message(
                    f'Limit order size {amount:.6f} {from_token} is below minimum {settings.MIN_ORDER_SIZE_BTC}',
                    level="WARNING"
                )
                return None
            
            # Проверка минимальной стоимости
            notional_value = amount * Decimal(str(limit_price))
            if notional_value < Decimal(str(settings.MIN_ORDER_NOTIONAL)):
                self.log_message(
                    f'Limit order notional ${notional_value:.2f} is below minimum ${settings.MIN_ORDER_NOTIONAL}',
                    level="WARNING"
                )
                return None
            
            # Шаг 1: Получаем котировку для лимитного ордера
            value = int(amount * Decimal(str(10 ** from_token_info["decimals"])))
            
            self.log_message(
                f'🔄 Requesting limit order quote: {amount} {from_token} @ ${limit_price:.2f}',
                level="DEBUG"
            )
            
            quote = await self.browser.get_limit_order_quote(
                from_token=from_token,
                to_token=to_token,
                value=value,
                limit_price=limit_price
            )
            
            if not quote or not quote.get('transaction'):
                raise Exception(f'Failed to get quote for limit order')
            
            # Шаг 2: Подписываем транзакцию
            from solders.transaction import VersionedTransaction
            from base64 import b64decode, b64encode
            
            tx = VersionedTransaction.from_bytes(b64decode(quote["transaction"]))
            
            self.log_message(
                f'🔍 [STEP 2] Signing limit order transaction: {amount} {from_token} @ ${limit_price:.2f}',
                level="INFO"
            )
            
            # Извлекаем limit_order_account_address из quote
            limit_order_account_address = quote.get('limit_order_account_address')
            if not limit_order_account_address:
                raise Exception('limit_order_account_address not found in quote response')
            
            self.log_message(
                f'📤 [STEP 2.5] Sending transaction to Solana blockchain...',
                level="INFO"
            )
            
            # ВАЖНО: Отправляем транзакцию в блокчейн (как в маркет ордерах!)
            tx_signature = await self.sol_wallet.send_transaction(
                tx_label=f"limit order {amount} {from_token} @ ${limit_price:.2f}",
                completed_tx_message=tx.message,
                signatures=tx.signatures,
            )
            
            self.log_message(
                f'✅ [STEP 2.5] Transaction sent! Signature: {str(tx_signature)[:16]}...',
                level="INFO"
            )
            
            # ВАЖНО: Транзакция уже в блокчейне, ордер создан!
            # Вызов /register опционален и часто возвращает ошибку кэша, но это не важно
            
            self.log_message(
                f'📝 [STEP 3] Optional: Trying to register on Ranger Finance...',
                level="DEBUG"
            )
            
            # Пробуем зарегистрировать (но это не обязательно для работы ордера)
            try:
                import base58
                user_signature = base58.b58encode(bytes(tx_signature)).decode('utf-8')
                
                register_response = await self.browser.register_limit_order(
                    limit_order_account_address=limit_order_account_address,
                    user_signature=user_signature
                )
                
                if register_response and register_response.get('success'):
                    self.log_message(
                        f'✅ [STEP 3] Registered on Ranger Finance',
                        level="DEBUG"
                    )
                else:
                    self.log_message(
                        f'⚠️ [STEP 3] Registration failed, but order is already on-chain (OK)',
                        level="DEBUG"
                    )
            except Exception as e:
                # Игнорируем ошибки регистрации - ордер уже в блокчейне
                self.log_message(
                    f'⚠️ [STEP 3] Registration error (ignored): {str(e)[:100]}',
                    level="DEBUG"
                )
            
            # Используем limit_order_account_address как order_id
            order_id = limit_order_account_address
            
            # Возвращаем информацию о созданном ордере
            order_info = {
                "from_token": from_token,
                "to_token": to_token,
                "from_amount": float(amount),
                "limit_price": limit_price,
                "expected_to_amount": float(amount * Decimal(str(limit_price))),
                "status": "open",
                "order_id": order_id
            }
            
            self.log_message(
                f'✅ Limit order placed on exchange: {amount} {from_token} @ ${limit_price:.2f} (ID: {order_id[:8]}...)',
                level="INFO"
            )
            
            return order_info
            
        except Exception as e:
            self.log_message(f'Failed to place limit order: {e}', level="ERROR")
            # Не прерываем работу, просто возвращаем None
            return None

    @classmethod
    def _find_best_quote(cls, quotes: list):
        """
        Находит лучшую котировку из списка (используется для маркет-ордеров)
        """
        ban_list = ["d_flow", "pyth_rfq"]
        not_banned_quotes = [q for q in quotes if q["provider"] not in ban_list]
        
        if not_banned_quotes:
            return sorted(
                not_banned_quotes,
                key=lambda x: x["output_token_info"]["amount"],
                reverse=True
            )[0]
        return None


    def log_message(self, text: str, smile: str = "•", level: str = "INFO", colors: bool = True):
        """
        Логирует сообщение с меткой аккаунта
        """
        label = f"<white>{self.label}</white>" if colors else self.label
        logger.opt(colors=colors).log(level.upper(), f'[{smile}] {label} | {text}')

