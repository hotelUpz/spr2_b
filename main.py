from api import get_dex_prices, get_mexc_prices, TelegramNotifier
from utils import Utils
import asyncio
import aiohttp
# from collections import deque
from typing import Optional, Tuple, List, Dict
import traceback
import os

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANEL_ID = os.getenv("CHANEL_ID")

# Settings:
# ///////////
SYMBOLS_DATA = {
    "TIBBIR_USDT": ('base', '0x0c3b466104545efa096b8f944c1e524e1d0d4888'),
    "ZERO_USDT": ('linea', '0x0040f36784dda0821e74ba67f86e084d70d67a3a')
}
SYMBOLS = ["TIBBIR_USDT"]

# Timing:
interval_map = {
    "1m": 60,
    "3m": 180,
    "5m": 300,
    "10m": 600,
    "30m": 1800,
}
text_tfr = "1m"
TEXT_REFRESH_INTERVAL = interval_map[text_tfr]
PRICE_REFRESH_INTERVAL = len(SYMBOLS)* 2

# Strayegy:
WINDOW = 1440 # minute
HIST_SPREAD_LIMIT = 1_500
DIRECTION_MODE = 3 # 1 -- Long only, 2 -- Short only, 3 -- Long + Short:
DEVIATION = 0.89 # hvh
FIXED_THRESHOLD = {
    "is_active": False,
    "val": 3.0 # %
}
EXIT_THRESHOLD = 0.21
CALC_SPREAD_METHOD = 'a'

# Utils:
PLOT_WINDOW = 1440 # minute
MAX_RECONNECT_ATTEMPTS = 21
    
class NetworkServices():
    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None

    async def initialize_session(self):
        if not self.session or self.session.closed:
            self.session = aiohttp.ClientSession()

    async def _check_session_connection(self, session):
        try:
            async with session.get("https://api.mexc.com/api/v3/ping") as response:
                return response.status == 200
        except aiohttp.ClientError:
            return False

    async def validate_session(self):
        for attempt in range(1, MAX_RECONNECT_ATTEMPTS + 1):
            if self.session and not self.session.closed:
                if await self._check_session_connection(self.session):
                    return True
                try:
                    await self.session.close()
                except Exception as e:
                    print(f"Ошибка при закрытии сессии: {e}")

            await asyncio.sleep((attempt * 1.6) + 1)
            print(f"🔁 Попытка восстановить сессию ({attempt}/{MAX_RECONNECT_ATTEMPTS})...")
            await self.initialize_session()

        print("❌ Не удалось восстановить сессию после нескольких попыток.", True)
        return False

    async def shutdown_session(self):
        """Закрытие aiohttp-сессии при остановке."""
        if self.session and not self.session.closed:
            try:
                await self.session.close()
            except Exception as e:
                print(f"Ошибка при закрытии сессии в shutdown_session(): {e}")
    
class SignalProcessor:
    @staticmethod
    def hvh_spread_calc(spread_pct_data, last_spread):
        """
        Простой HVH-индикатор для списка кортежей (timestamp, spread)
        - spread_pct_data: list of tuples (timestamp, spread_value)
        - WINDOW: количество последних значений для анализа
        - DEVIATION: множитель отклонения (например, 0.89)
        Returns: 1 (long), -1 (short), 0 (нейтрально)
        """

        if (not FIXED_THRESHOLD["is_active"]) and (len(spread_pct_data) >= WINDOW):
            recent_spreads = spread_pct_data[-WINDOW:]
            last_positive = [val for val in recent_spreads if val > 0]
            last_negative = [val for val in recent_spreads if val < 0]
            highest = max(last_positive, default=0) * DEVIATION
            lowest = min(last_negative, default=0) * DEVIATION
        else:
            highest = FIXED_THRESHOLD["val"]
            lowest = -FIXED_THRESHOLD["val"]

        if lowest != 0 and last_spread < lowest:
            return 1
        elif highest != 0 and last_spread > highest:
            return -1
        
        return 0
    
    @staticmethod
    def is_exit_signal(current_spread: float) -> bool:
        return abs(current_spread) < EXIT_THRESHOLD

    def signals_collector(
        self,
        spread_data: list,
        current_spread: float,
        in_position_long: bool,
        in_position_short: bool
    ) -> Tuple[List[Tuple[str, str]], List[Tuple[str, str]], bool, bool]:
        """
        Возвращает два списка:
        - instructions_open: список сигналов на открытие
        - instructions_close: список сигналов на закрытие
        """
        instructions_open = []
        instructions_close = []

        if self.is_exit_signal(current_spread):
            if in_position_long:
                instructions_close.append(("LONG", "is_closing"))
                in_position_long = False
            if in_position_short:
                instructions_close.append(("SHORT", "is_closing"))
                in_position_short = False

        signal = self.hvh_spread_calc(spread_data, current_spread)
        if signal == 1 and not in_position_long:
            instructions_open.append(("LONG", "is_opening"))
            in_position_long = True
        elif signal == -1 and not in_position_short:
            instructions_open.append(("SHORT", "is_opening"))
            in_position_short = True

        return instructions_open, instructions_close, in_position_long, in_position_short

