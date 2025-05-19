import aiohttp
import asyncio
from typing import Optional, Union

BASE_URL_MEXC = "https://contract.mexc.com"
BASE_URL_DEX = "https://api.dexscreener.com"

async def get_mexc_prices(session: aiohttp.ClientSession, symbols: list):
    """Получение последней цены фьючерса с MEXC по символу."""
    url = f"{BASE_URL_MEXC}/api/v1/contract/ticker"
    price_data = {}

    try:
        async with session.get(url) as response:
            if response.status == 200:
                data = await response.json()
                for s in data.get("data", []):
                    symbol_name = s.get("symbol")
                    if symbol_name in symbols and s.get("lastPrice") is not None:
                        # print("symbol_name in symbols")
                        price_data[symbol_name] = float(s["lastPrice"])               
                return price_data
            else:
                print(f"Ошибка запроса (MEXC): {response.status}, {await response.text()}")
    except Exception as e:
        print(f"Ошибка при получении данных с MEXC: {e}")

    return None

async def get_dex_prices(session: aiohttp.ClientSession, pairs: list[tuple[str, str]]) -> dict:
    """
    Получить цены по списку пар (net_token, token_address) с Dexscreener.
    Возвращает словарь: {(net_token, token_address): price}
    """

    async def fetch_price(net_token, token_address):
        url = f"{BASE_URL_DEX}/latest/dex/pairs/{net_token}/{token_address}"
        try:
            async with session.get(url) as response:
                if response.status == 200:
                    data = await response.json()
                    price = (
                        float(data["pairs"][0]["priceUsd"])
                        if data.get("pairs") and data["pairs"][0].get("priceUsd")
                        else None
                    )
                    return ((net_token, token_address), price)
                else:
                    print(f"[DEX ERROR] {response.status} for {net_token}/{token_address}")
        except Exception as e:
            print(f"[DEX EXCEPTION] {net_token}/{token_address}: {e}")
        return ((net_token, token_address), None)
    
    results = (
        await asyncio.gather(*[fetch_price(*pair) for pair in pairs])
        if len(pairs) > 1
        else [await fetch_price(*pairs[0])]
    )

    return {key: price for key, price in results if price is not None}

class TelegramNotifier:
    def __init__(self, token: str, chat_ids: list[int]):
        self.token = token
        self.chat_ids = chat_ids
        self.base_tg_url = f"https://api.telegram.org/bot{self.token}"
        self.send_text_endpoint = "/sendMessage"
        self.send_photo_endpoint = "/sendPhoto"
        self.delete_msg_endpoint = "/deleteMessage"

    async def _schedule_delete(self, chat_id: int, message_id: int, delay: Union[int, float]):
        await asyncio.sleep(delay)
        url = f"{self.base_tg_url}{self.delete_msg_endpoint}"
        params = {
            "chat_id": chat_id,
            "message_id": message_id
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, data=params) as resp:
                    if resp.status != 200:
                        print(f"Ошибка удаления сообщения: {await resp.text()}")
        except Exception as e:
            print(f"Ошибка при удалении сообщения: {e}")

    async def send(self, text: str, photo_bytes: bytes = None, auto_delete: Optional[Union[int, float]] = None):
        async with aiohttp.ClientSession() as session:
            for chat_id in self.chat_ids:
                if photo_bytes:
                    caption = str(text) if text is not None else ""
                    url = self.base_tg_url + self.send_photo_endpoint
                    data = aiohttp.FormData()
                    data.add_field("chat_id", str(chat_id))
                    data.add_field("caption", caption)
                    data.add_field("parse_mode", "HTML")
                    data.add_field("disable_web_page_preview", "true")
                    data.add_field("disable_notification", str(bool(auto_delete)).lower())
                    data.add_field("photo", photo_bytes, filename="spread.png", content_type="image/png")
                elif text:
                    url = self.base_tg_url + self.send_text_endpoint
                    data = {
                        "chat_id": chat_id,
                        "text": text,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                        "disable_notification": bool(auto_delete)
                    }
                else:
                    return

                try:
                    async with session.post(url, data=data) as resp:
                        if resp.status != 200:
                            print(f"Ошибка отправки сообщения: {await resp.text()}")
                            continue
                        response_json = await resp.json()
                        message_id = response_json.get("result", {}).get("message_id")

                        # Планируем удаление, если указано время
                        if auto_delete and message_id:
                            asyncio.create_task(self._schedule_delete(chat_id, message_id, auto_delete))
                except Exception as e:
                    print(f"Ошибка при запросе Telegram API: {e}")