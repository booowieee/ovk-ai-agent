from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

def get_main_menu_keyboard(is_enabled: bool, image_gen_enabled: bool = False) -> InlineKeyboardMarkup:
    """
    Создает и возвращает главное инлайн меню.
    """
    ai_status_text = "Включен" if is_enabled else "Выключен"
    image_gen_status_text = "Включена" if image_gen_enabled else "Выключена"
    
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"AI Бот: {ai_status_text}", 
                    callback_data="toggle_ai"
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"🖼 Генерация картинок: {image_gen_status_text}", 
                    callback_data="toggle_image_gen"
                )
            ],
            [
                InlineKeyboardButton(
                    text="Настройки OVK", 
                    callback_data="menu_ovk_settings"
                )
            ],
            [
                InlineKeyboardButton(
                    text="Изменить промпт", 
                    callback_data="menu_prompt"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🚫 Чёрный список", 
                    callback_data="menu_blacklist"
                )
            ],
            [
                InlineKeyboardButton(
                    text="📊 Статистика", 
                    callback_data="menu_stats"
                )
            ],
            [
                InlineKeyboardButton(
                    text="Статус", 
                    callback_data="menu_status"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🚨 Аварийный Стоп", 
                    callback_data="emergency_stop"
                ),
                InlineKeyboardButton(
                    text="✅ Старт", 
                    callback_data="emergency_resume"
                )
            ]
        ]
    )
    return keyboard

def get_stats_keyboard() -> InlineKeyboardMarkup:
    """
    Создает клавиатуру для меню статистики.
    """
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🔄 Обновить", callback_data="menu_stats")
            ],
            [
                InlineKeyboardButton(text="« Назад", callback_data="main_menu")
            ]
        ]
    )
    return keyboard

def get_blacklist_keyboard() -> InlineKeyboardMarkup:
    """
    Создает клавиатуру для управления черными списками.
    """
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📋 Показать ЧС", callback_data="blacklist_show"),
                InlineKeyboardButton(text="➕ Добавить в ЧС", callback_data="blacklist_add")
            ],
            [
                InlineKeyboardButton(text="➖ Удалить из ЧС", callback_data="blacklist_remove")
            ],
            [
                InlineKeyboardButton(text="🤖 Кто меня заблокировал", callback_data="blacklist_autoblocked")
            ],
            [
                InlineKeyboardButton(text="🧹 Очистить авто-ЧС", callback_data="blacklist_clear_auto")
            ],
            [
                InlineKeyboardButton(text="« Назад", callback_data="main_menu")
            ]
        ]
    )
    return keyboard

def get_back_keyboard() -> InlineKeyboardMarkup:
    """
    Создает клавиатуру с кнопкой возврата в главное меню.
    """
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Назад", 
                    callback_data="main_menu"
                )
            ]
        ]
    )
    return keyboard


def get_back_to_blacklist_keyboard() -> InlineKeyboardMarkup:
    """
    Создает клавиатуру с кнопкой возврата в меню черного списка.
    """
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Назад", 
                    callback_data="menu_blacklist"
                )
            ]
        ]
    )
    return keyboard
