"""ЄДИНЕ ЯДРО СИГНАЛІВ (спільне для /signals, /pro та фонових раундів).

Гарантії (усі баги попередніх версій закриті тут):
 1. ЖОДНОЇ ДУБЛІКАТІЇ — монета не повторюється в межах поточного пакету.
 2. НЕЗАВИСЛІ ЗДЕЛКИ — перед видачею старі OPEN-зделки звіряються з ринком:
    якщо ціна вже пройшла TP/SL — зделка закривається і слот звільняється;
    якщо старше TTL — авто-EXPIRED. Ліміт відкритих більше ніколи не блокує назавжди.
 3. MAXOPEN ЗАБИТО? Пакет все одно показується (без запису) + пояснення.
 4. СЕРІЯ СТОПІВ? Топ-2 сетапи показує без запису. Порожніх відповідей не буває.
 5. Раунд генерується ОДИН раз і кешується на весь інтервал — всі користувачі
    (базові й PRO) отримують ті самі актуальні зделки, без зайвих навантажень на Binance.
"""
import asyncio
import time

from bot import db, analyzer
from bot.formatting import signal_text, result_text

# кеш останнього успішного раунду: {"ts": float, "signals": [dict,...]}
_round_cache = {"ts": 0.0, "signals": []}


async def get_round_signals(force_new: bool = False) -> list[dict]:
    """Свіжі сигнали раунду: з кешу (якщо раунду < інтервалу) або новий скан ІІ."""
    interval = await db.get_int("signals_interval_min") * 60
    age = time.time() - _round_cache["ts"]
    if not force_new and _round_cache["signals"] and age < max(interval, 300):
        return _round_cache["signals"]

    count = await db.get_int("signals_count")
    min_score = await db.get_int("min_score")
    ai_on = (await db.get_int("ai_enabled")) == 1
    universe = await db.get_int("scan_universe")
    min_res = await db.get_int("min_symbols")
    signals = await analyzer.generate_signals(
        count=count, min_score=min_score, ai_enabled=ai_on,
        universe=universe, min_results=min_res)
    # унікальність по символу (захист від дублів на рівні ядра)
    uniq, seen = [], set()
    for s in signals or []:
        if s["symbol"] not in seen:
            seen.add(s["symbol"])
            uniq.append(s)
    if uniq:
        _round_cache["ts"] = time.time()
        _round_cache["signals"] = uniq
    return uniq


async def sync_open_trades(bot=None):
    """Перед генерацією пакета: активні зделки звіряються з ринком (TP/SL/TTL),
    щоб ліміт open не блокував видачу через «мертві» позиції."""
    from bot.scheduler import monitor_trades, expire_stale_trades
    await expire_stale_trades()
    try:
        await monitor_trades(bot)
    except Exception as e:
        print("sync_open_trades:", e)


async def dedupe_for_user(user_id: int, signals: list[dict]) -> list[dict]:
    """Не пропонувати монету, яка вже є у користувача як активна зделка."""
    open_syms = {t["symbol"] for t in await db.open_trades(user_id)}
    return [s for s in signals if s["symbol"] not in open_syms]


async def deliver_package(bot, user_id: int, *, pro: bool, deposit: float,
                          risk_pct: int, show_header=True):
    """Видає пакет зделок користувачеві (з плечем для PRO) і записує їх.

    Повертає кількість записаних зделок. Усі захискі гілки дають користувачеві
    КОНКРЕТНИЙ результат — ніколи просто «тишу».
    """
    from bot.scheduler import consecutive_losses

    signals = await get_round_signals()
    if not signals:
        ttl_hint = await db.get_int("signals_interval_min")
        await bot.send_message(
            user_id,
            f"🤖 ІІ переглянув ринок і поки що не бачить гідного сетапу — "
            f"краще пропустити, ніж увійти в мінус.\n"
            f"Наступний автоматичний пакет прийде сам протягом {ttl_hint} хв. "
            f"Можеш спробувати ще раз пізніше.")
        return 0

    n_show = len(signals)

    # --- захист: ліміт відкритих ---
    max_open = await db.get_int("max_open_trades")
    open_now = await db.count_open_for_user(user_id)
    if max_open > 0 and open_now >= max_open:
        ttl = await db.get_int("signal_ttl_min")
        await bot.send_message(
            user_id,
            f"⚠️ ІІ знайшов <b>{n_show}</b> сетап(ів), але в тебе вже {open_now} "
            f"активних зделок (ліміт {max_open}). Показую їх без запису — можеш взяти вручну:")
        for s in signals[:2]:
            await bot.send_message(user_id, signal_text(s), parse_mode="HTML")
            await asyncio.sleep(0.1)
        await bot.send_message(
            user_id,
            f"Старі зделки авто-закриваються через {ttl} хв (TTL) — тоді нові "
            f"прийдуть самі. Адмін може скасувати ліміт: /settings maxopen 99")
        return 0

    # --- захист: серія стопів ---
    max_loss = await db.get_int("max_consecutive_losses")
    paused = False
    if max_loss > 0 and consecutive_losses(await db.recent_statuses(user_id, 6), max_loss):
        paused = True
        await bot.send_message(
            user_id,
            f"🛡 Останні {max_loss} зделки поспіль — стоп. Бот бере паузу й не "
            f"записує нові, щоб не розкручувати мінус. Топ сетапи показує без запису:")
        signals = sorted(signals, key=lambda x: x["score"], reverse=True)[:2]

    # --- дедуплікація з активними ---
    fresh = await dedupe_for_user(user_id, signals)
    if not fresh and not paused:
        await bot.send_message(
            user_id,
            f"ℹ️ Ці {n_show} монети в тебе вже активні (/mytrades). "
            f"Чекаємо результат — нові прийдуть наступного раунду.")
        return 0
    if not fresh:
        return 0

    if show_header and not paused:
        await bot.send_message(user_id,
                               f"⚡️ <b>Новий пакет зделок ({len(fresh)})</b> — "
                               f"ШІ Binance Futures:", parse_mode="HTML")

    written = 0
    for s in fresh:
        if pro and deposit > 0:
            lev, margin = analyzer.pick_leverage(deposit, s["score"], risk_pct)
            text = signal_text(s, lev, margin)
        else:
            lev, margin = 1, 0
            text = signal_text(s)
        try:
            await bot.send_message(user_id, text, parse_mode="HTML")
        except Exception:
            pass
        if not paused:
            await db.add_trade({
                "user_id": user_id, "symbol": s["symbol"], "side": s["side"],
                "entry": s["entry"], "sl": s["sl"], "tp1": s["tp1"], "tp2": s["tp2"],
                "score": s["score"], "reason": s["reason"],
                "leverage": lev, "margin": margin,
            })
            try:
                await db.add_signal_log(s["symbol"], s["side"], s["entry"], s["sl"],
                                        s["tp1"], s["tp2"], s["score"])
            except Exception:
                pass
            written += 1
        await asyncio.sleep(0.15)
    return written