class DataFetcher:
    def __init__(self):
        self.utils = Utils(PLOT_WINDOW)
        self.signals = SignalProcessor()
        self.data = {}
        self._init_symbol_data()
        self.pairs: List[Tuple] = self.get_dex_pairs(self.data)

    def _init_symbol_data(self):
        for symbol in SYMBOLS:
            self.data[symbol] = {
                "spread_pct_data": [],
                "mexc_price": None,
                "dex_price": None,
                "spread_pct": None,
                "net_token": SYMBOLS_DATA[symbol][0],
                "token_address": SYMBOLS_DATA[symbol][1],
                "msg": None,
                "instruction_open": None,
                "instruction_close": None,
                "in_position_long": False,
                "in_position_short": False,
            }

    @staticmethod
    def get_dex_pairs(data):
        return [
            (info["net_token"], info["token_address"])
            for info in data.values()
            if info["net_token"] and info["token_address"]
        ]

    async def fetch_prices(self, session, symbols: List[str], pairs: List[Tuple]) -> Dict[str, Tuple[float, float]]:
        try:
            mexc_prices = await get_mexc_prices(session, symbols)
            dex_prices = await get_dex_prices(session, pairs)
            return {
                symbol: (mexc_prices.get(symbol), dex_prices.get((SYMBOLS_DATA[symbol][0], SYMBOLS_DATA[symbol][1])))
                for symbol in symbols
            }
        except Exception as e:
            raise RuntimeError(f"Ошибка при получении цен: {e}")

    async def refresh_data(self, session, is_spread_updated_time):
        try:
            prices = await self.fetch_prices(session, SYMBOLS, self.pairs)
            for symbol, (mexc_price, dex_price) in prices.items():
                symbol_data = self.data[symbol]

                if not (mexc_price and dex_price):
                    continue

                try:
                    spread_pct = self.utils.calc_spread(mexc_price, dex_price, CALC_SPREAD_METHOD)
                    symbol_data.update({
                        "mexc_price": mexc_price,
                        "dex_price": dex_price,
                        "spread_pct": spread_pct
                    })

                    if is_spread_updated_time:
                        symbol_data["spread_pct_data"].append(spread_pct)
                        if len(symbol_data["spread_pct_data"]) > HIST_SPREAD_LIMIT:
                            symbol_data["spread_pct_data"] = symbol_data["spread_pct_data"][-HIST_SPREAD_LIMIT:]


                    msg = f"\U0001F4E2 [{symbol.replace("_USDT", "")}]: Spread: {spread_pct:.4f} %"
                    in_position_long, in_position_short = symbol_data["in_position_long"], symbol_data["in_position_short"]
                    instr_open, instr_close, in_position_long_ren, in_position_short_ren = self.signals.signals_collector(
                        symbol_data["spread_pct_data"], spread_pct, in_position_long, in_position_short
                    )

                    symbol_data.update({
                        "msg": msg,
                        "instruction_open": instr_open,
                        "instruction_close": instr_close,
                        "in_position_long": in_position_long_ren,
                        "in_position_short": in_position_short_ren,
                    })

                except Exception as ex:
                    print(f"[ERROR] refresh_data for symbol {symbol} failed: {ex}\n{traceback.format_exc()}")

        except Exception as ex:
            print(f"[ERROR] refresh_data: {ex}\n{traceback.format_exc()}")

