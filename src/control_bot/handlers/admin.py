from aiogram import Router, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State

from src.config import settings
from src.database.connection import async_session_factory
from src.repositories.settings_repo import SettingsRepository
from src.control_bot.keyboards.reply import get_admin_reply_keyboard
from src.control_bot.keyboards.inline import get_main_menu_keyboard, get_back_keyboard

router = Router()

class PromptStates(StatesGroup):
    waiting_for_prompt = State()

def is_admin(user_id: int) -> bool:
    return user_id == settings.ADMIN_TELEGRAM_ID

@router.message(Command("start"))
async def cmd_start(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    async with async_session_factory() as session:
        repo = SettingsRepository(session)
        is_enabled = await repo.is_enabled()

    await message.answer(
        "👋 <b>Добро пожаловать в панель управления OpenVK AI Agent!</b>",
        reply_markup=get_admin_reply_keyboard(),
        parse_mode="HTML"
    )
    await message.answer(
        "Главное меню:",
        reply_markup=get_main_menu_keyboard(is_enabled),
        parse_mode="HTML"
    )

@router.callback_query(F.data == "main_menu")
async def cb_main_menu(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return

    await state.clear()
    async with async_session_factory() as session:
        repo = SettingsRepository(session)
        is_enabled = await repo.is_enabled()

    await callback.message.edit_text(
        "Главное меню:",
        reply_markup=get_main_menu_keyboard(is_enabled),
        parse_mode="HTML"
    )
    await callback.answer()

@router.callback_query(F.data == "toggle_ai")
async def cb_toggle_ai(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return

    async with async_session_factory() as session:
        repo = SettingsRepository(session)
        new_status = await repo.toggle_enabled()
        await session.commit()

    await callback.message.edit_reply_markup(
        reply_markup=get_main_menu_keyboard(new_status)
    )
    await callback.answer(f"Статус изменен: {'Включен' if new_status else 'Выключен'}")

@router.callback_query(F.data == "menu_status")
async def cb_menu_status(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return

    await _show_status(callback.message)
    await callback.answer()

@router.message(F.text == '📊 Статус')
async def msg_status(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    await _show_status(message)

async def _show_status(message: types.Message):
    async with async_session_factory() as session:
        repo = SettingsRepository(session)
        is_enabled = await repo.is_enabled()

    status_text = (
        "<b>📊 Статус системы:</b>\n\n"
        f"🤖 AI Бот: <b>{'Включен 🟢' if is_enabled else 'Выключен 🔴'}</b>\n"
        f"🌐 URL инстанса: <code>{settings.OVK_INSTANCE_URL}</code>\n"
        f"👤 ID пользователя: <code>{settings.OVK_USER_ID}</code>\n"
        f"⏱ Интервал опроса: <code>{settings.POLL_INTERVAL} сек.</code>\n"
        f"📡 Стратегия опроса: <code>{'Long Polling / Polling'}</code>\n"
    )

    if isinstance(message, types.Message):
        if message.from_user.id == message.chat.id: # not callback edit
            await message.answer(status_text, reply_markup=get_back_keyboard(), parse_mode="HTML")
        else: # callback edit
            await message.edit_text(status_text, reply_markup=get_back_keyboard(), parse_mode="HTML")

@router.callback_query(F.data == "menu_ovk_settings")
async def cb_menu_ovk_settings(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    await _show_ovk_settings(callback.message)
    await callback.answer()

@router.message(F.text == '⚙️ Настройки OVK')
async def msg_ovk_settings(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    await _show_ovk_settings(message)

async def _show_ovk_settings(message: types.Message):
    token_status = "Установлен ✅" if settings.OVK_ACCESS_TOKEN else "Не установлен ❌"
    
    text = (
        "<b>⚙️ Настройки OVK:</b>\n\n"
        f"URL инстанса: <code>{settings.OVK_INSTANCE_URL}</code>\n"
        f"ID бота: <code>{settings.OVK_USER_ID}</code>\n"
        f"Токен: <b>{token_status}</b>"
    )
    if isinstance(message, types.Message):
        if message.from_user.id == message.chat.id:
            await message.answer(text, reply_markup=get_back_keyboard(), parse_mode="HTML")
        else:
            await message.edit_text(text, reply_markup=get_back_keyboard(), parse_mode="HTML")

@router.callback_query(F.data == "menu_prompt")
async def cb_menu_prompt(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return

    async with async_session_factory() as session:
        repo = SettingsRepository(session)
        bot_settings = await repo.get_settings()
        current_prompt = bot_settings.system_prompt if bot_settings and bot_settings.system_prompt else "Не установлен"

    text = (
        f"<b>Текущий системный промпт:</b>\n<code>{current_prompt}</code>\n\n"
        "Отправьте новый текст промпта в ответном сообщении."
    )
    
    await callback.message.edit_text(text, reply_markup=get_back_keyboard(), parse_mode="HTML")
    await state.set_state(PromptStates.waiting_for_prompt)
    await callback.answer()

@router.message(F.text == '📝 Промпт')
async def msg_prompt(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    async with async_session_factory() as session:
        repo = SettingsRepository(session)
        bot_settings = await repo.get_settings()
        current_prompt = bot_settings.system_prompt if bot_settings and bot_settings.system_prompt else "Не установлен"

    text = (
        f"<b>Текущий системный промпт:</b>\n<code>{current_prompt}</code>\n\n"
        "Отправьте новый текст промпта в ответном сообщении."
    )
    
    await message.answer(text, reply_markup=get_back_keyboard(), parse_mode="HTML")
    await state.set_state(PromptStates.waiting_for_prompt)

@router.message(PromptStates.waiting_for_prompt)
async def process_prompt_update(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    new_prompt = message.text

    async with async_session_factory() as session:
        repo = SettingsRepository(session)
        await repo.update_settings(system_prompt=new_prompt)
        await session.commit()

    await state.clear()
    await message.answer("✅ Системный промпт успешно обновлен!", reply_markup=get_back_keyboard(), parse_mode="HTML")

@router.callback_query(F.data == "emergency_stop")
async def cb_emergency_stop(callback: types.CallbackQuery, redis):
    if not is_admin(callback.from_user.id):
        return

    await redis.set('ovk:bot:paused', '1')
    await callback.message.edit_text(
        "🚨 <b>АВАРИЙНАЯ ОСТАНОВКА АКТИВИРОВАНА</b> 🚨\n\nБот больше не обрабатывает сообщения.",
        reply_markup=get_back_keyboard(),
        parse_mode="HTML"
    )
    await callback.answer("Аварийная остановка активирована!")

@router.callback_query(F.data == "emergency_resume")
async def cb_emergency_resume(callback: types.CallbackQuery, redis):
    if not is_admin(callback.from_user.id):
        return

    await redis.delete('ovk:bot:paused')
    await callback.message.edit_text(
        "▶️ <b>Аварийная остановка снята.</b>\n\nБот снова обрабатывает сообщения.",
        reply_markup=get_back_keyboard(),
        parse_mode="HTML"
    )
    await callback.answer("Работа восстановлена!")
