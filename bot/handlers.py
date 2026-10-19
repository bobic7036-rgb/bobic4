"""Юзерські команди: /help, /signals, /pro, /deposit, /status, /pnl + адмін-команди."""
import asyncio
from aiogram import Router, F
from aiogram.types import Message
from aiogram.filters import Command, CommandObject
from bot import db, analyzer, signals_core
from bot.config import ADMIN_ID
from bot.formatting import signal_text, fmt_price
from bot.scheduler import send_signals_round

router = Router()


def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


async def has_any(user_id: int) -> bool:
    if is_admin(user_id):        # 👑 адміну все безкоштовно
        return True
    u = await db.get_user(user_id)
    now = db.time.time()
    return (u["base_until"] or 0) > now or (u["pro_until"] or 0) > now


async def is_pro(user_id: int) -> bool:
    if is_admin(user_id):        # 👑 адміну все безкоштовно
        return True
    u = await db.get_user(user_id)
    return (u["pro_until"] or 0) > db.time.time()


HELP_TEXT = """📖 <b>Довідка</b>

Я отримую дані з <b>Binance Futures</b>, а <b>безкоштовне ІІ</b> аналізує монети на 3 таймфреймах (15m/1h/4h): RSI, EMA, VWAP, обсяги, funding, ATR, підтримки/опори, стиски волатильності та «індекс бажання і страху». Тільки сетапи, які ІІ підтвердив одразу на всіх ТФ, перетворюються на зделку — решта ринку відсіюється.

<b>Кожна зделка містить:</b>
• Напрямок — 🟢 LONG / 🔴 SHORT
• Точку входу, стоп-лос (за рівнем підтримки/опору)
• Два тейк-профіти (R:R ≥ 1.5)
• % шансу від ІІ та пояснення «чому так піде»

<b>Як бот йде в плюс (а не в мінус):</b>
🛡 Self-learning ІІ: бот веде журнал усіх сигналів і сам підлаштовує фільтри під реальний винрейт
🛡 «Безлімітний» пошук: якщо сильних сетапів мало — ІІ розширює скан на 60+ монет і м'якше знижує планку, поки не знайде зделки
🛡 Час життя зделки — 180 хв: завислі позиції авто-закриваються (EXPIRED) і звільняють місце для нових сигналів
🛡 Стоп серій: після 2 стопів поспіль пауза + пропускаються лише топові сетапи
🛡 Фіксований ризик ~2% депозиту на кожну зделку (PRO) + штраф токсичним монетам, що вже ловили стопи
🛡 Вечірній AI-звіт із винрейтом /stats

<b>Підписки (оплата зірками ⭐ Telegram Stars):</b>
📦 <b>БАЗОВА</b> — сигнали за розкладом (кожні N хв) + команда /signals, бот сам повідомляє, чи спрацювала зделка: дійшла до ✅ ТЕЙКу або 🛑 СТОПУ.
💎 <b>ПРОФЕСІЙНА</b> — ВСЕ з базової (ті самі авто-розсилки кожні N хв + /signals) ПЛЮС:
• команда <b>/pro</b> — окремий вхід: ІІ видає готові зделки просто зараз;
• напиши /deposit 100 — бот сам підбере плече та маржу під кожну зделку;
• в кінці дня — AI-звіт: скільки заробив від депозиту, PnL і винрейт.
Оплата — натисни кнопку в /start, Telegram сам спише зірки, підписка активується миттєво.

<b>Команди:</b>
/start — головне меню та купівля підписки
/subscribe — обрати підписку
/signals — актуальні зделки просто зараз (обидві підписки)
/pro — PRO-пакет зделок + плече під депозит (лише ПРОФЕСІЙНА)
/deposit 500 — вказати депозит (для PRO, щоб ІІ підбирав плече)
/status — термін моїх підписок
/pnl — мій PnL за 24 год і загальний
/mytrades — останні зделки та їх результат
/help — ця довідка

⚠️ <i>Трейдинг — ризикований. Сигнали не є фінансовою порадою.</i>"""


@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(HELP_TEXT, parse_mode="HTML")


@router.message(Command("status"))
async def cmd_status(message: Message):
    u = await db.get_user(message.from_user.id)
    now = db.time.time()

    def until(ts):
        if message.from_user.id == ADMIN_ID:
            return "♾️ безкоштовно (адмін)"
        d = ts and (ts - now) // 86400
        return f"✅ діє ще {d} дн." if d and d >= 1 else "❌ немає"

    txt = (
        f"📊 <b>Твій статус</b>\n\n"
        f"📦 Базова: {until(u['base_until'])}\n"
        f"💎 Професійна: {until(u['pro_until'])}\n"
        f"💰 Депозит: {fmt_price(u['deposit'] or 0)} USDT"
    )
    await message.answer(txt, parse_mode="HTML")


