from loguru import logger
from aiohttp import ClientSession
import os


def _load_tg_tokens():
    """Загружает токены из input_data/tg_bot_tokens.txt"""
    token_file = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 'input_data', 'tg_bot_tokens.txt')
    
    bot_token = ''
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
                if len(lines) >= 3:
                    user_ids_str = extract_value(lines[2])
                    user_ids = [int(uid.strip()) for uid in user_ids_str.split(',') if uid.strip().isdigit()]
    except:
        pass
    
    return bot_token, user_ids


class TgReport:
    """
    Класс для отправки отчетов в Telegram.
    Аналогичен реализации в проекте hype.
    """
    
    def __init__(self, logs=""):
        self.logs = logs
        bot_token, user_ids = _load_tg_tokens()
        self.bot_token = bot_token
        self.user_ids = user_ids

    def update_logs(self, text: str):
        """Добавляет текст к логам"""
        self.logs += f'{text}\n'

    async def send_log(self, logs: str = None):
        """
        Отправляет логи в Telegram.
        Разбивает длинные сообщения на части по 1900 символов.
        """
        notification_text = logs or self.logs

        texts = []
        while len(notification_text) > 0:
            texts.append(notification_text[:1900])
            notification_text = notification_text[1900:]

        if not self.bot_token or not self.user_ids:
            logger.warning("Telegram bot token or user IDs not configured")
            return

        try:
            async with ClientSession() as session:
                for tg_id in self.user_ids:
                    for text in texts:
                        try:
                            url = f'https://api.telegram.org/bot{self.bot_token}/sendMessage'
                            data = {
                                'parse_mode': 'HTML',
                                'disable_web_page_preview': True,
                                'chat_id': tg_id,
                                'text': text,
                            }
                            
                            async with session.post(url, json=data) as response:
                                if response.status != 200:
                                    logger.error(f'Failed to send Telegram message to {tg_id}: HTTP {response.status}')
                                else:
                                    result = await response.json()
                                    if not result.get("ok"):
                                        logger.error(f'Telegram API error to {tg_id}: {result}')
                                        
                        except Exception as err:
                            logger.error(f'[-] TG | Send Telegram message error to {tg_id}: {err}')
                            
        except Exception as e:
            logger.error(f"Failed to send Telegram notification: {e}")
