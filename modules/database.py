from random import choice, randint, shuffle
from cryptography.fernet import Fernet
from base64 import urlsafe_b64encode
from os import path, mkdir
from loguru import logger
from hashlib import md5
from time import sleep
import asyncio
import json

from modules.utils import get_sol_address, WindowName
from modules.retry import DataBaseError, CustomError
from settings import SHUFFLE_WALLETS

from cryptography.fernet import InvalidToken


class DataBase:
    def __init__(self):

        self.modules_db_name = 'databases/modules.json'
        self.report_db_name = 'databases/report.json'
        self.stats_db_name = 'databases/stats.json'
        self.personal_key = None
        self.window_name = None

        self.lock = asyncio.Lock()

        # create db's if not exists
        if not path.isdir(self.modules_db_name.split('/')[0]):
            mkdir(self.modules_db_name.split('/')[0])

        for db in [
            {"name": self.modules_db_name, "default": "[]"},
            {"name": self.report_db_name, "default": "{}"},
            {"name": self.stats_db_name, "default": "{}"},
        ]:
            if not path.isfile(db["name"]):
                with open(db["name"], 'w') as f: f.write(db["default"])

        amounts = self.get_amounts()
        logger.info(f'Loaded {amounts["modules_amount"]} modules for {amounts["accs_amount"]} accounts\n')

    def set_password(self):
        if self.personal_key is not None: return

        logger.debug(f'Enter password to encrypt privatekeys (empty for default):')
        raw_password = input("")

        if not raw_password:
            raw_password = "@karamelniy dumb shit encrypting"
            logger.success(f'[+] Soft | You set empty password for Database\n')
        else:
            print(f'')
        sleep(0.2)

        password = md5(raw_password.encode()).hexdigest().encode()
        self.personal_key = Fernet(urlsafe_b64encode(password))

    def get_password(self):
        if self.personal_key is not None: return

        with open(self.modules_db_name, encoding="utf-8") as f: modules_db = json.load(f)
        if not modules_db: return

        test_pk = list(modules_db.keys())[0]
        try:
            temp_key = Fernet(urlsafe_b64encode(md5("@karamelniy dumb shit encrypting".encode()).hexdigest().encode()))
            self.decode_pk(pk=test_pk, key=temp_key)
            self.personal_key = temp_key
            return
        except InvalidToken: pass

        while True:
            try:
                logger.debug(f'Enter password to decrypt your privatekeys (empty for default):')
                raw_password = input("")
                password = md5(raw_password.encode()).hexdigest().encode()

                temp_key = Fernet(urlsafe_b64encode(password))
                self.decode_pk(pk=test_pk, key=temp_key)
                self.personal_key = temp_key
                
                # Показываем версию
                import settings
                version = getattr(settings, 'VERSION', 'unknown')
                logger.success(f'[+] Soft | Access granted! Version: {version}\n')
                
                # Логируем статус Telegram токенов
                self._log_tg_tokens_status()
                
                return

            except InvalidToken:
                logger.error(f'[-] Soft | Invalid password\n')

    def _log_tg_tokens_status(self):
        """Логирует статус загрузки Telegram токенов"""
        import os
        token_file = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'input_data', 'tg_bot_tokens.txt')
        
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
        
        if bot_token and user_ids:
            logger.success(
                f"✅ Telegram tokens: Main bot: ✓, Profit bot: {'✓' if profit_bot_token else '✗'}, Users: {len(user_ids)}"
            )
        elif os.path.exists(token_file):
            logger.warning(
                f"⚠️ Telegram tokens incomplete: Main bot: {'✓' if bot_token else '✗'}, "
                f"Profit bot: {'✓' if profit_bot_token else '✗'}, Users: {len(user_ids)}"
            )
        else:
            logger.warning(f"⚠️ Telegram tokens file not found: {token_file}")
            logger.info(f"   Create: nano {token_file}")
            logger.info(f"   Format: Line 1: Main bot token | Line 2: Profit bot token | Line 3: User IDs")

    def encode_pk(self, pk: str, key: None | Fernet = None):
        if key is None:
            return self.personal_key.encrypt(pk.encode()).decode()
        return key.encrypt(pk.encode()).decode()

    def decode_pk(self, pk: str, key: None | Fernet = None):
        if key is None:
            return self.personal_key.decrypt(pk).decode()
        return key.decrypt(pk).decode()

    def create_modules(self):
        self.set_password()

        sol_private_keys = []
        labels = []
        with open('input_data/sol_privatekeys.txt') as f: raw_sol_private_keys = f.read().splitlines()
        for pk_index, raw_pk in enumerate(raw_sol_private_keys):
            pkey_data = raw_pk.split(':')
            if len(pkey_data) == 2:
                labels.append(pkey_data[0])
                sol_private_keys.append(pkey_data[1])

            elif len(pkey_data) == 1:
                sol_address = get_sol_address(pkey_data[0])
                labels.append(sol_address[:5] + '...' + sol_address[-5:])
                sol_private_keys.append(pkey_data[0])

            else:
                raise DataBaseError(f"Unexpected SOL Privatekey format: {raw_pk}")


        with open('input_data/proxies.txt') as f:
            raw_proxies = f.read().splitlines()
        
        # Обрабатываем каждую строку прокси
        processed_proxies = []
        for proxy_line in raw_proxies:
            proxy_line = proxy_line.strip()
            
            # Пропускаем комментарии
            if proxy_line.startswith('#'):
                continue
            
            # "NONE" или пустая строка = без прокси
            if proxy_line.upper() == 'NONE' or not proxy_line:
                processed_proxies.append(None)
            # Шаблоны-подсказки = без прокси
            elif proxy_line in ['http://login:password@ip:port', 'https://log:pass@ip:port', 
                               'log:pass@ip:port', 'log:pass@ip:port1']:
                processed_proxies.append(None)
            # Реальный прокси
            else:
                processed_proxies.append(proxy_line)
        
        # Если файл пустой или только комментарии
        if len(processed_proxies) == 0:
            logger.warning('[•] Soft | proxies.txt is empty - all accounts will work without proxy')
            proxies = [None for _ in range(len(sol_private_keys))]
        else:
            # Циклически повторяем прокси для всех аккаунтов
            proxies = list(processed_proxies * (len(sol_private_keys) // len(processed_proxies) + 1))[:len(sol_private_keys)]
            
            # Логируем конфигурацию прокси
            proxy_with_count = sum(1 for p in proxies if p is not None)
            proxy_without_count = len(proxies) - proxy_with_count
            logger.info(f'[•] Soft | Proxy configuration: {proxy_with_count} accounts with proxy, {proxy_without_count} without proxy')

        with open(self.report_db_name, 'w') as f: f.write('{}')  # clear report db

        new_modules = {
            self.encode_pk(sol_pk): {
                "sol_address": get_sol_address(sol_pk),
                "label": label,
                "modules": [{"module_name": "averaging", "status": "to_run"}],
                "proxy": proxy,
            }
            for sol_pk, label, proxy in zip(sol_private_keys, labels, proxies)
        }
        with open(self.modules_db_name, 'w', encoding="utf-8") as f: json.dump(new_modules, f)

        amounts = self.get_amounts()
        logger.critical(f'Dont Forget To Remove Private Keys from sol_privatekeys.txt!')
        self.set_accounts_modules_done(new_modules)
        logger.info(f'Created Database for {amounts["accs_amount"]} accounts with {amounts["modules_amount"]} modules!\n')

    def get_amounts(self):
        with open(self.modules_db_name, encoding="utf-8") as f: modules_db = json.load(f)
        modules_len = sum([len(modules_db[acc]["modules"]) for acc in modules_db])

        for acc in modules_db:
            for index, module in enumerate(modules_db[acc]["modules"]):
                if module["status"] in ["failed", "in_progress"]: modules_db[acc]["modules"][index]["status"] = "to_run"

        with open(self.modules_db_name, 'w', encoding="utf-8") as f: json.dump(modules_db, f)

        if self.window_name == None: self.window_name = WindowName(accs_amount=len(modules_db))
        else: self.window_name.accs_amount = len(modules_db)
        self.window_name.set_modules(modules_amount=modules_len)

        return {'accs_amount': len(modules_db), 'modules_amount': modules_len}


    async def get_random_module(self):
        async with self.lock:
            self.get_password()

            last = False
            with open(self.modules_db_name, encoding="utf-8") as f: modules_db = json.load(f)

            if (
                    not modules_db or
                    [module["status"] for acc in modules_db for module in modules_db[acc]["modules"]].count('to_run') == 0
            ):
                    return 'No more accounts left'

            index = 0
            while True:
                if index == len(modules_db.keys()) - 1: index = 0
                if SHUFFLE_WALLETS: sol_privatekey = choice(list(modules_db.keys()))
                else: sol_privatekey = list(modules_db.keys())[index]
                module_info = choice(modules_db[sol_privatekey]["modules"])
                if module_info["status"] not in ["to_run"]:
                    index += 1
                    continue
                module_info["status"] = "in_progress"

                # simulate db
                for module in modules_db[sol_privatekey]["modules"]:
                    if module["module_name"] == module_info["module_name"] and module["status"] == module_info["status"]:
                        module["status"] = "in_progress"
                        with open(self.modules_db_name, 'w', encoding="utf-8") as f: json.dump(modules_db, f)
                        break

                if [module["status"] for module in modules_db[sol_privatekey]["modules"]].count('to_run') == 0: # if no modules left for this account
                    last = True

                return {
                    'sol_pk': self.decode_pk(pk=sol_privatekey),
                    'sol_encoded_pk': sol_privatekey,
                    'proxy': modules_db[sol_privatekey].get("proxy"),
                    'module_info': module_info,
                    'last': last
                }


    def get_modules_left(self, encoded_pk: str):
        self.get_password()
        with open(self.modules_db_name, encoding="utf-8") as f: modules_db = json.load(f)
        if not modules_db.get(encoded_pk): return 0
        return len([m for m in modules_db[encoded_pk]["modules"] if m["status"] == "to_run"])


    def set_accounts_modules_done(self, new_modules: dict):
        with open(self.stats_db_name, encoding="utf-8") as f: stats_db = json.load(f)
        stats_db["modules_done"] = {
            v["sol_address"]: [0, len(v["modules"])]
            for k, v in new_modules.items()
        }
        with open(self.stats_db_name, 'w', encoding="utf-8") as f: json.dump(stats_db, f)


    def increase_account_modules_done(self, address: str):
        with open(self.stats_db_name, encoding="utf-8") as f: stats_db = json.load(f)
        modules_done = stats_db["modules_done"].get(address)
        if modules_done is None:
            return None
        modules_done[0] += 1
        if modules_done[0] == modules_done[1]:
            del stats_db["modules_done"][address]
        else:
            stats_db["modules_done"][address] = modules_done

        with open(self.stats_db_name, 'w', encoding="utf-8") as f: json.dump(stats_db, f)
        return modules_done


    def get_all_modules(self, unique_wallets: bool = False):
        self.get_password()
        with open(self.modules_db_name, encoding="utf-8") as f: modules_db = json.load(f)

        if (
                not modules_db or
                (
                        [module["status"] for acc in modules_db for module in modules_db[acc]["modules"]].count('to_run') == 0 and
                        [module["status"] for acc in modules_db for module in modules_db[acc]["modules"]].count('cloudflare') == 0
                )
        ):
                return 'No more accounts left'

        all_wallets_modules = [
            {
                'sol_pk': self.decode_pk(pk=encoded_pk),
                'sol_encoded_pk': encoded_pk,
                'sol_address': modules_db[encoded_pk]["sol_address"],
                'label': modules_db[encoded_pk]["label"],
                'proxy': modules_db[encoded_pk].get("proxy"),
                'module_info': module_info,
                'last': module_index + 1 == len(modules_db[encoded_pk]["modules"])
            }
            for encoded_pk in modules_db
            for module_index, module_info in enumerate(modules_db[encoded_pk]["modules"])
            if (
                    module_info["status"] == "to_run" and
                    (not unique_wallets or module_index + 1 == len(modules_db[encoded_pk]["modules"]))
            )
        ]
        if SHUFFLE_WALLETS:
            shuffle(all_wallets_modules)
        return all_wallets_modules


    async def remove_module(self, module_data: dict):
        async with self.lock:
            with open(self.modules_db_name, encoding="utf-8") as f: modules_db = json.load(f)

            for index, module in enumerate(modules_db[module_data["sol_encoded_pk"]]["modules"]):
                if module["module_name"] == module_data["module_info"]["module_name"] and module["status"] == "to_run":
                    self.window_name.add_module()

                    if module_data["module_info"]["status"] in [True, "completed"]:
                        modules_db[module_data["sol_encoded_pk"]]["modules"].remove(module)
                    else:
                        modules_db[module_data["sol_encoded_pk"]]["modules"][index]["status"] = "failed"
                    break

            if [
                module["status"]
                for module in modules_db[module_data["sol_encoded_pk"]]["modules"]
            ].count('to_run') == 0:
                self.window_name.add_acc()
            if not modules_db[module_data["sol_encoded_pk"]]["modules"]:
                del modules_db[module_data["sol_encoded_pk"]]

            with open(self.modules_db_name, 'w', encoding="utf-8") as f: json.dump(modules_db, f)

    async def remove_account(self, module_data: dict):
        async with self.lock:
            with open(self.modules_db_name, encoding="utf-8") as f: modules_db = json.load(f)

            if module_data["module_info"]["status"] in [True, "completed"]:
                del modules_db[module_data["sol_encoded_pk"]]
                self.window_name.add_acc()

                with open(self.modules_db_name, 'w', encoding="utf-8") as f: json.dump(modules_db, f)


    async def append_report(self, key: str, text: str, success: bool = None):
        async with self.lock:
            status_smiles = {True: '✅ ', False: "❌ ", None: ""}

            with open(self.report_db_name, encoding="utf-8") as f: report_db = json.load(f)

            if not report_db.get(key): report_db[key] = {'texts': [], 'success_rate': [0, 0]}

            report_db[key]["texts"].append(status_smiles[success] + text)
            if success != None:
                report_db[key]["success_rate"][1] += 1
                if success == True: report_db[key]["success_rate"][0] += 1

            with open(self.report_db_name, 'w') as f: json.dump(report_db, f)


    async def get_account_reports(self, sol_encoded_pk: str, mode: int):
        async with self.lock:
            with open(self.report_db_name, encoding="utf-8") as f: report_db = json.load(f)

            sol_address = get_sol_address(self.decode_pk(pk=sol_encoded_pk))
            modules_done = self.increase_account_modules_done(address=sol_address)
            header_string = ""
            trade_amount = "\n\n"
            if (
                    (modules_done and modules_done[0] == modules_done[1]) or
                    not modules_done or
                    mode in [3]
            ):
                header_string = f"[{self.window_name.accs_done}/{self.window_name.accs_amount}] "
            if modules_done and mode != 3:
                trade_amount = f"\n📌 [Trade {modules_done[0]}/{modules_done[1]}]\n\n"
            title_text = f"{header_string}<b>{sol_address}</b>{trade_amount}"

            if report_db.get(sol_encoded_pk):
                account_reports = report_db[sol_encoded_pk]
                del report_db[sol_encoded_pk]
                with open(self.report_db_name, 'w', encoding="utf-8") as f: json.dump(report_db, f)

                logs_text = '\n'.join(account_reports['texts'])
                tg_text = f'{title_text}{logs_text}'
                if account_reports["success_rate"][1]:
                    tg_text += f'\n\nSuccess rate {account_reports["success_rate"][0]}/{account_reports["success_rate"][1]}'
                return tg_text

            else:
                return f'{title_text}No actions'