@router.message(Command("deposit"))
async def cmd_deposit(message: Message, command: CommandObject):
    if not await is_pro(message.from_user.id):
        await message.answer("💎 Ця функція доступна лише з ПРОФЕСІЙНОЮ підпискою.\n/subscribe — оформити.")
        return
    try:
        dep = float((command.args or "").replace(",", "."))
        assert dep > 0
    except (ValueError, AssertionError):
        await message.answer("Формат: <code>/deposit 500</code> (у USDT).", parse_mode="HTML")
        return
    await db.set_deposit(message.from_user.id, dep)
    await message.answer(
        f"✅ Депозит {fmt_price(dep)} USDT збережено.\n"
        f"Тепер у кожній новій зделці бот підбере плече та маржу автоматично.\n"
        f"Щоб отримати зделки просто зараз — /signals"
    )


@router.message(Command("signals"))
async def cmd_signals(message: Message):
    """БАЗОВИЙ вхід: пакет зделок просто зараз (без плеча)."""
    if not await has_any(message.from_user.id):
        await message.answer("🔒 Сигнали доступні тільки по підписці.\n/subscribe — обрати план.")
        return
    pro = await is_pro(message.from_user.id)
    await message.answer("🤖 <b>ШІ аналізує Binance Futures</b> (15m/1h/4h, VWAP, "
                         "підтримки/опори, бажання і страх), зачекай...", parse_mode="HTML")
    try:
        await signals_core.sync_open_trades(message.bot)   # звільняємо слоти мерців
        u = await db.get_user(message.from_user.id)
        # PRO-користувач через /signals теж отримує плече (бо може не знати /pro)
        await signals_core.deliver_package(
            message.bot, message.from_user.id,
            pro=pro, deposit=u["deposit"] or 0,
            risk_pct=await db.get_int("risk_per_trade_pct"))
    except Exception as e:
        await message.answer(f"⚠️ Не вдалось отримати дані з Binance: {e}")


@router.message(Command("pro"))
async def cmd_pro(message: Message):
    """PRO-вхід: окрема команда — ІІ видає готові зделки + плече під депозит."""
    if not await is_pro(message.from_user.id):
        await message.answer("💎 Команда /pro доступна лише з ПРОФЕСІЙНОЮ підпискою.\n"
                             "/subscribe — оформити (оплата ⭐ зірками).")
        return
    if (await db.get_int("pro_signals_command")) != 1:
        await message.answer("⚙️ Вихід зделок по команді /pro вимкнено адміном. "
                             "Автоматичні розсилки працюють як зазвичай.")
        return
    await message.answer("💎 <b>PRO-режим:</b> ШІ шукає найсильніші сетапи та "
                         "підбирає плече під твій депозит, зачекай...", parse_mode="HTML")
    try:
        await signals_core.sync_open_trades(message.bot)
        u = await db.get_user(message.from_user.id)
        dep = u["deposit"] or 0
        if dep <= 0:
            await message.answer("💰 Щоб ІІ підібрав плече й маржу — вкажи депозит: "
                                 "<code>/deposit 100</code> (USDT).\n"
                                 "Поки що даю зделки без плеча:")
        await signals_core.deliver_package(
            message.bot, message.from_user.id,
            pro=True, deposit=dep,
            risk_pct=await db.get_int("risk_per_trade_pct"))
    except Exception as e:
        await message.answer(f"⚠️ Не вдалось отримати дані з Binance: {e}")


@router.message(Command("mytrades"))
async def cmd_mytrades(message: Message):
    trades = await db.recent_trades(message.from_user.id, 10)
    if not trades:
        await message.answer("У тебе ще немає зделок. /signals — отримати перші.")
        return
    lines = ["📜 <b>Останні зделки:</b>", ""]
    label = {"OPEN": "⏳ відкрита", "TP1": "✅ TP1 взято", "TP2": "🏆 TP2",
             "SL": "🛑 стоп", "EXPIRED": "⌛ прострочена (не зайшла ні в TP, ні в SL)"}
    for t in trades:
        coin = t["symbol"].replace("USDT", "")
        pnl = t["pnl"] or 0
        pnl_s = f" ({'+' if pnl >= 0 else ''}{pnl:.2f}$)" if pnl else ""
        lines.append(
            f"• {coin} {t['side']} @ {fmt_price(t['entry'])} — {label.get(t['status'], t['status'])}{pnl_s}"
        )
    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("pnl"))
