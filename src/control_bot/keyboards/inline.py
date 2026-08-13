from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

def get_main_menu_keyboard(is_enabled: bool, image_gen_enabled: bool = False) -> InlineKeyboardMarkup:
    """
    Создает и возвращает главное инлайн меню.
    
    :param is_enabled: Текущий статус работы AI бота.
    :param image_gen_enabled: Текущий статус генерации изображений.
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
                    text="Статус", 
                    callback_data="menu_status"
                )
            ],
            [
                InlineKeyboardButton(
                    text="Аварийная остановка", 
                    callback_data="emergency_stop"
                )
            ],
            [
                InlineKeyboardButton(
                    text="Снять аварийную остановку", 
                    callback_data="emergency_resume"
                )
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
