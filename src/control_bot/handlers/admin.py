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

from redis.asyncio import Redis

@router.message(Command("start"))
async def cmd_start(message: types.Message, redis: Redis):
    if not is_admin(message.from_user.id):
        return

    is_enabled = await SettingsRepository.is_enabled()
    img_gen = await redis.get('ovk:settings:image_generation')
    image_gen_enabled = (img_gen == b'1' or img_gen == '1')

    await message.answer(
        "<b>Панель управления ботом OpenVK</b>",
        reply_markup=get_admin_reply_keyboard(),
        parse_mode="HTML"
    )
    await message.answer(
        "Главное меню:",
        reply_markup=get_main_menu_keyboard(is_enabled, image_gen_enabled),
        parse_mode="HTML"
    )

@router.callback_query(F.data == "main_menu")
async def cb_main_menu(callback: types.CallbackQuery, state: FSMContext, redis: Redis):
    if not is_admin(callback.from_user.id):
        return

    await state.clear()
    is_enabled = await SettingsRepository.is_enabled()
    img_gen = await redis.get('ovk:settings:image_generation')
    image_gen_enabled = (img_gen == b'1' or img_gen == '1')

    await callback.message.edit_text(
        "Главное меню:",
        reply_markup=get_main_menu_keyboard(is_enabled, image_gen_enabled),
        parse_mode="HTML"
    )
    await callback.answer()

@router.callback_query(F.data == "toggle_ai")
async def cb_toggle_ai(callback: types.CallbackQuery, redis: Redis):
    if not is_admin(callback.from_user.id):
        return

    new_status = await SettingsRepository.toggle_enabled()
    img_gen = await redis.get('ovk:settings:image_generation')
    image_gen_enabled = (img_gen == b'1' or img_gen == '1')

    await callback.message.edit_reply_markup(
        reply_markup=get_main_menu_keyboard(new_status, image_gen_enabled)
    )
    await callback.answer(f"Статус изменен: {'Включен' if new_status else 'Выключен'}")

@router.callback_query(F.data == "toggle_image_gen")
async def cb_toggle_image_gen(callback: types.CallbackQuery, redis: Redis):
    if not is_admin(callback.from_user.id):
        return

    img_gen = await redis.get('ovk:settings:image_generation')
    new_status = not (img_gen == b'1' or img_gen == '1')
    await redis.set('ovk:settings:image_generation', '1' if new_status else '0')

    is_enabled = await SettingsRepository.is_enabled()

    await callback.message.edit_reply_markup(
        reply_markup=get_main_menu_keyboard(is_enabled, new_status)
    )
    await callback.answer(f"Генерация картинок: {'Включена' if new_status else 'Выключена'}")

@router.callback_query(F.data == "menu_status")
async def cb_menu_status(callback: types.CallbackQuery, redis: Redis):
    if not is_admin(callback.from_user.id):
        return

    await _show_status(callback.message, redis)
    await callback.answer()

@router.message(F.text == 'Статус')
async def msg_status(message: types.Message, redis: Redis):
    if not is_admin(message.from_user.id):
        return
    await _show_status(message, redis)

