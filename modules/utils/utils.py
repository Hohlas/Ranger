from base58 import b58encode, b58decode
from http.cookies import SimpleCookie
from solders.keypair import Keypair
from datetime import datetime
from decimal import Decimal
from random import randint
from loguru import logger
from time import sleep
from web3 import Web3
from tqdm import tqdm
import asyncio
import string
import sys
sys.__stdout__ = sys.stdout # error with `import inquirer` without this string in some system


logger.remove()
logger.add(sys.stderr, format="<white>{time:HH:mm:ss}</white> | <level>{message}</level>")


def sleeping(*timing):
    if type(timing[0]) == list: timing = timing[0]
    if len(timing) == 2: x = randint(timing[0], timing[1])
    else: x = timing[0]
    desc = datetime.now().strftime('%H:%M:%S')
    if x <= 0: return
    for _ in tqdm(range(x), desc=desc, bar_format='{desc} | [•] Sleeping {n_fmt}/{total_fmt}'):
        sleep(1)


def make_border(
        table_elements: dict,
        keys_color: str | None = None,
        values_color: str | None = None,
        table_color: str | None = None,
):
    def tag_color(value: str, color: str | None):
        if keys_color:
            return f"<{color}>{value}</{color}>"
        return value

    left_margin = 25
    space = 2
    horiz = '━'
    vert = '║'
    conn = 'o'

    if not table_elements: return "No text"

    key_len = max([len(key) for key in table_elements.keys()])
    val_len = max([len(str(value)) for value in table_elements.values()])			# qL
    text = f'{" " * left_margin}{conn}{horiz * space}'

    text += horiz * (key_len + space) + conn
    text += horiz * space
    text += horiz * (val_len + space) + conn

    text += '\n'

    for table_index, element in enumerate(table_elements):
        text += f'{" " * left_margin}{vert}{" " * space}'

        text += f'{tag_color(element, keys_color)}{" " * (key_len - len(element) + space)}{vert}{" " * space}'
        text += f'{tag_color(table_elements[element], values_color)}{" " * (val_len - len(str(table_elements[element])) + space)}{vert}'
        text += "\n" + " " * left_margin + conn + horiz * space
        text += horiz * (key_len + space) + conn
        text += horiz * (space * 2 + val_len) + conn + '\n'
    return tag_color(text, table_color)


def format_password(password: str):
    # ADD UPPER CASE
    if not any([password_symbol in string.ascii_uppercase for password_symbol in password]):
        first_letter = next(
            (symbol for symbol in password if symbol in string.ascii_letters),
            "i"
        )
        password += first_letter.upper()

    # add lower case
    if not any([password_symbol in string.ascii_lowercase for password_symbol in password]):
        first_letter = next(
            (symbol for symbol in password if symbol in string.ascii_letters),
            "f"
        )
        password += first_letter.lower()

    # add numb3r5
    if not any([password_symbol in string.digits for password_symbol in password]):
        password += str(len(password))[0]

    # add $ymbol$
    symbols_list = '!"#$%&\'()*+,-./:;<=>?@[]^_`{|}~'
    if not any([password_symbol in symbols_list for password_symbol in password]):
        password += symbols_list[sum(ord(c) for c in password) % len(symbols_list)]

    # add 8 characters
    if len(password) < 8:
        all_symbols = string.digits + string.ascii_letters
        password += ''.join(
            all_symbols[sum(ord(c) for c in password[:i+1]) % len(symbols_list)]
            for i in range(max(0, 8 - len(password)))
        )

    return password


def get_address(pk: str):
    return Web3().eth.account.from_key(pk).address


def parse_cookies(cookies: str, key: str):
    cookie = SimpleCookie()
    cookie.load(cookies)
    return cookie[key].value if cookie.get(key) else None


def get_response_error_reason(response: dict):
    return str(response.get("errors", [{}])[0].get("message", response)).removeprefix("Authorization: ")


def round_cut(value: float | str | Decimal, digits: int):
    return Decimal(str(int(float(value) * 10 ** digits) / 10 ** digits))


