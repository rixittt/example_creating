from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.models import Task, Topic


def topics_keyboard(topics: list[Topic]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=topic.title, callback_data=f"teacher_topic:{topic.id}")] for topic in topics]
    )


def student_topics_keyboard(topics: list[Topic]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=topic.title, callback_data=f"student_topic:{topic.id}")] for topic in topics]
    )


def modes_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Обучение", callback_data="teacher_mode:learning")],
            [InlineKeyboardButton(text="Тестирование", callback_data="teacher_mode:testing")],
        ]
    )


def generated_review_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Подтвердить", callback_data="teacher_gen:approve")],
            [InlineKeyboardButton(text="🔁 Сгенерировать заново", callback_data="teacher_gen:regenerate")],
            [InlineKeyboardButton(text="⏭️ Пропустить", callback_data="teacher_gen:skip")],
            [InlineKeyboardButton(text="❌ Отменить", callback_data="teacher_gen:cancel")],
        ]
    )


def generated_regen_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔁 Сгенерировать заново", callback_data="teacher_gen:regenerate")],
            [InlineKeyboardButton(text="⏭️ Пропустить", callback_data="teacher_gen:skip")],
            [InlineKeyboardButton(text="❌ Отменить", callback_data="teacher_gen:cancel")],
        ]
    )


def learning_answer_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Показать ответ", callback_data="learning:show_answer")]]
    )


def pool_list_keyboard(tasks: list[Task], page: int, page_size: int = 10) -> InlineKeyboardMarkup:
    total_pages = max((len(tasks) - 1) // page_size + 1, 1)
    current_page = min(max(page, 0), total_pages - 1)

    start = current_page * page_size
    end = start + page_size
    current_tasks = tasks[start:end]

    rows = [
        [
            InlineKeyboardButton(
                text=f"#{task.id} | {task.topic_title} | {'обуч.' if task.mode == 'learning' else 'тест'}",
                callback_data=f"pool_open:{task.id}",
            )
        ]
        for task in current_tasks
    ]
    rows.append(
        [
            InlineKeyboardButton(text="⬅️", callback_data="pool_list_nav:prev"),
            InlineKeyboardButton(text=f"{current_page + 1}/{total_pages}", callback_data="pool_noop"),
            InlineKeyboardButton(text="➡️", callback_data="pool_list_nav:next"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def pool_nav_keyboard(index: int, total: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="⬅️", callback_data="pool_nav:prev"),
                InlineKeyboardButton(text=f"{index + 1}/{total}", callback_data="pool_noop"),
                InlineKeyboardButton(text="➡️", callback_data="pool_nav:next"),
            ],
            [InlineKeyboardButton(text="↩️ К списку", callback_data="pool_back")],
        ]
    )