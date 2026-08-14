from aiogram.types import ReplyKeyboardMarkup, KeyboardButton

def get_admin_reply_keyboard() -> ReplyKeyboardMarkup:
    """
    Создает и возвращает основную reply клавиатуру администратора.
    """
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text='Настройки OVK'),
                KeyboardButton(text='Промпт')
            ],
            [
                KeyboardButton(text='Чёрный список'),
                KeyboardButton(text='Статистика'),
                KeyboardButton(text='Статус')
            ]
        ],
        resize_keyboard=True
    )
    return keyboard