async def cmd_pnl(message: Message):
    u = await db.get_user(message.from_user.id)
    dep = u["deposit"] or 0
    day = await db.day_pnl(message.from_user.id)
    tot = await db.total_pnl(message.from_user.id)
    pnl = day["s"] or 0
    pct = (pnl / dep * 100) if dep else 0
    txt = (
        f"💹 <b>PnL</b>\n\n"
        f"За 24 год: <b>{'+' if pnl >= 0 else ''}{pnl:.2f} USDT</b>"
        + (f" ({'+' if pct >= 0 else ''}{pct:.2f}% від депозиту)" if dep else "") + "\n"
        f"Всього: <b>{'+' if (tot['s'] or 0) >= 0 else ''}{(tot['s'] or 0):.2f} USDT</b>"
    )
    await message.answer(txt, parse_mode="HTML")


# ========================= АДМІН =========================

SETTINGS_MAP = {
    "interval": ("signals_interval_min", "інтервал зделок (хв)"),
    "count": ("signals_count", "кількість зделок за прохід"),
    "minscore": ("min_score", "мінімальний шанс (%)"),
    "starsbase": ("stars_base", "ціна БАЗОВОЇ (зірок ⭐)"),
    "basedays": ("base_days", "тривалість БАЗОВОЇ (днів)"),
    "starspro": ("stars_pro", "ціна ПРО (зірок ⭐)"),
    "prodays": ("pro_days", "тривалість ПРО (днів)"),
    "ai": ("ai_enabled", "безкоштовне ІІ-фільтр (1=увімкнено, 0=вимкнено)"),
    "maxopen": ("max_open_trades", "макс. одночасно відкритих зделок на юзера"),
    "stopseries": ("max_consecutive_losses", "пауза після N стопів поспіль (0=вимк.)"),
    "risk": ("risk_per_trade_pct", "ризик на зделку (% депозиту, PRO)"),
    "ttl": ("signal_ttl_min", "час життя зделки в хв (потім авто-EXPIRED і слот звільняється)"),
    "minres": ("min_symbols", "мінімум зделок на раунд (ІІ розширює пошук якщо менше)"),
    "universe": ("scan_universe", "скільки монет сканує ІІ за раунд"),
    "autotune": ("auto_relax", "self-learning: авто-підстройка фільтрів під винрейт (1/0)"),
    "proauto": ("pro_auto_rounds", "PRO: авто-розсилки зделок кожні N хв, як на базовій (1/0)"),
    "procmd": ("pro_signals_command", "PRO: команда /pro видає зделки просто зараз (1/0)"),
}


@router.message(Command("settings"))
async def cmd_settings(message: Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID:
        return
    args = (command.args or "").split()
    if len(args) == 2 and args[0].lower() in SETTINGS_MAP:
        key, desc = SETTINGS_MAP[args[0].lower()]
        try:
            val = int(float(args[1]))
            assert val >= 0
        except (ValueError, AssertionError):
            await message.answer("Значення має бути невід\\'ємним числом.")
            return
        await db.set_setting(key, val)
        await message.answer(f"✅ {desc} = {val}")
        return
    cur = await db._db.execute("SELECT key, value FROM settings")
    rows = await cur.fetchall()
    lines = ["⚙️ <b>Налаштування бота</b> (редагуються без коду):\n"]
    for r in rows:
        lines.append(f"• <code>{r['key']}</code> = <b>{r['value']}</b>")
    lines.append("\n<b>Змінити:</b>\n<code>/settings interval 30</code> — кожні 30 хв давати зделки\n"
                 "<code>/settings count 5</code> — 5 зделок за прохід\n"
                 "<code>/settings minscore 70</code> — мінімальний шанс 70%\n"
                 "<code>/settings starsbase 3</code> / <code>/settings basedays 30</code> — ціна (⭐) і тривалість БАЗОВОЇ\n"
                 "<code>/settings starspro 7</code> / <code>/settings prodays 30</code> — ціна (⭐) і тривалість ПРО\n"
                 "<code>/settings ai 1</code> — безкоштовне ІІ-фільтр (0 — вимкнути)\n"
                 "<code>/settings maxopen 6</code> — максимум активних зделок на юзера\n"
                 "<code>/settings stopseries 2</code> — пауза після N стопів поспіль\n"
                 "<code>/settings risk 2</code> — ризик % депозиту на зделку (PRO)\n"
                 "<code>/settings ttl 180</code> — хв час життя зделки (потім авто-EXPIRED, слот вільний)\n"
                 "<code>/settings minres 3</code> — мінімум зделок на раунд (ІІ тоді шукає ширше)\n"
                 "<code>/settings universe 60</code> — скільки монет сканує ІІ\n"
                 "<code>/settings autotune 1</code> — self-learning авто-підстройка фільтрів\n"
                 "<code>/settings proauto 1</code> — PRO: авто-зделки кожні N хв (як базова)\n"
                 "<code>/settings procmd 1</code> — PRO: команда /pro видає зделки зараз")
    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("stats"))