async def _show_status(message: types.Message, redis: Redis):
    is_enabled = await SettingsRepository.is_enabled()
    img_gen = await redis.get('ovk:settings:image_generation')
    image_gen_enabled = (img_gen == b'1' or img_gen == '1')

    status_text = (
        "<b>Статус работы:</b>\n\n"
        f"Бот: <b>{'Включен' if is_enabled else 'Выключен'}</b>\n"
        f"Генерация картинок: <b>{'Включена' if image_gen_enabled else 'Выключена'}</b>\n"
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


@router.message(F.text == 'Чёрный список')
async def msg_blacklist(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.clear()
    await message.answer(
        "<b>Управление чёрными списками:</b>\n\n"
        "• <b>Ручной ЧС:</b> Полностью блокирует реакцию бота на пользователя (игнорируются комментарии, упоминания, ЛС).\n"
        "• <b>Авто-ЧС:</b> Демонстрационный список пользователей, которые заблокировали бота (или у которых закрыт профиль/комменты). Бот заносит их туда автоматически при ошибках отправки.",
        reply_markup=get_blacklist_keyboard(),
        parse_mode="HTML"
    )

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


class BlacklistStates(StatesGroup):
    waiting_for_add_id = State()
    waiting_for_remove_id = State()


from src.repositories.blacklist_repo import BlacklistRepository
from src.control_bot.keyboards.inline import get_blacklist_keyboard, get_back_to_blacklist_keyboard


@router.callback_query(F.data == "menu_blacklist")
async def cb_menu_blacklist(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    await state.clear()
    await callback.message.edit_text(
        "<b>Управление чёрными списками:</b>\n\n"
        "• <b>Ручной ЧС:</b> Полностью блокирует реакцию бота на пользователя (игнорируются комментарии, упоминания, ЛС).\n"
        "• <b>Авто-ЧС:</b> Демонстрационный список пользователей, которые заблокировали бота (или у которых закрыт профиль/комменты). Бот заносит их туда автоматически при ошибках отправки.",
        reply_markup=get_blacklist_keyboard(),
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data == "blacklist_show")
async def cb_blacklist_show(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    users = await BlacklistRepository.get_blacklist()
    if not users:
        text = "Ручной черный список пуст."
    else:
        text = "<b>Ручной черный список:</b>\n\n"
        for i, u in enumerate(users, 1):
            reason = f" (причина: {u['reason']})" if u['reason'] else ""
            text += f"{i}. ID: <code>{u['vk_id']}</code>{reason}\n"
    await callback.message.edit_text(text, reply_markup=get_back_to_blacklist_keyboard(), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "blacklist_add")
async def cb_blacklist_add(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    await callback.message.edit_text(
        "Отправьте ID пользователя OpenVK, которого хотите добавить в ручной ЧС.\n"
        "Можно отправить в формате: <code>ID [причина]</code> (например, <code>12345 спамер</code>).",
        reply_markup=get_back_to_blacklist_keyboard(),
        parse_mode="HTML"
    )
    await state.set_state(BlacklistStates.waiting_for_add_id)
    await callback.answer()


@router.message(BlacklistStates.waiting_for_add_id)
async def process_blacklist_add(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    
    parts = message.text.strip().split(maxsplit=1)
    if not parts:
        await message.answer("Неверный формат. Попробуйте еще раз.")
        return
        
    try:
        vk_id = int(parts[0])
    except ValueError:
        await message.answer("ID должен быть числом. Попробуйте еще раз.")
        return
        
    reason = parts[1] if len(parts) > 1 else None
    added = await BlacklistRepository.add_to_blacklist(vk_id, reason)
    
    await state.clear()
    if added:
        await message.answer(f"Пользователь с ID <code>{vk_id}</code> добавлен в ручной ЧС.", reply_markup=get_back_to_blacklist_keyboard(), parse_mode="HTML")
    else:
        await message.answer(f"Запись для пользователя с ID <code>{vk_id}</code> обновлена.", reply_markup=get_back_to_blacklist_keyboard(), parse_mode="HTML")


@router.callback_query(F.data == "blacklist_remove")
async def cb_blacklist_remove(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    await callback.message.edit_text(
        "Отправьте ID пользователя OpenVK, которого хотите удалить из ручного ЧС.",
        reply_markup=get_back_to_blacklist_keyboard(),
        parse_mode="HTML"
    )
    await state.set_state(BlacklistStates.waiting_for_remove_id)
    await callback.answer()


@router.message(BlacklistStates.waiting_for_remove_id)
async def process_blacklist_remove(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
        
    try:
        vk_id = int(message.text.strip())
    except ValueError:
        await message.answer("ID должен быть числом. Попробуйте еще раз.")
        return
        
    removed = await BlacklistRepository.remove_from_blacklist(vk_id)
    await state.clear()
    if removed:
        await message.answer(f"Пользователь с ID <code>{vk_id}</code> удален из ручного ЧС.", reply_markup=get_back_to_blacklist_keyboard(), parse_mode="HTML")
    else:
        await message.answer(f"Пользователь с ID <code>{vk_id}</code> не найден в ЧС.", reply_markup=get_back_to_blacklist_keyboard(), parse_mode="HTML")


@router.callback_query(F.data == "blacklist_autoblocked")
async def cb_blacklist_autoblocked(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    users = await BlacklistRepository.get_auto_blocked()
    if not users:
        text = "Авто-ЧС пуст. Блокировок бота не зафиксировано."
    else:
        text = "<b>Список пользователей, заблокировавших бота (авто-ЧС):</b>\n\n"
        for i, vk_id in enumerate(users, 1):
            text += f"{i}. ID: <code>{vk_id}</code>\n"
    await callback.message.edit_text(text, reply_markup=get_back_to_blacklist_keyboard(), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "blacklist_clear_auto")
async def cb_blacklist_clear_auto(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    users = await BlacklistRepository.get_auto_blocked()
    for vk_id in users:
        await BlacklistRepository.remove_from_auto_blocked(vk_id)
    await callback.message.edit_text("Авто-ЧС успешно очищен.", reply_markup=get_back_to_blacklist_keyboard(), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "menu_stats")
async def cb_menu_stats(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    await _show_stats(callback.message)
    await callback.answer()


@router.message(F.text == 'Статистика')
async def msg_stats(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    await _show_stats(message)


async def _show_stats(message: types.Message):
    from src.repositories.stats_repo import StatsRepository
    from src.control_bot.keyboards.inline import get_stats_keyboard
    
    stats = await StatsRepository.get_stats()
    g = stats["global"]
    
    text = (
        "<b>📊 Статистика использования бота:</b>\n\n"
        f"• Всего текстовых ответов: <b>{g['total_text_requests']}</b>\n"
        f"• Сгенерировано картинок: <b>{g['total_image_requests']}</b>\n"
        f"  — <i>Качественный FLUX:</i> <b>{g['flux_success_count']}</b>\n"
        f"  — <i>Временный Sana (Сбой):</i> <b>{g['fallback_success_count']}</b>\n"
        f"• Поставлено лайков: <b>{g['total_likes_count']}</b>\n\n"
    )
    
    text += "<b>🏆 Топ-5 пользователей по тексту:</b>\n"
    if not stats["top_text"]:
        text += "<i>Нет данных</i>\n\n"
    else:
        for i, u in enumerate(stats["top_text"], 1):
            text += f"{i}. <a href='https://openvk.org/id{u['vk_id']}'>{u['first_name']} {u['last_name']}</a> — {u['count']} запр.\n"
        text += "\n"
        
    text += "<b>🖼 Топ-5 по генерации картинок:</b>\n"
    if not stats["top_image"]:
        text += "<i>Нет данных</i>\n\n"
    else:
        for i, u in enumerate(stats["top_image"], 1):
            text += f"{i}. <a href='https://openvk.org/id{u['vk_id']}'>{u['first_name']} {u['last_name']}</a> — {u['count']} изобр.\n"
        text += "\n"
        
    if isinstance(message, types.Message):
        if message.from_user.id == message.chat.id: # not callback edit
            await message.answer(text, reply_markup=get_stats_keyboard(), parse_mode="HTML", disable_web_page_preview=True)
        else: # callback edit
            await message.edit_text(text, reply_markup=get_stats_keyboard(), parse_mode="HTML", disable_web_page_preview=True)


@router.callback_query(F.data == "stats_clear")
async def cb_stats_clear(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    from src.repositories.stats_repo import StatsRepository
    await StatsRepository.clear_stats()
    await callback.message.edit_text("Статистика сброшена.", reply_markup=get_stats_keyboard(), parse_mode="HTML")
    await callback.answer("Статистика сброшена.")
