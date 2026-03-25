from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.db import Database
from bot.handlers.common import (
    format_hint_for_student,
    get_student_from_callback_or_notify,
    get_student_or_notify,
    get_task_id_or_reset,
    process_learning_attempt,
    remove_inline_keyboard,
    send_learning_answer_photo,
    send_learning_task,
    send_testing_task,
    send_theory_page,
)
from bot.handlers.states import StudentFlow
from bot.keyboards.common import (
    learning_after_answer_keyboard,
    learning_incorrect_keyboard,
    student_menu_keyboard,
    theory_keyboard,
    waiting_answer_keyboard,
)
from bot.keyboards.inline import student_topics_keyboard
from bot.services.formula_renderer import FormulaRenderer
from bot.services.gemini_client import GeminiClient

router = Router()


@router.message(F.text == "Режим обучения")
async def student_learning_mode(message: Message, state: FSMContext, db: Database) -> None:
    student = await get_student_or_notify(message, db)
    if not student:
        return

    topics = await db.list_topics()
    if not topics:
        await message.answer("В базе нет тем.")
        return

    await state.set_state(StudentFlow.choosing_topic)
    await state.update_data(pending_mode="learning")
    await message.answer("Выберите тему:", reply_markup=student_topics_keyboard(topics))


@router.callback_query(StudentFlow.choosing_topic, F.data.startswith("student_topic:"))
async def student_select_topic(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    if not callback.data or not callback.message:
        return

    student = await get_student_from_callback_or_notify(callback, db)
    if not student:
        return

    topic_id = int(callback.data.split(":", 1)[1])
    topics = await db.list_topics()
    topic = next((item for item in topics if item.id == topic_id), None)
    if not topic:
        await callback.answer("Тема не найдена", show_alert=True)
        return

    data = await state.get_data()
    pending_mode = str(data.get("pending_mode") or "")
    await state.update_data(selected_topic_id=topic_id, selected_topic_title=topic.title)

    if pending_mode == "learning":
        pages = await db.list_theory_pages(topic_id=topic_id)
        if not pages:
            await state.update_data(selected_topic_id=topic_id)
            await send_learning_task(callback.message, state, db)
            await callback.answer()
            return

        await state.set_state(StudentFlow.showing_theory)
        await state.update_data(theory_index=0)
        await send_theory_page(callback.message, pages, 0)
        await callback.answer()
        return

    solved_count = await db.count_student_answers_by_mode_and_topic(student.id, "testing", topic_id)
    if solved_count >= 10:
        await remove_inline_keyboard(callback.message)
        await state.clear()
        await callback.message.answer(
            f"Вы уже завершили тестирование по теме «{topic.title}» (10 из 10).",
            reply_markup=student_menu_keyboard(),
        )
        await callback.answer()
        return

    await state.update_data(selected_topic_id=topic_id)
    await send_testing_task(callback.message, state, db, student)
    await callback.answer()


@router.message(StudentFlow.showing_theory, F.text == "Следующая страница")
async def next_theory_page(message: Message, state: FSMContext, db: Database) -> None:
    data = await state.get_data()
    selected_topic_id = int(data.get("selected_topic_id", 0)) or None
    pages = await db.list_theory_pages(topic_id=selected_topic_id)
    if not pages:
        await message.answer(
            "Теория по выбранной теме пока не добавлена. Нажмите «Начать решение».",
            reply_markup=theory_keyboard(False),
        )
        return

    index = int(data.get("theory_index", 0)) + 1
    if index >= len(pages):
        await state.update_data(theory_index=max(len(pages) - 1, 0))
        await message.answer("Теория закончилась. Нажмите «Начать решение».", reply_markup=theory_keyboard(False))
        return

    await state.update_data(theory_index=index)
    await send_theory_page(message, pages, index)


@router.message(StudentFlow.showing_theory, F.text == "Начать решение")
async def start_solving_after_theory(message: Message, state: FSMContext, db: Database) -> None:
    data = await state.get_data()
    selected_topic_id = int(data.get("selected_topic_id", 0)) or None
    await send_learning_task(message, state, db, topic_id=selected_topic_id)


@router.callback_query(F.data == "learning:show_answer")
async def learning_show_answer(callback: CallbackQuery, state: FSMContext, renderer: FormulaRenderer) -> None:
    if not callback.message:
        return

    data = await state.get_data()
    answer_text = data.get("current_answer")
    if not answer_text:
        await callback.answer("Ответ для текущего задания недоступен", show_alert=True)
        return

    await send_learning_answer_photo(callback.message, str(answer_text), renderer)
    await callback.answer()


@router.message(F.text == "Следующее задание")
async def student_next_learning_task(message: Message, state: FSMContext, db: Database) -> None:
    data = await state.get_data()
    selected_topic_id = int(data.get("selected_topic_id", 0)) or None
    await send_learning_task(message, state, db, topic_id=selected_topic_id)


@router.message(F.text == "Завершить обучение")
async def student_finish_learning(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Режим обучения завершён.", reply_markup=student_menu_keyboard())


@router.message(F.text == "Режим тестирования")
async def student_testing_mode(message: Message, state: FSMContext, db: Database) -> None:
    student = await get_student_or_notify(message, db)
    if not student:
        return

    topics = await db.list_topics()
    if not topics:
        await message.answer("В базе нет тем.")
        return

    await state.set_state(StudentFlow.choosing_topic)
    await state.update_data(pending_mode="testing")
    await message.answer("Выберите тему:", reply_markup=student_topics_keyboard(topics))


@router.message(StudentFlow.waiting_learning_answer, F.photo)
@router.message(StudentFlow.waiting_learning_answer, F.document)
async def learning_answer_first_attempt(
    message: Message,
    state: FSMContext,
    db: Database,
    llm: GeminiClient,
    renderer: FormulaRenderer,
) -> None:
    await process_learning_attempt(message, state, db, llm, renderer, is_retry=False)


@router.message(StudentFlow.waiting_learning_retry_answer, F.photo)
@router.message(StudentFlow.waiting_learning_retry_answer, F.document)
@router.message(StudentFlow.learning_incorrect_options, F.photo)
@router.message(StudentFlow.learning_incorrect_options, F.document)
async def learning_answer_retry_attempt(
    message: Message,
    state: FSMContext,
    db: Database,
    llm: GeminiClient,
    renderer: FormulaRenderer,
) -> None:
    await process_learning_attempt(message, state, db, llm, renderer, is_retry=True)


@router.message(StudentFlow.learning_incorrect_options, F.text == "Подсказка")
async def show_hint(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    hint = str(data.get("current_hint") or "Подсказка пока не добавлена для этого задания.")
    await message.answer(format_hint_for_student(hint), reply_markup=learning_incorrect_keyboard())


@router.message(StudentFlow.waiting_testing_answer, F.photo)
@router.message(StudentFlow.waiting_testing_answer, F.document)
async def testing_answer_photo(message: Message, state: FSMContext, db: Database, llm: GeminiClient) -> None:
    student = await get_student_or_notify(message, db)
    if not student:
        return

    from bot.handlers.common import check_student_answer, extract_image_file_id, finish_progress_message

    file_id = extract_image_file_id(message)
    if not file_id:
        await message.answer("Нужно отправить изображение (фото или документ-картинку).")
        return

    task_id = await get_task_id_or_reset(message, state)
    if task_id is None:
        return

    state_data = await state.get_data()
    expected_answer = str(state_data.get("current_answer") or "").strip()
    if not expected_answer:
        await message.answer("Не удалось найти эталонный ответ для проверки. Попробуйте другое задание.")
        return

    progress_message = await message.answer("⏳ Проверяю фото ответа…")
    current_task_text = str(state_data.get("current_task_text") or f"Задание #{task_id}")
    check = await check_student_answer(message, llm, file_id, expected_answer, current_task_text)
    await finish_progress_message(progress_message, check)
    if check is None:
        return

    if check.verdict == "unreadable":
        await message.answer(
            "Не удалось уверенно распознать ответ на фото. Пожалуйста, перефотографируйте и отправьте ещё раз.",
            reply_markup=waiting_answer_keyboard(),
        )
        return

    await db.save_answer(student.id, task_id, "testing", answer_image_file_id=file_id, is_correct=(check.verdict == "correct"))

    selected_topic_id = int(state_data.get("selected_topic_id", 0))
    total_topic_tasks = await db.count_tasks_by_teacher_mode_topic(student.teacher_id, "testing", selected_topic_id)
    total_target = min(10, total_topic_tasks)
    solved_count = await db.count_student_answers_by_mode_and_topic(student.id, "testing", selected_topic_id)
    if total_target > 0 and solved_count >= total_target:
        await state.clear()
        await message.answer(
            f"Тестирование завершено: {solved_count} из {total_target} задач отправлены.",
            reply_markup=student_menu_keyboard(),
        )
        return

    await send_testing_task(message, state, db, student)


@router.message(StudentFlow.waiting_learning_answer, F.text == "Пропустить задание")
@router.message(StudentFlow.waiting_learning_retry_answer, F.text == "Пропустить задание")
@router.message(StudentFlow.learning_incorrect_options, F.text == "Пропустить задание")
@router.message(StudentFlow.waiting_testing_answer, F.text == "Пропустить задание")
async def skip_task(message: Message, state: FSMContext, db: Database) -> None:
    student = await get_student_or_notify(message, db)
    if not student:
        return

    state_value = await state.get_state()
    task_id = await get_task_id_or_reset(message, state)
    if task_id is None:
        return

    mode = "testing" if state_value == StudentFlow.waiting_testing_answer.state else "learning"
    await db.save_answer(student.id, task_id, mode, answer_image_file_id=None, is_correct=False, is_skipped=True)

    if mode == "learning":
        state_data = await state.get_data()
        selected_topic_id = int(state_data.get("selected_topic_id", 0)) or None
        await state.clear()
        if selected_topic_id is not None:
            await state.update_data(selected_topic_id=selected_topic_id)
        await message.answer("Задание пропущено.", reply_markup=learning_after_answer_keyboard())
        return

    state_data = await state.get_data()
    selected_topic_id = int(state_data.get("selected_topic_id", 0))
    total_topic_tasks = await db.count_tasks_by_teacher_mode_topic(student.teacher_id, "testing", selected_topic_id)
    total_target = min(10, total_topic_tasks)
    solved_count = await db.count_student_answers_by_mode_and_topic(student.id, "testing", selected_topic_id)
    if total_target > 0 and solved_count >= total_target:
        await state.clear()
        await message.answer(
            f"Тестирование завершено: {solved_count} из {total_target} задач обработаны.",
            reply_markup=student_menu_keyboard(),
        )
        return

    await send_testing_task(message, state, db, student)


@router.message(StudentFlow.waiting_learning_answer)
@router.message(StudentFlow.waiting_learning_retry_answer)
@router.message(StudentFlow.learning_incorrect_options)
@router.message(StudentFlow.waiting_testing_answer)
async def waiting_photo_only(message: Message) -> None:
    await message.answer("Пожалуйста, отправьте фото ответа или нажмите «Пропустить задание».")