def get_sol_address(pk: str):
    return str(Keypair.from_base58_string(pk).pubkey())


async def async_sleep(seconds: int):
    for _ in range(int(seconds)):
        await asyncio.sleep(1)


def _load_tg_tokens():
    """Загружает токены из input_data/tg_bot_tokens.txt"""
    import os
    token_file = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 'input_data', 'tg_bot_tokens.txt')
    
    bot_token = ''
    profit_bot_token = ''
    user_ids = []
    
    def extract_value(line: str) -> str:
        """Извлекает значение из строки (поддержка Python-формата)"""
        if '=' in line:
            value = line.split('=', 1)[1].strip()
            if '#' in value:
                value = value.split('#')[0].strip()
            value = value.strip("'\"[]")
            return value
        return line
    
    try:
        if os.path.exists(token_file):
            with open(token_file, 'r', encoding='utf-8') as f:
                lines = [line.strip() for line in f.readlines() if line.strip() and not line.strip().startswith('#')]
                
                if len(lines) >= 1:
                    bot_token = extract_value(lines[0])
                if len(lines) >= 2:
                    profit_bot_token = extract_value(lines[1])
                if len(lines) >= 3:
                    user_ids_str = extract_value(lines[2])
                    user_ids = [int(uid.strip()) for uid in user_ids_str.split(',') if uid.strip().isdigit()]
    except:
        pass
    
    return bot_token, profit_bot_token, user_ids


async def send_warning_notification(error_type: str, error_message: str, account_label: str = "Unknown"):
    """
    Отправляет уведомление о критической ошибке в Telegram через основного бота (TG_BOT_TOKEN).
    Формат: 🚨 Ranger Bot | [аккаунт] ❌ Ошибка: [тип ошибки] 📝 Описание: [описание ошибки]
    """
    try:
        from aiohttp import ClientSession
        
        TG_BOT_TOKEN, _, TG_USER_ID = _load_tg_tokens()

        if not TG_BOT_TOKEN or not TG_USER_ID:
            return

        message = f"🚨 Ranger Bot | {account_label}\n"
        message += f"❌ Ошибка: {error_type}\n"
        message += f"📝 Описание: {error_message}"

        async with ClientSession() as session:
            for user_id in TG_USER_ID:
                url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
                data = {
                    "chat_id": user_id,
                    "text": message,
                    "parse_mode": "HTML"
                }

                try:
                    async with session.post(url, json=data) as response:
                        if response.status != 200:
                            logger.error(f"Failed to send warning notification: {response.status}")
                except Exception as e:
                    logger.error(f"Failed to send warning to {user_id}: {e}")

    except Exception as e:
        logger.error(f"Failed to send warning notification: {e}")


async def send_profit_notification(message: str):
    """
    ДУБЛИРУЕТ уведомление о профите в отдельный бот (PROFIT_BOT_TOKEN).
    
    ⚠️ Это ДОПОЛНИТЕЛЬНОЕ уведомление к основному боту (TG_BOT_TOKEN)!
    Профит отправляется в ОБА бота: основной + PROFIT_BOT.
    
    Args:
        message: Текст сообщения для отправки
    """
    try:
        from aiohttp import ClientSession
        
        _, PROFIT_BOT_TOKEN, TG_USER_ID = _load_tg_tokens()

        if not PROFIT_BOT_TOKEN or not TG_USER_ID:
            return

        # Добавляем префикс "ranger:" к сообщениям для profit bot
        profit_message = f"🎯 ranger: {message}"

        async with ClientSession() as session:
            for user_id in TG_USER_ID:
                url = f"https://api.telegram.org/bot{PROFIT_BOT_TOKEN}/sendMessage"
                data = {
                    "chat_id": user_id,
                    "text": profit_message,
                    "parse_mode": "HTML"
                }

                try:
                    async with session.post(url, json=data) as response:
                        if response.status != 200:
                            logger.error(f"Failed to send profit notification: {response.status}")
                except Exception as e:
                    logger.error(f"Failed to send profit to {user_id}: {e}")

    except Exception as e:
        logger.error(f"Failed to send profit notification: {e}")