class Main(DataFetcher):
    def __init__(self):
        super().__init__()  # ← Вызов конструктора родительского класса
        self.notifier = TelegramNotifier(
            token=BOT_TOKEN,
            chat_ids=[CHANEL_ID]  # твой chat_id или список chat_id'ов
        )
        self.connector = NetworkServices() 

    def reset_data(self):
        for symbol_data in self.data.values():
            symbol_data.update({
                "msg": None,
                "mexc_price": None,
                "dex_price": None,
                "spread_pct": None,
                "instruction_open": None,
                "instruction_close": None,
            })
        
    async def msg_collector(self, is_text_refresh_time: bool) -> None:
        """Collects and sends messages based on symbol data and conditions."""

        async def send_signal(msg, plot_bytes=None, auto_delete=None):
            await self.notifier.send(msg, photo_bytes=plot_bytes, auto_delete=auto_delete)

        def prepare_signal_message(symbol, symbol_data, position_side, action):
            mexc_price = symbol_data.get("mexc_price")
            dex_price = symbol_data.get("dex_price")
            token_address = symbol_data.get("token_address")
            net_token = symbol_data.get("net_token")
            spread_pct = symbol_data["spread_pct"]
            
            return self.utils.format_signal_message(
                symbol, position_side, action, spread_pct, mexc_price, dex_price, token_address, net_token
            ) 

        for symbol in SYMBOLS:
            symbol_data = self.data.get(symbol)

            try:
                spread_pct = symbol_data.get("spread_pct")
                spread_pct_data = symbol_data.get("spread_pct_data")
                instruction_open = symbol_data.get("instruction_open", [])
                instruction_close = symbol_data.get("instruction_close", [])
                is_instruction = bool(instruction_open) or bool(instruction_close)

                if spread_pct is None:
                    continue

                # Send regular update message if applicable
                if is_text_refresh_time:
                    msg = symbol_data.get("msg")                    
                    await send_signal(msg, plot_bytes=None, auto_delete=TEXT_REFRESH_INTERVAL + 2)

                if not is_instruction:
                    continue

                # Generate plot once if needed
                plot_bytes = self.utils.generate_plot_image(spread_pct_data, style=1)

                # Отправка сигналов на открытие
                for position_side, _ in instruction_open:
                    msg = prepare_signal_message(symbol, symbol_data, position_side, "is_opening")
                    await send_signal(msg, plot_bytes=plot_bytes)

                # Отправка сигналов на закрытие
                for position_side, _ in instruction_close:
                    msg = prepare_signal_message(symbol, symbol_data, position_side, "is_closing")
                    await send_signal(msg, plot_bytes=plot_bytes)

            except Exception as ex:
                print(f"[ERROR] msg_collector for symbol {symbol} failed: {ex}\n{traceback.format_exc()}")

    async def _run(self):
        await self.connector.initialize_session()
        if not await self.connector.validate_session():
            raise ConnectionError("Не удалось установить сессию.")  

        check_session_counter = 0   
        refresh_counter = 0

        session = self.connector.session
        
        while True:
            try:
                if check_session_counter == 120:
                    check_session_counter = 0
                    if not await self.connector.validate_session():
                        print("Ошибка: Сессия неактивна даже после реконнекта.")
                        await self.connector.shutdown_session()                        
                        await asyncio.sleep(900)
                        continue
                    
                    session = self.connector.session 

                if refresh_counter < PRICE_REFRESH_INTERVAL:                                
                    continue
                else:
                    refresh_counter = 0

                is_text_refresh_time = self.utils.is_new_interval(
                    refresh_interval=TEXT_REFRESH_INTERVAL
                )

                await self.refresh_data(session, is_text_refresh_time)
                await self.msg_collector(is_text_refresh_time)

            except Exception as ex:
                print(f"[ERROR] Inner loop: {ex}")
                traceback.print_exc()
                raise

            finally:
                refresh_counter += 1
                check_session_counter += 1
                self.reset_data()      
                await asyncio.sleep(1)

if __name__ == "__main__":
    print("Start Bot")
    try:
        asyncio.run(Main()._run())
    except KeyboardInterrupt:
        print("Остановка по Ctrl+C")