async def cmd_stats(message: Message):
    """Адмін: глобальний винрейт бота за 30 днів + журнал сигналів."""
    if message.from_user.id != ADMIN_ID:
        return
    st = await db.winrate_stats(None, days=30)
    sig = await db.signal_stats(days=30)
    ms = await db.get_int("min_score")
    cnt = await db.get_int("signals_count")
    ttl = await db.get_int("signal_ttl_min")
    await message.answer(
        f"📊 <b>Статистика бота (30 днів)</b>\n\n"
        f"<b>Твої зделки:</b>\n"
        f"✅ TP1: {st['tp1']} | 🏆 TP2: {st['tp2']} | 🛑 SL: {st['sl']}\n"
        f"Закрито: {st['total']} | Винрейт: <b>{st['winrate']}%</b>\n\n"
        f"<b>Журнал усіх сигналів ІІ:</b>\n"
        f"✅ TP1: {sig['tp1']} | 🏆 TP2: {sig['tp2']} | 🛑 SL: {sig['sl']}\n"
        f"Закрито: {sig['total']} | Винрейт сигналів: <b>{sig['winrate']}%</b>\n\n"
        f"⚙️ Зараз: планка {ms}%, {cnt} зделки/раунд, TTL {ttl} хв "
        f"(self-learning коригує їх сам)", parse_mode="HTML")


@router.message(Command("users"))
async def cmd_users(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    now = db.time.time()
    cur = await db._db.execute("SELECT * FROM users ORDER BY user_id")
    rows = await cur.fetchall()
    lines = [f"👥 <b>Користувачі ({len(rows)}):</b>\n"]
    for r in rows[:50]:
        b = "📦" if (r["base_until"] or 0) > now else "▫️"
        p = "💎" if (r["pro_until"] or 0) > now else "▫️"
        lines.append(f"{b}{p} <code>{r['user_id']}</code> @{r['username'] or '-'} dep={r['deposit'] or 0}")
    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("give"))
async def cmd_give(message: Message, command: CommandObject):
    """Адмін може вручну видати/забрати підписку: /give <user_id> base|pro [днів]"""
    if message.from_user.id != ADMIN_ID:
        return
    args = (command.args or "").split()
    if len(args) < 2:
        await message.answer("<code>/give 123456 base 30</code> — видати на 30 днів\n"
                             "<code>/give 123456 pro off</code> — забрати", parse_mode="HTML")
        return
    try:
        uid = int(args[0])
    except ValueError:
        await message.answer("user_id має бути числом.")
        return
    plan = args[1]
    if plan not in ("base", "pro"):
        return
    if len(args) > 2 and args[2] == "off":
        await db.revoke_subscription(uid, plan)
        await message.answer(f"❌ Підписку {plan} для {uid} забрано.")
        return
    days = int(args[2]) if len(args) > 2 else await db.get_int(f"{plan}_days")
    await db.set_subscription(uid, plan, days)
    await message.answer(f"✅ Видано {plan} на {days} днів для {uid}.")


@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID or not command.args:
        return
    subs = set()
    for plan in ("base", "pro"):
        for u in await db.active_subscribers(plan):
            subs.add(u["user_id"])
    n = 0
    for uid in subs:
        try:
            await message.bot.send_message(uid, command.args)
            n += 1
        except Exception:
            pass
    await message.answer(f"📣 Розіслано {n} активним підписникам.")


@router.message(Command("gennow"))
async def cmd_gennow(message: Message):
    """Примусово запустити раунд розсилки зделок зараз."""
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer("🚀 Запускаю раунд...")
    asyncio.create_task(send_signals_round(message.bot))
