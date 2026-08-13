from aiogram import Router, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State

from src.config import settings
from src.repositories.settings_repo import SettingsRepository
from src.control_bot.keyboards.reply import get_admin_reply_keyboard
from src.control_bot.keyboards.inline import get_main_menu_keyboard, get_back_keyboard

router = Router()

class PromptStates(StatesGroup):
    waiting_for_prompt = State()

from src.utils.logger import logger

def is_admin(user_id: int) -> bool:
    is_adm = user_id == settings.ADMIN_TELEGRAM_ID
    if not is_adm:
        logger.info(f"[Telegram Bot] Message from non-admin user (ID: {user_id}). Ignored. Configured admin ID is: {settings.ADMIN_TELEGRAM_ID}")
    else:
        logger.info(f"[Telegram Bot] Message verified from admin user (ID: {user_id}).")
    return is_adm

@router.message(Command("start"))
async def cmd_start(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    is_enabled = await SettingsRepository.is_enabled()

    await message.answer(
        "<b>Панель управления ботом OpenVK</b>",
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
    is_enabled = await SettingsRepository.is_enabled()

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

    new_status = await SettingsRepository.toggle_enabled()

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

@router.message(F.text == 'Статус')
async def msg_status(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    await _show_status(message)

async def _show_status(message: types.Message):
    is_enabled = await SettingsRepository.is_enabled()

    status_text = (
        "<b>Статус работы:</b>\n\n"
        f"Бот: <b>{'Включен' if is_enabled else 'Выключен'}</b>\n"
        f"Адрес: <code>{settings.OVK_INSTANCE_URL}</code>\n"
        f"ID бота: <code>{settings.OVK_USER_ID}</code>\n"
        f"Интервал опроса: <code>{settings.POLL_INTERVAL} сек.</code>\n"
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

@router.message(F.text == 'Настройки OVK')
async def msg_ovk_settings(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    await _show_ovk_settings(message)

async def _show_ovk_settings(message: types.Message):
    token_status = "Установлен" if settings.OVK_ACCESS_TOKEN else "Не установлен"
    
    text = (
        "<b>Настройки OVK:</b>\n\n"
        f"Адрес инстанса: <code>{settings.OVK_INSTANCE_URL}</code>\n"
        f"ID бота: <code>{settings.OVK_USER_ID}</code>\n"
        f"Токен: <b>{token_status}</b>"
    )
    if isinstance(message, types.Message):
        if message.from_user.id == message.chat.id:
            await message.answer(text, reply_markup=get_back_keyboard(), parse_mode="HTML")
        else:
            await message.edit_text(text, reply_markup=get_back_keyboard(), parse_mode="HTML")

import html

def format_prompt_preview(prompt: str, max_len: int = 300) -> str:
    if not prompt:
        return "Не установлен"
    if len(prompt) > max_len:
        return html.escape(prompt[:max_len]) + "..."
    return html.escape(prompt)

@router.callback_query(F.data == "menu_prompt")
async def cb_menu_prompt(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return

    bot_settings = await SettingsRepository.get_settings()
    raw_prompt = bot_settings.system_prompt if bot_settings else ""
    preview = format_prompt_preview(raw_prompt)

    text = (
        f"<b>Текущий системный промпт:</b>\n<code>{preview}</code>\n\n"
        "Отправьте новый текст промпта в ответном сообщении."
    )
    
    await callback.message.edit_text(text, reply_markup=get_back_keyboard(), parse_mode="HTML")
    await state.set_state(PromptStates.waiting_for_prompt)
    await callback.answer()

@router.message(F.text == 'Промпт')
async def msg_prompt(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    bot_settings = await SettingsRepository.get_settings()
    raw_prompt = bot_settings.system_prompt if bot_settings else ""
    preview = format_prompt_preview(raw_prompt)

    text = (
        f"<b>Текущий системный промпт:</b>\n<code>{preview}</code>\n\n"
        "Отправьте новый текст промпта в ответном сообщении."
    )
    
    await message.answer(text, reply_markup=get_back_keyboard(), parse_mode="HTML")
    await state.set_state(PromptStates.waiting_for_prompt)

@router.message(PromptStates.waiting_for_prompt)
async def process_prompt_update(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    new_prompt = message.text

    await SettingsRepository.update_settings(system_prompt=new_prompt)

    await state.clear()
    await message.answer("Системный промпт обновлен.", reply_markup=get_back_keyboard(), parse_mode="HTML")

@router.callback_query(F.data == "emergency_stop")
async def cb_emergency_stop(callback: types.CallbackQuery, redis):
    if not is_admin(callback.from_user.id):
        return

    await redis.set('ovk:bot:paused', '1')
    await callback.message.edit_text(
        "<b>Аварийная остановка активирована.</b>\n\nБот больше не обрабатывает сообщения.",
        reply_markup=get_back_keyboard(),
        parse_mode="HTML"
    )
    await callback.answer("Аварийная остановка активирована.")

@router.callback_query(F.data == "emergency_resume")
async def cb_emergency_resume(callback: types.CallbackQuery, redis):
    if not is_admin(callback.from_user.id):
        return

    await redis.delete('ovk:bot:paused')
    await callback.message.edit_text(
        "<b>Аварийная остановка снята.</b>\n\nБот снова обрабатывает сообщения.",
        reply_markup=get_back_keyboard(),
        parse_mode="HTML"
    )
    await callback.answer("Работа восстановлена.")
