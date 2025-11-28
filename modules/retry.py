from bs4 import BeautifulSoup
from loguru import logger
from time import sleep
import asyncio

from settings import RETRY

from requests.exceptions import JSONDecodeError as json_error1
from json.decoder import JSONDecodeError as json_error2


class CustomError(Exception): pass

class DataBaseError(Exception): pass

class RelayError(Exception): pass


def have_json(func):
    async def wrapper(*args, **kwargs):
        response = await func(*args, **kwargs)
        try:
            await response.json()
        except (json_error1, json_error2):
            error_msg = _get_text_error(await response.text)
            raise Exception(f'bad json response: {error_msg}')

        return response
    return wrapper


def _get_text_error(response_text: str):
    if "html" not in response_text.lower(): return response_text[:350].replace("\n", " ")

    response_error = BeautifulSoup(response_text, "lxml")
    return " ".join(response_error.text.replace('\n', ' ').split())[:350]



def async_retry(
        source: str,
        module_str: str = None,
        exceptions = Exception,
        retries: int = RETRY,
        not_except = (CustomError,),
        to_raise: bool = True,
        sleep_on_error: int = 2,
):
    def decorator(f):
        custom_module_str = f.__name__.replace('_', ' ').title() if not module_str else module_str
        async def newfn(*args, **kwargs):
            attempt = 0
            while attempt < retries:
                try:
                    return await f(*args, **kwargs)

                except not_except as e:
                    if to_raise: raise e.__class__(f'{custom_module_str}: {e}')
                    else: return False

                except exceptions as e:
                    try:
                        error_owner = args[0].sol_address if hasattr(args[0], "sol_address") else args[0].address
                    except:
                        error_owner = "Soft"

                    attempt += 1
                    logger.opt(colors=True).error(f'[-] {error_owner} <white>{source}</white> | {custom_module_str} | {e} [{attempt}/{retries}]')

                    if attempt == retries:
                        if to_raise: raise ValueError(f'{custom_module_str}: {e}')
                        else: return False

                    await asyncio.sleep(sleep_on_error)
        return newfn
    return decorator


def retry(
        source: str,
        module_str: str,
        exceptions,
        retries: int = RETRY,
        not_except: set = CustomError,
        to_raise: bool = True,
):
    def decorator(f):
        custom_module_str = f.__name__.replace('_', ' ').title() if not module_str else module_str
        def newfn(*args, **kwargs):
            attempt = 0
            while attempt < retries:
                try:
                    return f(*args, **kwargs)

                except not_except as e:
                    if to_raise: raise e.__class__(f'{custom_module_str}: {e}')
                    else: return False

                except exceptions as e:
                    try:
                        error_owner = args[0].sol_address if hasattr(args[0], "sol_address") else args[0].address
                    except:
                        error_owner = "Soft"

                    logger.error(f'[-] {error_owner} | {source} | {custom_module_str} | {e} [{attempt+1}/{retries}]')
                    attempt += 1

                    if attempt == retries:
                        if to_raise: raise ValueError(f'{custom_module_str}: {e}')
                        else: return False
                    else:
                        sleep(2)
        return newfn
    return decorator
